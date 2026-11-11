"""Targeted refreshes retain the registry and cannot silently widen on resume."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector.models import Batch, Record
from collector.runner import collect
from collector.store import Store


class TargetedRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.registry = {"schema_version": 1, "persons": [{"id": "person:one"}],
                         "sources": [{"id": key, "adapter": "text", "url": "https://example.org/" + key,
                                      "person_ids": ["person:one"]} for key in ("one", "two", "three")]}
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def adapter(self, source, context):
        self.calls.append(source["id"])
        return Batch(records=[Record(canonical_url=source["url"], title=source["id"], text="Captured body")],
                     coverage={"window_complete": True})

    def test_selected_sources_only_full_registry_and_untouched_watermark(self):
        result = collect(self.registry, self.state, source_ids=["two", "two"], adapter=self.adapter)
        self.assertEqual(self.calls, ["two"])
        self.assertEqual(result["selected_source_ids"], ["two"])
        self.assertTrue(result["targeted"])
        store = Store(self.state)
        try:
            snapshot = store.db.execute("SELECT value FROM settings WHERE key='registry_path'").fetchone()[0]
            self.assertEqual(json.loads(store.resolve_artifact(snapshot).read_text()), self.registry)
            self.assertIsNone(store.source_state("one")["watermark"])
            self.assertIsNotNone(store.source_state("two")["watermark"])
        finally:
            store.db.close()

    def test_unknown_disabled_and_empty_selections_refused(self):
        self.registry["sources"][2]["enabled"] = False
        for selection in (["missing"], ["three"], []):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                collect(self.registry, self.state, source_ids=selection, adapter=self.adapter)
        self.assertEqual(self.calls, [])

    def test_resume_keeps_original_selection_and_registry(self):
        def interrupt(source, context):
            if source["id"] == "three":
                raise KeyboardInterrupt()
            return self.adapter(source, context)
        with self.assertRaises(KeyboardInterrupt):
            collect(self.registry, self.state, source_ids=["two", "three"], adapter=interrupt)
        edited = copy.deepcopy(self.registry)
        edited["sources"][2]["url"] = "https://example.org/changed"
        result = collect(edited, self.state, resume_auto=True, adapter=self.adapter)
        self.assertTrue(result["resumed"])
        self.assertTrue(result["targeted"])
        self.assertEqual(self.calls, ["two", "three"])
        self.assertEqual(result["selected_source_ids"], ["three", "two"])
        store = Store(self.state)
        try:
            self.assertEqual({i["canonical_url"] for i in store.export()["items"]},
                             {"https://example.org/two", "https://example.org/three"})
        finally:
            store.db.close()

    def test_resume_rejects_different_selection_before_progress(self):
        with self.assertRaises(KeyboardInterrupt):
            collect(self.registry, self.state, source_ids=["two"], adapter=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(ValueError):
            collect(self.registry, self.state, resume_auto=True, source_ids=["one"], adapter=self.adapter)
        result = collect(self.registry, self.state, resume_auto=True, adapter=self.adapter)
        self.assertEqual(self.calls, ["two"])
        self.assertEqual(result["selected_source_ids"], ["two"])

    def test_default_still_checks_all_enabled_sources(self):
        result = collect(self.registry, self.state, adapter=self.adapter)
        self.assertEqual(self.calls, ["one", "two", "three"])
        self.assertFalse(result["targeted"])
        self.assertIsNone(result["selected_source_ids"])

    def test_crash_immediately_after_start_commit_keeps_selection(self):
        original = Store.start_run
        def crash_after_commit(store, *args, **kwargs):
            original(store, *args, **kwargs)
            raise KeyboardInterrupt()
        with patch.object(Store, "start_run", crash_after_commit), self.assertRaises(KeyboardInterrupt):
            collect(self.registry, self.state, source_ids=["two"], adapter=self.adapter)
        result = collect(self.registry, self.state, resume_auto=True, adapter=self.adapter)
        self.assertEqual(self.calls, ["two"])
        self.assertTrue(result["targeted"])


if __name__ == "__main__":
    unittest.main()
