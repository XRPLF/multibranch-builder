"""Clone the base, merge every branch in order, record what happened in manifest.json."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .conf import BranchEntry
from .merge import (
    AI_RESOLVED, CONFLICT, MERGED, Resolver, ai_resolve, configure_registry_driver,
    merge_source_into_current,
)

MANIFEST_FILE = "manifest.json"
TREE_DIR = "rippled"
TRAILER_KEY = "Multibranch-Builder-Manifest"
DEFAULT_GIT_NAME = "multibranch-builder"
DEFAULT_GIT_EMAIL = "compose@xrplf.local"


class ComposeError(RuntimeError):
    """The tree could not be composed as described."""


@dataclass
class BranchOutcome:
    repo: str
    branch: str
    sha: str
    outcome: str
    rebase: bool = False


@dataclass
class Manifest:
    base: str
    base_sha: str
    branches: list[BranchOutcome] = field(default_factory=list)
    composed_sha: str = ""
    datagram: str | None = None
    target: str | None = None
    force_supported: str = "OFF"

    @property
    def failed(self) -> list[str]:
        return [f"{b.repo}@{b.branch}" for b in self.branches if b.outcome == CONFLICT]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, separators=(",", ":") if indent is None else None)

    def trailer(self) -> str:
        return f"{TRAILER_KEY}: {self.to_json()}"

    def markdown(self) -> str:
        """A per-branch outcome table for a job summary."""
        lines = [f"**base** `{self.base}` @ `{self.base_sha[:12]}`",
                 f"**composed** `{self.composed_sha[:12]}`"]
        if self.target:
            lines.append(f"**target** `{self.target}`")
        lines += ["", "| repo | branch | sha | outcome |", "|---|---|---|---|"]
        lines += [f"| {b.repo} | {b.branch} | `{b.sha[:12]}` | {b.outcome} |" for b in self.branches]
        return "\n".join(lines) + "\n"

    def write(self, workdir: str | Path) -> Path:
        path = Path(workdir) / MANIFEST_FILE
        path.write_text(self.to_json(indent=2) + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["branches"] = [BranchOutcome(**b) for b in data.get("branches", [])]
        return cls(**data)


def _run(cmd: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)


def _log(msg: str) -> None:
    print(f"[multibranch-builder] {msg}", flush=True)


def plan(branches: list[BranchEntry], datagram: BranchEntry | None = None) -> list[BranchEntry]:
    """Merge order: the datagram ref first when given, then the conf entries in order."""
    return ([datagram] if datagram else []) + list(branches)


def _fetch(src: str, entry: BranchEntry) -> str:
    """Add `entry`'s remote, fetch its branch, return the fetched sha."""
    _run(["git", "remote", "add", entry.remote, entry.url], cwd=src, check=False)
    fetch = _run(["git", "fetch", entry.remote, entry.branch], cwd=src, check=False)
    if fetch.returncode != 0:
        raise ComposeError(f"fetch of {entry.label} failed:\n{fetch.stderr.strip()}")
    return _run(["git", "rev-parse", "FETCH_HEAD"], cwd=src).stdout.strip()


def compose(base: BranchEntry, branches: list[BranchEntry], workdir: str | Path, *,
            datagram: BranchEntry | None = None, target: BranchEntry | None = None,
            force_supported: str = "OFF", resolver: Resolver = ai_resolve) -> Manifest:
    """Clone `base`, merge `plan(branches, datagram)` in order, write and return the manifest.

    Every branch is attempted even after a conflict so the manifest shows each outcome; a
    manifest with any `conflict` is written and then raised as ComposeError.
    """
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    src = str(workdir / TREE_DIR)
    if os.path.isdir(src):
        shutil.rmtree(src)
    _log(f"clone {base.label}")
    clone = _run(["git", "clone", base.url, src], cwd=str(workdir), check=False)
    if clone.returncode != 0:
        raise ComposeError(f"clone of {base.url} failed:\n{clone.stderr.strip()}")
    checkout = _run(["git", "checkout", "--quiet", base.branch], cwd=src, check=False)
    if checkout.returncode != 0:
        raise ComposeError(f"checkout of {base.branch} failed:\n{checkout.stderr.strip()}")
    _run(["git", "config", "user.name", os.environ.get("GIT_BOT_NAME") or DEFAULT_GIT_NAME], cwd=src)
    _run(["git", "config", "user.email", os.environ.get("GIT_BOT_EMAIL") or DEFAULT_GIT_EMAIL], cwd=src)
    configure_registry_driver(src)
    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=src).stdout.strip()

    manifest = Manifest(
        base=base.label, base_sha=base_sha,
        datagram=datagram.label if datagram else None,
        target=target.label if target else None,
        force_supported=force_supported,
    )
    for entry in plan(branches, datagram):
        sha = _fetch(src, entry)
        _log(f"merge {entry.label} @ {sha[:12]}")
        outcome = merge_source_into_current(src, entry.ref, entry.label, base.label, resolver)
        if outcome in (MERGED, AI_RESOLVED):
            commit = _run(["git", "commit", "--no-edit"], cwd=src, check=False)
            if commit.returncode != 0:
                raise ComposeError(f"commit of {entry.label} merge failed:\n{commit.stderr.strip()}")
        _log(f"  {entry.label}: {outcome}")
        manifest.branches.append(BranchOutcome(entry.slug, entry.branch, sha, outcome, entry.rebase))

    manifest.composed_sha = _run(["git", "rev-parse", "HEAD"], cwd=src).stdout.strip()
    manifest.write(workdir)
    _log(f"composed -> {manifest.composed_sha[:12]}")
    if manifest.failed:
        raise ComposeError("could not merge: " + ", ".join(manifest.failed)
                           + " — the tree is incomplete; refusing to build or push it")
    return manifest
