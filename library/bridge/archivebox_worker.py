"""Native ArchiveBox bridge; no source HTTP retrieval or media execution."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

from collector.runner import atomic_json, state_lock
from collector.store import Store, digest
from collector.presentation import context_text, searchable_text


@contextmanager
def bridge_lock(data):
    with (data / "collector-bridge.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def immutable_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError("Existing immutable rendition differs: " + str(path))
        return
    temporary = path.with_name(".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o444)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("Concurrent rendition content differs")
    finally:
        temporary.unlink(missing_ok=True)


def load_state(data):
    path = data / "collector-bridge.json"
    return json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "items": {}}


def verify_publication(output, publication):
    if not publication.get("files"):
        raise ValueError("Publication file hash manifest is missing")
    for entry in publication["files"]:
        relative = Path(entry["path"])
        path = (output / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(output.resolve()) or not path.is_file() or file_hash(path) != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            raise ValueError("Published rendition integrity mismatch: " + str(relative))


def user_changes(item_state, Snapshot):
    changed_notes = []
    additions = set(item_state.get("ui_added_tags", []))
    removals = set(item_state.get("ui_removed_tags", []))
    for version_id, saved in item_state.get("versions", {}).items():
        snapshot = Snapshot.objects.filter(pk=saved["snapshot_id"]).first()
        if not snapshot:
            continue
        current_tags = set(snapshot.tags.values_list("name", flat=True))
        old_tags = set(saved.get("last_written_tags", []))
        added = {tag for tag in current_tags - old_tags if not tag.startswith("library:")}
        removed = {tag for tag in old_tags - current_tags if not tag.startswith("library:")}
        additions = (additions | added) - removed
        removals = (removals | removed) - added
        if snapshot.notes != saved.get("last_written_notes", ""):
            changed_notes.append((snapshot.modified_at.isoformat(), version_id, snapshot.notes))
        saved["observed_notes"] = snapshot.notes
        saved["observed_tags"] = sorted(current_tags)
    changed_notes.sort()
    notes = item_state.get("ui_notes_override")
    if changed_notes:
        unique = list(dict.fromkeys(value[2] for value in changed_notes))
        notes = unique[0] if len(unique) == 1 else "\n\n".join("ArchiveBox note " + str(i + 1) + ":\n" + note for i, note in enumerate(unique))
    item_state.update(ui_notes_override=notes, ui_added_tags=sorted(additions), ui_removed_tags=sorted(removals))
    return bool(changed_notes or additions or removals)


def merged_manual(item_state, manual):
    previous = item_state.get("neutral_manual", {"notes": "", "tags": [], "review_status": None})
    notes = manual.get("notes", "")
    override = item_state.get("ui_notes_override")
    conflict = False
    if override is not None:
        if previous.get("notes", "") != notes and override != notes:
            notes = "Library note:\n" + notes + "\n\nArchiveBox note:\n" + override
            conflict = True
        else:
            notes = override
    tags = (set(manual.get("tags", [])) | set(item_state.get("ui_added_tags", []))) - set(item_state.get("ui_removed_tags", []))
    return {"notes": notes, "tags": sorted(tags), "review_status": manual.get("review_status")}, conflict


def source_urls(item, version):
    urls = {item["canonical_url"], version["content"]["canonical_url"]}
    provenance = version.get("provenance", {})
    for key in ["registered_source_url", "retrieved_url", "endpoint"]:
        if provenance.get(key, "").startswith(("https://", "http://")):
            urls.add(provenance[key])
    urls.update(a["source_url"] for a in version.get("artifacts", []) if isinstance(a.get("source_url"), str) and a["source_url"].startswith(("https://", "http://")))
    return sorted(urls)


REVIEW_PREFIXES = {"needs_review": "[Review needed] ", "included": "", "excluded": "[Excluded] "}


def managed_title(content, review_status, *, custom_title=None):
    quality = "" if content["status"] == "available" else "[" + content["status"].replace("_", " ") + "] "
    body = (content["title"] or content["canonical_url"]) if custom_title is None else custom_title
    return (REVIEW_PREFIXES[review_status] + quality + body)[:512]


def current_snapshot_title(snapshot, saved, content, review_status, *, is_new):
    current = snapshot.title
    if "last_written_title" in saved:
        user_edited = current != saved["last_written_title"]
    else:
        # Older ledgers did not track titles. Only exact old generated forms are
        # considered managed; an arbitrary existing title remains user-authored.
        generated = {
            snapshot._normalize_title_candidate(managed_title(content, status), snapshot_url=snapshot.url) or None
            for status in REVIEW_PREFIXES
        }
        user_edited = not is_new and current not in generated
    if user_edited:
        body = current or ""
        for prefix in ("[Review needed] ", "[Excluded] "):
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        quality = "" if content["status"] == "available" else "[" + content["status"].replace("_", " ") + "] "
        if quality and body.startswith(quality):
            body = body[len(quality):]
        saved["custom_title"] = body
    return managed_title(content, review_status, custom_title=saved.get("custom_title"))


def rendition(item, version, artifacts, *, format_version=1):
    content = version["content"]
    status = content["status"]
    title = content["title"] or item["canonical_url"]
    links = "".join('<li><a rel="noreferrer" href="' + html.escape(url, quote=True) + '">' + html.escape(url) + "</a></li>" for url in source_urls(item, version))
    files = "".join('<li><a href="' + html.escape(a["local_path"], quote=True) + '">' + html.escape(a["role"]) + " — " + html.escape(a["media_type"]) + "</a></li>" for a in artifacts)
    detail = {"item_id": item["id"], "version_id": version["id"], "fingerprint": version["fingerprint"], "person_ids": item.get("person_ids", []), "work_id": item.get("work_id"), "review_status_at_publication": item["review_status"], "source_status_at_publication": item.get("source_status", []), "manual_annotations_at_publication": item.get("manual", {}), "provenance": version.get("provenance", {})}
    segments = ""
    if content.get("segments"):
        segments = '<details><summary>Timed transcript</summary><pre>' + html.escape(json.dumps(content["segments"], ensure_ascii=False, indent=2)) + "</pre></details>"
    if format_version >= 3 and context_text(content):
        segments += '<h2>Conversation context</h2><pre>' + html.escape(context_text(content)) + '</pre>'
    review_label = "Review at publication" if format_version >= 2 else "Review"
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>" + html.escape(title) + "</title><style>body{font:18px/1.65 system-ui,sans-serif;max-width:58rem;margin:3rem auto;padding:0 1.5rem;color:#20242b}h1{line-height:1.2}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.status{padding:1rem;background:#f0f3f6}a{overflow-wrap:anywhere}summary{cursor:pointer}details{margin:1.5rem 0}</style></head><body><h1>" + html.escape(title) + '</h1><p class="status">Capture quality: <strong>' + html.escape(status) + "</strong>. " + review_label + ": " + html.escape(item["review_status"]) + ". This is a preserved library rendition; source access and coverage are recorded separately.</p><p>Author: " + html.escape(content.get("author") or "Not specified") + ". Published: " + html.escape(content.get("published_at") or "Not specified") + ".</p><h2>Source links</h2><ul>" + links + "</ul><h2>Captured text</h2><pre>" + html.escape(content.get("text") or "No body text was available for this version. This is a metadata-only or unavailable capture.") + "</pre>" + segments + "<h2>Preserved source files</h2><ul>" + files + '</ul><details><summary>Provenance and annotations at publication</summary><pre>' + html.escape(json.dumps(detail, ensure_ascii=False, indent=2)) + "</pre></details></body></html>").encode()


def load_indexer(data):
    import abx_plugins
    plugin = Path(abx_plugins.__file__).parent / "plugins/search_backend_sqlite/on_Snapshot__90_index_sqlite.py"
    os.environ["DATA_DIR"] = str(data)
    os.environ["SNAP_DIR"] = str(data / "tmp/bridge-indexer")
    spec = importlib.util.spec_from_file_location("collector_archivebox_native_indexer", plugin)
    module = importlib.util.module_from_spec(spec)
    original = Path.cwd()
    try:
        spec.loader.exec_module(module)
    finally:
        os.chdir(original)
    return module


def publish(data, bundle_path):
    from archivebox.core.models import Snapshot, ArchiveResult
    from archivebox.crawls.models import Crawl
    from archivebox.base_models.models import get_or_create_system_user_pk
    from django.utils import timezone as django_timezone
    bundle = json.loads(bundle_path.read_text())
    if bundle.get("schema_version") != 1:
        raise ValueError("Unsupported neutral export schema")
    artifact_root = Path(bundle["artifact_root"]).resolve()
    state = load_state(data)
    if state.get("collector_state_id") not in {None, bundle["state_id"]}:
        raise ValueError("This ArchiveBox collection is already associated with a different neutral state")
    state["collector_state_id"] = bundle["state_id"]
    indexer = load_indexer(data)
    created = updated = conflicts = 0
    total = 0
    for item in bundle["items"]:
        info = state["items"].setdefault(item["id"], {"versions": {}})
        user_changes(info, Snapshot)
        manual, conflict = merged_manual(info, item["manual"])
        conflicts += int(conflict)
        if conflict:
            info["ui_notes_override"] = manual["notes"]
        info["neutral_manual"] = item["manual"]
        info["effective_manual"] = manual
        info["latest_version_id"] = item["latest_version_id"]
        for version in item["versions"]:
            version_id = version["id"]
            content = version["content"]
            sid = uuid.uuid5(uuid.NAMESPACE_URL, os.environ.get("PCL_ID_NAMESPACE", "public-content-library") + ":" + bundle["state_id"] + ":snapshot:" + version_id)
            snapshot = Snapshot.objects.filter(pk=sid).first()
            is_new = snapshot is None
            if snapshot is not None and version_id not in info["versions"]:
                # A crash may leave a native row before bridge bookkeeping. Preserve its
                # potentially user-authored annotations before establishing a baseline.
                if snapshot.notes and snapshot.notes != manual["notes"]:
                    manual["notes"] = ("Library note:\n" + manual["notes"] + "\n\nRecovered ArchiveBox note:\n" + snapshot.notes) if manual["notes"] else snapshot.notes
                    info["ui_notes_override"] = manual["notes"]
                    conflicts += 1
                recovered_tags = {tag for tag in snapshot.tags.values_list("name", flat=True) if not tag.startswith("library:")}
                manual["tags"] = sorted(set(manual["tags"]) | recovered_tags)
                info["ui_added_tags"] = sorted(set(info.get("ui_added_tags", [])) | recovered_tags)
            if is_new:
                crawl_id = uuid.uuid5(uuid.NAMESPACE_URL, os.environ.get("PCL_ID_NAMESPACE", "public-content-library") + ":" + bundle["state_id"] + ":crawl:" + version_id)
                crawl = Crawl.objects.filter(pk=crawl_id).first()
                if crawl is None:
                    crawl = Crawl(id=crawl_id, urls=item["canonical_url"], max_depth=0, created_by_id=get_or_create_system_user_pk(), label="Neutral library version " + version_id[:12], status="sealed", retry_at=None, config={"PLUGINS": "search_backend_sqlite"})
                    crawl.save()
                snapshot = Snapshot(id=sid, crawl=crawl, url=content["canonical_url"], title=managed_title(content, item["review_status"]), notes=manual["notes"], status="paused", retry_at=None, fs_version=Snapshot._fs_current_version(), config={"PERMISSIONS": "public", "PLUGINS": "search_backend_sqlite"})
                snapshot.save()
                created += 1
            saved = info["versions"].setdefault(version_id, {"snapshot_id": str(snapshot.id), "last_written_notes": snapshot.notes, "last_written_tags": list(snapshot.tags.values_list("name", flat=True))})
            output = Path(snapshot.output_dir) / "collector"
            complete_marker = output / "publication.json"
            if not complete_marker.exists():
                # Pin the first publication inputs before writing derived files so a crashed
                # publication can resume despite later retrieval times or annotation edits.
                intent_path = output / "publication-intent.json"
                if not intent_path.exists():
                    immutable_write(intent_path, json.dumps({"format_version": 3, "item": item, "version": version}, ensure_ascii=False, sort_keys=True).encode())
                intent = json.loads(intent_path.read_text())
                publication_item, publication_version = intent["item"], intent["version"]
                if publication_version["id"] != version_id or publication_version["fingerprint"] != version["fingerprint"]:
                    raise ValueError("Interrupted publication identity mismatch")
                preserved = []
                for artifact in publication_version.get("artifacts", []):
                    relative = Path(artifact["relative_path"])
                    original = (artifact_root / relative).resolve()
                    if relative.is_absolute() or not original.is_relative_to(artifact_root / "artifacts") or not original.is_file():
                        raise ValueError("Source artifact is outside the neutral object store")
                    raw_bytes = original.read_bytes()
                    if hashlib.sha256(raw_bytes).hexdigest() != artifact["sha256"]:
                        raise ValueError("Neutral artifact hash mismatch")
                    suffix = mimetypes.guess_extension(artifact["media_type"].split(";")[0]) or ".bin"
                    local = "sources/" + artifact["sha256"] + suffix
                    immutable_write(output / local, raw_bytes)
                    descriptor = {k: v for k, v in artifact.items() if k != "path"}
                    descriptor["local_path"] = local
                    preserved.append(descriptor)
                rendered_text = searchable_text(content) if int(intent.get("format_version", 1)) >= 3 else content.get("text", "")
                immutable_write(output / "content.txt", rendered_text.encode())
                immutable_write(output / "content.json", json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2).encode())
                immutable_write(output / "provenance.json", json.dumps({"item_id": item["id"], "version_id": version_id, "source_urls": source_urls(publication_item, publication_version), "provenance": publication_version.get("provenance", {}), "manual_at_publication": publication_item["manual"], "preserved_artifacts": preserved}, ensure_ascii=False, indent=2).encode())
                immutable_write(output / "content.html", rendition(publication_item, publication_version, preserved, format_version=int(intent.get("format_version", 1))))
                files = [{"path": str(p.relative_to(output)), "sha256": file_hash(p), "bytes": p.stat().st_size} for p in sorted(output.rglob("*")) if p.is_file() and not p.name.startswith(".tmp-")]
                immutable_write(complete_marker, json.dumps({"item_id": item["id"], "version_id": version_id, "fingerprint": version["fingerprint"], "source_url": content["canonical_url"], "files": files}, sort_keys=True).encode())
            publication = json.loads(complete_marker.read_text())
            if publication["version_id"] != version_id or publication["fingerprint"] != version["fingerprint"]:
                raise ValueError("Publication identity mismatch")
            verify_publication(output, publication)
            tags = set(item.get("generated_tags", [])) | set(manual["tags"])
            tags -= set(info.get("ui_removed_tags", []))
            tags.discard("library:public-professional")
            if item["review_status"] == "included":
                tags.add("library:public-professional")
            tags |= {"library:quality:" + content["status"], "library:review:" + item["review_status"], "library:kind:" + content["kind"]}
            tags.add("library:latest" if version_id == item["latest_version_id"] else "library:history")
            tags.update("library:person:" + p.removeprefix("person:") for p in item.get("person_ids", []))
            # Unknown edits made before bridge bookkeeping are kept as manual extras.
            current_tags = set(snapshot.tags.values_list("name", flat=True))
            tags |= {t for t in current_tags - set(saved.get("last_written_tags", [])) if not t.startswith("library:")}
            snapshot.notes = manual["notes"]
            snapshot.title = current_snapshot_title(snapshot, saved, content, item["review_status"], is_new=is_new)
            snapshot.status = "sealed"
            snapshot.retry_at = None
            snapshot.downloaded_at = snapshot.downloaded_at or django_timezone.now()
            snapshot.save(update_fields=["notes", "title", "status", "retry_at", "downloaded_at"])
            snapshot.save_tags(sorted(tags))
            result, _ = ArchiveResult.get_or_create_by_hook(snapshot, "collector", "collector_version_publish", defaults={"status": "succeeded"})
            result.output_str = "collector/content.html"
            result.output_json = {"neutral_item_id": item["id"], "neutral_version_id": version_id, "capture_quality": content["status"], "source_url": content["canonical_url"], "adapter_rendition": True}
            result.status = "succeeded"
            result.start_ts = result.start_ts or django_timezone.now()
            result.end_ts = result.end_ts or django_timezone.now()
            result.save()
            result.update_output_metadata_from_filesystem(snapshot_dir=snapshot.output_dir, full_scan=True)
            from abx_plugins.plugins.search_backend_sqlite.search import flush
            compact_id = uuid.UUID(str(snapshot.id)).hex
            # Newly constructed UUIDs stringify with hyphens; loaded native IDs do not.
            # Normalize both paths and remove a prior alias before idempotent indexing.
            flush([str(uuid.UUID(str(snapshot.id)))], environ={**os.environ, "DATA_DIR": str(data)})
            indexer.index_in_sqlite(compact_id, snapshot.url, snapshot.title or "", [searchable_text(content)])
            snapshot.write_index_jsonl()
            snapshot.write_json_details()
            snapshot.write_html_details()
            saved.update(snapshot_id=str(snapshot.id), last_written_notes=snapshot.notes, last_written_tags=sorted(tags), last_written_title=snapshot.title, fingerprint=version["fingerprint"], output_relative_path=str(Path(snapshot.output_dir).relative_to(data)))
            updated += int(not is_new)
            total += 1
            atomic_json(data / "collector-bridge.json", state)
    summary = {"bridge_schema": 1, "status": "ok", "data": str(data), "new_snapshots": created, "existing_snapshots": updated, "published_versions": total, "annotation_conflicts_preserved": conflicts, "network_source_fetches": 0, "method": "Native Django Snapshot/ArchiveResult models and installed SQLite search plugin; immutable neutral renditions", "source_state_id": bundle["state_id"]}
    atomic_json(data / "collector-publish-result.json", summary)
    return summary


def inspect(data):
    from archivebox.core.models import Snapshot
    from abx_plugins.plugins.search_backend_sqlite.search import search
    rows = []
    for snapshot in Snapshot.objects.order_by("created_at"):
        directory = Path(snapshot.output_dir)
        publication = directory / "collector/publication.json"
        files = [{"path": str(p.relative_to(directory)), "sha256": file_hash(p), "bytes": p.stat().st_size} for p in sorted((directory / "collector").rglob("*")) if p.is_file()]
        rows.append({"id": str(snapshot.id), "url": snapshot.url, "title": snapshot.title, "notes": snapshot.notes, "tags": sorted(snapshot.tags.values_list("name", flat=True)), "output_dir": str(directory), "publication": json.loads(publication.read_text()) if publication.exists() else None, "files": files})
    return {"bridge_schema": 1, "status": "ok", "snapshots": rows, "queries": {q: search(q, environ={**os.environ, "DATA_DIR": str(data)}) for q in ["orchestrator", "RLAIF", "terminal"]}}


def pull(data, neutral_state):
    from archivebox.core.models import Snapshot
    state = load_state(data)
    changes = []
    conflicts = []
    with state_lock(neutral_state):
        store = Store(neutral_state)
        try:
            bundle = store.export()
            if state.get("collector_state_id") not in {None, bundle["state_id"]}:
                raise ValueError("Annotation source state ID mismatch")
            for item in bundle["items"]:
                info = state["items"].get(item["id"])
                if not info:
                    continue
                user_changes(info, Snapshot)
                manual, conflict = merged_manual(info, item["manual"])
                if manual != item["manual"]:
                    store.annotate(item["id"], notes=manual["notes"], tags=manual["tags"], review_status=manual["review_status"])
                    changes.append(item["id"])
                if conflict:
                    conflicts.append(item["id"])
                info["neutral_manual"] = manual
                info["ui_notes_override"] = None
                info["ui_added_tags"] = []
                # Retain removal overlay for generated tags; manual tags are already reconciled.
                info["ui_removed_tags"] = [t for t in info.get("ui_removed_tags", []) if t in item.get("generated_tags", [])]
                for saved in info.get("versions", {}).values():
                    saved["last_written_notes"] = saved.get("observed_notes", saved.get("last_written_notes", ""))
                    saved["last_written_tags"] = saved.get("observed_tags", saved.get("last_written_tags", []))
            atomic_json(data / "collector-bridge.json", state)
        finally:
            store.db.close()
    return {"bridge_schema": 1, "status": "ok", "annotations_pulled": len(changes), "item_ids": changes, "conflicts_preserved": conflicts, "conflict_policy": "Preserve both independently edited notes with labels; union additions and honor UI removals for manual tags"}


def backup(data, output):
    if output.exists() or output.is_relative_to(data):
        raise ValueError("Backup destination must be a new directory outside this collection")
    from archivebox.core.models import Snapshot
    registered = load_state(data)
    for item in registered.get("items", {}).values():
        for version in item.get("versions", {}).values():
            snapshot = Snapshot.objects.filter(pk=version["snapshot_id"]).first()
            if snapshot is None:
                raise ValueError("Backup refuses a missing registered bridge snapshot")
            marker = Path(snapshot.output_dir) / "collector/publication.json"
            if not marker.is_file():
                raise ValueError("Backup refuses a missing registered publication marker")
            verify_publication(marker.parent, json.loads(marker.read_text()))
    for snapshot in Snapshot.objects.all():
        marker = Path(snapshot.output_dir) / "collector/publication.json"
        if marker.exists():
            verify_publication(marker.parent, json.loads(marker.read_text()))
    output.mkdir(parents=True)
    materialized = []
    expected_copies = {}
    database_names = {"index.sqlite3", "search.sqlite3"}
    excluded = {"tmp", "logs", "collector-bridge.lock"}
    receipt_name = "bridge-backup-manifest.json"
    def copy_entry(source, destination, ancestors):
        actual = source.resolve(strict=True)
        if not actual.is_relative_to(data):
            raise ValueError("Backup refuses an external symlink target: " + str(source))
        if source.is_symlink():
            materialized.append(str(source.relative_to(data)))
        if actual.is_dir():
            if actual in ancestors:
                raise ValueError("Backup refuses symlink cycle")
            destination.mkdir()
            for child in actual.iterdir():
                if actual == data and (child.name in excluded or child.name in database_names or child.name.endswith(("-wal", "-shm"))):
                    continue
                if child.name.endswith((".pid", ".sock")):
                    continue
                copy_entry(child, destination / child.name, ancestors | {actual})
        elif actual.is_file():
            expected = file_hash(actual)
            shutil.copy2(actual, destination)
            if file_hash(destination) != expected:
                raise ValueError("Source-to-backup hash mismatch: " + str(source))
            expected_copies[str(destination.relative_to(output))] = expected
        else:
            raise ValueError("Backup refuses special file")
    for child in data.iterdir():
        if child.name == receipt_name or child.name in excluded or child.name in database_names or child.name.endswith(("-wal", "-shm")):
            continue
        copy_entry(child, output / child.name, frozenset({data}))
    # A restored collection contains its previous backup receipt. Preserve its
    # exact bytes under a content-addressed history path before generating the
    # new reserved receipt; never hash a file and then overwrite it in-place.
    previous_receipt = None
    old_receipt = data / receipt_name
    if old_receipt.exists() or old_receipt.is_symlink():
        actual = old_receipt.resolve(strict=True)
        if not actual.is_relative_to(data) or not actual.is_file():
            raise ValueError("Backup receipt must resolve to a file inside the collection")
        previous_hash = file_hash(actual)
        relative = "bridge-backup-receipts/" + previous_hash + ".json"
        destination = output / relative
        destination.parent.mkdir(exist_ok=True)
        if destination.exists() and (not destination.is_file() or file_hash(destination) != previous_hash):
            raise ValueError("Backup receipt history conflicts with an existing file")
        copy_entry(old_receipt, destination, frozenset({data}))
        previous_receipt = {"source_path": receipt_name, "preserved_path": relative, "sha256": previous_hash}
    for name in database_names:
        if (data / name).is_file():
            source_db = sqlite3.connect(f"file:{data / name}?mode=ro", uri=True)
            target_db = sqlite3.connect(output / name)
            try:
                source_db.backup(target_db)
            finally:
                target_db.close()
                source_db.close()
    files = [{"path": str(p.relative_to(output)), "bytes": p.stat().st_size, "sha256": file_hash(p)} for p in sorted(output.rglob("*")) if p.is_file()]
    manifest = {"bridge_schema": 1, "created_at": datetime.now(timezone.utc).isoformat(), "files": files, "materialized_internal_links": materialized, "method": "Locked publisher, SQLite backup API, internal-link materialization, assets and annotation overlay"}
    if previous_receipt:
        manifest["previous_receipt"] = previous_receipt
    atomic_json(output / "bridge-backup-manifest.json", manifest)
    for entry in files:
        path = (output / entry["path"]).resolve()
        if not path.is_relative_to(output) or file_hash(path) != entry["sha256"]:
            raise ValueError("Backup verification failed")
        if entry["path"] in expected_copies and entry["sha256"] != expected_copies[entry["path"]]:
            raise ValueError("Backup differs from expected source hash")
    for marker in output.rglob("collector/publication.json"):
        verify_publication(marker.parent, json.loads(marker.read_text()))
    for name in database_names:
        if (output / name).exists():
            connection = sqlite3.connect(f"file:{output / name}?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup database integrity failed")
            finally:
                connection.close()
    return {"bridge_schema": 1, "status": "ok", "backup": str(output), "files_verified": len(files), "materialized_links": len(materialized), "database_integrity": "ok", "annotation_overlay_included": (output / "collector-bridge.json").is_file(), "external_symlinks": False}


def credentials(data, login_file, username):
    import re
    import secrets
    from django.contrib.auth import get_user_model
    from django.db import transaction
    login_file = login_file.resolve()
    if login_file.is_relative_to(data) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise ValueError("Login file must be outside the served collection; username must be simple")
    User = get_user_model()
    existing = User.objects.filter(username=username).first()
    if login_file.exists():
        values = dict(line.split(": ", 1) for line in login_file.read_text().splitlines() if ": " in line)
        if not existing or values.get("Username") != username or not existing.check_password(values.get("Password", "")):
            raise ValueError("Existing login file/account do not match; no credentials were changed")
        login_file.chmod(0o600)
        return {"bridge_schema": 1, "status": "ok", "created": False, "username": username, "login_file": str(login_file), "password_printed": False}
    if existing:
        raise ValueError("A user with this name already exists; no password reset was performed")
    login_file.parent.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)
    temporary = login_file.with_name(".login-" + uuid.uuid4().hex)
    text = "Local ArchiveBox login\nURL: " + os.environ.get("BASE_URL", "http://127.0.0.1:8864") + "/admin/login/\nUsername: " + username + "\nPassword: " + password + "\n\nThis local account can edit tags and notes. Keep this file private.\n"
    try:
        with transaction.atomic():
            User.objects.create_superuser(username=username, email="", password=password)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, login_file)
    finally:
        temporary.unlink(missing_ok=True)
    return {"bridge_schema": 1, "status": "ok", "created": True, "username": username, "login_file": str(login_file), "password_printed": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["publish", "inspect", "pull", "backup", "credentials"])
    parser.add_argument("--data", required=True)
    parser.add_argument("--bundle")
    parser.add_argument("--state")
    parser.add_argument("--output")
    parser.add_argument("--login-file")
    parser.add_argument("--username", default="library")
    args = parser.parse_args()
    data = Path(args.data).resolve()
    os.chdir(data)
    from archivebox.config.django import setup_django
    setup_django()
    with bridge_lock(data):
        if args.command == "publish":
            result = publish(data, Path(args.bundle))
        elif args.command == "pull":
            result = pull(data, Path(args.state))
        elif args.command == "backup":
            result = backup(data, Path(args.output).resolve())
        elif args.command == "credentials":
            result = credentials(data, Path(args.login_file), args.username)
        else:
            result = inspect(data)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
