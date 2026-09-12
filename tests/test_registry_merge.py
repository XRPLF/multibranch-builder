"""Unit tests for the structured registry merge driver."""
import subprocess
import sys
from pathlib import Path

import pytest

from xrpld_compose.registry_merge import merge3, parse

DRIVER = Path(__file__).parents[1] / "xrpld_compose" / "registry_merge.py"


FEATURES_BASE = """\
// features registry
XRPL_FEATURE(BatchV1_1,                   Supported::Yes, VoteBehavior::DefaultNo)
XRPL_FEATURE(ConfidentialTransfer,        Supported::Yes, VoteBehavior::DefaultNo)
"""


def test_features_union_appends_both_sides():
    ours = FEATURES_BASE + "XRPL_FEATURE(CouponPayments, Supported::Yes, VoteBehavior::DefaultNo)\n"
    theirs = FEATURES_BASE + "XRPL_FEATURE(OfferQualifiers, Supported::Yes, VoteBehavior::DefaultNo)\n"
    merged = merge3(FEATURES_BASE, ours, theirs, "features.macro")
    assert merged is not None
    assert "CouponPayments" in merged and "OfferQualifiers" in merged
    # ours' layout preserved: preamble comment first
    assert merged.startswith("// features registry")


def test_features_identical_addition_not_duplicated():
    added = FEATURES_BASE + "XRPL_FEATURE(Same, Supported::Yes, VoteBehavior::DefaultNo)\n"
    merged = merge3(FEATURES_BASE, added, added, "features.macro")
    assert merged is not None
    assert merged.count("XRPL_FEATURE(Same") == 1


def test_both_modified_same_entry_is_conflict():
    ours = FEATURES_BASE.replace("Supported::Yes, VoteBehavior::DefaultNo)",
                                 "Supported::Yes, VoteBehavior::DefaultYes)", 1)
    theirs = FEATURES_BASE.replace("Supported::Yes, VoteBehavior::DefaultNo)",
                                   "Supported::No,  VoteBehavior::DefaultNo)", 1)
    assert merge3(FEATURES_BASE, ours, theirs, "features.macro") is None


def test_theirs_modification_taken_when_ours_untouched():
    theirs = FEATURES_BASE.replace(
        "XRPL_FEATURE(BatchV1_1,                   Supported::Yes",
        "XRPL_FEATURE(BatchV1_1,                   Supported::No ", 1)
    merged = merge3(FEATURES_BASE, FEATURES_BASE, theirs, "features.macro")
    assert merged is not None
    assert "Supported::No " in merged


LEDGER_BASE = """\
#if !defined(LEDGER_ENTRY)
#error "undefined macro: LEDGER_ENTRY"
#endif
LEDGER_ENTRY(ltCHECK, 0x0043, Check, check, ({
    {sfAccount, soeREQUIRED},
}))
LEDGER_ENTRY(ltDID, 0x0049, DID, did, ({
    {sfAccount, soeREQUIRED},
}))
"""


def test_ledger_entry_number_collision_renumbered():
    ours = LEDGER_BASE + (
        "LEDGER_ENTRY(ltPASSKEY, 0x0084, Passkey, passkey, ({\n"
        "    {sfAccount, soeREQUIRED},\n"
        "}))\n"
    )
    theirs = LEDGER_BASE + (
        "LEDGER_ENTRY(ltCOUPON, 0x0084, Coupon, coupon, ({\n"
        "    {sfAccount, soeREQUIRED},\n"
        "}))\n"
    )
    merged = merge3(LEDGER_BASE, ours, theirs, "ledger_entries.macro")
    assert merged is not None
    assert "ltPASSKEY, 0x0084" in merged            # ours keeps its number
    assert "ltCOUPON, 0x0085" in merged             # incoming renumbered past max
    assert merged.count("0x0084") == 1


def test_ledger_entry_no_collision_keeps_number():
    theirs = LEDGER_BASE + (
        "LEDGER_ENTRY(ltCOUPON, 0x0090, Coupon, coupon, ({\n"
        "    {sfAccount, soeREQUIRED},\n"
        "}))\n"
    )
    merged = merge3(LEDGER_BASE, LEDGER_BASE, theirs, "ledger_entries.macro")
    assert merged is not None
    assert "ltCOUPON, 0x0090" in merged


TX_BASE = """\
TRANSACTION(ttPAYMENT, 0, Payment,
    Delegation::delegatable,
    ({
        {sfDestination, soeREQUIRED},
    }))
TRANSACTION(ttESCROW_CREATE, 1, EscrowCreate,
    Delegation::delegatable,
    ({
        {sfDestination, soeREQUIRED},
    }))
"""


def test_transaction_decimal_collision_renumbered():
    ours = TX_BASE + (
        "TRANSACTION(ttCOUPON, 70, Coupon,\n"
        "    Delegation::delegatable,\n"
        "    ({\n"
        "        {sfDestination, soeREQUIRED},\n"
        "    }))\n"
    )
    theirs = TX_BASE + (
        "TRANSACTION(ttQUALIFIER, 70, Qualifier,\n"
        "    Delegation::delegatable,\n"
        "    ({\n"
        "        {sfDestination, soeREQUIRED},\n"
        "    }))\n"
    )
    merged = merge3(TX_BASE, ours, theirs, "transactions.macro")
    assert merged is not None
    assert "ttCOUPON, 70" in merged
    assert "ttQUALIFIER, 71" in merged


SFIELDS_BASE = """\
TYPED_SFIELD(sfSequence,            UINT32,     4)
TYPED_SFIELD(sfFlags,               UINT32,     2)
TYPED_SFIELD(sfHookHash,            UINT256,    31)
"""


def test_sfield_collision_scoped_by_type():
    ours = SFIELDS_BASE + "TYPED_SFIELD(sfCouponRate,          UINT32,     90)\n"
    theirs = SFIELDS_BASE + (
        "TYPED_SFIELD(sfQualifierFlags,      UINT32,     90)\n"
        "TYPED_SFIELD(sfQualifierHash,       UINT256,    90)\n"
    )
    merged = merge3(SFIELDS_BASE, ours, theirs, "sfields.macro")
    assert merged is not None
    assert "sfCouponRate,          UINT32,     90" in merged
    assert "sfQualifierFlags,      UINT32,     91" in merged
    # UINT256/90 does not collide with UINT32/90 — different scope
    assert "sfQualifierHash,       UINT256,    90" in merged


JSS_BASE = """\
JSS(AL_size);                     // out: GetCounts
JSS(Account);                     // in: TransactionSign
"""


def test_jss_union_and_dedupe():
    ours = JSS_BASE + "JSS(coupon_rate);                 // out: Coupon\n"
    theirs = JSS_BASE + (
        "JSS(coupon_rate);                 // out: Coupon\n"
        "JSS(qualifier);                   // out: Qualifier\n"
    )
    merged = merge3(JSS_BASE, ours, theirs, "jss.h")
    assert merged is not None
    assert merged.count("JSS(coupon_rate)") == 1
    assert "JSS(qualifier)" in merged


def test_unparseable_returns_none():
    broken = "LEDGER_ENTRY(ltX, 0x0001, X, x, ({\n"  # unbalanced
    assert merge3(LEDGER_BASE, broken, LEDGER_BASE, "ledger_entries.macro") is None


def test_parse_roundtrip_preserves_text():
    chunks = parse(LEDGER_BASE)
    assert chunks is not None
    rebuilt = "".join(c if isinstance(c, str) else c.text for c in chunks)
    assert rebuilt == LEDGER_BASE


def test_driver_cli_resolves_and_writes_ours(tmp_path):
    base = tmp_path / "base"
    ours = tmp_path / "ours"
    theirs = tmp_path / "theirs"
    base.write_text(FEATURES_BASE)
    ours.write_text(FEATURES_BASE + "XRPL_FEATURE(A, Supported::Yes, VoteBehavior::DefaultNo)\n")
    theirs.write_text(FEATURES_BASE + "XRPL_FEATURE(B, Supported::Yes, VoteBehavior::DefaultNo)\n")
    proc = subprocess.run(
        [sys.executable, str(DRIVER), str(base), str(ours), str(theirs),
         "include/xrpl/protocol/detail/features.macro"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = ours.read_text()
    assert "XRPL_FEATURE(A" in result and "XRPL_FEATURE(B" in result


def test_driver_cli_unhandled_file_exits_nonzero(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    proc = subprocess.run(
        [sys.executable, str(DRIVER), str(f), str(f), str(f), "src/other.cpp"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
