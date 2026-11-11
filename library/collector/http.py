"""Anonymous HTTP with shared, explicit byte/request/time ceilings."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import Response


class FetchError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class LimitReached(FetchError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Budget:
    max_bytes: int = 25_000_000
    max_seconds: float = 600
    max_requests: int = 100
    bytes_received: int = 0
    requests: int = 0
    started: float = field(default_factory=time.monotonic)

    def remaining_seconds(self) -> float:
        return self.max_seconds - (time.monotonic() - self.started)

    def check(self) -> None:
        if self.remaining_seconds() <= 0:
            raise LimitReached("Run elapsed-time limit reached")
        if self.bytes_received >= self.max_bytes:
            raise LimitReached("Run response-byte limit reached")
        if self.requests >= self.max_requests:
            raise LimitReached("Run request limit reached")


class HTTPClient:
    def __init__(self, budget: Budget, *, max_bytes: int = 5_000_000, max_seconds: float = 120, attempts: int = 2, backoff: float = 0.5, opener=None):
        self.budget = budget
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.attempts = max(1, min(attempts, 3))
        self.backoff = max(0, min(backoff, 2))
        self.started = time.monotonic()
        self.history: list[Response] = []
        self.errors: list[dict] = []
        self.opener = opener or urllib.request.urlopen
        self.bytes_received = 0
        self.request_count = 0

    def get(self, url: str, max_bytes: int | None = None, accept: str | None = None) -> Response:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise FetchError("Only anonymous HTTP(S) URLs are supported")
        ceiling = min(max_bytes or self.max_bytes, self.max_bytes)
        last = None
        for attempt in range(self.attempts):
            self.budget.check()
            remaining = min(self.max_seconds - (time.monotonic() - self.started), self.budget.remaining_seconds())
            if remaining <= 0:
                raise LimitReached("Source elapsed-time limit reached")
            self.budget.requests += 1
            self.request_count += 1
            request = urllib.request.Request(url, headers={"User-Agent": "Public-Content-Library/0.1", "Accept": accept or "*/*", "Accept-Encoding": "identity"})
            try:
                with self.opener(request, timeout=min(20, remaining)) as response:
                    chunks = []
                    size = 0
                    while True:
                        if min(self.max_seconds - (time.monotonic() - self.started), self.budget.remaining_seconds()) <= 0:
                            raise LimitReached("Elapsed-time limit reached while reading response")
                        chunk = response.read(min(65536, ceiling - size + 1, self.budget.max_bytes - self.budget.bytes_received + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        self.bytes_received += len(chunk)
                        self.budget.bytes_received += len(chunk)
                        if size > ceiling or self.budget.bytes_received > self.budget.max_bytes:
                            raise LimitReached("Response/run byte cap reached; oversized response not parsed")
                        chunks.append(chunk)
                    record = Response(b"".join(chunks), response.geturl(), response.status, {k.lower(): v for k, v in response.headers.items()}, utc_now())
                    self.history.append(record)
                    return record
            except LimitReached:
                raise
            except urllib.error.HTTPError as exc:
                last = FetchError(f"HTTP {exc.code} while fetching {url}", status=exc.code)
                self.errors.append({"url": url, "status": exc.code, "attempt": attempt + 1})
                if exc.code not in {408, 429, 500, 502, 503, 504}:
                    raise last from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = FetchError(f"{type(exc).__name__} while fetching {url}: {exc}")
                self.errors.append({"url": url, "error_type": type(exc).__name__, "attempt": attempt + 1})
            if attempt + 1 < self.attempts:
                time.sleep(min(self.backoff * (2 ** attempt), max(0, self.budget.remaining_seconds())))
        raise last or FetchError("HTTP retrieval failed")
