"""The branch list that describes a composed tree."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_GITHUB = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_HEADER_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class ConfError(ValueError):
    """The conf text does not follow the header/entry grammar."""


@dataclass(frozen=True)
class BranchEntry:
    """One `owner/repo` at one ref; `rebase` is carried from the conf for the network repos."""

    owner: str
    repo: str
    branch: str
    rebase: bool = False

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.slug}.git"

    @property
    def remote(self) -> str:
        return f"{self.owner}-{self.repo}"

    @property
    def ref(self) -> str:
        return f"{self.remote}/{self.branch}"

    @property
    def label(self) -> str:
        return f"{self.slug}@{self.branch}"

    @property
    def is_commit(self) -> bool:
        return bool(_HEX40.match(self.branch))

    @classmethod
    def from_slug(cls, text: str, default_branch: str = "develop") -> "BranchEntry":
        """`owner/repo@branch` or `owner/repo` (defaults to `default_branch`)."""
        slug, sep, branch = text.partition("@")
        if "/" not in slug or slug.count("/") != 1:
            raise ConfError(f"expected owner/repo[@branch], got {text!r}")
        owner, repo = slug.split("/", 1)
        if not owner or not repo:
            raise ConfError(f"expected owner/repo[@branch], got {text!r}")
        return cls(owner, repo, branch if sep else default_branch)

    @classmethod
    def from_url(cls, url: str, default_branch: str = "develop") -> "BranchEntry":
        """A github tree/commit/repo URL; a bare repo URL means `default_branch`."""
        if "/commit/" in url:
            base, ref = url.split("/commit/", 1)
        elif "/tree/" in url:
            base, ref = url.split("/tree/", 1)
        else:
            base, ref = url, default_branch
        m = _GITHUB.match(base)
        if not m or not ref:
            raise ConfError(f"not a github repo/tree/commit URL: {url!r}")
        return cls(m.group(1), m.group(2), ref.strip("/"))

    @classmethod
    def parse(cls, text: str, default_branch: str = "develop") -> "BranchEntry":
        """Accept either a github URL or `owner/repo[@branch]`."""
        if text.startswith("https://"):
            return cls.from_url(text, default_branch)
        return cls.from_slug(text, default_branch)


@dataclass
class Config:
    base: BranchEntry
    target: BranchEntry | None = None
    branches: list[BranchEntry] = field(default_factory=list)
    kind: str | None = None
    settings: dict[str, str] = field(default_factory=dict)


def parse_config_text(text: str, source: str = "<conf>") -> Config:
    """Headers `base <owner/repo> <branch>` (required), `target <owner/repo> <branch>`,
    `kind <name>` and `<setting> <value>` for the kind; then `<owner/repo> <branch> [rebase]`
    per line; `#` starts a comment."""
    base: BranchEntry | None = None
    target: BranchEntry | None = None
    kind: str | None = None
    settings: dict[str, str] = {}
    branches: list[BranchEntry] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        where = f"{source}:{lineno}"
        if "/" not in parts[0]:
            key = parts[0]
            if not _HEADER_KEY.match(key) or len(parts) < 2:
                raise ConfError(f"{where}: expected `owner/repo branch [rebase]` or `<setting> <value>`")
            if key in ("base", "target"):
                if len(parts) != 3 or "/" not in parts[1]:
                    raise ConfError(f"{where}: expected `{key} owner/repo branch`")
                entry = BranchEntry.from_slug(parts[1], parts[2])
                if key == "base":
                    if base is not None:
                        raise ConfError(f"{where}: second `base` line")
                    base = entry
                else:
                    if target is not None:
                        raise ConfError(f"{where}: second `target` line")
                    target = entry
            elif key == "kind":
                if len(parts) != 2:
                    raise ConfError(f"{where}: expected `kind <name>`")
                if kind is not None:
                    raise ConfError(f"{where}: second `kind` line")
                kind = parts[1]
            else:
                if key in settings:
                    raise ConfError(f"{where}: second `{key}` line")
                settings[key] = " ".join(parts[1:])
            continue
        if len(parts) not in (2, 3):
            raise ConfError(f"{where}: expected `owner/repo branch [rebase]`")
        if len(parts) == 3 and parts[2].lower() != "rebase":
            raise ConfError(f"{where}: third field must be `rebase`, got {parts[2]!r}")
        entry = BranchEntry.from_slug(parts[0], parts[1])
        branches.append(BranchEntry(entry.owner, entry.repo, entry.branch, len(parts) == 3))
    if base is None:
        raise ConfError(f"{source}: no `base owner/repo branch` line")
    seen: set[str] = set()
    for entry in branches:
        if entry.label in seen:
            raise ConfError(f"{source}: duplicate entry {entry.label}")
        seen.add(entry.label)
    return Config(base=base, target=target, branches=branches, kind=kind, settings=settings)


def parse_config(path: str | Path) -> Config:
    path = Path(path)
    return parse_config_text(path.read_text(encoding="utf-8"), str(path))
