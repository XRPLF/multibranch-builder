"""CLI plan output and the push gate."""

import json
from unittest.mock import patch

import pytest

from multibranch_builder import cli
from multibranch_builder.compose import BranchOutcome, Manifest

CONF = """\
base XRPLF/rippled develop
target Transia-RnD/rippled alphanet
XRPLF/rippled xrplf/smart-contracts
XRPLF/rippled dangell7/subscriptions
"""


def test_compose_dry_run_prints_plan_without_cloning(tmp_path, capsys):
    conf = tmp_path / "alphanet.conf"
    conf.write_text(CONF)
    with patch("multibranch_builder.cli.compose") as mock_compose:
        rc = cli.main(["compose", "--conf", str(conf), "--workdir", str(tmp_path / "w"), "--dry-run",
                       "--datagram", "XRPLF/rippled@dangell7/datagram"])
    assert rc == 0
    mock_compose.assert_not_called()
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "base    XRPLF/rippled@develop",
        "merge  1 XRPLF/rippled@dangell7/datagram  (datagram)",
        "merge  2 XRPLF/rippled@xrplf/smart-contracts",
        "merge  3 XRPLF/rippled@dangell7/subscriptions",
        "target  Transia-RnD/rippled@alphanet",
        "force_supported OFF",
    ]
    assert not (tmp_path / "w").exists()


def test_compose_conf_excludes_src(tmp_path):
    conf = tmp_path / "c"
    conf.write_text(CONF)
    with pytest.raises(SystemExit, match="excludes"):
        cli.main(["compose", "--conf", str(conf), "--src", "https://github.com/XRPLF/rippled", "--workdir", "w"])


def _files(tmp_path, status="SUCCESS", outcome="merged", target="Transia-RnD/rippled@alphanet"):
    manifest = Manifest(base="XRPLF/rippled@develop", base_sha="b" * 40,
                        branches=[BranchOutcome("XRPLF/rippled", "x", "d" * 40, outcome)],
                        composed_sha="c" * 40, target=target)
    manifest.write(tmp_path)
    (tmp_path / "build.json").write_text(json.dumps({"status": status, "image": "img"}))
    return manifest


def _push_args(tmp_path, *extra):
    return ["push", "--tree", str(tmp_path / "rippled"), "--manifest", str(tmp_path / "manifest.json"),
            "--build", str(tmp_path / "build.json"), *extra]


def test_push_refuses_without_build_json(tmp_path):
    _files(tmp_path)
    (tmp_path / "build.json").unlink()
    with pytest.raises(SystemExit, match="not found"):
        cli.main(_push_args(tmp_path))


def test_push_refuses_unless_build_success(tmp_path):
    _files(tmp_path, status="FAILURE")
    with pytest.raises(SystemExit, match="not SUCCESS"):
        cli.main(_push_args(tmp_path))


def test_push_refuses_conflicted_manifest(tmp_path):
    _files(tmp_path, outcome="conflict")
    with pytest.raises(SystemExit, match="unmerged"):
        cli.main(_push_args(tmp_path))


def test_push_requires_a_target(tmp_path):
    _files(tmp_path, target=None)
    with pytest.raises(SystemExit, match="no --target"):
        cli.main(_push_args(tmp_path))


@patch("multibranch_builder.cli.git_push")
def test_push_signs_commits_trailer_and_force_pushes_with_lease(mock_gp, tmp_path, capsys):
    manifest = _files(tmp_path)
    mock_gp._run.return_value.stdout = "c" * 40 + "\n"
    mock_gp.commit_all.return_value = "e" * 40
    rc = cli.main(_push_args(tmp_path))
    assert rc == 0
    mock_gp.setup_signing.assert_called_once_with(str(tmp_path / "rippled"))
    tree, message = mock_gp.commit_all.call_args.args
    assert mock_gp.commit_all.call_args.kwargs == {"include_untracked": False, "allow_empty": True}
    assert message.startswith("compose: alphanet from XRPLF/rippled@develop @ bbbbbbbb (1 branches)\n\n")
    assert message.endswith(manifest.trailer())
    mock_gp.push.assert_called_once_with(str(tmp_path / "rippled"), "Transia-RnD", "rippled", "alphanet", force=True)
    assert capsys.readouterr().out.strip() == f"pushed {'e' * 40} -> Transia-RnD/rippled@alphanet"


@patch("multibranch_builder.cli.git_push")
def test_push_refuses_tree_that_is_not_the_manifest(mock_gp, tmp_path):
    _files(tmp_path)
    mock_gp._run.return_value.stdout = "f" * 40 + "\n"
    with pytest.raises(SystemExit, match="composed_sha"):
        cli.main(_push_args(tmp_path))
    mock_gp.push.assert_not_called()


def test_manifest_prints_trailer(tmp_path, capsys):
    manifest = _files(tmp_path)
    assert cli.main(["manifest", str(tmp_path)]) == 0
    assert capsys.readouterr().out == manifest.trailer() + "\n"
    cli.main(["manifest", str(tmp_path), "--markdown"])
    assert "| XRPLF/rippled | x |" in capsys.readouterr().out
