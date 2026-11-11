"""An absent or substituted disk must not create a replacement library."""
import contextlib
import io
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ops import manage_schedule, storage, web_server, weekly_run
import pcl


class ExternalStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.mount = self.base / 'external'
        self.root = self.mount / 'library'
        self.root.mkdir(parents=True)
        (self.root / '.pcl-storage.json').write_text('{"storage_id":"test-volume"}')
        registry = self.base / 'sources.json'
        registry.write_text('{"schema_version":1,"sources":[]}')
        self.config = {'python': sys.executable, 'module_root': str(Path(pcl.__file__).parent),
                       'registry': str(registry), 'state': str(self.root / 'state'),
                       'operations': str(self.root / 'operations'), 'archivebox_data': str(self.root / 'archivebox'),
                       'control': str(self.base / 'control'),
                       'schedule': {'weekday': 0, 'hour': 6, 'minute': 30, 'timezone': 'Asia/Shanghai'},
                       'storage': {'mount_root': str(self.mount), 'root': str(self.root), 'storage_id': 'test-volume'}}

    def test_missing_mount_does_not_create_state_or_advance_week(self):
        with patch.object(Path, 'is_mount', return_value=False), patch.object(weekly_run, 'run_child') as child:
            result, code = weekly_run.execute(self.config)
        self.assertEqual((result['status'], code), ('storage_unavailable', 0))
        self.assertFalse(result['schedule_period_completed'])
        self.assertFalse(Path(self.config['operations']).exists())
        self.assertFalse(Path(self.config['state']).exists())
        child.assert_not_called()
        self.assertTrue((self.base / 'control/storage-status.json').is_file())
        with patch.object(Path, 'is_mount', return_value=True):
            again, _ = weekly_run.execute(self.config, dry_run=True)
        self.assertTrue(again['due'])

    def test_identity_marker_mismatch_and_missing_are_refused(self):
        marker = self.root / '.pcl-storage.json'
        with patch.object(Path, 'is_mount', return_value=True):
            storage.require_storage(self.config)
            marker.write_text('{"storage_id":"different-library"}')
            with self.assertRaises(storage.StorageUnavailable):
                storage.require_storage(self.config)
            marker.unlink()
            with self.assertRaises(storage.StorageUnavailable):
                storage.require_storage(self.config)

    def test_different_disk_uuid_is_refused_even_with_copied_marker(self):
        self.config['storage']['volume_uuid'] = 'expected-uuid'
        fake = Mock(stdout=plistlib.dumps({'VolumeUUID': 'other-uuid'}))
        with patch.object(Path, 'is_mount', return_value=True), patch.object(storage.sys, 'platform', 'darwin'), patch.object(storage.subprocess, 'run', return_value=fake):
            with self.assertRaises(storage.StorageUnavailable):
                storage.require_storage(self.config)

    def test_symlink_escape_and_external_control_are_not_written(self):
        Path(self.config['state']).symlink_to(self.base)
        with patch.object(Path, 'is_mount', return_value=True):
            with self.assertRaises(storage.StorageUnavailable):
                storage.require_storage(self.config)
        self.config['control'] = str(self.root / 'control')
        storage.unavailable_result(self.config, 'missing')
        self.assertFalse((self.root / 'control').exists())

    def test_missing_disk_reader_does_not_start_or_create_data(self):
        with patch.object(Path, 'is_mount', return_value=False), patch.object(web_server.subprocess, 'Popen') as child, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(web_server.serve(self.config), 0)
        child.assert_not_called()
        self.assertFalse(Path(self.config['archivebox_data']).exists())

    def test_mount_trigger_and_logs_remain_on_computer(self):
        with patch.object(manage_schedule.os.path, 'realpath', return_value='/var/db/timezone/zoneinfo/Asia/Shanghai'):
            definition = manage_schedule.definition(self.config, self.base / 'settings.json')
        self.assertTrue(definition['StartOnMount'])
        self.assertEqual(definition['StartCalendarInterval'], {'Weekday': 1, 'Hour': 6, 'Minute': 30})
        self.assertTrue(definition['StandardOutPath'].startswith(str(self.base / 'control')))

    def test_disconnect_during_run_preserves_uncompleted_period(self):
        calls = 0
        def guard(config):
            nonlocal calls
            calls += 1
            if calls > 2:
                raise storage.StorageUnavailable('Synthetic disconnect')
        def child(command, cwd, environment, output, error, timeout):
            output.write_text('{"status":"ok","resumed":false,"sources":[]}')
            error.write_text('')
            return 0
        with patch.object(weekly_run, 'require_storage', side_effect=guard), patch.object(weekly_run, 'run_child', side_effect=child):
            result, code = weekly_run.execute(self.config)
        self.assertEqual((result['status'], code), ('storage_unavailable', 0))
        history = json.loads((Path(self.config['operations']) / 'schedule-state.json').read_text())
        self.assertEqual(history['status'], 'running')
        self.assertFalse(result['schedule_period_completed'])


class PublicEntryPoint(unittest.TestCase):
    def test_offline_demo_retains_versions_and_verifies_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = pcl.demo(Path(temporary) / 'demo')
        self.assertEqual((result['items'], result['versions'], result['network_requests']), (1, 2, 0))

    def test_init_missing_drive_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            code = pcl.main(['init', '--config-home', str(root / 'private'), '--data-root', str(root / 'missing/library'),
                             '--external-mount', str(root / 'missing'), '--weekday', 'mon', '--time', '06:30', '--timezone', 'Asia/Shanghai'])
            self.assertEqual(code, 3)
            self.assertEqual(list(root.iterdir()), [])

    def test_init_keeps_private_config_outside_data_and_installs_no_jobs(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            code = pcl.main(['init', '--config-home', str(root / 'private'), '--data-root', str(root / 'library'),
                             '--weekday', 'wed', '--time', '08:15', '--timezone', 'UTC'])
            config = json.loads((root / 'private/settings.json').read_text())
            self.assertEqual(code, 0)
            self.assertEqual(config['schedule'], {'weekday': 2, 'hour': 8, 'minute': 15, 'timezone': 'UTC'})
            self.assertFalse(config['audio']['enabled'])
            self.assertEqual((root / 'private/settings.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual(list((root / 'library').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
