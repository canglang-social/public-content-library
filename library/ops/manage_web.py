"""Manage the local-only ArchiveBox reading service for this installation."""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import sys

if __package__:
    from .manage_schedule import command
    from .weekly_run import write_json
    from .storage import control_root, require_storage
else:
    from manage_schedule import command
    from weekly_run import write_json
    from storage import control_root, require_storage

LABEL = "local.public-content-library.web"


def inspect(target):
    result = command("/bin/launchctl", "print", target)
    if result["exit_code"] == 0:
        return True, result
    if ('Could not find service "' + LABEL + '"') in result["stdout"] + result["stderr"]:
        return False, result
    raise RuntimeError("Could not determine the local library service state")


def live_arguments(result):
    match = re.search(r"(?m)^\s*arguments = \{\n(.*?)^\s*\}", result["stdout"], re.DOTALL)
    if not match:
        return []
    values = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    return [json.loads(value) if value.startswith('"') and value.endswith('"') else value for value in values]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "status", "remove"])
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    path = Path(args.config).absolute()
    config = json.loads(path.read_text()) if path.exists() else {}
    global LABEL
    LABEL = config.get("job_label_prefix", "local.public-content-library") + ".web"
    port = int(config.get("web_port", 8864))
    data = Path(config.get("archivebox_data", path.parent / "archivebox"))
    operations = control_root(config) if config else path.parent / "control"
    operations.mkdir(parents=True, exist_ok=True)
    tail = [str(Path(config.get("module_root", "")) / "ops/web_server.py"), "--config", str(path)]
    arguments = [config.get("python", "")] + tail
    wanted = {
        "Label": LABEL, "ProgramArguments": arguments,
        "WorkingDirectory": config.get("module_root", ""),
        "EnvironmentVariables": {"PYTHONPATH": config.get("module_root", ""), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PATH": config.get("path", "")},
        "RunAtLoad": True, "StartOnMount": bool(config.get("storage")), "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 60,
        "ProcessType": "Background", "ExitTimeOut": 15,
        "StandardOutPath": str(operations / "library.stdout.log"),
        "StandardErrorPath": str(operations / "library.stderr.log"),
    }
    plist = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    domain = "gui/" + str(os.getuid())
    target = domain + "/" + LABEL
    existing = plistlib.loads(plist.read_bytes()) if plist.exists() else None
    if existing and (existing.get("ProgramArguments", [])[1:] != tail or (config and existing.get("ProgramArguments") != arguments)):
        raise ValueError("Existing library service belongs to another installation; preserved")
    loaded, current = inspect(target)
    if loaded and (live_arguments(current)[1:] != tail or (config and live_arguments(current) != arguments)):
        raise ValueError("Loaded library service belongs to another installation; preserved")
    if args.action == "install":
        require_storage(config)
        if not all(config.get(key) for key in ["python", "module_root", "path"]):
            raise ValueError("The library configuration is incomplete")
        if not (data / "index.sqlite3").is_file() or not (data / "collector-bridge.json").is_file():
            raise ValueError("Publish and verify the library before starting the reading service")
        if not Path(config["python"]).is_file():
            raise ValueError("The configured Python environment is unavailable")
        if loaded and existing != wanted:
            command("/bin/launchctl", "bootout", target)
            loaded, current = inspect(target)
            if loaded:
                raise RuntimeError("Previous service could not be unloaded")
        if not loaded:
            with socket.socket() as check:
                check.settimeout(1)
                connection = check.connect_ex(("127.0.0.1", port))
                if connection == 0:
                    raise RuntimeError(f"Port {port} is occupied by another process; it was preserved")
                if connection != errno.ECONNREFUSED:
                    raise RuntimeError(f"Could not establish whether port {port} is free; no service was started")
            plist.parent.mkdir(parents=True, exist_ok=True)
            temporary = plist.with_suffix(".plist.tmp")
            temporary.write_bytes(plistlib.dumps(wanted))
            temporary.chmod(0o644)
            temporary.replace(plist)
            started = command("/bin/launchctl", "bootstrap", domain, str(plist))
            if started["exit_code"]:
                raise RuntimeError("The local service could not start: " + started["stderr"].strip())
    elif args.action == "remove":
        if loaded:
            command("/bin/launchctl", "bootout", target)
        loaded, current = inspect(target)
        if loaded:
            raise RuntimeError("The service is still loaded; its definition was preserved")
        if existing:
            plist.unlink()
    loaded, current = inspect(target)
    result = {"action": args.action, "loaded": loaded, "url": f"http://127.0.0.1:{port}", "binding": "127.0.0.1 only", "launchctl": current}
    write_json(operations / "web-status.json", result)
    print(json.dumps(result))
    return 0 if args.action != "install" or loaded else 3


if __name__ == "__main__":
    sys.exit(main())
