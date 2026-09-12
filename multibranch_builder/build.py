"""Build records and the helpers every kind's build step shares."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .errors import BuildError

BUILD_FILE = "build.json"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)


def resolve_sha(url: str, ref: str) -> str:
    """The 40-char sha `ref` names in the remote at `url`; a sha passes through."""
    if _HEX40.match(ref):
        return ref
    result = _run(["git", "ls-remote", url, ref], check=False)
    first = result.stdout.splitlines()[0].split() if result.stdout.strip() else []
    if result.returncode != 0 or not first:
        raise BuildError(f"could not resolve {ref!r} in {url}: {result.stderr.strip()}")
    return first[0]


def safe_name(ref: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in ref)


def image_tag(ref: str, sha: str, suffix: str = "") -> str:
    """`<safe-ref>-<sha8><suffix>`, or `<sha8><suffix>` when the ref is itself a sha."""
    tag = sha[:8] if _HEX40.match(ref) else f"{safe_name(ref)}-{sha[:8]}"
    return tag + suffix


def write_build(workdir: str | Path, record: dict) -> Path:
    path = Path(workdir) / BUILD_FILE
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def load_build(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
