"""Target kinds: one package per repository family the builder composes."""

from __future__ import annotations

from ..conf import Config, ConfError
from .base import BuildRequest, Kind
from .xrpl_js import XrplJsKind
from .xrpld import XrpldKind

KINDS: dict[str, Kind] = {k.name: k for k in (XrpldKind(), XrplJsKind())}

__all__ = ["KINDS", "BuildRequest", "Kind", "for_base", "for_config", "for_name"]


def for_name(name: str) -> Kind:
    try:
        return KINDS[name]
    except KeyError:
        raise ConfError(f"unknown kind {name!r} (known: {', '.join(sorted(KINDS))})") from None


def for_base(slug: str) -> Kind:
    """The kind a base repository implies: rippled and xrpld* are xrpld, xrpl.js is xrpl_js."""
    repo = slug.rsplit("/", 1)[-1]
    if repo == "rippled" or repo.startswith("xrpld"):
        return KINDS["xrpld"]
    if repo == "xrpl.js":
        return KINDS["xrpl_js"]
    raise ConfError(f"no kind for base repository {slug!r}; add a `kind <name>` line "
                    f"(known: {', '.join(sorted(KINDS))})")


def for_config(config: Config) -> Kind:
    """The conf's `kind` header, else the kind its base repository implies."""
    return for_name(config.kind) if config.kind else for_base(config.base.slug)
