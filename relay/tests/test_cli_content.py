"""Match the CLI's JSON-value contract; @file is rejected for --content."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import progress


class ContentContractTests(unittest.TestCase):
    def test_reply_and_send_pass_literal_unicode_json(self):
        card = progress.build_summary_card(task_id='contract', elapsed_sec=0,
                                           body_md='中文 "quote" $() `text`')
        calls = []

        def cli(argv, stdin_text):
            calls.append(argv)
            try:
                parsed = json.loads(argv[argv.index('--content') + 1])
            except (ValueError, IndexError):
                return 2, json.dumps({'ok': False, 'error': {'param': '--content'}}), ''
            self.assertEqual(parsed, card)
            return 0, json.dumps({'ok': True, 'data': {'message_id': 'om_result'}}), ''

        progress.set_runner(cli)
        self.addCleanup(progress.set_runner, None)
        self.assertTrue(progress.reply_interactive('om_original', card, reply_in_thread=True)[0])
        self.assertTrue(progress.send_interactive('oc_test', card)[0])
        self.assertIn('--reply-in-thread', calls[0])
        self.assertEqual(calls[0][calls[0].index('--message-id') + 1], 'om_original')


if __name__ == '__main__':
    unittest.main()
