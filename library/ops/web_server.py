"""Keep the loopback reader bound to its configured, verified storage volume."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

if __package__:
    from .storage import StorageUnavailable, require_storage, runtime_environment, unavailable_result
else:
    from storage import StorageUnavailable, require_storage, runtime_environment, unavailable_result


def stop(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        process.wait()


def serve(config):
    try:
        require_storage(config)
    except StorageUnavailable as error:
        print(json.dumps(unavailable_result(config, error)), flush=True)
        return 0
    data = Path(config['archivebox_data'])
    if not (data / 'index.sqlite3').is_file() or not (data / 'collector-bridge.json').is_file():
        raise ValueError('The configured reading library has not been initialized and verified.')
    command = [config['python'], '-m', 'bridge.archivebox', 'serve', '--data', str(data),
               '--port', str(config.get('web_port', 8864))]
    child = subprocess.Popen(command, cwd=config['module_root'], env=runtime_environment(config), start_new_session=True)
    def terminate(signum, frame):
        stop(child)
        raise SystemExit(0)
    old = {sig: signal.signal(sig, terminate) for sig in [signal.SIGTERM, signal.SIGINT]}
    try:
        while child.poll() is None:
            time.sleep(2)
            try:
                require_storage(config)
            except StorageUnavailable as error:
                stop(child)
                print(json.dumps(unavailable_result(config, error)), flush=True)
                return 0
        return child.returncode
    finally:
        stop(child)
        for sig, handler in old.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    return serve(json.loads(Path(args.config).read_text()))


if __name__ == '__main__':
    sys.exit(main())
