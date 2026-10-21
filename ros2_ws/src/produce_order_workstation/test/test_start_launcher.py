"""CLI contract tests with fake systemd commands; never start/stop real robots."""

import json
import os
from pathlib import Path
import subprocess
import shutil


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
        unit = next((word for word in args if word.endswith(".service")), "")
        print("loaded" if unit in active else "not-found")
"""


def run_launcher(tmp_path, command, **extra):
    # Copy the launcher plus harmless profile helper, stub preflight/health.
    # Tests must never import model environments or inspect live ROS state.
    app = tmp_path / "app"
    app.mkdir()
    launcher = app / "start.sh"
    shutil.copyfile(LAUNCHER, launcher)
    scripts = app / "ros2_ws/src/produce_order_workstation/scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(
        LAUNCHER.parent
        / "ros2_ws/src/produce_order_workstation/scripts/runtime_env.sh",
        scripts / "runtime_env.sh",
    )
    (scripts / "doctor.sh").write_text('#!/bin/bash\nexit "${FAKE_DOCTOR_STATUS:-0}"\n')
    (scripts / "check_runtime.sh").write_text(
        '#!/bin/bash\nexit "${FAKE_READY_STATUS:-0}"\n'
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("systemctl", "systemd-run", "pgrep"):
        file = tools / name
        file.write_text(FAKE)
        file.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    profile = tmp_path / "launch.env"
    profile.write_text(extra.pop("FAKE_PROFILE_BODY", ""))
    env = dict(
        os.environ,
        PATH=f"{tools}:/usr/bin:/bin",
        FAKE_LOG=str(log),
        DISPLAY=":99",
        PRODUCE_LAUNCH_CONFIG=str(profile),
    )
    # A full-suite runner exports a real profile/domain; this fake systemd
    # fixture must not accidentally inherit that machine's configuration.
    for name in tuple(env):
        if name.startswith("PRODUCE_") and name != "PRODUCE_LAUNCH_CONFIG":
            env.pop(name)
    env.update(extra)
    result = subprocess.run(
        ["/bin/bash", str(launcher), command],
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


def test_failed_preflight_does_not_start_heavy_processes(tmp_path):
    result, launched = run_launcher(tmp_path, "start", FAKE_DOCTOR_STATUS="1")
    assert result.returncode == 1 and not launched


def test_business_not_ready_is_not_reported_as_launch_success(tmp_path):
    result, launched = run_launcher(tmp_path, "start", FAKE_READY_STATUS="2")
    assert result.returncode == 2 and len(launched) == 2


def test_profile_reaches_systemd_but_terminal_settings_win_and_key_is_not_forwarded(
    tmp_path,
):
    result, launched = run_launcher(
        tmp_path,
        "start",
        FAKE_PROFILE_BODY="PRODUCE_AGENT_MODEL=profile-model\nPRODUCE_ROS_DOMAIN_ID=7\n",
        PRODUCE_ROS_DOMAIN_ID="8",
        DASHSCOPE_API_KEY="not-a-live-key",
    )
    assert result.returncode == 0
    assert all(
        "--setenv=PRODUCE_AGENT_MODEL=profile-model" in args for args in launched
    )
    assert all("--setenv=PRODUCE_ROS_DOMAIN_ID=8" in args for args in launched)
    assert all(
        "DASHSCOPE_API_KEY" not in str(args) and "not-a-live-key" not in str(args)
        for args in launched
    )


def test_missing_explicit_profile_fails_before_launch(tmp_path):
    result, launched = run_launcher(
        tmp_path, "start", PRODUCE_LAUNCH_CONFIG=str(tmp_path / "missing.env")
    )
    assert result.returncode == 1 and not launched


def test_start_cannot_silently_switch_a_running_database(tmp_path):
    result, launched = run_launcher(
        tmp_path,
        "start",
        FAKE_ACTIVE="produce-order-workstation.service",
        FAKE_PREVIOUS_DB="/tmp/old.sqlite3",
        PRODUCE_STATE_DB="/tmp/new.sqlite3",
    )
    assert result.returncode == 1 and not launched
    assert "restart" in result.stderr


def test_stop_does_not_require_valid_config_and_stops_test_controller(tmp_path):
    result, launched = run_launcher(
        tmp_path, "stop", PRODUCE_LAUNCH_CONFIG=str(tmp_path / "missing.env"),
        FAKE_ACTIVE="produce-four-bin-pressure.service,produce-order-workstation.service,produce-order-panel.service",
    )
    assert result.returncode == 0 and not launched
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert ["systemctl", ["--user", "stop", "produce-four-bin-pressure.service"]] in calls
