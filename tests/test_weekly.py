"""Behavior checks for the user-selected schedule and failure-safe orchestration."""
from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ops import weekly_run


class WeeklyBehavior(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="weekly-test-", dir=None)
        self.root = Path(self.temporary.name)
        registry = self.root / "registry.json"
        registry.write_text('{"schema_version":1,"sources":[]}')
        self.config = {
            "module_root": str(Path(__file__).resolve().parents[1] / "library"), "python": sys.executable,
            "registry": str(registry), "state": str(self.root / "state"), "operations": str(self.root / "operations"),
            "schedule": {"timezone": "Asia/Shanghai", "weekday": 0, "hour": 6, "minute": 30},
            "minimum_free_bytes": 1, "max_runtime_seconds": 30,
        }
        self.now = datetime.fromisoformat("2026-11-02T01:00:00+00:00")
        self.calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def fake_steps(self, *, collection_code=2, fail=None, old_resume=False, targeted_resume=False):
        def child(command, cwd, environment, output, error, timeout):
            name = output.stem
            self.calls.append(name)
            error.write_text("")
            if name in {"collect", "collect_current"}:
                summary = {"status": "partial" if collection_code == 2 else "ok", "resumed": (old_resume or targeted_resume) and name == "collect",
                           "targeted": targeted_resume and name == "collect",
                           "window_end": "2026-10-26T00:00:00+00:00" if old_resume and name == "collect" else self.now.isoformat(),
                           "new_items": 1, "changed_items": 0, "corpus_complete": False}
                output.write_text(json.dumps(summary))
            else:
                output.write_text('{}')
            if name == "export":
                Path(command[command.index("--output") + 1]).write_text('{"items":[]}')
            if name == fail:
                return 1
            return collection_code if name.startswith("collect") else 0
        return child

    def test_due_boundary_before_at_after_monday_and_same_instant_other_timezone(self):
        moments = [
            ("2026-11-01T22:29:59+00:00", "2026-10-26T06:30:00+08:00"),
            ("2026-11-01T22:30:00+00:00", "2026-11-02T06:30:00+08:00"),
            ("2026-11-01T22:30:01+00:00", "2026-11-02T06:30:00+08:00"),
            ("2026-11-01T18:30:00-04:00", "2026-11-02T06:30:00+08:00"),
            ("2026-11-05T12:00:00+00:00", "2026-11-02T06:30:00+08:00"),
        ]
        for instant, expected in moments:
            self.assertEqual(weekly_run.due_period(datetime.fromisoformat(instant), self.config["schedule"]).isoformat(), expected)

    def test_partial_attempt_suppresses_repeat_in_same_period(self):
        with patch.object(weekly_run, "now_utc", return_value=self.now), patch.object(weekly_run, "run_child", side_effect=self.fake_steps()):
            first, code = weekly_run.execute(self.config)
            self.assertEqual((first["status"], code), ("partial", 2))
            count = len(self.calls)
            again, code = weekly_run.execute(self.config)
            self.assertEqual(again["status"], "already_attempted_this_period")
            self.assertEqual(len(self.calls), count)
            dry, _ = weekly_run.execute(self.config, dry_run=True)
            self.assertFalse(dry["due"])

    def test_failed_backup_is_not_completed_and_next_invocation_can_retry(self):
        with patch.object(weekly_run, "now_utc", return_value=self.now), patch.object(weekly_run, "run_child", side_effect=self.fake_steps(fail="backup")):
            failed, code = weekly_run.execute(self.config)
        self.assertEqual((failed["status"], code), ("failed", 3))
        self.assertNotIn("verify_backup", self.calls)
        self.assertEqual(json.loads((self.root / "operations/schedule-state.json").read_text())["status"], "failed")
        with patch.object(weekly_run, "now_utc", return_value=self.now), patch.object(weekly_run, "run_child", side_effect=self.fake_steps(collection_code=0)):
            completed, code = weekly_run.execute(self.config)
        self.assertEqual((completed["status"], code), ("ok", 0))
        self.assertEqual(self.calls.count("collect"), 2)

    def test_failed_backup_verification_cannot_mark_success(self):
        with patch.object(weekly_run, "now_utc", return_value=self.now), patch.object(weekly_run, "run_child", side_effect=self.fake_steps(collection_code=0, fail="verify_backup")):
            result, code = weekly_run.execute(self.config)
        self.assertEqual((result["status"], code), ("failed", 3))
        self.assertIn("verify_backup", result["error"])

    def test_weekly_lock_excludes_overlapping_execution(self):
        operations = Path(self.config["operations"])
        operations.mkdir()
        with (operations / "weekly.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(weekly_run, "now_utc", return_value=self.now), patch.object(weekly_run, "run_child") as child:
                result, code = weekly_run.execute(self.config)
            self.assertEqual((result["status"], code), ("already_running", 0))
            child.assert_not_called()
            self.assertFalse((operations / "schedule-state.json").exists())

    def test_older_resumed_window_is_followed_by_current_period_collection_before_export(self):
        with patch.object(weekly_run, "now_utc", return_value=self.now), patch.object(weekly_run, "run_child", side_effect=self.fake_steps(collection_code=0, old_resume=True)):
            result, code = weekly_run.execute(self.config)
        self.assertEqual((result["status"], code), ("ok", 0))
        self.assertEqual(self.calls[:3], ["collect", "collect_current", "export"])
        self.assertEqual(len(result["collection_summaries"]), 2)
        self.assertFalse(result["collection_summary"]["resumed"])

    def test_current_week_targeted_resume_still_checks_all_sources(self):
        with patch.object(weekly_run, "now_utc", return_value=self.now), patch.object(weekly_run, "run_child", side_effect=self.fake_steps(collection_code=0, targeted_resume=True)):
            result, code = weekly_run.execute(self.config)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[:3], ["collect", "collect_current", "export"])
        self.assertTrue(result["targeted_run_resumed_before_full_check"])
        self.assertFalse(result["older_window_resumed_before_current_check"])

    def test_timeout_stops_owned_process_group_and_reaps_main_child(self):
        # Actual local processes, no network: a nested child writes a heartbeat.
        pid_file, heartbeat = self.root / "pid", self.root / "heartbeat"
        code = "import os,sys,time,subprocess,pathlib; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); subprocess.Popen([sys.executable,'-c',\"import pathlib,sys,time\\np=pathlib.Path(sys.argv[1])\\nwhile True: p.write_text(str(time.monotonic())); time.sleep(.02)\",sys.argv[2]]); time.sleep(30)"
        command = [sys.executable, "-c", code, str(pid_file), str(heartbeat)]
        process_id = None
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                weekly_run.run_child(command, str(self.root), os.environ.copy(), self.root / "stdout", self.root / "stderr", 0.6)
            process_id = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(process_id, 0)
            self.assertTrue(heartbeat.exists(), (self.root / "stderr").read_text())
            last = heartbeat.read_text()
            time.sleep(0.15)
            self.assertEqual(heartbeat.read_text(), last)
        finally:
            if process_id is None and pid_file.exists():
                process_id = int(pid_file.read_text())
            if process_id is not None:
                try:
                    os.killpg(process_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
