"""Bounded anonymous downloads of explicitly verified public audio offers.

The caller verifies the publisher/RSS offer and owns queue ordering. This module
downloads one file, never discovers media, and never uses cookies, authentication,
proxy configuration, or third-party download tools. The default transport connects
to prevalidated public IP addresses while retaining HTTPS hostname verification.

``download_audio`` returns a JSON-serializable report. Its directory contains an
atomic manifest, a resumable prefix, and immutable SHA-256-named completed files.
Only a strong ETag plus matching entity URL, length, MIME, and exact 206 range
permits appending. An ignored range is restarted from zero, never appended.

Injected openers implement ``open(AudioRequest, timeout=seconds)`` and return a
response with status, headers, read(size), close(), and optionally set_timeout().
They must not redirect, decode, authenticate, or read the response body eagerly.
AudioRequest.addresses are already resolved and validated. Test injection is a
trusted transport seam; the default implementation pins those exact addresses.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import stat
import threading
import time
from typing import Callable
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit


@dataclass(frozen=True)
class DownloadLimits:
    max_file_bytes: int = 80_000_000
    max_run_bytes: int = 80_000_000
    max_seconds: float = 600
    max_requests: int = 16
    max_redirects: int = 5
    chunk_bytes: int = 4_000_000
    io_timeout: float = 20

    def validate(self):
        for name in ("max_file_bytes", "max_run_bytes", "max_requests", "chunk_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.max_redirects, int) or not 0 <= self.max_redirects <= 20:
            raise ValueError("max_redirects must be between zero and twenty")
        for name in ("max_seconds", "io_timeout"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class AudioRequest:
    url: str
    method: str
    headers: dict
    addresses: tuple


class DownloadError(Exception):
    pass


class LocalIntegrityError(DownloadError):
    """Trusted local manifest/artifact no longer matches its recorded identity."""


class UnsafeURL(DownloadError):
    pass


class ProtocolError(DownloadError):
    pass


class Deferred(DownloadError):
    pass


class EntityChanged(Deferred):
    pass


MIME_EXTENSIONS = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
    "audio/x-m4a": "m4a", "audio/aac": "aac", "audio/ogg": "ogg",
    "application/ogg": "ogg", "audio/opus": "ogg", "audio/webm": "webm",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/flac": "flac", "audio/x-flac": "flac", "audio/aiff": "aiff",
    "audio/x-aiff": "aiff", "audio/basic": "au",
}
GENERIC_MIMES = {"application/octet-stream", "binary/octet-stream"}
# Some publisher MP3s have nearly 1 MB of ID3/XMP metadata before the audio.
# This is a bounded identification window, not permission to trust ID3 alone.
MAX_MIME_SNIFF_BYTES = 2_097_152
REDIRECTS = {301, 302, 303, 307, 308}
READ_SIZE = 65_536
MANIFEST_NAME = "manifest.json"
PARTIAL_NAME = "partial.bin"


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _url(url):
    if not isinstance(url, str) or len(url) > 16_384 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise UnsafeURL("Malformed URL or control characters")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise UnsafeURL("Only public HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise UnsafeURL("URL credentials and fragments are not allowed")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535 or not host or "%" in host:
            raise UnsafeURL("Invalid host or port")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")) or "." not in host and ":" not in host:
            raise UnsafeURL("Local or unqualified hostname refused")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+", host) or any(not label or len(label) > 63 for label in host.split(".")):
                raise UnsafeURL("Invalid public hostname")
        else:
            if not _public_ip(address):
                raise UnsafeURL("Non-public IP address refused")
        if parsed.scheme == "https" and port == 80 or parsed.scheme == "http" and port == 443:
            raise UnsafeURL("Scheme and standard port disagree")
        authority = f"[{host}]" if ":" in host else host
        if port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        normalized = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        normalized.encode("ascii")  # URL paths must already be percent encoded.
        return normalized, host, port
    except (ValueError, UnicodeError) as error:
        raise UnsafeURL("Invalid HTTP URL") from error


def _entity_key(url):
    """Keep content-selecting parameters; ignore only recognized CDN signatures.

    CloudFront's complete signing tuple is emitted by our verified Substack
    offer. A random query parameter named 'Signature' alone is not discarded.
    All other parameters remain part of the validator's resource scope.
    """
    parsed = urlsplit(url)
    parts = parsed.query.split("&") if parsed.query else []
    keys = {part.partition("=")[0] for part in parts}
    discard = set()
    known_cdn = parsed.hostname == "substackcdn.com" or parsed.hostname.endswith((".substackcdn.com", ".cloudfront.net"))
    if known_cdn and {"Signature", "Key-Pair-Id"} <= keys and ("Expires" in keys or "Policy" in keys):
        discard = {"Signature", "Key-Pair-Id", "Expires", "Policy"}
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(part for part in parts if part.partition("=")[0] not in discard), ""))


def _public_ip(address):
    if not address.is_global or address.is_multicast or address.is_unspecified or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        return not (address.is_site_local or address.ipv4_mapped or address.sixtofour or address.teredo)
    return True


def _strong_etag(value):
    return value if isinstance(value, str) and re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', value) else None


def _header(headers, name):
    values = headers.get_all(name) if hasattr(headers, "get_all") else [v for k, v in headers.items() if k.lower() == name.lower()]
    values = values or []
    if len(values) > 1:
        raise ProtocolError(f"Duplicate {name} header refused")
    value = str(values[0]).strip() if values else None
    if value is not None and any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProtocolError(f"Control characters in {name}")
    return value


def _length(headers):
    value = _header(headers, "Content-Length")
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise ProtocolError("Invalid Content-Length")
    return int(value)


def _entity(response, final_url, limits, *, total=None):
    encoding = _header(response.headers, "Content-Encoding")
    if encoding and encoding.lower() != "identity":
        raise ProtocolError("Encoded audio responses cannot be combined as original bytes")
    transfer = _header(response.headers, "Transfer-Encoding")
    length = _length(response.headers)
    if transfer and (transfer.lower() != "chunked" or length is not None):
        raise ProtocolError("Ambiguous or unsupported response framing")
    mime = (_header(response.headers, "Content-Type") or "").split(";", 1)[0].strip().lower()
    if mime not in MIME_EXTENSIONS and mime not in GENERIC_MIMES:
        raise ProtocolError(f"Unsupported audio MIME type: {mime or 'missing'}")
    total = length if total is None else total
    if total is not None and (total <= 0 or total > limits.max_file_bytes):
        raise Deferred("Declared complete audio exceeds the per-file limit or is empty")
    return {"etag": _header(response.headers, "ETag"), "last_modified": _header(response.headers, "Last-Modified"),
            "total_bytes": total, "mime_type": mime, "declared_mime_type": mime,
            "final_url": final_url, "entity_key": _entity_key(final_url)}


def _same(a, b):
    # A detected audio MIME must not replace the server's wire type when
    # comparing resumable entities. Older manifests predate declared_mime_type.
    return bool(a and b and _strong_etag(a.get("etag"))
                and all(a.get(key) == b.get(key) for key in ("etag", "total_bytes", "entity_key"))
                and a.get("declared_mime_type", a.get("mime_type")) == b.get("declared_mime_type", b.get("mime_type")))


def _offer(value):
    if not isinstance(value, dict) or value.get("public_access") is not True:
        raise ValueError("An explicitly verified anonymous public offer is required")
    if not value.get("parent_item_id") and not value.get("source_item_id"):
        raise ValueError("Public offer needs parent_item_id or source_item_id")
    if not isinstance(value.get("offer_kind"), str) or not value["offer_kind"].strip():
        raise ValueError("Public offer needs offer_kind")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(value.get("evidence_sha256", ""))):
        raise ValueError("Public offer needs the SHA-256 of publisher evidence")
    _url(value.get("publisher_page_url"))
    return {key: value[key] for key in ("publisher_page_url", "parent_item_id", "source_item_id", "evidence_sha256", "offer_kind", "public_access") if key in value}


class _Budget:
    def __init__(self, limits, clock):
        self.limits, self.clock, self.started = limits, clock, clock()
        self.requests, self.received_bytes = 0, 0
        self.dns_bytes, self.media_bytes = 0, 0

    def remaining(self):
        remaining = self.limits.max_seconds - (self.clock() - self.started)
        if remaining <= 0:
            raise Deferred("Download time budget exhausted")
        return remaining

    def request(self):
        self.remaining()
        if self.requests >= self.limits.max_requests:
            raise Deferred("HTTP request budget exhausted")
        self.requests += 1

    def summary(self):
        return {"requests": self.requests, "received_bytes": self.received_bytes,
                "dns_bytes": self.dns_bytes, "media_bytes": self.media_bytes,
                "byte_accounting": "Identity HTTP body bytes returned by the transport; DNS and media included, protocol/TLS overhead excluded",
                "elapsed_seconds": round(max(0, self.clock() - self.started), 6), "limits": asdict(self.limits)}


def _addresses(host, port, resolver, budget):
    """Bound DNS waiting; resolve once and pin all subsequent socket operations."""
    result = queue.Queue(maxsize=1)
    def resolve():
        try:
            result.put((True, resolver(host, port, type=socket.SOCK_STREAM)))
        except BaseException as error:
            result.put((False, error))
    thread = threading.Thread(target=resolve, name="public-audio-dns", daemon=True)
    thread.start()
    try:
        ok, addresses = result.get(timeout=min(budget.limits.io_timeout, budget.remaining()))
    except queue.Empty as error:
        raise Deferred("Public DNS lookup timed out; no HTTP request was sent") from error
    budget.remaining()
    if not ok:
        raise Deferred(f"Public DNS lookup failed: {type(addresses).__name__}") from addresses
    if not addresses or len(addresses) > 64:
        raise UnsafeURL("Missing or excessive public address results")
    accepted = []
    for family, socktype, protocol, _, sockaddr in addresses:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, TypeError, IndexError) as error:
            raise UnsafeURL("Invalid DNS address") from error
        if (family not in {socket.AF_INET, socket.AF_INET6} or socktype != socket.SOCK_STREAM
                or not _public_ip(ip) or sockaddr[1] != port):
            raise UnsafeURL("DNS included a non-public or unexpected address")
        row = (family, socktype, protocol, sockaddr)
        if row not in accepted:
            accepted.append(row)
    return tuple(accepted)


class _Response:
    def __init__(self, response, connection, connected_socket):
        self.response, self.connection, self.socket = response, connection, connected_socket
        self.status, self.headers = response.status, response.headers

    def set_timeout(self, seconds):
        self.socket.settimeout(seconds)

    def read(self, size):
        return self.response.read1(size)

    def close(self):
        self.response.close()
        self.connection.close()


class _SocketReader(io.RawIOBase):
    def __init__(self, owner):
        self.owner = owner

    def readable(self):
        return True

    def readinto(self, buffer):
        self.owner.arm()
        return self.owner.socket.recv_into(buffer)


class _DeadlineSocket:
    """A socket file whose repeated receives cannot reset a request deadline.

    A normal socket timeout alone can be extended indefinitely by trickled HTTP
    headers. Every underlying receive here checks the same absolute deadline.
    The body reader installs a new deadline bounded by the run's remaining time.
    """
    def __init__(self, connected, deadline):
        self.socket, self.deadline = connected, deadline

    def arm(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP operation deadline exhausted")
        self.socket.settimeout(remaining)

    def settimeout(self, seconds):
        self.deadline = time.monotonic() + seconds
        self.arm()

    def sendall(self, data):
        self.arm()
        return self.socket.sendall(data)

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("Only binary response reads are supported")
        return io.BufferedReader(_SocketReader(self))

    def close(self):
        self.socket.close()


class PublicOpener:
    """Direct, DNS-pinned stdlib HTTP. No environment proxies or auth handlers."""
    def open(self, request, *, timeout):
        parsed = urlsplit(request.url)
        host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        deadline, connection, connected = time.monotonic() + timeout, None, None
        error = None
        for family, kind, protocol, address in request.addresses[:4]:
            connected = socket.socket(family, kind, protocol)
            sent = False
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Connection deadline exhausted")
                connected.settimeout(remaining)
                connected.connect(address)
                if parsed.scheme == "https":
                    connected.settimeout(max(0.001, deadline - time.monotonic()))
                    connected = ssl.create_default_context().wrap_socket(connected, server_hostname=host)
                connection = http.client.HTTPConnection(host, port, timeout=timeout)
                connected = _DeadlineSocket(connected, deadline)
                connection.sock = connected
                target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                sent = True
                connection.request(request.method, target, headers={**request.headers, "Host": parsed.netloc})
                return _Response(connection.getresponse(), connection, connected)
            except BaseException as current:
                error = current
                if connection:
                    connection.close()
                connected.close()
                if sent or not isinstance(current, (OSError, http.client.HTTPException)):
                    raise
        raise error or OSError("No public destination could be connected")


class _HTTP:
    def __init__(self, budget, opener, resolver):
        self.budget, self.opener, self.resolver = budget, opener, resolver
        self.history = []
        self.dns_cache = {}

    def resolve(self, host, port):
        if self.resolver != "cloudflare_doh":
            return _addresses(host, port, self.resolver, self.budget)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not _public_ip(literal):
                raise UnsafeURL("Non-public literal refused")
            family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
            address = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
            return ((family, socket.SOCK_STREAM, 6, address),)
        now = self.budget.clock()
        cached = self.dns_cache.get(host)
        if cached and cached[0] > now:
            return tuple((socket.AF_INET, socket.SOCK_STREAM, 6, (ip, port)) for ip in cached[1])
        # Explicit application-only mode. No call to system DNS is made for
        # this endpoint, and TLS still verifies cloudflare-dns.com. The public
        # bootstrap IPs are Cloudflare's officially published resolver service.
        url = "https://cloudflare-dns.com/dns-query?" + urlencode({"name": host, "type": "A"})
        addresses = tuple((socket.AF_INET, socket.SOCK_STREAM, 6, (ip, 443)) for ip in ("1.1.1.1", "1.0.0.1"))
        request = AudioRequest(url, "GET", {"Accept": "application/dns-json", "Accept-Encoding": "identity",
                                           "User-Agent": "ClaudePublicLibrary/1.0 (public DNS lookup)"}, addresses)
        self.budget.request()
        row = {"role": "dns", "method": "GET", "url": url, "queried_host": host, "at": _utc()}
        self.history.append(row)
        response = None
        try:
            response = self.opener.open(request, timeout=min(self.budget.limits.io_timeout, self.budget.remaining()))
            row["status"] = response.status
            if response.status != 200:
                raise Deferred(f"Public DNS service returned HTTP {response.status}; redirects are not followed")
            mime = (_header(response.headers, "Content-Type") or "").split(";", 1)[0].lower()
            if mime not in {"application/dns-json", "application/json"}:
                raise ProtocolError("Public DNS did not return JSON")
            if (_header(response.headers, "Content-Encoding") or "identity").lower() != "identity":
                raise ProtocolError("Encoded public DNS response refused")
            length = _length(response.headers)
            if length is not None and not 0 < length <= 32_768:
                raise ProtocolError("Public DNS response exceeds its 32 KiB limit")
            transfer = _header(response.headers, "Transfer-Encoding")
            if transfer and (transfer.lower() != "chunked" or length is not None):
                raise ProtocolError("Ambiguous public DNS response framing")
            body = bytearray()
            while length is None or len(body) < length:
                remaining = min(32_768 - len(body), self.budget.limits.max_run_bytes - self.budget.received_bytes)
                if length is not None:
                    remaining = min(remaining, length - len(body))
                if remaining <= 0:
                    raise Deferred("Public DNS body or run byte budget exhausted")
                if hasattr(response, "set_timeout"):
                    response.set_timeout(min(self.budget.limits.io_timeout, self.budget.remaining()))
                self.budget.remaining()
                failure = None
                try:
                    block = response.read(min(READ_SIZE, remaining))
                except http.client.IncompleteRead as error:
                    block, failure = error.partial, error
                if not isinstance(block, bytes) or len(block) > remaining:
                    raise ProtocolError("Public DNS transport exceeded bounded read")
                self.budget.received_bytes += len(block)
                self.budget.dns_bytes += len(block)
                body.extend(block)
                if failure:
                    raise Deferred("Public DNS body interrupted") from failure
                if not block:
                    break
            self.budget.remaining()
            if length is not None and len(body) != length:
                raise Deferred("Public DNS body ended before its declared length")
            ips, ttl = _doh_answers(bytes(body), host)
            row.update(response_bytes=len(body), answer_ips=ips, cache_ttl_seconds=ttl)
            self.dns_cache[host] = (self.budget.clock() + ttl, ips)
            return tuple((socket.AF_INET, socket.SOCK_STREAM, 6, (ip, port)) for ip in ips)
        except (OSError, http.client.HTTPException) as error:
            row["error"] = type(error).__name__
            raise Deferred(f"Public DNS transport failed: {type(error).__name__}") from error
        finally:
            if response:
                response.close()

    def open(self, source_url, method, headers=None):
        url, _, _ = _url(source_url)
        headers = {"User-Agent": "ClaudePublicLibrary/1.0 (anonymous bounded public audio archive)",
                   "Accept": "audio/*, application/ogg;q=0.9", "Accept-Encoding": "identity", **(headers or {})}
        for redirect in range(self.budget.limits.max_redirects + 1):
            url, host, port = _url(url)
            addresses = self.resolve(host, port)
            self.budget.request()
            row = {"role": "media", "method": method, "url": url, "at": _utc(), "request_headers": {k: v for k, v in headers.items() if k in {"Range", "If-Range", "If-None-Match"}}}
            self.history.append(row)
            try:
                response = self.opener.open(AudioRequest(url, method, headers, addresses), timeout=min(self.budget.limits.io_timeout, self.budget.remaining()))
            except (OSError, http.client.HTTPException) as error:
                row["error"] = type(error).__name__
                raise Deferred(f"HTTP transport failed: {type(error).__name__}") from error
            try:
                row["status"] = response.status
                row["response_headers"] = {name.lower(): value for name in ("Content-Type", "Content-Length", "Content-Range", "Content-Encoding", "ETag", "Last-Modified", "Accept-Ranges") if (value := _header(response.headers, name)) is not None}
                self.budget.remaining()
                if response.status not in REDIRECTS:
                    return response, url
                location = _header(response.headers, "Location")
                if not location or redirect == self.budget.limits.max_redirects:
                    raise ProtocolError("Missing redirect location or redirect limit exceeded")
                target, _, _ = _url(urljoin(url, location))
                if urlsplit(url).scheme == "https" and urlsplit(target).scheme != "https":
                    raise UnsafeURL("HTTPS to HTTP redirect refused")
                row["redirect_to"] = target
                url = target
            except BaseException:
                response.close()
                raise
            response.close()
        raise ProtocolError("Redirect limit exceeded")


def _doh_answers(raw, host):
    """Accept only the requested name's finite CNAME chain and public A records."""
    try:
        payload = json.loads(raw)
        if payload.get("Status") != 0 or payload.get("TC") is True:
            raise Deferred("Public DNS returned no complete successful answer")
        questions = payload.get("Question", [])
        if len(questions) != 1 or questions[0].get("type") != 1 or questions[0].get("name", "").rstrip(".").lower() != host:
            raise ProtocolError("Public DNS question does not match the requested name")
        answers = payload.get("Answer", [])
        if not isinstance(answers, list) or len(answers) > 64:
            raise ProtocolError("Invalid or excessive public DNS answers")
        aliases, addresses, ttls = {}, {}, []
        for answer in answers:
            name = answer["name"].rstrip(".").lower()
            ttl = answer.get("TTL", 0)
            if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
                raise ProtocolError("Invalid public DNS TTL")
            if answer.get("type") == 5:
                target = answer["data"].rstrip(".").lower()
                _url("https://" + target + "/")
                if name in aliases and aliases[name] != target:
                    raise ProtocolError("Ambiguous public DNS CNAME chain")
                aliases[name] = target
                ttls.append(ttl)
            elif answer.get("type") == 1:
                ip = ipaddress.ip_address(answer["data"])
                if ip.version != 4 or not _public_ip(ip):
                    raise UnsafeURL("Public DNS answer included a non-public IPv4 address")
                addresses.setdefault(name, []).append(str(ip))
                ttls.append(ttl)
        current, visited = host, set()
        while current in aliases:
            if current in visited or len(visited) >= 8 or current in addresses:
                raise ProtocolError("Looping, excessive, or ambiguous public DNS CNAME chain")
            visited.add(current)
            current = aliases[current]
        ips = list(dict.fromkeys(addresses.get(current, [])))
        if set(aliases) != visited or set(addresses) - {current}:
            raise ProtocolError("Public DNS supplied answers outside the requested CNAME chain")
        if not ips:
            raise Deferred("Public DNS supplied no public IPv4 answer for the requested CNAME chain")
        return ips, min([600, *ttls])
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ProtocolError("Invalid public DNS JSON or record schema") from error


def _owned_directory(path):
    path = Path(path).absolute()
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise LocalIntegrityError("Download paths may not traverse symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise LocalIntegrityError("Download directory is not a directory")
    return path


def _open_file(path, flags, mode=0o600):
    if Path(path).is_symlink():
        raise LocalIntegrityError("Task-owned file may not be a symlink")
    fd = os.open(path, flags | os.O_NOFOLLOW, mode)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise LocalIntegrityError("Only regular task-owned files are allowed")
    return fd


def _json_read(path):
    with os.fdopen(_open_file(path, os.O_RDONLY), "rb") as file:
        raw = file.read(1_048_577)
    if len(raw) > 1_048_576:
        raise LocalIntegrityError("Oversized download manifest")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise LocalIntegrityError("Invalid download manifest JSON") from error


def _save(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with os.fdopen(_open_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC), "w") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def _hash(path, budget=None, length=None):
    digest, size = hashlib.sha256(), 0
    try:
        fd = _open_file(path, os.O_RDONLY)
    except FileNotFoundError as error:
        raise LocalIntegrityError("A recorded artifact or partial file is missing") from error
    with os.fdopen(fd, "rb") as file:
        while length is None or size < length:
            if budget:
                budget.remaining()
            block = file.read(min(READ_SIZE, length - size) if length is not None else READ_SIZE)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest, size


def _artifact_path(directory, item):
    name = item.get("path", "")
    expected = f"objects/{item.get('sha256', '')}.{MIME_EXTENSIONS.get(item.get('mime_type'), 'unknown')}"
    if name != expected or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
        raise LocalIntegrityError("Invalid completed-artifact manifest path")
    _owned_directory(directory / "objects")
    return directory / name


def _load(directory, source_url, public_offer, budget):
    path = directory / MANIFEST_NAME
    state = _json_read(path) if path.exists() or path.is_symlink() else {"schema_version": 1, "source_url": source_url, "complete": None, "partial": None}
    if state.get("schema_version") != 1 or state.get("source_url") != source_url:
        raise LocalIntegrityError("Download directory belongs to a different source or schema")
    state["public_offer"] = public_offer
    if complete := state.get("complete"):
        if complete["bytes"] > budget.limits.max_file_bytes:
            raise Deferred("Completed artifact exceeds the current per-file limit")
        artifact = _artifact_path(directory, complete)
        digest, size = _hash(artifact, budget)
        if size != complete["bytes"] or digest.hexdigest() != complete["sha256"]:
            raise LocalIntegrityError("Completed artifact checksum or length mismatch")
    if partial := state.get("partial"):
        if partial.get("path") != PARTIAL_NAME or not isinstance(partial.get("bytes"), int) or partial["bytes"] < 0:
            raise LocalIntegrityError("Invalid partial-file manifest")
        total = partial.get("total_bytes")
        if partial["bytes"] > budget.limits.max_file_bytes or total is not None and not partial["bytes"] <= total <= budget.limits.max_file_bytes:
            raise Deferred("Retained partial exceeds current per-file limits")
        digest, size = _hash(directory / PARTIAL_NAME, budget, partial["bytes"])
        if size != partial["bytes"] or digest.hexdigest() != partial["sha256"]:
            raise LocalIntegrityError("Retained partial checksum or length mismatch")
        # A crash can leave an uncommitted tail. Only the durable hashed prefix is
        # trusted; discard the tail without changing immutable completed objects.
        if (directory / PARTIAL_NAME).stat().st_size != size:
            with os.fdopen(_open_file(directory / PARTIAL_NAME, os.O_WRONLY), "wb") as file:
                file.truncate(size)
    elif (directory / PARTIAL_NAME).exists() or (directory / PARTIAL_NAME).is_symlink():
        # A prior completion can leave the redundant partial after manifest commit.
        if (directory / PARTIAL_NAME).is_symlink():
            raise LocalIntegrityError("Untracked partial symlink refused")
        (directory / PARTIAL_NAME).unlink()
    return state


def _clear_partial(directory, state):
    state["partial"] = None
    _save(directory / MANIFEST_NAME, state)
    path = directory / PARTIAL_NAME
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise LocalIntegrityError("Partial symlink refused")
        path.unlink()


def _new_partial(directory, state, entity, *, seeded_bytes=0):
    _clear_partial(directory, state)
    with os.fdopen(_open_file(directory / PARTIAL_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"):
        pass
    state["partial"] = {**entity, "path": PARTIAL_NAME, "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(), "seeded_bytes": seeded_bytes}
    _save(directory / MANIFEST_NAME, state)


def _check_audio_prefix(prefix, mime):
    kind = MIME_EXTENSIONS[mime]
    valid = ((kind == "mp3" and (prefix.startswith(b"ID3") or len(prefix) >= 2 and prefix[0] == 255 and prefix[1] & 224 == 224))
             or kind == "m4a" and len(prefix) >= 12 and prefix[4:8] == b"ftyp"
             or kind == "aac" and len(prefix) >= 2 and prefix[0] == 255 and prefix[1] & 246 == 240
             or kind == "ogg" and prefix.startswith(b"OggS")
             or kind == "webm" and prefix.startswith(b"\x1aE\xdf\xa3")
             or kind == "wav" and prefix[:4] in {b"RIFF", b"RIFX", b"RF64"} and prefix[8:12] == b"WAVE"
             or kind == "flac" and prefix.startswith(b"fLaC")
             or kind == "aiff" and prefix.startswith(b"FORM") and prefix[8:12] in {b"AIFF", b"AIFC"}
             or kind == "au" and prefix.startswith(b".snd"))
    if not valid:
        raise ProtocolError("Body does not have the declared audio container signature; HTML/paywall bodies are refused")


def _mp3_frame(prefix, offset):
    """Parse one MPEG Layer III header; free-format/reserved fields are refused."""
    if len(prefix) < offset + 4:
        return None
    raw = prefix[offset:offset + 4]
    word = int.from_bytes(raw, "big")
    version, layer = (word >> 19) & 3, (word >> 17) & 3
    bitrate_index, rate_index = (word >> 12) & 15, (word >> 10) & 3
    if (word >> 21 != 0x7ff or version == 1 or layer != 1 or bitrate_index in {0, 15}
            or rate_index == 3 or word & 3 == 2):
        raise ProtocolError("Generic body does not contain valid MPEG Layer III frame headers")
    rates = [44100, 48000, 32000]
    sample_rate = rates[rate_index] // (1 if version == 3 else 2 if version == 2 else 4)
    table = ([32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
             if version == 3 else [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160])
    bitrate = table[bitrate_index - 1] * 1000
    frame_bytes = (144 if version == 3 else 72) * bitrate // sample_rate + ((word >> 9) & 1)
    return {"offset": offset, "header_hex": raw.hex(), "frame_bytes": frame_bytes,
            "mpeg_version": {3: "1", 2: "2", 0: "2.5"}[version], "layer": 3,
            "sample_rate_hz": sample_rate, "bitrate_bps": bitrate}


def _sniff_generic_audio(prefix, *, total_bytes=None, final=False):
    """Identify generic binary MP3 only after two correctly spaced frame headers.

    ID3 magic alone is insufficient. A valid bounded ID3v2 tag may be skipped,
    but MPEG audio must then be present at its declared end. Other containers
    still require their supported explicit audio MIME; generic binary is not a
    catch-all format allowance. Full-file ffprobe/duration validation remains a
    separate mandatory publication check in audio_queue.
    """
    offset, id3 = 0, None
    if prefix.startswith(b"ID3") or len(prefix) < 3 and b"ID3".startswith(prefix):
        if len(prefix) < 10:
            if final:
                raise ProtocolError("Generic body ends inside an ID3 header")
            return None
        major, revision, flags = prefix[3:6]
        valid_flags = {2: 0xc0, 3: 0xe0, 4: 0xf0}
        if (major not in valid_flags or revision == 255 or flags & ~valid_flags[major]
                or any(value >= 128 for value in prefix[6:10])):
            raise ProtocolError("Generic body has an invalid ID3v2 header")
        tag_bytes = sum(value << shift for value, shift in zip(prefix[6:10], [21, 14, 7, 0]))
        offset = 10 + tag_bytes + (10 if major == 4 and flags & 16 else 0)
        id3 = {"header_hex": prefix[:10].hex(), "version": major, "tag_bytes": tag_bytes,
               "first_audio_frame_offset": offset}
        if offset + 4 > MAX_MIME_SNIFF_BYTES or total_bytes is not None and offset + 4 > total_bytes:
            raise ProtocolError("ID3 tag leaves no verifiable audio within the bounded identification window")
        if major == 4 and flags & 16 and len(prefix) >= offset:
            footer = prefix[offset - 10:offset]
            if footer[:3] != b"3DI" or footer[3:] != prefix[3:10]:
                raise ProtocolError("ID3v2 footer does not match its header")
    elif prefix and prefix[0] != 0xff:
        raise ProtocolError("Generic binary is not recognized MP3 audio; HTML/error bodies are refused")
    first = _mp3_frame(prefix, offset)
    second = _mp3_frame(prefix, offset + first["frame_bytes"]) if first else None
    if first and second:
        if any(first[key] != second[key] for key in ("mpeg_version", "layer", "sample_rate_hz")):
            raise ProtocolError("Consecutive MPEG frames disagree about audio format")
        examined = second["offset"] + 4
        if total_bytes is not None and second["offset"] + second["frame_bytes"] > total_bytes:
            raise ProtocolError("Declared entity ends before two complete MPEG audio frames")
        return {"method": "bounded_id3_and_two_mpeg_layer_iii_frame_headers", "detected_mime_type": "audio/mpeg",
                "bytes_examined": examined, "prefix_sha256": hashlib.sha256(prefix[:examined]).hexdigest(),
                "id3": id3, "frames": [first, second], "max_sniff_bytes": MAX_MIME_SNIFF_BYTES,
                "full_file_decoder_validated": False}
    if final or len(prefix) >= MAX_MIME_SNIFF_BYTES:
        raise ProtocolError("Generic binary lacks two MPEG audio frames within the bounded identification window")
    return None


def _mime_evidence(prefix, entity, *, final=False):
    declared = entity.get("declared_mime_type", entity["mime_type"])
    if declared in GENERIC_MIMES:
        evidence = _sniff_generic_audio(prefix, total_bytes=entity.get("total_bytes"), final=final)
        if evidence:
            if entity["mime_type"] not in GENERIC_MIMES | {evidence["detected_mime_type"]}:
                raise LocalIntegrityError("Retained detected audio MIME disagrees with its actual byte signature")
            return {"mime_type": evidence["detected_mime_type"], "mime_detection": evidence}
        return {}
    if len(prefix) >= 16 or final:
        _check_audio_prefix(prefix, entity["mime_type"])
        return {"mime_type": entity["mime_type"]}
    return {}


def _stream(response, directory, state, budget, expected_bytes):
    partial = state["partial"]
    digest, _ = _hash(directory / PARTIAL_NAME, budget, partial["bytes"])
    received, eof = 0, False
    generic = partial.get("declared_mime_type", partial["mime_type"]) in GENERIC_MIMES
    prefix_limit = MAX_MIME_SNIFF_BYTES if generic else 16
    if partial["bytes"]:
        with os.fdopen(_open_file(directory / PARTIAL_NAME, os.O_RDONLY), "rb") as file:
            prefix = file.read(min(prefix_limit, partial["bytes"]))
    else:
        prefix = b""
    evidence = _mime_evidence(prefix, partial)
    partial.update(evidence)
    prefix_checked = bool(evidence)
    with os.fdopen(_open_file(directory / PARTIAL_NAME, os.O_WRONLY | os.O_APPEND), "ab") as file:
        try:
            while expected_bytes is None or received < expected_bytes:
                remaining_time = budget.remaining()
                allowance = min(READ_SIZE, budget.limits.max_run_bytes - budget.received_bytes,
                                budget.limits.max_file_bytes - partial["bytes"])
                if expected_bytes is not None:
                    allowance = min(allowance, expected_bytes - received)
                if generic and not prefix_checked:
                    allowance = min(allowance, MAX_MIME_SNIFF_BYTES - len(prefix))
                if allowance <= 0:
                    raise Deferred("Received-byte budget exhausted; complete capture not asserted")
                if hasattr(response, "set_timeout"):
                    response.set_timeout(min(budget.limits.io_timeout, remaining_time))
                failure = None
                try:
                    block = response.read(allowance)
                except http.client.IncompleteRead as error:
                    block, failure = error.partial, error
                if not isinstance(block, bytes) or len(block) > allowance:
                    raise ProtocolError("Transport exceeded the requested bounded read")
                budget.received_bytes += len(block)
                budget.media_bytes += len(block)
                if not block:
                    eof = True
                    if failure:
                        raise Deferred("Audio body was interrupted") from failure
                    break
                if not prefix_checked:
                    prefix = (prefix + block)[:prefix_limit]
                    evidence = _mime_evidence(prefix, partial)
                file.write(block)
                digest.update(block)
                partial["bytes"] += len(block)
                received += len(block)
                if not prefix_checked and evidence:
                    partial.update(evidence)
                    prefix_checked = True
                if failure:
                    raise Deferred("Audio body was interrupted") from failure
            if expected_bytes is not None and received != expected_bytes:
                raise Deferred("Audio response ended before the declared length")
            if not prefix_checked:
                # A complete bounded range is not necessarily the full file:
                # metadata-only prefixes remain pending, never audio artifacts.
                final = eof or partial["total_bytes"] is not None and partial["bytes"] == partial["total_bytes"]
                partial.update(_mime_evidence(prefix, partial, final=final))
            if partial["total_bytes"] is None and eof:
                partial["total_bytes"] = partial["bytes"]
        except (OSError, http.client.HTTPException) as error:
            raise Deferred(f"Audio body interrupted: {type(error).__name__}") from error
        finally:
            file.flush()
            os.fsync(file.fileno())
            partial["sha256"] = digest.hexdigest()
            _save(directory / MANIFEST_NAME, state)
    return eof


def _commit(directory, state, budget):
    partial = state["partial"]
    if not partial or partial["bytes"] != partial["total_bytes"] or not partial["bytes"]:
        raise ProtocolError("Incomplete audio cannot be committed")
    digest, size = _hash(directory / PARTIAL_NAME, budget)
    if size != partial["bytes"] or digest.hexdigest() != partial["sha256"]:
        raise LocalIntegrityError("Audio changed before immutable commit")
    with os.fdopen(_open_file(directory / PARTIAL_NAME, os.O_RDONLY), "rb") as file:
        sniff_limit = MAX_MIME_SNIFF_BYTES if partial.get("declared_mime_type", partial["mime_type"]) in GENERIC_MIMES else 16
        partial.update(_mime_evidence(file.read(sniff_limit), partial, final=True))
    complete = {**partial, "path": f"objects/{partial['sha256']}.{MIME_EXTENSIONS[partial['mime_type']]}"}
    artifact = _artifact_path(directory, complete)
    if artifact.exists() or artifact.is_symlink():
        saved_hash, saved_size = _hash(artifact, budget)
        if saved_size != size or saved_hash.hexdigest() != complete["sha256"]:
            raise LocalIntegrityError("Immutable completed-object collision or corruption")
    else:
        # A hardlink avoids another 80 MB copy. It is never mutated: all restarts
        # unlink the partial, and appending only happens before it is complete.
        os.link(directory / PARTIAL_NAME, artifact, follow_symlinks=False)
    state["complete"], state["partial"] = complete, None
    _save(directory / MANIFEST_NAME, state)
    (directory / PARTIAL_NAME).unlink(missing_ok=True)
    return complete


def _http_status(status):
    if status == 429 or 500 <= status < 600:
        raise Deferred(f"Publisher returned HTTP {status}; no bypass attempted")
    raise ProtocolError(f"Publisher returned HTTP {status}; no authentication or bypass attempted")


def _acquire(directory):
    fd = _open_file(directory / ".download.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(fd)
        raise Deferred("This download directory is already in use") from error
    return fd


def download_audio(source_url, work_dir, *, public_offer, limits=None, opener=None,
                   clock: Callable = time.monotonic, resolver=socket.getaddrinfo):
    """Check/update one public audio file, returning complete/unchanged/deferred/error.

    ``path`` is populated only after a verified complete entity or an unchanged
    conditional check. A retained prefix is never a completed media artifact.
    A 304 is accepted only with the same strong ETag and scoped destination.
    Failed runs retain durable prefixes; callers may invoke this again later.
    ``resolver='cloudflare_doh'`` explicitly opts into application-only public
    IPv4 DNS over HTTPS; system DNS and network settings remain untouched.
    """
    limits = limits or DownloadLimits()
    limits.validate()
    if isinstance(resolver, str) and resolver != "cloudflare_doh":
        raise ValueError("Unknown resolver mode")
    budget = _Budget(limits, clock)
    http = _HTTP(budget, opener or PublicOpener(), resolver)
    result = {"schema_version": 1, "status": "error", "path": None, "source_url": source_url,
              "final_url": None, "sha256": None, "bytes": 0, "mime_type": None,
              "declared_mime_type": None, "mime_detection": None, "validators": {},
              "public_offer": None, "started_at": _utc(), "errors": [], "error_kind": None,
              "dns_method": resolver if isinstance(resolver, str) else "system" if resolver is socket.getaddrinfo else "injected",
              "resume": {"reused_bytes": 0, "seeded_bytes": 0, "retained_bytes": 0, "restarted": False, "reasons": []}}
    state, directory, lock = None, None, None
    try:
        source_url, _, _ = _url(source_url)
        result["source_url"], result["public_offer"] = source_url, _offer(public_offer)
        directory = _owned_directory(work_dir)
        lock = _acquire(directory)
        try:
            state = _load(directory, source_url, result["public_offer"], budget)
        except (KeyError, TypeError, AttributeError) as error:
            raise LocalIntegrityError("Download manifest has invalid fields") from error
        previous = state.get("complete")
        if prior := state.get("partial"):
            result["resume"].update(reused_bytes=prior["bytes"], seeded_bytes=min(prior.get("seeded_bytes", 0), prior["bytes"]))
        completed = None
        for generation_attempt in range(2):
            try:
                conditional = {"If-None-Match": previous["etag"]} if previous and _strong_etag(previous.get("etag")) else {}
                response, final_url = http.open(source_url, "HEAD", conditional)
                result["final_url"] = final_url
                try:
                    if response.status == 304:
                        if not conditional or _header(response.headers, "ETag") != previous.get("etag") or _entity_key(final_url) != previous.get("entity_key"):
                            raise ProtocolError("304 did not validate the existing scoped strong ETag")
                        completed = {**previous, "final_url": final_url}
                        break
                    if response.status == 200:
                        if _header(response.headers, "Content-Range"):
                            raise ProtocolError("HEAD unexpectedly describes a partial response")
                        entity = _entity(response, final_url, limits)
                    elif response.status in {405, 501}:
                        entity = None
                    else:
                        _http_status(response.status)
                finally:
                    response.close()
                if entity and _same(previous, entity):
                    completed = {**previous, "final_url": final_url}
                    break
                partial = state.get("partial")
                range_can_validate_without_head = (entity is None and partial and _strong_etag(partial.get("etag")) and partial.get("total_bytes") is not None)
                if partial and not (entity and _same(partial, entity) or range_can_validate_without_head):
                    result["resume"]["restarted"] = True
                    result["resume"]["reasons"].append("Stored prefix entity changed or cannot be strongly revalidated")
                    result["resume"]["reused_bytes"] = 0
                    result["resume"]["seeded_bytes"] = 0
                    _clear_partial(directory, state)
                while True:
                    partial = state.get("partial")
                    if partial and partial["total_bytes"] is not None and partial["bytes"] == partial["total_bytes"]:
                        completed = _commit(directory, state, budget)
                        break
                    remaining_bytes = limits.max_run_bytes - budget.received_bytes
                    if remaining_bytes <= 0:
                        raise Deferred("Received-byte budget exhausted")
                    start = partial["bytes"] if partial else 0
                    current = partial or entity
                    ranged = bool(current and _strong_etag(current.get("etag")) and current.get("total_bytes") is not None)
                    headers = {}
                    if ranged:
                        end = min(current["total_bytes"] - 1, start + limits.chunk_bytes - 1, start + remaining_bytes - 1)
                        headers = {"Range": f"bytes={start}-{end}", "If-Range": current["etag"]}
                    elif not partial and previous and _strong_etag(previous.get("etag")):
                        headers = {"If-None-Match": previous["etag"]}
                    # HEAD has just revalidated the publisher redirect chain.
                    # Use its fresh CDN URL to avoid repeating that chain for
                    # every chunk; each URL/address and later redirect is still
                    # checked independently, and entity scope must still match.
                    request_url = entity["final_url"] if entity else source_url
                    response, final_url = http.open(request_url, "GET", headers)
                    result["final_url"] = final_url
                    try:
                        if response.status == 304:
                            if (not headers.get("If-None-Match") or _header(response.headers, "ETag") != previous.get("etag")
                                    or _entity_key(final_url) != previous.get("entity_key")):
                                raise ProtocolError("GET 304 did not validate the existing scoped strong ETag")
                            completed = {**previous, "final_url": final_url}
                            break
                        elif response.status == 206:
                            if not ranged:
                                raise ProtocolError("Unexpected partial response without a range request")
                            expected_range = f"bytes {start}-{end}/{current['total_bytes']}"
                            if _header(response.headers, "Content-Range") != expected_range or _length(response.headers) != end - start + 1:
                                raise ProtocolError("Server did not honor the exact bounded byte range")
                            incoming = _entity(response, final_url, limits, total=current["total_bytes"])
                            if not _same(current, incoming):
                                raise EntityChanged("Entity changed between validation and range response")
                            if not partial:
                                _new_partial(directory, state, incoming)
                            expected_bytes = end - start + 1
                        elif response.status == 200:
                            if _header(response.headers, "Content-Range"):
                                raise ProtocolError("A full response unexpectedly declared a partial range")
                            incoming = _entity(response, final_url, limits)
                            if _same(previous, incoming):
                                completed = {**previous, "final_url": final_url}
                                break
                            if partial and partial["bytes"]:
                                result["resume"].update(restarted=True, reused_bytes=0, seeded_bytes=0)
                                result["resume"]["reasons"].append("Server ignored If-Range; full response restarted from zero" if ranged else "No range-safe complete length; full response restarted from zero")
                            _new_partial(directory, state, incoming)
                            expected_bytes = incoming["total_bytes"]
                        else:
                            _http_status(response.status)
                        eof = _stream(response, directory, state, budget, expected_bytes)
                    finally:
                        response.close()
                    partial = state["partial"]
                    if partial["total_bytes"] is not None and partial["bytes"] == partial["total_bytes"]:
                        completed = _commit(directory, state, budget)
                        break
                    if eof:
                        raise Deferred("Audio ended before complete entity was available")
                    entity = partial
                break
            except EntityChanged as error:
                result["resume"].update(restarted=True, reused_bytes=0, seeded_bytes=0)
                result["resume"]["reasons"].append(str(error))
                _clear_partial(directory, state)
                if generation_attempt == 1:
                    raise Deferred("Entity changed repeatedly; retry on a later run") from error
        if not completed:
            raise Deferred("No complete audio entity was obtained")
        if state.get("partial"):
            _clear_partial(directory, state)
        state["complete"] = completed
        _save(directory / MANIFEST_NAME, state)
        result.update(status="unchanged" if previous and previous["sha256"] == completed["sha256"] else "complete",
                      path=str(_artifact_path(directory, completed)), sha256=completed["sha256"], bytes=completed["bytes"],
                      mime_type=completed["mime_type"], final_url=completed["final_url"],
                      declared_mime_type=completed.get("declared_mime_type", completed["mime_type"]),
                      mime_detection=completed.get("mime_detection"),
                      validators={key: completed.get(key) for key in ("etag", "last_modified", "total_bytes", "entity_key")})
    except Exception as error:
        result["status"] = "deferred" if isinstance(error, (Deferred, TimeoutError)) else "error"
        result["error_kind"] = ("local_integrity" if isinstance(error, LocalIntegrityError) else
                                "remote_protocol" if isinstance(error, (ProtocolError, UnsafeURL)) else
                                "budget_or_network" if isinstance(error, (Deferred, TimeoutError)) else
                                "invalid_input" if isinstance(error, ValueError) else
                                "local_io" if isinstance(error, OSError) else "internal")
        result["errors"].append({"type": type(error).__name__, "message": str(error)})
    except BaseException as error:
        result["status"] = "deferred"
        result["errors"].append({"type": type(error).__name__, "message": "Download interrupted; validated prefix retained"})
        raise
    finally:
        if state and state.get("partial"):
            partial = state["partial"]
            result["resume"]["retained_bytes"] = partial["bytes"]
            result["resume"]["resumable"] = bool(_strong_etag(partial.get("etag")) and partial.get("total_bytes"))
            if result["status"] not in {"complete", "unchanged"}:
                result["validators"] = {key: partial.get(key) for key in ("etag", "last_modified", "total_bytes", "entity_key")}
                result["mime_type"] = partial.get("mime_type")
                result["declared_mime_type"] = partial.get("declared_mime_type", partial.get("mime_type"))
                result["mime_detection"] = partial.get("mime_detection")
        result["budget"], result["requests"], result["finished_at"] = budget.summary(), http.history, _utc()
        if directory is not None and lock is not None:
            try:
                _save(directory / "last-result.json", result)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)
    return result


def seed_partial(source_url, work_dir, prefix_path, *, public_offer, final_url, etag,
                 total_bytes, mime_type, expected_sha256, last_modified=None, limits=None):
    """Offline bootstrap from an independently verified prefix; never downloads.

    Use an empty task-owned directory. The next download rechecks the live stable
    source before resuming. This helper is for migration/fixtures, not routine
    offer discovery. The report distinctly counts seeded versus received bytes.
    """
    limits = limits or DownloadLimits()
    limits.validate()
    source_url, _, _ = _url(source_url)
    final_url, _, _ = _url(final_url)
    evidence = _offer(public_offer)
    if not _strong_etag(etag) or mime_type not in MIME_EXTENSIONS.keys() | GENERIC_MIMES or not isinstance(total_bytes, int) or not 0 < total_bytes <= limits.max_file_bytes:
        raise ValueError("Bootstrap needs a strong ETag, supported MIME, and bounded complete length")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Bootstrap needs exact expected prefix SHA-256")
    prefix_path = Path(prefix_path).absolute()
    for path in [prefix_path, *prefix_path.parents]:
        if path.is_symlink():
            raise DownloadError("Bootstrap paths may not traverse symlinks")
    directory, budget = _owned_directory(work_dir), _Budget(limits, time.monotonic)
    lock = _acquire(directory)
    try:
        if any(path.name != ".download.lock" for path in directory.iterdir()):
            raise DownloadError("Bootstrap directory must be empty")
        temporary, digest, size = directory / "seed.tmp", hashlib.sha256(), 0
        with os.fdopen(_open_file(prefix_path, os.O_RDONLY), "rb") as source, os.fdopen(_open_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as target:
            prefix = source.read(MAX_MIME_SNIFF_BYTES if mime_type in GENERIC_MIMES else 16)
            detection = _mime_evidence(prefix, {"mime_type": mime_type, "declared_mime_type": mime_type, "total_bytes": total_bytes})
            source.seek(0)
            while block := source.read(READ_SIZE):
                budget.remaining()
                size += len(block)
                if size >= total_bytes:
                    raise ValueError("Bootstrap must be a nonempty strict prefix of the complete entity")
                digest.update(block)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        if not size or digest.hexdigest() != expected_sha256:
            raise DownloadError("Bootstrap prefix SHA-256 mismatch")
        os.replace(temporary, directory / PARTIAL_NAME)
        partial = {"path": PARTIAL_NAME, "bytes": size, "sha256": expected_sha256, "seeded_bytes": size,
                   "total_bytes": total_bytes, "etag": etag, "last_modified": last_modified,
                   "mime_type": mime_type, "declared_mime_type": mime_type,
                   "final_url": final_url, "entity_key": _entity_key(final_url), **detection}
        state = {"schema_version": 1, "source_url": source_url, "public_offer": evidence, "complete": None, "partial": partial}
        _save(directory / MANIFEST_NAME, state)
        return {"status": "seeded", "bytes": size, "sha256": expected_sha256, "manifest_path": str(directory / MANIFEST_NAME), "network_bytes": 0}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
