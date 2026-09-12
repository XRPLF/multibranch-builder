"""Cloud Build submission of a rippled tree or branch with the xrpld dockerfiles."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ...build import BuildError
from ...cloudbuild import REGION, submit
from ...conf import BranchEntry
from ..base import BuildRequest

CI_IMAGE_REPO = "ghcr.io/xrplf/xrpld/nix-ubuntu"
DEFAULT_CI_IMAGE = f"{CI_IMAGE_REPO}:sha-2e25435"
STRATEGY_MATRIX = ".github/scripts/strategy-matrix/linux.json"
_CLOUDBUILD = Path(__file__).with_name("cloudbuild")


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


def submit_tree(tree: str | Path, project: str, ar: str, tag: str, *, pool: str | None = None,
                force_supported: str = "OFF", ci_image: str | None = None,
                region: str = REGION) -> dict:
    """Upload `tree` (without .git) as `rippled/` and compile it with composed.dockerfile."""
    image = f"{ar}/xrpld:{tag}"
    ci_image = ci_image or ci_image_from_tree(tree)
    stage = tempfile.mkdtemp(prefix="multibranch-builderd-")
    try:
        shutil.copytree(tree, os.path.join(stage, "rippled"), ignore=shutil.ignore_patterns(".git"))
        shutil.copy2(_CLOUDBUILD / "composed.dockerfile", stage)
        print(f"[multibranch-builder] submitting composed tree -> {image} (ci image {ci_image})",
              flush=True)
        build_id, status = submit(
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
        build_id, status = submit(
            stage, str(_CLOUDBUILD / "cloudbuild.yaml"),
            {"_REPO": source.url, "_BRANCH": sha,
             "_DATAGRAM_REPO": datagram.url if datagram else "",
             "_DATAGRAM_REF": datagram.branch if datagram else "",
             "_AR": ar, "_TAG": tag, "_FORCE_SUPPORTED": force_supported, "_CI_IMAGE": ci_image},
            project, pool, region)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"image": image, "build_id": build_id, "status": status, "ci_image": ci_image}


def build(req: BuildRequest, *, datagram: BranchEntry | None = None) -> dict:
    """Submit the request's composed tree, or its source branch at its sha, to Cloud Build."""
    if not req.project or not req.ar:
        raise BuildError("xrpld builds need --project and --ar")
    force_supported = req.options.get("force_supported", "OFF")
    if req.tree is not None:
        return submit_tree(req.tree, req.project, req.ar, req.tag, pool=req.pool,
                           force_supported=force_supported, ci_image=req.ci_image)
    if req.source is None:
        raise BuildError("build needs a composed tree or a source branch")
    return submit_branch(req.source, req.sha, req.project, req.ar, req.tag, datagram=datagram,
                         pool=req.pool, force_supported=force_supported, ci_image=req.ci_image)
