"""Mention boundaries, full pagination, group isolation and durable snapshots."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import conversation_context as context
import chat_queue
import progress
import relay


def message(mid, text, *, chat="oc_group", timestamp=100, sender="ou_member", mentions=None):
    return {"message_id": mid, "chat_id": chat, "create_time": str(timestamp),
            "sender": {"id": sender, "sender_type": "user"}, "msg_type": "text",
            "body": {"content": json.dumps({"text": text})}, "mentions": mentions or []}


class ConversationContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patch in (mock.patch.object(progress, "TASKS_DIR", self.tmp.name + "/tasks"),
                      mock.patch.dict(os.environ, {"FEISHU_ALLOWED_USERS": "ou_owner",
                          "FEISHU_ALLOWED_CHATS": "oc_group,oc_other", "FEISHU_BOT_OPEN_ID": "ou_bot"})):
            patch.start()
            self.addCleanup(patch.stop)
        Path(progress.TASKS_DIR).mkdir()
        self.event = {"message_id": "om_current", "chat_id": "oc_group", "chat_type": "group",
                      "sender_id": "ou_owner", "sender_type": "user", "message_type": "text",
                      "create_time": "200", "content": "@Grok 总结讨论",
                      "mentions": [{"id": "ou_bot"}]}
        self.previous = {"message_id": "om_previous", "create_time": "50"}

    def page(self, items, more=False, token=None):
        return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {
            "items": items, "has_more": more, "page_token": token}}), "")

    def test_full_interval_spans_pages_and_has_no_twenty_message_cap(self):
        items = [message("om_current", "now", timestamp=200)]
        items += [message("om_%d" % i, "未 @ 的讨论 %d" % i, timestamp=i) for i in range(150, 90, -1)]
        pages = [self.page(items[:50], True, "cursor"),
                 self.page(items[50:] + [message("om_previous", "before", timestamp=50),
                                        message("om_old", "must not include", timestamp=40)])]
        with mock.patch.object(context.subprocess, "run", side_effect=pages) as run:
            result = context.read_interval(self.event, "fake-cli", self.previous)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["messages"]), 60)
        self.assertEqual(result["messages"][0]["message_id"], "om_91")
        self.assertEqual(result["messages"][-1]["message_id"], "om_150")
        self.assertEqual(json.loads(run.call_args.args[0][5])["page_token"], "cursor")
        self.assertIn("--as", run.call_args.args[0])
        for call in run.call_args_list:
            self.assertEqual(json.loads(call.args[0][5])["card_msg_content_type"], "user_card_content")

    def test_original_card_body_is_preserved_and_placeholder_is_partial(self):
        card = message("om_card", "unused")
        card["msg_type"] = "interactive"
        original = {"schema": "2.0", "body": {"elements": [
            {"tag": "markdown", "content": "方案1：授权 GitHub App；方案2：提供 diff"}]}}
        placeholder = {"title": "Grok · 结果", "elements": [[
            {"tag": "text", "text": "请升级至最新版本客户端，以查看内容"},
            {"tag": "text", "text": ""}]]}
        for body, complete in ((original, True), (placeholder, False)):
            card["body"]["content"] = json.dumps(body)
            with mock.patch.object(context.subprocess, "run", return_value=self.page([
                message("om_current", "now", timestamp=200), card,
                message("om_previous", "before", timestamp=50)])):
                result = context.read_interval(self.event, "cli", self.previous)
            self.assertEqual(result["complete"], complete)
            text = context.append_context("我按方案1安装了", result)
            if complete:
                self.assertIn("授权 GitHub App", text)
                self.assertIn("提供 diff", text)
            else:
                self.assertEqual(result["error"], "card_content_unavailable")
                self.assertIn("不得假定已看到全部", text)
        # A proper card discussing this bug is real content, not a placeholder.
        card["body"]["content"] = json.dumps({"schema": "2.0", "body": {"elements": [
            {"tag": "text", "text": "请升级至最新版本客户端，以查看内容"}]}})
        self.assertFalse(context.card_placeholder(card))
        card["body"]["content"] = json.dumps(placeholder)
        card["create_time"] = "40"
        with mock.patch.object(context.subprocess, "run", return_value=self.page([
            message("om_current", "now", timestamp=200),
            message("om_previous", "before", timestamp=50), card])):
            self.assertTrue(context.read_interval(self.event, "cli", self.previous)["complete"])

    def test_ignores_newer_messages_and_deduplicates_pages(self):
        with mock.patch.object(context.subprocess, "run", side_effect=[
            self.page([message("om_future", "future"), message("om_current", "now"),
                       message("om_a", "a")], True, "next"),
            self.page([message("om_a", "a"), message("om_previous", "before")])]):
            result = context.read_interval(self.event, "cli", self.previous)
        self.assertEqual([m["message_id"] for m in result["messages"]], ["om_a"])

    def test_first_use_discovers_only_authorized_previous_mention(self):
        bot = [{"id": "ou_bot"}]
        with mock.patch.object(context.subprocess, "run", return_value=self.page([
            message("om_current", "now"), message("om_other_at", "@Grok unrelated", mentions=bot),
            message("om_discussion", "important"),
            message("om_authorized", "@Grok earlier", sender="ou_owner", mentions=bot),
            message("om_older", "old")])):
            result = context.read_interval(self.event, "cli")
        self.assertTrue(result["complete"])
        self.assertEqual(result["previous_trigger"]["message_id"], "om_authorized")
        self.assertEqual(len(result["messages"]), 2)

    def test_failures_never_claim_complete_or_leak_cli_details(self):
        response = subprocess.CompletedProcess([], 3, "", json.dumps({"ok": False,
            "error": {"code": 230027, "message": "PRIVATE SECRET"}}))
        with mock.patch.object(context.subprocess, "run", return_value=response):
            result = context.read_interval(self.event, "cli", self.previous)
        self.assertFalse(result["complete"])
        self.assertEqual(result["error"], "permission_denied")
        text = context.append_context("总结", result)
        self.assertIn("不得假定已看到全部", text)
        self.assertNotIn("PRIVATE", text)

    def test_cross_chat_content_is_not_retained(self):
        with mock.patch.object(context.subprocess, "run", return_value=self.page([
            message("om_current", "now"), message("om_private", "PRIVATE", chat="oc_other")])):
            result = context.read_interval(self.event, "cli", self.previous)
        self.assertEqual(result["error"], "message_scope_mismatch")
        self.assertEqual(result["messages"], [])

    def test_missing_boundaries_and_repeated_cursor_are_explicit(self):
        for pages, expected in (
            ([self.page([message("om_a", "a")])], "current_message_not_visible"),
            ([self.page([message("om_current", "now"), message("om_a", "a")])], "previous_boundary_not_visible"),
            ([self.page([message("om_current", "now")], True, "same"),
              self.page([message("om_a", "a")], True, "same")], "invalid_pagination")):
            with mock.patch.object(context.subprocess, "run", side_effect=pages):
                result = context.read_interval(self.event, "cli", self.previous)
            self.assertFalse(result["complete"])
            self.assertEqual(result["error"], expected)

    def test_snapshot_restart_and_per_group_boundaries(self):
        result = {"complete": True, "status": "ok", "messages": [
            {"message_id": "om_a", "content": "discussion"}]}
        with mock.patch.object(context, "read_interval", return_value=result) as read:
            first = context.capture(self.event, "cli")
            self.assertEqual(context.capture(self.event, "cli"), first)
            self.assertEqual(read.call_count, 1)
            next_event = dict(self.event, message_id="om_next", create_time="300")
            context.capture(next_event, "cli")
            self.assertEqual(read.call_args.args[2]["message_id"], "om_current")
            context.capture(dict(next_event, chat_id="oc_other"), "cli")
            self.assertIsNone(read.call_args.args[2])
        state = {"task_id": "saved", "request_text": "original discussion",
                 "user_text": "总结讨论", "conversation_context": first}
        rebuilt = chat_queue.build_summary_from_task(state)
        self.assertEqual(rebuilt["conversation_context"], first)
        self.assertEqual(rebuilt["user_text"], "总结讨论")
        for path in Path(self.tmp.name, "conversations").glob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_upgrade_bootstrap_and_late_trigger_do_not_regress_boundary(self):
        task = {"chat_id": "oc_group", "chat_type": "group", "message_id": "om_previous",
                "create_time": "50", "user_text": "上次任务"}
        Path(progress.TASKS_DIR, "old.json").write_text(json.dumps(task))
        with mock.patch.object(context, "read_interval", return_value={"complete": True, "messages": []}) as read:
            context.capture(self.event, "cli")
            self.assertEqual(read.call_args.args[2]["message_id"], "om_previous")
            late = context.capture(dict(self.event, message_id="om_late", create_time="150"), "cli")
            self.assertEqual(late["error"], "out_of_order_trigger")
            context.capture(dict(self.event, message_id="om_next", create_time="300"), "cli")
            self.assertEqual(read.call_args.args[2]["message_id"], "om_current")

    def test_card_repair_is_once_only_durable_and_group_scoped(self):
        card = message("om_repair", "unused", timestamp=100)
        card.update(msg_type="interactive", body={"content": json.dumps({"schema": "2.0",
            "body": {"elements": [{"tag": "markdown", "content": "方案1安装步骤"}]}})})
        with mock.patch.object(context, "read_interval", side_effect=lambda *args: {"complete": True, "messages": []}):
            original = context.capture(self.event, "cli")
            self.assertTrue(context.stage_card_repair("oc_group", [card]))
            self.assertFalse(context.stage_card_repair("oc_group", [card]))
            for chat in ("oc_other", "oc_private"):
                with self.assertRaises(ValueError):
                    context.stage_card_repair(chat, [card])
            with self.assertRaises(ValueError):
                context.stage_card_repair("oc_group", [dict(card, create_time="999")])
            # Duplicates, out-of-order deliveries and other groups cannot consume it.
            self.assertEqual(context.capture(self.event, "cli"), original)
            late = context.capture(dict(self.event, message_id="om_late", create_time="150"), "cli")
            self.assertNotIn("restored_prior_cards", late)
            other = context.capture(dict(self.event, chat_id="oc_other"), "cli")
            self.assertNotIn("restored_prior_cards", other)
            event = dict(self.event, message_id="om_next", create_time="300")
            restored = context.capture(event, "cli")
            text = context.append_context("验证授权", restored)
            self.assertIn("方案1安装步骤", text)
            self.assertIn("不得因此重新执行旧任务", text)
            self.assertEqual(context.capture(event, "cli"), restored)
            self.assertFalse(context.stage_card_repair("oc_group", [card]))
            later = context.capture(dict(event, message_id="om_later", create_time="400"), "cli")
            self.assertNotIn("restored_prior_cards", later)

    def test_placeholder_cannot_be_staged_as_card_repair(self):
        card = message("om_repair", "unused", timestamp=100)
        card.update(msg_type="interactive", body={"content": json.dumps({"elements": [[
            {"tag": "text", "text": "请升级至最新版本客户端，以查看内容"}]]})})
        with self.assertRaises(ValueError):
            context.stage_card_repair("oc_group", [card])

    def test_unmentioned_or_unauthorized_messages_do_not_fetch_or_dispatch(self):
        with mock.patch.object(context.subprocess, "run") as run, \
             mock.patch.object(progress, "create_ack") as ack, \
             mock.patch.object(relay, "first_time") as dedup:
            for changes in ({"mentions": []}, {"sender_id": "ou_other"}, {"chat_id": "oc_private"}):
                relay.on_message(dict(self.event, **changes))
            run.assert_not_called()
            ack.assert_not_called()
            dedup.assert_not_called()

    def test_empty_mention_does_not_capture_history_or_dispatch(self):
        with mock.patch.object(context, "capture") as capture, \
             mock.patch.object(relay, "first_time", return_value=True), \
             mock.patch.object(relay, "react"), \
             mock.patch.object(progress, "reply_interactive", return_value=(True, "hint", {})), \
             mock.patch.object(progress, "create_ack") as ack:
            relay.on_message(dict(self.event, content="@Grok"))
            ack.assert_not_called()
            capture.assert_not_called()

    def test_old_thread_roots_and_current_thread_are_expanded(self):
        # The previous @ is a group message; the current @ and intervening
        # discussion are replies under a root created much earlier.
        old_root = dict(message("om_root", "old topic", timestamp=10), thread_id="omt_old")
        pages = [self.page([message("om_group", "群里讨论", timestamp=100),
                           message("om_previous", "previous", timestamp=50), old_root]),
                 self.page([message("om_current", "now", timestamp=200),
                            message("om_thread", "话题讨论", timestamp=150), old_root])]
        with mock.patch.object(context.subprocess, "run", side_effect=pages) as run:
            result = context.read_interval(dict(self.event, thread_id="omt_old"), "cli", self.previous)
        self.assertTrue(result["complete"])
        self.assertEqual([m["message_id"] for m in result["messages"]], ["om_group", "om_thread"])
        self.assertEqual(json.loads(run.call_args.args[0][5])["container_id_type"], "thread")
        for call in run.call_args_list:
            self.assertEqual(json.loads(call.args[0][5])["card_msg_content_type"], "user_card_content")

    def test_thread_failure_returns_only_explicitly_partial_context(self):
        root = dict(message("om_root", "discussion", timestamp=100), thread_id="omt_old")
        pages = [self.page([message("om_current", "now", timestamp=200), root,
                           message("om_previous", "previous", timestamp=50)]),
                 subprocess.CompletedProcess([], 3, "", '{"ok":false,"error":{"code":230027}}')]
        with mock.patch.object(context.subprocess, "run", side_effect=pages):
            result = context.read_interval(self.event, "cli", self.previous)
        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error"], "permission_denied")

    def test_mention_only_does_not_inject_any_old_discussion_or_repairs(self):
        discussion = context._normalize(message("om_a", "这里的搜索页面布局需要调整"))
        snapshot = {"status": "ok", "complete": True, "messages": [discussion]}
        snapshot["restored_prior_cards"] = [discussion]
        with mock.patch.object(context, "capture", return_value=snapshot) as capture, \
             mock.patch.object(relay, "first_time", return_value=True), \
             mock.patch.object(relay, "react"), \
             mock.patch.object(progress, "reply_interactive", return_value=(True, "hint", {})) as hint, \
             mock.patch.object(progress, "create_ack") as ack:
            relay.on_message(dict(self.event, content="@Grok"))
            hint.assert_called_once()
            ack.assert_not_called()
            capture.assert_not_called()

    def test_timeout_is_explicit_and_never_marks_complete(self):
        with mock.patch.object(context.time, "monotonic", side_effect=[0, 61]):
            result = context.read_interval(self.event, "cli", self.previous)
        self.assertEqual(result["error"], "read_deadline_exceeded")
        self.assertFalse(result["complete"])


if __name__ == "__main__":
    unittest.main()
