"""Small anonymous adapters; bounded discovery is never a full-corpus claim."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlencode, urlsplit
import xml.etree.ElementTree as ET

from .http import FetchError, LimitReached
from .models import Batch, Record


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.title_depth = False
        self.title = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.title_depth = True
        if tag in {"script", "style", "nav", "header", "footer", "aside", "noscript"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self.title_depth = False
        if tag in {"script", "style", "nav", "header", "footer", "aside", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.title_depth:
            self.title.append(data)
        elif not self.skip:
            self.parts.append(data)


def html_text(text: str) -> tuple[str, str]:
    parser = TextHTML()
    parser.feed(text)
    return " ".join(" ".join(parser.parts).split()), " ".join(" ".join(parser.title).split())


def date(value) -> str | None:
    if not value:
        return None
    try:
        if isinstance(value, (float, int)):
            d = datetime.fromtimestamp(value, timezone.utc)
        else:
            try:
                d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                d = parsedate_to_datetime(value)
        return d.replace(tzinfo=timezone.utc).isoformat() if d.tzinfo is None else d.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, AttributeError):
        return None


def review(source) -> str:
    return source.get("review_status", source.get("options", {}).get("review_status", "needs_review"))


def simple(source, context) -> Batch:
    response = context.client.get(source["url"])
    text = response.body.decode("utf-8", errors="replace")
    title = source.get("title", "")
    if source["adapter"] == "html":
        text, extracted_title = html_text(text)
        title = title or extracted_title
    status = "available" if len(text.strip()) >= int(source.get("options", {}).get("min_chars", 20)) else "metadata_only"
    record = Record(response.url, title=title or response.url, text=text, work_id=source.get("work_id"), kind=source.get("kind", "article"), status=status, review_status=review(source), provenance={"retrieved_at": response.retrieved_at, "extraction": "bounded_stdlib_html" if source["adapter"] == "html" else "utf8_text", "decode_replacements": text.count("\ufffd")})
    return Batch([record], status="ok" if status == "available" else "partial", cursor={}, coverage={"scope": "registered_urls", "window_complete": status == "available", "corpus_complete": False})


def feed_records(body: bytes, source: dict) -> list[Record]:
    root = ET.fromstring(body)
    records = []
    local = lambda tag: tag.rsplit("}", 1)[-1]
    for entry in root.iter():
        if local(entry.tag) not in {"item", "entry"}:
            continue
        values = {}
        link = None
        for child in entry:
            name = local(child.tag)
            values[name] = "".join(child.itertext()).strip()
            if name == "link" and (child.get("rel", "alternate") == "alternate"):
                link = child.get("href") or values[name]
        if not link:
            continue
        text, _ = html_text(values.get("encoded") or values.get("content") or values.get("description") or values.get("summary") or "")
        records.append(Record(link, title=values.get("title", ""), text=text, source_item_id=values.get("guid") or values.get("id"), published_at=date(values.get("pubDate") or values.get("published") or values.get("updated")), kind=source.get("kind", "feed_entry"), status="metadata_only", review_status=review(source), provenance={"discovery_only": True, "text_completeness": "feed_excerpt_not_assumed_complete"}))
    return records


def rss(source, context) -> Batch:
    response = context.client.get(source["url"], accept="application/rss+xml,application/atom+xml,application/xml")
    current = feed_records(response.body, source)
    pending = [Record(**r) for r in context.cursor.get("pending_records", [])]
    old_seen = set(context.cursor.get("seen_urls", []))
    pending = [r for r in pending if r.canonical_url not in old_seen]
    new = [r for r in current if r.canonical_url not in old_seen]
    refresh = [r for r in current if r.canonical_url in old_seen]
    candidates = new[:max(1, context.max_items - bool(pending))] + pending + new[max(1, context.max_items - bool(pending)):]
    unique = {}
    for record in candidates:
        unique.setdefault(record.canonical_url, record)
    ordered = list(unique.values())
    selected, remaining = ordered[:context.max_items], ordered[context.max_items:]
    selected += refresh[:context.max_items - len(selected)]
    cursor = {"pending_records": [asdict(r) for r in remaining], "seen_urls": list(dict.fromkeys([*context.cursor.get("seen_urls", []), *(r.canonical_url for r in selected)]))[-2000:]}
    return Batch(selected, status="partial", cursor=cursor, coverage={"scope": "visible_feed_entries", "window_complete": None, "corpus_complete": False, "provider_returned": len(current), "selected": len(selected), "pending": len(remaining), "cap_reached": bool(remaining), "note": "Feed excerpts and retention are not a complete historical corpus"})


def _x_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    return value if re.fullmatch(r"[1-9][0-9]{0,29}", value) else None


def _x_handle(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,50}", value) else None


def captured_x_posts(payload, max_posts=1000):
    """Pure bounded reader for cached FxEmbed status/tweet/results/thread shapes.

    Returns primary raw post dictionaries, preserving the first duplicate. Quote
    objects remain attached to their primary post; they are not independent items.
    Callers pooling several observations choose their evidence order explicitly.
    """
    if isinstance(max_posts, bool) or not isinstance(max_posts, int) or not 1 <= max_posts <= 10000:
        raise ValueError("max_posts must be an integer between 1 and 10000")
    queue = deque([payload])
    result, seen, visits = [], set(), 0
    while queue and len(result) < max_posts and visits < max_posts * 8 + 32:
        entry = queue.popleft()
        visits += 1
        if isinstance(entry, list):
            queue.extendleft(reversed(entry[:max_posts * 8 + 32]))
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("code", 200) != 200:
            continue
        pid = _x_id(entry.get("id"))
        if pid:
            if pid not in seen:
                result.append(entry)
                seen.add(pid)
        elif entry.get("type") == "thread" and isinstance(entry.get("statuses"), list):
            queue.appendleft(entry["statuses"])
        elif entry.get("code", 200) == 200:
            child = entry.get("tweet") or entry.get("status") or entry.get("results")
            if isinstance(child, (dict, list)):
                queue.appendleft(child)
    return result


def _x_edges(tweet):
    edges = []
    parent = tweet.get("replying_to_status")
    embedded = parent if isinstance(parent, dict) and "text" in parent else None
    parent_id = _x_id(parent.get("id") or parent.get("status")) if isinstance(parent, dict) else _x_id(parent)
    reply = tweet.get("replying_to")
    if not parent_id and isinstance(reply, dict):
        value = reply.get("status") or reply.get("status_id")
        if isinstance(value, dict):
            parent_id, embedded = _x_id(value.get("id")), value
        else:
            parent_id = _x_id(value)
    if parent_id or parent or reply:
        edges.append(("reply_parent", parent_id, embedded, None if parent_id else "parent_id_unavailable"))
    quote = tweet.get("quote")
    if quote is None:
        quote = tweet.get("quoted_status")
    quote_id = _x_id(quote.get("id")) if isinstance(quote, dict) else _x_id(quote)
    quote_id = quote_id or _x_id(tweet.get("quoted_status_id"))
    if quote_id or quote is not None or tweet.get("quoted_status_id") is not None:
        edges.append(("quoted_post", quote_id, quote if isinstance(quote, dict) else None,
                      None if quote_id else "quote_id_unavailable"))
    return edges


def _context_pool(posts):
    if isinstance(posts, dict) and not _x_id(posts.get("id")):
        posts = list(posts.values())
    candidates = deque(captured_x_posts(posts or [], max_posts=10000))
    pool = {}
    while candidates and len(pool) < 10000:
        post = candidates.popleft()
        pid = _x_id(post.get("id"))
        if not pid or pid in pool:
            continue
        pool[pid] = post
        for _, target, embedded, _ in _x_edges(post):
            if isinstance(embedded, dict) and target and _x_id(embedded.get("id")) == target:
                candidates.append(embedded)
    return pool


def enrich_x_context(record, tweet, context_posts=None, *, max_entries=8, max_depth=3, max_text_chars=120000):
    """Return a new record with bounded, separately attributed captured context.

    This function never fetches, mutates raw inputs, creates context items, changes
    the primary text/author/identity, or assigns member identities to cited authors.
    The same raw input and pool produce identical stable metadata on every replay.
    """
    for name, value, maximum in (("max_entries", max_entries, 64), ("max_depth", max_depth, 8), ("max_text_chars", max_text_chars, 1_000_000)):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(name + " is outside the bounded context range")
    if not isinstance(tweet, dict) or not _x_id(tweet.get("id")):
        raise ValueError("Context enrichment requires a valid primary post ID")
    metadata = {k: v for k, v in record.metadata.items() if k != "x_context"}
    root_id = _x_id(tweet["id"])
    edges = _x_edges(tweet)
    if not edges:
        return replace(record, metadata=metadata)
    pool = _context_pool(context_posts)
    pending = deque((relation, pid, embedded, reason, root_id, 1, (root_id,)) for relation, pid, embedded, reason in edges)
    entries, seen_edges, used, omitted, limited = [], set(), 0, 0, False
    while pending:
        relation, pid, embedded, reason, via, depth, ancestors = pending.popleft()
        key = (relation, via, pid)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        if depth > max_depth or len(entries) >= max_entries or pid in ancestors:
            omitted += 1
            limited = True
            continue
        node = embedded if pid is not None and isinstance(embedded, dict) and _x_id(embedded.get("id")) == pid else pool.get(pid)
        author = node.get("author") if node and isinstance(node.get("author"), dict) else {}
        handle = _x_handle(author.get("screen_name"))
        name = author.get("name") if isinstance(author.get("name"), str) else None
        text = node.get("text") if node and isinstance(node.get("text"), str) else ""
        status = "unavailable" if node is None else "available" if text else "metadata_only"
        reason = reason or ("not_captured" if node is None else "text_unavailable" if not text else None)
        if node and (node.get("truncated") is True or node.get("is_truncated") is True):
            status, reason = "truncated", "provider_truncated"
        allowance = max_text_chars - used
        if len(text) > allowance:
            text, status, reason, limited = text[:allowance], "truncated", "context_text_cap", True
        used += len(text)
        entries.append({"relation": relation, "via_post_id": via, "depth": depth, "post_id": pid,
                        "url": f"https://x.com/{handle or 'i'}/status/{pid}" if pid else None,
                        "author_handle": handle, "author_name": name, "published_at": date(node.get("created_timestamp") or node.get("created_at")) if node else None,
                        "text": text, "status": status, "person_ids": [], "reason": reason})
        if node:
            pending.extend((kind, target, child, why, pid, depth + 1, (*ancestors, pid))
                           for kind, target, child, why in _x_edges(node))
    metadata["x_context"] = {"schema_version": 1, "thread_complete": False, "limited": limited,
                             "omitted_links": omitted, "entries": entries}
    return replace(record, metadata=metadata)


def _context_options(source):
    options = source.get("options", {})
    values = {}
    for name, default, maximum in (("max_entries", 8, 16), ("max_depth", 3, 5), ("max_text_chars", 120000, 200000), ("max_fetches", 1, 4)):
        value = options.get("context_" + name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError("Invalid bounded context_" + name)
        values[name] = value
    return values


def _enrich_x_records(records, tweets, source, context, captured):
    options = _context_options(source)
    maximum = options.pop("max_fetches")
    pool = _context_pool(captured)
    attempted, errors = [], []
    while True:
        enriched = [enrich_x_context(record, tweet, pool, **options) for record, tweet in zip(records, tweets)]
        missing = list(dict.fromkeys(entry["post_id"] for record in enriched
                                    for entry in record.metadata.get("x_context", {}).get("entries", [])
                                    if entry["status"] == "unavailable" and entry["post_id"] and entry["post_id"] not in attempted))
        if not missing or len(attempted) >= maximum or context.remaining_seconds() <= 1:
            break
        pid = missing[0]
        attempted.append(pid)
        try:
            response = context.client.get("https://api.fxtwitter.com/2/status/" + pid,
                                          max_bytes=min(context.max_bytes, 2_000_000))
            payload = json.loads(response.body)
            found = captured_x_posts(payload)
            if not found or _x_id(found[0].get("id")) != pid:
                raise FetchError("Context response ID did not match the requested public post")
            pool[pid] = found[0]
        except (FetchError, ValueError, TypeError) as error:
            errors.append({"post_id": pid, "error": str(error)[:300]})
    for record in enriched:
        if record.metadata.get("x_context"):
            record.provenance = {**record.provenance, "context_retrieval": {"attempted_ids": attempted,
                "errors": errors, "fetch_limit": maximum, "scope": "bounded linked context; not a complete thread"}}
    entries = [entry for record in enriched for entry in record.metadata.get("x_context", {}).get("entries", [])]
    return enriched, {"context_entries": len(entries), "context_unavailable": sum(e["status"] == "unavailable" for e in entries),
                      "context_lookup_requests": len(attempted), "context_lookup_errors": errors,
                      "thread_complete": False}


def post_record(tweet: dict, source: dict, provenance=None, *, context_posts=None) -> Record:
    if not isinstance(tweet, dict) or not _x_id(tweet.get("id")):
        raise FetchError("Invalid public X post ID")
    post_id = _x_id(tweet["id"])
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    handle = author.get("screen_name")
    if not isinstance(handle, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,50}", handle):
        handle = None
    text = tweet.get("text") if isinstance(tweet.get("text"), str) else ""
    attachments = []
    media_info = tweet.get("media") if isinstance(tweet.get("media"), dict) else {}
    for media in media_info.get("all", []) if isinstance(media_info.get("all", []), list) else []:
        if isinstance(media, dict):
            attachments.append({k: media[k] for k in ["id", "type", "url", "altText", "width", "height", "duration"] if k in media})
    record = Record(f"https://x.com/{handle or 'i'}/status/{post_id}", title=text.splitlines()[0][:180] if text else "X post " + post_id, text=text, identity_key="x:" + post_id, source_item_id=post_id, kind="social_post", author=handle, published_at=date(tweet.get("created_timestamp") or tweet.get("created_at")), status="truncated" if tweet.get("truncated") or tweet.get("is_truncated") else ("available" if text else "metadata_only"), review_status=review(source), work_id=source.get("work_id"), metadata={"author_id": str(author.get("id", "")), "reply_to": tweet.get("replying_to_status") or tweet.get("replying_to"), "language": tweet.get("lang"), "attachments": attachments}, provenance={"provider": "FxEmbed anonymous public API", "engagement_observed": {k: tweet.get(k) for k in ["views", "likes", "retweets"]}, **(provenance or {})})
    registered_handle = source.get("options", {}).get("handle") or urlsplit(source["url"]).path.strip("/").split("/")[0]
    author_matches = handle.casefold() == registered_handle.casefold() if handle and registered_handle and registered_handle != "i" else None
    if handle is None:
        record.person_ids = []
        record.review_status = "needs_review"
        record.tags.append("context-author-unverified")
        record.provenance.update(registered_handle=registered_handle, author_matches_registered_handle=None, attribution_status="Provider author handle absent or invalid; no registered-person attribution")
        record.metadata["verified_person_ids"] = []
        record.metadata["attribution_status"] = "unverified_missing_author"
    elif author_matches is False:
        record.person_ids = []
        record.review_status = "needs_review"
        record.tags.append("context-author-unverified")
        record.provenance.update(registered_handle=registered_handle, author_matches_registered_handle=False, attribution_status="Unverified author returned as timeline/context content; no registered-person attribution")
        record.metadata["verified_person_ids"] = []
        record.metadata["attribution_status"] = "unverified_timeline_context"
    elif author_matches is True:
        record.person_ids = source.get("person_ids", [])
    return enrich_x_context(record, tweet, context_posts)


def x_post(source, context) -> Batch:
    options = source.get("options", {})
    match = re.search(r"/status/(\d+)", source["url"])
    post_id = str(options.get("post_id") or (match.group(1) if match else ""))
    if not post_id.isdigit():
        raise ValueError("x_post needs a public post URL or numeric options.post_id")
    response = context.client.get("https://api.fxtwitter.com/2/status/" + post_id)
    data = json.loads(response.body)
    if not isinstance(data, dict):
        raise FetchError("FxEmbed did not return a public post object")
    tweet = data.get("tweet") or data.get("status")
    if not tweet and data.get("id"):
        tweet = data
    if not isinstance(tweet, dict) or data.get("code", 200) != 200:
        raise FetchError("FxEmbed did not return the requested public post")
    if str(tweet.get("id")) != post_id:
        raise FetchError("FxEmbed response ID did not match the requested public post")
    record = post_record(tweet, source, {"retrieved_at": response.retrieved_at, "endpoint": response.url})
    records, context_coverage = _enrich_x_records([record], [tweet], source, context, captured_x_posts(data))
    return Batch(records, status="ok" if record.status == "available" and not context_coverage["context_unavailable"] else "partial", cursor={}, coverage={"scope": "registered_post_ids", "window_complete": record.status == "available", "corpus_complete": False, **context_coverage})


def x_timeline(source, context) -> Batch:
    options = source.get("options", {})
    handle = options.get("handle") or urlsplit(source["url"]).path.strip("/").split("/")[0]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", handle):
        raise ValueError("Invalid public X handle")
    base = "https://api.fxtwitter.com/2/profile/" + handle + "/statuses"
    cursor = dict(context.cursor)
    old_seen = set(cursor.get("seen_ids", []))
    pending = captured_x_posts(cursor.get("pending_posts", []), max_posts=1001)
    captured = list(pending)
    pages = 0
    provider_count = 0
    errors = []
    next_cursor = cursor.get("next_cursor")
    fresh = []
    reached_boundary = False
    try:
        response = context.client.get(base + "?" + urlencode({"count": min(context.max_items, 50), "with_replies": "true"}))
        pages += 1
        payload = json.loads(response.body)
        if not isinstance(payload, dict) or payload.get("code", 200) != 200:
            raise FetchError("FxEmbed timeline service returned a non-success code")
        fresh = captured_x_posts(payload, max_posts=1001)
        captured.extend(fresh)
        provider_count += len(fresh)
        next_cursor = next_cursor or (payload.get("cursor") or {}).get("bottom")
        if len(fresh) + len(pending) > 1000:
            raise LimitReached("Timeline pending-candidate cap reached")
        def unique_posts(posts):
            seen = set()
            result = []
            for post in posts:
                pid = str(post.get("id") or "")
                if pid and pid not in seen:
                    seen.add(pid)
                    result.append(post)
            return result
        new = [r for r in fresh if str(r.get("id")) not in old_seen]
        refresh = [r for r in fresh if str(r.get("id")) in old_seen]
        pending = [r for r in pending if str(r.get("id")) not in old_seen]
        reserve = 1 if pending and context.max_items >= 2 else 0
        ordered = unique_posts(new[:context.max_items - reserve] + pending + new[context.max_items - reserve:])
        # Older paging has its own slot before known-item refreshes, so it cannot be starved by a busy first page.
        if "next_cursor" in cursor:
            next_cursor = cursor["next_cursor"]
        while len(ordered) < context.max_items and next_cursor and pages < context.max_pages:
            page = context.client.get(base + "?" + urlencode({"count": min(context.max_items, 50), "with_replies": "true", "cursor": next_cursor}))
            payload = json.loads(page.body)
            if not isinstance(payload, dict) or payload.get("code", 200) != 200:
                raise FetchError("FxEmbed history page returned a non-success code")
            pages += 1
            older = captured_x_posts(payload, max_posts=1001)
            captured.extend(older)
            if len(older) + len(ordered) > 1000:
                raise LimitReached("Timeline pending-candidate cap reached")
            provider_count += len(older)
            ordered = unique_posts(ordered + [t for t in older if str(t.get("id")) not in old_seen])
            next_cursor = (payload.get("cursor") or {}).get("bottom")
            if not older:
                next_cursor = None
                break
        selected, pending = ordered[:context.max_items], ordered[context.max_items:]
        selected = unique_posts(selected + refresh)[:context.max_items]
        reached_boundary = any((date(t.get("created_timestamp")) or "9999") <= context.window_start for t in fresh)
        records = [post_record(t, source, {"retrieved_at": response.retrieved_at, "route": "timeline", "edit_monitoring": "bounded timeline refresh; not exhaustive"}, context_posts=captured) for t in selected]
        records, context_coverage = _enrich_x_records(records, selected, source, context, captured)
        next_state = {"pending_posts": pending, "next_cursor": next_cursor, "seen_ids": list(dict.fromkeys([*cursor.get("seen_ids", []), *(str(t["id"]) for t in selected)]))[-2000:]}
        # An old/pinned result is not evidence that an anonymous provider returned every intervening post.
        return Batch(records, status="partial", cursor=next_state, errors=errors, coverage={"scope": "bounded_public_timeline", "window_complete": None, "observed_date_boundary": reached_boundary, "corpus_complete": False, "provider_returned": provider_count, "selected": len(records), "pages": pages, "pending": len(pending), "local_item_cap": context.max_items, "cap_reached": bool(pending) or bool(next_cursor), "historical_search": "not relied upon; previously failed", "paging_documented_not_exhaustively_tested": True, **context_coverage})
    except (FetchError, ValueError, KeyError) as exc:
        if isinstance(exc, LimitReached):
            return Batch(status="partial", cursor=cursor, errors=[str(exc)], coverage={"window_complete": False, "corpus_complete": False, "cap_reached": True})
        errors.append("Timeline unavailable: " + str(exc))
        # A distinct tested anonymous RSS route is a fallback, never a history proof.
        fallback = {**source, "adapter": "rss", "url": f"https://fxtwitter.com/{handle}/feed.xml?count={min(context.max_items, 50)}"}
        try:
            feed = context.client.get(fallback["url"])
            candidates = feed_records(feed.body, fallback)[:context.max_items]
            records = []
            for candidate in candidates:
                child = {**source, "adapter": "x_post", "url": candidate.canonical_url,
                         "options": {**source.get("options", {}), "context_max_fetches": 0}}
                try:
                    records.extend(x_post(child, context).records)
                except FetchError:
                    candidate.status = "truncated"
                    candidate.person_ids = []
                    candidate.review_status = "needs_review"
                    candidate.tags.append("context-author-unverified")
                    candidate.metadata.update(verified_person_ids=[], attribution_status="unverified_feed_excerpt")
                    candidate.provenance.update(feed_entry_url=candidate.canonical_url, registered_handle=handle,
                                                attribution_status="Known-post retrieval failed; feed excerpt does not verify its author")
                    match = re.search(r"/status/(\d+)", candidate.canonical_url)
                    candidate.identity_key = "x:" + match.group(1) if match else None
                    if match:
                        candidate.canonical_url = "https://x.com/i/status/" + match.group(1)
                    records.append(candidate)
            return Batch(records, status="partial", cursor=cursor, errors=errors, coverage={"route": "RSS fallback", "window_complete": None, "corpus_complete": False, "selected": len(records), "primary_timeline_failed": True})
        except (FetchError, ET.ParseError) as fallback_error:
            return Batch(status="unavailable", cursor=cursor, errors=errors + ["RSS fallback unavailable: " + str(fallback_error)], coverage={"window_complete": False, "corpus_complete": False})


BUILTINS = {"text": simple, "html": simple, "rss": rss, "x_post": x_post, "x_timeline": x_timeline}


def dispatch(source, context) -> Batch:
    name = source["adapter"]
    if name in BUILTINS:
        return BUILTINS[name](source, context)
    if name == "pdf":
        from pdf_adapter import collect
    else:
        from media.adapters import collect
    return collect(source, context)
