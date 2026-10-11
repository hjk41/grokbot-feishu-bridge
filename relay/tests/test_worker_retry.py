"""Retry transient transport failures without changing a worker claim."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker_progress as worker


class WorkerRetryTests(unittest.TestCase):
    def setUp(self):
        self.payload = {'task_id': 'test_retry', 'worker_id': 'a' * 32,
                        'callback': {'url': 'https://bridge.example/v1/tasks/test_retry',
                                     'token': 'example-capability'}}

    def response(self):
        return io.BytesIO(json.dumps({'ok': True, 'task': {'status': 'done'}}).encode())

    def test_lost_terminal_reply_reuses_identical_request(self):
        opener = mock.Mock()
        opener.open.side_effect = [HTTPError('https://bridge.example', 502, 'temporary', {}, None), self.response()]
        with mock.patch.object(worker.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(worker.time, 'sleep') as sleep:
            self.assertEqual(worker.call(self.payload, 'done', {'summary': 'Completed'})['status'], 'done')
        self.assertEqual(opener.open.call_count, 2)
        first = opener.open.call_args_list[0].args[0]
        second = opener.open.call_args_list[1].args[0]
        self.assertIs(first, second)
        self.assertEqual(json.loads(first.data)['worker_id'], self.payload['worker_id'])
        sleep.assert_called_once_with(1)

    def test_network_failure_recovers(self):
        opener = mock.Mock()
        opener.open.side_effect = [URLError('temporary'), self.response()]
        with mock.patch.object(worker.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(worker.time, 'sleep'):
            worker.call(self.payload, 'show')
        self.assertEqual(opener.open.call_count, 2)

    def test_auth_and_claim_conflicts_are_not_retried(self):
        for status in (401, 409):
            with self.subTest(status=status):
                opener = mock.Mock()
                opener.open.side_effect = HTTPError('https://bridge.example', status, 'rejected', {}, None)
                with mock.patch.object(worker.urllib.request, 'build_opener', return_value=opener), \
                        mock.patch.object(worker.time, 'sleep') as sleep:
                    with self.assertRaises(HTTPError):
                        worker.call(self.payload, 'start')
                self.assertEqual(opener.open.call_count, 1)
                sleep.assert_not_called()

    def test_retry_budget_is_bounded(self):
        opener = mock.Mock()
        opener.open.side_effect = URLError('temporary')
        with mock.patch.object(worker.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(worker.time, 'sleep') as sleep:
            with self.assertRaises(URLError):
                worker.call(self.payload, 'show')
        self.assertEqual(opener.open.call_count, 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4])
