"""Small user-facing launchers for the local library."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

if __package__:
    from .storage import StorageUnavailable, control_root, require_storage, unavailable_result
else:
    from storage import StorageUnavailable, control_root, require_storage, unavailable_result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["open", "update", "pause", "resume", "status"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config).absolute()
    config = json.loads(config_path.read_text())
    if args.action in {"open", "update", "resume"}:
        try:
            require_storage(config)
        except StorageUnavailable as error:
            print(json.dumps(unavailable_result(config, error)))
            return 2
    operations = control_root(config)
    operations.mkdir(parents=True, exist_ok=True)
    scripts = Path(config["module_root"]) / "ops"
    selected = {
        "open": [str(scripts / "manage_web.py"), "install"],
        "update": [str(scripts / "weekly_run.py"), "--force"],
        "pause": [str(scripts / "manage_schedule.py"), "remove"],
        "resume": [str(scripts / "manage_schedule.py"), "install"],
        "status": [str(scripts / "manage_schedule.py"), "status"],
    }[args.action]
    messages = {"open": "Opening the library…", "update": "Checking sources and saving a verified backup. This may take several minutes…", "pause": "Pausing weekly updates…", "resume": "Resuming weekly updates…", "status": "Checking the weekly schedule…"}
    print(messages[args.action], flush=True)
    output = operations / ("last-" + args.action + ".json")
    error = operations / ("last-" + args.action + ".stderr")
    with output.open("w") as stdout, error.open("w") as stderr:
        process = subprocess.run([config["python"], *selected, "--config", str(config_path)], stdout=stdout, stderr=stderr)
    if process.returncode not in {0, 2}:
        print("The action needs attention. Details are saved in " + str(error))
        return process.returncode
    result = json.loads(output.read_text())
    if args.action == "open":
        url = "http://127.0.0.1:" + str(config.get("web_port", 8864)) + "/public/"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.3)
        else:
            print("The reading service has not become ready. See the control directory library.stderr.log.")
            return 3
        if not args.no_open:
            webbrowser.open(url)
        print("Library: " + url)
    elif args.action == "update":
        status = result.get("status")
        print("An update is already running." if status == "already_running" else "Update saved. Some sources remain incomplete; see Latest update.txt." if status == "partial" else "Update completed." if status == "ok" else "Update status: " + str(status))
    elif args.action == "pause":
        print("Weekly updates are paused.")
    elif args.action == "resume":
        print("Weekly schedule: " + json.dumps(config["schedule"], sort_keys=True))
    else:
        print("Weekly updates: " + ("active" if result["loaded"] else "paused"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
