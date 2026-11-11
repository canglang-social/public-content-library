"""Run a bounded local collection workflow only when its user-chosen week is due."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

if __package__:
    from .storage import StorageUnavailable, require_storage, runtime_environment, unavailable_result
    from .audio_policy import estimate_disk_reserve, remaining_audio_seconds, remaining_collection_seconds
else:
    from storage import StorageUnavailable, require_storage, runtime_environment, unavailable_result
    from audio_policy import estimate_disk_reserve, remaining_audio_seconds, remaining_collection_seconds


def tree_bytes(path):
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def argument_limit(command, name, default):
    return float(command[command.index(name) + 1]) if name in command else default


def command_budget(command, seconds):
    command = list(command)
    if "--max-seconds" in command:
        command[command.index("--max-seconds") + 1] = str(max(.1, seconds))
    else:
        command += ["--max-seconds", str(max(.1, seconds))]
    return command


def disk_reservation(config, candidate_bytes=0, before_collection=False):
    state = Path(config["state"])
    native = 0
    for step in config.get("post_steps", []):
        if step["name"] == "publish_library" and "--data" in step["command"]:
            native = tree_bytes(step["command"][step["command"].index("--data") + 1])
    text_growth = 0
    if before_collection:
        # Text artifacts, native copies, their backups, and normalized records.
        text_growth = 6 * int(argument_limit(config.get("collector_args", []), "--max-artifact-bytes", 50_000_000))
    return estimate_disk_reserve(tree_bytes(state / "artifacts") + tree_bytes(state / "library.sqlite3"), native,
                                 queue_backup_bytes=tree_bytes(state / "audio"), candidate_bytes=candidate_bytes,
                                 minimum_free_bytes=config.get("minimum_free_bytes", 2 * 1024**3), other_growth_bytes=text_growth)


def now_utc():
    return datetime.now(timezone.utc)


def due_period(now, schedule):
    local = now.astimezone(ZoneInfo(schedule["timezone"]))
    candidate = (local - timedelta(days=(local.weekday() - schedule["weekday"]) % 7)).replace(hour=schedule["hour"], minute=schedule["minute"], second=0, microsecond=0)
    if candidate > local:
        candidate -= timedelta(days=7)
    return candidate


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def run_child(command, cwd, environment, output, error, timeout):
    with output.open("w") as stdout, error.open("w") as stderr:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
    return code


def validate_config(config):
    schedule = config["schedule"]
    if not 0 <= schedule["weekday"] <= 6 or not 0 <= schedule["hour"] <= 23 or not 0 <= schedule["minute"] <= 59:
        raise ValueError("Invalid user-selected schedule")
    ZoneInfo(schedule["timezone"])
    for key in ["module_root", "python", "registry", "state", "operations"]:
        if not Path(config[key]).is_absolute():
            raise ValueError(key + " must be an absolute path")
    if not Path(config["python"]).is_file() or not Path(config["registry"]).is_file():
        raise ValueError("Configured Python or source registry is absent")
    if not Path(config["module_root"], "collector", "__main__.py").is_file():
        raise ValueError("Collector installation is incomplete")


def digest_text(record):
    lines = ["Public Content Library — latest update", "", "Result: " + record["status"], "Schedule: " + json.dumps(record.get("schedule", {}), sort_keys=True), "Finished: " + str(record.get("finished_at", "not finished")), ""]
    for step in record.get("steps", []):
        if step.get("exit_code") not in {0, 2}:
            lines.append("Needs attention: " + step["name"].replace("_", " "))
    if record.get("error"):
        lines += ["", "Attention: " + record["error"]]
    summary = record.get("collection_summary")
    if summary:
        lines += ["New items: " + str(summary.get("new_items", "not recorded")), "Updated items: " + str(summary.get("changed_items", "not recorded")), "New saved versions: " + str(summary.get("new_versions", "not recorded")), "", "Source checks:"]
        for source in summary.get("sources", []):
            label = source["source_id"].replace("-", " ")
            line = "- " + label + ": " + source["status"]
            errors = source.get("errors", [])
            if errors:
                message = str(errors[0])
                if any(word in message.lower() for word in ["ssl", "connection", "urlerror"]):
                    message = "connection failed; existing captures retained"
                elif any(word in message.lower() for word in ["timeout", "time limit", "elapsed-time", "budget", "cap reached"]):
                    message = "run limit reached; more work remains"
                else:
                    message = message[:170]
                line += " — " + message
            lines.append(line)
    stats = record.get("library_stats")
    if stats:
        lines += ["", "Library: " + str(stats["items"]) + " items, " + str(stats["versions"]) + " saved versions.", "Awaiting professional-content review: " + str(stats["needs_review"]) + "."]
    audio = record.get("audio_summary")
    if audio:
        lines += ["", "Audio: " + audio["status"], "New or changed saved audio editions: " + str(audio.get("new_versions", 0)),
                  "Waiting audio downloads: " + str(audio.get("pending_count", "see queue")),
                  "Audio bytes downloaded this run: " + str(audio.get("media_bytes", audio.get("received_bytes", 0)))]
        if audio.get("deferred_reason"):
            lines.append(audio["deferred_reason"])
    lines += ["", "Open Library.command opens the searchable library. Sources and saved originals are linked from each captured item.", "Partial means there are unfinished queues, limits, or failed sources. It never means that no updates exist.", "Registered sources and observed pages are a bounded collection, not a complete historical corpus.", "When enabled, the separate bounded audio queue saves explicit public publisher offers. Other media platforms, video downloads and automatic transcription are not included in this queue.", "Detailed evidence and verified backups are in operations/runs. No paid AI API is used."]
    return "\n".join(lines) + "\n"


def execute(config, *, force=False, dry_run=False):
    validate_config(config)
    try:
        require_storage(config)
    except StorageUnavailable as error:
        return unavailable_result(config, error), 0
    operations = Path(config["operations"])
    operations.mkdir(parents=True, exist_ok=True)
    history = operations / "schedule-state.json"
    previous = json.loads(history.read_text()) if history.exists() else {}
    period = due_period(now_utc(), config["schedule"]).isoformat()
    completed = previous.get("period") == period and previous.get("status") in {"ok", "partial"}
    if dry_run:
        return {"status": "dry_run", "period": period, "due": force or not completed, "schedule": config["schedule"]}, 0
    lock = (operations / "weekly.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {"status": "already_running", "period": period}, 0
    try:
        # Re-read after acquiring the lock: a manual update may just have finished.
        previous = json.loads(history.read_text()) if history.exists() else {}
        if not force and previous.get("period") == period and previous.get("status") in {"ok", "partial"}:
            return {"status": "already_attempted_this_period", "period": period, "last_result": previous["status"]}, 0
        run_id = now_utc().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        run_dir = operations / "runs" / run_id
        run_dir.mkdir(parents=True)
        record = {"run_id": run_id, "period": period, "schedule": config["schedule"], "started_at": now_utc().isoformat(), "status": "running", "steps": []}
        write_json(history, record)
        deadline = time.monotonic() + config.get("max_runtime_seconds", 1200)
        environment = runtime_environment(config)
        for key in list(environment):
            if any(name in key.upper() for name in ["OPENAI", "ANTHROPIC_API", "GEMINI_API", "SMTP_PASSWORD", "AUTH_TOKEN"]):
                environment.pop(key, None)
        environment.update({"PYTHONPATH": config["module_root"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PATH": config.get("path", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")})
        common = [config["python"], "-m", "collector"]
        audio_enabled = config.get("audio", {}).get("enabled", False)
        reserve = config.get("audio", {}).get("finalization_reserve_seconds", 300) if audio_enabled else 0
        collection_deadline = None
        collection_cap = argument_limit(config.get("collector_args", []), "--max-seconds", 600)
        steps = [
            ("collect", common + ["run", "--registry", config["registry"], "--state", config["state"], "--resume-auto"] + config.get("collector_args", [])),
            ("export", common + ["export", "--state", config["state"], "--output", str(run_dir / "bundle.json")]),
            ("backup", common + ["backup", "--state", config["state"], "--output", str(run_dir / "backup")]),
            ("verify_backup", common + ["verify-backup", "--path", str(run_dir / "backup")]),
        ]
        substitutions = {"run_dir": str(run_dir), "bundle": str(run_dir / "bundle.json"), "state": config["state"], "python": config["python"], "module_root": config["module_root"]}
        pre_steps = [(pre["name"], [part.format(**substitutions) for part in pre["command"]]) for pre in config.get("pre_steps", [])]
        steps = pre_steps + steps
        if audio_enabled:
            write_json(run_dir / "settings.json", config)
            steps.insert(len(pre_steps) + 1, ("collect_audio", [config["python"], "-m", "media.audio_queue", "--config", str(run_dir / "settings.json")]))
        for post in config.get("post_steps", []):
            steps.append((post["name"], [part.format(**substitutions) for part in post["command"]]))
        exit_code = 0
        try:
            admission = disk_reservation(config, before_collection=True) if audio_enabled else {"required_free_bytes": config.get("minimum_free_bytes", 2 * 1024**3)}
            record["disk_reservation"] = admission
            if shutil.disk_usage(operations).free < admission["required_free_bytes"]:
                raise RuntimeError("Insufficient disk space for collection and verified backups above the configured floor; existing library data was preserved")
            for name, command in steps:
                require_storage(config)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Weekly workflow elapsed-time limit reached")
                output, error = run_dir / (name + ".json"), run_dir / (name + ".stderr")
                actual_command = command
                timeout = remaining
                skipped = None
                if audio_enabled and name in {"collect", "collect_current"}:
                    if collection_deadline is None:
                        collection_deadline = time.monotonic() + collection_cap
                    allocation = remaining_collection_seconds(remaining, collection_deadline - time.monotonic(), cap_seconds=collection_cap, finalization_reserve_seconds=reserve)
                    if allocation < 1:
                        skipped = {"status": "partial", "resumed": False, "window_end": now_utc().isoformat(), "deferred_reason": "Shared text-collection budget exhausted before this source-window check", "sources": [], "new_items": 0, "changed_items": 0, "new_versions": 0}
                        record["current_window_check_deferred"] = True
                    else:
                        actual_command = command_budget(command, allocation)
                        timeout = min(remaining, allocation + 10)
                if name == "collect_audio":
                    allocation = remaining_audio_seconds(remaining, cap_seconds=config["audio"].get("max_seconds", 600), finalization_reserve_seconds=reserve)
                    if allocation < 6:
                        skipped = {"status": "partial", "deferred_reason": "Audio deferred to preserve the library publication and backup time reserve", "attempts": [], "new_versions": 0}
                    else:
                        reservation = disk_reservation(config, candidate_bytes=config["audio"].get("max_file_bytes", 80_000_000))
                        record["audio_disk_reservation"] = reservation
                        actual_command = command + ["--max-seconds", str(allocation), "--required-free-bytes", str(reservation["required_free_bytes"])]
                        timeout = min(remaining, allocation + 10)
                if skipped:
                    write_json(output, skipped)
                    error.write_text("")
                    code = 2
                else:
                    try:
                        code = run_child(actual_command, config["module_root"], environment, output, error, timeout)
                    except subprocess.TimeoutExpired:
                        if not audio_enabled or name not in {"collect", "collect_current", "collect_audio"}:
                            raise
                        # Owned child is reaped. Retained state can resume next run;
                        # already committed content must still be published/backed up.
                        skipped = {"status": "partial", "resumed": False, "window_end": now_utc().isoformat(), "deferred_reason": name + " reached its time allocation; committed content and validated partials retained", "sources": [], "attempts": [], "new_items": 0, "changed_items": 0, "new_versions": 0}
                        write_json(output, skipped)
                        code = 2
                record["steps"].append({"name": name, "exit_code": code, "stdout": str(output), "stderr": str(error)})
                allowed = {0, 2} if name in {"collect", "collect_current", "collect_audio"} else {0}
                if code not in allowed:
                    raise RuntimeError(name + " did not complete successfully; see its retained log")
                if name in {"collect", "collect_current"}:
                    summary = json.loads(output.read_text())
                    record["collection_summary"] = summary
                    record.setdefault("collection_summaries", []).append(summary)
                    exit_code = max(exit_code, code)
                    # Resume keeps its original window. It cannot discharge a later
                    # scheduled period without also checking that period's sources.
                    older_window = summary.get("resumed") and datetime.fromisoformat(summary["window_end"]) < datetime.fromisoformat(period)
                    if name == "collect" and (older_window or summary.get("targeted")):
                        steps.insert(steps.index((name, command)) + 1, ("collect_current", common + ["run", "--registry", config["registry"], "--state", config["state"]] + config.get("collector_args", [])))
                        record["older_window_resumed_before_current_check"] = bool(older_window)
                        record["targeted_run_resumed_before_full_check"] = bool(summary.get("targeted"))
                if name == "collect_audio":
                    record["audio_summary"] = json.loads(output.read_text())
                    exit_code = max(exit_code, code)
                require_storage(config)
                write_json(run_dir / "result.json", record)
            record["status"] = "ok" if exit_code == 0 else "partial"
        except KeyboardInterrupt:
            record["status"] = "interrupted"
            record["error"] = "Interrupted; the next invocation will resume unfinished collector work"
            exit_code = 130
        except Exception as error:
            record["status"] = "failed"
            record["error"] = str(error)
            exit_code = 3
        try:
            require_storage(config)
        except StorageUnavailable as error:
            result = unavailable_result(config, error)
            result.update(run_id=run_id, data_changed="possible_before_disconnect")
            return result, 0
        record["finished_at"] = now_utc().isoformat()
        if (run_dir / "bundle.json").exists():
            bundle = json.loads((run_dir / "bundle.json").read_text())
            record["library_stats"] = {"items": len(bundle["items"]), "versions": sum(len(item["versions"]) for item in bundle["items"]), "needs_review": sum(item["review_status"] == "needs_review" for item in bundle["items"])}
        write_json(run_dir / "result.json", record)
        write_json(history, record)
        write_json(operations / "latest-result.json", record)
        digest_temp = operations / ("latest-update." + run_id + ".tmp")
        digest_temp.write_text(digest_text(record))
        digest_temp.replace(operations / "latest-update.txt")
        if (run_dir / "bundle.json").exists():
            bundle_temp = operations / ("latest-bundle." + run_id + ".tmp")
            shutil.copy2(run_dir / "bundle.json", bundle_temp)
            bundle_temp.replace(operations / "latest-bundle.json")
        return record, exit_code
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--force", action="store_true", help="Run now even if this week already had an attempt")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        result, code = execute(json.loads(Path(args.config).read_text()), force=args.force, dry_run=args.dry_run)
    except Exception as error:
        result, code = {"status": "configuration_error", "error": str(error)}, 3
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
