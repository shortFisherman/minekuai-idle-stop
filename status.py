"""Read the VPS's local Minekuai service records and print a status summary."""

import argparse
import json
import os
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

from inventory import Inventory
from keepalive import CHINA_TIME
from minekuai import APIError, AuthError, Minekuai
from monitor import DEFAULT_AUTH, DEFAULT_CONFIG, DEFAULT_STATE, load_config

MONITOR_SERVICE = "minekuai-stop.service"
COLD_SERVICE = "minekuai-cold.service"
COLD_TIMER = "minekuai-cold.timer"
PROBE_MESSAGE = "游戏人数检查完成："
PROBE_COUNTS = re.compile(r"探测 (\d+) 台，有效人数 (\d+) 台，名单共 (\d+) 台")


def system_output(*args, allow_empty=False):
    result = subprocess.run(args, text=True, encoding="utf-8", capture_output=True, timeout=8,
                            env={**os.environ, "TZ": "UTC", "LC_ALL": "C.UTF-8"})
    if allow_empty and result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return ""
    result.check_returncode()
    return result.stdout


def unit_properties(unit, *names):
    output = system_output("systemctl", "show", unit, *(f"--property={name}" for name in names))
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def china_time(value):
    return datetime.fromtimestamp(value, CHINA_TIME).isoformat(timespec="seconds")


def system_time(value):
    if not value or value == "n/a":
        return None
    parsed = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    return china_time(parsed.timestamp())


def last_probe():
    output = system_output("journalctl", "-u", MONITOR_SERVICE, "--no-pager", "-n", "1",
                           "--output=json", "--grep", PROBE_MESSAGE, allow_empty=True)
    if not output.strip():
        return None
    event = json.loads(output)
    match = PROBE_COUNTS.search(event["MESSAGE"])
    if match is None:
        return None
    probed, responses, known = map(int, match.groups())
    return {"completed_at": china_time(int(event["__REALTIME_TIMESTAMP"]) / 1_000_000),
            "probed": probed, "responses": responses, "known": known}


def read_record(path):
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(record, dict):
        raise ValueError("VPS 本地运行记录格式无效")
    return record


def collect_status(config, state_file, auth_file, backup_root):
    state_directory = state_file.parent
    api = Minekuai(config.instance, auth_file, state_directory=state_directory)
    inventory = Inventory(api, state_directory).read() if config.instance is None else {config.instance: None}
    try:
        api.ensure_session_usable()
        session_state = "no_rejection_recorded"
    except AuthError:
        session_state = "reauthorization_needed"

    backups = backup_root / "instances"
    backup_dates = []
    instances = []
    deadlines = []
    need_keepalive = False
    for instance in sorted(inventory or {}):
        folder = backups / instance
        shutdown_file = state_file if config.instance is not None else state_directory / f"state-{instance}.json"
        keepalive_file = state_directory / (
            "keepalive.json" if config.instance is not None else f"keepalive-{instance}.json"
        )
        keepalive_record = read_record(keepalive_file)
        task = read_record(folder / "task.json")
        failure = read_record(folder / "failure.json").get("at")
        pending = []
        if shutdown_file.is_file():
            pending.append("shutdown")
        if "requested_at" in keepalive_record:
            pending.append("keepalive")
        if (folder / "billing.json").is_file():
            pending.append("cold_backup")
        row = {
            "instance_suffix": instance[-4:],
            "backup_date": None,
            "backup_failed_at": china_time(failure) if type(failure) in (int, float) else None,
            "backup_blocked": task.get("needs_review"),
            "pending_billing": pending,
        }
        if task and not (task.get("id") or task.get("archive") or row["backup_blocked"]):
            row["backup_blocked"] = "task_id_missing"
        instances.append(row)
        index = read_record(folder / "latest.json")
        filename = index.get("file")
        if isinstance(filename, str) and filename.endswith(".zip") and filename == Path(filename).name:
            path = folder / filename
            if not path.is_symlink() and path.is_file() and path.stat().st_size == index.get("size"):
                row["backup_date"] = date.fromisoformat(index["date"]).isoformat()
                backup_dates.append(row["backup_date"])
        if config.keepalive_days is None or (
            config.keepalive_instances is not None and instance not in config.keepalive_instances
        ):
            continue
        after = keepalive_record.get("next_after")
        if "requested_at" in keepalive_record or type(after) not in (int, float) or after <= 0:
            need_keepalive = True
        else:
            deadlines.append(after)

    monitor = unit_properties(MONITOR_SERVICE, "ActiveState", "ActiveEnterTimestamp")
    timer = unit_properties(COLD_TIMER, "LoadState", "ActiveState", "NextElapseUSecRealtime")
    backup = unit_properties(COLD_SERVICE, "Result") if timer.get("LoadState") == "loaded" else {}
    return {
        "read_at": datetime.now(CHINA_TIME).isoformat(timespec="seconds"),
        "instances": instances if inventory is not None else None,
        "monitor": {
            "state": monitor.get("ActiveState"), "active_since": system_time(monitor.get("ActiveEnterTimestamp")),
            "last_game_check": last_probe(), "poll_seconds": config.poll_seconds,
            "known_instances": None if inventory is None else len(inventory),
        },
        "minekuai_session": session_state,
        "keepalive": {
            "enabled": config.keepalive_days is not None,
            "next_known_check_at": china_time(min(deadlines)) if deadlines else None,
            "has_instances_to_recheck": need_keepalive,
        },
        "cold_backup": {
            "timer_state": timer.get("ActiveState"), "last_run_result": backup.get("Result"),
            "next_check_at": system_time(timer.get("NextElapseUSecRealtime")),
            "instances_with_verified_archive": len(backup_dates),
            "latest_verified_archive_date": max(backup_dates, default=None),
            "pending_billing_cleanups": sum(1 for _ in backups.glob("*/billing.json")),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH)
    parser.add_argument("--backup-root", type=Path, default=Path("/var/lib/minekuai-cold"))
    args = parser.parse_args()
    try:
        result = collect_status(load_config(args.config), args.state, args.auth, args.backup_root)
    except (APIError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        parser.exit(1, "无法读取 VPS 的本地运行记录\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
