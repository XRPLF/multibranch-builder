"""The kind registry and the xrpld kind's options, plan and merge setup."""

import subprocess

import pytest

from multibranch_builder.cli import parse_set
from multibranch_builder.conf import BranchEntry, ConfError, parse_config_text
from multibranch_builder.targets import KINDS, for_base, for_config, for_name

XRPLD = KINDS["xrpld"]


def test_for_base_maps_rippled_and_xrpld_forks():
    assert for_base("XRPLF/rippled") is XRPLD
    assert for_base("Transia-RnD/xrpld-cp-private") is XRPLD


def test_for_base_unknown_repo_is_conf_error():
    with pytest.raises(ConfError, match="no kind for base repository 'XRPLF/xrpl-py'"):
        for_base("XRPLF/xrpl-py")


def test_for_name_unknown_kind_is_conf_error():
    with pytest.raises(ConfError, match="unknown kind 'nope'"):
        for_name("nope")


def test_for_config_kind_header_wins_over_base():
    assert for_config(parse_config_text("kind xrpld\nbase XRPLF/xrpl-py main\n")) is XRPLD
    assert for_config(parse_config_text("base XRPLF/rippled develop\n")) is XRPLD


def test_xrpld_options_default_and_normalise():
    assert XRPLD.validate_options({}) == {"force_supported": "OFF"}
    out = XRPLD.validate_options({"force_supported": "ON",
                                  "datagram": "https://github.com/XRPLF/rippled/tree/dangell7/datagram"})
    assert out == {"force_supported": "ON", "datagram": "XRPLF/rippled@dangell7/datagram"}
    assert XRPLD.validate_options({"datagram": "XRPLF/rippled"})["datagram"] == "XRPLF/rippled@datagram"


def test_xrpld_options_reject_unknown_key_and_bad_value():
    with pytest.raises(ConfError, match="does not accept option 'nope'"):
        XRPLD.validate_options({"nope": "1"})
    with pytest.raises(ConfError, match="ON or OFF"):
        XRPLD.validate_options({"force_supported": "yes"})


def test_xrpld_settings_reject_any_key():
    XRPLD.validate_settings({})
    with pytest.raises(ConfError, match="does not accept setting 'definitions'"):
        XRPLD.validate_settings({"definitions": "x"})


def test_xrpld_plan_prepends_datagram():
    a, b = BranchEntry("o", "r", "a"), BranchEntry("o", "r", "b")
    assert XRPLD.plan([a, b], {"datagram": "o/r@dg"}) == [BranchEntry("o", "r", "dg"), a, b]
    assert XRPLD.plan([a, b], {}) == [a, b]


def test_xrpld_image_suffix_marks_datagram():
    assert XRPLD.image_suffix({"datagram": "o/r@dg"}) == "-dg"
    assert XRPLD.image_suffix({}) == ""


def test_xrpld_prepare_is_a_noop(tmp_path):
    assert XRPLD.prepare(tmp_path, {}, {}) == ({}, [])


def test_xrpld_configure_merge_registers_driver_attributes_and_rerere(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    XRPLD.configure_merge(str(tmp_path))

    def config(key):
        return subprocess.run(["git", "config", key], cwd=tmp_path, text=True, capture_output=True).stdout.strip()

    assert config("merge.xrplregistry.driver").endswith("registry_merge.py %O %A %B %P")
    assert config("rerere.enabled") == "true" and config("rerere.autoUpdate") == "true"
    attributes = (tmp_path / ".git" / "info" / "attributes").read_text()
    assert attributes.count("merge=xrplregistry") == 5
    XRPLD.configure_merge(str(tmp_path))
    assert (tmp_path / ".git" / "info" / "attributes").read_text() == attributes


def test_parse_set_pairs():
    assert parse_set(["a=1", "b=x=y", "c="]) == {"a": "1", "b": "x=y", "c": ""}
    with pytest.raises(ConfError, match="KEY=VALUE"):
        parse_set(["bare"])
