"""Context remains searchable without changing the primary author or losing history."""
import copy
from pathlib import Path
import tempfile
import unittest

from collector.models import Batch, Record
from collector.runner import collect
from collector.store import Store
from collector.presentation import context_text


class ContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = {"id": "member", "adapter": "text", "url": "https://x.com/member/status/1", "person_ids": ["person:member"]}
        self.record = Record(canonical_url=self.source["url"], text="My own words.", author="member", review_status="included",
                             metadata={"x_context": {"schema_version": 1, "thread_complete": False, "entries": [
                                 {"relation": "reply_parent", "via_post_id": "1", "post_id": "2", "author_handle": "outsider",
                                  "url": "https://x.com/outsider/status/2", "text": "Uniquequartz context words", "status": "available", "person_ids": []}]}})

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, record):
        return collect({"schema_version": 1, "sources": [self.source]}, self.root,
                       adapter=lambda *_: Batch([record], coverage={"window_complete": True}))

    def test_context_search_keeps_primary_text_and_member_attribution(self):
        self.capture(self.record)
        store = Store(self.root)
        try:
            found = store.search("Uniquequartz")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["person_ids"], ["person:member"])
            self.assertEqual(found[0]["versions"][0]["content"]["text"], "My own words.")
            store.fts = False
            self.assertEqual(len(store.search("Uniquequartz")), 1)
        finally:
            store.db.close()

    def test_failed_same_parent_refresh_preserves_last_complete_context(self):
        self.capture(self.record)
        missing = copy.deepcopy(self.record)
        entry = missing.metadata["x_context"]["entries"][0]
        entry.update(text="", author_handle=None, status="unavailable", reason="Fetch failed")
        result = self.capture(missing)
        self.assertEqual(result["new_versions"], 0)
        store = Store(self.root)
        try:
            self.assertEqual(len(store.search("Uniquequartz")), 1)
        finally:
            store.db.close()

    def test_changed_reference_does_not_revive_old_parent(self):
        self.capture(self.record)
        changed = copy.deepcopy(self.record)
        changed.metadata["x_context"]["entries"][0].update(post_id="3", url="https://x.com/another/status/3", text="", status="unavailable")
        self.capture(changed)
        store = Store(self.root)
        try:
            self.assertEqual(store.search("Uniquequartz"), [])
            self.assertEqual(len(store.export()["items"][0]["versions"]), 2)
        finally:
            store.db.close()

    def test_failed_ancestor_keeps_known_descendant_but_fresh_changed_edge_does_not(self):
        child = {"relation": "reply_parent", "via_post_id": "2", "post_id": "3", "author_handle": "another",
                 "text": "Uniquetopaz descendant words", "status": "available", "person_ids": []}
        self.record.metadata["x_context"]["entries"].append(child)
        self.capture(self.record)
        failure = copy.deepcopy(self.record)
        failure.metadata["x_context"]["entries"] = [{**failure.metadata["x_context"]["entries"][0], "text": "", "status": "unavailable"}]
        self.assertEqual(self.capture(failure)["new_versions"], 0)
        store = Store(self.root)
        try:
            self.assertEqual(len(store.search("Uniquetopaz")), 1)
        finally:
            store.db.close()
        changed = copy.deepcopy(self.record)
        changed.metadata["x_context"]["entries"] = changed.metadata["x_context"]["entries"][:1]
        self.capture(changed)
        store = Store(self.root)
        try:
            self.assertEqual(store.search("Uniquetopaz"), [])
        finally:
            store.db.close()

    def test_no_context_has_no_added_rendition_text(self):
        self.assertEqual(context_text({"text": "Body"}), "")

    def test_failed_lookup_preserves_known_context_omission(self):
        self.record.metadata['x_context'].update(limited=True, omitted_links=1)
        self.capture(self.record)
        failure = copy.deepcopy(self.record)
        failure.metadata['x_context'].update(limited=False, omitted_links=0)
        failure.metadata['x_context']['entries'][0].update(text='', status='unavailable')
        self.assertEqual(self.capture(failure)['new_versions'], 0)

    def test_fresh_shared_node_overrides_old_descendant_body_and_edges(self):
        self.record.metadata['x_context']['entries'] += [
            {'relation': 'reply_parent', 'via_post_id': '2', 'post_id': '3', 'text': 'Oldopalbody', 'status': 'available'},
            {'relation': 'reply_parent', 'via_post_id': '3', 'post_id': '4', 'text': 'Stalediamondedge', 'status': 'available'}]
        self.capture(self.record)
        fresh = copy.deepcopy(self.record)
        fresh.metadata['x_context']['entries'] = [
            {**fresh.metadata['x_context']['entries'][0], 'text': '', 'status': 'unavailable'},
            {'relation': 'quoted_post', 'via_post_id': '1', 'post_id': '3', 'text': 'Freshjadebody', 'status': 'available'}]
        self.capture(fresh)
        store = Store(self.root)
        try:
            self.assertEqual(len(store.search('Freshjadebody')), 1)
            self.assertEqual(store.search('Oldopalbody'), [])
            self.assertEqual(store.search('Stalediamondedge'), [])
        finally:
            store.db.close()


if __name__ == "__main__":
    unittest.main()
