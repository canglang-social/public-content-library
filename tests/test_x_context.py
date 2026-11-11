"""Offline linked-context checks using synthetic API response shapes and bounded fixtures."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from collector.adapters import captured_x_posts, enrich_x_context, post_record, x_post, x_timeline
from collector.http import FetchError, LimitReached
from collector.models import Batch, CollectionContext, Response
from collector.runner import collect
from collector.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures/x-context.json"
NOW = '2026-11-03T00:00:00+00:00'


class Client:
    def __init__(self, route):
        self.route, self.calls, self.history, self.errors = route, [], [], []
        self.bytes_received = self.request_count = 0

    def get(self, url, **kwargs):
        self.calls.append(url)
        self.request_count += 1
        payload = self.route(url)
        if isinstance(payload, Exception):
            raise payload
        body = json.dumps(payload).encode()
        response = Response(body, url, 200, {'content-type': 'application/json'}, NOW)
        self.history.append(response)
        self.bytes_received += len(body)
        return response


def source(handle='example_member', **options):
    return {'id': 'fixture-x', 'adapter': 'x_timeline', 'url': 'https://x.com/' + handle,
            'person_ids': ['person:member'], 'review_status': 'included', 'options': options}


class XContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(FIXTURES.read_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def context(self, client, items=3):
        return CollectionContext('2026-10-20T00:00:00+00:00', NOW, {}, items, 1, client, self.root)

    def test_synthetic_quote_keeps_primary_and_outsider_authorship_separate(self):
        tweet = deepcopy(self.raw['org_quote'])
        before = deepcopy(tweet)
        record = post_record(tweet, source('ExampleLab'))
        quoted = record.metadata['x_context']['entries'][0]
        self.assertEqual((record.identity_key, record.text, record.author), ('x:' + tweet['id'], tweet['text'], 'ExampleLab'))
        self.assertEqual(record.person_ids, ['person:member'])
        self.assertEqual(quoted['author_handle'], 'ExampleFoundation')
        self.assertEqual(quoted['text'], tweet['quote']['text'])
        self.assertEqual(quoted['person_ids'], [])
        self.assertEqual(quoted['url'], 'https://x.com/ExampleFoundation/status/' + tweet['quote']['id'])
        self.assertEqual(quoted['relation'], 'quoted_post')
        self.assertFalse(record.metadata['x_context']['thread_complete'])
        self.assertEqual(tweet, before)

    def test_synthetic_v1_reply_resolves_captured_parent_without_changing_primary(self):
        tweet, parent = self.raw['member_reply_v1'], self.raw['member_root']
        record = post_record(tweet, source(), context_posts=[parent])
        entry = record.metadata['x_context']['entries'][0]
        self.assertEqual((entry['post_id'], entry['text'], entry['author_handle']), (parent['id'], parent['text'], 'example_member'))
        self.assertEqual(entry['status'], 'available')
        self.assertEqual(record.text, tweet['text'])

    def test_synthetic_v2_reply_does_not_assign_parent_to_member(self):
        tweet, parent = self.raw['reply_v2'], self.raw['reply_parent_v2']
        record = post_record(tweet, source('example_researcher'), context_posts=[parent])
        entry = record.metadata['x_context']['entries'][0]
        self.assertEqual(entry['post_id'], tweet['replying_to']['status'])
        self.assertEqual(entry['author_handle'], 'example_outsider')
        self.assertEqual(entry['text'], parent['text'])
        self.assertEqual(entry['person_ids'], [])
        self.assertEqual(record.person_ids, ['person:member'])

    def test_unresolved_parent_does_not_synthesize_author_from_reply_hint(self):
        record = post_record(self.raw['reply_v2'], source('example_researcher'))
        entry = record.metadata['x_context']['entries'][0]
        self.assertEqual(entry['status'], 'unavailable')
        self.assertEqual(entry['text'], '')
        self.assertIsNone(entry['author_handle'])
        self.assertEqual(entry['url'], 'https://x.com/i/status/' + entry['post_id'])

    def test_parent_already_embedded_in_another_captured_quote_is_reused(self):
        cited = self.raw['org_quote']['quote']
        tweet = {'id': '123', 'text': 'A response', 'author': {'screen_name': 'example_member'}, 'replying_to_status': cited['id']}
        entry = post_record(tweet, source(), context_posts=[self.raw['org_quote']]).metadata['x_context']['entries'][0]
        self.assertEqual((entry['author_handle'], entry['text']), ('ExampleFoundation', cited['text']))

    def test_empty_quote_object_is_visible_as_unavailable_context(self):
        record = post_record({'id': '123', 'text': 'A post', 'quote': {}}, source())
        entry = record.metadata['x_context']['entries'][0]
        self.assertEqual((entry['relation'], entry['status'], entry['text']), ('quoted_post', 'unavailable', ''))

    def test_missing_quote_author_stays_unknown(self):
        tweet = deepcopy(self.raw['org_quote'])
        tweet['quote']['author'] = None
        entry = post_record(tweet, source('ExampleLab')).metadata['x_context']['entries'][0]
        self.assertIsNone(entry['author_handle'])
        self.assertIsNone(entry['author_name'])
        self.assertEqual(entry['person_ids'], [])
        self.assertTrue(entry['url'].startswith('https://x.com/i/status/'))

    def test_malformed_fields_are_not_invented_as_post_text_or_ids(self):
        tweet = {'id': '123', 'text': ['not', 'text'], 'author': [], 'media': ['invalid'],
                 'quote': {'id': ['456'], 'text': {'not': 'text'}}, 'replying_to': {'screen_name': 'member'},
                 'created_at': {'invalid': 'date'}}
        record = post_record(tweet, source())
        self.assertEqual(record.text, '')
        self.assertEqual(record.status, 'metadata_only')
        self.assertEqual(record.person_ids, [])
        self.assertEqual(len(record.metadata['x_context']['entries']), 2)
        self.assertTrue(all(e['post_id'] is None and e['text'] == '' for e in record.metadata['x_context']['entries']))
        with self.assertRaises(FetchError):
            post_record({'id': True}, source())

    def test_cached_wrapper_shapes_and_duplicates_are_bounded(self):
        root, reply = self.raw['member_root'], self.raw['member_reply_v1']
        for wrapper in [{'status': root}, {'tweet': root}, root]:
            self.assertEqual(captured_x_posts(wrapper), [root])
        wrapped = {'code': 200, 'results': [None, {'type': 'thread', 'statuses': [reply, root, reply]}, {'id': 'bad'}, 7]}
        self.assertEqual(captured_x_posts(wrapped), [reply, root])
        self.assertEqual(captured_x_posts(wrapped, max_posts=1), [reply])
        self.assertEqual(captured_x_posts({'code': 500, 'status': root}), [])

    def test_long_ancestry_cycles_and_text_caps_are_explicit(self):
        tweets = {str(i): {'id': str(i), 'text': 'Context text ' + str(i), 'author': {'screen_name': 'author_' + str(i)},
                           'replying_to_status': str(i + 1)} for i in range(1, 21)}
        tweets['20']['replying_to_status'] = '1'
        base = post_record(tweets['1'], source('author_1'))
        bounded = enrich_x_context(base, tweets['1'], tweets, max_depth=3)
        self.assertEqual(len(bounded.metadata['x_context']['entries']), 3)
        self.assertTrue(bounded.metadata['x_context']['limited'])
        self.assertEqual(bounded.metadata['x_context']['omitted_links'], 1)
        short = enrich_x_context(base, tweets['1'], tweets, max_entries=2, max_text_chars=7)
        self.assertEqual(sum(len(e['text']) for e in short.metadata['x_context']['entries']), 7)
        self.assertEqual(short.metadata['x_context']['entries'][0]['status'], 'truncated')
        self.assertEqual(short.text, base.text)
        tweets['2']['replying_to_status'] = '1'
        cyclic = enrich_x_context(base, tweets['1'], tweets)
        self.assertEqual(len(cyclic.metadata['x_context']['entries']), 1)
        self.assertEqual(cyclic.metadata['x_context']['omitted_links'], 1)

    def test_timeline_resolves_parent_outside_selected_item_cap_without_fetch(self):
        child, parent = self.raw['member_reply_v1'], self.raw['member_root']
        payload = {'results': [{'type': 'thread', 'statuses': [child, parent, child]}], 'cursor': {}}
        client = Client(lambda url: payload)
        batch = x_timeline(source(context_max_fetches=0), self.context(client, items=1))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.records[0].source_item_id, child['id'])
        self.assertEqual(batch.records[0].metadata['x_context']['entries'][0]['text'], parent['text'])
        self.assertEqual(batch.coverage['context_lookup_requests'], 0)
        self.assertEqual(len(batch.cursor['pending_posts']), 1)

    def test_parent_lookup_obeys_shared_limit_and_rejects_wrong_identity(self):
        tweets = [{'id': str(i), 'text': 'Reply ' + str(i), 'author': {'screen_name': 'example_member'}, 'replying_to_status': str(i + 100)} for i in [1, 2, 3]]
        client = Client(lambda url: {'results': tweets, 'cursor': {}} if '/profile/' in url else {'status': {'id': '999', 'text': 'wrong parent'}})
        batch = x_timeline(source(context_max_fetches=1), self.context(client))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(batch.records), 3)
        self.assertEqual(batch.coverage['context_lookup_requests'], 1)
        self.assertEqual(batch.coverage['context_unavailable'], 3)
        self.assertEqual(len(batch.coverage['context_lookup_errors']), 1)
        self.assertTrue(all(r.status == 'available' for r in batch.records))

    def test_direct_reply_parent_lookup_retains_both_raw_responses(self):
        child, parent = self.raw['member_reply_v1'], self.raw['member_root']
        registered = {**source(), 'adapter': 'x_post', 'url': child['url']}
        client = Client(lambda url: {'status': child if url.endswith(child['id']) else parent})
        batch = x_post(registered, self.context(client))
        self.assertEqual(batch.status, 'ok')
        self.assertEqual(len(client.history), 2)
        self.assertEqual(batch.records[0].metadata['x_context']['entries'][0]['text'], parent['text'])

    def test_context_budget_failure_keeps_successful_primary_capture(self):
        child = self.raw['member_reply_v1']
        registered = {**source(), 'adapter': 'x_post', 'url': child['url']}
        client = Client(lambda url: {'status': child} if url.endswith(child['id']) else LimitReached('Fixture byte cap'))
        batch = x_post(registered, self.context(client))
        self.assertEqual((batch.status, len(batch.records), batch.records[0].status), ('partial', 1, 'available'))
        self.assertEqual(batch.records[0].text, child['text'])
        self.assertEqual(batch.coverage['context_lookup_requests'], 1)
        self.assertEqual(batch.coverage['context_unavailable'], 1)

    def test_replay_is_idempotent_and_changed_context_preserves_annotations(self):
        tweet = deepcopy(self.raw['org_quote'])
        registered = source('ExampleLab', context_max_fetches=0)
        registry = {'schema_version': 1, 'sources': [registered]}
        def run():
            record = post_record(tweet, registered)
            return collect(registry, self.root / 'state', adapter=lambda *_: Batch([record], coverage={'window_complete': True}))
        first, repeat = run(), run()
        self.assertEqual((first['new_items'], repeat['new_versions']), (1, 0))
        store = Store(self.root / 'state')
        item = store.export()['items'][0]
        store.annotate(item['id'], notes='Keep this note', tags=['context-check'])
        primary_hash = item['versions'][0]['text_hash']
        store.db.close()
        tweet['quote']['text'] += '\nA captured correction to the quoted post.'
        self.assertEqual(run()['new_versions'], 1)
        store = Store(self.root / 'state')
        updated = store.export()['items'][0]
        store.db.close()
        self.assertEqual(len(updated['versions']), 2)
        self.assertTrue(all(v['text_hash'] == primary_hash for v in updated['versions']))
        self.assertEqual(updated['manual']['notes'], 'Keep this note')
        self.assertEqual(updated['manual']['tags'], ['context-check'])
        self.assertEqual(asdict(post_record(tweet, registered)), asdict(post_record(deepcopy(tweet), registered)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
