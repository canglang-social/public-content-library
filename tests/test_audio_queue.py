"""Queue behavior across edits, interruptions, review decisions and restoration."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from collector.backup import backup, verify_backup
from collector.models import Artifact, Batch, Record
from collector.runner import collect
from collector.store import Store
from media.audio_queue import run_queue


class AudioQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.source = {"id": "publisher", "adapter": "html_article", "url": "https://publisher.example/p/episode", "person_ids": ["person:eligible"], "enabled": True}
        self.registry = {"schema_version": 1, "sources": [self.source]}
        self.registry_path = self.root / "registry.json"
        self.registry_path.write_text(json.dumps(self.registry))
        self.config = {"state": str(self.state), "registry": str(self.registry_path), "minimum_free_bytes": 1,
                       "audio": {"enabled": True, "max_file_bytes": 1000, "max_run_bytes": 1000, "max_seconds": 30}}
        self.body = b"ID3 test audio fixture version one"
        self.result_status = "complete"
        self.calls = 0
        self.capture()

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, text="Publisher summary only", **post_overrides):
        post = {"id": 42, "audience": "everyone", "free_unlock_required": False, "podcast_url": "https://cdn.example/episode.mp3", "podcast_duration": 4, **post_overrides}
        html = '<script id="preloads-json" type="application/json">' + json.dumps({"post": post}) + '</script><article>' + text + '</article>'
        record = Record(canonical_url=self.source["url"], identity_key="publisher:episode", title="A public interview", text=text,
                        review_status="included", kind="article", work_id="work:episode", artifacts=[Artifact(data=html.encode(), media_type="text/html", role="source_html", source_url=self.source["url"])])
        collect(self.registry, self.state, adapter=lambda s, c: Batch(records=[record], coverage={"window_complete": True}), max_artifact_bytes=10_000)
        store = Store(self.state)
        try:
            self.parent = next(i for i in store.export()["items"] if i["identity_key"] == "publisher:episode")
        finally:
            store.db.close()

    def download(self, url, directory, *, public_offer, limits):
        self.calls += 1
        directory.mkdir(parents=True, exist_ok=True)
        if self.result_status in {"deferred", "error"}:
            (directory / "partial.bin").write_bytes(self.body[:8])
            (directory / "manifest.json").write_text(json.dumps({"prefix_sha256": hashlib.sha256(self.body[:8]).hexdigest(), "bytes": 8}))
            return {"status": self.result_status, "errors": ["upstream unavailable"], "budget": {"received_bytes": 8}, "resume": {"retained_bytes": 8}}
        path = directory / (hashlib.sha256(self.body).hexdigest() + ".mp3")
        path.write_bytes(self.body)
        return {"status": self.result_status, "path": str(path), "sha256": hashlib.sha256(self.body).hexdigest(), "bytes": len(self.body),
                "mime_type": "audio/mpeg", "budget": {"received_bytes": len(self.body)}, "resume": {"reused_bytes": 0}}

    def run_audio(self, **kwargs):
        return run_queue(self.config, downloader=self.download, validator=lambda *a, **k: {"duration_seconds": 4}, **kwargs)

    def bundle(self):
        store = Store(self.config["state"])
        try:
            return store.export()
        finally:
            store.db.close()

    def audio(self):
        return next(i for i in self.bundle()["items"] if i["identity_key"].startswith("audio-edition:"))

    def test_complete_and_repeated_audio_share_identity_without_duplicate_versions(self):
        first, code = self.run_audio()
        self.assertEqual((code, first["new_items"], first["new_versions"]), (0, 1, 1))
        item = self.audio()
        self.assertEqual(item["work_id"], self.parent["work_id"])
        self.assertEqual(item["person_ids"], ["person:eligible"])
        self.assertIn("not a full audio transcript", item["versions"][0]["content"]["text"])
        self.result_status = "unchanged"
        repeated, code = self.run_audio()
        self.assertEqual((repeated["new_items"], repeated["new_versions"], code), (0, 0, 0))
        self.assertEqual(self.calls, 2, "completed audio must still be rechecked")

    def test_100mb_admission_and_above_limit_refusal(self):
        self.config["audio"].update(max_file_bytes=100_000_000, max_run_bytes=100_000_000)
        result, code = self.run_audio()
        self.assertEqual(code, 0)
        self.assertEqual(result["limits"]["max_file_bytes"], 100_000_000)
        self.config["audio"]["max_file_bytes"] += 1
        calls = self.calls
        with self.assertRaises(ValueError):
            self.run_audio()
        self.assertEqual(self.calls, calls)

    def test_changed_audio_and_parent_text_keep_old_versions_and_manual_notes(self):
        self.run_audio()
        item = self.audio()
        store = Store(self.state)
        store.annotate(item["id"], notes="My retained note", tags=["listen-later"])
        store.db.close()
        self.body = b"ID3 changed episode bytes"
        self.run_audio()
        self.capture(text="Updated publisher summary")
        self.result_status = "unchanged"
        summary, _ = self.run_audio()
        current = self.audio()
        self.assertEqual(len(current["versions"]), 3)
        self.assertEqual(summary["new_versions"], 1)
        self.assertEqual(current["manual"]["notes"], "My retained note")
        self.assertEqual(current["manual"]["tags"], ["listen-later"])
        self.assertEqual({v["content"]["metadata"]["original_media_sha256"] for v in current["versions"]}.__len__(), 2)

    def test_unavailable_retry_preserves_complete_audio_and_text(self):
        self.run_audio()
        before = self.audio()
        self.result_status = "error"
        result, code = self.run_audio()
        self.assertEqual((result["status"], code, result["pending_count"]), ("partial", 2, 1))
        after = self.audio()
        self.assertEqual(after["latest_complete_version_id"], before["latest_complete_version_id"])
        self.assertEqual(after["availability"], "available")

    def test_pending_queue_is_portable_and_restored_run_is_idempotent(self):
        self.result_status = "deferred"
        self.run_audio()
        destination = self.root / "backup"
        backup(self.state, destination)
        queue_before = (self.state / "audio/queue.json").read_bytes()
        shutil.move(str(self.state), str(self.root / "unavailable-original"))
        self.config["state"] = str(destination)
        self.assertEqual(verify_backup(destination)["status"], "ok")
        self.assertEqual((destination / "audio/queue.json").read_bytes(), queue_before)
        self.assertEqual(len(list((destination / "audio").rglob("partial.bin"))), 1)
        self.result_status = "complete"
        result, _ = self.run_audio()
        self.assertEqual(result["new_items"], 1)
        result, _ = self.run_audio()
        self.assertEqual(result["new_versions"], 0)
        self.assertEqual(backup(destination, self.root / "backup-after-restore")["status"], "ok")

    def test_crash_after_neutral_commit_before_queue_completion_replays_once(self):
        import media.audio_queue as module
        original = module.atomic_json
        def fail_after_commit(path, value):
            if Path(path).name == "queue.json" and any(j.get("status") == "complete" for j in value.get("jobs", {}).values()):
                raise OSError("simulated atomic-write failure after committed audio")
            return original(path, value)
        with patch.object(module, "atomic_json", side_effect=fail_after_commit):
            with self.assertRaises(OSError):
                self.run_audio()
        self.assertEqual(len(self.audio()["versions"]), 1)
        result, _ = self.run_audio()
        self.assertEqual((result["new_items"], result["new_versions"]), (0, 0))
        self.assertEqual(len(self.audio()["versions"]), 1)

    def test_preview_and_nonpublic_offer_never_download(self):
        self.capture(audience="only_paid")
        result, code = self.run_audio()
        self.assertEqual((self.calls, result["eligible_count"], code), (0, 0, 0))
        self.capture(is_editor_preview=True)
        self.run_audio()
        self.assertEqual(self.calls, 0)

    def test_low_space_defers_audio_without_downloader_or_content_mutation(self):
        result, code = self.run_audio(required_free_bytes=10**30)
        self.assertEqual((self.calls, code, result["pending_count"]), (0, 2, 1))
        self.assertEqual(len(self.bundle()["items"]), 1)

    def test_parent_scope_change_propagates_but_audio_manual_override_remains(self):
        self.run_audio()
        audio = self.audio()
        store = Store(self.state)
        store.annotate(self.parent["id"], review_status="excluded")
        store.db.close()
        result, _ = self.run_audio()
        self.assertEqual(self.audio()["review_status"], "excluded")
        self.assertEqual(result["audio_scope_classifications_updated"], 1)
        store = Store(self.state)
        store.annotate(audio["id"], review_status="included", notes="Explicit edition decision")
        store.db.close()
        self.run_audio()
        self.assertEqual(self.audio()["review_status"], "included")
        self.assertEqual(self.audio()["manual"]["notes"], "Explicit edition decision")

    def test_local_integrity_error_is_fatal_and_cannot_publish(self):
        with self.assertRaisesRegex(ValueError, "trusted audio"):
            run_queue(self.config, downloader=lambda *a, **k: {"status": "error", "error_kind": "local_integrity", "errors": ["corrupt prefix"]})
        self.assertEqual(len(self.bundle()["items"]), 1)

    def test_queue_backup_detects_tampered_prefix(self):
        self.result_status = "deferred"
        self.run_audio()
        destination = self.root / "backup"
        backup(self.state, destination)
        prefix = next((destination / "audio").rglob("partial.bin"))
        prefix.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "integrity"):
            verify_backup(destination)


if __name__ == "__main__":
    unittest.main(verbosity=2)
