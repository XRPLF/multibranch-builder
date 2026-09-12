"""The xrpld kind: rippled trees, the registry merge driver, Cloud Build."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from ...conf import BranchEntry, ConfError
from ...merge import enable_rerere, write_attributes
from ..base import BuildRequest, check_keys
from . import build as build_mod

_HERE = Path(__file__).parent
_DRIVER = _HERE / "registry_merge.py"
_REGISTRY_ATTRIBUTES = (
    "include/xrpl/protocol/detail/features.macro merge=xrplregistry",
    "include/xrpl/protocol/detail/ledger_entries.macro merge=xrplregistry",
    "include/xrpl/protocol/detail/transactions.macro merge=xrplregistry",
    "include/xrpl/protocol/detail/sfields.macro merge=xrplregistry",
    "include/xrpl/protocol/jss.h merge=xrplregistry",
)


def _git_config(tree: str, key: str, value: str) -> None:
    subprocess.run(["git", "config", key, value], cwd=tree, check=False, capture_output=True)


class XrpldKind:
    name = "xrpld"
    tree_dir = "rippled"
    default_branch = "develop"
    merge_guide = _HERE / "merge.md"
    options = {
        "force_supported": "ON|OFF: compile every amendment as Supported::Yes (default OFF)",
        "datagram": "owner/repo[@branch] or github URL merged before the conf branches; "
                    "a bare repo means branch `datagram`",
    }
    settings: dict[str, str] = {}

    def validate_options(self, options: dict[str, str]) -> dict[str, str]:
        check_keys(options, self.options, "option", self.name)
        out = {"force_supported": options.get("force_supported", "OFF")}
        if out["force_supported"] not in ("ON", "OFF"):
            raise ConfError(f"force_supported must be ON or OFF, got {out['force_supported']!r}")
        if options.get("datagram"):
            out["datagram"] = BranchEntry.parse(options["datagram"], "datagram").label
        return out

    def validate_settings(self, settings: dict[str, str]) -> None:
        check_keys(settings, self.settings, "setting", self.name)

    def configure_merge(self, tree: str) -> None:
        _git_config(tree, "merge.xrplregistry.name", "xrpld registry union merge")
        _git_config(tree, "merge.xrplregistry.driver",
                    f"{shlex.quote(sys.executable)} {shlex.quote(str(_DRIVER))} %O %A %B %P")
        enable_rerere(tree)
        write_attributes(tree, _REGISTRY_ATTRIBUTES)

    def datagram(self, options: dict[str, str]) -> BranchEntry | None:
        return BranchEntry.from_slug(options["datagram"]) if options.get("datagram") else None

    def plan(self, branches: list[BranchEntry], options: dict[str, str]) -> list[BranchEntry]:
        datagram = self.datagram(options)
        return ([datagram] if datagram else []) + list(branches)

    def prepare(self, tree: Path, settings: dict[str, str], options: dict[str, str]) -> tuple[dict, list[str]]:
        return {}, []

    def build(self, req: BuildRequest) -> dict:
        return build_mod.build(req, datagram=self.datagram(req.options))

    def image_suffix(self, options: dict[str, str]) -> str:
        return "-dg" if options.get("datagram") else ""
