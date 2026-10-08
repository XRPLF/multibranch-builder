"""GPG-signed commit and token-authenticated push as the service identity or, with --operator, the
workstation's own git and gh login; refuses anything unsigned."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile

import requests

GITHUB_API = "https://api.github.com"
_TIMEOUT = 30


class SigningNotConfigured(RuntimeError):
    """The service identity, PAT or GPG key needed for a verified push is missing."""


def _run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)


def _log(msg: str) -> None:
    print(f"[git_push] {msg}", file=sys.stderr, flush=True)


def _service_identity_and_token() -> tuple[str, str, str]:
    """(name, email, pat) from GIT_BOT_NAME, GIT_BOT_EMAIL, GITHUB_BOT_PAT, read now."""
    name = os.environ.get("GIT_BOT_NAME")
    email = os.environ.get("GIT_BOT_EMAIL")
    token = os.environ.get("GITHUB_BOT_PAT")
    if not (name and email and token):
        raise SigningNotConfigured(
            "GITHUB_BOT_PAT, GIT_BOT_NAME and GIT_BOT_EMAIL must all be set to produce "
            "verified service-account commits."
        )
    return name, email, token


def _armored_key() -> str:
    """GIT_SIGNING_KEY as armored text; accepts the armor itself or its base64."""
    raw = os.environ.get("GIT_SIGNING_KEY")
    if not raw:
        raise SigningNotConfigured("GIT_SIGNING_KEY not set — cannot produce verified commits.")
    if raw.lstrip().startswith("-----BEGIN"):
        return raw
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception as e:
        raise SigningNotConfigured(f"Failed to decode GIT_SIGNING_KEY: {e}")


def setup_signing(repo_dir: str) -> str:
    """Import GIT_SIGNING_KEY and configure `repo_dir` to sign commits as the service identity."""
    service_name, service_email, _ = _service_identity_and_token()
    armored = _armored_key()

    fd, keyfile = tempfile.mkstemp(suffix=".asc")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(armored)
        _run(["gpg", "--batch", "--yes", "--import", keyfile], check=False)
    finally:
        os.unlink(keyfile)

    listing = _run(["gpg", "--list-secret-keys", "--with-colons", service_email], check=False)
    key_id = None
    for line in listing.stdout.splitlines():
        if line.startswith("fpr:"):
            parts = line.split(":")
            if len(parts) > 9 and parts[9]:
                key_id = parts[9]
                break
    if not key_id:
        raise SigningNotConfigured(f"No GPG secret key found for {service_email} after import.")

    _run(["git", "config", "user.name", service_name], cwd=repo_dir)
    _run(["git", "config", "user.email", service_email], cwd=repo_dir)
    _run(["git", "config", "user.signingkey", key_id], cwd=repo_dir)
    _run(["git", "config", "commit.gpgsign", "true"], cwd=repo_dir)
    _run(["git", "config", "gpg.program", "gpg"], cwd=repo_dir)

    _log(f"Local GPG signing configured (key {key_id[-16:]})")
    return key_id


def _global_git(key: str) -> str:
    return _run(["git", "config", "--global", "--get", key], check=False).stdout.strip()


def setup_operator_signing(repo_dir: str) -> str:
    """Configure `repo_dir` to sign as the operator: the global git identity and user.signingkey."""
    _operator_token()
    name, email, key_id = (_global_git(k) for k in ("user.name", "user.email", "user.signingkey"))
    if not (name and email and key_id):
        raise SigningNotConfigured(
            "--operator needs user.name, user.email and user.signingkey in the global git config."
        )
    for key, value in (("user.name", name), ("user.email", email), ("user.signingkey", key_id),
                       ("commit.gpgsign", "true")):
        _run(["git", "config", key, value], cwd=repo_dir)
    _log(f"Operator signing configured ({email})")
    return key_id


def _operator_token() -> str:
    """The token `gh` is logged in with."""
    result = _run(["gh", "auth", "token"], check=False)
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise SigningNotConfigured("--operator needs `gh auth login`; `gh auth token` returned nothing.")
    return token


def commit_all(repo_dir: str, message: str, *, include_untracked: bool = True,
               allow_empty: bool = False) -> str:
    """Stage changes and create one signed commit; return the new HEAD sha."""
    add_cmd = ["git", "add", "-A"] if include_untracked else ["git", "add", "-u"]
    _run(add_cmd, cwd=repo_dir)
    commit_cmd = ["git", "commit", "-m", message]
    if allow_empty:
        commit_cmd.insert(2, "--allow-empty")
    result = _run(commit_cmd, cwd=repo_dir, check=False)
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip())[:500]
        raise RuntimeError(f"git commit failed: {detail}")
    return _run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()


def head_is_signed(repo_dir: str) -> bool:
    return _run(["git", "verify-commit", "HEAD"], cwd=repo_dir, check=False).returncode == 0


def remote_sha(url: str, branch: str, token: str) -> str:
    """The sha `branch` has on the remote, or "" when the branch does not exist."""
    result = _run(["git", "ls-remote", url, f"refs/heads/{branch}"], check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {result.stderr.strip().replace(token, '***')[:500]}")
    first = result.stdout.split()
    return first[0] if first else ""


def push(repo_dir: str, owner: str, repo: str, branch: str, *, force: bool = False,
         operator: bool = False) -> None:
    """Push a signed HEAD to `owner/repo:branch` over token-authenticated HTTPS.

    `force` uses `--force-with-lease` pinned to the sha the branch has right now. `operator`
    authenticates with the `gh` login instead of GITHUB_BOT_PAT.
    """
    token = _operator_token() if operator else _service_identity_and_token()[2]
    if not head_is_signed(repo_dir):
        raise SigningNotConfigured("HEAD is not GPG-signed — refusing to push an unverified commit.")
    url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    cmd = ["git", "push"]
    if force:
        cmd.append(f"--force-with-lease=refs/heads/{branch}:{remote_sha(url, branch, token)}")
    cmd += [url, f"HEAD:refs/heads/{branch}"]
    result = _run(cmd, cwd=repo_dir, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip().replace(token, "***")[:500]
        raise RuntimeError(f"git push to {owner}/{repo}:{branch} failed: {detail}")


def commit_and_push(repo_dir: str, owner: str, repo: str, branch: str, message: str, *,
                    force: bool = False, include_untracked: bool = True) -> str:
    """Signed commit of all changes, then push; returns the new sha."""
    sha = commit_all(repo_dir, message, include_untracked=include_untracked)
    push(repo_dir, owner, repo, branch, force=force)
    return sha


def update_ref(owner: str, repo: str, ref: str, sha: str, *, force: bool = False,
               required: bool = True) -> dict | None:
    """Point `owner/repo:ref` at an existing `sha` through the GitHub REST API as the service PAT."""
    token = os.environ.get("GITHUB_BOT_PAT")
    if not token:
        if required:
            raise SigningNotConfigured("GITHUB_BOT_PAT not set — cannot update ref via the service account.")
        _log(f"No service PAT — skipping ref update {owner}/{repo}:{ref}")
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    created = requests.post(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs", headers=headers,
                            json={"ref": f"refs/heads/{ref}", "sha": sha}, timeout=_TIMEOUT)
    if created.status_code == 201:
        return created.json()
    if created.status_code != 422:
        created.raise_for_status()
    updated = requests.patch(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{ref}",
                             headers=headers, json={"sha": sha, "force": force}, timeout=_TIMEOUT)
    updated.raise_for_status()
    return updated.json()
