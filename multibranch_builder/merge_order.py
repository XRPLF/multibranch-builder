"""Order a repository's open PRs carrying a label, or in a milestone, so the fewest need conflicts resolved.

Conflicts come from real three-way merges (`git merge-tree --write-tree`) in a bare blobless
cache clone. A PR merged after one it conflicts with needs conflicts resolved, so each round
is a maximum set of mutually non-conflicting units, and round 1 is verified by merging it
cumulatively onto the base branch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import MergeOrderError

DEFAULT_CACHE = Path.home() / ".cache" / "multibranch-builder" / "merge-order"
DEFAULT_LABEL = "Ready to merge"

# Fixed identity and dates keep the scratch merge commits deterministic.
SCRATCH_ENV = {
    "GIT_AUTHOR_NAME": "multibranch-builder",
    "GIT_AUTHOR_EMAIL": "multibranch-builder@localhost",
    "GIT_AUTHOR_DATE": "1700000000 +0000",
    "GIT_COMMITTER_NAME": "multibranch-builder",
    "GIT_COMMITTER_EMAIL": "multibranch-builder@localhost",
    "GIT_COMMITTER_DATE": "1700000000 +0000",
}


def _run(cmd: list[str], env: dict | None = None, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env)
    if proc.returncode not in ok:
        raise MergeOrderError(f"{' '.join(cmd[:4])} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


def _log(msg: str) -> None:
    print(f"[merge-order] {msg}", file=sys.stderr)


@dataclass(frozen=True)
class PR:
    number: int
    title: str
    base: str
    head: str
    oid: str
    draft: bool
    cross: bool
    ready: bool
    url: str
    head_repo: str = ""

    @property
    def ref(self) -> str:
        return f"refs/pr/{self.number}"


@dataclass
class Unit:
    """A PR, or a stack of PRs merged parent first."""

    prs: list[PR]

    @property
    def key(self) -> int:
        return self.prs[0].number

    @property
    def label(self) -> str:
        return " → ".join(f"#{p.number}" for p in self.prs)

    @property
    def title(self) -> str:
        return self.prs[0].title

    def summary(self) -> dict:
        return {"unit": [p.number for p in self.prs], "label": self.label, "title": self.title,
                "url": self.prs[0].url, "branches": [{"repo": p.head_repo, "branch": p.head} for p in self.prs]}


def remote_url(slug: str) -> str:
    return f"https://github.com/{slug}.git"


def list_prs(slug: str, label: str, milestones: tuple[str, ...] = ()) -> list[PR]:
    """Every open PR of `slug`; `ready` marks those in one of `milestones`, or carrying `label` when none is given."""
    fields = ("number,title,baseRefName,headRefName,headRefOid,isDraft,isCrossRepository,labels,url,milestone,"
              "headRepository,headRepositoryOwner")
    out = _run(["gh", "pr", "list", "-R", slug, "--state", "open", "--limit", "1000", "--json", fields]).stdout

    def ready(p: dict) -> bool:
        if milestones:
            return (p.get("milestone") or {}).get("title") in milestones
        return any(lbl["name"] == label for lbl in p["labels"])

    def head_repo(p: dict) -> str:
        if not p["isCrossRepository"]:
            return slug
        owner, repo = (p.get("headRepositoryOwner") or {}).get("login"), (p.get("headRepository") or {}).get("name")
        return f"{owner}/{repo}" if owner and repo else ""  # a deleted fork has neither

    return [PR(number=p["number"], title=p["title"], base=p["baseRefName"], head=p["headRefName"],
               oid=p["headRefOid"], draft=p["isDraft"], cross=p["isCrossRepository"], ready=ready(p), url=p["url"],
               head_repo=head_repo(p))
            for p in json.loads(out)]


def merged_prs(slug: str, milestones: tuple[str, ...]) -> list[dict]:
    """The merged PRs of `slug` in each of `milestones`, oldest merge first."""
    limit = 1000
    seen: dict[int, dict] = {}
    for m in milestones:
        quoted = m.replace("\\", "\\\\").replace('"', '\\"')
        rows = json.loads(_run(["gh", "pr", "list", "-R", slug, "--state", "merged", "--search", f'milestone:"{quoted}"',
                                "--limit", str(limit), "--json", "number,title,mergedAt,url"]).stdout)
        if len(rows) >= limit:
            _log(f"{slug}: milestone {m!r} returned {limit} merged PRs, the search limit; the list may be cut short")
        for p in rows:
            seen.setdefault(p["number"], {"pr": p["number"], "title": p["title"], "merged_at": p["mergedAt"],
                                          "url": p["url"], "milestone": m})
    return sorted(seen.values(), key=lambda p: (p["merged_at"], p["pr"]))


def default_branch(slug: str) -> str:
    return _run(["gh", "repo", "view", slug, "--json", "defaultBranchRef",
                 "--jq", ".defaultBranchRef.name"]).stdout.strip()


def build_units(prs: list[PR], base: str, unselected: str = "not labelled") -> tuple[list[Unit], dict[int, str]]:
    """Group ready PRs into stacks rooted on `base`; return the units and why each other ready PR is left out.
    `unselected` says why a parent that is not ready was not chosen."""
    by_head = {p.head: p for p in sorted(prs, key=lambda p: p.number) if not p.cross}
    ready = [p for p in prs if p.ready]
    skipped: dict[int, str] = {}
    roots: dict[int, PR] = {}
    for pr in ready:
        p, seen = pr, set()
        while p.base != base:
            parent = by_head.get(p.base)
            if parent is None:
                skipped[pr.number] = f"targets {p.base}, which is not {base} or an open PR's branch"
                break
            if parent.number in seen or parent.number == pr.number:
                skipped[pr.number] = f"its stack of base branches loops back through #{parent.number}"
                break
            if not parent.ready or parent.draft:
                skipped[pr.number] = f"stacked on #{parent.number}, which is {'a draft' if parent.ready else unselected}"
                break
            seen.add(p.number)
            p = parent
        if pr.draft:
            skipped[pr.number] = "draft"
        if pr.number not in skipped:
            roots[pr.number] = p

    units = []
    for root in sorted({r.number: r for r in roots.values()}.values(), key=lambda r: r.number):
        members = [p for p in ready if roots.get(p.number) == root]
        order, frontier, queued = [], [root], {root.number}
        while frontier:
            p = frontier.pop(0)
            order.append(p)
            if p.cross:
                continue  # a fork PR's head branch is not in this repository
            children = sorted((c for c in members if c.base == p.head and c.number not in queued),
                              key=lambda c: c.number)
            queued |= {c.number for c in children}
            frontier += children
        units.append(Unit(order))
    return units, skipped


class Cache:
    """A bare blobless clone holding the base branch and refs/pr/<n> for each PR."""

    def __init__(self, slug: str, root: Path):
        self.git_dir = root / f"{slug.replace('/', '__')}.git"
        self.env = {**os.environ, **SCRATCH_ENV}
        if not self.git_dir.exists():
            self.git_dir.parent.mkdir(parents=True, exist_ok=True)
            _run(["git", "init", "--bare", "--quiet", str(self.git_dir)])
            self.git("remote", "add", "origin", remote_url(slug))
            self.git("config", "credential.https://github.com.helper", "!gh auth git-credential")

    def git(self, *args: str, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        return _run(["git", f"--git-dir={self.git_dir}", *args], env=self.env, ok=ok)

    def fetch(self, base: str, prs: list[PR]) -> str:
        refspecs = [f"+refs/heads/{base}:refs/heads/{base}"] + [f"+refs/pull/{p.number}/head:{p.ref}" for p in prs]
        self.git("fetch", "--quiet", "--no-tags", "--filter=blob:none", "origin", *refspecs)
        for p in prs:
            got = self.git("rev-parse", p.ref).stdout.strip()
            if got != p.oid:
                raise MergeOrderError(f"#{p.number}: fetched {got[:10]}, GitHub reports head {p.oid[:10]}; run again")
        return self.git("rev-parse", f"refs/heads/{base}").stdout.strip()

    def merge(self, onto: str, commit: str) -> tuple[str | None, list[str]]:
        """Merge `commit` into `onto`; return the merge commit, or None and the conflicted paths."""
        proc = self.git("merge-tree", "--write-tree", "--name-only", "--no-messages", onto, commit, ok=(0, 1))
        lines = proc.stdout.splitlines()
        if proc.returncode == 1:
            return None, sorted({line for line in lines[1:] if line})
        merged = self.git("commit-tree", "--no-gpg-sign", lines[0], "-p", onto, "-p", commit,
                          "-m", "merge-order scratch")
        return merged.stdout.strip(), []

    def apply(self, onto: str, unit: Unit) -> tuple[str | None, list[str]]:
        """Merge each PR of `unit` in order onto `onto`."""
        for p in unit.prs:
            onto, conflicts = self.merge(onto, p.ref)
            if onto is None:
                return None, conflicts
        return onto, []


def older_first(chosen: frozenset[int]) -> tuple[int, ...]:
    """Ranks the set holding the lowest PR number at its first difference highest."""
    return tuple(-n for n in sorted(chosen))


def max_independent_set(nodes: set[int], adj: dict[int, set[int]],
                        score: Callable[[frozenset[int]], tuple]) -> set[int]:
    """Exact maximum independent set by branch and bound; ties broken by the highest `score`."""
    best: list = [None, frozenset()]

    def consider(chosen: frozenset[int]) -> None:
        key = (len(chosen), score(chosen))
        if best[0] is None or key > best[0]:
            best[0], best[1] = key, chosen

    def search(cands: frozenset[int], chosen: frozenset[int]) -> None:
        if best[0] is not None and len(chosen) + len(cands) < best[0][0]:
            return
        v = max(cands, key=lambda x: (len(adj[x] & cands), -x), default=None)
        if v is None or not adj[v] & cands:
            consider(chosen | cands)
            return
        search(cands - {v} - adj[v], chosen | {v})
        search(cands - {v}, chosen)

    search(frozenset(nodes), frozenset())
    return set(best[1])


def order_repo(slug: str, *, label: str = DEFAULT_LABEL, base: str | None = None,
               cache_root: Path = DEFAULT_CACHE, milestones: tuple[str, ...] = ()) -> dict:
    """The merge order of `slug`'s open PRs carrying `label`, or in `milestones` when given, as a report dict."""
    base = base or default_branch(slug)
    units, skipped = build_units(list_prs(slug, label, milestones=milestones), base,
                                 "in none of the milestones" if milestones else "not labelled")
    cache = Cache(slug, cache_root)
    _log(f"{slug}: fetching {base} and {sum(len(u.prs) for u in units)} PR heads")
    base_oid = cache.fetch(base, [p for u in units for p in u.prs])
    by_key = {u.key: u for u in units}

    tips: dict[int, str] = {}
    stale: dict[int, list[str]] = {}
    for u in units:
        tip, conflicts = cache.apply(base_oid, u)
        if tip is None:
            stale[u.key] = conflicts
        else:
            tips[u.key] = tip

    keys = sorted(tips)
    pair_files: dict[tuple[int, int], list[str]] = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            tip, conflicts = cache.apply(tips[a], by_key[b])
            if tip is None:
                pair_files[(a, b)] = pair_files[(b, a)] = conflicts
    adj = {k: {b for (a, b) in pair_files if a == k} for k in keys}

    rounds: list[list[int]] = []
    combo: dict[int, list[str]] = {}
    remaining = set(keys)
    while remaining:
        pool = remaining - set(combo) if not rounds else remaining

        def score(chosen: frozenset[int], pool: frozenset[int] = frozenset(pool)) -> tuple:
            files = sum(len(pair_files[(d, s)]) for d in pool - chosen for s in chosen if s in adj[d])
            return (-files, older_first(chosen))

        picked = sorted(max_independent_set(pool, adj, score))
        if not rounds:
            # Pairwise-clean units can still conflict as a combination; defer the one that fails and re-pick.
            cur: str | None = base_oid
            for k in picked:
                cur, conflicts = cache.apply(cur, by_key[k])
                if cur is None:
                    combo[k] = conflicts
                    break
            else:
                rounds.append(picked)
                remaining -= set(picked)
            continue
        rounds.append(picked)
        remaining -= set(picked)

    merged_before: set[int] = set()
    out_rounds = []
    for r in rounds:
        out_rounds.append([
            {**by_key[k].summary(),
             "conflicts_with": {f"#{m}": pair_files[(k, m)] for m in sorted(merged_before) if (k, m) in pair_files},
             "combination_conflict": combo.get(k, [])}
            for k in r
        ])
        merged_before |= set(r)

    return {
        "repo": slug,
        "label": label,
        "milestones": list(milestones),
        "merged": merged_prs(slug, milestones) if milestones else [],
        "base": base,
        "base_oid": base_oid,
        "rounds": out_rounds,
        "conflicts_with_base": [{**by_key[k].summary(), "files": f} for k, f in sorted(stale.items())],
        "skipped": [{"pr": n, "reason": r} for n, r in sorted(skipped.items())],
        "conflicts": [{"a": f"#{a}", "b": f"#{b}", "files": f} for (a, b), f in sorted(pair_files.items()) if a < b],
    }


def _files_brief(files: list[str], limit: int = 3) -> str:
    shown = ", ".join(files[:limit])
    return shown + (f" +{len(files) - limit} more" if len(files) > limit else "")


def render(report: dict) -> str:
    """The report as text: rounds in merge order, then what is left out and every conflicting pair."""
    total = sum(len(r) for r in report["rounds"]) + len(report["conflicts_with_base"])
    selector = (f"in milestone {', '.join(repr(m) for m in report['milestones'])}" if report.get("milestones")
                else f"labelled {report['label']!r}")
    lines = [f"{report['repo']}: {total} merge units {selector} onto {report['base']} ({report['base_oid'][:10]})"]
    if report.get("merged"):
        lines += ["", f"Already merged: {len(report['merged'])} PRs"]
        lines += [f"      #{m['pr']:<27} {m['title']}" for m in report["merged"]]
    n = 0
    for i, entries in enumerate(report["rounds"], 1):
        lines.append("")
        if i == 1:
            lines.append(f"Round 1: merge in this order; none conflicts with {report['base']} or with another in the round")
        else:
            lines.append(f"Round {i}: each needs conflicts resolved after the rounds before it; "
                         "none conflicts with another in the round")
        for e in entries:
            n += 1
            lines.append(f"  {n:>2}. {e['label']:<28} {e['title']}")
            for other, files in e["conflicts_with"].items():
                lines.append(f"      conflicts with {other}: {_files_brief(files)}")
            if e["combination_conflict"]:
                lines.append(f"      conflicts with the round-1 PRs merged together: {_files_brief(e['combination_conflict'])}")
    if report["conflicts_with_base"]:
        lines += ["", f"Does not merge onto {report['base']} now (a stack merges parent first); resolve before merging"]
        for e in report["conflicts_with_base"]:
            lines.append(f"      {e['label']:<28} {e['title']}")
            lines.append(f"      {_files_brief(e['files'])}")
    if report["skipped"]:
        lines += ["", "Not ordered"]
        lines += [f"      #{s['pr']:<27} {s['reason']}" for s in report["skipped"]]
    if report["conflicts"]:
        lines += ["", "Conflicting pairs"]
        lines += [f"      {c['a']} x {c['b']}: {_files_brief(c['files'])}" for c in report["conflicts"]]
    return "\n".join(lines)


def compose_conf(reports: list[dict]) -> str:
    """A `multibranch-builder compose` conf merging every ordered unit of `reports` onto the first report's base:
    each repository's rounds in order, then the units that conflict with the base, each stack parent first. PRs left
    unordered appear as comments with the reason."""
    if not reports:
        raise MergeOrderError("no report to write a compose conf from")
    first = reports[0]
    out = [f"# {', '.join(r['repo'] for r in reports)} at base {first['base']} ({first['base_oid'][:10]})",
           f"base   {first['repo']} {first['base']}", ""]
    for r in reports:
        if r["base_oid"] != first["base_oid"]:
            out.append(f"# {r['repo']} {r['base']} is at {r['base_oid'][:10]}, not {first['base_oid'][:10]}")
        entries = [e for rnd in r["rounds"] for e in rnd] + r["conflicts_with_base"]
        for e in entries:
            gone = [b for b in e["branches"] if not b["repo"] or "#" in b["branch"]]
            if gone:
                out.append(f"# left out: {e['label']} {e['title']}: a head repository is gone or a branch name "
                           f"holds '#', which the conf cannot carry")
                continue
            out.append(f"# {e['label']} {e['title']}")
            out += [f"{b['repo']} {b['branch']}" for b in e["branches"]]
        out += [f"# not ordered: #{s['pr']} {s['reason']}" for s in r["skipped"]]
        out.append("")
    return "\n".join(out)
