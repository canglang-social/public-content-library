"""Preserve a public PDF and extract searchable text with the local Poppler tool."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import time

from collector.models import Artifact, Batch, Record
from collector.http import LimitReached


def collect(source, context):
    started = time.monotonic()
    options = source.get("options", {})
    canonical = source.get("canonical_url") or options.get("canonical_url") or source["url"]
    coverage = {"scope": "registered_pdf", "window_complete": False, "corpus_complete": False}
    try:
        response = context.client.get(source["url"], max_bytes=context.max_bytes, accept="application/pdf")
        body = response.body
        if b"%PDF-" not in body[:1024]:
            raise ValueError("The response is not a recognizable PDF; no PDF capture accepted")
        digest = hashlib.sha256(body).hexdigest()
        scratch = Path(context.artifact_dir)
        scratch.mkdir(parents=True, exist_ok=True)
        pdf = scratch / (digest + ".pdf")
        pdf.write_bytes(body)
        text_path = scratch / (digest + ".txt")
        binary = shutil.which("pdftotext")
        text = ""
        extraction_status = "tool_unavailable"
        diagnostics = "pdftotext is not installed"
        if binary:
            remaining = context.max_seconds - (time.monotonic() - started)
            if remaining <= 1:
                extraction_status = "time_cap_reached"
                diagnostics = "PDF was saved; remaining source time was insufficient for text extraction"
            else:
                try:
                    process = subprocess.run([binary, "-enc", "UTF-8", "-layout", str(pdf), str(text_path)], capture_output=True, text=True, timeout=min(60, remaining))
                    diagnostics = process.stderr[:2000]
                    if process.returncode:
                        extraction_status = "extractor_failed"
                    elif text_path.stat().st_size > 16_000_000:
                        extraction_status = "text_size_cap_reached"
                    else:
                        text = text_path.read_text(encoding="utf-8").strip()
                        extraction_status = "complete" if text else "no_extractable_text"
                except subprocess.TimeoutExpired:
                    extraction_status = "extractor_timed_out"
                    diagnostics = "PDF saved; pdftotext exceeded the bounded extraction time"
        artifacts = [Artifact(path=pdf, media_type="application/pdf", role="original", source_url=response.url)]
        if text:
            artifacts.append(Artifact(data=text.encode("utf-8"), media_type="text/plain; charset=utf-8", role="extracted_text", source_url=canonical))
        complete = extraction_status == "complete"
        record = Record(
            canonical_url=canonical,
            identity_key=source.get("identity_key"),
            source_item_id=source.get("source_item_id"),
            work_id=source.get("work_id"),
            title=source.get("title") or canonical,
            kind=source.get("kind", "paper"),
            author=source.get("author"),
            published_at=source.get("published_at"),
            text=text,
            status="available" if complete else "metadata_only",
            review_status=source.get("review_status", "needs_review"),
            metadata={"original_sha256": digest, "original_media_type": "application/pdf", "text_extraction_status": extraction_status},
            provenance={"retrieval_url": response.url, "retrieved_at": response.retrieved_at, "extractor": "pdftotext", "extractor_path": binary, "diagnostics": diagnostics, "source_response_bytes": len(body)},
            tags=source.get("tags", []),
            artifacts=artifacts,
        )
        coverage.update(window_complete=complete, records_returned=1, original_preserved=True, text_extraction_status=extraction_status)
        return Batch(records=[record], status="ok" if complete else "partial", cursor={} if complete else None, errors=[] if complete else ["PDF text extraction: " + extraction_status], coverage=coverage)
    except LimitReached as error:
        coverage.update(cap_reached=True)
        return Batch(status="partial", cursor=None, errors=[str(error)], coverage=coverage)
    except Exception as error:
        return Batch(status="unavailable", cursor=None, errors=[str(error)], coverage=coverage)
