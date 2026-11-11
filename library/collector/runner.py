"""Deterministic, bounded runs with a process-held lock and resumable commits."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import time
import uuid

from .adapters import dispatch
from .http import Budget, FetchError, HTTPClient, LimitReached, utc_now
from .models import Artifact, Batch, CollectionContext
from .store import Store, dumps


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def state_lock(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "run.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("Another collector operation holds this state lock") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(dumps({"pid": os.getpid(), "acquired_at": utc_now()}))
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(uuid.uuid4()))
    try:
        with temporary.open("x") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_registry(registry):
    if registry.get("schema_version") != 1 or not isinstance(registry.get("sources"), list):
        raise ValueError("Registry schema_version=1 and sources list are required")
    seen = set()
    for source in registry["sources"]:
        if not all(isinstance(source.get(k), str) and source[k] for k in ("id", "adapter", "url")):
            raise ValueError("Each source needs nonempty id, adapter and url")
        if source["id"] in seen:
            raise ValueError("Duplicate registry source ID: " + source["id"])
        seen.add(source["id"])
    if not any(s.get("enabled", True) for s in registry["sources"]):
        raise ValueError("Registry must contain at least one enabled source")


def collect(registry, state, *, resume=None, resume_auto=False, source_ids=None, now=None,
            max_items=8, max_total_items=100, max_pages=2, max_bytes=25_000_000,
            max_artifact_bytes=50_000_000, max_seconds=600, max_requests=100,
            source_max_bytes=5_000_000, source_max_seconds=90,
            overlap_days=14, initial_days=30, adapter=dispatch, client_factory=HTTPClient):
    validate_registry(registry)
    requested_sources = sorted(set(source_ids)) if source_ids is not None else None
    if requested_sources == []:
        raise ValueError("A source selection must not be empty")
    limits = {"max_items_per_source": max_items, "max_total_items": max_total_items, "max_pages_per_source": max_pages,
              "max_http_bytes": max_bytes, "max_artifact_bytes": max_artifact_bytes, "max_seconds": max_seconds,
              "max_http_requests": max_requests, "source_response_bytes": source_max_bytes, "source_seconds": source_max_seconds,
              "overlap_days": overlap_days, "initial_days": initial_days}
    if any(v <= 0 for v in limits.values()):
        raise ValueError("All limits must be positive")
    with state_lock(state):
        store = Store(state, max_artifact_bytes=max_artifact_bytes)
        run_id = None
        try:
            if resume_auto and not resume:
                unfinished = store.db.execute("SELECT id FROM runs WHERE status IN ('running','interrupted') ORDER BY started_at DESC LIMIT 1").fetchone()
                resume = unfinished[0] if unfinished else None
            if resume:
                # A resumed run uses the original immutable registry, even if the editable file changed.
                old = store.db.execute("SELECT value FROM settings WHERE key=?", ("run_registry:" + resume,)).fetchone()
                if old:
                    registry = json.loads(store.resolve_artifact(old[0]).read_text())
                    validate_registry(registry)
                saved_selection = store.db.execute("SELECT value FROM settings WHERE key=?", ("run_source_ids:" + resume,)).fetchone()
                original_selection = json.loads(saved_selection[0]) if saved_selection else None
                if requested_sources is not None and requested_sources != original_selection:
                    raise ValueError("A resumed run must keep its original source selection")
                requested_sources = original_selection
            enabled_ids = {s["id"] for s in registry["sources"] if s.get("enabled", True)}
            if requested_sources is not None and not set(requested_sources) <= enabled_ids:
                raise ValueError("Selected sources must exist and be enabled in the run registry")
            store.register_sources(registry["sources"])
            store.register_registry(registry)
            registry_path = store.db.execute("SELECT value FROM settings WHERE key='registry_path'").fetchone()[0]
            run_id = store.start_run(now or utc_now(), resume, registry_path=registry_path, source_ids=requested_sources)
            end = store.db.execute("SELECT window_end FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            budget = Budget(max_bytes=max_bytes, max_seconds=max_seconds, max_requests=max_requests)
            used_items = 0
            used_artifacts = 0
            results = []
            discovery = {"html_index", "youtube_channel_rss", "x_timeline", "rss"}
            sources = [s for s in registry["sources"] if s.get("enabled", True)]
            if requested_sources is not None:
                sources = [s for s in sources if s["id"] in requested_sources]
            sources.sort(key=lambda s: (s["adapter"] not in discovery, s.get("priority", 100)))
            for source in sources:
                sid = source["id"]
                saved = store.source_state(sid)
                prior_run = store.db.execute("SELECT * FROM source_runs WHERE run_id=? AND source_id=?", (run_id, sid)).fetchone()
                if prior_run and prior_run["status"] == "ok":
                    previous_summary = json.loads(prior_run["summary_json"])
                    results.append({**previous_summary, "resumed_skip": True})
                    continue
                anchor = datetime.fromisoformat(saved["watermark"] or end)
                start = (anchor - timedelta(days=overlap_days if saved["watermark"] else initial_days)).isoformat()
                if prior_run:
                    start, source_end = prior_run["window_start"], prior_run["window_end"]
                else:
                    source_end = end
                store.begin_source(run_id, sid, start, source_end)
                options = source.get("options", {})
                count = min(max_items, int(options.get("max_items", max_items)), max_total_items - used_items)
                seconds = min(source_max_seconds, float(options.get("max_seconds", source_max_seconds)), max(0.01, budget.remaining_seconds()))
                response_bytes = min(source_max_bytes, int(options.get("max_bytes", source_max_bytes)))
                client = client_factory(budget, max_bytes=response_bytes, max_seconds=seconds)
                scratch = store.root / "scratch" / run_id / (str(uuid.uuid4()) + "-source")
                scratch.mkdir(parents=True, exist_ok=True)
                context = CollectionContext(start, source_end, saved["cursor"], max(0, count), min(max_pages, int(options.get("max_pages", max_pages))), client, scratch,
                                            run_id, sid, response_bytes, seconds, saved["pending_start"], saved["pending_end"])
                try:
                    budget.check()
                    if count < 1 or used_artifacts >= max_artifact_bytes:
                        raise LimitReached("Run item/artifact budget reached before this source")
                    batch = adapter(source, context)
                    if not isinstance(batch, Batch):
                        raise TypeError("Adapter did not return Batch")
                    if len(batch.records) > count:
                        batch.records = batch.records[:count]
                        batch.status, batch.cursor = "partial", saved["cursor"]
                        batch.errors.append("Adapter exceeded local item cap; checkpoint held. Adapter repair required before advancing.")
                        batch.coverage = {**batch.coverage, "window_complete": False, "cap_reached": True, "adapter_contract_violation": True}
                except LimitReached as exc:
                    batch = Batch(status="partial", errors=[str(exc)], coverage={"window_complete": False, "corpus_complete": False, "cap_reached": True})
                except Exception as exc:
                    batch = Batch(status="unavailable", errors=[f"{type(exc).__name__}: {exc}"], coverage={"window_complete": False, "corpus_complete": False, "item_deletion_inferred": False})
                observations = []
                for response in getattr(client, "history", []):
                    batch.artifacts.append(Artifact(data=response.body, media_type=response.headers.get("content-type", "application/octet-stream"), role="http_response", source_url=response.url))
                    observations.append({"url": response.url, "status": response.status, "retrieved_at": response.retrieved_at, "bytes": len(response.body)})
                batch.coverage = {**batch.coverage, "corpus_complete": False, "http_bytes": getattr(client, "bytes_received", 0),
                                  "http_requests": getattr(client, "request_count", 0), "http_observations": observations,
                                  "http_errors": getattr(client, "errors", []), "local_item_cap": count}
                # Disk cap includes subprocess output artifacts, although their wire bytes are separately reported.
                artifacts = batch.artifacts + [a for record in batch.records for a in record.artifacts]
                unique_sizes = {}
                for a in artifacts:
                    if a.data is not None:
                        import hashlib
                        unique_sizes[hashlib.sha256(a.data).hexdigest()] = len(a.data)
                    elif a.path is not None:
                        p = Path(a.path)
                        unique_sizes[str(p.resolve())] = p.stat().st_size
                size = sum(unique_sizes.values()) + sum(len(r.text.encode()) * 2 for r in batch.records)
                if size + used_artifacts > max_artifact_bytes:
                    batch = Batch(status="partial", errors=["Run artifact storage cap reached; source not committed, checkpoint held"], coverage={"window_complete": False, "corpus_complete": False, "cap_reached": True, "attempted_artifact_bytes": size})
                    size = 0
                result = store.commit_batch(run_id, source, batch, start, source_end, utc_now())
                used_items += result["observed_items"]
                used_artifacts += size
                results.append(result)
                atomic_json(store.root / "runs" / run_id / "progress.json", {"run_id": run_id, "status": "running", "sources": results})
            status = "ok" if all(r["status"] == "ok" for r in results) else "partial"
            summary = {"schema_version": 1, "run_id": run_id, "status": status, "resumed": bool(resume), "window_end": end,
                       "targeted": requested_sources is not None, "selected_source_ids": requested_sources,
                       "limits": limits, "limits_scope": "per_invocation; bounded retries/resume receive a fresh budget", "sources": results, "new_items": sum(r["new_items"] for r in results),
                       "changed_items": sum(r["changed_items"] for r in results), "new_versions": sum(r["new_versions"] for r in results),
                       "http_bytes": budget.bytes_received, "http_requests": budget.requests, "estimated_artifact_bytes": used_artifacts,
                       "elapsed_seconds": round(time.monotonic() - budget.started, 3), "corpus_complete": False,
                       "no_updates": status == "ok" and not any(r["new_items"] or r["changed_items"] for r in results),
                       "network_accounting": "HTTP client bytes are bounded. Media subprocess wire traffic is separately reported; output/time caps apply."}
            store.finish_run(run_id, status, summary)
            atomic_json(store.root / "runs" / run_id / "summary.json", summary)
            atomic_json(store.root / "latest-run.json", summary)
            return summary
        except BaseException as exc:
            if run_id:
                summary = {"schema_version": 1, "run_id": run_id, "status": "interrupted", "error": f"{type(exc).__name__}: {exc}", "resume_command": "run --resume-auto"}
                store.finish_run(run_id, "interrupted", summary)
                atomic_json(store.root / "latest-run.json", summary)
            raise
        finally:
            store.db.close()


def interrupt_on_signal(signum, frame):
    raise KeyboardInterrupt("Process termination requested; resume with --resume-auto")
