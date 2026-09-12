"""Cloud Build submission for a composed tree or a single branch."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .conf import BranchEntry

REGION = "us-central1"
DEFAULT_MACHINE = "E2_HIGHCPU_32"
CI_IMAGE_REPO = "ghcr.io/xrplf/xrpld/nix-ubuntu"
DEFAULT_CI_IMAGE = f"{CI_IMAGE_REPO}:sha-2e25435"
STRATEGY_MATRIX = ".github/scripts/strategy-matrix/linux.json"
BUILD_FILE = "build.json"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_CLOUDBUILD = Path(__file__).parent / "targets" / "xrpld" / "cloudbuild"


class BuildError(RuntimeError):
    """A Cloud Build submission could not be made or did not finish SUCCESS."""


def _run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)


def _warn(msg: str) -> None:
    print(f"[multibranch-builder] WARNING: {msg}", file=sys.stderr, flush=True)


def _image_from_matrix(text: str) -> str | None:
    try:
        tag = json.loads(text).get("image_tag", "")
    except (ValueError, AttributeError):
        return None
    return f"{CI_IMAGE_REPO}:{tag}" if tag else None


def ci_image_for(slug: str, ref: str) -> str:
    """CI image named by `owner/repo`'s strategy-matrix at `ref`, via `gh api`; loud default on failure."""
    if shutil.which("gh") is None:
        _warn(f"gh not found — using default CI image {DEFAULT_CI_IMAGE} (may be stale)")
        return DEFAULT_CI_IMAGE
    result = _run(
        ["gh", "api", f"repos/{slug}/contents/{STRATEGY_MATRIX}?ref={ref}", "--jq", ".content"],
        check=False,
    )
    image = None
    if result.returncode == 0 and result.stdout.strip():
        try:
            image = _image_from_matrix(base64.b64decode(result.stdout).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            image = None
    if image is None:
        _warn(f"no image_tag resolved from {slug}@{ref[:12]} {STRATEGY_MATRIX} — "
              f"using default CI image {DEFAULT_CI_IMAGE} (may be stale)")
        return DEFAULT_CI_IMAGE
    return image


def ci_image_from_tree(tree: str | Path) -> str:
    """CI image named by the strategy-matrix inside a local tree; loud default on failure."""
    path = Path(tree) / STRATEGY_MATRIX
    image = _image_from_matrix(path.read_text(encoding="utf-8")) if path.is_file() else None
    if image is None:
        _warn(f"no image_tag in {path} — using default CI image {DEFAULT_CI_IMAGE} (may be stale)")
        return DEFAULT_CI_IMAGE
    return image


def resolve_sha(url: str, ref: str) -> str:
    """The 40-char sha `ref` names in the remote at `url`; a sha passes through."""
    if _HEX40.match(ref):
        return ref
    result = _run(["git", "ls-remote", url, ref], check=False)
    first = result.stdout.splitlines()[0].split() if result.stdout.strip() else []
    if result.returncode != 0 or not first:
        raise BuildError(f"could not resolve {ref!r} in {url}: {result.stderr.strip()}")
    return first[0]


def safe_name(ref: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in ref)


def image_tag(ref: str, sha: str, datagram: bool) -> str:
    """`<safe-ref>-<sha8>[-dg]`, or `<sha8>[-dg]` when the ref is itself a sha."""
    tag = sha[:8] if _HEX40.match(ref) else f"{safe_name(ref)}-{sha[:8]}"
    return tag + ("-dg" if datagram else "")


def _machine_flags(project: str, pool: str | None, region: str) -> list[str]:
    if pool:
        return ["--worker-pool", f"projects/{project}/locations/{region}/workerPools/{pool}",
                "--region", region]
    return ["--machine-type", DEFAULT_MACHINE]


def _region_flags(pool: str | None, region: str) -> list[str]:
    return ["--region", region] if pool else []


def submit_command(stage: str, config: str, substitutions: dict[str, str], project: str,
                   pool: str | None, region: str = REGION) -> list[str]:
    """The `gcloud builds submit` argv for one build."""
    for key, value in substitutions.items():
        if "," in value or "=" in value:
            raise BuildError(f"substitution {key} may not contain ',' or '=': {value!r}")
    subs = ",".join(f"{k}={v}" for k, v in substitutions.items())
    return ["gcloud", "builds", "submit", stage, "--config", config, "--substitutions", subs,
            "--async", "--format=value(id)", *_machine_flags(project, pool, region),
            "--project", project]


def _submit(stage: str, config: str, substitutions: dict[str, str], project: str,
            pool: str | None, region: str) -> tuple[str, str]:
    """Submit, stream the log, return (build_id, final status)."""
    if shutil.which("gcloud") is None:
        raise BuildError("gcloud not found; install the Cloud SDK")
    cmd = submit_command(stage, config, substitutions, project, pool, region)
    result = _run(cmd, check=False)
    build_id = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    if result.returncode != 0 or not build_id:
        raise BuildError(f"gcloud builds submit failed: {result.stderr.strip()[:2000]}")
    scope = ["--project", project, *_region_flags(pool, region)]
    subprocess.run(["gcloud", "builds", "log", "--stream", build_id, *scope], check=False)
    status = _run(["gcloud", "builds", "describe", build_id, *scope, "--format=value(status)"],
                  check=False).stdout.strip() or "UNKNOWN"
    return build_id, status


def submit_tree(tree: str | Path, project: str, ar: str, tag: str, *, pool: str | None = None,
                force_supported: str = "OFF", ci_image: str | None = None,
                region: str = REGION) -> dict:
    """Upload `tree` (without .git) and compile it with composed.dockerfile."""
    image = f"{ar}/xrpld:{tag}"
    ci_image = ci_image or ci_image_from_tree(tree)
    stage = tempfile.mkdtemp(prefix="multibranch-builderd-")
    try:
        shutil.copytree(tree, os.path.join(stage, "rippled"), ignore=shutil.ignore_patterns(".git"))
        shutil.copy2(_CLOUDBUILD / "composed.dockerfile", stage)
        print(f"[multibranch-builder] submitting composed tree -> {image} (ci image {ci_image})",
              flush=True)
        build_id, status = _submit(
            stage, str(_CLOUDBUILD / "cloudbuild.composed.yaml"),
            {"_AR": ar, "_TAG": tag, "_FORCE_SUPPORTED": force_supported, "_CI_IMAGE": ci_image},
            project, pool, region)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"image": image, "build_id": build_id, "status": status, "ci_image": ci_image}


def submit_branch(source: BranchEntry, sha: str, project: str, ar: str, tag: str, *,
                  datagram: BranchEntry | None = None, pool: str | None = None,
                  force_supported: str = "OFF", ci_image: str | None = None,
                  region: str = REGION) -> dict:
    """Compile `source` at `sha` (plus the datagram ref, merged inside the build) with xrpld.dockerfile."""
    image = f"{ar}/xrpld:{tag}"
    ci_image = ci_image or ci_image_for(source.slug, sha)
    stage = tempfile.mkdtemp(prefix="xrpld-branch-")
    try:
        shutil.copy2(_CLOUDBUILD / "xrpld.dockerfile", stage)
        print(f"[multibranch-builder] submitting {source.label} @ {sha[:12]} -> {image} "
              f"(ci image {ci_image})", flush=True)
        build_id, status = _submit(
            stage, str(_CLOUDBUILD / "cloudbuild.yaml"),
            {"_REPO": source.url, "_BRANCH": sha,
             "_DATAGRAM_REPO": datagram.url if datagram else "",
             "_DATAGRAM_REF": datagram.branch if datagram else "",
             "_AR": ar, "_TAG": tag, "_FORCE_SUPPORTED": force_supported, "_CI_IMAGE": ci_image},
            project, pool, region)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"image": image, "build_id": build_id, "status": status, "ci_image": ci_image}


def write_build(workdir: str | Path, record: dict) -> Path:
    path = Path(workdir) / BUILD_FILE
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def load_build(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
