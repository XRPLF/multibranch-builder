"""Tag derivation, Cloud Build argv, and --ar/--set propagation into the xrpld submission."""

import json
from unittest.mock import patch

import pytest

from multibranch_builder import cli, cloudbuild
from multibranch_builder.build import BuildError, image_tag
from multibranch_builder.compose import BranchOutcome, Manifest
from multibranch_builder.conf import BranchEntry
from multibranch_builder.targets.xrpld import build as xbuild

SHA = "0123456789abcdef0123456789abcdef01234567"
XBUILD = "multibranch_builder.targets.xrpld.build"


@pytest.mark.parametrize("ref,suffix,expected", [
    ("dangell7/parallel-apply-access-set", "", "dangell7-parallel-apply-access-set-01234567"),
    ("dangell7/parallel-apply-access-set", "-dg", "dangell7-parallel-apply-access-set-01234567-dg"),
    ("alphanet", "", "alphanet-01234567"),
    ("feat/x y@z", "", "feat-x-y-z-01234567"),
    (SHA, "", "01234567"),
    (SHA, "-dg", "01234567-dg"),
])
def test_image_tag(ref, suffix, expected):
    assert image_tag(ref, SHA, suffix) == expected


def test_submit_command_uses_pool_with_region():
    cmd = cloudbuild.submit_command("/stage", "/cfg.yaml", {"_AR": "reg/xrpld", "_TAG": "t"},
                                    "xrplf-alphanet", "xrpld-pool")
    assert "--worker-pool" in cmd
    assert cmd[cmd.index("--worker-pool") + 1] == "projects/xrplf-alphanet/locations/us-central1/workerPools/xrpld-pool"
    assert cmd[cmd.index("--region") + 1] == "us-central1"
    assert cmd[cmd.index("--substitutions") + 1] == "_AR=reg/xrpld,_TAG=t"
    assert "--async" in cmd and "--format=value(id)" in cmd
    assert "--machine-type" not in cmd


def test_submit_command_default_pool_uses_machine_type():
    cmd = cloudbuild.submit_command("/stage", "/cfg.yaml", {"_AR": "r"}, "p", None)
    assert cmd[cmd.index("--machine-type") + 1] == cloudbuild.DEFAULT_MACHINE
    assert "--region" not in cmd


def test_submit_command_rejects_commas_in_substitutions():
    with pytest.raises(BuildError):
        cloudbuild.submit_command("/s", "/c", {"_TAG": "a,b"}, "p", None)


@patch(f"{XBUILD}.submit", return_value=("build-1", "SUCCESS"))
def test_submit_tree_propagates_ar_into_substitutions_and_image(mock_submit, tmp_path):
    tree = tmp_path / "rippled"
    (tree / ".github/scripts/strategy-matrix").mkdir(parents=True)
    (tree / ".github/scripts/strategy-matrix/linux.json").write_text(json.dumps({"image_tag": "sha-abc1234"}))
    ar = "us-central1-docker.pkg.dev/xrplf-alphanet/xrpld"
    record = xbuild.submit_tree(tree, "xrplf-alphanet", ar, "alphanet-01234567", pool="xrpld-pool",
                                force_supported="ON")
    assert record["image"] == f"{ar}/xrpld:alphanet-01234567"
    assert record["build_id"] == "build-1"
    stage, config, subs, project, pool, region = mock_submit.call_args.args
    assert subs == {"_AR": ar, "_TAG": "alphanet-01234567", "_FORCE_SUPPORTED": "ON",
                    "_CI_IMAGE": "ghcr.io/xrplf/xrpld/nix-ubuntu:sha-abc1234"}
    assert config.endswith("cloudbuild.composed.yaml")
    assert (project, pool, region) == ("xrplf-alphanet", "xrpld-pool", "us-central1")


@patch(f"{XBUILD}.ci_image_for", return_value="ghcr.io/xrplf/xrpld/nix-ubuntu:sha-x")
@patch(f"{XBUILD}.submit", return_value=("build-2", "SUCCESS"))
def test_submit_branch_substitutions(mock_submit, mock_ci):
    src = BranchEntry("XRPLF", "rippled", "dangell7/x")
    dg = BranchEntry("XRPLF", "rippled", "dangell7/datagram")
    ar = "us-central1-docker.pkg.dev/xrplf-perf-network/xrpld"
    record = xbuild.submit_branch(src, SHA, "xrplf-perf-network", ar, "dangell7-x-01234567-dg", datagram=dg)
    subs = mock_submit.call_args.args[2]
    assert subs["_REPO"] == "https://github.com/XRPLF/rippled.git"
    assert subs["_BRANCH"] == SHA
    assert subs["_DATAGRAM_REPO"] == dg.url and subs["_DATAGRAM_REF"] == "dangell7/datagram"
    assert subs["_AR"] == ar
    assert subs["_CI_IMAGE"] == "ghcr.io/xrplf/xrpld/nix-ubuntu:sha-x"
    assert mock_submit.call_args.args[1].endswith("cloudbuild.yaml")
    assert record["image"] == f"{ar}/xrpld:dangell7-x-01234567-dg"


def test_ci_image_from_tree_falls_back_loudly(tmp_path, capsys):
    assert xbuild.ci_image_from_tree(tmp_path) == xbuild.DEFAULT_CI_IMAGE
    assert "WARNING" in capsys.readouterr().err


@patch(f"{XBUILD}._run")
@patch(f"{XBUILD}.shutil.which", return_value="/usr/bin/gh")
def test_ci_image_for_reads_gh_api(mock_which, mock_run):
    import base64
    content = base64.b64encode(json.dumps({"image_tag": "sha-2e25435"}).encode()).decode()
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = content + "\n"
    assert xbuild.ci_image_for("XRPLF/rippled", SHA) == "ghcr.io/xrplf/xrpld/nix-ubuntu:sha-2e25435"
    assert mock_run.call_args.args[0][:3] == ["gh", "api", f"repos/XRPLF/rippled/contents/{xbuild.STRATEGY_MATRIX}?ref={SHA}"]


def _write_manifest(workdir, **overrides):
    manifest = Manifest(base="XRPLF/rippled@develop", base_sha="b" * 40,
                        branches=[BranchOutcome("XRPLF/rippled", "dangell7/datagram", "d" * 40, "merged")],
                        composed_sha=SHA, target="Transia-RnD/rippled@alphanet",
                        options={"force_supported": "ON", "datagram": "XRPLF/rippled@dangell7/datagram"})
    for k, v in overrides.items():
        setattr(manifest, k, v)
    manifest.write(workdir)
    return manifest


def _workdir(tmp_path, **overrides):
    workdir = tmp_path / "w"
    (workdir / "rippled").mkdir(parents=True)
    _write_manifest(workdir, **overrides)
    return workdir


@patch(f"{XBUILD}.submit_tree")
def test_cli_build_tree_honors_ar_and_prints_image_last(mock_submit, tmp_path, capsys):
    workdir = _workdir(tmp_path)
    ar = "us-central1-docker.pkg.dev/xrplf-alphanet/xrpld"
    mock_submit.return_value = {"image": f"{ar}/xrpld:alphanet-01234567-dg", "build_id": "b", "status": "SUCCESS",
                                "ci_image": "img"}
    rc = cli.main(["build", "--tree", str(workdir / "rippled"), "--project", "xrplf-alphanet", "--ar", ar,
                   "--pool", "xrpld-pool", "--workdir", str(workdir)])
    assert rc == 0
    args, kwargs = mock_submit.call_args
    assert args[1:] == ("xrplf-alphanet", ar, "alphanet-01234567-dg")
    assert kwargs == {"pool": "xrpld-pool", "force_supported": "ON", "ci_image": None}
    assert capsys.readouterr().out.strip().splitlines()[-1] == f"{ar}/xrpld:alphanet-01234567-dg"
    record = json.loads((workdir / "build.json").read_text())
    assert record["ar"] == ar
    assert record["build_version"] == "b" * 40
    assert record["build_server"] == "https://github.com/XRPLF/rippled/tree/develop"
    assert record["tag"] == "alphanet-01234567-dg"
    assert record["kind"] == "xrpld"
    assert record["options"] == {"force_supported": "ON", "datagram": "XRPLF/rippled@dangell7/datagram"}


@patch(f"{XBUILD}.submit_tree")
def test_cli_build_defaults_tree_from_manifest(mock_submit, tmp_path):
    workdir = _workdir(tmp_path)
    mock_submit.return_value = {"image": "x", "build_id": "b", "status": "SUCCESS", "ci_image": "i"}
    assert cli.main(["build", "--project", "p", "--ar", "reg", "--workdir", str(workdir)]) == 0
    assert mock_submit.call_args.args[0] == workdir / "rippled"


@patch(f"{XBUILD}.submit_tree")
def test_cli_build_explicit_tag_and_set_override(mock_submit, tmp_path):
    workdir = _workdir(tmp_path, target=None, options={"force_supported": "ON"})
    mock_submit.return_value = {"image": "x", "build_id": "b", "status": "SUCCESS", "ci_image": "i"}
    cli.main(["build", "--project", "p", "--ar", "reg", "--tag", "custom",
              "--set", "force_supported=OFF", "--workdir", str(workdir)])
    args, kwargs = mock_submit.call_args
    assert args[3] == "custom" and kwargs["force_supported"] == "OFF"
    assert json.loads((workdir / "build.json").read_text())["options"] == {"force_supported": "OFF"}


@patch(f"{XBUILD}.submit_tree")
def test_cli_build_fails_when_status_not_success(mock_submit, tmp_path):
    workdir = _workdir(tmp_path)
    mock_submit.return_value = {"image": "x", "build_id": "b", "status": "FAILURE", "ci_image": "i"}
    with pytest.raises(SystemExit, match="FAILURE"):
        cli.main(["build", "--project", "p", "--ar", "reg", "--workdir", str(workdir)])
    assert json.loads((workdir / "build.json").read_text())["status"] == "FAILURE"


def test_cli_build_refuses_manifest_with_conflict(tmp_path):
    workdir = _workdir(tmp_path, branches=[BranchOutcome("XRPLF/rippled", "x", "d" * 40, "conflict")])
    with pytest.raises(SystemExit, match="unmerged"):
        cli.main(["build", "--project", "p", "--ar", "reg", "--workdir", str(workdir)])


def test_cli_build_refuses_manifest_without_composed_sha(tmp_path):
    workdir = _workdir(tmp_path, composed_sha="")
    with pytest.raises(SystemExit, match="prepare failed"):
        cli.main(["build", "--project", "p", "--ar", "reg", "--workdir", str(workdir)])


def test_cli_build_xrpld_needs_project_and_ar(tmp_path):
    workdir = _workdir(tmp_path)
    with pytest.raises(SystemExit, match="--project and --ar"):
        cli.main(["build", "--workdir", str(workdir)])


@patch(f"{XBUILD}.submit_branch")
@patch("multibranch_builder.cli.resolve_sha", return_value=SHA)
def test_cli_build_src_tag_and_ar(mock_sha, mock_submit, tmp_path, capsys):
    ar = "us-central1-docker.pkg.dev/xrplf-perf-network/xrpld"
    mock_submit.return_value = {"image": f"{ar}/xrpld:t", "build_id": "b", "status": "SUCCESS", "ci_image": "i"}
    cli.main(["build", "--src", "https://github.com/XRPLF/rippled/tree/dangell7/x",
              "--set", "datagram=https://github.com/XRPLF/rippled/tree/dangell7/datagram",
              "--project", "xrplf-perf-network", "--ar", ar, "--workdir", str(tmp_path)])
    args, kwargs = mock_submit.call_args
    assert args == (BranchEntry("XRPLF", "rippled", "dangell7/x"), SHA, "xrplf-perf-network", ar, "dangell7-x-01234567-dg")
    assert kwargs["datagram"] == BranchEntry("XRPLF", "rippled", "dangell7/datagram")
    assert kwargs["pool"] is None and kwargs["force_supported"] == "OFF"
    record = json.loads((tmp_path / "build.json").read_text())
    assert record["build_version"] == SHA
    assert record["build_server"] == "https://github.com/XRPLF/rippled/tree/dangell7/x"
    assert record["options"] == {"force_supported": "OFF", "datagram": "XRPLF/rippled@dangell7/datagram"}
