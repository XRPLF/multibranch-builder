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
    monkeypatch.setattr(mo, "list_prs", lambda slug, label, milestones=(): prs)
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
    monkeypatch.setattr(mo, "list_prs", lambda slug, label, milestones=(): moved)
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
    monkeypatch.setattr(mo, "list_prs", lambda slug, label, milestones=(): [pr(n) for n in (1, 2, 3, 4)])
    report = mo.order_repo("x/y", base="develop", cache_root=tmp_path)
    assert [[e["label"] for e in r] for r in report["rounds"]] == [["#1", "#2", "#4"], ["#3"]]
    deferred = report["rounds"][1][0]
    assert deferred["combination_conflict"] == ["combo.txt"]
    assert deferred["conflicts_with"] == {"#4": ["pair.txt"]}


def test_cli_orders_only_the_named_repositories(monkeypatch, capsys):
    calls = []

    def fake_order(slug, *, label, base, cache_root, milestones=()):
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


def gh_pr(number, base="develop", head=None, *, milestone=None, labels=(), cross=False, owner="XRPLF"):
    return {"number": number, "title": f"title {number}", "baseRefName": base, "headRefName": head or f"feat/{number}",
            "headRefOid": "a" * 40, "isDraft": False, "isCrossRepository": cross,
            "labels": [{"name": n} for n in labels], "url": f"u/{number}",
            "milestone": {"title": milestone} if milestone else None,
            "headRepository": {"name": "rippled"}, "headRepositoryOwner": {"login": owner}}


def fake_gh(monkeypatch, open_prs, merged=()):
    def run(cmd, env=None, ok=(0,)):
        state = cmd[cmd.index("--state") + 1]
        rows = list(open_prs) if state == "open" else [p for p in merged if f'milestone:"{p["ms"]}"' in cmd]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(rows), "")
    monkeypatch.setattr(mo, "_run", run)


def test_milestone_selects_by_milestone_not_label(monkeypatch):
    fake_gh(monkeypatch, [gh_pr(1, milestone="3.5.0"), gh_pr(2, labels=["Ready to merge"]),
                          gh_pr(3, milestone="3.5.0 (tests)"), gh_pr(4, milestone="3.6.0", labels=["Ready to merge"])])
    by_label = {p.number: p.ready for p in mo.list_prs("XRPLF/rippled", "Ready to merge")}
    by_ms = {p.number: p.ready for p in mo.list_prs("XRPLF/rippled", "Ready to merge",
                                                     milestones=("3.5.0", "3.5.0 (tests)"))}
    assert by_label == {1: False, 2: True, 3: False, 4: True}
    assert by_ms == {1: True, 2: False, 3: True, 4: False}


def test_head_repo_names_the_fork_for_a_cross_repository_pr(monkeypatch):
    fake_gh(monkeypatch, [gh_pr(1), gh_pr(2, cross=True, owner="someone", head="fix")])
    heads = {p.number: (p.head_repo, p.head) for p in mo.list_prs("XRPLF/rippled", "x")}
    assert heads == {1: ("XRPLF/rippled", "feat/1"), 2: ("someone/rippled", "fix")}


def test_a_milestone_pr_stacked_on_a_branch_outside_the_milestone_is_not_ordered(monkeypatch):
    fake_gh(monkeypatch, [gh_pr(1, milestone="3.6.0"), gh_pr(2, base="feat/1", milestone="3.5.0")])
    units, skipped = mo.build_units(mo.list_prs("x/y", "l", milestones=("3.5.0",)), "develop",
                                    "in none of the milestones")
    assert units == [] and skipped == {2: "stacked on #1, which is in none of the milestones"}


def test_merged_prs_lists_each_milestone_once_oldest_first(monkeypatch):
    merged = [{"number": 7, "title": "b", "mergedAt": "2026-10-02T00:00:00Z", "url": "u/7", "ms": "3.5.0"},
              {"number": 5, "title": "a", "mergedAt": "2026-10-01T00:00:00Z", "url": "u/5", "ms": "3.5.0"}]
    fake_gh(monkeypatch, [], merged)
    assert [p["pr"] for p in mo.merged_prs("x/y", ("3.5.0",))] == [5, 7]


def report(repo, rounds, stale=(), skipped=(), base_oid="b" * 40):
    def unit(numbers, repo_of=repo):
        return {"unit": numbers, "label": " → ".join(f"#{n}" for n in numbers), "title": f"title {numbers[0]}",
                "branches": [{"repo": repo_of, "branch": f"feat/{n}"} for n in numbers], "conflicts_with": {},
                "combination_conflict": [], "files": []}
    return {"repo": repo, "base": "develop", "base_oid": base_oid, "label": "l", "milestones": ["3.5.0"],
            "rounds": [[unit(u) for u in r] for r in rounds], "conflicts_with_base": [unit(u) for u in stale],
            "skipped": [{"pr": n, "reason": "why"} for n in skipped], "conflicts": []}


def test_compose_conf_lists_rounds_then_stale_units_parent_first_with_skips_as_comments():
    text = mo.compose_conf([report("XRPLF/rippled", [[[1, 2]], [[3]]], stale=[[4]], skipped=[9]),
                            report("XRPLF/other", [[[5]]])])
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert lines == ["base   XRPLF/rippled develop", "XRPLF/rippled feat/1", "XRPLF/rippled feat/2",
                     "XRPLF/rippled feat/3", "XRPLF/rippled feat/4", "XRPLF/other feat/5"]
    assert "# not ordered: #9 why" in text


def test_compose_conf_notes_a_repository_whose_base_differs():
    text = mo.compose_conf([report("a/r", [[[1]]]), report("b/r", [[[2]]], base_oid="c" * 40)])
    assert "# b/r develop is at cccccccccc, not bbbbbbbbbb" in text


def test_compose_conf_is_read_back_by_the_conf_parser():
    from multibranch_builder.conf import parse_config_text
    conf = parse_config_text(mo.compose_conf([report("XRPLF/rippled", [[[1, 2]]])]))
    assert [(f"{b.owner}/{b.repo}", b.branch) for b in conf.branches] == [("XRPLF/rippled", "feat/1"),
                                                                         ("XRPLF/rippled", "feat/2")]


def test_cli_passes_milestones_and_writes_the_conf(monkeypatch, tmp_path, capsys):
    seen = []

    def fake_order(slug, *, label, base, cache_root, milestones=()):
        seen.append((slug, milestones))
        return report(slug, [[[1]]])

    monkeypatch.setattr(mo, "order_repo", fake_order)
    out = tmp_path / "c.conf"
    cli.main(["merge-order", "--repo", "a/r", "--milestone", "3.5.0", "--milestone", "3.5.0 (tests)",
              "--conf-out", str(out), "--json"])
    assert seen == [("a/r", ("3.5.0", "3.5.0 (tests)"))]
    assert "a/r feat/1" in out.read_text()


def test_a_deleted_fork_gives_an_empty_head_repo_and_is_left_out_of_the_conf(monkeypatch):
    gone = gh_pr(2, cross=True, head="fix")
    gone["headRepository"] = gone["headRepositoryOwner"] = None
    fake_gh(monkeypatch, [gh_pr(1), gone])
    assert {p.number: p.head_repo for p in mo.list_prs("XRPLF/rippled", "x")} == {1: "XRPLF/rippled", 2: ""}
    rep = report("XRPLF/rippled", [[[1], [2]]])
    rep["rounds"][0][1]["branches"][0]["repo"] = ""
    text = mo.compose_conf([rep])
    assert "XRPLF/rippled feat/1" in text and "feat/2" not in text.replace("# left out: #2", "")
    assert "# left out: #2 title 2: a head repository is gone" in text


def test_a_branch_name_holding_a_hash_is_left_out_of_the_conf():
    rep = report("a/r", [[[1]]])
    rep["rounds"][0][0]["branches"][0]["branch"] = "fix#1"
    text = mo.compose_conf([rep])
    assert [ln for ln in text.splitlines() if ln and not ln.startswith("#")] == ["base   a/r develop"]


def test_merged_prs_escapes_quotes_and_keeps_the_first_milestone_of_a_pr_in_two(monkeypatch):
    searches = []

    def run(cmd, env=None, ok=(0,)):
        searches.append(cmd[cmd.index("--search") + 1])
        row = {"number": 5, "title": "a", "mergedAt": "2026-10-01T00:00:00Z", "url": "u/5"}
        return subprocess.CompletedProcess(cmd, 0, json.dumps([row]), "")

    monkeypatch.setattr(mo, "_run", run)
    merged = mo.merged_prs("x/y", ('say "hi"', "b"))
    assert searches == ['milestone:"say \\"hi\\""', 'milestone:"b"']
    assert merged == [{"pr": 5, "title": "a", "merged_at": "2026-10-01T00:00:00Z", "url": "u/5",
                       "milestone": 'say "hi"'}]


def test_render_lists_the_merged_prs_in_milestone_mode():
    rep = report("a/r", [[[1]]])
    rep["merged"] = [{"pr": 5, "title": "done thing", "merged_at": "t", "url": "u", "milestone": "3.5.0"}]
    text = mo.render(rep)
    assert "in milestone '3.5.0'" in text and "#5" in text and "done thing" in text


def test_cli_refuses_label_with_milestone(monkeypatch):
    with pytest.raises(SystemExit):
        cli.main(["merge-order", "--label", "x", "--milestone", "3.5.0"])


def test_order_repo_in_milestone_mode_feeds_compose_conf_end_to_end(upstream, tmp_path, monkeypatch):
    prs = [mo.PR(**{**p.__dict__, "head_repo": "XRPLF/rippled"}) for p in mo.list_prs("x", "y")]
    monkeypatch.setattr(mo, "list_prs", lambda slug, label, milestones=(): prs)
    monkeypatch.setattr(mo, "merged_prs", lambda slug, milestones: [{"pr": 1, "title": "t", "merged_at": "m",
                                                                     "url": "u", "milestone": milestones[0]}])
    rep = mo.order_repo("XRPLF/rippled", base="develop", cache_root=tmp_path / "cache", milestones=("3.5.0",))
    assert rep["milestones"] == ["3.5.0"] and [m["pr"] for m in rep["merged"]] == [1]
    assert all(b["repo"] == "XRPLF/rippled" for rnd in rep["rounds"] for e in rnd for b in e["branches"])
    assert {"pr": 18, "reason": "stacked on #17, which is in none of the milestones"} in rep["skipped"]
    from multibranch_builder.conf import parse_config_text
    conf = parse_config_text(mo.compose_conf([rep]))
    branches = [b.branch for b in conf.branches]
    assert branches.index("feat/15") < branches.index("feat/16")
    assert "feat/18" not in branches and "feat/17" not in branches
