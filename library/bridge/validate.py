"""Bounded native bridge trial. Synthetic records; no source downloads or ZIP route."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

from collector.models import Artifact, Batch, Record
from collector.runner import atomic_json, collect
from collector.store import Store
from bridge.archivebox import PYTHON, run, publisher_hashes

ROOT = Path(__file__).absolute().parent


def main():
    lane = Path(os.environ.get("PCL_VALIDATION_ROOT", ROOT / "validation")) / ("native-" + str(time.time_ns()))
    lane.mkdir(parents=True)
    state, data = lane / "neutral", lane / "archivebox"
    start_hashes = publisher_hashes()
    result = {"status": "running", "lane": str(lane), "steps": [], "synthetic_fixture": True, "source_network_requests": 0, "code_hashes": start_hashes}
    def command(name, arguments, expected_returncode=0):
        completed = subprocess.run([sys.executable, "-m", "bridge.archivebox", *arguments], env={**os.environ, "PCL_BRIDGE_VALIDATION": "1"}, capture_output=True, text=True, timeout=240)
        (lane / (name + ".stdout")).write_text(completed.stdout)
        (lane / (name + ".stderr")).write_text(completed.stderr)
        parsed = json.loads(completed.stdout.splitlines()[-1])
        result["steps"].append({"name": name, "returncode": completed.returncode, "result": parsed})
        atomic_json(lane / "results.json", result)
        print(json.dumps({"step": name, "returncode": completed.returncode, "status": parsed.get("status")}), flush=True)
        if completed.returncode != expected_returncode:
            raise RuntimeError(name + " failed: " + parsed.get("error", "see retained log"))
        return parsed
    source = {"id": "bridge-fixture", "adapter": "text", "url": "https://example.org/library-bridge-fixture", "review_status": "needs_review", "tags": ["fixture"], "person_ids": ["person:fixture"]}
    registry = {"schema_version": 1, "sources": [source]}
    def collect_version(text, status="available"):
        context = {"schema_version": 1, "thread_complete": False, "entries": [{"relation": "quoted_post", "post_id": "123", "url": "https://x.com/other_author/status/123", "author_handle": "other_author", "author_name": "Context Author", "text": "RLAIF is a context-only fixture term <script>inert</script>", "status": "available", "person_ids": []}]}
        record = Record(source["url"], title="Bridge fixture", text=text, status=status, review_status=source["review_status"], metadata={"x_context": context}, provenance={"synthetic_fixture": True}, artifacts=[Artifact(data=("Synthetic original: " + text).encode(), media_type="text/plain", role="fixture_original", source_url=source["url"])])
        collect(registry, state, adapter=lambda s, c: Batch([record], status="ok" if status == "available" else "partial", coverage={"window_complete": status == "available"}))
        store = Store(state)
        try:
            bundle = store.export()
        finally:
            store.db.close()
        atomic_json(lane / "bundle.json", bundle)
        return bundle
    def export():
        store = Store(state)
        try:
            bundle = store.export()
        finally:
            store.db.close()
        atomic_json(lane / "bundle.json", bundle)
        return bundle
    publish = ["publish", "--data", str(data), "--bundle", str(lane / "bundle.json")]
    held = lane / "original-held"
    try:
        first = collect_version("The body-only orchestrator term proves fulltext capture. Amber version one.")
        item_id = first["items"][0]["id"]
        store = Store(state)
        store.annotate(item_id, notes="Initial neutral note", tags=["manual-before"])
        store.db.close()
        export()
        a = command("publish-v1", publish)
        b = command("publish-duplicate", publish)
        assert a["new_snapshots"] == 1 and b["new_snapshots"] == 0
        initial = command("inspect-v1", ["inspect", "--data", str(data)])
        assert len(initial["snapshots"]) == 1 and len(initial["queries"]["orchestrator"]) == 1
        assert len(initial["queries"]["RLAIF"]) == 1
        initial_output = Path(initial["snapshots"][0]["output_dir"]) / "collector"
        context_html = (initial_output / "content.html").read_text()
        assert "Conversation context" in context_html and "@other_author" in context_html
        assert "&lt;script&gt;inert&lt;/script&gt;" in context_html and "<script>inert</script>" not in context_html
        assert "RLAIF" in (initial_output / "content.txt").read_text()
        assert "RLAIF" not in json.loads((initial_output / "content.json").read_text())["text"]
        result["context_checks"] = {"separate_author_rendered": True, "context_fulltext_search": True, "primary_text_unchanged": True, "html_escaped": True}
        before = {f["path"]: f["sha256"] for f in initial["snapshots"][0]["files"]}
        # Simulate a crash after native output/intent exists but before bookkeeping.
        ledger_path = data / "collector-bridge.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["items"][item_id]["versions"] = {}
        atomic_json(ledger_path, ledger)
        marker = Path(initial["snapshots"][0]["output_dir"]) / "collector/publication.json"
        marker.unlink()
        recovery_edit = '''from archivebox.config.django import setup_django
setup_django()
from archivebox.core.models import Snapshot
s=Snapshot.objects.first()
s.notes="UI note after interrupted publication"
s.save(update_fields=["notes"])
s.save_tags(list(s.tags.values_list("name",flat=True)) + ["ui-before-ledger"])
print('{"status":"ok"}')
'''
        run([str(PYTHON), "-c", recovery_edit], data, "fixture-untracked-ui-edit")
        recovered = command("recover-untracked-publication", publish)
        assert recovered["new_snapshots"] == 0
        command("pull-recovered-annotations", ["pull", "--data", str(data), "--state", str(state)])
        recovered_manual = export()["items"][0]["manual"]
        assert "UI note after interrupted publication" in recovered_manual["notes"] and "ui-before-ledger" in recovered_manual["tags"]
        edit = '''from archivebox.config.django import setup_django
setup_django()
from archivebox.core.models import Snapshot
s=Snapshot.objects.first()
s.notes="ArchiveBox user note"
s.save(update_fields=["notes"])
s.save_tags([t for t in s.tags.values_list("name",flat=True) if t != "manual-before"] + ["ui-reading"])
print('{"status":"ok"}')
'''
        run([str(PYTHON), "-c", edit], data, "fixture-ui-edit")
        pulled = command("pull-ui-annotations", ["pull", "--data", str(data), "--state", str(state)])
        assert pulled["annotations_pulled"] == 1
        manual = export()["items"][0]["manual"]
        assert manual["notes"] == "ArchiveBox user note" and "ui-reading" in manual["tags"] and "manual-before" not in manual["tags"]
        store = Store(state)
        store.annotate(item_id, notes="Independent neutral edit")
        store.db.close()
        run([str(PYTHON), "-c", edit.replace("ArchiveBox user note", "Independent UI edit")], data, "fixture-ui-conflict")
        export()
        conflict_publish = command("publish-conflicting-notes", publish)
        assert conflict_publish["annotation_conflicts_preserved"] == 1
        command("publish-conflicting-notes-again", publish)
        conflict = command("pull-conflict", ["pull", "--data", str(data), "--state", str(state)])
        assert conflict["annotations_pulled"] == 1
        manual = export()["items"][0]["manual"]
        assert "Independent neutral edit" in manual["notes"] and "Independent UI edit" in manual["notes"]
        collect_version("The body-only orchestrator term remains. Blue corrected version two.")
        second = command("publish-v2", publish)
        assert second["new_snapshots"] == 1
        collect_version("The body-only", "truncated")
        third = command("publish-partial", publish)
        assert third["new_snapshots"] == 1
        latest = command("inspect-all", ["inspect", "--data", str(data)])
        assert len(latest["snapshots"]) == 3
        old = next(s for s in latest["snapshots"] if s["id"] == initial["snapshots"][0]["id"])
        assert before == {f["path"]: f["sha256"] for f in old["files"]}
        assert all("Independent neutral edit" in s["notes"] and "Independent UI edit" in s["notes"] and "ui-reading" in s["tags"] for s in latest["snapshots"])
        assert any("library:quality:truncated" in s["tags"] for s in latest["snapshots"])
        # Review decisions change current native metadata, never immutable captures.
        capture_hashes = {s["id"]: s["files"] for s in latest["snapshots"]}
        notes_before_review = {s["id"]: s["notes"] for s in latest["snapshots"]}
        user_tags_before_review = {s["id"]: [t for t in s["tags"] if not t.startswith("library:")] for s in latest["snapshots"]}
        assert all(s["title"].startswith("[Review needed] ") and "library:review:needs_review" in s["tags"] for s in latest["snapshots"])
        assert all("library:public-professional" not in s["tags"] for s in latest["snapshots"])
        for snapshot in latest["snapshots"]:
            html = (Path(snapshot["output_dir"]) / "collector/content.html").read_text()
            assert "Review at publication: needs_review" in html
        # Exercise legacy ledgers, an edited legacy title, and an edited tracked title.
        first_version = latest["snapshots"][0]["publication"]["version_id"]
        third_version = latest["snapshots"][2]["publication"]["version_id"]
        ledger = json.loads(ledger_path.read_text())
        for vid in [first_version, third_version]:
            ledger["items"][item_id]["versions"][vid].pop("last_written_title", None)
        atomic_json(ledger_path, ledger)
        edit_titles = '''from archivebox.config.django import setup_django
setup_django()
from archivebox.core.models import Snapshot
rows=list(Snapshot.objects.order_by("created_at"))
rows[0].title="[Review needed] Legacy custom title"
rows[0].save(update_fields=["title"])
rows[1].title="My chosen title"
rows[1].save(update_fields=["title"])
print('{"status":"ok"}')
'''
        run([str(PYTHON), "-c", edit_titles], data, "fixture-custom-titles")
        expected_bodies = {latest["snapshots"][0]["id"]: "Legacy custom title", latest["snapshots"][1]["id"]: "My chosen title", latest["snapshots"][2]["id"]: "[truncated] Bridge fixture"}
        review_checks = []
        for review_status, prefix in [("included", ""), ("needs_review", "[Review needed] "), ("excluded", "[Excluded] ")]:
            store = Store(state)
            store.annotate(item_id, review_status=review_status)
            store.db.close()
            export()
            for repeat in [1, 2]:
                published = command(f"publish-review-{review_status}-{repeat}", publish)
                assert published["new_snapshots"] == 0 and published["published_versions"] == 3
            current = command("inspect-review-" + review_status, ["inspect", "--data", str(data)])
            assert len(current["snapshots"]) == 3
            for snapshot in current["snapshots"]:
                assert snapshot["title"] == prefix + expected_bodies[snapshot["id"]]
                assert [t for t in snapshot["tags"] if t.startswith("library:review:")] == ["library:review:" + review_status]
                assert ("library:public-professional" in snapshot["tags"]) == (review_status == "included")
                assert snapshot["notes"] == notes_before_review[snapshot["id"]]
                assert [t for t in snapshot["tags"] if not t.startswith("library:")] == user_tags_before_review[snapshot["id"]]
                assert snapshot["files"] == capture_hashes[snapshot["id"]]
            review_checks.append({"status": review_status, "snapshot_count": 3, "repeat_publish_new_snapshots": 0, "titles": [s["title"] for s in current["snapshots"]], "immutable_history_unchanged": True, "notes_tags_preserved": True, "professional_tag_matches_inclusion": True})
            latest = current
        result["review_transitions"] = {"initial": "needs_review", "transitions": review_checks, "legacy_and_tracked_custom_titles_preserved": True, "new_renditions_label_review_as_historical": True}
        login_file = lane / "private-login.txt"
        login = command("credentials", ["credentials", "--data", str(data), "--login-file", str(login_file)])
        assert login["created"] and login_file.stat().st_mode & 0o777 == 0o600
        login_again = command("credentials-idempotent", ["credentials", "--data", str(data), "--login-file", str(login_file)])
        assert not login_again["created"]
        backup_path = lane / "backup"
        user_file = data / "user-document-preservation.txt"
        user_file.write_text("User document preserved across backup generations.\n")
        user_file_bytes = user_file.read_bytes()
        command("backup", ["backup", "--data", str(data), "--output", str(backup_path)])
        data.rename(held)
        try:
            restored = command("restore-inspect-original-unavailable", ["inspect", "--data", str(backup_path)])
            assert len(restored["snapshots"]) == 3
            assert restored["queries"] == latest["queries"]
            for snapshot in restored["snapshots"]:
                assert Path(snapshot["output_dir"]).resolve().is_relative_to(backup_path.resolve())
                original = next(s for s in latest["snapshots"] if s["id"] == snapshot["id"])
                assert snapshot["files"] == original["files"]
                assert snapshot["notes"] == original["notes"] and snapshot["tags"] == original["tags"]
            previous_receipt_bytes = (backup_path / "bridge-backup-manifest.json").read_bytes()
            next_backup = lane / "backup-from-restored"
            repeated_backup = command("backup-restored-collection", ["backup", "--data", str(backup_path), "--output", str(next_backup)])
            receipt = json.loads((next_backup / "bridge-backup-manifest.json").read_text())
            retained = receipt["previous_receipt"]
            assert retained["sha256"] == hashlib.sha256(previous_receipt_bytes).hexdigest()
            assert (next_backup / retained["preserved_path"]).read_bytes() == previous_receipt_bytes
            assert any(f["path"] == retained["preserved_path"] and f["sha256"] == retained["sha256"] for f in receipt["files"])
            assert all(f["path"] != "bridge-backup-manifest.json" for f in receipt["files"])
            assert (next_backup / user_file.name).read_bytes() == user_file_bytes
            repeated_restore = command("inspect-repeated-backup", ["inspect", "--data", str(next_backup)])
            for snapshot in repeated_restore["snapshots"]:
                original = next(s for s in restored["snapshots"] if s["id"] == snapshot["id"])
                assert snapshot["files"] == original["files"]
                assert snapshot["notes"] == original["notes"] and snapshot["tags"] == original["tags"]
            assert repeated_restore["queries"] == restored["queries"]
            result["backup_after_restore"] = {"status": repeated_backup["status"], "previous_receipt_preserved": True,
                                             "fresh_receipt_verified": True, "user_file_preserved": True,
                                             "captured_files_notes_tags_search_preserved": True, "original_collection_unavailable": True}
        finally:
            held.rename(data)
        damaged = Path(initial["snapshots"][0]["output_dir"]) / "collector/content.html"
        original_bytes = damaged.read_bytes()
        damaged.chmod(0o600)
        try:
            damaged.write_bytes(original_bytes + b"damaged fixture")
            command("detect-damaged-rendition", publish, expected_returncode=3)
        finally:
            damaged.write_bytes(original_bytes)
            damaged.chmod(0o444)
        marker_bytes = marker.read_bytes()
        marker.unlink()
        try:
            command("detect-missing-marker-backup", ["backup", "--data", str(data), "--output", str(lane / "invalid-missing-marker-backup")], expected_returncode=3)
        finally:
            marker.write_bytes(marker_bytes)
            marker.chmod(0o444)
        assert start_hashes == {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in start_hashes}, "Bridge code changed during validation; rerun required"
        result.update(status="ok", checks={"duplicate_idempotence": True, "body_fulltext": True, "ui_annotations_pulled_to_neutral": True, "conflicting_notes_both_preserved": True, "three_versions_retained": True, "old_rendition_hashes_unchanged": True, "partial_version_marked": True, "backup_integrity": True, "restore_original_unavailable": True, "restored_files_queries_notes_tags": True, "changed_rendition_fails_visibly": True, "missing_marker_backup_fails_visibly": True, "untracked_interrupted_publication_preserves_ui_edits": True, "review_labels_current_across_versions": True, "classification_does_not_rewrite_history": True, "user_edited_title_bodies_preserved": True})
        atomic_json(Path(os.environ.get("PCL_BRIDGE_READY", ROOT / "validation/ready.json")), {"status": "ok", "results": str(lane / "results.json"), "code_hashes": start_hashes})
    except BaseException as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        raise
    finally:
        if held.exists() and not data.exists():
            held.rename(data)
        atomic_json(lane / "results.json", result)
        atomic_json(lane.parent / "latest.json", result)
        print(json.dumps({"status": result["status"], "results": str(lane / "results.json")}), flush=True)


if __name__ == "__main__":
    main()
