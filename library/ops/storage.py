"""Check an explicitly configured external volume before touching library data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys


class StorageUnavailable(RuntimeError):
    pass


def control_root(config):
    return Path(config.get('control', config['operations']))


def require_storage(config):
    storage = config.get('storage')
    if not storage:
        return {'status': 'available', 'external': False}
    mount = Path(storage['mount_root']).absolute()
    root = Path(storage['root']).absolute()
    if mount.is_symlink() or not mount.is_mount():
        raise StorageUnavailable('Connect the configured external drive; its mount is unavailable.')
    if (not root.is_relative_to(mount) or root == mount or root.is_symlink()
            or root.resolve() == mount.resolve() or not root.resolve().is_relative_to(mount.resolve())):
        raise StorageUnavailable('The configured library root is not a dedicated directory on the expected volume.')
    expected_uuid = storage.get('volume_uuid')
    if expected_uuid:
        if sys.platform != 'darwin':
            raise StorageUnavailable('This volume-UUID check requires macOS; configure a supported mount guard for this system.')
        try:
            result = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', str(mount)],
                                    capture_output=True, timeout=15, check=True)
            actual = plistlib.loads(result.stdout).get('VolumeUUID')
        except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException) as exc:
            raise StorageUnavailable('Unable to verify the configured volume identity.') from exc
        if not actual or str(actual).lower() != str(expected_uuid).lower():
            raise StorageUnavailable('A different external volume is mounted at the configured location.')
    marker = root / '.pcl-storage.json'
    if marker.is_symlink() or not marker.is_file():
        raise StorageUnavailable('The external library identity marker is missing.')
    try:
        identity = json.loads(marker.read_text())
    except (OSError, ValueError) as exc:
        raise StorageUnavailable('The external library identity marker is unreadable.') from exc
    if not storage.get('storage_id') or identity.get('storage_id') != storage['storage_id']:
        raise StorageUnavailable('The external library identity does not match this private configuration.')
    paths = [config.get('state'), config.get('operations'), config.get('archivebox_data')]
    for value in paths:
        if value is None:
            continue
        p = Path(value).absolute()
        if not p.is_relative_to(root) or p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
            raise StorageUnavailable('A library data path leaves the configured external storage root.')
    return {'status': 'available', 'external': True, 'root': str(root), 'mount_root': str(mount)}


def unavailable_result(config, error):
    result = {'status': 'storage_unavailable', 'reason': str(error),
              'data_changed': False, 'schedule_period_completed': False}
    # Status is kept on the Mac so an absent disk never causes a fallback
    # directory, empty database, or misleading completed-week checkpoint.
    control = control_root(config)
    mount = Path(config.get('storage', {}).get('mount_root', '/nonexistent-external-storage'))
    if control.is_absolute() and not control.is_relative_to(mount):
        control.mkdir(parents=True, exist_ok=True)
        output = control / 'storage-status.json'
        temp = output.with_suffix('.json.tmp')
        temp.write_text(json.dumps(result, indent=2) + '\n')
        temp.chmod(0o600)
        temp.replace(output)
    return result


def runtime_environment(config):
    env = os.environ.copy()
    env.update(PYTHONPATH=config['module_root'], PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    if config.get('path'):
        env['PATH'] = config['path']
    if config.get('storage'):
        env['PCL_STORAGE_GUARD'] = json.dumps({key: config[key] for key in
            ['storage', 'state', 'operations', 'archivebox_data'] if key in config})
    else:
        env.pop('PCL_STORAGE_GUARD', None)
    for key, name in [('id_namespace', 'PCL_ID_NAMESPACE'),
                      ('archivebox_python', 'PCL_ARCHIVEBOX_PYTHON'),
                      ('bridge_runtime', 'PCL_BRIDGE_RUNTIME'),
                      ('bridge_ready', 'PCL_BRIDGE_READY')]:
        if config.get(key):
            env[name] = str(config[key])
    return env
