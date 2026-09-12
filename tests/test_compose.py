"""compose() against local git repositories: clean merge, resolver merge, conflict, prepare."""

import json
import subprocess

import pytest

from multibranch_builder import compose as compose_mod
from multibranch_builder.compose import ComposeError, Manifest, compose
from multibranch_builder.conf import BranchEntry, Config
from multibranch_builder.targets import for_name

XRPLD = for_name("xrpld")
BASE = BranchEntry("XRPLF", "rippled", "develop")


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


def _config(*branches, target=None):
    return Config(BASE, target, list(branches))


def test_compose_clean_and_resolved(upstream, tmp_path):
    branches = [BranchEntry("XRPLF", "rippled", "feat/clean"),
                BranchEntry("XRPLF", "rippled", "feat/conflict", rebase=True)]
    target = BranchEntry("Transia-RnD", "rippled", "alphanet")
    work = tmp_path / "work"
    manifest = compose(_config(*branches, target=target), work, kind=XRPLD,
                       options={"force_supported": "ON"}, resolver=resolve_by_taking_theirs)

    assert manifest.base == "XRPLF/rippled@develop"
    assert manifest.base_sha == git(upstream, "rev-parse", "develop")
    assert [b.outcome for b in manifest.branches] == ["merged", "ai-resolved"]
    assert manifest.branches[0].sha == git(upstream, "rev-parse", "feat/clean")
    assert manifest.branches[1].rebase is True
    assert manifest.target == "Transia-RnD/rippled@alphanet"
    assert (manifest.kind, manifest.tree_dir) == ("xrpld", "rippled")
    assert manifest.options == {"force_supported": "ON"}
    assert manifest.prepared == {}
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
    assert loaded.trailer().startswith("Multibranch-Builder-Manifest: {")
    assert json.loads(loaded.trailer().split(": ", 1)[1])["composed_sha"] == manifest.composed_sha
    assert "**kind** `xrpld`" in loaded.markdown()
    assert "| XRPLF/rippled | feat/clean |" in loaded.markdown()


def test_compose_conflict_writes_manifest_then_raises(upstream, tmp_path):
    branches = [BranchEntry("XRPLF", "rippled", "feat/conflict"),
                BranchEntry("XRPLF", "rippled", "feat/clean")]
    work = tmp_path / "work"
    with pytest.raises(ComposeError, match="feat/conflict"):
        compose(_config(*branches), work, kind=XRPLD, options={}, resolver=lambda cwd, b, d: False)
    manifest = Manifest.load(work / "manifest.json")
    assert [b.outcome for b in manifest.branches] == ["conflict", "merged"]
    assert manifest.failed == ["XRPLF/rippled@feat/conflict"]
    assert git(work / "rippled", "status", "--porcelain") == ""


def test_compose_up_to_date_branch(upstream, tmp_path):
    manifest = compose(_config(BranchEntry("XRPLF", "rippled", "develop")), tmp_path / "work",
                       kind=XRPLD, options={}, resolver=lambda cwd, b, d: False)
    assert manifest.branches[0].outcome == "up-to-date"
    assert manifest.composed_sha == manifest.base_sha


def test_compose_accepts_relative_workdir(upstream, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manifest = compose(_config(BranchEntry("XRPLF", "rippled", "feat/clean")), "work",
                       kind=XRPLD, options={}, resolver=lambda cwd, b, d: False)
    assert (tmp_path / "work" / "rippled" / "clean.txt").exists()
    assert not (tmp_path / "work" / "work").exists()
    assert manifest.branches[0].outcome == "merged"


class PreparingKind:
    """A kind whose prepare step writes a generated file, or fails."""

    name = "fake"
    tree_dir = "tree"
    default_branch = "develop"
    merge_guide = None
    options: dict = {}
    settings: dict = {}

    def __init__(self, fail=False):
        self.fail = fail

    def validate_options(self, options):
        return dict(options)

    def validate_settings(self, settings):
        return None

    def configure_merge(self, tree):
        return None

    def plan(self, branches, options):
        return list(branches)

    def prepare(self, tree, settings, options):
        if self.fail:
            raise ComposeError("rpc down")
        (tree / "gen.txt").write_text(settings.get("gen", "generated") + "\n")
        return {"gen": "1"}, ["gen.txt"]

    def build(self, req):
        return {"status": "SUCCESS"}

    def image_suffix(self, options):
        return ""


def test_compose_commits_prepare_output_before_composed_sha(upstream, tmp_path):
    work = tmp_path / "work"
    manifest = compose(_config(BranchEntry("XRPLF", "rippled", "feat/clean")), work,
                       kind=PreparingKind(), options={}, resolver=lambda cwd, b, d: False)
    tree = work / "tree"
    assert (manifest.kind, manifest.tree_dir) == ("fake", "tree")
    assert manifest.prepared == {"gen": "1"}
    assert manifest.composed_sha == git(tree, "rev-parse", "HEAD")
    assert git(tree, "status", "--porcelain") == ""
    assert git(tree, "log", "-1", "--format=%s") == "prepare: fake gen.txt"
    assert git(tree, "rev-list", "--count", "--first-parent", f"{manifest.base_sha}..HEAD") == "2"
    assert "**gen** `1`" in manifest.markdown()


def test_compose_prepare_failure_writes_manifest_with_empty_composed_sha(upstream, tmp_path):
    work = tmp_path / "work"
    with pytest.raises(ComposeError, match="rpc down"):
        compose(_config(BranchEntry("XRPLF", "rippled", "feat/clean")), work,
                kind=PreparingKind(fail=True), options={}, resolver=lambda cwd, b, d: False)
    manifest = Manifest.load(work / "manifest.json")
    assert manifest.composed_sha == ""
    assert manifest.prepared == {"error": "rpc down"}
    assert manifest.failed == []
