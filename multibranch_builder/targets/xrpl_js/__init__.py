"""The xrpl.js kind: the SDK monorepo, definitions taken from a live node."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ...conf import BranchEntry, ConfError
from ...errors import ComposeError
from ...merge import enable_rerere, write_attributes
from ..base import BuildRequest, check_keys
from .definitions import DEFINITIONS_PATH, LOADER_KEYS, LOCK_PATH, prepare

_HERE = Path(__file__).parent
_OURS_ATTRIBUTES = (
    f"{DEFINITIONS_PATH} merge=ours",
    f"{LOCK_PATH} merge=ours",
)


def _git(tree: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=tree, text=True, capture_output=True, check=check)


class XrplJsKind:
    name = "xrpl_js"
    tree_dir = "xrpl.js"
    default_branch = "main"
    merge_guide = _HERE / "merge.md"
    options: dict[str, str] = {}
    settings = {"definitions": "JSON-RPC URL of the node whose server_definitions the tree carries"}

    def validate_options(self, options: dict[str, str]) -> dict[str, str]:
        check_keys(options, self.options, "option", self.name)
        return {}

    def validate_settings(self, settings: dict[str, str]) -> None:
        check_keys(settings, self.settings, "setting", self.name)
        url = settings.get("definitions", "")
        if not url:
            raise ConfError(f"{self.name} needs a `definitions <json-rpc url>` line in the conf")
        if not url.startswith(("http://", "https://")):
            raise ConfError(f"definitions must be an http(s) URL, got {url!r}")

    def configure_merge(self, tree: str) -> None:
        _git(tree, "config", "merge.ours.name", "keep the base tree's version")
        _git(tree, "config", "merge.ours.driver", "true")
        enable_rerere(tree)
        write_attributes(tree, _OURS_ATTRIBUTES)

    def plan(self, branches: list[BranchEntry], options: dict[str, str]) -> list[BranchEntry]:
        return list(branches)

    def prepare(self, tree: Path, settings: dict[str, str], options: dict[str, str]) -> tuple[dict, list[str]]:
        return prepare(tree, settings, options)

    def build(self, req: BuildRequest) -> dict:
        """Check the composed tree carries definitions the binary codec can load."""
        tree = req.tree
        if tree is None:
            raise ComposeError(f"{self.name} builds a composed tree, not a single branch")
        path = tree / DEFINITIONS_PATH
        try:
            definitions = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"status": "FAILURE", "step": "verify", "summary": f"{DEFINITIONS_PATH}: {e}"}
        missing = [k for k in (*LOADER_KEYS, "hash") if k not in definitions]
        if missing:
            return {"status": "FAILURE", "step": "verify",
                    "summary": f"{DEFINITIONS_PATH} is missing {', '.join(missing)}"}
        dirty = _git(str(tree), "status", "--porcelain").stdout.strip()
        if dirty:
            return {"status": "FAILURE", "step": "verify",
                    "summary": f"the composed tree has uncommitted changes: {dirty.splitlines()[0]}"}
        return {"status": "SUCCESS", "step": "verify",
                "summary": f"definitions {definitions['hash'][:12]} verified"}

    def image_suffix(self, options: dict[str, str]) -> str:
        return ""
