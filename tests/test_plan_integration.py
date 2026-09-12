"""Unit tests for the incremental-integration planner."""
from multibranch_builder.merge import plan_integration

DEV_OLD = "d" * 40
DEV_NEW = "e" * 40
ALPHA = "a" * 40
TIP1_OLD, TIP1_NEW = "1" * 40, "f1" + "1" * 38
TIP2 = "2" * 40


def _manifest(develop=DEV_OLD, alphanet=ALPHA, branches=None):
    return {"develop": develop, "alphanet": alphanet,
            "branches": branches if branches is not None else {}}


def _state(tip, extends=True):
    return {"tip": tip, "extends_manifest": extends}


def test_no_manifest_forces_full():
    plan, _ = plan_integration(None, DEV_NEW, False, ALPHA, {}, from_scratch=False)
    assert plan == "full"


def test_from_scratch_flag_forces_full():
    m = _manifest(branches={"origin/b1": TIP1_OLD})
    plan, _ = plan_integration(
        m, DEV_OLD, True, ALPHA, {"origin/b1": _state(TIP1_OLD)}, from_scratch=True)
    assert plan == "full"


def test_alphanet_moved_externally_forces_full():
    m = _manifest(branches={"origin/b1": TIP1_OLD})
    plan, _ = plan_integration(
        m, DEV_OLD, True, "x" * 40, {"origin/b1": _state(TIP1_OLD)}, from_scratch=False)
    assert plan == "full"


def test_branch_removed_forces_full():
    m = _manifest(branches={"origin/b1": TIP1_OLD, "origin/b2": TIP2})
    plan, _ = plan_integration(
        m, DEV_OLD, True, ALPHA, {"origin/b1": _state(TIP1_OLD)}, from_scratch=False)
    assert plan == "full"


def test_non_fast_forward_tip_forces_full():
    m = _manifest(branches={"origin/b1": TIP1_OLD})
    plan, _ = plan_integration(
        m, DEV_OLD, True, ALPHA,
        {"origin/b1": _state(TIP1_NEW, extends=False)}, from_scratch=False)
    assert plan == "full"


def test_develop_rewritten_forces_full():
    m = _manifest(branches={"origin/b1": TIP1_OLD})
    plan, _ = plan_integration(
        m, DEV_NEW, False, ALPHA, {"origin/b1": _state(TIP1_OLD)}, from_scratch=False)
    assert plan == "full"


def test_nothing_changed_fast_with_no_deltas():
    m = _manifest(branches={"origin/b1": TIP1_OLD})
    plan, deltas = plan_integration(
        m, DEV_OLD, True, ALPHA, {"origin/b1": _state(TIP1_OLD)}, from_scratch=False)
    assert plan == "fast"
    assert deltas == []


def test_fast_forward_tip_is_delta():
    m = _manifest(branches={"origin/b1": TIP1_OLD})
    plan, deltas = plan_integration(
        m, DEV_OLD, True, ALPHA, {"origin/b1": _state(TIP1_NEW)}, from_scratch=False)
    assert plan == "fast"
    assert deltas == ["origin/b1"]


def test_new_branch_is_delta():
    m = _manifest(branches={"origin/b1": TIP1_OLD})
    plan, deltas = plan_integration(
        m, DEV_OLD, True, ALPHA,
        {"origin/b1": _state(TIP1_OLD), "origin/b2": _state(TIP2)}, from_scratch=False)
    assert plan == "fast"
    assert deltas == ["origin/b2"]


def test_develop_advance_alone_is_fast_no_branch_deltas():
    m = _manifest(develop=DEV_OLD, branches={"origin/b1": TIP1_OLD})
    plan, deltas = plan_integration(
        m, DEV_NEW, True, ALPHA, {"origin/b1": _state(TIP1_OLD)}, from_scratch=False)
    assert plan == "fast"
    assert deltas == []
