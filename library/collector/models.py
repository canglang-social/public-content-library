"""Shared adapter contract; adapters return facts and never mutate the database."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any


@dataclass
class Artifact:
    data: bytes | None = None
    path: Path | str | None = None
    media_type: str = "application/octet-stream"
    role: str = "source_response"
    source_url: str | None = None


@dataclass
class Record:
    canonical_url: str
    title: str = ""
    text: str = ""
    identity_key: str | None = None
    source_item_id: str | None = None
    work_id: str | None = None
    kind: str = "article"
    author: str | None = None
    published_at: str | None = None
    status: str = "available"
    review_status: str = "needs_review"
    segments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    person_ids: list[str] | None = None


@dataclass
class Batch:
    records: list[Record] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    status: str = "ok"
    cursor: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)


@dataclass
class Response:
    body: bytes
    url: str
    status: int
    headers: dict[str, str]
    retrieved_at: str


@dataclass
class CollectionContext:
    window_start: str
    window_end: str
    cursor: dict[str, Any]
    max_items: int
    max_pages: int
    client: Any
    artifact_dir: Path
    run_id: str = ""
    source_id: str = ""
    max_bytes: int = 5_000_000
    max_seconds: float = 120
    backfill_window_start: str | None = None
    backfill_window_end: str | None = None
    started_monotonic: float = field(default_factory=time.monotonic)

    @property
    def now(self) -> str:
        return self.window_end

    def remaining_seconds(self) -> float:
        return max(0, self.max_seconds - (time.monotonic() - self.started_monotonic))
