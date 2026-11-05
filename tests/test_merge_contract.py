"""Tests for merge_source_into_current's four outcomes and the claude resolver call."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multibranch_builder import merge
from multibranch_builder.errors import ComposeError
from multibranch_builder.merge import (
    AI_RESOLVED, CONFLICT, MERGED, UP_TO_DATE, ai_resolve, files_with_conflict_markers,
    merge_prompt, merge_source_into_current,
)

XRPLD_GUIDE = Path(merge.__file__).parent / "targets" / "xrpld" / "merge.md"
PINNED = ("Numbers are pinned by the operator's registry: keep every entry's number exactly as its branch "
          "has it, never renumber, and when two sides give one number to different entries leave that "
          "hunk's conflict markers in place.")
ABORT = ["git", "merge", "--abort"]
RESET = ["git", "reset", "--hard", "HEAD"]
VERIFY = ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"]
HEAD = ["git", "rev-parse", "HEAD"]
ORIG = ["git", "rev-parse", "ORIG_HEAD"]
RESET_ORIG = ["git", "reset", "--hard", "ORIG_HEAD"]


def _completed(returncode=0, stdout="", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class FakeGit:
    """Answers the git commands merge_source_into_current issues."""

    def __init__(self, merge_rc=0, conflicted=(), staged_rc=1, abort_rc=0, merge_head_kept=False,
                 head_moved=False, head_stays_moved=False):
        self.merge_rc = merge_rc
        self.conflicted = list(conflicted)
        self.staged_rc = staged_rc
        self.abort_rc = abort_rc
        self.merge_head_kept = merge_head_kept
        self.head_moved = head_moved
        self.head_stays_moved = head_stays_moved
        self.calls = []

    def __call__(self, cmd, cwd, check=True):
        self.calls.append(cmd)
        if cmd == ABORT:
            return _completed(self.abort_rc)
        if cmd == VERIFY:
            return _completed(0 if self.merge_head_kept else 1)
        if cmd == RESET_ORIG and not self.head_stays_moved:
            self.head_moved = False
        if cmd == HEAD:
            return _completed(stdout="bbb\n" if self.head_moved else "aaa\n")
        if cmd == ORIG:
            return _completed(stdout="aaa\n")
        if cmd[:2] == ["git", "merge"]:
            return _completed(self.merge_rc)
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return _completed(stdout="\n".join(self.conflicted))
        if cmd[:3] == ["git", "diff", "--cached"]:
            return _completed(self.staged_rc)
        return _completed()


def _without_head_reads(calls):
    return [c for c in calls if c not in (HEAD, ORIG)]


def test_up_to_date_when_nothing_staged():
    git = FakeGit(merge_rc=0, staged_rc=0)
    with patch.object(merge, "_run", git):
        assert merge_source_into_current("/w", "r/b", "b", "develop", resolver=MagicMock()) == UP_TO_DATE
    assert git.calls[0] == ["git", "merge", "--no-commit", "--no-ff", "r/b"]


def test_merged_when_clean_and_staged():
    git = FakeGit(merge_rc=0, staged_rc=1)
    resolver = MagicMock()
    with patch.object(merge, "_run", git):
        assert merge_source_into_current("/w", "r/b", "b", "develop", resolver=resolver) == MERGED
    resolver.assert_not_called()


def test_ai_resolved_when_rerere_or_driver_finished_the_merge():
    git = FakeGit(merge_rc=1, conflicted=())
    resolver = MagicMock()
    with patch.object(merge, "_run", git):
        assert merge_source_into_current("/w", "r/b", "b", "develop", resolver=resolver) == AI_RESOLVED
    resolver.assert_not_called()


def test_ai_resolved_when_resolver_succeeds():
    git = FakeGit(merge_rc=1, conflicted=["src/a.cpp"])
    resolver = MagicMock(return_value=True)
    with patch.object(merge, "_run", git):
        assert merge_source_into_current("/w", "r/b", "b", "develop", resolver=resolver) == AI_RESOLVED
    resolver.assert_called_once_with("/w", "b", "develop")
    assert ["git", "merge", "--abort"] not in git.calls


def test_conflict_aborts_when_resolver_fails():
    git = FakeGit(merge_rc=1, conflicted=["src/a.cpp"])
    with patch.object(merge, "_run", git):
        assert merge_source_into_current("/w", "r/b", "b", "develop", resolver=MagicMock(return_value=False)) == CONFLICT
    assert _without_head_reads(git.calls)[-2:] == [ABORT, VERIFY]
    assert RESET not in git.calls


def test_conflict_resets_when_abort_fails():
    git = FakeGit(merge_rc=1, conflicted=["src/a.cpp"], abort_rc=128)
    with patch.object(merge, "_run", git):
        assert merge_source_into_current("/w", "r/b", "b", "develop", resolver=MagicMock(return_value=False)) == CONFLICT
    assert _without_head_reads(git.calls)[-3:] == [ABORT, RESET, VERIFY]


def test_conflict_resets_a_merge_the_resolver_committed():
    git = FakeGit(merge_rc=1, conflicted=["src/a.cpp"], head_moved=True)
    with patch.object(merge, "_run", git):
        assert merge_source_into_current("/w", "r/b", "b", "develop", resolver=MagicMock(return_value=False)) == CONFLICT
    assert RESET_ORIG in git.calls


def test_raises_when_head_stays_off_the_pre_merge_commit():
    git = FakeGit(merge_rc=1, conflicted=["src/a.cpp"], head_moved=True, head_stays_moved=True)
    with patch.object(merge, "_run", git), pytest.raises(ComposeError, match="pre-merge"):
        merge_source_into_current("/w", "r/b", "b", "develop", resolver=MagicMock(return_value=False))


def test_raises_when_merge_head_survives_the_abort():
    git = FakeGit(merge_rc=1, conflicted=["src/a.cpp"], abort_rc=128, merge_head_kept=True)
    with patch.object(merge, "_run", git), pytest.raises(ComposeError, match="MERGE_HEAD"):
        merge_source_into_current("/w", "r/b", "b", "develop", resolver=MagicMock(return_value=False))
    assert _without_head_reads(git.calls)[-3:] == [ABORT, RESET, VERIFY]


def test_files_with_conflict_markers(tmp_path):
    (tmp_path / "clean.cpp").write_text("int x;\n")
    (tmp_path / "dirty.cpp").write_text("<<<<<<< HEAD\nint x;\n=======\nint y;\n>>>>>>> theirs\n")
    (tmp_path / "arrows.cpp").write_text("if (a <<<<<<< b) {}\n")
    files = ["clean.cpp", "dirty.cpp", "arrows.cpp", "gone.cpp"]
    assert files_with_conflict_markers(str(tmp_path), files) == ["dirty.cpp"]


def test_merge_prompt_names_files_and_carries_guide():
    prompt = merge_prompt(["a.cpp", "b.h"], "XRPLF/rippled@x", "XRPLF/rippled@develop",
                          guide=XRPLD_GUIDE.read_text())
    assert "a.cpp\nb.h" in prompt
    assert "XRPLF/rippled@x" in prompt
    assert "registry-number collisions" in prompt


def test_merge_prompt_pinned_appends_the_pinned_paragraph():
    guide = XRPLD_GUIDE.read_text()
    prompt = merge_prompt(["a.cpp"], "XRPLF/rippled@x", "XRPLF/rippled@develop", guide=guide, pinned=True)
    assert prompt.endswith("\n\n" + PINNED)
    assert PINNED not in merge_prompt(["a.cpp"], "XRPLF/rippled@x", "XRPLF/rippled@develop", guide=guide)


def test_merge_prompt_without_guide_is_the_task_alone():
    prompt = merge_prompt(["a.ts"], "XRPLF/xrpl.js@x", "XRPLF/xrpl.js@main")
    assert prompt.endswith("Conflicted files:\na.ts")
    assert "registry-number collisions" not in prompt


@patch("multibranch_builder.merge.shutil.which", return_value="/usr/bin/claude")
def test_ai_resolve_invokes_claude_with_budget_and_timeout(mock_which, tmp_path):
    conflicted = ["src/a.cpp"]
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.cpp").write_text("resolved\n")
    state = {"conflicted": conflicted}

    def fake_run(cmd, cwd, check=True):
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return _completed(stdout="\n".join(state["conflicted"]))
        if cmd[:2] == ["git", "add"]:
            state["conflicted"] = []
        return _completed()

    with patch.object(merge, "_run", fake_run), patch("multibranch_builder.merge.subprocess.run") as claude:
        claude.return_value = _completed()
        assert ai_resolve(str(tmp_path), "b", "develop", guide_path=XRPLD_GUIDE,
                          budget_usd=2.5, timeout_s=42) is True
    cmd = claude.call_args.args[0]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--max-budget-usd") + 1] == "2.5"
    assert claude.call_args.kwargs["timeout"] == 42
    assert "registry-number collisions" in cmd[2]


@patch("multibranch_builder.merge.shutil.which", return_value="/usr/bin/claude")
def test_ai_resolve_forwards_pinned_to_the_prompt(mock_which, tmp_path):
    (tmp_path / "a.cpp").write_text("resolved\n")
    state = {"conflicted": ["a.cpp"]}

    def fake_run(cmd, cwd, check=True):
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return _completed(stdout="\n".join(state["conflicted"]))
        if cmd[:2] == ["git", "add"]:
            state["conflicted"] = []
        return _completed()

    with patch.object(merge, "_run", fake_run), patch("multibranch_builder.merge.subprocess.run") as claude:
        claude.return_value = _completed()
        assert ai_resolve(str(tmp_path), "b", "develop", guide_path=XRPLD_GUIDE, pinned=True) is True
    assert claude.call_args.args[0][2].endswith(PINNED)


@patch("multibranch_builder.merge.shutil.which", return_value="/usr/bin/claude")
def test_ai_resolve_false_on_timeout(mock_which, tmp_path):
    with patch.object(merge, "_run", lambda cmd, cwd, check=True: _completed(stdout="a.cpp")), \
         patch("multibranch_builder.merge.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 1)):
        assert ai_resolve(str(tmp_path), "b", "develop") is False


@patch("multibranch_builder.merge.shutil.which", return_value="/usr/bin/claude")
def test_ai_resolve_false_when_markers_remain(mock_which, tmp_path):
    (tmp_path / "a.cpp").write_text("<<<<<<< HEAD\n=======\n>>>>>>> x\n")
    with patch.object(merge, "_run", lambda cmd, cwd, check=True: _completed(stdout="a.cpp")), \
         patch("multibranch_builder.merge.subprocess.run", return_value=_completed()):
        assert ai_resolve(str(tmp_path), "b", "develop") is False


@patch("multibranch_builder.merge.shutil.which", return_value=None)
def test_ai_resolve_false_without_claude_cli(mock_which):
    with patch.object(merge, "_run", lambda cmd, cwd, check=True: _completed(stdout="a.cpp")), \
         patch("multibranch_builder.merge.subprocess.run") as claude:
        assert ai_resolve("/w", "b", "develop") is False
    claude.assert_not_called()


def test_ai_resolve_true_when_nothing_conflicted():
    with patch.object(merge, "_run", lambda cmd, cwd, check=True: _completed(stdout="")):
        assert ai_resolve("/w", "b", "develop") is True
