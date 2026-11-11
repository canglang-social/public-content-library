"""Pure extraction helpers. Fetching, persistence, and scheduling live elsewhere."""
from __future__ import annotations

import hashlib
import html
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit


def fingerprint(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalized_text(value: str) -> str:
    return " ".join(value.split())


def content_fingerprints(text: str, segments: list, metadata: dict) -> dict:
    """Timing and selected source metadata count as changes independently of words."""
    return {
        "text_sha256": fingerprint(normalized_text(text)),
        "segments_sha256": fingerprint(segments),
        "metadata_sha256": fingerprint(metadata),
        "combined_sha256": fingerprint({"text": normalized_text(text), "segments": segments, "metadata": metadata}),
    }


def soup_from(body: bytes):
    from bs4 import BeautifulSoup
    return BeautifulSoup(body.decode("utf-8", errors="replace"), "html.parser")


def clean_body(node) -> str:
    for unwanted in node.select("script, style, noscript, nav, footer, form"):
        unwanted.decompose()
    return node.get_text("\n", strip=True)


def structured_metadata(soup) -> dict:
    """Read publication metadata only; never manufacture a date from retrieval time."""
    candidates = []

    def walk(value):
        if isinstance(value, dict):
            kind = value.get("@type", "")
            kinds = kind if isinstance(kind, list) else [kind]
            if set(kinds) & {"Article", "NewsArticle", "BlogPosting", "ScholarlyArticle"}:
                candidates.append(value)
            for child in value.values():
                if isinstance(child, (list, dict)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.get_text()))
        except (ValueError, TypeError):
            pass
    item = candidates[0] if candidates else {}
    meta = {tag.get("property") or tag.get("name"): tag.get("content") for tag in soup.select("meta[content]")}
    author = item.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    elif isinstance(author, list):
        author = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in author)
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else item.get("headline") or meta.get("og:title")
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    canonical = soup.find("link", rel="canonical")
    return {
        "title": title,
        "published_at": item.get("datePublished") or meta.get("article:published_time"),
        "modified_at": item.get("dateModified") or meta.get("article:modified_time"),
        "author": author or meta.get("author"),
        "description": item.get("description") or meta.get("description") or meta.get("og:description"),
        "declared_canonical_url": canonical.get("href") if canonical else meta.get("og:url"),
    }


def extract_article(body: bytes, source: dict) -> dict:
    soup = soup_from(body)
    metadata = structured_metadata(soup)
    title_selector = source.get("options", {}).get("title_selector")
    if title_selector:
        heading = soup.select_one(title_selector)
        if heading is None or not heading.get_text(" ", strip=True):
            raise ValueError("Registered article title selector is absent or empty")
        metadata["title"] = heading.get_text(" ", strip=True)
    if source.get("options", {}).get("require_substack_public_audience"):
        preload = soup.find("script", id="preloads-json")
        try:
            data = json.loads(preload.get_text()) if preload else {}
            post = data.get("post", {})
        except (ValueError, AttributeError) as error:
            raise ValueError("Publisher public-access declaration is unreadable") from error
        if (post.get("audience") != "everyone"
                or post.get("free_unlock_required") is not False
                or post.get("is_editor_preview") is True
                or data.get("is_editor_preview") is True
                or soup.select_one('[class*="paywall"], [data-testid*="paywall"]')):
            raise ValueError("Publisher page is restricted, a preview, or lacks an explicit public-access declaration; complete capture not asserted")
        metadata["access_check"] = {"policy": "explicit_public_substack_post", "publisher_audience": "everyone", "free_unlock_required": False}
    selectors = source.get("options", {}).get("body_selectors", ["article .blog-item-content", "article", "main"])
    node = next((found for selector in selectors if (found := soup.select_one(selector))), None)
    if node is None:
        raise ValueError("Expected article/body container is absent; refusing navigation-only text")
    text = clean_body(node)
    minimum = source.get("options", {}).get("min_text_characters", 400)
    if len(text) < minimum:
        raise ValueError(f"Body has {len(text)} characters; expected at least {minimum}")
    if sum(character.isalpha() for character in text) < min(80, max(2, minimum // 2)):
        raise ValueError("Body contains too little readable prose; interactive/ASCII-art shell is not accepted as an article")
    if normalized_text(metadata["title"] or "").lower() in {"just a moment", "just a moment...", "access denied", "verify you are human", "sign in", "log in"}:
        raise ValueError("Page presents an access/login screen rather than the article")
    if not metadata["title"]:
        metadata["title"] = source.get("title") or source["url"]
    return {"title": metadata["title"], "text": text, "segments": [], "metadata": metadata}


def seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        parts.insert(0, "0")
    if len(parts) != 3:
        raise ValueError("Unsupported timestamp: " + value)
    return round(float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2]), 6)


RANGE = re.compile(r"^\((\d{1,2}:\d{2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2}:\d{2})\):?$")
CHAPTER = re.compile(r"^\((\d{1,2}:\d{2}:\d{2})\)\s*[-–]\s*(.+)$")


def publisher_segments(text: str) -> tuple[list, list]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    segments, chapters = [], []
    current = None
    speaker = None
    for index, line in enumerate(lines):
        if index + 1 < len(lines) and RANGE.fullmatch(lines[index + 1]):
            speaker = line
            continue
        match = RANGE.fullmatch(line)
        if match:
            if current:
                current["text"] = "\n".join(current["text"]).strip()
                segments.append(current)
            start, end = map(seconds, match.groups())
            if end < start:
                raise ValueError("Publisher segment ends before it starts")
            current = {"start": start, "end": end, "text": [], "speaker": speaker}
            speaker = None
            continue
        chapter = CHAPTER.fullmatch(line)
        if chapter:
            chapters.append({"start": seconds(chapter[1]), "title": chapter[2]})
            continue
        if current:
            current["text"].append(line)
    if current:
        current["text"] = "\n".join(current["text"]).strip()
        segments.append(current)
    return segments, chapters


def extract_publisher(body: bytes, source: dict) -> dict:
    soup = soup_from(body)
    metadata = structured_metadata(soup)
    post = {}
    preload = soup.find("script", id="preloads-json")
    if preload:
        try:
            post = json.loads(preload.get_text()).get("post", {})
        except (ValueError, AttributeError):
            pass
    node = soup.select_one(".body.markup")
    if node is None:
        raise ValueError("Publisher transcript body container absent")
    text = clean_body(node)
    heading = re.search(r"(?m)^Transcript\s*$", text)
    if not heading:
        raise ValueError("Publisher Transcript heading absent; page is not accepted as a transcript")
    text = text[heading.end():].lstrip()
    if len(text) < source.get("options", {}).get("min_text_characters", 1000):
        raise ValueError("Publisher transcript is unexpectedly short")
    segments, chapters = publisher_segments(text)
    if not segments:
        raise ValueError("Publisher transcript has no recognized time ranges")
    metadata.update({
        "title": post.get("title") or metadata["title"] or source.get("title"),
        "published_at": post.get("post_date") or metadata["published_at"],
        "publisher_post_id": str(post["id"]) if post.get("id") is not None else None,
        "duration_seconds": post.get("podcast_duration"),
        "original_audio_url": post.get("podcast_url"),
        "chapters": chapters,
        "transcript_provenance": "publisher_supplied",
        "generation_method": "unspecified_by_publisher",
    })
    return {"title": metadata["title"], "text": text, "segments": segments, "metadata": metadata}


def parse_vtt(raw: str) -> list:
    """Keep repeated speech and exact cue intervals; never deduplicate adjacent lines."""
    blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n").strip())
    rows = []
    cue = re.compile(r"((?:\d{1,2}:)?\d{2}:\d{2}\.\d+)\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}\.\d+)")
    for block in blocks:
        if block.startswith(("NOTE", "STYLE", "REGION")):
            continue
        lines = block.splitlines()
        for i, line in enumerate(lines):
            match = cue.search(line)
            if not match:
                continue
            text = html.unescape(re.sub(r"<[^>]+>", "", "\n".join(lines[i + 1:]))).strip()
            start, end = map(seconds, match.groups())
            if end < start:
                raise ValueError("VTT cue ends before it starts")
            if text:
                rows.append({"start": start, "end": end, "text": text})
            break
    if not rows:
        raise ValueError("No VTT caption cues found")
    return rows


def youtube_id(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        value = parse_qs(parsed.query).get("v", [""])[0]
    elif host in {"youtu.be", "www.youtu.be"}:
        value = parsed.path.strip("/")
    else:
        raise ValueError("Unsupported YouTube URL host")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise ValueError("Expected one YouTube video ID")
    return value


def candidate_url_allowed(url: str, source: dict) -> bool:
    parsed = urlsplit(urljoin(source["url"], url))
    options = source.get("options", {})
    allowed_hosts = set(options.get("allowed_hosts", [urlsplit(source["url"]).hostname]))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in allowed_hosts or parsed.username or parsed.password:
        return False
    prefixes = options.get("allowed_path_prefixes", [])
    if prefixes and not any(parsed.path.startswith(prefix) for prefix in prefixes):
        return False
    if any(parsed.path.startswith(prefix) for prefix in options.get("excluded_path_prefixes", [])):
        return False
    if parsed.path.rstrip("/") in options.get("excluded_paths", []):
        return False
    return parsed.path.rstrip("/") != urlsplit(source["url"]).path.rstrip("/")


def article_candidates(body: bytes, source: dict) -> list[dict]:
    soup = soup_from(body)
    source_host = urlsplit(source["url"]).hostname
    options = source.get("options", {})
    seen = set()
    candidates = []
    for anchor in soup.select(options.get("discovery_selector", "a[href]")):
        parsed = urlsplit(urljoin(source["url"], anchor["href"]))
        if not candidate_url_allowed(anchor["href"], source):
            continue
        # These registered first-party article sites identify articles by path.
        # These verified sites redirect their bare/www aliases to one host.
        host = urlsplit(source["url"]).netloc if (parsed.hostname or "").removeprefix("www.") == (source_host or "").removeprefix("www.") else parsed.netloc
        url = urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))
        if url in seen:
            continue
        title = anchor.get_text(" ", strip=True)
        if not title or title.lower() in {"read more", "see more", "view more"}:
            continue
        seen.add(url)
        candidates.append({"url": url, "title": title})
    return candidates
