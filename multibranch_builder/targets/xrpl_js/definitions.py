"""definitions.json for a composed xrpl.js tree, taken from a live node's server_definitions."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import requests

from ...errors import ComposeError

DEFINITIONS_PATH = "packages/ripple-binary-codec/src/enums/definitions.json"
LOCK_PATH = "package-lock.json"
LOADER_KEYS = ("FIELDS", "LEDGER_ENTRY_TYPES", "TRANSACTION_RESULTS", "TRANSACTION_TYPES", "TYPES")
TIMEOUT_S = 30


def rpc(url: str, method: str, timeout: int = TIMEOUT_S) -> dict:
    """The `result` object of one JSON-RPC call, or ComposeError."""
    try:
        response = requests.post(url, json={"method": method, "params": [{}]}, timeout=timeout)
    except requests.RequestException as e:
        raise ComposeError(f"{method} to {url} failed: {e}") from None
    if response.status_code != 200:
        raise ComposeError(f"{method} to {url} returned HTTP {response.status_code}")
    try:
        result = response.json()["result"]
    except (ValueError, KeyError, TypeError):
        raise ComposeError(f"{method} to {url} returned no result object") from None
    if not isinstance(result, dict) or result.get("status") != "success":
        raise ComposeError(f"{method} to {url} returned status "
                           f"{result.get('status') if isinstance(result, dict) else result!r}")
    return result


def fetch_definitions(url: str, timeout: int = TIMEOUT_S) -> dict:
    """The node's definitions, as the binary codec loads them: the RPC envelope fields removed."""
    result = rpc(url, "server_definitions", timeout)
    definitions = {k: v for k, v in result.items() if k not in ("status", "warnings", "warning")}
    missing = [k for k in LOADER_KEYS if k not in definitions]
    if missing:
        raise ComposeError(f"server_definitions from {url} is missing {', '.join(missing)}")
    return definitions


def fetch_build_version(url: str, timeout: int = TIMEOUT_S) -> str:
    """The node's `build_version`, so the manifest names the daemon these definitions came from."""
    info = rpc(url, "server_info", timeout).get("info", {})
    version = info.get("build_version", "")
    if not version:
        raise ComposeError(f"server_info from {url} names no build_version")
    return version


def refresh_lock(tree: Path) -> str:
    """Regenerate package-lock.json from the merged package.json files; a reason when it is skipped."""
    if shutil.which("npm") is None:
        return "npm not found"
    result = subprocess.run(["npm", "install", "--package-lock-only", "--ignore-scripts"],
                            cwd=tree, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return f"npm install --package-lock-only failed: {result.stderr.strip()[:400]}"
    return ""


def prepare(tree: Path, settings: dict[str, str], options: dict[str, str]) -> tuple[dict, list[str]]:
    """Write the node's definitions over the merged tree's and refresh the lock.

    Both files are kept from the base by the `ours` merge driver, so this is where they get
    their composed content. compose commits whichever of them git sees change.
    """
    url = settings["definitions"]
    path = tree / DEFINITIONS_PATH
    if not path.is_file():
        raise ComposeError(f"{DEFINITIONS_PATH} is not in the composed tree")
    definitions = fetch_definitions(url)
    build_version = fetch_build_version(url)
    path.write_text(json.dumps(definitions, indent=2) + "\n", encoding="utf-8")
    record = {"definitions_url": url, "definitions_hash": definitions.get("hash", ""),
              "build_version": build_version}
    skipped = refresh_lock(tree)
    if skipped:
        record["lock"] = f"not refreshed: {skipped}"
    return record, [DEFINITIONS_PATH, LOCK_PATH]
