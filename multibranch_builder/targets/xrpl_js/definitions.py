"""definitions.json for a composed xrpl.js tree, taken from a live node's server_definitions."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import requests

from ...errors import BuildError, ComposeError

DEFINITIONS_PATH = "packages/ripple-binary-codec/src/enums/definitions.json"
LOCK_PATH = "package-lock.json"
LOADER_KEYS = ("FIELDS", "LEDGER_ENTRY_TYPES", "TRANSACTION_RESULTS", "TRANSACTION_TYPES", "TYPES")
TIMEOUT_S = 30
NPM_LOG = "npm-build.log"
NPM_COMMANDS = (["npm", "ci"], ["npm", "run", "build"], ["npm", "test"])


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


def _log(msg: str) -> None:
    print(f"[multibranch-builder] {msg}", file=sys.stderr, flush=True)


def check_definitions(tree: Path) -> tuple[str, str]:
    """(what is wrong with the tree's definitions.json, its hash); the first is empty when it loads."""
    path = tree / DEFINITIONS_PATH
    try:
        definitions = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"{DEFINITIONS_PATH}: {e}", ""
    missing = [k for k in (*LOADER_KEYS, "hash") if k not in definitions]
    if missing:
        return f"{DEFINITIONS_PATH} is missing {', '.join(missing)}", ""
    return "", definitions["hash"]


def _version(tool: str) -> str:
    result = subprocess.run([tool, "--version"], text=True, capture_output=True, check=False)
    return result.stdout.strip()


def npm_build(tree: Path, log_path: Path) -> dict:
    """Install, build and unit-test the monorepo, with every command's output in `log_path`."""
    if shutil.which("npm") is None:
        raise BuildError("npm not found; install node (the tree's .nvmrc names the version) and npm")
    record = {"step": "npm", "node": _version("node"), "npm": _version("npm"),
              "commands": [" ".join(c) for c in NPM_COMMANDS], "log": str(log_path), "failed": None}
    with open(log_path, "w", encoding="utf-8") as log:
        for command in NPM_COMMANDS:
            text = " ".join(command)
            _log(f"{text} in {tree}")
            log.write(f"$ {text}\n")
            log.flush()
            result = subprocess.run(command, cwd=tree, stdout=log, stderr=subprocess.STDOUT, check=False)
            if result.returncode != 0:
                _log(f"{text} exited {result.returncode}; see {log_path}")
                return {**record, "status": "FAILURE", "failed": text,
                        "summary": f"{text} exited {result.returncode}; see {log_path}"}
    return {**record, "status": "SUCCESS", "summary": f"{len(NPM_COMMANDS)} npm commands passed"}
