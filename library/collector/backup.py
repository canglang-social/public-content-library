"""Portable locked SQLite snapshots with verified relative immutable objects."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from .http import utc_now
from .runner import atomic_json, state_lock
from .store import Store


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(state, output):
    output = Path(output).resolve()
    state = Path(state).resolve()
    if output == state or output.is_relative_to(state):
        raise ValueError("Backup destination must be separate from collector state")
    if output.exists():
        raise ValueError("Backup destination must not already exist")
    with state_lock(state):
        store = Store(state)
        try:
            output.mkdir(parents=True)
            target = sqlite3.connect(output / "library.sqlite3")
            try:
                store.db.backup(target)
            finally:
                target.close()
            files = []
            for row in store.db.execute("SELECT * FROM artifacts ORDER BY path"):
                original = store.resolve_artifact(row["path"])
                if sha256(original) != row["hash"]:
                    raise ValueError("Source artifact failed integrity check: " + row["path"])
                destination = output / row["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
                destination.chmod(0o444)
                files.append({"path": row["path"], "sha256": row["hash"], "bytes": row["size"]})
            # The optional audio queue owns resumable prefixes and validators.
            # A portable restore must preserve these together, under the same lock.
            audio = state / "audio"
            if audio.is_symlink():
                raise ValueError("Audio queue root cannot be a symlink")
            if audio.exists():
                for original in sorted(audio.rglob("*")):
                    if original.is_symlink():
                        raise ValueError("Audio queue backup refuses symlinks")
                    if not original.is_file():
                        continue
                    if original.name.endswith(".lock") or ".tmp-" in original.name:
                        continue
                    relative = original.relative_to(state)
                    destination = output / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, destination)
                    files.append({"path": str(relative), "sha256": sha256(original), "bytes": original.stat().st_size})
            database = output / "library.sqlite3"
            files.append({"path": "library.sqlite3", "sha256": sha256(database), "bytes": database.stat().st_size})
            manifest = {"schema_version": 1, "created_at": utc_now(), "type": "neutral_collector_backup", "files": files,
                        "audio_queue_included": audio.exists(),
                        "registry_included": bool(store.db.execute("SELECT 1 FROM settings WHERE key='registry_path'").fetchone()),
                        "source_state_id": store.db.execute("SELECT value FROM settings WHERE key='state_id'").fetchone()[0]}
            atomic_json(output / "backup-manifest.json", manifest)
        finally:
            store.db.close()
    return verify_backup(output)


def verify_backup(output):
    output = Path(output).resolve()
    manifest = json.loads((output / "backup-manifest.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("type") != "neutral_collector_backup":
        raise ValueError("Unrecognized backup manifest")
    for file in manifest["files"]:
        relative = Path(file["path"])
        path = (output / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(output) or not path.is_file() or path.is_symlink():
            raise ValueError("Unsafe/missing backup path: " + str(relative))
        if path.stat().st_size != file["bytes"] or sha256(path) != file["sha256"]:
            raise ValueError("Backup integrity mismatch: " + str(relative))
    db = sqlite3.connect(f"file:{output / 'library.sqlite3'}?mode=ro", uri=True)
    try:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise ValueError("Backup database integrity check failed")
        tracked = {r[0] for r in db.execute("SELECT path FROM artifacts")}
        manifested = {f["path"] for f in manifest["files"]}
        if not tracked.issubset(manifested):
            raise ValueError("Backup manifest omits referenced objects")
    finally:
        db.close()
    return {"schema_version": 1, "status": "ok", "backup_path": str(output), "files_verified": len(manifest["files"]), "registry_included": manifest["registry_included"], "database_integrity": "ok", "external_paths_required": False}
