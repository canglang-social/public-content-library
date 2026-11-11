"""Durable source/item/version state with immutable content and separate annotations."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit, urlunsplit
import uuid

from .http import utc_now
from .models import Artifact, Batch, Record
from .presentation import searchable_text


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Record URL must be anonymous HTTP(S)")
    host = p.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if p.port and (p.scheme, p.port) not in {("http", 80), ("https", 443)}:
        host += f":{p.port}"
    return urlunsplit((p.scheme.lower(), host, p.path or "/", p.query, ""))


VOLATILE_KEYS = {"retrieved_at", "fetched_at", "request_id", "retrieval_time", "views", "view_count", "likes", "like_count", "retweets", "retweet_count", "bookmarks", "engagement", "http_headers"}


def stable_metadata(value):
    if isinstance(value, dict):
        return {k: stable_metadata(v) for k, v in value.items() if k.lower() not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [stable_metadata(v) for v in value]
    return value


class Store:
    def __init__(self, root: str | Path, *, max_artifact_bytes: int = 25_000_000):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_artifact_bytes = max_artifact_bytes
        self.db = sqlite3.connect(self.root / "library.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,spec_json TEXT NOT NULL,cursor_json TEXT NOT NULL DEFAULT '{}',watermark TEXT,pending_start TEXT,pending_end TEXT,last_checked_at TEXT,last_status TEXT,coverage_json TEXT NOT NULL DEFAULT '{}',last_error TEXT);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,started_at TEXT NOT NULL,window_end TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,summary_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS source_runs(run_id TEXT REFERENCES runs(id),source_id TEXT REFERENCES sources(id),status TEXT NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,summary_json TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(run_id,source_id));
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,identity_key TEXT UNIQUE NOT NULL,canonical_url TEXT NOT NULL,work_id TEXT,latest_version_id TEXT,latest_complete_version_id TEXT,availability TEXT NOT NULL,source_review_status TEXT NOT NULL,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES items(id),fingerprint TEXT NOT NULL,text_hash TEXT NOT NULL,segments_hash TEXT NOT NULL,metadata_hash TEXT NOT NULL,payload_path TEXT NOT NULL,text_path TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(item_id,fingerprint));
CREATE TABLE IF NOT EXISTS item_sources(item_id TEXT REFERENCES items(id),source_id TEXT REFERENCES sources(id),source_item_id TEXT,tags_json TEXT NOT NULL,person_ids_json TEXT NOT NULL,PRIMARY KEY(item_id,source_id));
CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY,run_id TEXT REFERENCES runs(id),source_id TEXT REFERENCES sources(id),item_id TEXT REFERENCES items(id),version_id TEXT REFERENCES versions(id),observed_at TEXT NOT NULL,status TEXT NOT NULL,provenance_json TEXT NOT NULL,artifacts_json TEXT NOT NULL,UNIQUE(run_id,source_id,item_id));
CREATE TABLE IF NOT EXISTS annotations(item_id TEXT PRIMARY KEY REFERENCES items(id),notes TEXT NOT NULL DEFAULT '',tags_json TEXT NOT NULL DEFAULT '[]',review_status TEXT,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(hash TEXT PRIMARY KEY,path TEXT NOT NULL,size INTEGER NOT NULL,media_type TEXT NOT NULL);
''')
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('schema_version','1')")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('state_id',?)", (str(uuid.uuid4()),))
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS content_search USING fts5(item_id UNINDEXED,title,text)")
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def put_artifact(self, artifact: Artifact) -> dict:
        if (artifact.data is None) == (artifact.path is None):
            raise ValueError("Artifact needs exactly one of data or path")
        if artifact.path is not None:
            p = Path(artifact.path)
            if p.is_symlink() or not p.is_file() or p.stat().st_size > self.max_artifact_bytes:
                raise ValueError("Artifact byte cap exceeded")
            source = p.open("rb")
        else:
            if not isinstance(artifact.data, bytes) or len(artifact.data) > self.max_artifact_bytes:
                raise ValueError("Artifact must contain bounded bytes")
            source = io.BytesIO(artifact.data)
        objects = self.root / "artifacts"
        objects.mkdir(parents=True, exist_ok=True)
        temporary = objects / (".tmp-" + str(uuid.uuid4()))
        size, fingerprint = 0, hashlib.sha256()
        try:
            with source, temporary.open("xb") as handle:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(block)
                    if size > self.max_artifact_bytes:
                        raise ValueError("Artifact byte cap exceeded during copy")
                    fingerprint.update(block)
                    handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
            sha = fingerprint.hexdigest()
            relative = Path("artifacts") / sha[:2] / sha
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.chmod(0o444)
            try:
                os.link(temporary, target)  # atomic no-overwrite publication
            except FileExistsError:
                existing_hash = hashlib.sha256()
                if target.is_symlink() or not target.is_file():
                    raise ValueError("Existing immutable artifact failed integrity check")
                with target.open("rb") as existing:
                    for block in iter(lambda: existing.read(1024 * 1024), b""):
                        existing_hash.update(block)
                if existing_hash.hexdigest() != sha:
                    raise ValueError("Existing immutable artifact failed integrity check")
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        self.db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?)", (sha, str(relative), size, artifact.media_type))
        return {"sha256": sha, "path": str(target), "relative_path": str(relative), "bytes": size, "media_type": artifact.media_type, "role": artifact.role, "source_url": artifact.source_url}

    def register_sources(self, sources: list[dict]) -> None:
        with self.transaction():
            for source in sources:
                self.db.execute("INSERT INTO sources(id,spec_json) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET spec_json=excluded.spec_json", (source["id"], dumps(source)))

    def register_registry(self, registry: dict) -> None:
        with self.transaction():
            artifact = self.put_artifact(Artifact(data=dumps(registry).encode(), media_type="application/json", role="source_registry"))
            self.db.execute("INSERT INTO settings VALUES('registry_path',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (artifact["relative_path"],))

    def resolve_artifact(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(self.root / "artifacts") or not path.is_file():
            raise ValueError("Artifact path must resolve to an existing object inside this state")
        return path

    def artifact_descriptor(self, descriptor: dict) -> dict:
        return {**descriptor, "path": str(self.resolve_artifact(descriptor["relative_path"]))}

    def source_state(self, source_id: str) -> dict:
        row = dict(self.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())
        row["cursor"] = json.loads(row["cursor_json"])
        return row

    def start_run(self, now: str, resume: str | None = None, *, registry_path=None, source_ids=None) -> str:
        with self.transaction():
            if resume:
                row = self.db.execute("SELECT * FROM runs WHERE id=?", (resume,)).fetchone()
                if not row or row["status"] not in {"running", "interrupted"}:
                    raise ValueError("Resume requires an unfinished run ID")
                self.db.execute("UPDATE runs SET status='running',finished_at=NULL WHERE id=?", (resume,))
                return resume
            unfinished = self.db.execute("SELECT id FROM runs WHERE status IN ('running','interrupted') ORDER BY started_at DESC LIMIT 1").fetchone()
            if unfinished:
                raise ValueError(f"Unfinished run {unfinished['id']}; use --resume {unfinished['id']}")
            run_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO runs(id,started_at,window_end,status) VALUES(?,?,?,'running')", (run_id, now, now))
            if registry_path is not None:
                self.db.execute("INSERT INTO settings VALUES(?,?)", ("run_registry:" + run_id, registry_path))
                self.db.execute("INSERT INTO settings VALUES(?,?)", ("run_source_ids:" + run_id, dumps(source_ids)))
            return run_id

    def begin_source(self, run_id: str, source_id: str, start: str, end: str) -> bool:
        with self.transaction():
            previous = self.db.execute("SELECT status FROM source_runs WHERE run_id=? AND source_id=?", (run_id, source_id)).fetchone()
            if previous and previous["status"] == "ok":
                return False
            self.db.execute("INSERT INTO source_runs VALUES(?,?, 'running',?,?,'{}') ON CONFLICT(run_id,source_id) DO UPDATE SET status='running'", (run_id, source_id, start, end))
            return True

    def commit_batch(self, run_id: str, source: dict, batch: Batch, start: str, end: str, observed_at: str) -> dict:
        if batch.status not in {"ok", "partial", "unavailable"}:
            raise ValueError("Invalid batch status")
        summary = {"source_id": source["id"], "status": batch.status, "window_start": start, "window_end": end, "new_items": 0, "changed_items": 0, "unchanged_items": 0, "observed_items": 0, "new_versions": 0, "errors": list(batch.errors), "coverage": batch.coverage, "watermark_advanced": False}
        if batch.status == "ok" and batch.coverage.get("window_complete") is not True:
            summary["status"] = "partial"
            summary["errors"].append("Source-window completeness not established")
        with self.transaction():
            batch_artifacts = [self.put_artifact(a) for a in batch.artifacts]
            summary["artifacts"] = batch_artifacts
            seen = set()
            for record in batch.records:
                url = canonical_url(record.canonical_url)
                identity = record.identity_key or "url:" + url
                item_id = hashlib.sha256(identity.encode()).hexdigest()
                if item_id in seen:
                    continue
                seen.add(item_id)
                if record.status not in {"available", "truncated", "metadata_only", "unavailable", "deleted"}:
                    raise ValueError("Invalid item availability")
                if record.status == "deleted" and not record.provenance.get("deletion_evidence"):
                    raise ValueError("Deletion requires explicit evidence; HTTP failure is insufficient")
                if record.review_status not in {"included", "needs_review", "excluded"}:
                    raise ValueError("Invalid review status")
                existing = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                previous = None
                if existing and existing["latest_version_id"]:
                    pv = self.db.execute("SELECT payload_path FROM versions WHERE id=?", (existing["latest_version_id"],)).fetchone()
                    previous = json.loads(self.resolve_artifact(pv[0]).read_text())
                # Discovery excerpts attach evidence without replacing a previously captured body.
                discovery_only = bool(record.provenance.get("discovery_only") and previous and existing["latest_complete_version_id"])
                work_id = record.work_id or (existing["work_id"] if existing else None)
                metadata = stable_metadata(record.metadata)
                # A failed contextual fetch must not erase previously captured
                # text for the same referenced post. Removed/changed references
                # are not revived, and fresh complete context can still change.
                incoming_context = metadata.get("x_context")
                old_context = (previous or {}).get("metadata", {}).get("x_context")
                if isinstance(incoming_context, dict) and isinstance(old_context, dict):
                    def context_key(entry):
                        return (entry.get("relation"), entry.get("via_post_id"), entry.get("post_id"))
                    fresh_posts = {e['post_id']: e for e in incoming_context.get('entries', [])
                                   if isinstance(e, dict) and e.get('post_id') and e.get('status') == 'available' and e.get('text')}
                    old_entries = {context_key(e): e for e in old_context.get("entries", [])
                                   if isinstance(e, dict) and e.get("post_id") and e.get("status") == "available" and e.get("text")}
                    retained = []
                    for index, entry in enumerate(incoming_context.get("entries", [])):
                        if not isinstance(entry, dict):
                            continue
                        old_entry = old_entries.get(context_key(entry))
                        if old_entry and entry.get("status") in {"unavailable", "metadata_only", "truncated"}:
                            incoming_context["entries"][index] = old_entry
                            retained.append(entry.get("post_id"))
                    if retained:
                        if old_context.get('limited'):
                            incoming_context['limited'] = True
                            incoming_context['omitted_links'] = max(incoming_context.get('omitted_links', 0), old_context.get('omitted_links', 0))
                        # Failed ancestors can hide their outgoing links from the
                        # new response. Keep their known descendants as well,
                        # without reviving edges from successfully fetched nodes.
                        reachable = set(retained)
                        entries = incoming_context["entries"]
                        keys = {context_key(e) for e in entries if isinstance(e, dict)}
                        text_size = sum(len(e.get("text") or "") for e in entries if isinstance(e, dict))
                        for _ in range(3):
                            added = False
                            for child in old_context.get("entries", []):
                                if not isinstance(child, dict) or child.get("via_post_id") not in reachable or context_key(child) in keys:
                                    continue
                                # The same post can be reached through another,
                                # successfully fetched path in this observation.
                                fresh = fresh_posts.get(child.get('post_id'))
                                if fresh:
                                    child = {**fresh, **{k: child[k] for k in ('relation', 'via_post_id', 'depth') if k in child}}
                                if len(entries) >= 64 or text_size + len(child.get("text") or "") > 120000:
                                    incoming_context["limited"] = True
                                    continue
                                entries.append(child)
                                keys.add(context_key(child))
                                if not fresh:
                                    reachable.add(child.get("post_id"))
                                text_size += len(child.get("text") or "")
                                added = True
                            if not added:
                                break
                        record.provenance = {**record.provenance, "context_retained_from_version": existing["latest_version_id"],
                                             "context_retained_post_ids": retained}
                content = previous if discovery_only else {"canonical_url": url, "title": record.title, "text": record.text, "segments": record.segments, "metadata": metadata, "author": record.author or (previous or {}).get("author"), "published_at": record.published_at or (previous or {}).get("published_at"), "kind": record.kind, "work_id": work_id, "status": record.status}
                fingerprint = digest(content)
                version_id = digest({"item_id": item_id, "fingerprint": fingerprint})
                if not existing:
                    self.db.execute("INSERT INTO items(id,identity_key,canonical_url,work_id,availability,source_review_status,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?)", (item_id, identity, url, record.work_id, record.status, record.review_status, observed_at, observed_at))
                    summary["new_items"] += 1
                elif existing["latest_version_id"] != version_id:
                    summary["changed_items"] += 1
                else:
                    summary["unchanged_items"] += 1
                original_artifacts = batch_artifacts + [self.put_artifact(a) for a in record.artifacts]
                if not self.db.execute("SELECT 1 FROM versions WHERE id=?", (version_id,)).fetchone():
                    payload = self.put_artifact(Artifact(data=dumps(content).encode(), media_type="application/json", role="normalized_record"))
                    text = self.put_artifact(Artifact(data=content["text"].encode(), media_type="text/plain; charset=utf-8", role="searchable_text"))
                    self.db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?)", (version_id, item_id, fingerprint, hashlib.sha256(content["text"].encode()).hexdigest(), digest(content["segments"]), digest({k: v for k, v in content.items() if k not in {"text", "segments"}}), payload["relative_path"], text["relative_path"], observed_at))
                    summary["new_versions"] += 1
                complete = version_id if content["status"] == "available" else (existing["latest_complete_version_id"] if existing else None)
                classification = record.review_status
                if existing and existing["source_review_status"] == "included" and classification == "needs_review":
                    classification = "included"
                self.db.execute("UPDATE items SET canonical_url=?,work_id=?,latest_version_id=?,latest_complete_version_id=?,availability=?,source_review_status=?,last_seen_at=? WHERE id=?", (content["canonical_url"], work_id, version_id, complete, content["status"], classification, observed_at, item_id))
                person_ids = record.person_ids if record.person_ids is not None else source.get("person_ids", [])
                if record.person_ids is None and not person_ids:
                    previous_association = self.db.execute("SELECT person_ids_json FROM item_sources WHERE item_id=? AND source_id=?", (item_id, source["id"])).fetchone()
                    if previous_association:
                        person_ids = json.loads(previous_association[0])
                self.db.execute("INSERT INTO item_sources VALUES(?,?,?,?,?) ON CONFLICT(item_id,source_id) DO UPDATE SET source_item_id=COALESCE(excluded.source_item_id,item_sources.source_item_id),tags_json=CASE WHEN excluded.tags_json='[]' THEN item_sources.tags_json ELSE excluded.tags_json END,person_ids_json=excluded.person_ids_json", (item_id, source["id"], record.source_item_id, dumps(sorted(set(record.tags + source.get("tags", [])))), dumps(person_ids)))
                provenance = {**record.provenance, "source_id": source["id"], "registered_source_url": source.get("url"), "adapter": source["adapter"], "observed_at": observed_at}
                self.db.execute("INSERT INTO observations(run_id,source_id,item_id,version_id,observed_at,status,provenance_json,artifacts_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,source_id,item_id) DO UPDATE SET version_id=excluded.version_id,observed_at=excluded.observed_at,status=excluded.status,provenance_json=excluded.provenance_json,artifacts_json=excluded.artifacts_json", (run_id, source["id"], item_id, version_id, observed_at, record.status, dumps(provenance), dumps(original_artifacts)))
                if self.fts:
                    searchable = content
                    if complete and content["status"] in {"truncated", "metadata_only", "unavailable"}:
                        complete_path = self.db.execute("SELECT payload_path FROM versions WHERE id=?", (complete,)).fetchone()[0]
                        searchable = json.loads(self.resolve_artifact(complete_path).read_text())
                    self.db.execute("DELETE FROM content_search WHERE item_id=?", (item_id,))
                    self.db.execute("INSERT INTO content_search VALUES(?,?,?)", (item_id, searchable["title"], searchable_text(searchable)))
                summary["observed_items"] += 1
            prior = self.source_state(source["id"])
            cursor = batch.cursor if batch.cursor is not None else ({} if summary["status"] == "ok" else prior["cursor"])
            watermark = end if summary["status"] == "ok" else prior["watermark"]
            pending_start, pending_end = (prior["pending_start"] or start, prior["pending_end"] or end) if cursor and summary["status"] != "ok" else (None, None)
            summary["watermark_advanced"] = summary["status"] == "ok"
            summary["cursor_saved"] = bool(cursor)
            self.db.execute("UPDATE sources SET cursor_json=?,watermark=?,pending_start=?,pending_end=?,last_checked_at=?,last_status=?,coverage_json=?,last_error=? WHERE id=?", (dumps(cursor), watermark, pending_start, pending_end, observed_at, summary["status"], dumps(batch.coverage), "; ".join(summary["errors"]) or None, source["id"]))
            self.db.execute("UPDATE source_runs SET status=?,summary_json=? WHERE run_id=? AND source_id=?", (summary["status"], dumps(summary), run_id, source["id"]))
        return summary

    def finish_run(self, run_id: str, status: str, summary: dict) -> None:
        with self.transaction():
            self.db.execute("UPDATE runs SET status=?,finished_at=?,summary_json=? WHERE id=?", (status, utc_now(), dumps(summary), run_id))

    def annotate(self, item_id: str, *, notes=None, tags=None, review_status=None) -> None:
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
                raise ValueError("Unknown item ID")
            old = self.db.execute("SELECT * FROM annotations WHERE item_id=?", (item_id,)).fetchone()
            old = dict(old) if old else {"notes": "", "tags_json": "[]", "review_status": None}
            if review_status is not None and review_status not in {"included", "needs_review", "excluded"}:
                raise ValueError("Invalid manual review status")
            self.db.execute("INSERT INTO annotations VALUES(?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET notes=excluded.notes,tags_json=excluded.tags_json,review_status=excluded.review_status,updated_at=excluded.updated_at", (item_id, old["notes"] if notes is None else notes, old["tags_json"] if tags is None else dumps(sorted(set(tags))), review_status or old["review_status"], utc_now()))

    def status(self) -> dict:
        return {"schema_version": 1, "items": self.db.execute("SELECT count(*) FROM items").fetchone()[0], "versions": self.db.execute("SELECT count(*) FROM versions").fetchone()[0], "sources": [self.public_source(dict(r)) for r in self.db.execute("SELECT * FROM sources ORDER BY id")], "recent_runs": [dict(r) for r in self.db.execute("SELECT id,status,started_at,finished_at FROM runs ORDER BY started_at DESC LIMIT 20")]}

    @staticmethod
    def public_source(row: dict) -> dict:
        return {"id": row["id"], "status": row["last_status"], "watermark": row["watermark"], "last_checked_at": row["last_checked_at"], "cursor": json.loads(row["cursor_json"]), "pending_window": [row["pending_start"], row["pending_end"]], "coverage": json.loads(row["coverage_json"]), "error": row["last_error"]}

    def export(self) -> dict:
        output = {"schema_version": 1, "state_id": self.db.execute("SELECT value FROM settings WHERE key='state_id'").fetchone()[0], "generated_at": utc_now(), "artifact_root": str(self.root), "items": [], "sources": self.status()["sources"]}
        registry_path = self.db.execute("SELECT value FROM settings WHERE key='registry_path'").fetchone()
        output["registry"] = json.loads(self.resolve_artifact(registry_path[0]).read_text()) if registry_path else None
        for row in self.db.execute("SELECT * FROM items ORDER BY id"):
            item = dict(row)
            annotation = self.db.execute("SELECT * FROM annotations WHERE item_id=?", (item["id"],)).fetchone()
            manual = {"notes": annotation["notes"] if annotation else "", "tags": json.loads(annotation["tags_json"]) if annotation else [], "review_status": annotation["review_status"] if annotation else None}
            item["manual"] = manual
            item["annotation_fingerprint"] = digest(manual)
            item["review_status"] = manual["review_status"] or item["source_review_status"]
            associations = list(self.db.execute("SELECT * FROM item_sources WHERE item_id=?", (item["id"],)))
            item["person_ids"] = sorted({p for r in associations for p in json.loads(r["person_ids_json"])})
            item["generated_tags"] = sorted({t for r in associations for t in json.loads(r["tags_json"])})
            item["source_ids"] = [r["source_id"] for r in associations]
            item["source_status"] = [s for s in output["sources"] if s["id"] in item["source_ids"]]
            item["versions"] = []
            for v in self.db.execute("SELECT * FROM versions WHERE item_id=? ORDER BY created_at,id", (item["id"],)):
                version = dict(v)
                version["payload_relative_path"] = version["payload_path"]
                version["text_relative_path"] = version["text_path"]
                version["payload_path"] = str(self.resolve_artifact(version["payload_path"]))
                version["text_path"] = str(self.resolve_artifact(version["text_path"]))
                version["content"] = json.loads(Path(version["payload_path"]).read_text())
                obs = self.db.execute("SELECT * FROM observations WHERE version_id=? ORDER BY id DESC LIMIT 1", (v["id"],)).fetchone()
                version["provenance"] = json.loads(obs["provenance_json"]) if obs else {}
                version["artifacts"] = [self.artifact_descriptor(a) for a in json.loads(obs["artifacts_json"])] if obs else []
                item["versions"].append(version)
            output["items"].append(item)
        return output

    def search(self, query: str, limit: int = 20) -> list[dict]:
        if self.fts:
            phrase = '"' + query.replace('"', '""') + '"'
            ids = [r[0] for r in self.db.execute("SELECT item_id FROM content_search WHERE content_search MATCH ? LIMIT ?", (phrase, min(limit, 100)))]
        else:
            ids = []
            for item in self.export()["items"]:
                searchable_id = item["latest_complete_version_id"] if item["availability"] in {"truncated", "metadata_only", "unavailable"} and item["latest_complete_version_id"] else item["latest_version_id"]
                if any(query.casefold() in searchable_text(v["content"]).casefold() for v in item["versions"] if v["id"] == searchable_id):
                    ids.append(item["id"])
            ids = ids[:limit]
        return [item for item in self.export()["items"] if item["id"] in ids]
