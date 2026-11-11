"""Offline boundary tests; synthetic bytes test transfer integrity, not decoding."""
from dataclasses import replace
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "library"))
from media import audio_downloads as audio

SOURCE = "https://publisher.example/episode.mp3"
DATA = b"ID3\x04\x00\x00\x00\x00\x00\x00" + bytes(range(118))
OFFER = {"publisher_page_url": "https://publisher.example/episode", "parent_item_id": "fixture:episode",
         "evidence_sha256": "1" * 64, "offer_kind": "publisher_download", "public_access": True}
LIMITS = audio.DownloadLimits(max_file_bytes=1024, max_run_bytes=1024, max_seconds=10,
                              max_requests=32, chunk_bytes=64, io_timeout=2)


def resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def mp3_bytes(tag_bytes=None):
    # MPEG-1 Layer III, 96 kbps, 44.1 kHz: exactly 313 bytes per frame.
    # Synthetic silent payload tests framing only; it is not a listening fixture.
    frame = bytes.fromhex("fffb70c0") + bytes(309)
    prefix = b""
    if tag_bytes is not None:
        size = bytes((tag_bytes >> shift) & 127 for shift in (21, 14, 7, 0))
        prefix = b"ID3\x04\x00\x00" + size + bytes(tag_bytes)
    return prefix + frame * 3


class Clock:
    def __init__(self):
        self.value = 0
    def __call__(self):
        return self.value
    def advance(self, amount):
        self.value += amount


class Response:
    def __init__(self, status=200, headers=None, body=b"", *, fail_after=None, failure=None, clock=None):
        self.status, self.headers, self.body = status, headers or {}, body
        self.offset, self.reads, self.closed = 0, 0, False
        self.fail_after, self.failure, self.clock = fail_after, failure, clock
        self.timeouts = []
    def set_timeout(self, value):
        self.timeouts.append(value)
    def read(self, size):
        self.reads += 1
        if self.fail_after is not None and self.offset >= self.fail_after:
            raise self.failure or TimeoutError("synthetic interrupted connection")
        if self.clock:
            self.clock.advance(1)
        take = min(size, 32, len(self.body) - self.offset)
        if self.fail_after is not None:
            take = min(take, self.fail_after - self.offset)
        block = self.body[self.offset:self.offset + take]
        self.offset += len(block)
        return block
    def close(self):
        self.closed = True


class Server:
    def __init__(self, body=DATA, etag='"version-1"'):
        self.body, self.etag, self.calls, self.responses = body, etag, [], []
        self.conditional = True
        self.ignore_range = False
        self.unknown_length = False
        self.head_status = 200
        self.mutate_response = None
        self.fail_once = None
    def open(self, request, *, timeout):
        self.calls.append(request)
        headers = {"Content-Type": "audio/mpeg", "ETag": self.etag,
                   "Content-Length": str(len(self.body)), "Accept-Ranges": "bytes"}
        if self.unknown_length:
            headers.pop("Content-Length")
        if request.method == "HEAD":
            status = 304 if self.conditional and request.headers.get("If-None-Match") == self.etag else self.head_status
            response = Response(status, headers)
        else:
            value = request.headers.get("Range")
            if value and not self.ignore_range:
                start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", value).groups())
                headers.update({"Content-Range": f"bytes {start}-{end}/{len(self.body)}", "Content-Length": str(end - start + 1)})
                response = Response(206, headers, self.body[start:end + 1])
            else:
                response = Response(200, headers, self.body)
            if self.fail_once is not None:
                response.fail_after, response.failure = self.fail_once
                self.fail_once = None
        if self.mutate_response:
            self.mutate_response(request, response)
        self.responses.append(response)
        return response


class AudioDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / "job"
        self.clock = Clock()
    def tearDown(self):
        self.temp.cleanup()
    def run_download(self, server, **kwargs):
        return audio.download_audio(SOURCE, self.folder, public_offer=OFFER, opener=server,
                                    resolver=kwargs.pop("resolver", resolver), clock=self.clock,
                                    limits=kwargs.pop("limits", LIMITS), **kwargs)
    def seed(self, size=64, *, final_url=SOURCE, etag='"version-1"', payload=DATA):
        path = self.root / "verified-prefix.mp3"
        path.write_bytes(payload[:size])
        return audio.seed_partial(SOURCE, self.folder, path, public_offer=OFFER, final_url=final_url,
                                  etag=etag, total_bytes=len(payload), mime_type="audio/mpeg",
                                  expected_sha256=hashlib.sha256(payload[:size]).hexdigest(), limits=LIMITS)
    def manifest(self):
        return json.loads((self.folder / "manifest.json").read_text())
    def assert_complete(self, result, data=DATA):
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["bytes"], len(data))
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(Path(result["path"]).read_bytes(), data)
        self.assertIsNone(self.manifest()["partial"])

    def test_complete_then_conditional_rerun_uses_zero_payload_bytes(self):
        server = Server()
        first = self.run_download(server)
        self.assert_complete(first)
        self.assertEqual(first["budget"]["received_bytes"], len(DATA))
        self.assertEqual([r.headers.get("Range") for r in server.calls[1:]], ["bytes=0-63", "bytes=64-127"])
        server.calls.clear()
        second = self.run_download(server)
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["budget"]["received_bytes"], 0)
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(server.calls[0].headers["If-None-Match"], '"version-1"')

    def test_conditional_ignored_head_with_same_strong_entity_is_unchanged(self):
        server = Server()
        self.assert_complete(self.run_download(server))
        server.conditional = False
        result = self.run_download(server)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["budget"]["requests"], 1)

    def test_same_url_changed_entity_preserves_old_complete_bytes(self):
        server = Server()
        first = self.run_download(server)
        server.body, server.etag = DATA + b"new edition", '"version-2"'
        second = self.run_download(server)
        self.assert_complete(second, DATA + b"new edition")
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(Path(first["path"]).read_bytes(), DATA)

    def test_interrupted_prefix_resumes_only_missing_bytes(self):
        server = Server()
        server.fail_once = (32, TimeoutError("interrupted"))
        first = self.run_download(server)
        self.assertEqual(first["status"], "deferred")
        self.assertIsNone(first["path"])
        self.assertEqual(first["resume"]["retained_bytes"], 32)
        self.assertEqual(first["budget"]["received_bytes"], 32)
        server.calls.clear()
        second = self.run_download(server)
        self.assert_complete(second)
        self.assertEqual(server.calls[1].headers["Range"], "bytes=32-95")
        self.assertEqual(second["resume"]["reused_bytes"], 32)
        self.assertEqual(second["budget"]["received_bytes"], 96)

    def test_seeded_prefix_distinguishes_reused_and_fetched_bytes(self):
        self.seed(96)
        result = self.run_download(Server())
        self.assert_complete(result)
        self.assertEqual(result["resume"]["seeded_bytes"], 96)
        self.assertEqual(result["resume"]["reused_bytes"], 96)
        self.assertEqual(result["budget"]["received_bytes"], 32)

    def test_seeded_old_entity_restarts_before_appending_new_entity(self):
        self.seed()
        server = Server(DATA + b"replacement", '"version-2"')
        result = self.run_download(server)
        self.assert_complete(result, server.body)
        self.assertTrue(result["resume"]["restarted"])
        self.assertEqual(result["resume"]["reused_bytes"], 0)
        self.assertEqual(server.calls[1].headers["Range"], "bytes=0-63")

    def test_ignored_range_full_response_restarts_instead_of_appending(self):
        self.seed()
        server = Server()
        server.ignore_range = True
        result = self.run_download(server)
        self.assert_complete(result)
        self.assertTrue(result["resume"]["restarted"])
        self.assertEqual(result["budget"]["received_bytes"], len(DATA))

    def test_wrong_range_or_length_never_reads_or_changes_prefix(self):
        self.seed()
        for bad_header, bad_value in [("Content-Range", "bytes 0-63/128"), ("Content-Length", "65")]:
            with self.subTest(header=bad_header):
                server = Server()
                server.mutate_response = lambda request, response: response.headers.update({bad_header: bad_value}) if request.method == "GET" else None
                result = self.run_download(server)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error_kind"], "remote_protocol")
                self.assertEqual(result["budget"]["received_bytes"], 0)
                self.assertEqual((self.folder / "partial.bin").read_bytes(), DATA[:64])
                self.assertTrue(all(response.reads == 0 for response in server.responses))

    def test_changed_range_etag_restarts_boundedly_without_mixing_bytes(self):
        self.seed()
        server = Server()
        server.mutate_response = lambda request, response: response.headers.update({"ETag": '"unexpected"'}) if request.method == "GET" else None
        result = self.run_download(server)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["budget"]["requests"], 4)
        self.assertEqual(result["budget"]["received_bytes"], 0)
        self.assertTrue(result["resume"]["restarted"])

    def test_weak_etag_is_not_resumed_or_used_for_conditional_cache(self):
        server = Server(etag='W/"version-1"')
        server.fail_once = (32, TimeoutError())
        first = self.run_download(server)
        self.assertEqual(first["status"], "deferred")
        self.assertFalse(first["resume"]["resumable"])
        server.calls.clear()
        second = self.run_download(server)
        self.assert_complete(second)
        self.assertNotIn("Range", server.calls[1].headers)
        server.calls.clear()
        third = self.run_download(server)
        self.assertEqual(third["status"], "unchanged")
        self.assertNotIn("If-None-Match", server.calls[0].headers)
        self.assertEqual(third["budget"]["received_bytes"], len(DATA))

    def test_corrupt_prefix_and_completed_object_are_local_integrity_failures(self):
        self.seed()
        (self.folder / "partial.bin").write_bytes(b"bad" + DATA[3:64])
        server = Server()
        result = self.run_download(server)
        self.assertEqual(result["error_kind"], "local_integrity")
        self.assertEqual(server.calls, [])
        (self.folder / "partial.bin").write_bytes(DATA[:64])
        completed = self.run_download(server)
        self.assert_complete(completed)
        Path(completed["path"]).write_bytes(b"corrupted")
        server.calls.clear()
        result = self.run_download(server)
        self.assertEqual(result["error_kind"], "local_integrity")
        self.assertEqual(server.calls, [])

    def test_partial_symlink_is_rejected_without_touching_target(self):
        self.seed()
        target = self.root / "outside.bin"
        target.write_bytes(b"do not change")
        (self.folder / "partial.bin").unlink()
        (self.folder / "partial.bin").symlink_to(target)
        result = self.run_download(Server())
        self.assertEqual(result["error_kind"], "local_integrity")
        self.assertEqual(target.read_bytes(), b"do not change")

    def test_corrupt_manifest_and_missing_recorded_file_are_integrity_errors(self):
        self.seed()
        (self.folder / "manifest.json").write_text("{")
        self.assertEqual(self.run_download(Server())["error_kind"], "local_integrity")
        (self.folder / "manifest.json").write_text(json.dumps({"schema_version": 1, "source_url": SOURCE, "partial": {"path": "partial.bin", "bytes": 64, "sha256": "0" * 64, "total_bytes": 128}}))
        (self.folder / "partial.bin").unlink()
        self.assertEqual(self.run_download(Server())["error_kind"], "local_integrity")

    def test_unsafe_source_urls_are_rejected_before_transport(self):
        for source in ["file:///etc/passwd", "http://127.0.0.1/audio", "http://[::1]/audio", "https://user:secret@publisher.example/audio", "https://localhost/audio", "https://router.local/audio", "https://publisher.example/audio#fragment"]:
            with self.subTest(url=source):
                server = Server()
                result = audio.download_audio(source, self.folder, public_offer=OFFER, opener=server, resolver=resolver, limits=LIMITS)
                self.assertEqual(result["status"], "error")
                self.assertEqual(server.calls, [])

    def test_private_dns_answer_rejects_entire_host_even_when_public_answer_exists(self):
        def mixed(host, port, **kwargs):
            return resolver(host, port) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", port))]
        server = Server()
        result = self.run_download(server, resolver=mixed)
        self.assertEqual(result["status"], "error")
        self.assertEqual(server.calls, [])

    def test_redirect_to_private_or_insecure_target_is_never_followed(self):
        for target in ["http://127.0.0.1/audio", "https://internal.example/audio", "http://publisher.example/audio"]:
            with self.subTest(target=target):
                class Redirect:
                    calls = []
                    def open(self, request, *, timeout):
                        self.calls.append(request)
                        return Response(302, {"Location": target})
                opener = Redirect()
                def resolve(host, port, **kwargs):
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (("10.0.0.1" if host == "internal.example" else "93.184.216.34"), port))]
                result = self.run_download(opener, resolver=resolve)
                self.assertEqual(result["status"], "error")
                self.assertEqual(len(opener.calls), 1)

    def test_redirects_count_toward_request_cap(self):
        class Redirect:
            def open(self, request, *, timeout):
                return Response(302, {"Location": request.url + "x"})
        result = self.run_download(Redirect(), limits=replace(LIMITS, max_requests=2))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["budget"]["requests"], 2)

    def test_byte_request_and_time_caps_preserve_only_accepted_prefix(self):
        result = self.run_download(Server(), limits=replace(LIMITS, max_run_bytes=40))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["budget"]["received_bytes"], 40)
        self.assertEqual(result["resume"]["retained_bytes"], 40)
        server = Server()
        request_limited = self.run_download(server, limits=replace(LIMITS, max_requests=1))
        self.assertEqual(request_limited["status"], "deferred")
        self.assertEqual(len(server.calls), 1)
        server.mutate_response = lambda request, response: setattr(response, "clock", self.clock) if request.method == "GET" else None
        timed = self.run_download(server, limits=replace(LIMITS, max_seconds=1))
        self.assertEqual(timed["status"], "deferred")
        self.assertLessEqual(timed["budget"]["received_bytes"], 32)

    def test_declared_oversize_refused_before_body(self):
        server = Server()
        result = self.run_download(server, limits=replace(LIMITS, max_file_bytes=100))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(result["budget"]["received_bytes"], 0)

    def test_unknown_length_requires_observed_eof_not_just_reaching_cap(self):
        server = Server()
        server.unknown_length = True
        limited = self.run_download(server, limits=replace(LIMITS, max_run_bytes=128))
        self.assertEqual(limited["status"], "deferred")
        self.assertIsNone(limited["path"])
        self.assertFalse(limited["resume"]["resumable"])
        complete = self.run_download(server)
        self.assert_complete(complete)

    def test_head_unsupported_uses_bounded_full_get(self):
        server = Server()
        server.head_status = 405
        result = self.run_download(server)
        self.assert_complete(result)
        self.assertNotIn("Range", server.calls[1].headers)

    def test_head_unsupported_resumes_only_when_exact_strong_range_validates(self):
        self.seed()
        server = Server()
        server.head_status = 405
        result = self.run_download(server)
        self.assert_complete(result)
        self.assertEqual(result["budget"]["media_bytes"], 64)
        self.assertEqual(server.calls[1].headers["If-Range"], '"version-1"')
        self.assertEqual(server.calls[1].headers["Range"], "bytes=64-127")

    def test_head_unsupported_complete_file_get_condition_can_return_304(self):
        server = Server()
        self.assert_complete(self.run_download(server))
        server.conditional = False
        server.head_status = 405
        server.calls.clear()
        def mutate(request, response):
            if request.method == "GET" and request.headers.get("If-None-Match") == server.etag:
                response.status = 304
        server.mutate_response = mutate
        result = self.run_download(server)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["budget"]["media_bytes"], 0)
        self.assertEqual(len(server.calls), 2)

    def test_head_unsupported_get_metadata_can_validate_strong_complete_file(self):
        server = Server()
        self.assert_complete(self.run_download(server))
        server.conditional = False
        server.head_status = 405
        server.calls.clear()
        result = self.run_download(server)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["budget"]["media_bytes"], 0)
        self.assertEqual(server.responses[-1].reads, 0)

    def test_bad_mime_encoding_or_html_body_never_becomes_complete(self):
        for variant in ["mime", "encoding", "html"]:
            with self.subTest(variant=variant):
                self.folder = self.root / variant
                server = Server(b"<html>subscribe to continue</html>" if variant == "html" else DATA)
                def mutate(request, response):
                    if variant == "mime": response.headers["Content-Type"] = "text/html"
                    if variant == "encoding": response.headers["Content-Encoding"] = "gzip"
                server.mutate_response = mutate
                result = self.run_download(server)
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["path"])

    def test_interrupt_checkpoints_and_rethrows_without_losing_prefix(self):
        server = Server()
        server.fail_once = (32, KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_download(server)
        partial = self.manifest()["partial"]
        self.assertEqual(partial["bytes"], 32)
        self.assertEqual(partial["sha256"], hashlib.sha256(DATA[:32]).hexdigest())
        self.assert_complete(self.run_download(server))

    def test_uncommitted_tail_is_discarded_before_resume(self):
        self.seed()
        with (self.folder / "partial.bin").open("ab") as file:
            file.write(b"uncommitted crash tail")
        result = self.run_download(Server())
        self.assert_complete(result)
        self.assertEqual(result["resume"]["reused_bytes"], 64)

    def test_commit_interruption_is_idempotent_and_does_not_duplicate_objects(self):
        original_save, failed = audio._save, [False]
        def fail_once(path, state):
            if path.name == "manifest.json" and state.get("complete") and not failed[0]:
                failed[0] = True
                raise OSError("synthetic interrupted manifest commit")
            return original_save(path, state)
        with patch.object(audio, "_save", fail_once):
            interrupted = self.run_download(Server())
        self.assertEqual(interrupted["status"], "error")
        result = self.run_download(Server())
        self.assert_complete(result)
        self.assertEqual(result["budget"]["received_bytes"], 0)
        self.assertEqual(len(list((self.folder / "objects").iterdir())), 1)

    def test_missing_or_wrong_304_validator_is_not_unchanged(self):
        server = Server()
        self.assert_complete(self.run_download(server))
        server.mutate_response = lambda request, response: response.headers.pop("ETag", None)
        result = self.run_download(server)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["path"])

    def test_complete_signing_tuple_rotates_but_content_query_remains_scoped(self):
        first = "https://substackcdn.com/audio.mp3?post_id=1&Expires=1&Key-Pair-Id=key&Signature=old"
        second = "https://substackcdn.com/audio.mp3?post_id=1&Expires=2&Key-Pair-Id=key&Signature=new"
        self.assertEqual(audio._entity_key(first), audio._entity_key(second))
        self.assertNotEqual(audio._entity_key(first), audio._entity_key(second.replace("post_id=1", "post_id=2")))
        self.assertNotEqual(audio._entity_key("https://cdn.example/audio?Signature=one"), audio._entity_key("https://cdn.example/audio?Signature=two"))
        self.assertNotEqual(audio._entity_key(first.replace("substackcdn.com", "other.example")), audio._entity_key(second.replace("substackcdn.com", "other.example")))
        self.assertNotEqual(audio._entity_key("https://cdn.example/audio?name=a+b"), audio._entity_key("https://cdn.example/audio?name=a%20b"))

    def test_directory_lock_prevents_overlapping_downloads(self):
        self.folder.mkdir()
        with (self.folder / ".download.lock").open("w") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            server = Server()
            result = self.run_download(server)
            self.assertEqual(result["status"], "deferred")
            self.assertEqual(server.calls, [])

    def test_nonpublic_offer_is_not_downloaded(self):
        server = Server()
        result = audio.download_audio(SOURCE, self.folder, public_offer={**OFFER, "public_access": False}, opener=server, resolver=resolver)
        self.assertEqual(result["status"], "error")
        self.assertEqual(server.calls, [])

    def test_default_transport_uses_pinned_public_ip_and_no_proxy_or_credentials(self):
        class WireSocket:
            def __init__(self):
                self.addresses, self.sent, self.closed = [], [], False
                self.data = io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            def connect(self, address): self.addresses.append(address)
            def settimeout(self, timeout): pass
            def sendall(self, value): self.sent.append(value)
            def recv_into(self, buffer):
                data = self.data.read(len(buffer)); buffer[:len(data)] = data; return len(data)
            def close(self): self.closed = True
        wire = WireSocket()
        request = audio.AudioRequest("http://publisher.example/audio", "HEAD", {"Accept-Encoding": "identity"},
                                     ((socket.AF_INET, socket.SOCK_STREAM, 6, ("93.184.216.34", 80)),))
        with patch.dict(os.environ, {"HTTP_PROXY": "http://user:secret@127.0.0.1:9999"}), patch.object(audio.socket, "socket", return_value=wire):
            response = audio.PublicOpener().open(request, timeout=2)
            self.assertEqual(response.status, 200)
            response.close()
        self.assertEqual(wire.addresses, [("93.184.216.34", 80)])
        sent = b"".join(wire.sent)
        self.assertIn(b"Host: publisher.example", sent)
        self.assertNotIn(b"Authorization", sent)
        self.assertNotIn(b"Cookie", sent)
        self.assertNotIn(b"127.0.0.1", sent)

    def test_optional_doh_pins_public_bootstrap_and_counts_caches_all_requests(self):
        server = Server()
        doh_calls, dns_payloads = [], []
        def open_request(request, *, timeout):
            if urlsplit(request.url).hostname == "cloudflare-dns.com":
                doh_calls.append(request)
                host = parse_qs(urlsplit(request.url).query)["name"][0]
                payload = {"Status": 0, "TC": False, "Question": [{"name": host, "type": 1}],
                           "Answer": [{"name": host, "type": 5, "TTL": 300, "data": "edge.example."},
                                      {"name": "edge.example.", "type": 1, "TTL": 60, "data": "93.184.216.34"}]}
                body = json.dumps(payload).encode()
                dns_payloads.append(body)
                return Response(200, {"Content-Type": "application/dns-json", "Content-Length": str(len(body))}, body)
            return server.open(request, timeout=timeout)
        class Opener:
            open = staticmethod(open_request)
        result = self.run_download(Opener(), resolver="cloudflare_doh")
        self.assert_complete(result)
        self.assertEqual(len(doh_calls), 1)
        self.assertEqual([address[3][0] for address in doh_calls[0].addresses], ["1.1.1.1", "1.0.0.1"])
        self.assertEqual(result["budget"]["requests"], 4)
        self.assertEqual(result["budget"]["dns_bytes"], len(dns_payloads[0]))
        self.assertEqual(result["budget"]["media_bytes"], len(DATA))
        self.assertEqual(result["budget"]["received_bytes"], len(DATA) + len(dns_payloads[0]))
        self.assertEqual(result["dns_method"], "cloudflare_doh")
        self.assertTrue(all(call.addresses[0][3][0] == "93.184.216.34" for call in server.calls))
        self.assertEqual(doh_calls[0].headers["Accept"], "application/dns-json")
        self.assertNotIn("episode.mp3", doh_calls[0].url)

    def test_doh_rejects_private_wrong_question_unlinked_and_looping_answers(self):
        base = {"Status": 0, "Question": [{"name": "publisher.example", "type": 1}],
                "Answer": [{"name": "publisher.example", "type": 1, "TTL": 30, "data": "93.184.216.34"}]}
        examples = []
        for kind in ["private", "wrong_question", "unlinked", "loop", "truncated"]:
            payload = json.loads(json.dumps(base))
            if kind == "private": payload["Answer"][0]["data"] = "198.18.0.1"
            if kind == "wrong_question": payload["Question"][0]["name"] = "other.example"
            if kind == "unlinked": payload["Answer"].append({"name": "other.example", "type": 1, "TTL": 30, "data": "1.1.1.1"})
            if kind == "loop": payload["Answer"] = [{"name": "publisher.example", "type": 5, "TTL": 30, "data": "publisher.example"}]
            if kind == "truncated": payload["TC"] = True
            examples.append((kind, payload))
        for kind, payload in examples:
            with self.subTest(kind=kind):
                with self.assertRaises(audio.DownloadError):
                    audio._doh_answers(json.dumps(payload).encode(), "publisher.example")

    def test_doh_failure_retains_seed_and_never_accepts_synthetic_addresses(self):
        self.seed()
        calls = []
        class Opener:
            def open(self, request, *, timeout):
                calls.append(request)
                return Response(503, {"Content-Type": "application/dns-json"})
        result = self.run_download(Opener(), resolver="cloudflare_doh")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["resume"]["retained_bytes"], 64)
        self.assertEqual((self.folder / "partial.bin").read_bytes(), DATA[:64])
        self.assertEqual(len(calls), 1)
        self.assertEqual(urlsplit(calls[0].url).hostname, "cloudflare-dns.com")

    def test_doh_bytes_and_requests_share_media_budget(self):
        body = b" " * 200
        calls = []
        class Opener:
            def open(self, request, *, timeout):
                calls.append(request)
                return Response(200, {"Content-Type": "application/dns-json", "Content-Length": "200"}, body)
        result = self.run_download(Opener(), resolver="cloudflare_doh", limits=replace(LIMITS, max_run_bytes=40))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["budget"]["received_bytes"], 40)
        self.assertEqual(result["budget"]["dns_bytes"], 40)
        self.assertEqual(result["budget"]["media_bytes"], 0)
        self.assertEqual(len(calls), 1)

    def test_tls_transport_verifies_hostname_while_connecting_pinned_ip(self):
        class WireSocket:
            def __init__(self):
                self.data = io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                self.address = None
            def connect(self, address): self.address = address
            def settimeout(self, timeout): pass
            def sendall(self, value): pass
            def recv_into(self, buffer):
                data = self.data.read(len(buffer)); buffer[:len(data)] = data; return len(data)
            def close(self): pass
        wire, verified = WireSocket(), []
        class Context:
            def wrap_socket(self, sock, *, server_hostname):
                verified.append(server_hostname)
                return sock
        request = audio.AudioRequest("https://cloudflare-dns.com/dns-query?name=publisher.example&type=A", "GET", {},
                                     ((socket.AF_INET, socket.SOCK_STREAM, 6, ("1.1.1.1", 443)),))
        with patch.object(audio.socket, "socket", return_value=wire), patch.object(audio.ssl, "create_default_context", return_value=Context()):
            response = audio.PublicOpener().open(request, timeout=2)
            response.close()
        self.assertEqual(wire.address, ("1.1.1.1", 443))
        self.assertEqual(verified, ["cloudflare-dns.com"])

    def test_generic_binary_needs_frames_then_returns_detected_and_declared_mime(self):
        server = Server(mp3_bytes(tag_bytes=20))
        server.mutate_response = lambda request, response: response.headers.update({"Content-Type": "binary/octet-stream"})
        result = self.run_download(server)
        self.assert_complete(result, server.body)
        self.assertEqual(result["declared_mime_type"], "binary/octet-stream")
        self.assertEqual(result["mime_type"], "audio/mpeg")
        proof = result["mime_detection"]
        self.assertEqual(proof["id3"]["first_audio_frame_offset"], 30)
        self.assertEqual([row["offset"] for row in proof["frames"]], [30, 343])
        self.assertEqual(proof["prefix_sha256"], hashlib.sha256(server.body[:347]).hexdigest())
        self.assertFalse(proof["full_file_decoder_validated"])
        repeated = self.run_download(server)
        self.assertEqual(repeated["status"], "unchanged")
        self.assertEqual(repeated["budget"]["media_bytes"], 0)

    def test_generic_detected_prefix_resumes_without_confusing_wire_mime(self):
        server = Server(mp3_bytes())
        server.mutate_response = lambda request, response: response.headers.update({"Content-Type": "application/octet-stream"})
        first = self.run_download(server, limits=replace(LIMITS, max_run_bytes=400))
        self.assertEqual(first["status"], "deferred")
        self.assertEqual(first["mime_type"], "audio/mpeg")
        self.assertEqual(first["resume"]["retained_bytes"], 400)
        self.assertEqual(first["declared_mime_type"], "application/octet-stream")
        server.calls.clear()
        second = self.run_download(server)
        self.assert_complete(second, server.body)
        self.assertEqual(second["resume"]["reused_bytes"], 400)
        self.assertFalse(second["resume"]["restarted"])
        self.assertEqual(server.calls[1].headers["Range"], "bytes=400-463")
        self.assertEqual(second["budget"]["media_bytes"], len(server.body) - 400)

    def test_generic_metadata_only_partial_is_not_labeled_complete_audio(self):
        server = Server(mp3_bytes(tag_bytes=500))
        server.mutate_response = lambda request, response: response.headers.update({"Content-Type": "binary/octet-stream"})
        limits = replace(LIMITS, max_file_bytes=2048, max_run_bytes=64)
        first = self.run_download(server, limits=limits)
        self.assertEqual(first["status"], "deferred")
        self.assertIsNone(first["path"])
        self.assertIsNone(first["mime_detection"])
        self.assertEqual(first["mime_type"], "binary/octet-stream")
        self.assertEqual(first["resume"]["retained_bytes"], 64)
        second = self.run_download(server, limits=replace(limits, max_run_bytes=2048))
        self.assert_complete(second, server.body)
        self.assertEqual(second["resume"]["reused_bytes"], 64)

    def test_generic_html_errors_id3_spoofs_bad_frames_and_unknown_types_are_rejected(self):
        samples = [b"<html>Subscribe to continue</html>", b'{"error":"denied"}', b"ID3not-an-id3-header",
                   b"ID3\x04\x00\x00\x00\x00\x00\x00<html>not audio</html>", b"\xff\xfb\x00\x00" + bytes(64),
                   bytes.fromhex("89504e470d0a1a0a") + bytes(100)]
        for index, body in enumerate(samples):
            with self.subTest(index=index):
                self.folder = self.root / f"generic-bad-{index}"
                server = Server(body)
                server.mutate_response = lambda request, response: response.headers.update({"Content-Type": "application/octet-stream"})
                result = self.run_download(server)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error_kind"], "remote_protocol")
                self.assertIsNone(result["path"])
                self.assertFalse((self.folder / "objects").exists())
                self.assertEqual(result["resume"]["retained_bytes"], 0)
        self.folder = self.root / "unsupported-content-type"
        server = Server(mp3_bytes())
        server.mutate_response = lambda request, response: response.headers.update({"Content-Type": "application/x-unverified"})
        result = self.run_download(server)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["budget"]["media_bytes"], 0)

    def test_generic_single_frame_does_not_establish_complete_audio(self):
        server = Server(mp3_bytes()[:313])
        server.mutate_response = lambda request, response: response.headers.update({"Content-Type": "binary/octet-stream"})
        result = self.run_download(server)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["path"])
        self.assertFalse((self.folder / "objects").exists())

    def test_generic_excessive_id3_tag_is_rejected_before_large_body_read(self):
        tag_bytes = audio.MAX_MIME_SNIFF_BYTES
        header = b"ID3\x04\x00\x00" + bytes((tag_bytes >> shift) & 127 for shift in (21, 14, 7, 0))
        with self.assertRaises(audio.ProtocolError):
            audio._sniff_generic_audio(header, total_bytes=100_000_000)
        real_sized_tag = 992084
        body = mp3_bytes(tag_bytes=real_sized_tag)
        self.assertIsNone(audio._sniff_generic_audio(body[:262144], total_bytes=len(body)))
        proof = audio._sniff_generic_audio(body, total_bytes=len(body), final=True)
        self.assertEqual(proof["frames"][0]["offset"], 992094)

    def test_generic_corrupt_retained_prefix_still_stops_before_network(self):
        server = Server(mp3_bytes())
        server.mutate_response = lambda request, response: response.headers.update({"Content-Type": "binary/octet-stream"})
        self.run_download(server, limits=replace(LIMITS, max_run_bytes=400))
        path = self.folder / "partial.bin"
        value = bytearray(path.read_bytes()); value[50] ^= 1; path.write_bytes(value)
        server.calls.clear()
        result = self.run_download(server)
        self.assertEqual(result["error_kind"], "local_integrity")
        self.assertEqual(server.calls, [])

    def test_generic_wire_type_change_restarts_instead_of_appending(self):
        server = Server(mp3_bytes())
        server.mutate_response = lambda request, response: response.headers.update({"Content-Type": "binary/octet-stream"})
        self.run_download(server, limits=replace(LIMITS, max_run_bytes=400))
        server.mutate_response = lambda request, response: response.headers.update({"Content-Type": "application/octet-stream"})
        result = self.run_download(server)
        self.assert_complete(result, server.body)
        self.assertTrue(result["resume"]["restarted"])
        self.assertEqual(result["resume"]["reused_bytes"], 0)
        self.assertEqual(result["budget"]["media_bytes"], len(server.body))

    def test_generic_bootstrap_requires_hash_and_retains_pending_signature_state(self):
        body = mp3_bytes(tag_bytes=500)
        path = self.root / "generic-prefix.bin"
        path.write_bytes(body[:128])
        limits = replace(LIMITS, max_file_bytes=2048, max_run_bytes=2048)
        result = audio.seed_partial(SOURCE, self.folder, path, public_offer=OFFER, final_url=SOURCE, etag='"version-1"',
                                    total_bytes=len(body), mime_type="binary/octet-stream", expected_sha256=hashlib.sha256(body[:128]).hexdigest(), limits=limits)
        self.assertEqual(result["status"], "seeded")
        self.assertNotIn("mime_detection", self.manifest()["partial"])
        server = Server(body)
        server.mutate_response = lambda request, response: response.headers.update({"Content-Type": "binary/octet-stream"})
        complete = self.run_download(server, limits=limits)
        self.assert_complete(complete, body)
        self.assertEqual(complete["resume"]["seeded_bytes"], 128)


if __name__ == "__main__":
    unittest.main(verbosity=2)
