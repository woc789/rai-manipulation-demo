"""CLI contract tests with fake systemd commands; never start/stop real robots."""

import json
import os
from pathlib import Path
import subprocess


LAUNCHER = Path(__file__).resolve().parents[4] / "start.sh"
FAKE = """#!/usr/bin/python3
import json, os, pathlib, shlex, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as stream:
    stream.write(json.dumps([tool, args]) + "\\n")
active = os.getenv("FAKE_ACTIVE", "").split(",")
if tool == "pgrep":
    sys.exit(1)
if tool == "systemctl" and "is-active" in args:
    sys.exit(0 if args[-1] in active else 3)
if tool == "systemctl" and "show" in args:
    if "Environment" in args:
        print(shlex.quote("PRODUCE_STATE_DB=" + os.environ["FAKE_PREVIOUS_DB"]) if os.getenv("FAKE_PREVIOUS_DB") else "")
    else:
        print("loaded" if args[1] in active else "not-found")
"""


def run_launcher(tmp_path, command, **extra):
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("systemctl", "systemd-run", "pgrep"):
        file = tools / name
        file.write_text(FAKE)
        file.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    env = dict(
        os.environ, PATH=f"{tools}:/usr/bin:/bin", FAKE_LOG=str(log), DISPLAY=":99"
    )
    env.pop("PRODUCE_STATE_DB", None)
    env.update(extra)
    result = subprocess.run(
        ["/bin/bash", str(LAUNCHER), command],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    return result, [args for name, args in calls if name == "systemd-run"]


def test_start_creates_both_units_from_another_directory(tmp_path):
    result, launched = run_launcher(tmp_path, "start")
    assert result.returncode == 0 and len(launched) == 2
    assert any("--unit=produce-order-workstation.service" in args for args in launched)
    assert any("--unit=produce-order-panel.service" in args for args in launched)


def test_start_does_not_duplicate_existing_units(tmp_path):
    result, launched = run_launcher(
        tmp_path,
        "start",
        FAKE_ACTIVE="produce-order-workstation.service,produce-order-panel.service",
    )
    assert result.returncode == 0 and not launched


def test_legacy_instance_blocks_duplicate_start(tmp_path):
    result, launched = run_launcher(
        tmp_path, "start", FAKE_ACTIVE="produce-large-order-validation.service"
    )
    assert result.returncode == 1 and not launched


def test_restart_preserves_selected_database_including_spaces(tmp_path):
    previous = "/tmp/previous state.sqlite3"
    result, launched = run_launcher(tmp_path, "restart", FAKE_PREVIOUS_DB=previous)
    assert result.returncode == 0 and len(launched) == 2
    assert all(f"--setenv=PRODUCE_STATE_DB={previous}" in args for args in launched)


def test_explicit_database_wins_on_restart(tmp_path):
    result, launched = run_launcher(
        tmp_path,
        "restart",
        FAKE_PREVIOUS_DB="/tmp/old.sqlite3",
        PRODUCE_STATE_DB="/tmp/chosen.sqlite3",
    )
    assert result.returncode == 0 and len(launched) == 2
    assert all(
        "--setenv=PRODUCE_STATE_DB=/tmp/chosen.sqlite3" in args for args in launched
    )
