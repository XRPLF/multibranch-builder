"""The xrpl.js kind: conf settings, the definitions fetch, prepare, the ours driver, verify."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from multibranch_builder import cli
from multibranch_builder.conf import ConfError, parse_config_text
from multibranch_builder.errors import ComposeError
from multibranch_builder.targets import KINDS, for_base, for_config
from multibranch_builder.targets.base import BuildRequest
from multibranch_builder.targets.xrpl_js import definitions as defs

KIND = KINDS["xrpl_js"]
DEFS = "packages/ripple-binary-codec/src/enums/definitions.json"
MODULE = "multibranch_builder.targets.xrpl_js.definitions"
URL = "https://alphanet.xrpl.org"

CONF = f"""\
base XRPLF/xrpl.js main
target XRPLF/xrpl.js xrplf/alphanet
definitions {URL}
XRPLF/xrpl.js smart-contracts
"""


def _result(**overrides):
    result = {"status": "success", "hash": "C685734F5FEB0123", "FIELDS": [], "LEDGER_ENTRY_TYPES": {},
              "TRANSACTION_RESULTS": {}, "TRANSACTION_TYPES": {}, "TYPES": {}, "TRANSACTION_FLAGS": {}}
    result.update(overrides)
    return result


def _response(payload, status_code=200):
    response = MagicMock(status_code=status_code)
    response.json.return_value = payload
    return response


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def test_for_base_and_for_config_map_xrpl_js():
    assert for_base("XRPLF/xrpl.js") is KIND
    assert for_config(parse_config_text(CONF)) is KIND


def test_conf_carries_the_definitions_setting():
    config = parse_config_text(CONF)
    assert config.settings == {"definitions": URL}
    assert config.target.label == "XRPLF/xrpl.js@xrplf/alphanet"
    assert [b.branch for b in config.branches] == ["smart-contracts"]


def test_settings_require_an_http_definitions_url():
    KIND.validate_settings({"definitions": URL})
    with pytest.raises(ConfError, match="needs a `definitions"):
        KIND.validate_settings({})
    with pytest.raises(ConfError, match="http"):
        KIND.validate_settings({"definitions": "alphanet.xrpl.org"})
    with pytest.raises(ConfError, match="does not accept setting 'force_supported'"):
        KIND.validate_settings({"definitions": URL, "force_supported": "ON"})


def test_options_reject_any_key():
    assert KIND.validate_options({}) == {}
    with pytest.raises(ConfError, match="does not accept option 'force_supported'"):
        KIND.validate_options({"force_supported": "ON"})


def test_image_suffix_and_plan_are_plain():
    assert KIND.image_suffix({}) == ""
    config = parse_config_text(CONF)
    assert KIND.plan(config.branches, {}) == config.branches


def test_fetch_definitions_drops_the_rpc_envelope_and_keeps_hash():
    with patch(f"{MODULE}.requests.post", return_value=_response({"result": _result(warnings=["x"])})) as post:
        definitions = defs.fetch_definitions(URL)
    assert "status" not in definitions and "warnings" not in definitions
    assert definitions["hash"] == "C685734F5FEB0123"
    assert definitions["TRANSACTION_FLAGS"] == {}
    assert post.call_args.kwargs["json"] == {"method": "server_definitions", "params": [{}]}


def test_fetch_definitions_rejects_a_response_the_codec_cannot_load():
    payload = _result()
    del payload["TYPES"]
    with patch(f"{MODULE}.requests.post", return_value=_response({"result": payload})):
        with pytest.raises(ComposeError, match="missing TYPES"):
            defs.fetch_definitions(URL)


@pytest.mark.parametrize("response,match", [
    (_response({"result": _result(status="error")}), "returned status"),
    (_response({"nope": 1}), "no result object"),
    (_response({"result": _result()}, status_code=503), "HTTP 503"),
])
def test_rpc_failures_are_compose_errors(response, match):
    with patch(f"{MODULE}.requests.post", return_value=response):
        with pytest.raises(ComposeError, match=match):
            defs.fetch_definitions(URL)


def test_rpc_connection_failure_is_a_compose_error():
    with patch(f"{MODULE}.requests.post", side_effect=defs.requests.RequestException("refused")):
        with pytest.raises(ComposeError, match="refused"):
            defs.fetch_definitions(URL)


def test_fetch_build_version_needs_one():
    with patch(f"{MODULE}.requests.post",
               return_value=_response({"result": {"status": "success", "info": {"build_version": "2.6.1-b1"}}})):
        assert defs.fetch_build_version(URL) == "2.6.1-b1"
    with patch(f"{MODULE}.requests.post", return_value=_response({"result": {"status": "success", "info": {}}})):
        with pytest.raises(ComposeError, match="no build_version"):
            defs.fetch_build_version(URL)


@pytest.fixture
def tree(tmp_path):
    (tmp_path / DEFS).parent.mkdir(parents=True)
    (tmp_path / DEFS).write_text('{"FIELDS": [], "hash": "old"}\n')
    (tmp_path / "package-lock.json").write_text("{}\n")
    return tmp_path


def _post(method_results):
    def post(url, json=None, timeout=None):
        return _response({"result": method_results[json["method"]]})
    return post


def test_prepare_writes_the_nodes_definitions_and_records_the_node(tree):
    results = {"server_definitions": _result(), "server_info": {"status": "success", "info": {"build_version": "2.6.1"}}}
    with patch(f"{MODULE}.requests.post", side_effect=_post(results)), \
         patch(f"{MODULE}.shutil.which", return_value=None):
        record, paths = KIND.prepare(tree, {"definitions": URL}, {})
    assert paths == [DEFS, "package-lock.json"]
    assert record["definitions_hash"] == "C685734F5FEB0123"
    assert record["build_version"] == "2.6.1"
    assert record["definitions_url"] == URL
    assert record["lock"] == "not refreshed: npm not found"
    written = (tree / DEFS).read_text()
    assert json.loads(written) == {k: v for k, v in _result().items() if k != "status"}
    assert written == json.dumps(json.loads(written), indent=2) + "\n"


def test_prepare_refreshes_the_lock_when_npm_is_present(tree):
    results = {"server_definitions": _result(), "server_info": {"status": "success", "info": {"build_version": "2.6.1"}}}
    with patch(f"{MODULE}.requests.post", side_effect=_post(results)), \
         patch(f"{MODULE}.shutil.which", return_value="/usr/bin/npm"), \
         patch(f"{MODULE}.subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as npm:
        record, _ = KIND.prepare(tree, {"definitions": URL}, {})
    assert npm.call_args.args[0] == ["npm", "install", "--package-lock-only", "--ignore-scripts"]
    assert npm.call_args.kwargs["cwd"] == tree
    assert "lock" not in record


def test_prepare_records_a_failed_lock_refresh(tree):
    results = {"server_definitions": _result(), "server_info": {"status": "success", "info": {"build_version": "2.6.1"}}}
    with patch(f"{MODULE}.requests.post", side_effect=_post(results)), \
         patch(f"{MODULE}.shutil.which", return_value="/usr/bin/npm"), \
         patch(f"{MODULE}.subprocess.run", return_value=MagicMock(returncode=1, stderr="ERR! registry")):
        record, _ = KIND.prepare(tree, {"definitions": URL}, {})
    assert "ERR! registry" in record["lock"]


def test_prepare_needs_definitions_json_in_the_tree(tmp_path):
    with pytest.raises(ComposeError, match="not in the composed tree"):
        KIND.prepare(tmp_path, {"definitions": URL}, {})


@pytest.fixture
def repo(tmp_path):
    """A repo whose main and a feature branch both changed definitions.json."""
    repo = tmp_path / "repo"
    (repo / DEFS).parent.mkdir(parents=True)
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / DEFS).write_text("base\n")
    (repo / "other.ts").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / DEFS).write_text("theirs\n")
    (repo / "other.ts").write_text("theirs\n")
    git(repo, "commit", "-q", "-am", "feature")
    git(repo, "checkout", "-q", "main")
    return repo


def test_ours_driver_keeps_the_trees_definitions_when_both_sides_changed(repo):
    (repo / DEFS).write_text("ours\n")
    (repo / "other.ts").write_text("ours\n")
    git(repo, "commit", "-q", "-am", "main moves")
    KIND.configure_merge(str(repo))
    subprocess.run(["git", "merge", "--no-commit", "--no-ff", "feat"], cwd=repo, capture_output=True)
    assert (repo / DEFS).read_text() == "ours\n"
    assert git(repo, "diff", "--name-only", "--diff-filter=U") == "other.ts"


def test_ours_driver_does_not_run_when_only_the_branch_changed_definitions(repo):
    KIND.configure_merge(str(repo))
    subprocess.run(["git", "merge", "--no-commit", "--no-ff", "feat"], cwd=repo, capture_output=True)
    assert (repo / DEFS).read_text() == "theirs\n"


def test_configure_merge_writes_both_attributes_once(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    KIND.configure_merge(str(tmp_path))
    KIND.configure_merge(str(tmp_path))
    attributes = (tmp_path / ".git" / "info" / "attributes").read_text()
    assert attributes.count("merge=ours") == 2
    assert git(tmp_path, "config", "merge.ours.driver") == "true"
    assert git(tmp_path, "config", "rerere.enabled") == "true"


def _verify(tree):
    return KIND.build(BuildRequest(options={}, tag="t", tree=tree))


def test_verify_passes_a_clean_tree_with_loadable_definitions(repo):
    (repo / DEFS).write_text(json.dumps({k: v for k, v in _result().items() if k != "status"}, indent=2) + "\n")
    git(repo, "commit", "-q", "-am", "definitions")
    record = _verify(repo)
    assert record["status"] == "SUCCESS"
    assert record["summary"] == "definitions C685734F5FEB verified"


def test_verify_fails_a_dirty_tree(repo):
    (repo / DEFS).write_text(json.dumps({k: v for k, v in _result().items() if k != "status"}, indent=2) + "\n")
    record = _verify(repo)
    assert record["status"] == "FAILURE"
    assert "uncommitted changes" in record["summary"]


def test_verify_fails_definitions_the_codec_cannot_load(repo):
    (repo / DEFS).write_text('{"FIELDS": []}\n')
    git(repo, "commit", "-q", "-am", "half a file")
    record = _verify(repo)
    assert record["status"] == "FAILURE"
    assert "missing LEDGER_ENTRY_TYPES" in record["summary"]


def test_verify_refuses_a_single_branch_build():
    with pytest.raises(ComposeError, match="composed tree"):
        KIND.build(BuildRequest(options={}, tag="t"))


def test_cli_dry_run_prints_the_kind_and_its_setting(tmp_path, capsys):
    conf = tmp_path / "xrpljs.conf"
    conf.write_text(CONF)
    assert cli.main(["compose", "--conf", str(conf), "--workdir", str(tmp_path / "w"), "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "kind    xrpl_js",
        "base    XRPLF/xrpl.js@main",
        "merge  1 XRPLF/xrpl.js@smart-contracts",
        "target  XRPLF/xrpl.js@xrplf/alphanet",
        f"setting definitions={URL}",
        "options (none)",
    ]


def test_cli_dry_run_refuses_a_conf_without_the_definitions_setting(tmp_path):
    conf = tmp_path / "xrpljs.conf"
    conf.write_text("base XRPLF/xrpl.js main\nXRPLF/xrpl.js smart-contracts\n")
    with pytest.raises(SystemExit, match="needs a `definitions"):
        cli.main(["compose", "--conf", str(conf), "--workdir", str(tmp_path / "w"), "--dry-run"])


def test_kinds_lists_xrpl_js(capsys):
    assert cli.main(["kinds", "--kind", "xrpl_js"]) == 0
    out = capsys.readouterr().out
    assert "tree_dir        xrpl.js" in out
    assert "default_branch  main" in out
    assert "setting  definitions" in out
