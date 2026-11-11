"""One public video's metadata and timed captions; never download original media."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import requests
from youtube_transcript_api import YouTubeTranscriptApi

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media.extractors import parse_vtt, youtube_id


class BoundedSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (10, 20))
        return super().request(method, url, **kwargs)


def run(video_url: str, out: Path, max_seconds: float, max_bytes: int) -> dict:
    started = time.monotonic()
    video_id = youtube_id(video_url)
    out.mkdir(parents=True, exist_ok=True)
    result = {"video_id": video_id, "canonical_url": "https://www.youtube.com/watch?v=" + video_id,
              "status": "unavailable", "errors": [], "segments": [], "metadata": {},
              "versions": {n: importlib.metadata.version(n) for n in ["yt-dlp", "youtube-transcript-api"]},
              "network_accounting": "Tool-managed anonymous requests; response-body output bounded separately from collector HTTP accounting. No original media requested."}
    cli = Path(sys.executable).with_name("yt-dlp")
    command = [str(cli), "--ignore-config", "--no-plugin-dirs", "--no-cache-dir", "--no-mark-watched",
               "--no-remote-components", "--no-playlist", "--skip-download", "--dump-single-json",
               "--socket-timeout", "12", "--retries", "1", "--extractor-retries", "1"]
    node = shutil.which("node")
    if node:
        command += ["--js-runtimes", "node:" + node]
    command.append(result["canonical_url"])
    info = {}
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=max(5, min(60, max_seconds * 0.55)))
        (out / "yt-dlp.log").write_text(process.stderr)
        if process.returncode:
            raise RuntimeError("yt-dlp metadata exit " + str(process.returncode) + ": " + process.stderr[-1200:])
        if len(process.stdout.encode()) > max_bytes:
            raise RuntimeError("yt-dlp metadata exceeded output-byte cap")
        info = json.loads(process.stdout)
        result["metadata"] = {k: info.get(k) for k in ["id", "title", "description", "uploader", "uploader_id", "channel", "channel_id", "channel_url", "upload_date", "duration", "categories", "tags", "language", "chapters", "webpage_url"]}
        for field in ["categories", "tags"]:
            if isinstance(result["metadata"].get(field), list):
                result["metadata"][field] = sorted(result["metadata"][field])
        (out / "video-metadata.json").write_text(json.dumps(result["metadata"], ensure_ascii=False, indent=2) + "\n")
    except Exception as error:
        result["errors"].append({"stage": "metadata", "type": type(error).__name__, "message": str(error)})
    (out / "probe-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    languages = ["en", "en-US", "en-GB"]
    try:
        available = list(YouTubeTranscriptApi(http_client=BoundedSession()).list(video_id))
        selected = next((t for generated in [False, True]
                         for language in languages + sorted({t.language_code for t in available} - set(languages))
                         for t in available if t.language_code == language and t.is_generated == generated), None)
        if selected is None:
            raise ValueError("No caption language available")
        fetched = selected.fetch()
        raw = fetched.to_raw_data()
        raw_bytes = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode()
        if len(raw_bytes) > max_bytes:
            raise RuntimeError("Caption response exceeded byte cap")
        (out / "captions.source.json").write_bytes(raw_bytes)
        result["segments"] = [{"start": round(float(s["start"]), 6), "end": round(float(s["start"] + s["duration"]), 6), "text": s["text"]} for s in raw]
        result["caption"] = {"tool": "youtube-transcript-api", "language": fetched.language_code, "machine_generated": fetched.is_generated, "origin": "platform_auto_caption" if fetched.is_generated else "platform_manual_caption"}
    except Exception as error:
        result["errors"].append({"stage": "caption_api", "type": type(error).__name__, "message": str(error)})
        # Public caption URL fallback supplied by the same public video metadata.
        selected_track = None
        for generated, field in [(False, "subtitles"), (True, "automatic_captions")]:
            tracks = info.get(field) or {}
            for language in languages + ["en-orig"] + sorted(set(tracks) - set(languages + ["en-orig"])):
                for track in (info.get(field) or {}).get(language, []):
                    if track.get("ext") == "vtt" and track.get("url"):
                        selected_track = (track, generated, language)
                        break
                if selected_track:
                    break
            if selected_track:
                break
        if selected_track:
            try:
                track, generated, language = selected_track
                with BoundedSession().get(track["url"], stream=True) as response:
                    response.raise_for_status()
                    chunks, total = [], 0
                    for chunk in response.iter_content(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeError("VTT exceeded byte cap")
                        chunks.append(chunk)
                body = b"".join(chunks)
                (out / "captions.source.vtt").write_bytes(body)
                result["segments"] = parse_vtt(body.decode("utf-8", errors="replace"))
                result["caption"] = {"tool": "yt-dlp-public-caption-url", "language": language, "machine_generated": generated, "origin": "platform_auto_caption" if generated else "platform_manual_caption"}
            except Exception as error:
                result["errors"].append({"stage": "caption_vtt_fallback", "type": type(error).__name__, "message": str(error)})
    if result["segments"]:
        for segment in result["segments"]:
            if segment["start"] < 0 or segment["end"] < segment["start"]:
                raise ValueError("Caption has an invalid timestamp interval")
        result["text"] = "\n".join(s["text"] for s in result["segments"])
        result["status"] = "ok" if result["metadata"] else "partial"
        result["metadata"].update({"caption_language": result["caption"]["language"], "caption_origin": result["caption"]["origin"], "caption_machine_generated": result["caption"]["machine_generated"]})
        duration = result["metadata"].get("duration")
        result["timing_observations"] = {"start_times_monotone": all(b["start"] >= a["start"] for a, b in zip(result["segments"], result["segments"][1:])),
                                         "segments_beyond_declared_duration": sum(s["end"] > duration for s in result["segments"]) if isinstance(duration, (int, float)) else None,
                                         "audio_alignment_listening_audit": False}
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["original_media_downloaded"] = False
    (out / "probe-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-seconds", type=float, default=120)
    parser.add_argument("--max-bytes", type=int, default=5_000_000)
    args = parser.parse_args()
    result = run(args.url, args.output_dir, args.max_seconds, args.max_bytes)
    print(json.dumps({"status": result["status"], "video_id": result["video_id"], "segments": len(result["segments"]), "elapsed_seconds": result["elapsed_seconds"]}))
