"""Clone the base, merge every branch in order, run the kind's prepare step, record it all in manifest.json."""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .conf import Config
from .merge import AI_RESOLVED, CONFLICT, MERGED, Resolver, ai_resolve, merge_source_into_current
from .targets.base import Kind

MANIFEST_FILE = "manifest.json"
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
    kind: str = "xrpld"
    tree_dir: str = "rippled"
    target: str | None = None
    options: dict = field(default_factory=dict)
    prepared: dict = field(default_factory=dict)

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
        lines = [f"**kind** `{self.kind}`",
                 f"**base** `{self.base}` @ `{self.base_sha[:12]}`",
                 f"**composed** `{self.composed_sha[:12]}`"]
        if self.target:
            lines.append(f"**target** `{self.target}`")
        lines += [f"**{key}** `{value}`" for key, value in self.prepared.items()]
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


def _fetch(src: str, entry) -> str:
    """Add `entry`'s remote, fetch its branch, return the fetched sha."""
    _run(["git", "remote", "add", entry.remote, entry.url], cwd=src, check=False)
    fetch = _run(["git", "fetch", entry.remote, entry.branch], cwd=src, check=False)
    if fetch.returncode != 0:
        raise ComposeError(f"fetch of {entry.label} failed:\n{fetch.stderr.strip()}")
    return _run(["git", "rev-parse", "FETCH_HEAD"], cwd=src).stdout.strip()


def compose(config: Config, workdir: str | Path, *, kind: Kind, options: dict[str, str],
            resolver: Resolver | None = None) -> Manifest:
    """Clone the base into `workdir/<kind.tree_dir>`, merge `kind.plan(...)` in order, run
    `kind.prepare`, write and return the manifest.

    Every branch is attempted even after a conflict so the manifest shows each outcome; a
    manifest with any `conflict` is written and then raised as ComposeError. A prepare failure
    is written as `prepared.error` with an empty `composed_sha` and raised the same way.
    """
    base = config.base
    resolver = resolver or functools.partial(ai_resolve, guide_path=kind.merge_guide)
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    src = str(workdir / kind.tree_dir)
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
    kind.configure_merge(src)
    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=src).stdout.strip()

    manifest = Manifest(
        base=base.label, base_sha=base_sha, kind=kind.name, tree_dir=kind.tree_dir,
        target=config.target.label if config.target else None, options=dict(options),
    )
    for entry in kind.plan(config.branches, options):
        sha = _fetch(src, entry)
        _log(f"merge {entry.label} @ {sha[:12]}")
        outcome = merge_source_into_current(src, entry.ref, entry.label, base.label, resolver)
        if outcome in (MERGED, AI_RESOLVED):
            commit = _run(["git", "commit", "--no-edit"], cwd=src, check=False)
            if commit.returncode != 0:
                raise ComposeError(f"commit of {entry.label} merge failed:\n{commit.stderr.strip()}")
        _log(f"  {entry.label}: {outcome}")
        manifest.branches.append(BranchOutcome(entry.slug, entry.branch, sha, outcome, entry.rebase))

    if manifest.failed:
        manifest.composed_sha = _run(["git", "rev-parse", "HEAD"], cwd=src).stdout.strip()
        manifest.write(workdir)
        raise ComposeError("could not merge: " + ", ".join(manifest.failed)
                           + " — the tree is incomplete; refusing to build or push it")

    try:
        prepared, paths = kind.prepare(Path(src), config.settings, options)
    except ComposeError as e:
        manifest.prepared = {"error": str(e)}
        manifest.write(workdir)
        raise
    if paths:
        _run(["git", "add", "--", *paths], cwd=src)
        if _run(["git", "diff", "--cached", "--quiet"], cwd=src, check=False).returncode != 0:
            _log(f"prepare: {kind.name} wrote {', '.join(paths)}")
            commit = _run(["git", "commit", "--quiet", "-m", f"prepare: {kind.name} {' '.join(paths)}"],
                          cwd=src, check=False)
            if commit.returncode != 0:
                raise ComposeError(f"commit of the prepare step failed:\n{commit.stderr.strip()}")
    manifest.prepared = prepared
    manifest.composed_sha = _run(["git", "rev-parse", "HEAD"], cwd=src).stdout.strip()
    manifest.write(workdir)
    _log(f"composed -> {manifest.composed_sha[:12]}")
    return manifest
