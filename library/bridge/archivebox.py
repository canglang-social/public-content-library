"""Isolated ArchiveBox launcher. Native app output stays in retained logs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).absolute().parent
LIBRARY = ROOT.parent
PYTHON = Path(os.environ.get("PCL_ARCHIVEBOX_PYTHON", sys.executable))
CLI = PYTHON.parent / "archivebox"


def publisher_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ["archivebox.py", "archivebox_worker.py", "../collector/presentation.py"]}


def environment(data, port=8870):
    runtime = Path(os.environ.get("PCL_BRIDGE_RUNTIME", Path(tempfile.gettempdir()) / "public-content-library-bridge"))
    for name in ["cache", "config", "lib", "local-data", "logs"]:
        (runtime / name).mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in list(env):
        if any(part in key.upper() for part in ("API_KEY", "APIKEY", "AUTH_TOKEN", "ACCESS_TOKEN")):
            env.pop(key, None)
    env.update({"PATH": str(PYTHON.parent) + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str(LIBRARY), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "NO_COLOR": "1", "TERM": "dumb", "DATA_DIR": str(data),
                "UV_CACHE_DIR": str(runtime / "cache/uv"), "XDG_CACHE_HOME": str(runtime / "cache"),
                "XDG_CONFIG_HOME": str(runtime / "config"), "XDG_DATA_HOME": str(runtime / "local-data"),
                "ABXPKG_LIB_DIR": str(runtime / "lib"),
                "TMP_DIR": str(Path(tempfile.gettempdir()) / ("public-content-library-" + hashlib.sha256(str(data).encode()).hexdigest()[:10])),
                "PLUGINS": "search_backend_sqlite", "SEARCH_BACKEND_ENGINE": "sqlite", "SEARCH_BACKEND_SQLITE_ENABLED": "true",
                "SEARCH_BACKEND_SONIC_ENABLED": "false", "WGET_ENABLED": "false", "CHROME_ENABLED": "false",
                "ARCHIVEDOTORG_ENABLED": "false", "OPENCODE_ENABLED": "false", "CLAUDECODE_ENABLED": "false",
                "COOKIES_FILE": "", "AUTH_STORAGE_FILE": "", "WGET_COOKIES_FILE": "",
                "BIND_ADDR": f"127.0.0.1:{port}", "BASE_URL": f"http://127.0.0.1:{port}",
                "ALLOWED_HOSTS": "127.0.0.1,localhost", "PUBLIC_INDEX": "true", "PUBLIC_ADD_VIEW": "false",
                "SERVER_SECURITY_MODE": "safe-onedomain-nojsreplay"})
    Path(env["TMP_DIR"]).mkdir(parents=True, exist_ok=True)
    return env


def run(command, data, label, *, timeout=180, port=8870):
    logs = Path(os.environ.get("PCL_BRIDGE_RUNTIME", Path(tempfile.gettempdir()) / "public-content-library-bridge")) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = str(time.time_ns())
    output, error = logs / (stamp + "-" + label + ".stdout"), logs / (stamp + "-" + label + ".stderr")
    with output.open("w") as stdout, error.open("w") as stderr:
        process = subprocess.Popen(command, cwd=data, env=environment(data, port), stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
    if code:
        raise RuntimeError(f"ArchiveBox {label} exited {code}; logs: {output}, {error}")
    rows = []
    for line in output.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except ValueError:
            pass
    return rows[-1] if rows else {"status": "ok", "stdout": str(output), "stderr": str(error)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["publish", "inspect", "backup", "pull", "serve", "credentials"])
    parser.add_argument("--data", required=True)
    parser.add_argument("--bundle")
    parser.add_argument("--state")
    parser.add_argument("--output")
    parser.add_argument("--login-file")
    parser.add_argument("--username", default="library")
    parser.add_argument("--port", type=int, default=8870)
    args = parser.parse_args(argv)
    try:
        if os.environ.get("PCL_STORAGE_GUARD"):
            from ops.storage import require_storage
            require_storage(json.loads(os.environ["PCL_STORAGE_GUARD"]))
        data = Path(args.data).resolve()
        data.mkdir(parents=True, exist_ok=True)
        if not PYTHON.is_file():
            raise RuntimeError("Tested ArchiveBox 0.9.77 virtual environment is unavailable")
        if args.command == "publish":
            validation = os.environ.get("PCL_BRIDGE_VALIDATION") == "1" and data.is_relative_to(Path(os.environ.get("PCL_VALIDATION_ROOT", ROOT / "validation")).resolve())
            if not validation:
                ready_path = Path(os.environ.get("PCL_BRIDGE_READY", ROOT / "validation/ready.json"))
                ready = json.loads(ready_path.read_text()) if ready_path.exists() else {}
                hashes = publisher_hashes()
                if ready.get("status") != "ok" or ready.get("code_hashes") != hashes:
                    raise RuntimeError("Native bridge validation has not passed for the current publisher; publication was not attempted")
        if not (data / "index.sqlite3").is_file():
            if args.command == "pull":
                print(json.dumps({"status": "ok", "annotations_pulled": 0, "reason": "No ArchiveBox collection exists yet"}))
                return 0
            if args.command != "publish":
                raise ValueError("Collection does not exist; publish a bundle first")
            run([str(CLI), "init"], data, "init", timeout=120, port=args.port)
        if args.command == "serve":
            os.chdir(data)
            os.execve(CLI, [str(CLI), "manage", "runserver", f"127.0.0.1:{args.port}", "--noreload"], environment(data, args.port))
        command = [str(PYTHON), "-m", "bridge.archivebox_worker", args.command, "--data", str(data)]
        for name in ["bundle", "state", "output", "login_file"]:
            value = getattr(args, name)
            if value:
                command += ["--" + name.replace("_", "-"), str(Path(value).absolute())]
        if args.command == "credentials":
            command += ["--username", args.username]
        result = run(command, data, args.command, port=args.port)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted"}))
        return 130
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 3


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
    sys.exit(main())
