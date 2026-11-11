"""Network adapters following collector.models; no database or schedule mutations."""
from __future__ import annotations

from dataclasses import replace
import json
import sys
import os
from pathlib import Path
import signal
import subprocess
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from collector.models import Artifact, Batch, CollectionContext, Record
from media.extractors import article_candidates, candidate_url_allowed, content_fingerprints, extract_article, extract_publisher, youtube_id

ROOT = Path(__file__).resolve().parent
DEFAULT_PYTHON = Path(sys.executable)


def coverage(scope: str, **values) -> dict:
    return {"scope": scope, "window_complete": None, "corpus_complete": False, **values}


def article(source: dict, context: CollectionContext) -> Batch:
    response = context.client.get(source["url"], max_bytes=context.max_bytes, accept="text/html")
    extractor = extract_publisher if source["adapter"] == "publisher_transcript" else extract_article
    data = extractor(response.body, source)
    canonical = source.get("canonical_url") or response.url
    declared = data["metadata"].get("declared_canonical_url")
    if not source.get("canonical_url") and declared:
        target, actual = urlsplit(declared), urlsplit(response.url)
        if target.scheme in {"http", "https"} and not target.username and not target.password and (target.hostname or "").removeprefix("www.") == (actual.hostname or "").removeprefix("www."):
            canonical = declared
    metadata = {**data["metadata"], "person_ids": source.get("person_ids", []), "canonical_source_url": canonical}
    record = Record(
        canonical_url=canonical,
        identity_key=source.get("identity_key"), source_item_id=metadata.get("publisher_post_id"),
        work_id=source.get("work_id"), title=data["title"], text=data["text"],
        kind=source.get("kind", "article"), author=metadata.get("author") or source.get("author"),
        published_at=metadata.get("published_at"), segments=data["segments"], metadata=metadata,
        tags=source.get("tags", []), review_status=source.get("review_status", "needs_review"),
        provenance={"adapter": source["adapter"], "retrieved_at": response.retrieved_at, "retrieved_url": response.url,
                    "http_status": response.status, "membership_evidence": source.get("membership_evidence", []),
                    "content_fingerprints": content_fingerprints(data["text"], data["segments"], metadata),
                    "transcript_generation_method": "unspecified_by_publisher" if source["adapter"] == "publisher_transcript" else None},
        artifacts=[Artifact(data=response.body, media_type="text/html", role="source_html", source_url=response.url)],
    )
    return Batch(records=[record], cursor={}, coverage=coverage("registered_url", window_complete=True, fetched_count=1,
                 limitation="Confirms this registered URL only; source archives and other members remain outside this coverage claim."))


def youtube(source: dict, context: CollectionContext) -> Batch:
    video_id = youtube_id(source["url"])
    output = context.artifact_dir / ("youtube-" + video_id)
    output.mkdir(parents=True, exist_ok=True)
    python = Path(source.get("options", {}).get("python", DEFAULT_PYTHON))
    if not python.exists():
        raise RuntimeError("Tested media Python environment is unavailable; see media/requirements.txt")
    limit = min(context.remaining_seconds(), float(source.get("options", {}).get("max_seconds", 150)))
    if limit <= 0:
        raise TimeoutError("YouTube source time budget exhausted before starting a child process")
    command = [str(python), str(ROOT / "youtube_probe.py"), "--url", source["url"], "--output-dir", str(output),
               "--max-seconds", str(limit), "--max-bytes", str(context.max_bytes)]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["XDG_CACHE_HOME"] = str(output / "cache")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=limit)
    except BaseException:
        # This subprocess owns a separate group. Clean it up even when the
        # collector itself is interrupted by the weekly runner's deadline.
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        (output / "runner.log").write_text(stdout + "\n" + stderr)
        raise
    (output / "runner.log").write_text(stdout + "\n" + stderr)
    result_path = output / "probe-result.json"
    if process.returncode or not result_path.exists():
        raise RuntimeError("YouTube probe did not complete: " + stderr[-1000:])
    data = json.loads(result_path.read_text())
    if not data.get("segments"):
        return Batch(status="unavailable", cursor=None, errors=[json.dumps(e) for e in data.get("errors", [])] or ["No usable caption track"],
                     artifacts=[Artifact(path=result_path, media_type="application/json", role="retrieval_diagnostics", source_url=source["url"])],
                     coverage=coverage("registered_video", window_complete=False, fallback="Local ASR requires an explicitly supplied accessible audio file; no original media downloaded automatically."))
    metadata = {**data["metadata"], "person_ids": source.get("person_ids", []), "canonical_source_url": data["canonical_url"]}
    upload = metadata.get("upload_date") or ""
    published = f"{upload[:4]}-{upload[4:6]}-{upload[6:8]}" if len(upload) == 8 and upload.isdigit() else None
    artifacts = []
    for name, mime, role in [("video-metadata.json", "application/json", "video_metadata"),
                              ("captions.source.json", "application/json", "timed_caption_source"),
                              ("captions.source.vtt", "text/vtt", "timed_caption_source"),
                              ("probe-result.json", "application/json", "retrieval_diagnostics")]:
        if (output / name).exists():
            artifacts.append(Artifact(path=output / name, media_type=mime, role=role, source_url=source["url"]))
    record = Record(canonical_url=data["canonical_url"], identity_key="youtube:" + video_id, source_item_id=video_id,
                    work_id=source.get("work_id"), title=metadata.get("title") or source.get("title") or video_id,
                    text=data["text"], kind="video_transcript", author=metadata.get("channel") or source.get("author"),
                    published_at=published, segments=data["segments"], metadata=metadata, tags=source.get("tags", []),
                    review_status=source.get("review_status", "needs_review"), artifacts=artifacts,
                    provenance={"adapter": "youtube_video", "retrieved_at": context.now, "caption": data["caption"],
                                "tools": data["versions"], "network_accounting": data["network_accounting"],
                                "timing_observations": data.get("timing_observations", {}),
                                "original_media_downloaded": False, "retrieval_warnings": data.get("errors", []),
                                "content_fingerprints": content_fingerprints(data["text"], data["segments"], metadata)})
    return Batch(records=[record], status=data["status"], cursor={} if data["status"] == "ok" else None,
                 errors=[json.dumps(e) for e in data.get("errors", []) if e["stage"] == "metadata"],
                 coverage=coverage("registered_video", window_complete=data["status"] == "ok", fetched_count=1,
                                   network_accounting="Caption and yt-dlp requests use separate process/time/output limits; collector client byte counters do not include all tool traffic."))


def feed_candidates(body: bytes, source: dict) -> list[dict]:
    tree = ET.fromstring(body)
    ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    rows = []
    expected = source.get("options", {}).get("channel_id")
    channel = tree.findtext("yt:channelId", namespaces=ns)
    # Feed headers may omit UC, while entry IDs and author URIs retain it.
    accepted_ids = {expected, expected[2:]} if expected and expected.startswith("UC") else {expected}
    if expected and channel not in accepted_ids:
        raise ValueError("YouTube feed channel ID differs from registered channel")
    author_uri = tree.findtext("atom:author/atom:uri", namespaces=ns)
    if expected and author_uri and urlsplit(author_uri).path.rstrip("/") != "/channel/" + expected:
        raise ValueError("YouTube feed author URI differs from registered channel")
    for entry in tree.findall("atom:entry", ns):
        entry_channel = entry.findtext("yt:channelId", namespaces=ns)
        if expected and entry_channel and entry_channel not in accepted_ids:
            raise ValueError("YouTube feed entry channel differs from registered channel")
        video_id = entry.findtext("yt:videoId", namespaces=ns)
        if video_id:
            youtube_id("https://www.youtube.com/watch?v=" + video_id)
            rows.append({"url": "https://www.youtube.com/watch?v=" + video_id,
                         "title": entry.findtext("atom:title", default=video_id, namespaces=ns),
                         "published_at": entry.findtext("atom:published", namespaces=ns)})
    return rows


def discover(source: dict, context: CollectionContext) -> Batch:
    prior = context.cursor or {}
    pending = list(prior.get("pending_urls") or [])
    refresh = list(prior.get("refresh_queue") or [])
    excluded = list(prior.get("excluded_candidates") or [])
    if source["adapter"] == "html_index":
        excluded_urls = {row["url"] for row in excluded}
        for row in pending + refresh:
            if not candidate_url_allowed(row["url"], source) and row["url"] not in excluded_urls:
                excluded.append({**row, "reason": "Outside current registered article path rules; category/navigation evidence retained"})
                excluded_urls.add(row["url"])
        pending = [row for row in pending if candidate_url_allowed(row["url"], source)]
        refresh = [row for row in refresh if candidate_url_allowed(row["url"], source)]
    seen = set(prior.get("seen_urls") or []) | {x["url"] for x in pending + refresh}
    artifacts, errors, latest = [], [], []
    index_refreshed = False
    try:
        response = context.client.get(source["url"], max_bytes=context.max_bytes)
        latest = feed_candidates(response.body, source) if source["adapter"] == "youtube_channel_rss" else article_candidates(response.body, source)
        artifacts.append(Artifact(data=response.body, media_type="application/atom+xml" if source["adapter"] == "youtube_channel_rss" else "text/html",
                                  role="discovery_response", source_url=response.url))
        index_refreshed = True
        if not latest:
            errors.append("Current index/feed produced no eligible candidates; empty discovery is not accepted as complete history")
    except Exception as error:
        errors.append("Current index refresh failed: " + type(error).__name__ + ": " + str(error))
    new = [row for row in latest if row["url"] not in seen]
    seen.update(row["url"] for row in latest)
    if not latest and not pending and not refresh:
        return Batch(status="unavailable" if not index_refreshed else "partial", cursor=None, artifacts=artifacts, errors=errors,
                     coverage=coverage("first_party_index", window_complete=False, candidate_count=0, current_index_refreshed=index_refreshed))

    # Every invocation checks fresh discovery. Slots also serve backlog and edits,
    # so a large initial archive cannot indefinitely delay new publications.
    take = min(context.max_items, source.get("options", {}).get("max_child_fetches", context.max_items))
    groups = [("new", new), ("backfill", pending), ("refresh", refresh)]
    selected, selected_urls = [], set()
    for label, rows in groups:
        if len(selected) >= take:
            break
        candidate = next((r for r in rows if r["url"] not in selected_urls), None)
        if candidate:
            selected.append((label, candidate))
            selected_urls.add(candidate["url"])
    for label, rows in groups:
        for candidate in rows:
            if len(selected) >= take:
                break
            if candidate["url"] not in selected_urls:
                selected.append((label, candidate))
                selected_urls.add(candidate["url"])

    records, failed, succeeded, attempted_urls = [], [], [], set()
    processed = {"new": 0, "backfill": 0, "refresh": 0}
    for index, (queue_kind, candidate) in enumerate(selected):
        remaining_seconds = context.remaining_seconds()
        if remaining_seconds < 5:
            break
        attempted_urls.add(candidate["url"])
        child = {**source, "url": candidate["url"], "title": candidate["title"],
                 "adapter": "youtube_video" if source["adapter"] == "youtube_channel_rss" else source.get("options", {}).get("candidate_adapter", "html_article"),
                 "kind": "video_transcript" if source["adapter"] == "youtube_channel_rss" else "article"}
        # Index identity/work identity must never be inherited by each child.
        for key in ["identity_key", "canonical_url", "work_id"]:
            child.pop(key, None)
        options = source.get("options", {})
        if candidate["url"] in options.get("work_id_by_url", {}):
            child["work_id"] = options["work_id_by_url"][candidate["url"]]
        if candidate["url"] in options.get("person_ids_by_url", {}):
            child["person_ids"] = options["person_ids_by_url"][candidate["url"]]
        # Preserve the original start and total deadline across every child.
        subcontext = replace(context, artifact_dir=context.artifact_dir / ("candidate-" + str(index)))
        batch = collect(child, subcontext)
        records.extend(batch.records)
        artifacts.extend(batch.artifacts)
        errors.extend(batch.errors)
        if batch.status != "ok":
            failed.append(candidate)
        else:
            succeeded.append(candidate)
            processed[queue_kind] += 1

    def unique(rows):
        found = set()
        result = []
        for row in rows:
            if row["url"] not in found:
                found.add(row["url"])
                result.append(row)
        return result

    backlog = unique([r for r in pending + new if r["url"] not in attempted_urls] + failed)
    backlog_urls = {r["url"] for r in backlog}
    refresh = unique([r for r in refresh if r["url"] not in attempted_urls and r["url"] not in backlog_urls] + succeeded)
    cursor = {"pending_urls": backlog, "refresh_queue": refresh, "seen_urls": sorted(seen), "index_url": source["url"],
              "excluded_candidates": excluded,
              "current_index_checked_at": context.now if index_refreshed else prior.get("current_index_checked_at"),
              "backfill_window_start": prior.get("backfill_window_start", context.window_start),
              "backfill_window_end": prior.get("backfill_window_end", context.window_end)}
    return Batch(records=records, artifacts=artifacts, status="partial", cursor=cursor, errors=errors,
                 coverage=coverage("first_party_index_or_recent_feed", window_complete=False if backlog else None,
                                   current_index_refreshed=index_refreshed, current_index_candidates=len(latest),
                                   newly_discovered_count=len(new), new_items_processed=processed["new"],
                                   newly_discovered_pending_count=sum(r["url"] in {n["url"] for n in new} for r in backlog),
                                   backfill_items_processed=processed["backfill"], refresh_items_processed=processed["refresh"],
                                   fetched_count=len(records), pending_count=len(backlog), refresh_queue_count=len(refresh),
                                   pending_candidates=backlog, backfill_complete=not backlog,
                                   excluded_prior_candidate_count=len(excluded), excluded_prior_candidates=excluded,
                                   limitation="Current index/feed is refreshed every invocation. New URLs, older pending captures, and rotating edit checks have separate slots. Visible-page pagination/unlisted items and full date-window/corpus coverage remain unverified."))


def collect(source: dict, context: CollectionContext) -> Batch:
    try:
        if context.max_items < 1:
            return Batch(status="partial", cursor=context.cursor, errors=["Source item budget is zero"], coverage=coverage("budget", window_complete=False))
        if context.remaining_seconds() <= 0:
            return Batch(status="partial", cursor=context.cursor, errors=["Source time budget exhausted"], coverage=coverage("budget", window_complete=False))
        if source["adapter"] in {"html_article", "publisher_transcript"}:
            return article(source, context)
        if source["adapter"] == "youtube_video":
            return youtube(source, context)
        if source["adapter"] in {"html_index", "youtube_channel_rss"}:
            return discover(source, context)
        if source["adapter"] == "local_asr_excerpt":
            from media.local_asr import collect_excerpt
            return collect_excerpt(source, context)
        raise ValueError("Unknown media adapter: " + str(source["adapter"]))
    except Exception as error:
        return Batch(status="unavailable", cursor=None, errors=[type(error).__name__ + ": " + str(error)],
                     coverage=coverage("retrieval_attempt", window_complete=False, source_url=source.get("url"),
                                       limitation="A failed read does not establish deletion or lack of new content."))
