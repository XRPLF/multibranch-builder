"""The compile RUN of each xrpld dockerfile, run under sh and dash with its external commands stubbed."""

import shutil
import subprocess
from pathlib import Path

import pytest

CLOUDBUILD_DIR = Path(__file__).resolve().parent.parent / "multibranch_builder/targets/xrpld/cloudbuild"
DOCKERFILES = ["xrpld.dockerfile", "composed.dockerfile"]
SHELLS = [s for s in ("sh", "dash") if shutil.which(s)]
STUBBED = ["cmake", "ccache", "nproc", "sed", "awk", "strip", "mkdir", "cp"]
RECORDED_ENV = ["FORCE_XRPLD_VERSION", "GITHUB_HEAD_SHA", "GITHUB_BRANCH_NAME", "CCACHE_DISABLE"]

# Logs `name|argv|VAR=value;...` per call (`<unset>` for an unset variable), prints what nproc and the
# meminfo awk would, and exits with the code in `$STUB_DIR/<name>.rc` when that file exists.
STUB = """#!/bin/sh
name=${0##*/}
printf '%s|%s|' "$name" "$*" >> "$STUB_LOG"
""" + "".join(
    f"printf '{var}=%s;' \"${{{var}-<unset>}}\" >> \"$STUB_LOG\"\n" for var in RECORDED_ENV
) + """printf '\\n' >> "$STUB_LOG"
case "$name" in
  nproc) echo 8 ;;
  awk) echo 4 ;;
esac
if [ -f "$STUB_DIR/$name.rc" ]; then exit "$(cat "$STUB_DIR/$name.rc")"; fi
exit 0
"""


def compile_run(name):
    """The compile RUN's command in `name`, with line continuations joined as docker joins them."""
    instruction, instructions = [], []
    for line in (CLOUDBUILD_DIR / name).read_text().splitlines():
        if not instruction and not line.startswith("RUN "):
            continue
        instruction.append(line)
        if not line.endswith("\\"):
            instructions.append("\n".join(instruction))
            instruction = []
    (run,) = [i for i in instructions if "cmake --build . --target xrpld" in i]
    return run[len("RUN "):].replace("\\\n", "")


class Calls:
    def __init__(self, log):
        self.rows = []
        for line in log.read_text().splitlines() if log.exists() else []:
            name, argv, env = line.split("|", 2)
            self.rows.append((name, argv, dict(kv.split("=", 1) for kv in env.split(";") if kv)))

    def index(self, name, argv_prefix=""):
        """The position of the first call of `name` whose argv starts with `argv_prefix`, else None."""
        return next((i for i, (n, a, _) in enumerate(self.rows) if n == name and a.startswith(argv_prefix)), None)

    def env(self, name, argv_prefix=""):
        return self.rows[self.index(name, argv_prefix)][2]


def run_compile(tmp_path, shell, dockerfile, *, rc=None, build_dir=True, macro=None, **args):
    """Runs the compile RUN in a fake rippled tree; returns (exit code, Calls, output)."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for command in STUBBED:
        (stubs / command).write_text(STUB)
        (stubs / command).chmod(0o755)
    for command, code in (rc or {}).items():
        (stubs / f"{command}.rc").write_text(str(code))
    tree = tmp_path / "rippled"
    tree.mkdir()
    if build_dir:
        (tree / ".build").mkdir()
    if macro is not None:
        (tree / "include/xrpl/protocol/detail").mkdir(parents=True)
        (tree / "include/xrpl/protocol/detail/features.macro").write_text(macro)
    log = tmp_path / "calls.log"
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": str(log), "STUB_DIR": str(stubs),
           "FORCE_SUPPORTED": "OFF", "SYMBOLS": "OFF", "CCACHE": "ON",
           "GIT_SHA": "", "GIT_BRANCH": "", "XRPLD_VERSION": ""}
    env.update(args)
    proc = subprocess.run([shutil.which(shell), "-c", compile_run(dockerfile)], cwd=tree, env=env,
                          capture_output=True, text=True)
    return proc.returncode, Calls(log), proc.stdout + proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_compile_runs_to_the_end(tmp_path, shell, dockerfile):
    code, calls, out = run_compile(tmp_path, shell, dockerfile)
    assert code == 0, out
    assert calls.index("cmake", "--build . --target xrpld") is not None
    assert calls.index("cp", "xrpld /out/") is not None


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_unsupported_amendment_left_stops_the_build(tmp_path, shell, dockerfile):
    code, calls, out = run_compile(tmp_path, shell, dockerfile, FORCE_SUPPORTED="ON",
                                   macro="XRPL_FEATURE(Batch, Supported::No, VoteBehavior::DefaultNo)\n")
    assert code != 0, out
    assert calls.index("cmake") is None


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_failed_features_macro_edit_stops_the_build(tmp_path, shell, dockerfile):
    code, calls, out = run_compile(tmp_path, shell, dockerfile, FORCE_SUPPORTED="ON", rc={"sed": 2})
    assert code != 0, out
    assert calls.index("cmake") is None


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_missing_build_dir_stops_the_build(tmp_path, shell, dockerfile):
    code, calls, out = run_compile(tmp_path, shell, dockerfile, build_dir=False)
    assert code != 0, out
    assert calls.index("cmake") is None


@pytest.mark.parametrize("shell", SHELLS)
def test_cmake_configure_sees_the_composed_version(tmp_path, shell):
    code, calls, out = run_compile(tmp_path, shell, "composed.dockerfile",
                                   GIT_SHA="c" * 40, GIT_BRANCH="dangell7/x", XRPLD_VERSION="3.4.2-rc1+perf")
    assert code == 0, out
    env = calls.env("cmake", "-D")
    assert env["GITHUB_HEAD_SHA"] == "c" * 40
    assert env["GITHUB_BRANCH_NAME"] == "dangell7/x"
    assert env["FORCE_XRPLD_VERSION"] == "3.4.2-rc1+perf"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_ccache_off_compiles_with_ccache_disabled(tmp_path, shell, dockerfile):
    code, calls, out = run_compile(tmp_path, shell, dockerfile, CCACHE="OFF")
    assert code == 0, out
    assert calls.env("cmake", "--build . --target xrpld")["CCACHE_DISABLE"] == "1"
    assert calls.index("ccache") is None
    assert "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache" not in calls.rows[calls.index("cmake", "-D")][1]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_ccache_on_zeroes_statistics_before_compiling(tmp_path, shell, dockerfile):
    code, calls, out = run_compile(tmp_path, shell, dockerfile, CCACHE="ON")
    assert code == 0, out
    assert calls.index("ccache", "-z") < calls.index("cmake", "--build . --target xrpld") < calls.index("ccache", "-s")
    assert calls.env("cmake", "--build . --target xrpld")["CCACHE_DISABLE"] == "<unset>"
    assert calls.rows[calls.index("cmake", "-D")][1].startswith("-DCMAKE_CXX_COMPILER_LAUNCHER=ccache ")


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_ccache_statistics_failures_do_not_fail_the_build(tmp_path, shell, dockerfile):
    code, calls, out = run_compile(tmp_path, shell, dockerfile, CCACHE="ON", rc={"ccache": 1})
    assert code == 0, out
    assert calls.index("ccache", "-z") is not None and calls.index("ccache", "-s") is not None
    assert calls.index("cp", "xrpld /out/") is not None
