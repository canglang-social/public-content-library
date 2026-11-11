"""Offline behavior tests; recorded public responses are replayed, not refetched."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from collector.adapters import dispatch, post_record, x_post, x_timeline
from collector.backup import backup, verify_backup
from collector.http import Budget, FetchError, HTTPClient, LimitReached
from collector.models import Artifact, Batch, CollectionContext, Record, Response
from collector.runner import AlreadyRunning, collect, state_lock
from collector.store import Store


NOW = "2026-11-02T00:00:00+00:00"
ROOT = Path(__file__).resolve().parent
SOURCE = {"id": "fixture", "adapter": "text", "url": "https://example.org/article", "review_status": "included", "person_ids": ["person:one"], "work_id": "work:one"}


def registry(*sources):
    return {"schema_version": 1, "persons": [{"id": "person:one"}], "sources": list(sources or [SOURCE])}


def record(**changes):
    values = dict(canonical_url=SOURCE["url"], title="Example", text="Body-only orchestrator evidence.", review_status="included", work_id="work:one", author="Author")
    values.update(changes)
    return Record(**values)


def ok(*records, cursor=None):
    return Batch(list(records), cursor={} if cursor is None else cursor, coverage={"window_complete": True, "corpus_complete": False})


class MemoryClient:
    def __init__(self, routes):
        self.routes = routes
        self.history = []
        self.errors = []
        self.bytes_received = self.request_count = 0

    def get(self, url, **kwargs):
        data = self.routes(url) if callable(self.routes) else self.routes[url]
        if isinstance(data, Exception):
            raise data
        response = Response(data, url, 200, {"content-type": "application/json"}, NOW)
        self.history.append(response)
        self.bytes_received += len(data)
        self.request_count += 1
        return response


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def run_records(self, *records, sources=None):
        return collect(registry(*(sources or [])), self.state, now=NOW, adapter=lambda s, c: ok(*records))

    def export(self):
        s = Store(self.state)
        try:
            return s.export()
        finally:
            s.db.close()

    def test_versions_ignore_observations_but_preserve_changes_and_annotations(self):
        first = self.run_records(record(metadata={"views": 10, "attachment_hash": "a"}))
        item = self.export()["items"][0]
        store = Store(self.state)
        store.annotate(item["id"], notes="My note", tags=["keep"], review_status="included")
        store.db.close()
        repeated = self.run_records(record(metadata={"views": 99, "retrieved_at": "later", "attachment_hash": "a"}, provenance={"retrieved_at": "later"}))
        self.assertEqual(repeated["new_versions"], 0)
        self.assertTrue(repeated["no_updates"])
        self.run_records(record(text="Corrected body-only orchestrator evidence.", metadata={"attachment_hash": "a"}))
        self.run_records(record(text="Corrected body-only orchestrator evidence.", metadata={"attachment_hash": "b"}))
        self.run_records(record(text="Corrected body-only orchestrator evidence.", metadata={"attachment_hash": "b"}, segments=[{"start": 1.2, "text": "Corrected"}]))
        result = self.export()["items"][0]
        self.assertEqual(len(result["versions"]), 4)
        self.assertEqual(result["manual"], {"notes": "My note", "tags": ["keep"], "review_status": "included"})
        self.assertEqual(first["new_items"], 1)

    def test_duplicate_global_identity_and_attribution_preservation(self):
        self.run_records(record(identity_key="same"), record(identity_key="same"))
        other = {**SOURCE, "id": "index", "person_ids": [], "work_id": None}
        self.run_records(record(identity_key="same", work_id=None, author=None, review_status="needs_review"), sources=[other])
        result = self.export()["items"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["work_id"], "work:one")
        self.assertEqual(result[0]["person_ids"], ["person:one"])
        self.assertEqual(result[0]["review_status"], "included")
        self.assertEqual(len(result[0]["versions"]), 1)

    def test_timeline_context_cannot_inherit_owner_affiliation(self):
        source = {**SOURCE, "adapter": "x_timeline", "url": "https://x.com/registered_owner", "options": {"handle": "registered_owner"}, "person_ids": ["person:owner"]}
        tweet = {"id": "123", "text": "A public professional contextual post.", "author": {"screen_name": "different_author"}}
        corrected = post_record(tweet, source)
        self.assertEqual(corrected.person_ids, [])
        self.assertEqual(corrected.review_status, "needs_review")
        self.assertIn("context-author-unverified", corrected.tags)
        # Explicit [] repairs an earlier wrong association; absent override still preserves mappings.
        old = copy.deepcopy(corrected)
        old.person_ids = None
        self.run_records(old, sources=[source])
        self.assertEqual(self.export()["items"][0]["person_ids"], ["person:owner"])
        self.run_records(corrected, sources=[source])
        self.assertEqual(self.export()["items"][0]["person_ids"], [])
        owner = post_record({**tweet, "author": {"screen_name": "registered_owner"}}, source)
        self.assertEqual(owner.person_ids, ["person:owner"])

    def test_missing_x_author_cannot_be_invented_from_requested_account(self):
        source = {**SOURCE, "adapter": "x_timeline", "url": "https://x.com/registered_owner", "options": {"handle": "registered_owner"}, "person_ids": ["person:owner"]}
        records = []
        for index, author in enumerate([None, {}, {"id": "42"}, {"screen_name": ""}, {"screen_name": " "}]):
            with self.subTest(author=author):
                item = post_record({"id": str(index + 1), "text": "Professional text with no verified author.", "author": author}, source)
                self.assertIsNone(item.author)
                self.assertEqual(item.canonical_url, "https://x.com/i/status/" + str(index + 1))
                self.assertEqual(item.person_ids, [])
                self.assertEqual(item.review_status, "needs_review")
                self.assertEqual(item.metadata["attribution_status"], "unverified_missing_author")
                records.append(item)
        self.run_records(*records, sources=[source])
        for item in self.export()["items"]:
            self.assertEqual(item["person_ids"], [])
            self.assertEqual(item["review_status"], "needs_review")

    def test_x_rss_fallback_requires_verified_post_author_before_attribution(self):
        source = {**SOURCE, "adapter": "x_timeline", "url": "https://x.com/registered_owner", "options": {"handle": "registered_owner"}, "person_ids": ["person:owner"]}
        feed = b'''<rss><channel>
          <item><title>Unverified matching URL</title><link>https://x.com/registered_owner/status/111</link><description>Feed excerpt one.</description></item>
          <item><title>Unverified context URL</title><link>https://x.com/another_author/status/222</link><description>Feed excerpt two.</description></item>
          <item><title>Verified owner</title><link>https://x.com/registered_owner/status/333</link><description>Feed excerpt three.</description></item>
        </channel></rss>'''
        def route(url):
            if "/feed.xml" in url:
                return feed
            if url.endswith("/2/status/333"):
                return json.dumps({"id": "333", "text": "Full professional post.", "author": {"screen_name": "registered_owner"}}).encode()
            return FetchError("Fixture timeline/known-post unavailable")
        context = CollectionContext("2026-10-26T00:00:00+00:00", NOW, {}, 3, 1, MemoryClient(route), self.root)
        batch = x_timeline(source, context)
        self.assertEqual(batch.status, "partial")
        self.assertEqual(batch.coverage["route"], "RSS fallback")
        self.assertEqual(len(batch.records), 3)
        for item, original in zip(batch.records[:2], ["registered_owner", "another_author"]):
            self.assertIsNone(item.author)
            self.assertEqual(item.person_ids, [])
            self.assertEqual(item.review_status, "needs_review")
            self.assertEqual(item.status, "truncated")
            self.assertIn("https://x.com/" + original + "/status/", item.provenance["feed_entry_url"])
            self.assertIn("https://x.com/i/status/", item.canonical_url)
            self.assertTrue(item.provenance["discovery_only"])
        verified = batch.records[2]
        self.assertEqual(verified.author, "registered_owner")
        self.assertEqual(verified.person_ids, ["person:owner"])
        self.assertEqual(verified.review_status, "included")
        self.assertNotIn("attribution_status", verified.metadata)
        self.run_records(*batch.records, sources=[source])
        items = self.export()["items"]
        for item in items:
            self.assertEqual(item["person_ids"], ["person:owner"] if item["canonical_url"].endswith("/333") else [])
        self.assertEqual(self.run_records(*batch.records, sources=[source])["new_versions"], 0)

    def test_discovery_cannot_replace_captured_text(self):
        self.run_records(record())
        result = self.run_records(record(text="Short feed excerpt", status="metadata_only", provenance={"discovery_only": True}))
        item = self.export()["items"][0]
        self.assertEqual(item["availability"], "available")
        self.assertEqual(result["new_versions"], 0)
        self.assertIn("orchestrator", item["versions"][0]["content"]["text"])

    def test_failure_is_visible_and_never_deletion_or_no_updates(self):
        self.run_records(record())
        def failed(source, context):
            raise FetchError("HTTP 404", status=404)
        result = collect(registry(), self.state, now=NOW, adapter=failed)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sources"][0]["status"], "unavailable")
        self.assertFalse(result["no_updates"])
        self.assertEqual(self.export()["items"][0]["availability"], "available")

    def test_nonempty_truncation_keeps_last_complete_text_searchable(self):
        self.run_records(record(text="Complete text contains distant-needle beyond the preview."))
        self.run_records(record(text="Complete text", status="truncated"))
        store = Store(self.state)
        try:
            self.assertEqual(len(store.search("distant-needle")), 1)
            store.fts = False
            self.assertEqual(len(store.search("distant-needle")), 1)
            item = store.export()["items"][0]
            self.assertEqual(item["availability"], "truncated")
            self.assertNotEqual(item["latest_version_id"], item["latest_complete_version_id"])
        finally:
            store.db.close()

    def test_partial_cursor_durable_without_watermark_and_current_window_fresh(self):
        observed = []
        def page(source, context):
            observed.append((context.window_end, context.cursor, context.backfill_window_end))
            return Batch([record()], status="partial", cursor={"pending": ["older"]}, coverage={"window_complete": False})
        collect(registry(), self.state, now=NOW, adapter=page)
        later = "2026-11-12T00:00:00+00:00"
        collect(registry(), self.state, now=later, adapter=page)
        source = self.export()["sources"][0]
        self.assertIsNone(source["watermark"])
        self.assertEqual(observed[1], (later, {"pending": ["older"]}, NOW))
        self.assertEqual(source["pending_window"][1], NOW)

    def test_interruption_resume_skips_committed_source(self):
        sources = [SOURCE, {**SOURCE, "id": "second", "url": "https://example.org/two"}]
        calls = []
        def interrupted(source, context):
            calls.append(source["id"])
            if source["id"] == "second":
                raise KeyboardInterrupt("fixture")
            return ok(record())
        with self.assertRaises(KeyboardInterrupt):
            collect(registry(*sources), self.state, now=NOW, adapter=interrupted)
        first_id = self.export()["items"][0]["id"]
        def resumed(source, context):
            calls.append("resume:" + source["id"])
            return ok(record(canonical_url=source["url"]))
        result = collect(registry(*sources), self.state, now="2026-11-12T00:00:00+00:00", adapter=resumed, resume_auto=True)
        self.assertTrue(result["resumed"])
        self.assertEqual(result["window_end"], NOW)
        self.assertEqual(calls, ["fixture", "second", "resume:second"])
        self.assertIn(first_id, [i["id"] for i in self.export()["items"]])

    def test_atomic_source_failure_does_not_commit_earlier_records(self):
        with self.assertRaises(ValueError):
            self.run_records(record(), record(canonical_url="https://example.org/bad", status="deleted"))
        self.assertEqual(self.export()["items"], [])
        self.assertIsNone(self.export()["sources"][0]["watermark"])

    def test_artifact_atomic_publication_and_stale_temporary_recovery(self):
        store = Store(self.state)
        artifact = Artifact(data=b"complete immutable content")
        with patch("collector.store.os.link", side_effect=KeyboardInterrupt("simulated interruption before publication")):
            with self.assertRaises(KeyboardInterrupt):
                store.put_artifact(artifact)
        self.assertEqual(list((self.state / "artifacts").rglob("[0-9a-f]" * 64)), [])
        abandoned = self.state / "artifacts" / "aa" / ".tmp-crashed-process"
        abandoned.parent.mkdir(exist_ok=True)
        abandoned.write_bytes(b"partial")
        with store.transaction():
            saved = store.put_artifact(artifact)
        self.assertEqual(Path(saved["path"]).read_bytes(), artifact.data)
        self.assertEqual(abandoned.read_bytes(), b"partial")
        with store.transaction():
            self.assertEqual(store.put_artifact(artifact)["sha256"], saved["sha256"])
        store.db.close()

    def test_lock_prevents_overlapping_writers(self):
        with state_lock(self.state):
            with self.assertRaises(AlreadyRunning):
                collect(registry(), self.state, now=NOW, adapter=lambda s, c: ok(record()))

    def test_portable_backup_restores_without_original_and_rebases_evidence(self):
        self.run_records(record(artifacts=[Artifact(data=b"raw source", source_url=SOURCE["url"])]))
        item = self.export()["items"][0]
        store = Store(self.state)
        store.annotate(item["id"], tags=["retained"], notes="My independent note")
        store.db.close()
        destination = self.root / "backup"
        self.assertEqual(backup(self.state, destination)["status"], "ok")
        self.assertEqual(verify_backup(destination)["database_integrity"], "ok")
        hidden = self.root / "unavailable-original"
        self.state.rename(hidden)
        try:
            restored = Store(destination)
            result = restored.export()
            self.assertEqual(len(restored.search("orchestrator")), 1)
            self.assertEqual(result["items"][0]["manual"]["notes"], "My independent note")
            self.assertEqual(result["items"][0]["id"], item["id"])
            for version in result["items"][0]["versions"]:
                for file in [version["text_path"], version["payload_path"], *(a["path"] for a in version["artifacts"])]:
                    self.assertTrue(Path(file).is_file())
                    self.assertTrue(Path(file).resolve().is_relative_to(destination.resolve()))
            restored.db.close()
        finally:
            hidden.rename(self.state)

    def test_local_item_limit_cannot_trust_provider_count(self):
        records = [record(canonical_url=f"https://example.org/{i}") for i in range(5)]
        result = collect(registry(), self.state, now=NOW, max_items=2, adapter=lambda s, c: ok(*records, cursor={"unsafe": "advanced"}))
        self.assertEqual(len(self.export()["items"]), 2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.export()["sources"][0]["cursor"], {})
        self.assertIsNone(self.export()["sources"][0]["watermark"])

    def test_synthetic_x_route_dedup_and_local_timeline_limit(self):
        raw = ROOT / "fixtures"
        source = {"id": "x", "adapter": "x_post", "url": "https://x.com/example_member/status/4001", "review_status": "included"}
        data = (raw / "x-post.json").read_bytes()
        factory = lambda *a, **kw: MemoryClient(lambda url: data)
        first = collect(registry(source), self.state, now=NOW, client_factory=factory)
        second = collect(registry(source), self.state, now=NOW, client_factory=factory)
        self.assertEqual((first["new_items"], second["new_versions"]), (1, 0))
        post = self.export()["items"][0]
        self.assertEqual(post["review_status"], "included")
        self.assertEqual(len(post["versions"][0]["content"]["text"]), 473)
        source = {"id": "timeline", "adapter": "x_timeline", "url": "https://x.com/ExampleLab", "review_status": "included"}
        timeline = (raw / "x-timeline.json").read_bytes()
        client = MemoryClient(lambda url: timeline if "cursor=" not in url else b'{"code":200,"results":[],"cursor":{}}')
        context = CollectionContext("2026-10-22T00:00:00+00:00", NOW, {}, 2, 1, client, self.root)
        batch = x_timeline(source, context)
        self.assertEqual(len(batch.records), 2)
        self.assertGreater(batch.coverage["provider_returned"], 2)
        self.assertIsNone(batch.coverage["window_complete"])
        seen = {r.identity_key for r in batch.records}
        for _ in range(12):
            context.cursor = batch.cursor
            batch = x_timeline(source, context)
            seen.update(r.identity_key for r in batch.records)
        expected = {"x:" + str(t["id"]) for t in json.loads(timeline)["results"]}
        self.assertEqual(seen, expected)

    def test_x_rejects_response_for_another_id(self):
        client = MemoryClient(lambda url: b'{"code":200,"status":{"id":"999","text":"wrong"}}')
        context = CollectionContext(NOW, NOW, {}, 1, 1, client, self.root)
        with self.assertRaises(FetchError):
            x_post({"url": "https://x.com/a/status/123"}, context)

    def test_http_retries_are_bounded_and_byte_cap_is_local(self):
        calls = []
        def fail(request, **kwargs):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 429, "busy", {}, None)
        client = HTTPClient(Budget(), opener=fail, attempts=2, backoff=0)
        with self.assertRaises(FetchError):
            client.get("https://example.org/a")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("Cookie", calls[0].headers)
        class Reply(io.BytesIO):
            status = 200
            headers = {}
            def geturl(self): return "https://example.org/a"
        client = HTTPClient(Budget(), opener=lambda *a, **kw: Reply(b"1234567890"), max_bytes=5)
        with self.assertRaises(LimitReached):
            client.get("https://example.org/a")
        self.assertEqual(client.history, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
