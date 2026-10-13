"""merge-order: stack grouping, the exact independent set, real merges against a local upstream, the CLI."""

import itertools
import json
import random
import subprocess

import pytest

from multibranch_builder import cli
from multibranch_builder import merge_order as mo


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def pr(number, base="develop", head=None, *, ready=True, draft=False, cross=False, oid=""):
    return mo.PR(number, f"title {number}", base, head or f"feat/{number}", oid, draft, cross, ready, f"u/{number}")


def labels(units):
    return [u.label for u in units]


def test_stack_is_one_unit_parent_first():
    prs = [pr(3, base="feat/2"), pr(1), pr(2, base="feat/1"), pr(4)]
    units, skipped = mo.build_units(prs, "develop")
    assert labels(units) == ["#1 → #2 → #3", "#4"]
    assert skipped == {}


def test_parent_with_two_children_merges_children_in_number_order():
    units, _ = mo.build_units([pr(1), pr(5, base="feat/1"), pr(2, base="feat/1")], "develop")
    assert labels(units) == ["#1 → #2 → #5"]


def test_unlabelled_parent_draft_and_other_base_are_skipped():
    prs = [pr(1, ready=False), pr(2, base="feat/1"), pr(3, draft=True), pr(4, base="feat/3"),
           pr(5, base="release")]
    units, skipped = mo.build_units(prs, "develop")
    assert units == []
    assert skipped == {
        2: "stacked on #1, which is not labelled",
        3: "draft",
        4: "stacked on #3, which is a draft",
        5: "targets release, which is not develop or an open PR's branch",
    }


def test_base_branch_loop_is_skipped():
    units, skipped = mo.build_units([pr(1, base="feat/2"), pr(2, base="feat/1")], "develop")
    assert units == []
    assert set(skipped) == {1, 2}
    assert all("loops back" in reason for reason in skipped.values())


def test_fork_head_with_a_colliding_name_is_not_a_stack_parent():
    prs = [pr(1, head="topic"), pr(2, head="topic", cross=True), pr(3, base="topic")]
    units, _ = mo.build_units(prs, "develop")
    assert labels(units) == ["#1 → #3", "#2"]


def brute_force(nodes, adj, score):
    best = None
    for r in range(len(nodes), -1, -1):
        for combo in itertools.combinations(sorted(nodes), r):
            chosen = frozenset(combo)
            if all(adj[a].isdisjoint(chosen) for a in chosen):
                key = (len(chosen), score(chosen))
                if best is None or key > best[0]:
                    best = (key, set(chosen))
    return best[1]


def test_max_independent_set_matches_brute_force():
    rng = random.Random(7)
    for _ in range(200):
        nodes = set(range(1, rng.randint(1, 9) + 1))
        adj = {n: set() for n in nodes}
        for a, b in itertools.combinations(sorted(nodes), 2):
            if rng.random() < 0.35:
                adj[a].add(b)
                adj[b].add(a)
        assert mo.max_independent_set(nodes, adj, mo.older_first) == brute_force(nodes, adj, mo.older_first)


def commit_file(repo, path, text, message):
    (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """A repository with develop and PR heads under refs/pull/<n>/head, served to the cache by path.

    #11 and #12 edit the same line of a.txt; #13 edits c.txt; #14 edits b.txt, which develop then
    changes too; #15 <- #16 is a stack; #18 is stacked on the unlabelled #17.
    """
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "develop")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "uploadpack.allowFilter", "true")
    (repo / "a.txt").write_text("".join(f"{i}\n" for i in range(1, 10)))
    (repo / "b.txt").write_text("b\n")
    commit_file(repo, "c.txt", "c\n", "base")

    def branch(name, start, path, text):
        git(repo, "checkout", "-q", "-b", name, start)
        commit_file(repo, path, text, name)
        return git(repo, "rev-parse", "HEAD")

    lines = "".join(f"{i}\n" for i in range(2, 10))
    heads = {
        11: branch("feat/11", "develop", "a.txt", "one\n" + lines),
        12: branch("feat/12", "develop", "a.txt", "two\n" + lines),
        13: branch("feat/13", "develop", "c.txt", "changed\n"),
        14: branch("feat/14", "develop", "b.txt", "stale\n"),
        15: branch("feat/15", "develop", "d.txt", "stack a\n"),
        16: branch("feat/16", "feat/15", "d.txt", "stack b\n"),
        17: branch("feat/17", "develop", "e.txt", "unlabelled\n"),
        18: branch("feat/18", "feat/17", "e.txt", "on unlabelled\n"),
    }
    git(repo, "checkout", "-q", "develop")
    commit_file(repo, "b.txt", "develop\n", "develop moves")
    for number, sha in heads.items():
        git(repo, "update-ref", f"refs/pull/{number}/head", sha)

    bases = {16: "feat/15", 18: "feat/17"}
    prs = [pr(n, base=bases.get(n, "develop"), oid=sha, ready=n != 17) for n, sha in heads.items()]
    monkeypatch.setattr(mo, "remote_url", lambda slug: str(repo))
    monkeypatch.setattr(mo, "list_prs", lambda slug, label: prs)
    return repo


def test_order_repo_with_real_merges(upstream, tmp_path):
    report = mo.order_repo("XRPLF/rippled", base="develop", cache_root=tmp_path / "cache")

    assert report["base_oid"] == git(upstream, "rev-parse", "develop")
    assert [[e["label"] for e in r] for r in report["rounds"]] == [["#11", "#13", "#15 → #16"], ["#12"]]
    assert report["rounds"][1][0]["conflicts_with"] == {"#11": ["a.txt"]}
    assert [(e["label"], e["files"]) for e in report["conflicts_with_base"]] == [("#14", ["b.txt"])]
    assert report["skipped"] == [{"pr": 18, "reason": "stacked on #17, which is not labelled"}]
    assert report["conflicts"] == [{"a": "#11", "b": "#12", "files": ["a.txt"]}]
    assert git(tmp_path / "cache" / "XRPLF__rippled.git", "config", "remote.origin.promisor") == "true"

    text = mo.render(report)
    assert "  1. #11 " in text and "  4. #12 " in text
    assert "conflicts with #11: a.txt" in text


def test_order_repo_rejects_a_head_that_moved(upstream, tmp_path, monkeypatch):
    moved = [p if p.number != 13 else mo.PR(**{**p.__dict__, "oid": "0" * 40}) for p in mo.list_prs("x", "y")]
    monkeypatch.setattr(mo, "list_prs", lambda slug, label: moved)
    with pytest.raises(mo.MergeOrderError, match="#13: fetched"):
        mo.order_repo("XRPLF/rippled", base="develop", cache_root=tmp_path / "cache")


def test_combination_conflict_defers_one_unit_and_repicks(monkeypatch, tmp_path):
    """#3 merges after #1 or #2 alone but not after both; #4 conflicts only with #3."""

    class FakeCache:
        def __init__(self, slug, root):
            pass

        def fetch(self, base, prs):
            return "B"

        def apply(self, onto, unit):
            if unit.key == 3 and "+1" in onto and "+2" in onto:
                return None, ["combo.txt"]
            if unit.key == 4 and "+3" in onto:
                return None, ["pair.txt"]
            return f"{onto}+{unit.key}", []

    monkeypatch.setattr(mo, "Cache", FakeCache)
    monkeypatch.setattr(mo, "list_prs", lambda slug, label: [pr(n) for n in (1, 2, 3, 4)])
    report = mo.order_repo("x/y", base="develop", cache_root=tmp_path)
    assert [[e["label"] for e in r] for r in report["rounds"]] == [["#1", "#2", "#4"], ["#3"]]
    deferred = report["rounds"][1][0]
    assert deferred["combination_conflict"] == ["combo.txt"]
    assert deferred["conflicts_with"] == {"#4": ["pair.txt"]}


def test_cli_orders_only_the_named_repositories(monkeypatch, capsys):
    calls = []

    def fake_order(slug, *, label, base, cache_root):
        calls.append((slug, label, base))
        return {"repo": slug}

    monkeypatch.setattr(mo, "order_repo", fake_order)
    assert cli.main(["merge-order", "--repo", "a/b", "--repo", "c/d", "--label", "L", "--json"]) == 0
    assert calls == [("a/b", "L", None), ("c/d", "L", None)]
    assert json.loads(capsys.readouterr().out) == [{"repo": "a/b"}, {"repo": "c/d"}]

    calls.clear()
    cli.main(["merge-order", "--json"])
    assert calls == [("XRPLF/rippled", mo.DEFAULT_LABEL, None)]


def test_cli_reports_a_merge_order_error(monkeypatch):
    def fail(slug, **kw):
        raise mo.MergeOrderError("boom")

    monkeypatch.setattr(mo, "order_repo", fail)
    with pytest.raises(SystemExit, match=r"\[multibranch-builder\] boom"):
        cli.main(["merge-order"])
