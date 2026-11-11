"""Regression checks for timing changes, transcript fidelity, and resumable discovery."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "library"))
from collector.models import Batch, CollectionContext, Record, Response
from media.adapters import collect, feed_candidates
from media.extractors import article_candidates, content_fingerprints, extract_publisher, parse_vtt

ROOT = Path(__file__).resolve().parent


class FakeClient:
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def get(self, url, **kwargs):
        self.requests.append(url)
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return Response(value.encode(), url, 200, {"content-type": "text/html"}, "2026-11-02T00:00:00Z")


class MediaRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.context = CollectionContext("2026-10-26T00:00:00Z", "2026-11-02T00:00:00Z", {}, 1, 3, None, Path(self.tmp.name), max_seconds=30)

    def tearDown(self):
        self.tmp.cleanup()

    def test_public_newsletter_guard_rejects_paid_unknown_and_preview_pages(self):
        source = {"id": "public-newsletter", "adapter": "html_article", "url": "https://example.substack.com/p/post", "options": {"body_selectors": [".body.markup"], "min_text_characters": 5, "require_substack_public_audience": True}}
        def page(post, extra=""):
            return '<h1>Article</h1><script id="preloads-json" type="application/json">' + json.dumps({"post": post}) + '</script><div class="body markup">Public professional article text long enough to resemble a valid preview.</div>' + extra
        public = {"audience": "everyone", "free_unlock_required": False, "truncated_body_text": "This also exists on fully public posts."}
        good = collect(source, replace(self.context, client=FakeClient({source["url"]: page(public)})))
        self.assertEqual(good.status, "ok")
        self.assertEqual(good.records[0].metadata["access_check"]["publisher_audience"], "everyone")
        for post, extra in [({**public, "audience": "only_paid"}, ""), ({}, ""), ({**public, "free_unlock_required": True}, ""), ({**public, "is_editor_preview": True}, ""), (public, '<div class="paywall">Subscribe to keep reading</div>')]:
            with self.subTest(post=post, extra=extra):
                blocked = collect(source, replace(self.context, client=FakeClient({source["url"]: page(post, extra)})))
                self.assertEqual(blocked.status, "unavailable")
                self.assertEqual(blocked.records, [])
                self.assertIn("complete capture not asserted", blocked.errors[0])

    def test_configured_title_avoids_forum_vote_score_and_rejects_layout_drift(self):
        source = {"id": "forum", "adapter": "html_article", "url": "https://example.org/posts/post", "options": {"title_selector": "h1.post-title", "body_selectors": [".post-body"], "min_text_characters": 5}}
        page = '<h1>14</h1><h1 class="post-title">How I choose research projects</h1><div class="post-body">Detailed research discussion.</div>'
        good = collect(source, replace(self.context, client=FakeClient({source["url"]: page})))
        self.assertEqual(good.records[0].title, "How I choose research projects")
        missing = collect(source, replace(self.context, client=FakeClient({source["url"]: page.replace('class="post-title"', 'class="new-layout-title"')})))
        self.assertEqual(missing.status, "unavailable")
        self.assertEqual(missing.records, [])

    def test_timing_only_and_metadata_only_changes_have_distinct_fingerprints(self):
        a = content_fingerprints("same words", [{"start": 1, "end": 2, "text": "same words"}], {"title": "A"})
        b = content_fingerprints("same words", [{"start": 1.5, "end": 2.5, "text": "same words"}], {"title": "A"})
        c = content_fingerprints("same words", [{"start": 1, "end": 2, "text": "same words"}], {"title": "Corrected title"})
        self.assertEqual(a["text_sha256"], b["text_sha256"])
        self.assertNotEqual(a["combined_sha256"], b["combined_sha256"])
        self.assertNotEqual(a["combined_sha256"], c["combined_sha256"])

    def test_vtt_repeated_speech_and_entities_survive(self):
        rows = parse_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nYes&nbsp;&amp; yes\n\n00:00:02.000 --> 00:00:03.000\nYes&nbsp;&amp; yes\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["text"], "Yes\u00a0& yes")
        self.assertEqual(rows[0]["text"], rows[1]["text"])
        self.assertEqual(rows[1]["start"], 2)

    def test_publisher_transcript_excludes_footer_and_handles_en_dash(self):
        raw = '<html><h1>Interview</h1><div class="body markup"><p>Transcript</p><p>Speaker One</p><p>(00:00:01 – 00:00:03):</p><p>Useful professional speech.</p><p>(00:00:03) - Next chapter</p><p>Speaker Two</p><p>(00:00:04 - 00:00:05):</p><p>Thank you.</p></div><footer>Subscribe and comments</footer></html>'
        data = extract_publisher(raw.encode(), {"url": "https://example.org/interview", "options": {"min_text_characters": 10}})
        self.assertEqual(len(data["segments"]), 2)
        self.assertEqual(data["segments"][0]["speaker"], "Speaker One")
        self.assertEqual(data["segments"][1]["start"], 4)
        self.assertNotIn("Subscribe", data["text"])
        self.assertEqual(data["metadata"]["generation_method"], "unspecified_by_publisher")

    def test_capped_discovery_resumes_untouched_candidate(self):
        source = {"id": "index", "url": "https://example.org/posts", "adapter": "html_index", "review_status": "included", "options": {"allowed_path_prefixes": ["/posts/"], "min_text_characters": 5}}
        client = FakeClient({source["url"]: '<a href="/posts/a">A</a><a href="/posts/b">B</a>',
                             "https://example.org/posts/a": "<article><h1>A</h1><p>First article body.</p></article>",
                             "https://example.org/posts/b": "<article><h1>B</h1><p>Second article body.</p></article>"})
        first = collect(source, replace(self.context, client=client))
        self.assertEqual([r.title for r in first.records], ["A"])
        self.assertEqual(first.coverage["pending_count"], 1)
        second = collect(source, replace(self.context, client=client, cursor=first.cursor))
        self.assertEqual([r.title for r in second.records], ["B"])
        self.assertEqual(second.cursor["pending_urls"], [])
        self.assertEqual(client.requests.count(source["url"]), 2)
        self.assertFalse(second.coverage["corpus_complete"])

    def test_fresh_discovery_has_a_slot_while_old_backfill_remains(self):
        source = {"id": "index", "url": "https://example.org/posts", "adapter": "html_index", "options": {"allowed_path_prefixes": ["/posts/"], "min_text_characters": 5}}
        client = FakeClient({source["url"]: '<a href="/posts/a">A</a><a href="/posts/b">B</a><a href="/posts/d">D</a>',
                             **{"https://example.org/posts/" + key.lower(): f"<article><h1>{key}</h1><p>Professional article body.</p></article>" for key in "ABCD"}})
        first = collect(source, replace(self.context, client=client))
        client.routes[source["url"]] = '<a href="/posts/c">C</a><a href="/posts/a">A</a><a href="/posts/b">B</a><a href="/posts/d">D</a>'
        second = collect(source, replace(self.context, client=client, cursor=first.cursor, max_items=2))
        self.assertEqual([r.title for r in second.records], ["C", "B"])
        self.assertEqual(second.coverage["new_items_processed"], 1)
        self.assertEqual(second.coverage["backfill_items_processed"], 1)
        self.assertEqual([r["title"] for r in second.cursor["pending_urls"]], ["D"])

    def test_failed_candidate_rotates_without_losing_it(self):
        source = {"id": "index", "url": "https://example.org/posts", "adapter": "html_index", "options": {"allowed_path_prefixes": ["/posts/"], "min_text_characters": 5}}
        client = FakeClient({source["url"]: '<a href="/posts/a">A</a><a href="/posts/b">B</a>',
                             "https://example.org/posts/a": OSError("temporary offline"),
                             "https://example.org/posts/b": "<article><h1>B</h1><p>Second article body.</p></article>"})
        first = collect(source, replace(self.context, client=client))
        self.assertEqual(first.cursor["pending_urls"][0]["url"], "https://example.org/posts/b")
        second = collect(source, replace(self.context, client=client, cursor=first.cursor))
        self.assertEqual([r.title for r in second.records], ["B"])
        self.assertEqual(second.cursor["pending_urls"][0]["url"], "https://example.org/posts/a")
        self.assertTrue(second.coverage["pending_count"])

    def test_discovery_children_share_source_deadline_and_keep_unattempted_urls(self):
        source = {"id": "index", "url": "https://example.org/posts", "adapter": "html_index", "options": {"allowed_path_prefixes": ["/posts/"]}}
        client = FakeClient({source["url"]: '<a href="/posts/a">A</a><a href="/posts/b">B</a>'})
        clock = [100.0]
        context = replace(self.context, client=client, max_items=2, max_seconds=10, started_monotonic=100.0)

        def slow_article(child, subcontext):
            self.assertEqual(subcontext.started_monotonic, 100.0)
            self.assertEqual(subcontext.max_seconds, 10)
            clock[0] = 108.0
            return Batch(records=[Record(canonical_url=child["url"], title=child["title"], text="Professional article")])

        with patch("collector.models.time.monotonic", side_effect=lambda: clock[0]), patch("media.adapters.article", side_effect=slow_article):
            batch = collect(source, context)
        self.assertEqual([record.title for record in batch.records], ["A"])
        self.assertEqual([row["title"] for row in batch.cursor["pending_urls"]], ["B"])
        self.assertEqual(batch.status, "partial")

    def test_expired_source_starts_no_network_or_subprocess_work(self):
        client = FakeClient({})
        context = replace(self.context, client=client, started_monotonic=time.monotonic() - 31)
        batch = collect({"adapter": "youtube_video", "url": "https://www.youtube.com/watch?v=om2lIWXLLN4"}, context)
        self.assertEqual(batch.status, "partial")
        self.assertIn("time budget", batch.errors[0])
        self.assertEqual(client.requests, [])

    def test_interrupted_youtube_probe_reaps_its_owned_group(self):
        from unittest.mock import Mock
        import signal
        process = Mock(pid=123456)
        process.poll.return_value = None
        process.communicate.side_effect = [KeyboardInterrupt(), ("terminated", "")]
        source = {"adapter": "youtube_video", "url": "https://www.youtube.com/watch?v=om2lIWXLLN4"}
        with patch("media.adapters.subprocess.Popen", return_value=process), patch("media.adapters.os.killpg") as terminate:
            with self.assertRaises(KeyboardInterrupt):
                collect(source, self.context)
        terminate.assert_called_once_with(123456, signal.SIGTERM)
        self.assertEqual(process.communicate.call_count, 2)

    def test_discovery_rejects_unregistered_host_and_section(self):
        body = b'<a href="/posts/good">Good</a><a href="https://other.test/posts/bad">Other</a><a href="/personal/no">Personal</a><a href="http://127.0.0.1/posts/no">Local</a>'
        rows = article_candidates(body, {"url": "https://example.org/posts", "options": {"allowed_path_prefixes": ["/posts/"]}})
        self.assertEqual([r["url"] for r in rows], ["https://example.org/posts/good"])

    def test_new_path_policy_retires_category_backlog_without_losing_evidence(self):
        source = {"id": "index", "url": "https://example.org/research", "adapter": "html_index", "options": {
            "allowed_path_prefixes": ["/research/"], "excluded_path_prefixes": ["/research/team/"], "min_text_characters": 5}}
        category = {"url": "https://example.org/research/team/alignment", "title": "Alignment"}
        client = FakeClient({source["url"]: '<a href="/research/team/alignment">Alignment</a><a href="/research/good">Good</a>',
                             "https://example.org/research/good": "<article><h1>Good</h1><p>Research article body.</p></article>"})
        batch = collect(source, replace(self.context, client=client, cursor={"pending_urls": [category]}))
        self.assertEqual([r.title for r in batch.records], ["Good"])
        self.assertEqual(batch.cursor["pending_urls"], [])
        self.assertEqual(batch.cursor["excluded_candidates"][0]["url"], category["url"])
        self.assertNotIn(category["url"], client.requests)

    def test_youtube_feed_header_prefix_alias_requires_matching_entry_and_author(self):
        source = {"options": {"channel_id": "UCrDwWp7EBBv4NwvScIpBDOA"}}
        feed = '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><yt:channelId>rDwWp7EBBv4NwvScIpBDOA</yt:channelId><author><uri>https://www.youtube.com/channel/UCrDwWp7EBBv4NwvScIpBDOA</uri></author><entry><yt:channelId>UCrDwWp7EBBv4NwvScIpBDOA</yt:channelId><yt:videoId>om2lIWXLLN4</yt:videoId><title>Official video</title></entry></feed>'
        self.assertEqual(len(feed_candidates(feed.encode(), source)), 1)
        with self.assertRaisesRegex(ValueError, "entry channel"):
            feed_candidates(feed.replace('<entry><yt:channelId>UCrDwWp7EBBv4NwvScIpBDOA', '<entry><yt:channelId>UC_OTHER_CHANNEL').encode(), source)
        with self.assertRaisesRegex(ValueError, "author URI"):
            feed_candidates(feed.replace('/channel/UCrDwWp7EBBv4NwvScIpBDOA', '/channel/UC_OTHER_CHANNEL').encode(), source)

    def test_synthetic_index_and_channel_feed_yield_only_article_candidates(self):
        source = {"url": "https://example.org/research", "options": {"link_selector":"a[href]", "allowed_hosts":["example.org"], "allowed_path_prefixes":["/research/"], "excluded_path_prefixes":["/research/team/"]}}
        raw = '<a href="/research/a">A</a><a href="/research/b">B</a><a href="/research/team/person">Person</a><a href="/announcements">News</a>'
        rows = article_candidates(raw.encode(), source)
        self.assertEqual({r["url"] for r in rows}, {"https://example.org/research/a", "https://example.org/research/b"})
        feed = '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><yt:videoId>abcdefghijk</yt:videoId><yt:channelId>example-channel</yt:channelId><title>Synthetic video</title><link href="https://www.youtube.com/watch?v=abcdefghijk"/></entry></feed>'
        rows = feed_candidates(feed.encode(), {"url":"https://www.youtube.com/feeds/videos.xml?channel_id=example-channel", "options":{}})
        self.assertEqual(len(rows), 1)

    def test_first_party_canonical_and_www_aliases_share_identity(self):
        source = {"adapter": "html_article", "url": "https://example.org/old", "options": {"min_text_characters": 5}}
        client = FakeClient({source["url"]: '<html><head><link rel="canonical" href="https://www.example.org/current"></head><body><article><h1>Title</h1><p>Professional body.</p></article></body></html>'})
        record = collect(source, replace(self.context, client=client)).records[0]
        self.assertEqual(record.canonical_url, "https://www.example.org/current")
        candidates = article_candidates(b'<a href="https://example.org/posts/a">A</a><a href="https://www.example.org/posts/a">A again</a>',
                                        {"url": "https://www.example.org/posts", "options": {"allowed_hosts": ["example.org", "www.example.org"], "allowed_path_prefixes": ["/posts/"]}})
        self.assertEqual(len(candidates), 1)

    def test_interactive_ascii_shell_is_not_a_full_article(self):
        url = "https://example.org/feature"
        client = FakeClient({url: "<main><h1>" + "█" * 700 + "</h1><button>Read as Article</button></main>"})
        batch = collect({"adapter": "html_article", "url": url}, replace(self.context, client=client))
        self.assertEqual(batch.status, "unavailable")
        self.assertIn("readable prose", batch.errors[0])

    def test_unavailable_read_never_claims_deletion(self):
        client = FakeClient({"https://example.org/a": OSError("offline")})
        batch = collect({"adapter": "html_article", "url": "https://example.org/a"}, replace(self.context, client=client))
        self.assertEqual(batch.status, "unavailable")
        self.assertEqual(batch.records, [])
        self.assertFalse(batch.coverage["window_complete"])

    def test_local_asr_rejects_oversized_input_before_processing(self):
        path = Path(self.tmp.name) / "too-large.bin"
        path.write_bytes(b"x" * 200)
        source = {"adapter": "local_asr_excerpt", "url": "https://example.org/public-audio", "options": {
            "audio_is_public": True, "local_audio_path": str(path), "source_offset_seconds": 0, "max_audio_bytes": 100}}
        batch = collect(source, self.context)
        self.assertEqual(batch.status, "unavailable")
        self.assertIn("byte cap", batch.errors[0])
        self.assertEqual(batch.records, [])

    def test_synthetic_publisher_text_and_timestamp_styles_preserved(self):
        raw = '<h1>Synthetic interview</h1><div class="body markup"><p>Transcript</p><p>Speaker One</p><p>(00:00:01 – 00:00:03):</p><p>First synthetic statement.</p><p>Speaker Two</p><p>(00:00:04 - 00:00:06):</p><p>Second synthetic statement.</p></div><footer>Unrelated footer</footer>'
        data = extract_publisher(raw.encode(), {"url":"https://example.org/interview", "options":{"min_text_characters": 50}})
        self.assertEqual(len(data["segments"]), 2)
        self.assertEqual({s["speaker"] for s in data["segments"]}, {"Speaker One", "Speaker Two"})
        self.assertTrue(all(s["text"] and s["end"] >= s["start"] for s in data["segments"]))
        self.assertNotIn("Unrelated footer", data["text"])



if __name__ == "__main__":
    unittest.main(verbosity=2)
