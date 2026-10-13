"""The failures the CLI reports: composing a tree, building one, and ordering PRs to merge."""

from __future__ import annotations


class ComposeError(RuntimeError):
    """The tree could not be composed as described."""


class BuildError(RuntimeError):
    """A build could not be submitted or did not finish SUCCESS."""


class MergeOrderError(RuntimeError):
    """The PRs could not be listed, fetched or merged to order them."""
