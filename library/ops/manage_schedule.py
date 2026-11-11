"""Install or inspect the user's explicitly selected local weekly schedule."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

if __package__:
    from .weekly_run import validate_config, write_json
    from .storage import control_root
else:
    from weekly_run import validate_config, write_json
    from storage import control_root

LABEL = "local.public-content-library.weekly"


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def inspect_job(target):
    result = command("/bin/launchctl", "print", target)
    if result["exit_code"] == 0:
        return "loaded", result
    message = result["stdout"] + result["stderr"]
    if ('Could not find service "' + LABEL + '"') in message:
        return "absent", result
    raise RuntimeError("Could not determine schedule state: " + message.strip())


def loaded_is_owned(result, config_path):
    # Check the live job as well as its on-disk definition. A missing plist is
    # not permission to unload another in-memory service with the same label.
    match = re.search(r"(?m)^\s*arguments = \{\n(.*?)^\s*\}", result["stdout"], re.DOTALL)
    if not match:
        return False
    arguments = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    arguments = [json.loads(arg) if arg.startswith('"') and arg.endswith('"') else arg for arg in arguments]
    return len(arguments) == 4 and arguments[1].endswith("/ops/weekly_run.py") and arguments[2:] == ["--config", str(config_path)]


def definition(config, config_path):
    validate_config(config)
    schedule = config["schedule"]
    local_zone = os.path.realpath("/etc/localtime").split("zoneinfo/")[-1]
    if local_zone != schedule["timezone"]:
        raise ValueError("The Mac timezone differs from the selected schedule; do not silently change either timezone")
    operations = control_root(config)
    return {
        "Label": LABEL,
        "ProgramArguments": [config["python"], str(Path(config["module_root"]) / "ops/weekly_run.py"), "--config", str(config_path)],
        "WorkingDirectory": config["module_root"],
        "StartCalendarInterval": {"Weekday": (schedule["weekday"] + 1) % 7, "Hour": schedule["hour"], "Minute": schedule["minute"]},
        "RunAtLoad": True,
        "StartOnMount": bool(config.get("storage")),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "ExitTimeOut": 20,
        "StandardOutPath": str(operations / "scheduler.stdout.log"),
        "StandardErrorPath": str(operations / "scheduler.stderr.log"),
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "install", "status", "remove"])
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config_path = Path(args.config).absolute()
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    global LABEL
    LABEL = config.get("job_label_prefix", "local.public-content-library") + ".weekly"
    wanted = definition(config, config_path) if args.action in {"prepare", "install"} else None
    plist_path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    domain = "gui/" + str(os.getuid())
    target = domain + "/" + LABEL
    existing = plistlib.loads(plist_path.read_bytes()) if plist_path.exists() else None
    expected_tail = ["--config", str(config_path)]
    if existing and existing.get("ProgramArguments", [])[-2:] != expected_tail:
        raise ValueError("An existing job with this name belongs to another installation; it was preserved")
    operations = control_root(config) if config else config_path.parent / "control"
    operations.mkdir(parents=True, exist_ok=True)
    prepared = operations / (LABEL + ".plist")
    if wanted:
        prepared.write_bytes(plistlib.dumps(wanted))
    state, live = inspect_job(target)
    if state == "loaded" and not loaded_is_owned(live, config_path) and args.action in {"install", "remove"}:
        raise ValueError("The loaded job belongs to another installation; it was preserved")
    results = {"action": args.action, "schedule": config.get("schedule"), "mac_timezone": os.path.realpath("/etc/localtime"), "prepared_plist": str(prepared), "installed_plist": str(plist_path)}
    if args.action == "install":
        # A tested manual run must precede activation, including verified backup.
        prior = json.loads((Path(config["operations"]) / "latest-result.json").read_text())
        if prior.get("status") not in {"ok", "partial"} or not any(s["name"] == "verify_backup" and s["exit_code"] == 0 for s in prior.get("steps", [])):
            raise ValueError("Run and verify the complete weekly workflow before activating the schedule")
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        if state == "loaded" and existing != wanted:
            stopped = command("/bin/launchctl", "bootout", target)
            if stopped["exit_code"]:
                raise RuntimeError("Could not unload the previous task definition")
            state, live = inspect_job(target)
            if state != "absent":
                raise RuntimeError("The previous job did not unload")
        temporary = plist_path.with_suffix(".plist.tmp")
        temporary.write_bytes(plistlib.dumps(wanted))
        temporary.chmod(0o644)
        temporary.replace(plist_path)
        if state == "absent":
            results["bootstrap"] = command("/bin/launchctl", "bootstrap", domain, str(plist_path))
    elif args.action == "remove":
        if state == "loaded":
            results["bootout"] = command("/bin/launchctl", "bootout", target)
        state, live = inspect_job(target)
        if state != "absent":
            raise RuntimeError("The job is still loaded; its definition was retained")
        if existing:
            plist_path.unlink()
    state, check = inspect_job(target)
    results.update(loaded=state == "loaded", launchctl=check)
    write_json(operations / "schedule-status.json", results)
    print(json.dumps(results))
    return 0 if args.action != "install" or results["loaded"] else 3


if __name__ == "__main__":
    sys.exit(main())
