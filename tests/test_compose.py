"""compose() against local git repositories: clean merge, resolver merge, conflict."""

import json
import subprocess

import pytest

from xrpld_compose import compose as compose_mod
from xrpld_compose.compose import ComposeError, Manifest, compose, plan
from xrpld_compose.conf import BranchEntry


def git(cwd, *args, **kw):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True, **kw).stdout.strip()


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """An `XRPLF/rippled` stand-in with develop, a clean feature and a conflicting feature."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "develop")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feat/clean")
    (repo / "clean.txt").write_text("clean\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "clean feature")
    git(repo, "checkout", "-q", "develop")
    git(repo, "checkout", "-q", "-b", "feat/conflict")
    (repo / "a.txt").write_text("theirs\n")
    git(repo, "commit", "-q", "-am", "conflicting feature")
    git(repo, "checkout", "-q", "develop")
    (repo / "a.txt").write_text("ours\n")
    git(repo, "commit", "-q", "-am", "develop moves")
    monkeypatch.setattr(BranchEntry, "url", property(lambda self: str(repo)))
    return repo


def resolve_by_taking_theirs(cwd, branch_label, base_label):
    (compose_mod.Path(cwd) / "a.txt").write_text("theirs\n")
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
    return True


def test_plan_puts_datagram_first():
    a, b, dg = BranchEntry("o", "r", "a"), BranchEntry("o", "r", "b"), BranchEntry("o", "r", "dg")
    assert plan([a, b], dg) == [dg, a, b]
    assert plan([a, b]) == [a, b]


def test_compose_clean_and_resolved(upstream, tmp_path):
    base = BranchEntry("XRPLF", "rippled", "develop")
    branches = [BranchEntry("XRPLF", "rippled", "feat/clean"),
                BranchEntry("XRPLF", "rippled", "feat/conflict", rebase=True)]
    target = BranchEntry("Transia-RnD", "rippled", "alphanet")
    work = tmp_path / "work"
    manifest = compose(base, branches, work, target=target, force_supported="ON",
                       resolver=resolve_by_taking_theirs)

    assert manifest.base == "XRPLF/rippled@develop"
    assert manifest.base_sha == git(upstream, "rev-parse", "develop")
    assert [b.outcome for b in manifest.branches] == ["merged", "ai-resolved"]
    assert manifest.branches[0].sha == git(upstream, "rev-parse", "feat/clean")
    assert manifest.branches[1].rebase is True
    assert manifest.target == "Transia-RnD/rippled@alphanet"
    assert manifest.force_supported == "ON"
    tree = work / "rippled"
    assert manifest.composed_sha == git(tree, "rev-parse", "HEAD")
    assert (tree / "clean.txt").read_text() == "clean\n"
    assert (tree / "a.txt").read_text() == "theirs\n"
    assert git(tree, "status", "--porcelain") == ""
    assert git(tree, "rev-list", "--count", "--first-parent", f"{manifest.base_sha}..HEAD") == "2"
    assert git(tree, "config", "merge.xrplregistry.driver").endswith("%O %A %B %P")
    assert "merge=xrplregistry" in (tree / ".git" / "info" / "attributes").read_text()

    loaded = Manifest.load(work / "manifest.json")
    assert loaded == manifest
    assert loaded.trailer().startswith("Xrpld-Compose-Manifest: {")
    assert json.loads(loaded.trailer().split(": ", 1)[1])["composed_sha"] == manifest.composed_sha
    assert "| XRPLF/rippled | feat/clean |" in loaded.markdown()


def test_compose_conflict_writes_manifest_then_raises(upstream, tmp_path):
    base = BranchEntry("XRPLF", "rippled", "develop")
    branches = [BranchEntry("XRPLF", "rippled", "feat/conflict"),
                BranchEntry("XRPLF", "rippled", "feat/clean")]
    work = tmp_path / "work"
    with pytest.raises(ComposeError, match="feat/conflict"):
        compose(base, branches, work, resolver=lambda cwd, b, d: False)
    manifest = Manifest.load(work / "manifest.json")
    assert [b.outcome for b in manifest.branches] == ["conflict", "merged"]
    assert manifest.failed == ["XRPLF/rippled@feat/conflict"]
    assert git(work / "rippled", "status", "--porcelain") == ""


def test_compose_up_to_date_branch(upstream, tmp_path):
    base = BranchEntry("XRPLF", "rippled", "develop")
    manifest = compose(base, [BranchEntry("XRPLF", "rippled", "develop")], tmp_path / "work",
                       resolver=lambda cwd, b, d: False)
    assert manifest.branches[0].outcome == "up-to-date"
    assert manifest.composed_sha == manifest.base_sha
