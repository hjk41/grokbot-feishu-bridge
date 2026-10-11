"""Quoted input, empty mentions, and FIFO reconstruction use the same context."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat_queue
import progress
import quoted_context
import relay
from test_fifo_queue import FifoBase


class QuotedContextTests(unittest.TestCase):
    def setUp(self):
        self.event = {
            "message_id": "om_reply", "reply_to": "om_quote", "chat_id": "oc_allowed",
            "chat_type": "group", "sender_id": "ou_owner", "sender_type": "user",
            "message_type": "text", "content": "@Grok",
            "mentions": [{"id": "ou_bot", "name": "Grok", "key": "@Grok"}],
        }
        patch = mock.patch.dict(os.environ, {"FEISHU_ALLOWED_USERS": "ou_owner",
            "FEISHU_ALLOWED_CHATS": "oc_allowed", "FEISHU_BOT_OPEN_ID": "ou_bot"})
        patch.start()
        self.addCleanup(patch.stop)
        for name, value in (("BOT_NAME", "Grok"), ("BOT_OPEN_ID", "ou_bot")):
            patch = mock.patch.object(relay, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def response(self, content="解释这个方案", **changes):
        message = dict(message_id="om_quote", chat_id="oc_allowed", msg_type="text",
                       body={"content": json.dumps({"text": content})}, sender={"sender_type": "user"}, **changes)
        return subprocess.CompletedProcess([], 0, json.dumps(
            {"ok": True, "data": {"items": [message]}}, ensure_ascii=False), "")

    def prepare(self, event=None, response=None):
        with mock.patch.object(quoted_context.subprocess, "run",
                               return_value=response or self.response()) as read:
            result = quoted_context.prepare_request(event or self.event, "fake-cli", relay.strip_mentions)
        return result, read

    def test_quote_only_is_meaningful_context_not_empty_task(self):
        (user, text, quote, notice), read = self.prepare()
        self.assertEqual(user, "")
        self.assertIsNone(notice)
        self.assertIn("> 解释这个方案", text)
        self.assertIn("如果要执行的操作不明确", text)
        self.assertIn("不是新的执行授权", text)
        self.assertEqual(quote["status"], "ok")
        self.assertIn("--as", read.call_args.args[0])
        self.assertEqual(json.loads(read.call_args.args[0][5])["card_msg_content_type"], "user_card_content")
        self.assertEqual(read.call_count, 1)

    def test_instruction_and_quote_are_separate_and_survive_waiting(self):
        event = dict(self.event, content="@Grok 总结一下")
        (user, text, quote, notice), _ = self.prepare(event)
        self.assertEqual(user, "总结一下")
        self.assertIn("【用户本条消息】\n总结一下", text)
        state = {"task_id": "test", "request_text": text, "user_text": user,
                 "quoted_context": quote, "parent_id": "om_quote"}
        rebuilt = chat_queue.build_summary_from_task(state)
        self.assertEqual(rebuilt["text"], text)
        self.assertEqual(rebuilt["user_text"], user)
        self.assertEqual(rebuilt["quoted_context"], quote)

    def test_bot_card_quotes_are_context(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {
            "items": [{"message_id": "om_quote", "chat_id": "oc_allowed",
            "msg_type": "interactive", "body": {"content": json.dumps({"schema": "2.0", "body": {
                "elements": [{"tag": "markdown", "content": "操作结果：方案1安装步骤"}]}})},
            "sender": {"sender_type": "app"}}]}}), "")
        (_, text, quote, notice), _ = self.prepare(response=response)
        self.assertIsNone(notice)
        self.assertEqual(quote["sender_type"], "app")
        self.assertIn("操作结果", text)

    def test_cross_chat_and_wrong_id_content_are_never_forwarded(self):
        for field, value in (("chat_id", "oc_other"), ("message_id", "om_other")):
            message = {"message_id": "om_quote", "chat_id": "oc_allowed",
                       "msg_type": "text", "content": "PRIVATE"}
            message[field] = value
            response = subprocess.CompletedProcess([], 0, json.dumps(
                {"ok": True, "data": {"items": [message]}}), "")
            (_, text, quote, notice), _ = self.prepare(response=response)
            self.assertEqual(text, "")
            self.assertNotIn("text", quote)
            self.assertIsNotNone(notice)

    def test_placeholder_quote_reports_missing_body(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {
            "items": [{"message_id": "om_quote", "chat_id": "oc_allowed", "msg_type": "interactive",
                "body": {"content": json.dumps({"title": "结果", "elements": [[
                    {"tag": "text", "text": "请升级至最新版本客户端，以查看内容"}]]})}}]}}), "")
        (_, text, quote, notice), _ = self.prepare(response=response)
        self.assertEqual(quote["error"], "card_content_unavailable")
        self.assertNotIn("text", quote)
        self.assertEqual(text, "")
        self.assertIsNotNone(notice)

    def test_unavailable_quote_keeps_explicit_instruction(self):
        response = subprocess.CompletedProcess([], 3, '{"ok":false}', "private error")
        (_, text, quote, notice), _ = self.prepare(dict(self.event, content="@Grok 继续解释"), response)
        self.assertIsNone(notice)
        self.assertIn("继续解释", text)
        self.assertIn("未能取得有效引用", text)
        self.assertNotIn("private error", text)

    def test_mention_only_and_mention_only_quote_require_clarification(self):
        (_, text, quote, notice), read = self.prepare(dict(self.event, reply_to=None))
        self.assertEqual(text, "")
        self.assertIsNone(quote)
        self.assertIsNotNone(notice)
        read.assert_not_called()
        (_, text, quote, notice), _ = self.prepare(response=self.response("@Grok"))
        self.assertEqual(text, "")
        self.assertEqual(quote["status"], "empty")
        self.assertIsNotNone(notice)

    def test_empty_input_notice_does_not_create_or_dispatch_task(self):
        with mock.patch.object(relay, "first_time", return_value=True), \
             mock.patch.object(relay, "react"), \
             mock.patch.object(progress, "reply_interactive", return_value=(True, "om_hint", {})) as reply, \
             mock.patch.object(progress, "create_ack") as ack, \
             mock.patch.object(chat_queue, "dispatch_task") as dispatch:
            relay.on_message(dict(self.event, reply_to=None))
            reply.assert_called_once()
            self.assertFalse(reply.call_args.kwargs["reply_in_thread"])
            ack.assert_not_called()
            dispatch.assert_not_called()

    def test_unauthorized_request_cannot_fetch_quote(self):
        with mock.patch.object(quoted_context.subprocess, "run") as read:
            relay.on_message(dict(self.event, sender_id="ou_other"))
            read.assert_not_called()

    def test_invalid_quote_id_is_not_used_as_cli_argument(self):
        (_, text, quote, notice), read = self.prepare(dict(self.event, reply_to="--help"))
        read.assert_not_called()
        self.assertEqual(quote["error"], "invalid_message_id")
        self.assertIsNotNone(notice)

    def test_quote_length_is_bounded(self):
        (_, text, quote, _), _ = self.prepare(response=self.response("x" * 7000))
        self.assertEqual(len(quote["text"]), 6000)
        self.assertTrue(quote["truncated"])
        self.assertIn("引用内容已截断", text)

    def test_timeout_and_invalid_json_return_read_failure(self):
        for exception in (subprocess.TimeoutExpired("cli", 15), OSError("private detail")):
            with mock.patch.object(quoted_context.subprocess, "run", side_effect=exception):
                self.assertEqual(quoted_context.read_quote(self.event, "cli")["error"], "read_failed")
        (_, _, quote, notice), _ = self.prepare(response=subprocess.CompletedProcess([], 0, "[]", ""))
        self.assertIsNotNone(notice)
        self.assertEqual(quote["error"], "read_failed")


class QuoteWithoutAutomaticHistoryTests(FifoBase):
    def test_new_group_tasks_keep_only_current_text_and_explicit_quote(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'data': {'items': [{
            'message_id': 'om_quote', 'chat_id': 'oc_1', 'msg_type': 'text',
            'body': {'content': json.dumps({'text': '被明确引用的原文'})},
            'sender': {'sender_type': 'user'}}]}}), '')
        with mock.patch.dict(os.environ, {'FEISHU_BOT_OPEN_ID': 'ou_bot'}), \
             mock.patch.object(quoted_context.conversation_context, 'capture') as history, \
             mock.patch.object(quoted_context.conversation_context, 'append_context') as append, \
             mock.patch.object(quoted_context.subprocess, 'run', return_value=response) as read, \
             mock.patch.object(relay, 'react'):
            for index, (body, quote) in enumerate((('检查当前状态', None),
                                                  ('解释引用', 'om_quote'), ('', 'om_quote'))):
                event = {'message_id': 'om_current_%s' % index, 'event_id': 'evt_%s' % index,
                         'chat_id': 'oc_1', 'chat_type': 'group', 'create_time': '200',
                         'sender_id': 'ou_owner', 'sender_type': 'user', 'message_type': 'text',
                         'content': '@_user_1 ' + body, 'reply_to': quote,
                         'mentions': [{'id': 'ou_bot', 'key': '@_user_1', 'name': 'Grok'}]}
                relay.on_message(event)
                task = progress.load_task(progress.make_task_id(event['message_id']))
                self.assertNotIn('conversation_context', task)
                rebuilt = chat_queue.build_summary_from_task(task)
                self.assertNotIn('conversation_context', rebuilt)
                self.assertNotIn('【两次 @Grok', task['request_text'])
                self.assertEqual(task['user_text'], body)
                self.assertEqual(rebuilt['message_id'], event['message_id'])
                if quote:
                    self.assertIn('被明确引用的原文', task['request_text'])
                    self.assertEqual(rebuilt['quoted_context']['message_id'], quote)
                else:
                    self.assertEqual(task['request_text'], body)
            history.assert_not_called()
            append.assert_not_called()
            self.assertEqual(read.call_count, 2)
            self.assertNotIn('conversation_context', self.posts[0])


if __name__ == "__main__":
    unittest.main()
