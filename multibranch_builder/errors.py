"""The two failures the CLI reports: composing a tree and building one."""

from __future__ import annotations


class ComposeError(RuntimeError):
    """The tree could not be composed as described."""


class BuildError(RuntimeError):
    """A build could not be submitted or did not finish SUCCESS."""
