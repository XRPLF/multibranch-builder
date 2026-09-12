"""`gcloud builds submit`: the argv, the async submission, the streamed log and the final status."""

from __future__ import annotations

import shutil
import subprocess

from .build import BuildError

REGION = "us-central1"
DEFAULT_MACHINE = "E2_HIGHCPU_32"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


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


def submit(stage: str, config: str, substitutions: dict[str, str], project: str,
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
