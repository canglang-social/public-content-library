"""Plain searchable renditions keep X context separate from the primary author."""


def context_text(content):
    metadata = content.get("metadata") or {}
    context = metadata.get("x_context") if isinstance(metadata, dict) else None
    if not isinstance(context, dict) or context.get("schema_version") != 1:
        return ""
    entries = context.get("entries")
    if not isinstance(entries, list) or not entries:
        return ""
    lines = ["Conversation context — separate authors",
             "These posts provide context, not evidence that their authors worked at Anthropic. The conversation may be incomplete."]
    for entry in entries[:64]:
        if not isinstance(entry, dict):
            continue
        relation = {"reply_parent": "Reply parent", "quoted_post": "Quoted post"}.get(entry.get("relation"), "Context post")
        handle = entry.get("author_handle")
        author = "@" + handle if isinstance(handle, str) and handle else "Author unavailable"
        name = entry.get("author_name")
        if isinstance(name, str) and name:
            author += " (" + name + ")"
        lines += ["", relation + " — " + author]
        for key in ("url", "published_at"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                lines.append(value)
        status = entry.get("status", "unavailable")
        lines.append("Context capture: " + str(status))
        text = entry.get("text")
        lines.append(text if isinstance(text, str) and text else "No captured text available.")
        reason = entry.get("reason")
        if isinstance(reason, str) and reason:
            lines.append("Capture note: " + reason)
    if context.get("limited"):
        lines += ["", "Context was limited by the capture budget; additional links may be missing."]
    if len(entries) > 64:
        lines += ["", "This reading view shows only the first 64 context entries."]
    return "\n".join(lines)


def searchable_text(content):
    primary = content.get("text") or ""
    context = context_text(content)
    return primary + ("\n\n" + context if context else "")
