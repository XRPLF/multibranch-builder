"""What a target kind provides: tree layout, merge setup, the prepare and build steps."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..conf import BranchEntry, ConfError

if TYPE_CHECKING:
    from ..compose import Manifest


@dataclass
class BuildRequest:
    """One build: a composed tree with its manifest, or a single branch at a sha."""

    options: dict[str, str]
    tag: str
    tree: Path | None = None
    manifest: "Manifest | None" = None
    source: BranchEntry | None = None
    sha: str = ""
    project: str | None = None
    ar: str | None = None
    pool: str | None = None
    ci_image: str | None = None


class Kind(Protocol):
    """A repository family: where its tree lives, how its files merge, how it is prepared and built."""

    name: str
    tree_dir: str
    default_branch: str
    merge_guide: Path | None
    options: dict[str, str]
    settings: dict[str, str]

    def validate_options(self, options: dict[str, str]) -> dict[str, str]:
        """The `--set` options with defaults filled; ConfError on an unknown key or bad value."""

    def validate_settings(self, settings: dict[str, str]) -> None:
        """ConfError on a conf header this kind does not accept or is missing."""

    def configure_merge(self, tree: str) -> None:
        """Repo-local merge drivers, attributes and rerere for the cloned tree."""

    def plan(self, branches: list[BranchEntry], options: dict[str, str]) -> list[BranchEntry]:
        """The merge order."""

    def prepare(self, tree: Path, settings: dict[str, str], options: dict[str, str]) -> tuple[dict, list[str]]:
        """Runs after the merges: (record for the manifest, tree-relative paths it wrote)."""

    def build(self, req: BuildRequest) -> dict:
        """The build.json record; `status` is SUCCESS when the build passed."""

    def image_suffix(self, options: dict[str, str]) -> str:
        """Appended to the default image tag."""


def check_keys(given: dict[str, str], allowed: dict[str, str], what: str, kind: str) -> None:
    """ConfError naming the first key `kind` does not accept."""
    for key in given:
        if key not in allowed:
            accepted = ", ".join(sorted(allowed)) or "none"
            raise ConfError(f"{kind} does not accept {what} {key!r} (accepted: {accepted})")
