"""Merge one source ref into the checked-out tree; claude resolves what git and the drivers cannot."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

CLAUDE_TOOLS = "Read,Edit,Bash,Glob,Grep"
CLAUDE_BUDGET_USD = 5.0
CLAUDE_TIMEOUT_S = 3600

UP_TO_DATE = "up-to-date"
MERGED = "merged"
AI_RESOLVED = "ai-resolved"
CONFLICT = "conflict"
OUTCOMES = (UP_TO_DATE, MERGED, AI_RESOLVED, CONFLICT)

Resolver = Callable[[str, str, str], bool]

_CONFLICT_MARKER_RE = re.compile(r"^(<{7} |={7}$|>{7} )", re.MULTILINE)


def _run(cmd: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)


def _log(msg: str) -> None:
    print(f"[multibranch-builder] {msg}", file=sys.stderr, flush=True)


def enable_rerere(repo_dir: str) -> None:
    """Record and replay conflict resolutions within this clone."""
    _run(["git", "config", "rerere.enabled", "true"], cwd=repo_dir, check=False)
    _run(["git", "config", "rerere.autoUpdate", "true"], cwd=repo_dir, check=False)


def write_attributes(repo_dir: str, lines: tuple[str, ...] | list[str]) -> None:
    """Append gitattributes lines to .git/info/attributes (repo-local, never committed), once each."""
    attributes = os.path.join(repo_dir, ".git", "info", "attributes")
    os.makedirs(os.path.dirname(attributes), exist_ok=True)
    existing = ""
    if os.path.isfile(attributes):
        with open(attributes) as f:
            existing = f.read()
    with open(attributes, "a") as f:
        for line in lines:
            if line not in existing:
                f.write(line + "\n")


def conflicted_files(cwd: str) -> list[str]:
    result = _run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=cwd, check=False)
    return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]


def files_with_conflict_markers(cwd: str, files: list[str]) -> list[str]:
    """Subset of `files` whose on-disk content still has git conflict markers."""
    remaining = []
    for f in files:
        path = os.path.join(cwd, f)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                if _CONFLICT_MARKER_RE.search(fh.read()):
                    remaining.append(f)
        except OSError:
            continue
    return remaining


def merge_prompt(files: list[str], source_label: str, base_label: str, guide: str | None = None) -> str:
    """The claude prompt: the task, the conflicted files, then the kind's merge guide when given."""
    task = (
        "This repository has an in-progress git merge with conflicts. This merge layers "
        f"'{source_label}' on top of the already-composed tree based on '{base_label}' — KEEP "
        "BOTH the existing tree's logic and this branch's additions. For every conflicted file, "
        "remove all conflict markers (<<<<<<<, =======, >>>>>>>) with a correct combined result "
        "that keeps both sides' behaviour and builds. Then run `git add -A`. Verify `git diff "
        "--check` reports nothing. Do NOT git commit and do NOT git merge --abort.\n\n"
        "Conflicted files:\n" + "\n".join(files)
    )
    return task + ("\n\n---\n\n" + guide if guide else "")


def ai_resolve(cwd: str, source_label: str, base_label: str, *, guide_path: Path | None = None,
               budget_usd: float = CLAUDE_BUDGET_USD, timeout_s: int = CLAUDE_TIMEOUT_S) -> bool:
    """Run `claude -p` on the conflicted files; True when none remain unmerged or marked."""
    files = conflicted_files(cwd)
    if not files:
        return True
    if shutil.which("claude") is None:
        _log("`claude` CLI not found — cannot resolve merge conflicts")
        return False
    guide = guide_path.read_text(encoding="utf-8") if guide_path else None
    _log(f"AI-resolving {len(files)} conflicted file(s) from {source_label} via claude")
    try:
        subprocess.run(
            ["claude", "-p", merge_prompt(files, source_label, base_label, guide),
             "--permission-mode", "bypassPermissions",
             "--allowedTools", CLAUDE_TOOLS,
             "--add-dir", cwd,
             "--max-budget-usd", str(budget_usd)],
            cwd=cwd, check=False, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        _log(f"claude timed out after {timeout_s}s resolving {source_label}")
        return False
    leftover = files_with_conflict_markers(cwd, files)
    if leftover:
        _log(f"{len(leftover)} file(s) still contain conflict markers: {leftover}")
        return False
    _run(["git", "add", "-A"], cwd=cwd)
    remaining = conflicted_files(cwd)
    if remaining:
        _log(f"{len(remaining)} file(s) still unmerged after AI resolution: {remaining}")
        return False
    _log(f"AI resolved all {len(files)} conflicted file(s) from {source_label}")
    return True


def no_resolve(cwd: str, source_label: str, base_label: str) -> bool:
    """Resolver that leaves every conflict unresolved, so the merge is aborted as `conflict`."""
    _log(f"{source_label}: {len(conflicted_files(cwd))} conflicted file(s), no resolver (--no-ai)")
    return False


def merge_source_into_current(repo_dir: str, source_ref: str, branch_label: str,
                              base_label: str, resolver: Resolver = ai_resolve) -> str:
    """Merge `source_ref` into the checked-out branch with `--no-commit`; the result stays staged.

    Returns one of OUTCOMES: `up-to-date` (nothing staged), `merged` (clean), `ai-resolved`
    (rerere, the registry driver or the resolver finished it), or `conflict` (aborted, tree clean).
    """
    merge = _run(["git", "merge", "--no-commit", "--no-ff", source_ref], cwd=repo_dir, check=False)
    conflicted = conflicted_files(repo_dir)
    if merge.returncode == 0 and not conflicted:
        staged = _run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir, check=False)
        if staged.returncode == 0:
            return UP_TO_DATE
        return MERGED
    if merge.returncode != 0 and not conflicted:
        _log(f"{branch_label}: conflicts auto-resolved (rerere/registry driver)")
        return AI_RESOLVED
    if resolver(repo_dir, branch_label, base_label):
        return AI_RESOLVED
    _run(["git", "merge", "--abort"], cwd=repo_dir, check=False)
    return CONFLICT
