# Configuration

`pcl init` writes `settings.json` and `sources.json` outside the repository. It requires an explicit weekday, time and IANA timezone. It creates no service. Use stable absolute paths, including a program installation outside folders subject to interactive macOS document-access prompts.

Core settings are `module_root`, `python`, `registry`, `state`, `operations`, `control` and `schedule`. `control` should stay on the computer; `state` and `operations` may live on external storage. `max_runtime_seconds`, `minimum_free_bytes` and `collector_args` bound work. `job_label_prefix` distinguishes multiple Mac installations; choose a unique prefix before installing jobs.

`sources.json` has `schema_version: 1` and a `sources` list. Each entry needs a stable `id`, adapter name and source URL. `enabled: false` excludes a source from fetching. `review_status` is `included`, `needs_review` or `excluded`; a source being public does not establish professional relevance. Use `person_ids` only with separately verified attribution. The examples are synthetic, disabled and contain no personal watchlist.

Built-in adapters include `html`, `text`, `rss`, `x_post` and `x_timeline` (see the dispatch table in `collector/adapters.py` for exact names). Additional adapters include `html_article`, `html_index`, `publisher_transcript`, `youtube_channel_rss`, `youtube_video`, `pdf` and `local_asr_excerpt`. Site-specific extraction options are code-level integrations in this early release; a URL alone does not promise a correct transcript.

## Audio

Install FFmpeg separately and make `ffprobe` available. Enable the following private setting after reviewing disk space:

```json
{
  "audio": {
    "enabled": true,
    "max_file_bytes": 100000000,
    "max_run_bytes": 100000000,
    "max_seconds": 600,
    "max_completed": 1,
    "max_attempts": 3,
    "finalization_reserve_seconds": 300,
    "ffprobe": "ffprobe",
    "max_stale_days": 8
  }
}
```

The queue discovers publisher-declared public audio from included transcript records. It resumes validated prefixes, checks response identity, validates complete audio and publisher duration, and retains older complete editions. A deferral or failed remote check preserves existing downloads. A request cap, network failure, oversized file or unavailable publisher can leave work pending. Automated validation is not a listening review.

The default resolver uses the normal network path. An optional `resolver: "cloudflare_doh"` selects application-level public DNS resolution for the audio downloader; it does not change system DNS, VPN or proxy settings. This option still depends on network reachability and is not an access guarantee.

## Exit status

Core commands normally return `0` on success, `2` on partial/deferred collection and `3` on error. A scheduled invocation with an absent external disk exits `0` with JSON status `storage_unavailable`, preventing a retry loop while leaving the weekly checkpoint incomplete. Always inspect the JSON `status`; exit code alone is not a coverage claim.
