"""Actual weekly orchestration with fake children; no network or live library."""
from __future__ import annotations

from datetime import datetime
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

LIBRARY = Path(__file__).resolve().parents[1] / "library"
sys.path.insert(0, str(LIBRARY))
from ops import weekly_run


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class WeeklyAudioBehavior(unittest.TestCase):
    def setUp(self):
        lane = Path(tempfile.gettempdir())
        lane.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="weekly-audio-test-", dir=lane)
        self.root = Path(self.temporary.name)
        self.registry = self.root / "sources.json"
        self.registry.write_text('{"schema_version":1,"sources":[]}')
        self.config_path = self.root / "settings.json"
        self.config = {
            "module_root": str(LIBRARY), "python": sys.executable,
            "registry": str(self.registry), "state": str(self.root / "state"),
            "operations": str(self.root / "operations"), "_config_path": str(self.config_path),
            "schedule": {"timezone": "Asia/Shanghai", "weekday": 0, "hour": 6, "minute": 30},
            "minimum_free_bytes": 1, "max_runtime_seconds": 1800,
            "collector_args": ["--max-seconds", "1200", "--max-artifact-bytes", "120000000"],
            "audio": {"enabled": True, "max_seconds": 600, "max_file_bytes": 80000000,
                      "finalization_reserve_seconds": 300},
            "pre_steps": [{"name": "pull_notes_and_tags", "command": ["fake-pull"]}],
            "post_steps": [{"name": "publish_library", "command": ["fake-publish"]},
                           {"name": "backup_library", "command": ["fake-native-backup"]}],
        }
        self.config_path.write_text(json.dumps(self.config))
        self.now = datetime.fromisoformat("2026-11-03T00:00:00+00:00")
        self.clock = FakeClock()
        self.calls = []
        self.reservations = []

    def tearDown(self):
        self.temporary.cleanup()

    def reservation(self, config, candidate_bytes=0, before_collection=False):
        self.reservations.append({"candidate_bytes": candidate_bytes, "before_collection": before_collection})
        return {"required_free_bytes": 200 if candidate_bytes else 100}

    def child(self, *, audio_code=0, collection_code=0, elapsed=None, old_resume=False,
              audio_deferred=None, timeout_step=None):
        elapsed = elapsed or {}

        def run(command, cwd, environment, output, error, timeout):
            name = output.stem
            self.calls.append({"name": name, "command": list(command), "timeout": timeout,
                               "started": self.clock(), "remaining_before": 2800 - self.clock()})
            error.write_text("")
            if name in {"collect", "collect_current"}:
                summary = {"status": "partial" if collection_code == 2 else "ok", "sources": [],
                           "resumed": old_resume and name == "collect",
                           "window_end": "2026-10-26T00:00:00+00:00" if old_resume and name == "collect" else self.now.isoformat(),
                           "new_items": 1, "changed_items": 0, "new_versions": 1}
                output.write_text(json.dumps(summary))
            elif name == "collect_audio":
                summary = {"status": "partial" if audio_code == 2 else "ok", "attempts": [],
                           "new_versions": 0, "pending_count": 1 if audio_code == 2 else 0,
                           "received_bytes": 0}
                if audio_deferred:
                    summary["deferred_reason"] = audio_deferred
                output.write_text(json.dumps(summary))
            else:
                output.write_text('{}')
            if name == "export":
                Path(command[command.index("--output") + 1]).write_text('{"items":[]}')
            self.clock.value += elapsed.get(name, 0)
            if name == timeout_step:
                raise subprocess.TimeoutExpired(command, timeout)
            return audio_code if name == "collect_audio" else collection_code if name.startswith("collect") else 0
        return run

    def run_fake(self, **options):
        free_bytes = options.pop("free_bytes", 1000)
        with patch.object(weekly_run, "now_utc", return_value=self.now), \
             patch.object(weekly_run.time, "monotonic", side_effect=self.clock), \
             patch.object(weekly_run, "disk_reservation", side_effect=self.reservation), \
             patch.object(weekly_run.shutil, "disk_usage", return_value=SimpleNamespace(free=free_bytes)), \
             patch.object(weekly_run, "run_child", side_effect=self.child(**options)):
            return weekly_run.execute(self.config)

    def names(self):
        return [row["name"] for row in self.calls]

    def assert_finalized(self):
        self.assertEqual(self.names()[-5:], ["export", "backup", "verify_backup", "publish_library", "backup_library"])

    def test_partial_audio_still_exports_verifies_publishes_and_backs_up(self):
        result, code = self.run_fake(audio_code=2)
        self.assertEqual((result["status"], code), ("partial", 2))
        self.assertEqual(self.names()[:3], ["pull_notes_and_tags", "collect", "collect_audio"])
        self.assert_finalized()
        self.assertEqual(result["audio_summary"]["pending_count"], 1)
        self.assertEqual(json.loads((self.root / "operations/schedule-state.json").read_text())["status"], "partial")

    def test_audio_uses_at_most600_seconds_and_keeps300_plus_shutdown_reserve(self):
        result, code = self.run_fake(elapsed={"collect": 1200})
        self.assertEqual((result["status"], code), ("ok", 0))
        audio = next(row for row in self.calls if row["name"] == "collect_audio")
        allocation = float(audio["command"][audio["command"].index("--max-seconds") + 1])
        self.assertLessEqual(allocation, 600)
        self.assertLessEqual(audio["timeout"] + 300, audio["remaining_before"])
        self.assertLessEqual(allocation + 10, audio["timeout"])
        self.assertEqual(allocation, 290)
        self.assert_finalized()

    def test_exhausted_audio_time_is_partial_without_running_audio_child(self):
        result, code = self.run_fake(elapsed={"pull_notes_and_tags": 300, "collect": 1190})
        self.assertEqual((result["status"], code), ("partial", 2))
        self.assertNotIn("collect_audio", self.names())
        self.assertEqual(result["audio_summary"]["status"], "partial")
        self.assertIn("reserve", result["audio_summary"]["deferred_reason"])
        self.assert_finalized()

    def test_collect_current_uses_remaining_shared_phase_before_audio(self):
        result, code = self.run_fake(old_resume=True, elapsed={"collect": 1100})
        self.assertEqual((result["status"], code), ("ok", 0))
        self.assertEqual(self.names()[:4], ["pull_notes_and_tags", "collect", "collect_current", "collect_audio"])
        current = next(row for row in self.calls if row["name"] == "collect_current")
        allocation = float(current["command"][current["command"].index("--max-seconds") + 1])
        self.assertGreater(allocation, 0)
        self.assertLessEqual(allocation, 100)
        self.assertLess(allocation, 1200)
        self.assertTrue(result["older_window_resumed_before_current_check"])

    def test_zero_remaining_text_phase_records_unchecked_current_window(self):
        result, code = self.run_fake(old_resume=True, elapsed={"collect": 1200})
        self.assertEqual((result["status"], code), ("partial", 2))
        self.assertNotIn("collect_current", self.names())
        self.assertTrue(result["current_window_check_deferred"])
        self.assertEqual(len(result["collection_summaries"]), 2)
        self.assert_finalized()

    def test_schedule_settings_and_caller_config_are_preserved(self):
        original = copy.deepcopy(self.config)
        result, code = self.run_fake()
        self.assertEqual(code, 0)
        self.assertEqual(self.config, original)
        self.assertEqual(result["schedule"], original["schedule"])
        self.assertEqual(json.loads(self.config_path.read_text()), original)
        self.assertEqual(result["period"], "2026-11-02T06:30:00+08:00")

    def test_insufficient_backup_space_fails_before_collection_or_annotation_pull(self):
        result, code = self.run_fake(free_bytes=99)
        self.assertEqual((result["status"], code), ("failed", 3))
        self.assertEqual(self.calls, [])
        self.assertIn("verified backups", result["error"])
        self.assertTrue(self.reservations[0]["before_collection"])

    def test_queue_disk_deferral_is_partial_and_keeps_text_publication(self):
        result, code = self.run_fake(audio_code=2, audio_deferred="Disk space reserved for publication and verified backups", free_bytes=150)
        self.assertEqual((result["status"], code), ("partial", 2))
        audio = next(row for row in self.calls if row["name"] == "collect_audio")
        self.assertEqual(audio["command"][audio["command"].index("--required-free-bytes") + 1], "200")
        self.assertIn("Disk space", result["audio_summary"]["deferred_reason"])
        self.assert_finalized()

    def test_audio_timeout_preserves_partial_status_and_runs_finalization(self):
        result, code = self.run_fake(timeout_step="collect_audio", elapsed={"collect_audio": 610})
        self.assertEqual((result["status"], code), ("partial", 2))
        self.assertIn("time allocation", result["audio_summary"]["deferred_reason"])
        self.assert_finalized()

    def test_fatal_audio_integrity_or_configuration_error_is_not_success(self):
        result, code = self.run_fake(audio_code=3)
        self.assertEqual((result["status"], code), ("failed", 3))
        self.assertNotIn("export", self.names())
        self.assertIn("collect_audio", result["error"])

    def test_direct_dry_run_entrypoint_does_not_need_inherited_pythonpath(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run([sys.executable, str(LIBRARY / "ops/weekly_run.py"), "--config", str(self.config_path), "--dry-run"],
                                cwd=self.root, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "dry_run")


if __name__ == "__main__":
    unittest.main(verbosity=2)
