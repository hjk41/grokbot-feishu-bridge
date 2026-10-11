"""Real HTTP transport with fake Feishu; tests cross-machine task ownership."""
import copy
import json
import os
import sys
from pathlib import Path
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fifo_queue import FifoBase
import callback_server as callbacks
import worker_progress as worker
import chat_queue
import progress


class CallbackTests(FifoBase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {'RELAY_ROLE': 'inbound', 'BRIDGE_PUBLIC_URL': 'https://bridge.example'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = callbacks.start_server(port=0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self._seed('task_a', 'oc_fifo_1', 'A')
        self._seed('task_b', 'oc_fifo_1', 'B')
        for tid in ('task_a', 'task_b'):
            chat_queue.enqueue('oc_fifo_1', tid)
        chat_queue.mark_waiting('task_b', 1)
        self.assertEqual(chat_queue.dispatch_task('task_a', chat_id='oc_fifo_1'), 'posted')
        self.payload = copy.deepcopy(self.posts[0])
        self.payload['callback']['url'] = 'http://127.0.0.1:%s/v1/tasks/task_a' % self.server.server_port
        self.payload['worker_id'] = 'a' * 32

    def call(self, command, payload=None, **fields):
        return worker.call(payload or self.payload, command, fields, allow_http=True)

    def rejected(self, status, command, payload=None, **fields):
        with self.assertRaises(HTTPError) as caught:
            self.call(command, payload=payload, **fields)
        self.assertEqual(caught.exception.code, status)

    def test_stages_terminal_idempotency_and_fifo(self):
        self.assertEqual(self.call('start', action='Stage A')['status'], 'running')
        self.call('update', action='Stage B')
        finished = self.call('done', summary='Completed')
        self.assertEqual(finished['status'], 'done')
        count = len(self.fake.calls)
        self.call('done', summary='Duplicate')
        self.assertEqual(len(self.fake.calls), count)
        self.rejected(409, 'update', action='Late update')
        chat_queue.scan_all_queues()
        self.assertEqual([s['task_id'] for s in self.posts], ['task_a', 'task_b'])
        self.assertIn('callback', self.posts[1])

    def test_distinct_worker_lease(self):
        self.call('start')
        other = copy.deepcopy(self.payload)
        other['worker_id'] = 'b' * 32
        self.rejected(409, 'start', payload=other)
        self.rejected(409, 'done', payload=other)
        self.assertEqual(progress.load_task('task_a')['status'], 'running')

    def test_capability_expiry_and_cross_task(self):
        bad = copy.deepcopy(self.payload)
        bad['callback']['token'] = 'wrong'
        self.rejected(401, 'start', payload=bad)
        bad = copy.deepcopy(self.payload)
        bad['task_id'] = 'task_b'
        bad['callback']['url'] = bad['callback']['url'].replace('task_a', 'task_b')
        self.rejected(401, 'start', payload=bad)
        state = progress.load_task('task_a')
        state['callback_expires_at'] = time.time() - 1
        progress.save_task(state)
        self.rejected(401, 'start')

    def test_show_stop_without_secrets(self):
        self.call('start')
        progress.request_stop('task_a', operator_id='ou_owner')
        shown = self.call('show')
        self.assertTrue(shown['stop_requested'])
        for private in ('callback_token', 'worker_id', 'callback_expires_at'):
            self.assertNotIn(private, shown)
        self.assertNotIn(self.payload['callback']['token'], json.dumps(shown))

    def test_fail_and_invalid_field(self):
        self.rejected(400, 'start', action=['invalid'])
        self.assertNotIn('worker_id', progress.load_task('task_a'))
        self.rejected(409, 'update', action='No start')
        self.call('start')
        self.assertEqual(self.call('fail', error='Expected failure')['status'], 'failed')
        self.call('fail', error='Retry')

    def test_http_refused_by_production_client(self):
        with self.assertRaises(ValueError):
            worker.call(self.payload, 'show')

    def fetch_payload(self, token=None, task='task_a'):
        url = 'http://127.0.0.1:%s/v1/tasks/%s/payload' % (self.server.server_port, task)
        request = Request(url, headers={'Authorization': 'Bearer ' + (
            self.payload['callback']['token'] if token is None else token)})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_full_payload_fetch_is_exact_and_authenticated(self):
        full = self.fetch_payload()
        self.assertEqual(full['payload'], self.posts[0])
        self.assertEqual(full['sha256'], chat_queue.payload_digest(self.posts[0])[0])
        self.assertEqual(progress.load_task('task_a')['status'], 'queued')
        for token, task in (('wrong', 'task_a'), (None, 'task_b')):
            with self.assertRaises(HTTPError) as caught:
                self.fetch_payload(token, task)
            self.assertEqual(caught.exception.code, 401)
        state = progress.load_task('task_a')
        state['callback_expires_at'] = time.time() - 1
        progress.save_task(state)
        with self.assertRaises(HTTPError) as caught:
            self.fetch_payload()
        self.assertEqual(caught.exception.code, 401)

    def test_payload_fetch_rejects_missing_cross_task_and_external_files(self):
        state = progress.load_task('task_a')
        for path in ('/etc/hosts', progress.load_task('task_b').get('pending_path', ''),
                     os.path.join(chat_queue.PENDING_DIR, 'missing.json')):
            state['pending_path'] = path
            progress.save_task(state)
            with self.assertRaises(HTTPError) as caught:
                self.fetch_payload()
            self.assertEqual(caught.exception.code, 409)

    def test_compact_control_preserves_large_unicode_payload_before_post(self):
        full = copy.deepcopy(self.posts[0])
        full['text'] = '完整群聊上下文' * 10000
        full['conversation_context'] = {'complete': True, 'messages': [{'content': full['text']}]}
        path = chat_queue.write_executable_pending(full)
        self.assertEqual(progress.load_task('task_a')['pending_path'], path)
        wake = chat_queue.webhook_envelope(full)
        wire = json.dumps(wake, ensure_ascii=False, separators=(',', ':')).encode()
        self.assertLess(len(wire), 2048)
        self.assertLess(wire.index(b'"callback"'), 200)
        self.assertNotIn(full['text'], wire.decode())
        loaded = self.fetch_payload()
        self.assertEqual(loaded['payload'], full)
        self.assertEqual(loaded['sha256'], wake['payload']['sha256'])
        self.assertEqual(chat_queue.payload_digest(loaded['payload'])[1], wake['payload']['bytes'])
        with mock.patch.object(chat_queue, '_post_hook', None), \
             mock.patch.dict(os.environ, {'FEISHU_RELAY_WEBHOOK_URL': 'https://routine.example',
                                         'FEISHU_RELAY_WEBHOOK_KEY': 'dummy-webhook-key'}), \
             mock.patch.object(chat_queue.urllib.request, 'urlopen') as transport:
            transport.return_value.__enter__.return_value.status = 200
            self.assertEqual(chat_queue.post_webhook(full), (True, None))
            self.assertEqual(json.loads(transport.call_args.args[0].data), wake)

    def test_compact_input_is_inbound_only_and_not_a_new_lease(self):
        with mock.patch.dict(os.environ, {'RELAY_ROLE': 'full'}):
            self.assertEqual(chat_queue.webhook_envelope(self.posts[0]), self.posts[0])
        action = {'kind': 'card_action', 'task_id': 'task_a'}
        self.assertEqual(chat_queue.webhook_envelope(action), action)
        self.call('start')
        self.assertEqual(self.fetch_payload()['payload'], self.posts[0])
        other = copy.deepcopy(self.payload)
        other['worker_id'] = 'b' * 32
        self.rejected(409, 'start', payload=other)


if __name__ == '__main__':
    unittest.main()
