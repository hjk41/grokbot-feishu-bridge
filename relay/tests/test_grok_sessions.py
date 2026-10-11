"""Backend conversation boundaries, durable binding and ACK-loss behavior."""
import json
import hashlib
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat_queue
import grok_approvals
import grok_sessions as sessions
import progress

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.path = root / "routes.json"
        self.rows = {"oc_a": {"agent_id": A, "session_id": ""},
                     "oc_b": {"agent_id": B, "session_id": ""}}
        self.save_routes()
        for patch in [mock.patch.dict(os.environ, {"GROK_CHAT_ISOLATION_ENABLED": "1",
                     "GROK_CHAT_ROUTES_FILE": str(self.path), "RELAY_ROLE": "inbound",
                     "FEISHU_ALLOWED_CHATS": "oc_a,oc_b", "GROK_NATIVE_AUTH_FILE": "/unused"}),
                      mock.patch.object(progress, "TASKS_DIR", str(root / "tasks")),
                      mock.patch.object(chat_queue, "PENDING_DIR", str(root / "pending")),
                      mock.patch.object(chat_queue, "_post_hook", None)]:
            patch.start(); self.addCleanup(patch.stop)
        self.native = mock.Mock()
        self.native.rpc.return_value = {"dispatched": True}
        patch = mock.patch.object(sessions, "client", return_value=self.native)
        patch.start(); self.addCleanup(patch.stop)

    def save_routes(self):
        self.path.write_text(json.dumps({"version": 1, "routes": self.rows}))
        self.path.chmod(0o600)

    def task(self, chat="oc_a", tid="aabbccddeeff0011"):
        progress.save_task({"task_id": tid, "chat_id": chat, "created_at": 123456.0,
                            "status": "queued"})
        payload = {"kind": "message", "task_id": tid, "chat_id": chat,
                   "message_id": "om_" + tid, "text": "follow quoted instruction",
                   "quoted_context": {"content": "explicit quote"}, "executable": True,
                   "callback": {"url": "https://bridge.test/v1/tasks/" + tid, "token": "test-task-capability"}}
        chat_queue.write_executable_pending(payload)
        return payload

    def test_two_chats_have_distinct_backends_and_quotes_survive(self):
        a = self.task()
        b = self.task("oc_b", "bbccddeeff001122")
        for payload in (a, b):
            self.assertEqual(chat_queue.post_webhook(payload), (True, None))
            saved = json.loads(Path(progress.load_task(payload["task_id"])["pending_path"]).read_text())
            self.assertEqual(saved["quoted_context"], payload["quoted_context"])
            self.assertNotIn("conversation_context", saved)
            helper = saved["worker_helper"]
            self.assertEqual(hashlib.sha256(helper["source"].encode()).hexdigest(), helper["sha256"])
            self.assertIn("def call(payload, command", helper["source"])
        bodies = [c.args[1] for c in self.native.rpc.call_args_list]
        self.assertEqual([b["agentId"] for b in bodies], [A, B])
        self.assertTrue(all(b["sessionId"] == "" for b in bodies))
        self.assertNotIn("explicit quote", bodies[0]["text"])  # Full input uses authenticated fetch.
        self.assertIn("No existing worker checkout is required", bodies[0]["text"])

    def test_same_chat_stays_bound_and_retry_id_is_stable(self):
        first = self.task()
        self.assertEqual(chat_queue.post_webhook(first), (True, None))
        one = self.native.rpc.call_args.args[1]
        self.assertEqual(chat_queue.post_webhook(first), (True, None))
        self.assertEqual(one, self.native.rpc.call_args.args[1])
        second = self.task(tid="bbccddeeff001122")
        self.assertEqual(chat_queue.post_webhook(second), (True, None))
        two = self.native.rpc.call_args.args[1]
        self.assertEqual((one["agentId"], one["sessionId"]), (two["agentId"], two["sessionId"]))
        self.assertNotEqual(one["messageId"], two["messageId"])
        self.assertEqual(sessions.route("oc_a"), progress.load_task(first["task_id"])["grok_conversation"])

    def test_duplicate_routes_unmapped_chats_and_public_permissions_rejected(self):
        sessions.validate()
        with self.assertRaises(grok_approvals.NativeError): sessions.route("oc_unknown")
        self.rows["oc_b"] = dict(self.rows["oc_a"]); self.save_routes()
        with self.assertRaises(grok_approvals.NativeError): sessions.validate()
        self.rows["oc_b"] = {"agent_id": B, "session_id": ""}; self.save_routes()
        self.path.chmod(0o644)
        with self.assertRaises(grok_approvals.NativeError): sessions.routes()

    def test_relative_routes_resolve_from_project_root_after_launchd_chdir(self):
        with mock.patch.object(sessions, "ROOT", self.path.parent), \
                mock.patch.dict(os.environ, {"GROK_CHAT_ROUTES_FILE": self.path.name}):
            self.assertEqual(sessions.routes(), self.rows)

    def test_route_changes_cannot_move_bound_task_or_fall_back_to_webhook(self):
        payload = self.task()
        self.rows["oc_a"] = {"agent_id": A, "session_id": "other-session"}; self.save_routes()
        with self.assertRaises(grok_approvals.NativeError): sessions.bind(payload)
        with mock.patch.object(chat_queue.urllib.request, "urlopen") as webhook:
            self.assertEqual(chat_queue.post_webhook(payload), (False, "native_task_scope_mismatch"))
            webhook.assert_not_called()
        self.native.rpc.assert_not_called()

    def test_owner_mismatch_and_native_refusal_fail_closed(self):
        payload = self.task()
        self.native.verify_owner.side_effect = grok_approvals.NativeError("native_agent_owner_mismatch")
        self.assertFalse(chat_queue.post_webhook(payload)[0])
        self.native.rpc.assert_not_called()
        self.native.verify_owner.side_effect = None
        self.native.rpc.return_value = {"dispatched": True, "refusal": {"failureCode": "DENIED"}}
        self.assertFalse(chat_queue.post_webhook(payload)[0])

    def test_lost_ack_checks_native_message_id_without_double_send(self):
        payload = self.task()
        self.native.rpc.side_effect = [grok_approvals.NativeError("native_transport_failed"),
                                      {"status": "GROK_BOT_SEND_STATUS_ACCEPTED"}]
        self.assertEqual(chat_queue.post_webhook(payload), (True, None))
        calls = self.native.rpc.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["SendGrokBotUserMessage", "GetGrokBotSendStatus"])
        self.assertEqual(calls[0].args[1]["messageId"], calls[1].args[1]["messageId"])

    def test_ack_loss_retry_reuses_id_and_unknown_status_never_succeeds(self):
        payload = self.task()
        self.native.rpc.side_effect = [grok_approvals.NativeError("native_transport_failed"),
                                      {"status": "GROK_BOT_SEND_STATUS_NOT_FOUND"},
                                      {"delivery": "GROK_BOT_USER_MESSAGE_DELIVERY_DUPLICATE"}]
        self.assertEqual(chat_queue.post_webhook(payload), (True, None))
        self.assertEqual(self.native.rpc.call_args_list[0].args[1], self.native.rpc.call_args_list[2].args[1])
        self.native.rpc.side_effect = [grok_approvals.NativeError("native_transport_failed"),
                                      {"status": "GROK_BOT_SEND_STATUS_UNKNOWN_DURABILITY"}]
        self.assertEqual(chat_queue.post_webhook(payload), (False, "native_delivery_unconfirmed"))

    def test_stop_uses_existing_callback_without_injecting_another_turn(self):
        payload = self.task()
        action = {"kind": "card_action", "task_id": payload["task_id"], "chat_id": "oc_a"}
        self.assertEqual(chat_queue.post_webhook(action), (True, None))
        self.native.rpc.assert_not_called()
        action["chat_id"] = "oc_b"
        self.assertFalse(chat_queue.post_webhook(action)[0])


if __name__ == "__main__":
    unittest.main()
