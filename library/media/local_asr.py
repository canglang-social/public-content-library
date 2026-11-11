"""Explicit, bounded local-ASR fallback for already accessible public audio excerpts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import shutil

from collector.models import Artifact, Batch, Record
from media.extractors import content_fingerprints

MODEL_SHA256 = "c77c5766f1cef09b6b7d47f21b546cbddd4157886b3b5d6d4f709e91e66c7c2b"


def collect_excerpt(source, context):
    def remaining(cap):
        value = min(cap, context.remaining_seconds())
        if value <= 0:
            raise TimeoutError("Local ASR source time budget exhausted")
        return value

    options = source.get("options", {})
    if not options.get("audio_is_public") or not options.get("local_audio_path"):
        raise ValueError("Local ASR requires an explicit accessible public-audio path; it never downloads media automatically")
    if "source_offset_seconds" not in options:
        raise ValueError("Source audio offset must be supplied explicitly")
    audio = Path(options["local_audio_path"]).resolve(strict=True)
    byte_limit = min(context.max_bytes, int(options.get("max_audio_bytes", 8_000_000)))
    if audio.stat().st_size > byte_limit:
        raise ValueError(f"Audio exceeds explicit byte cap {byte_limit}")
    duration_limit = min(45.0, float(options.get("max_audio_seconds", 45)))
    probe = subprocess.run([(shutil.which("ffprobe") or "ffprobe"), "-v", "error", "-show_format", "-of", "json", str(audio)], capture_output=True, text=True, timeout=remaining(15), check=True)
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    if not 0 < duration <= duration_limit:
        raise ValueError(f"Audio duration {duration} exceeds explicit excerpt cap {duration_limit}")
    model = Path(options["model_path"])
    if hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError("ASR model does not match the verified official tiny.en-q5_1 hash")
    binary = Path(options["whisper_binary"])
    folder = context.artifact_dir / "local-asr"
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / "input.wav"
    subprocess.run([(shutil.which("ffmpeg") or "ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(audio), "-t", str(duration_limit), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], capture_output=True, text=True, timeout=remaining(30), check=True)
    prefix = folder / "transcript"
    command = [str(binary), "-m", str(model), "-f", str(wav), "-l", "en", "-t", "4", "-oj", "-otxt", "-osrt", "-of", str(prefix)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=remaining(120), check=True)
    (folder / "transcribe.log").write_text(result.stdout + "\n" + result.stderr)
    raw = json.loads(prefix.with_suffix(".json").read_text())
    offset = float(options["source_offset_seconds"])
    segments, clipped = [], []
    for item in raw.get("transcription", []):
        start, end = item["offsets"]["from"] / 1000, item["offsets"]["to"] / 1000
        if end > duration or start > duration:
            clipped.append({"start": start, "end": end})
        segments.append({"start": round(offset + min(start, duration), 6), "end": round(offset + min(end, duration), 6), "text": item["text"].strip()})
    if not segments:
        raise ValueError("ASR returned no transcript segments")
    text = "\n".join(s["text"] for s in segments)
    metadata = {"source_url": source["url"], "person_ids": source.get("person_ids", []), "machine_generated": True,
                "model": "tiny.en-q5_1", "model_sha256": MODEL_SHA256, "language": "en",
                "coverage": "excerpt_only", "source_offset_seconds": offset, "duration_seconds": duration,
                "original_audio_url": options.get("audio_source_url"),
                "source_audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}
    record = Record(canonical_url=source["url"], identity_key=f"asr-excerpt:{source['url']}:{offset}:{duration}",
                    work_id=source.get("work_id"), title=source.get("title", "Public audio excerpt") + " — local machine transcript excerpt",
                    text=text, segments=segments, metadata=metadata, kind="transcript_excerpt", status="truncated",
                    review_status=source.get("review_status", "needs_review"), author=source.get("author"), tags=source.get("tags", []) + ["machine-transcript", "excerpt"],
                    provenance={"adapter": "local_asr_excerpt", "retrieved_at": context.now, "tool": "whisper.cpp v1.9.5",
                                "raw_timestamp_overruns": clipped, "index_policy": "Only derived endpoints clamped to measured excerpt duration; raw JSON/SRT retained",
                                "accuracy": "Machine-generated; no calibrated word-error rate or listening audit asserted",
                                "content_fingerprints": content_fingerprints(text, segments, metadata)},
                    artifacts=[Artifact(path=p, media_type=mime, role=role, source_url=source["url"]) for p, mime, role in [
                        (wav, "audio/wav", "public_audio_excerpt"), (prefix.with_suffix(".json"), "application/json", "raw_machine_transcript"),
                        (prefix.with_suffix(".srt"), "application/x-subrip", "raw_machine_transcript")]])
    return Batch(records=[record], status="partial", cursor={}, coverage={"scope": "explicit_public_audio_excerpt", "window_complete": False, "corpus_complete": False,
                                                                        "excerpt_seconds": duration, "full_media_transcript": False})
