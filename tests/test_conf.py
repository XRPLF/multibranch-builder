"""Tests for the conf grammar and github URL parsing."""

import pytest

from multibranch_builder.conf import BranchEntry, ConfError, parse_config, parse_config_text

ALPHANET = """\
# alphanet
base   XRPLF/rippled develop
target Transia-RnD/rippled alphanet

XRPLF/rippled xrplf/smart-contracts
XRPLF/rippled dangell7/subscriptions rebase   # keep in step with develop
XRPLF/rippled dangell7/datagram
XRPLF/rippled dangell7/export-ledger-name-spaces
XRPLF/rippled dangell7/supported-cicd
"""


def test_parse_headers_and_entries():
    cfg = parse_config_text(ALPHANET)
    assert cfg.base == BranchEntry("XRPLF", "rippled", "develop")
    assert cfg.target == BranchEntry("Transia-RnD", "rippled", "alphanet")
    assert [b.branch for b in cfg.branches] == [
        "xrplf/smart-contracts", "dangell7/subscriptions", "dangell7/datagram",
        "dangell7/export-ledger-name-spaces", "dangell7/supported-cicd",
    ]
    assert cfg.branches[1].rebase is True
    assert cfg.branches[0].rebase is False


def test_parse_config_from_file(tmp_path):
    path = tmp_path / "alphanet.conf"
    path.write_text(ALPHANET)
    assert len(parse_config(path).branches) == 5


def test_target_is_optional():
    cfg = parse_config_text("base XRPLF/rippled develop\nXRPLF/rippled a/b\n")
    assert cfg.target is None


def test_missing_base_is_error():
    with pytest.raises(ConfError, match="no `base"):
        parse_config_text("XRPLF/rippled a/b\n")


def test_duplicate_base_is_error():
    with pytest.raises(ConfError, match="second `base`"):
        parse_config_text("base o/r a\nbase o/r b\n")


def test_bad_entry_is_error_with_line_number():
    with pytest.raises(ConfError, match="<conf>:2"):
        parse_config_text("base o/r a\nnot-a-slug branch\n")


def test_third_field_must_be_rebase():
    with pytest.raises(ConfError, match="rebase"):
        parse_config_text("base o/r a\no/r b squash\n")


def test_duplicate_entry_is_error():
    with pytest.raises(ConfError, match="duplicate"):
        parse_config_text("base o/r a\no/r b\no/r b\n")


def test_entry_properties():
    e = BranchEntry("XRPLF", "rippled", "dangell7/datagram")
    assert e.slug == "XRPLF/rippled"
    assert e.url == "https://github.com/XRPLF/rippled.git"
    assert e.remote == "XRPLF-rippled"
    assert e.ref == "XRPLF-rippled/dangell7/datagram"
    assert e.label == "XRPLF/rippled@dangell7/datagram"


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/XRPLF/rippled/tree/dangell7/datagram", ("XRPLF", "rippled", "dangell7/datagram")),
    ("https://github.com/Transia-RnD/rippled/commit/" + "a" * 40, ("Transia-RnD", "rippled", "a" * 40)),
    ("https://github.com/XRPLF/rippled", ("XRPLF", "rippled", "develop")),
    ("https://github.com/XRPLF/rippled.git", ("XRPLF", "rippled", "develop")),
])
def test_from_url(url, expected):
    e = BranchEntry.from_url(url)
    assert (e.owner, e.repo, e.branch) == expected


def test_from_url_rejects_other_hosts():
    with pytest.raises(ConfError):
        BranchEntry.from_url("https://gitlab.com/x/y/tree/z")


def test_parse_accepts_slug_and_url():
    assert BranchEntry.parse("XRPLF/rippled@develop") == BranchEntry("XRPLF", "rippled", "develop")
    assert BranchEntry.parse("XRPLF/rippled", "datagram").branch == "datagram"
    assert BranchEntry.parse("https://github.com/XRPLF/rippled/tree/x").branch == "x"


def test_is_commit():
    assert BranchEntry("o", "r", "f" * 40).is_commit
    assert not BranchEntry("o", "r", "develop").is_commit


def test_kind_and_setting_headers():
    cfg = parse_config_text("kind xrpld\nbase XRPLF/xrpl.js main\ndefinitions https://alphanet.xrpl.org\n"
                            "XRPLF/xrpl.js smart-contracts\n")
    assert cfg.kind == "xrpld"
    assert cfg.settings == {"definitions": "https://alphanet.xrpl.org"}
    assert cfg.base.branch == "main"
    assert [b.branch for b in cfg.branches] == ["smart-contracts"]


def test_headers_default_to_none_and_empty():
    cfg = parse_config_text(ALPHANET)
    assert cfg.kind is None
    assert cfg.settings == {}


def test_setting_value_keeps_its_words():
    assert parse_config_text("base o/r a\nnote hello world\n").settings == {"note": "hello world"}


def test_duplicate_setting_is_error():
    with pytest.raises(ConfError, match="second `definitions`"):
        parse_config_text("base o/r a\ndefinitions x\ndefinitions y\n")


def test_header_needs_a_value():
    with pytest.raises(ConfError, match="<conf>:2"):
        parse_config_text("base o/r a\ndefinitions\n")


def test_kind_takes_one_word():
    with pytest.raises(ConfError, match="expected `kind <name>`"):
        parse_config_text("kind a b\nbase o/r a\n")
    with pytest.raises(ConfError, match="second `kind`"):
        parse_config_text("kind a\nkind b\nbase o/r a\n")
