"""Mocked launchctl recovery checks; never install, remove, or signal real jobs."""
import contextlib
import io
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from ops import manage_schedule as manager


def absent(label=manager.LABEL):
    return {"exit_code": 113, "stdout": "", "stderr": f'Could not find service "{label}" in domain for user gui: 501\n'}


def loaded(config_path, script="/trial/module/ops/weekly_run.py"):
    return {"exit_code": 0, "stderr": "", "stdout": f'{manager.LABEL} = {{\n\targuments = {{\n\t\t/trial/python\n\t\t{script}\n\t\t--config\n\t\t{config_path}\n\t}}\n}}\n'}


class JobIdentityTests(unittest.TestCase):
    def test_exact_absent_label_is_recognized(self):
        with patch.object(manager, "command", return_value=absent()):
            self.assertEqual(manager.inspect_job("gui/501/" + manager.LABEL)[0], "absent")

    def test_label_prefix_collision_is_not_confirmed_absent(self):
        with patch.object(manager, "command", return_value=absent(manager.LABEL + "-other")):
            with self.assertRaises(RuntimeError):
                manager.inspect_job("gui/501/" + manager.LABEL)

    def test_live_config_argument_must_match_exactly(self):
        config = Path("/trial/config.json")
        self.assertTrue(manager.loaded_is_owned(loaded(config), config))
        self.assertFalse(manager.loaded_is_owned(loaded(str(config) + ".backup"), config))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="schedule-control-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.operations = self.root / "operations"
        self.config = self.root / "config.json"
        # Deliberately invalid installation/timezone: recovery must still work.
        self.config.write_text(json.dumps({"operations": str(self.operations), "python": "/missing/python", "registry": "/missing/registry", "schedule": {"timezone": "Missing/Zone"}}))
        self.plist = self.home / "Library/LaunchAgents" / (manager.LABEL + ".plist")

    def invoke(self, action, responses):
        with patch.object(manager.sys, "argv", ["manage_schedule.py", action, "--config", str(self.config)]), patch.object(manager.Path, "home", return_value=self.home), patch.object(manager, "command", side_effect=responses) as command, patch.object(manager, "definition", side_effect=AssertionError("Recovery must not validate installation")), contextlib.redirect_stdout(io.StringIO()):
            result = manager.main()
        return result, command.call_args_list

    def write_owned_plist(self):
        self.plist.parent.mkdir(parents=True)
        value = {"Label": manager.LABEL, "ProgramArguments": ["/missing/python", "/trial/module/ops/weekly_run.py", "--config", str(self.config)]}
        self.plist.write_bytes(plistlib.dumps(value))
        return self.plist.read_bytes()

    def test_status_survives_missing_config_and_installation(self):
        self.config.unlink()
        result, calls = self.invoke("status", [absent(), absent()])
        self.assertEqual(result, 0)
        self.assertTrue(all(call.args[1] == "print" for call in calls))

    def test_loaded_owned_job_without_plist_can_be_removed(self):
        result, calls = self.invoke("remove", [loaded(self.config), {"exit_code": 0, "stdout": "", "stderr": ""}, absent(), absent()])
        self.assertEqual(result, 0)
        self.assertEqual(sum(call.args[1] == "bootout" for call in calls), 1)
        self.assertFalse(json.loads((self.operations / "schedule-status.json").read_text())["loaded"])

    def test_unknown_inspection_preserves_installed_definition(self):
        original = self.write_owned_plist()
        with self.assertRaises(RuntimeError):
            self.invoke("remove", [{"exit_code": 1, "stdout": "", "stderr": "Operation not permitted"}])
        self.assertEqual(self.plist.read_bytes(), original)

    def test_still_loaded_after_bootout_preserves_definition(self):
        original = self.write_owned_plist()
        with self.assertRaises(RuntimeError):
            self.invoke("remove", [loaded(self.config), {"exit_code": 1, "stdout": "", "stderr": "failed"}, loaded(self.config)])
        self.assertEqual(self.plist.read_bytes(), original)

    def test_foreign_live_job_with_config_prefix_is_preserved(self):
        with self.assertRaises(ValueError):
            self.invoke("remove", [loaded(str(self.config) + ".backup")])
        self.assertFalse(self.plist.exists())


if __name__ == "__main__":
    unittest.main()
