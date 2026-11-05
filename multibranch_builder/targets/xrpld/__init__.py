"""The xrpld kind: rippled trees, the registry merge driver, Cloud Build."""

from __future__ import annotations

import math
import re
import shlex
import subprocess
import sys
from pathlib import Path

from ...conf import BranchEntry, ConfError
from ...merge import enable_rerere, write_attributes
from ..base import BuildRequest, check_keys
from . import build as build_mod

_HERE = Path(__file__).parent
# The form beast::SemanticVersion::parse and cmake's FORCE_XRPLD_VERSION check accept.
_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                     r"(-[A-Za-z1-9-][A-Za-z0-9-]*(\.[A-Za-z1-9-][A-Za-z0-9-]*)*)?"
                     r"(\+[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*)?")
_DRIVER = _HERE / "registry_merge.py"
_REGISTRY_ATTRIBUTES = (
    "include/xrpl/protocol/detail/features.macro merge=xrplregistry",
    "include/xrpl/protocol/detail/ledger_entries.macro merge=xrplregistry",
    "include/xrpl/protocol/detail/transactions.macro merge=xrplregistry",
    "include/xrpl/protocol/detail/sfields.macro merge=xrplregistry",
    "include/xrpl/protocol/jss.h merge=xrplregistry",
)


def _positive_number(value: str) -> str:
    """`value` when it parses as a finite number above zero; ConfError otherwise."""
    try:
        number = float(value)
    except ValueError:
        number = math.nan
    if not (math.isfinite(number) and number > 0):
        raise ConfError(f"ai_budget_usd must be a positive number, got {value!r}")
    return value


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
        "version": "semantic version the binary reports: cmake's FORCE_XRPLD_VERSION where the tree "
                   "reads it, else its BuildInfo.cpp versionString in the uploaded copy "
                   "(default: the tree's own)",
        "symbols": "ON|OFF: keep the symbol table and debug info instead of stripping xrpld and "
                   "validator-keys, for profiling; the image tag gets -sym (default OFF)",
        "renumber": "ON|OFF: OFF pins every registry number; the registry merge driver never renumbers "
                    "and leaves a number collision to the textual merge and the resolver (default ON)",
        "ai_budget_usd": "positive number: the claude resolver's --max-budget-usd per conflicted merge "
                         "(default 5)",
    }
    settings: dict[str, str] = {}

    def validate_options(self, options: dict[str, str]) -> dict[str, str]:
        check_keys(options, self.options, "option", self.name)
        out = {"force_supported": options.get("force_supported", "OFF")}
        if out["force_supported"] not in ("ON", "OFF"):
            raise ConfError(f"force_supported must be ON or OFF, got {out['force_supported']!r}")
        if options.get("datagram"):
            out["datagram"] = BranchEntry.parse(options["datagram"], "datagram").label
        if options.get("symbols", "OFF") != "OFF":
            if options["symbols"] != "ON":
                raise ConfError(f"symbols must be ON or OFF, got {options['symbols']!r}")
            out["symbols"] = "ON"
        if options.get("version"):
            if not _SEMVER.fullmatch(options["version"]):
                raise ConfError(f"version must be a semantic version xrpld accepts, got {options['version']!r}")
            out["version"] = options["version"]
        if "renumber" in options:
            if options["renumber"] not in ("ON", "OFF"):
                raise ConfError(f"renumber must be ON or OFF, got {options['renumber']!r}")
            out["renumber"] = options["renumber"]
        if "ai_budget_usd" in options:
            out["ai_budget_usd"] = _positive_number(options["ai_budget_usd"])
        return out

    def validate_settings(self, settings: dict[str, str]) -> None:
        check_keys(settings, self.settings, "setting", self.name)

    def configure_merge(self, tree: str, options: dict[str, str]) -> None:
        pin = " --no-renumber" if options.get("renumber") == "OFF" else ""
        _git_config(tree, "merge.xrplregistry.name", "xrpld registry union merge")
        _git_config(tree, "merge.xrplregistry.driver",
                    f"{shlex.quote(sys.executable)} {shlex.quote(str(_DRIVER))}{pin} %O %A %B %P")
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
        return ("-dg" if options.get("datagram") else "") + ("-sym" if options.get("symbols") == "ON" else "")
