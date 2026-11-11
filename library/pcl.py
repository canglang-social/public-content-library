"""Small public entry point; each installation keeps its configuration outside Git."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

from collector import __main__ as collector_cli
from collector.backup import backup
from collector.models import Artifact, Batch, Record
from collector.runner import atomic_json, collect
from collector.store import Store
from ops.storage import StorageUnavailable, require_storage, runtime_environment, unavailable_result
from ops.weekly_run import execute, write_json


def initialize(args):
    home = Path(args.config_home).expanduser().absolute()
    root = Path(args.data_root).expanduser().absolute()
    if home == root or home.is_relative_to(root) or root.is_relative_to(home):
        raise ValueError('Configuration and data must use separate directories.')
    if home.exists() or root.exists():
        raise ValueError('Initialization requires new configuration and data directories; existing files were preserved.')
    ZoneInfo(args.timezone)
    hour, minute = map(int, args.time.split(':'))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError('Time must be HH:MM in the selected timezone.')
    config = {
        'module_root': str(Path(__file__).absolute().parent), 'python': sys.executable,
        'registry': str(home / 'sources.json'), 'state': str(root / 'state'),
        'operations': str(root / 'operations'), 'control': str(home / 'control'),
        'schedule': {'weekday': ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'].index(args.weekday),
                     'hour': hour, 'minute': minute, 'timezone': args.timezone},
        'max_runtime_seconds': 1200, 'minimum_free_bytes': 2 * 1024**3,
        'path': os.environ.get('PATH', '/usr/bin:/bin'),
        'audio': {'enabled': False},
    }
    if args.external_mount:
        mount = Path(args.external_mount).expanduser().absolute()
        if (mount.is_symlink() or not mount.is_mount() or root == mount
                or not root.is_relative_to(mount) or not root.resolve().is_relative_to(mount.resolve())
                or home.is_relative_to(mount) or home.resolve().is_relative_to(mount.resolve())):
            raise ValueError('Connect the expected drive and choose a new data directory on it; configuration must stay on the computer.')
        storage = {'mount_root': str(mount), 'root': str(root), 'storage_id': str(uuid.uuid4())}
        if sys.platform == 'darwin':
            result = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', str(mount)], check=True, capture_output=True, timeout=15)
            storage['volume_uuid'] = plistlib.loads(result.stdout)['VolumeUUID']
        config['storage'] = storage
    root.mkdir(parents=True, mode=0o700)
    home.mkdir(parents=True, mode=0o700)
    if config.get('storage'):
        write_json(root / '.pcl-storage.json', {'storage_id': config['storage']['storage_id']})
    write_json(home / 'sources.json', {'schema_version': 1, 'sources': []})
    write_json(home / 'settings.json', config)
    return {'status': 'initialized', 'config': str(home / 'settings.json'), 'data_root': str(root),
            'sources': 0, 'schedule_installed': False, 'next': 'Add public sources to sources.json, then run pcl update --config PATH.'}


def demo(output):
    root = Path(output).expanduser().absolute()
    if root.exists():
        raise ValueError('Choose a new demo directory; existing files were preserved.')
    state = root / 'state'
    source = {'id': 'synthetic-demo', 'adapter': 'text', 'url': 'https://example.org/synthetic-research',
              'tags': ['example'], 'review_status': 'included'}
    registry = {'schema_version': 1, 'sources': [source]}
    for text in ['Synthetic research note about reusable libraries.',
                 'Synthetic research note about reusable libraries. A second edition adds provenance.']:
        record = Record(source['url'], title='Synthetic research note', text=text, review_status='included',
                        provenance={'synthetic_fixture': True},
                        artifacts=[Artifact(data=text.encode(), media_type='text/plain', role='original', source_url=source['url'])])
        collect(registry, state, adapter=lambda s, c: Batch([record], status='ok', coverage={'window_complete': True}))
    store = Store(state)
    try:
        item = store.search('provenance')[0]
        store.annotate(item['id'], notes='An example private note.', tags=['demo'])
        bundle = store.export()
        atomic_json(root / 'export.json', bundle)
        counts = {'items': len(bundle['items']), 'versions': sum(len(i['versions']) for i in bundle['items'])}
    finally:
        store.db.close()
    verified = backup(state, root / 'backup')
    return {'status': 'ok', 'synthetic': True, 'network_requests': 0, **counts,
            'search_match': item['id'], 'backup': verified, 'directory': str(root)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='pcl', description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init', help='Create private configuration; does not install a schedule.')
    init.add_argument('--config-home', required=True)
    init.add_argument('--data-root', required=True)
    init.add_argument('--external-mount')
    init.add_argument('--weekday', choices=['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], required=True)
    init.add_argument('--time', required=True, help='HH:MM')
    init.add_argument('--timezone', required=True, help='IANA timezone name')
    example = sub.add_parser('demo', help='Test collection, versioning, search and backup offline.')
    example.add_argument('--output', required=True)
    for name in ['status', 'search', 'export', 'backup', 'update', 'schedule', 'reader']:
        command = sub.add_parser(name)
        command.add_argument('--config', required=True)
        if name == 'search':
            command.add_argument('query')
        if name in {'export', 'backup'}:
            command.add_argument('--output', required=True)
        if name == 'update':
            command.add_argument('--due-only', action='store_true', help='Skip an already attempted week.')
            command.add_argument('--dry-run', action='store_true')
        if name == 'schedule':
            command.add_argument('operation', choices=['prepare', 'install', 'status', 'remove'])
        if name == 'reader':
            command.add_argument('operation', choices=['install', 'status', 'remove'])
    verify = sub.add_parser('verify-backup')
    verify.add_argument('--path', required=True)
    args = parser.parse_args(argv)
    config = None
    try:
        if args.action == 'init':
            result = initialize(args)
        elif args.action == 'demo':
            result = demo(args.output)
        elif args.action == 'verify-backup':
            return collector_cli.main(['verify-backup', '--path', args.path])
        else:
            config = json.loads(Path(args.config).expanduser().read_text())
            if args.action in {'schedule', 'reader'}:
                if sys.platform != 'darwin':
                    raise ValueError('Service management currently supports macOS only.')
                module = 'ops.manage_schedule' if args.action == 'schedule' else 'ops.manage_web'
                return subprocess.call([config['python'], '-m', module, args.operation, '--config', str(Path(args.config).expanduser().absolute())],
                                       env=runtime_environment(config))
            require_storage(config)
            if args.action == 'update':
                result, code = execute(config, force=not args.due_only, dry_run=args.dry_run)
                print(json.dumps(result, ensure_ascii=False))
                return code
            command = [args.action, '--state', config['state']]
            if args.action == 'search':
                command.append(args.query)
            if args.action in {'export', 'backup'}:
                command += ['--output', str(Path(args.output).expanduser().absolute())]
            return collector_cli.main(command)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except StorageUnavailable as error:
        print(json.dumps(unavailable_result(config, error)))
        return 2
    except Exception as error:
        print(json.dumps({'status': 'error', 'error': str(error)}))
        return 3


if __name__ == '__main__':
    sys.exit(main())
