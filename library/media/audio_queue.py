"""Opt-in bounded audio editions from explicit public publisher offers.

The text collector remains independent. This queue runs after it, under its
state lock, and only commits complete verified audio to stable edition IDs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from collector.backup import sha256
from collector.http import utc_now
from collector.models import Artifact, Batch, Record
from collector.runner import atomic_json, state_lock
from collector.store import Store, canonical_url
from media.audio_downloads import DownloadLimits, download_audio
from media.extractors import soup_from


def public_offer(body, page_url, parent_id, evidence_hash):
    """Accept an explicit anonymous Substack episode offer, never a preview.

    Other platforms are intentionally outside this first queue's discovery
    coverage. A successful media download is not evidence of a transcript.
    """
    soup = soup_from(body)
    node = soup.find("script", id="preloads-json")
    if node is None:
        return None
    try:
        data = json.loads(node.get_text())
        post = data.get("post", {})
    except (ValueError, AttributeError):
        return None
    url = post.get("podcast_url")
    if (not isinstance(url, str) or not url
            or post.get("audience") != "everyone"
            or post.get("free_unlock_required") is not False
            or post.get("is_editor_preview") is True
            or data.get("is_editor_preview") is True
            or soup.select_one('[class*="paywall"], [data-testid*="paywall"]')):
        return None
    try:
        url = canonical_url(url)
        canonical_url(page_url)
    except (ValueError, TypeError):
        return None
    return {"public_access": True, "offer_kind": "explicit_public_substack_episode",
            "publisher_page_url": page_url, "parent_item_id": parent_id,
            "evidence_sha256": evidence_hash, "source_url": url,
            "publisher_post_id": str(post.get("id", "")),
            "duration_seconds": post.get("podcast_duration")}


def discover(store, *, enabled_sources=None, max_stale_days=8):
    found = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_stale_days)
    for item in store.export()["items"]:
        if item["review_status"] != "included":
            continue
        if enabled_sources is not None and not set(item["source_ids"]) & enabled_sources:
            continue
        if datetime.fromisoformat(item["last_seen_at"]) < cutoff:
            continue
        version = next(v for v in item["versions"] if v["id"] == item["latest_version_id"])
        if version["content"]["kind"] == "audio_download" or version["content"]["status"] != "available":
            continue
        for artifact in version["artifacts"]:
            if artifact["role"] != "source_html" or artifact["bytes"] > 12_000_000:
                continue
            if urlsplit(artifact.get("source_url") or "").hostname != urlsplit(item["canonical_url"]).hostname:
                continue
            path = store.resolve_artifact(artifact["relative_path"])
            if sha256(path) != artifact["sha256"]:
                raise ValueError("Publisher evidence failed immutable-object verification")
            offer = public_offer(path.read_bytes(), item["canonical_url"], item["id"], artifact["sha256"])
            if offer:
                found[item["id"]] = {"parent": item, "version": version, "offer": offer, "evidence": artifact}
                break
    return found


def validate_audio(path, offer, *, executable="ffprobe", timeout=20):
    command = [executable, "-v", "error", "-protocol_whitelist", "file", "-show_format", "-show_streams", "-of", "json", str(path)]
    response = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True)
    data = json.loads(response.stdout)
    if not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
        raise ValueError("The complete file has no recognized audio stream")
    duration = float(data["format"]["duration"])
    if duration <= 0:
        raise ValueError("The complete file has no positive audio duration")
    expected = offer.get("duration_seconds")
    if isinstance(expected, (int, float)) and expected > 0 and abs(duration - expected) > max(2, expected * .02):
        raise ValueError("Audio duration differs from the publisher's episode declaration")
    return {"duration_seconds": duration, "format": data["format"].get("format_name"),
            "validation": "container_audio_stream_and_publisher_duration; no listening or full-decode claim"}


def edition(candidate, result, media_info):
    parent, version = candidate["parent"], candidate["version"]
    content, offer = version["content"], candidate["offer"]
    transcript = content.get("metadata", {}).get("transcript_provenance") == "publisher_supplied"
    label = "Publisher-supplied transcript" if transcript else "Publisher page text (not a full audio transcript)"
    text = ("Complete publisher-distributed audio saved.\nSource: " + parent["canonical_url"]
            + "\n\n" + label + ":\n\n" + content["text"])
    metadata = {"parent_item_id": parent["id"], "parent_version_id": version["id"],
                "original_audio_url": offer["source_url"], "original_media_sha256": result["sha256"],
                "original_media_bytes": result["bytes"], "original_media_mime": result["mime_type"],
                "audio_scope": "complete_publisher_distributed_entity_not_recording_master",
                "text_coverage": "publisher_transcript" if transcript else "publisher_page_text_only",
                "automatic_transcription_performed": False, **media_info}
    return Record(canonical_url=parent["canonical_url"], identity_key="audio-edition:" + parent["id"],
                  title=content["title"] + " — saved audio", text=text,
                  work_id=parent.get("work_id") or "work:parent:" + parent["id"], kind="audio_download",
                  author=content.get("author"), published_at=content.get("published_at"), review_status="included",
                  segments=content.get("segments", []) if transcript else [], metadata=metadata,
                  tags=sorted(set(parent["generated_tags"] + ["audio", "original-audio", "publisher-transcript" if transcript else "publisher-page-text"])),
                  person_ids=parent["person_ids"], source_item_id=offer["publisher_post_id"],
                  provenance={"adapter": "public_audio_queue", "public_offer": offer, "download": result,
                              "parent_text_label": label, "automatic_transcription_performed": False},
                  artifacts=[Artifact(path=result["path"], media_type=result["mime_type"], role="original_audio", source_url=offer["source_url"]),
                             Artifact(path=candidate["evidence"]["path"], media_type="text/html", role="publisher_offer_evidence", source_url=parent["canonical_url"])])


def run_queue(config, *, max_seconds=None, required_free_bytes=None, downloader=download_audio,
              validator=validate_audio, clock=time.monotonic):
    from ops.storage import require_storage
    require_storage(config)
    options = config.get("audio", {})
    if not options.get("enabled"):
        return {"status": "disabled", "audio_downloads_enabled": False}, 0
    maximum = int(options.get("max_file_bytes", 80_000_000))
    run_bytes = int(options.get("max_run_bytes", 80_000_000))
    seconds = float(max_seconds if max_seconds is not None else options.get("max_seconds", 600))
    if not (0 < maximum <= 100_000_000 and 0 < run_bytes <= 100_000_000 and 0 < seconds <= 600):
        raise ValueError("Audio limits exceed the reviewed 100MB / 600s pilot bounds")
    started = clock()
    root = Path(config["state"])
    required = required_free_bytes if required_free_bytes is not None else config.get("minimum_free_bytes", 2 * 1024**3) + 7 * maximum
    registry = json.loads(Path(config["registry"]).read_text())
    enabled = {s["id"] for s in registry["sources"] if s.get("enabled", True)}
    with state_lock(root):
        store = Store(root, max_artifact_bytes=maximum)
        queue_path = root / "audio/queue.json"
        if queue_path.is_symlink():
            raise ValueError("Queue manifest must not be a symlink")
        queue = json.loads(queue_path.read_text()) if queue_path.exists() else {"schema_version": 1, "jobs": {}}
        if queue.get("schema_version") != 1 or not isinstance(queue.get("jobs"), dict):
            raise ValueError("Unrecognized audio queue")
        run_id = "audio:" + str(uuid.uuid4())
        summary = {"status": "running", "run_id": run_id, "attempts": [], "new_items": 0, "new_versions": 0,
                   "completed": 0, "unchanged": 0, "received_bytes": 0, "media_bytes": 0, "dns_bytes": 0, "reused_bytes": 0,
                   "coverage": "explicit public Substack offers in recently captured included publisher pages",
                   "automatic_transcription_performed": False, "limits": {"max_file_bytes": maximum, "max_run_bytes": run_bytes,
                       "max_seconds": seconds, "max_completed": options.get("max_completed", 1), "max_attempts": options.get("max_attempts", 3)}}
        now = utc_now()
        try:
            candidates = discover(store, enabled_sources=enabled, max_stale_days=options.get("max_stale_days", 8))
            for key, candidate in candidates.items():
                prior = queue["jobs"].get(key, {})
                queue["jobs"][key] = {**prior, "parent_item_id": key, "source_url": candidate["offer"]["source_url"],
                                      "publisher_page_url": candidate["parent"]["canonical_url"], "eligible": True,
                                      "offer": candidate["offer"], "status": prior.get("status", "pending")}
            for key, job in queue["jobs"].items():
                if key not in candidates:
                    job["eligible"] = False
                    job["suspension_reason"] = "Parent is stale, not included, disabled, or lacks a current explicit public offer"
            # Generated editions inherit parent scope changes; an explicit
            # manual review on the audio edition remains the user's override.
            reclassified = 0
            with store.transaction():
                for key in queue["jobs"]:
                    parent_review = store.db.execute("SELECT COALESCE(a.review_status,i.source_review_status) FROM items i LEFT JOIN annotations a ON a.item_id=i.id WHERE i.id=?", (key,)).fetchone()
                    if parent_review:
                        cursor = store.db.execute("UPDATE items SET source_review_status=? WHERE identity_key=? AND source_review_status<>?", (parent_review[0], "audio-edition:" + key, parent_review[0]))
                        reclassified += cursor.rowcount
            summary["audio_scope_classifications_updated"] = reclassified
            atomic_json(queue_path, queue)
            with store.transaction():
                # Never enter the text collector's interrupted/running namespace.
                store.db.execute("INSERT INTO runs(id,started_at,window_end,status) VALUES(?,?,?,'audio_running')", (run_id, now, now))
            ordered = sorted(candidates, key=lambda k: (queue["jobs"][k].get("last_attempt_at", ""), k))
            for key in ordered:
                if len(summary["attempts"]) >= options.get("max_attempts", 3) or summary["new_versions"] >= options.get("max_completed", 1):
                    break
                remaining = seconds - (clock() - started) - 5
                available_bytes = run_bytes - summary["received_bytes"]
                if remaining <= 1 or available_bytes <= 0:
                    break
                job = queue["jobs"][key]
                if shutil.disk_usage(root).free < required:
                    summary["deferred_reason"] = "Disk space reserved for publication, verified backups, and the configured free-space floor"
                    break
                job.update(last_attempt_at=utc_now(), status="attempting")
                atomic_json(queue_path, queue)
                candidate = candidates[key]
                directory = root / "audio/jobs" / key / hashlib.sha256(job["source_url"].encode()).hexdigest()[:20]
                transport = {"resolver": options["resolver"]} if options.get("resolver") else {}
                result = downloader(job["source_url"], directory, public_offer=candidate["offer"],
                                    limits=DownloadLimits(max_file_bytes=maximum, max_run_bytes=available_bytes, max_seconds=remaining, max_requests=32), **transport)
                if result["status"] not in {"complete", "unchanged", "deferred", "error"}:
                    raise ValueError("Unrecognized download result")
                if result.get("error_kind") == "local_integrity":
                    raise ValueError("Previously trusted audio download state failed integrity verification: " + str(result.get("errors", [])))
                summary["received_bytes"] += result.get("budget", {}).get("received_bytes", 0)
                summary["media_bytes"] += result.get("budget", {}).get("media_bytes", result.get("budget", {}).get("received_bytes", 0))
                summary["dns_bytes"] += result.get("budget", {}).get("dns_bytes", 0)
                summary["reused_bytes"] += result.get("resume", {}).get("reused_bytes", 0)
                attempt = {"parent_item_id": key, "source_url": job["source_url"], "status": result["status"],
                           "bytes": result.get("bytes", 0), "errors": result.get("errors", []), "resume": result.get("resume", {}),
                           "network_bytes": result.get("budget", {}).get("received_bytes", 0)}
                summary["attempts"].append(attempt)
                job.update(status=result["status"], last_result=result)
                if result["status"] in {"complete", "unchanged"}:
                    path = Path(result["path"])
                    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or path.stat().st_size != result["bytes"] or sha256(path) != result["sha256"]:
                        raise ValueError("Completed audio failed local immutable-byte verification")
                    try:
                        info = validator(path, candidate["offer"], executable=options.get("ffprobe", "ffprobe"), timeout=max(.1, min(20, seconds - (clock() - started))))
                    except (ValueError, subprocess.SubprocessError) as error:
                        job["status"] = attempt["status"] = "deferred"
                        attempt["errors"] = ["Media validation did not pass: " + str(error)[:300]]
                        atomic_json(queue_path, queue)
                        continue
                    record = edition(candidate, result, info)
                    source = {"id": "audio:" + key, "adapter": "public_audio_queue", "url": candidate["parent"]["canonical_url"], "enabled": False}
                    store.register_sources([source])
                    store.begin_source(run_id, source["id"], now, now)
                    committed = store.commit_batch(run_id, source, Batch(records=[record], coverage={"window_complete": True, "corpus_complete": False, "scope": "one_verified_publisher_audio_entity"}), now, now, utc_now())
                    for count in ["new_items", "new_versions"]:
                        summary[count] += committed[count]
                    if job.get("committed_sha256") == result["sha256"]:
                        summary["unchanged"] += 1
                    else:
                        summary["completed"] += 1
                    job.update(status="complete", committed_sha256=result["sha256"], last_success_at=utc_now(),
                               audio_item_id=hashlib.sha256(record.identity_key.encode()).hexdigest())
                atomic_json(queue_path, queue)
            attempted = {a["parent_item_id"] for a in summary["attempts"]}
            pending = [k for k in candidates if queue["jobs"][k].get("status") != "complete"]
            summary.update(pending_count=len(pending), eligible_count=len(candidates), unchecked_this_run=len(set(candidates) - attempted),
                           suspended_count=sum(not j.get("eligible") for j in queue["jobs"].values()))
            partial = bool(pending or summary["unchecked_this_run"] or summary.get("deferred_reason"))
            summary["status"] = "partial" if partial else "ok"
            summary["elapsed_seconds"] = round(clock() - started, 3)
            store.finish_run(run_id, "audio_" + summary["status"], summary)
            atomic_json(root / "audio/latest-result.json", summary)
            return summary, 2 if partial else 0
        except BaseException as error:
            # Persist queue progress even if the last neutral commit already won.
            # Replaying its deterministic item+version IDs is idempotent.
            atomic_json(queue_path, queue)
            if store.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                store.finish_run(run_id, "audio_failed", {"error": type(error).__name__ + ": " + str(error)})
            raise
        finally:
            store.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--required-free-bytes", type=int)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        result, code = run_queue(json.loads(Path(args.config).read_text()), max_seconds=args.max_seconds, required_free_bytes=args.required_free_bytes)
    except KeyboardInterrupt:
        result, code = {"status": "partial", "error": "Interrupted; validated queued bytes retained"}, 2
    except Exception as error:
        result, code = {"status": "failed", "error": type(error).__name__ + ": " + str(error)}, 3
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
