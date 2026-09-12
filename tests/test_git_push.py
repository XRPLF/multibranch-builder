"""Tests for git_push — signed commit and PAT push as the service identity."""

import base64
from unittest.mock import MagicMock, patch

import pytest

from multibranch_builder import git_push
from multibranch_builder.git_push import SigningNotConfigured


def _completed(returncode=0, stdout="", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setenv("GIT_BOT_NAME", "dangell8")
    monkeypatch.setenv("GIT_BOT_EMAIL", "dangell8@users.noreply.github.com")
    monkeypatch.setenv("GITHUB_BOT_PAT", "secret-pat")


@pytest.fixture
def no_identity(monkeypatch):
    for var in ("GIT_BOT_NAME", "GIT_BOT_EMAIL", "GITHUB_BOT_PAT", "GIT_SIGNING_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestSetupSigning:
    """setup_signing imports the key and configures git as the service identity."""

    @patch("multibranch_builder.git_push._run")
    def test_configures_git_as_service_identity(self, mock_run, identity, monkeypatch):
        monkeypatch.setenv("GIT_SIGNING_KEY", base64.b64encode(b"-----BEGIN PGP-----").decode())
        mock_run.side_effect = [
            _completed(),  # gpg --import
            _completed(stdout="sec:...\nfpr:::::::::ABCDEF0123456789FPR:\n"),  # list-secret-keys
            _completed(), _completed(), _completed(), _completed(), _completed(),  # git config x5
        ]
        key_id = git_push.setup_signing("/workspace/repo")

        assert key_id == "ABCDEF0123456789FPR"
        config_calls = [c.args[0] for c in mock_run.call_args_list if c.args[0][:2] == ["git", "config"]]
        assert ["git", "config", "user.name", "dangell8"] in config_calls
        assert ["git", "config", "user.email", "dangell8@users.noreply.github.com"] in config_calls
        assert ["git", "config", "commit.gpgsign", "true"] in config_calls

    @patch("multibranch_builder.git_push._run")
    def test_accepts_armored_key_directly(self, mock_run, identity, monkeypatch):
        monkeypatch.setenv("GIT_SIGNING_KEY", "-----BEGIN PGP PRIVATE KEY BLOCK-----\nabc\n")
        mock_run.side_effect = [
            _completed(),
            _completed(stdout="fpr:::::::::FPR1:\n"),
            _completed(), _completed(), _completed(), _completed(), _completed(),
        ]
        assert git_push.setup_signing("/w") == "FPR1"

    def test_raises_when_gpg_key_missing(self, identity, monkeypatch):
        monkeypatch.delenv("GIT_SIGNING_KEY", raising=False)
        with pytest.raises(SigningNotConfigured, match="GIT_SIGNING_KEY"):
            git_push.setup_signing("/w")

    @patch("multibranch_builder.git_push._run")
    def test_raises_when_no_secret_key_found(self, mock_run, identity, monkeypatch):
        monkeypatch.setenv("GIT_SIGNING_KEY", base64.b64encode(b"key").decode())
        mock_run.side_effect = [
            _completed(),  # gpg --import
            _completed(stdout="no keys here\n"),  # list-secret-keys, no fpr line
        ]
        with pytest.raises(SigningNotConfigured, match="No GPG secret key"):
            git_push.setup_signing("/w")

    def test_raises_when_service_identity_missing(self, no_identity):
        with pytest.raises(SigningNotConfigured, match="GITHUB_BOT_PAT"):
            git_push.setup_signing("/w")


class TestCommitAll:
    """commit_all stages everything and makes one signed commit."""

    @patch("multibranch_builder.git_push._run")
    def test_stages_and_commits_returns_head_sha(self, mock_run):
        mock_run.side_effect = [
            _completed(),  # git add -A
            _completed(returncode=0),  # git commit -m
            _completed(stdout="newsha123\n"),  # git rev-parse HEAD
        ]
        sha = git_push.commit_all("/w", "fix: something")
        assert sha == "newsha123"
        assert mock_run.call_args_list[0].args[0] == ["git", "add", "-A"]
        assert mock_run.call_args_list[1].args[0] == ["git", "commit", "-m", "fix: something"]

    @patch("multibranch_builder.git_push._run")
    def test_tracked_only_staging(self, mock_run):
        mock_run.side_effect = [_completed(), _completed(returncode=0), _completed(stdout="sha\n")]
        git_push.commit_all("/w", "fix: x", include_untracked=False)
        assert mock_run.call_args_list[0].args[0] == ["git", "add", "-u"]

    @patch("multibranch_builder.git_push._run")
    def test_allow_empty(self, mock_run):
        mock_run.side_effect = [_completed(), _completed(returncode=0), _completed(stdout="sha\n")]
        git_push.commit_all("/w", "m", allow_empty=True)
        assert mock_run.call_args_list[1].args[0] == ["git", "commit", "--allow-empty", "-m", "m"]

    @patch("multibranch_builder.git_push._run")
    def test_raises_when_nothing_to_commit(self, mock_run):
        mock_run.side_effect = [
            _completed(),  # git add -A
            _completed(returncode=1, stdout="nothing to commit, working tree clean"),
        ]
        with pytest.raises(RuntimeError, match="git commit failed"):
            git_push.commit_all("/w", "fix: nothing")


class TestPush:
    """push sends a signed HEAD to the branch over a PAT-authenticated URL."""

    @patch("multibranch_builder.git_push._run")
    def test_pushes_head_to_branch_with_pat_and_lease(self, mock_run, identity):
        mock_run.side_effect = [
            _completed(),  # git verify-commit HEAD
            _completed(stdout="abc123\trefs/heads/fix/ci\n"),  # git ls-remote
            _completed(),  # git push
        ]
        git_push.push("/w", "XRPLF", "rippled", "fix/ci", force=True)

        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ["git", "push", "--force-with-lease=refs/heads/fix/ci:abc123"]
        assert cmd[-1] == "HEAD:refs/heads/fix/ci"
        assert "https://x-access-token:secret-pat@github.com/XRPLF/rippled.git" in cmd

    @patch("multibranch_builder.git_push._run")
    def test_lease_on_missing_branch_requires_absence(self, mock_run, identity):
        mock_run.side_effect = [_completed(), _completed(stdout=""), _completed()]
        git_push.push("/w", "o", "r", "new", force=True)
        assert "--force-with-lease=refs/heads/new:" in mock_run.call_args.args[0]

    @patch("multibranch_builder.git_push._run")
    def test_plain_push_has_no_force(self, mock_run, identity):
        mock_run.side_effect = [_completed(), _completed()]
        git_push.push("/w", "o", "r", "b")
        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["git", "push"] and not any(a.startswith("--force") for a in cmd)

    @patch("multibranch_builder.git_push._run")
    def test_refuses_unsigned_head(self, mock_run, identity):
        mock_run.return_value = _completed(returncode=1)
        with pytest.raises(SigningNotConfigured, match="not GPG-signed"):
            git_push.push("/w", "o", "r", "b")
        assert all(c.args[0][:2] != ["git", "push"] for c in mock_run.call_args_list)

    def test_refuses_without_pat(self, no_identity):
        with pytest.raises(SigningNotConfigured, match="GITHUB_BOT_PAT"):
            git_push.push("/w", "o", "r", "b")

    @patch("multibranch_builder.git_push._run")
    def test_scrubs_pat_from_error(self, mock_run, identity):
        mock_run.side_effect = [
            _completed(),
            _completed(stdout=""),
            _completed(returncode=1,
                       stderr="fatal: unable to access https://x-access-token:secret-pat@github.com/o/r.git"),
        ]
        with pytest.raises(RuntimeError) as exc:
            git_push.push("/w", "o", "r", "b", force=True)
        assert "secret-pat" not in str(exc.value)
        assert "***" in str(exc.value)


class TestCommitAndPush:
    """commit_and_push chains commit_all + push."""

    @patch("multibranch_builder.git_push.push")
    @patch("multibranch_builder.git_push.commit_all")
    def test_commits_then_pushes(self, mock_commit, mock_push):
        mock_commit.return_value = "sha999"
        sha = git_push.commit_and_push("/w", "o", "r", "b", "msg", force=True)
        assert sha == "sha999"
        mock_commit.assert_called_once_with("/w", "msg", include_untracked=True)
        mock_push.assert_called_once_with("/w", "o", "r", "b", force=True)

    @patch("multibranch_builder.git_push.push")
    @patch("multibranch_builder.git_push.commit_all")
    def test_threads_include_untracked(self, mock_commit, mock_push):
        mock_commit.return_value = "sha999"
        git_push.commit_and_push("/w", "o", "r", "b", "msg", include_untracked=False)
        mock_commit.assert_called_once_with("/w", "msg", include_untracked=False)


class TestUpdateRef:
    """update_ref points a branch at a sha through the REST API with the service PAT."""

    @patch("multibranch_builder.git_push.requests.patch")
    @patch("multibranch_builder.git_push.requests.post")
    def test_creates_ref(self, mock_post, mock_patch, identity):
        mock_post.return_value = MagicMock(status_code=201, json=lambda: {"ref": "refs/heads/develop"})
        out = git_push.update_ref("Transia-RnD", "rippled", "develop", "sha123", force=True)
        assert out == {"ref": "refs/heads/develop"}
        args, kwargs = mock_post.call_args
        assert args[0] == "https://api.github.com/repos/Transia-RnD/rippled/git/refs"
        assert kwargs["headers"]["Authorization"] == "Bearer secret-pat"
        assert kwargs["json"] == {"ref": "refs/heads/develop", "sha": "sha123"}
        mock_patch.assert_not_called()

    @patch("multibranch_builder.git_push.requests.patch")
    @patch("multibranch_builder.git_push.requests.post")
    def test_updates_existing_ref_with_force(self, mock_post, mock_patch, identity):
        mock_post.return_value = MagicMock(status_code=422)
        mock_patch.return_value = MagicMock(status_code=200, json=lambda: {"object": {"sha": "sha123"}})
        git_push.update_ref("o", "r", "develop", "sha123", force=True)
        args, kwargs = mock_patch.call_args
        assert args[0] == "https://api.github.com/repos/o/r/git/refs/heads/develop"
        assert kwargs["json"] == {"sha": "sha123", "force": True}

    def test_raises_when_no_pat_and_required(self, no_identity):
        with pytest.raises(SigningNotConfigured, match="GITHUB_BOT_PAT"):
            git_push.update_ref("o", "r", "main", "sha")

    @patch("multibranch_builder.git_push.requests.post")
    def test_noop_when_no_pat_and_not_required(self, mock_post, no_identity):
        assert git_push.update_ref("o", "r", "main", "sha", required=False) is None
        mock_post.assert_not_called()
