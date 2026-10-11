"""Native approval integrity, restart recovery and actual decision dispatch."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import grok_approvals as approvals
import progress
import relay
import settings


def entry(status="pending", command="ls /workspace", kind="auto-review-approval"):
    body = {"kind": "send-message", "id": "ar-request", "timestampMs": 123456,
            "message": {"type": kind, "approval" if kind == "auto-review-approval" else "permission":
                        {"requestId": "req-1", "status": status, "command": command,
                         "summary": "查看目录", "reason": "需要单次权限", "surface": "box_shell"}}}
    return {"entryId": "ar-request", "seq": "15", "body": base64.b64encode(json.dumps(body).encode()).decode()}


class FakeNative:
    agent_id = "owned-agent"
    def __init__(self):
        self.rows, self.decisions = [entry()], []
        self.fail = False
    def snapshot(self, session_id=""):
        return self.rows
    def resolve(self, request, decision):
        self.decisions.append((request, decision))
        if self.fail:
            raise approvals.NativeError("native_transport_failed")
        return {"dispatched": True}


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        env = mock.patch.dict(os.environ, {"FEISHU_ALLOWED_USERS": "ou_owner,ou_second",
            "FEISHU_ALLOWED_CHATS": "oc_inbox,oc_group", "BRIDGE_PUBLIC_URL": "https://bridge.test/callback"})
        env.start(); self.addCleanup(env.stop)
        patch = mock.patch.object(progress, "TASKS_DIR", str(Path(self.root.name) / "tasks"))
        patch.start(); self.addCleanup(patch.stop)
        self.client = FakeNative()
        self.manager = approvals.ApprovalManager(self.client, "oc_inbox", "ou_owner", self.root.name)
        self.manager.ready = True
        self.sent = []
        def send(state, card):
            self.sent.append(card)
            return True, "om_approval", {}
        for patch in [mock.patch.object(approvals, "send_inbox", side_effect=send),
                      mock.patch.object(progress, "reply_interactive", return_value=(True, "om_approval", {})),
                      mock.patch.object(progress, "patch_interactive", return_value=(True, {}, 1))]:
            patch.start(); self.addCleanup(patch.stop)

    def state(self):
        key = approvals.parse_entry(self.client.rows[0], self.client.agent_id)["approval_id"]
        return self.manager.load(key)

    def event(self, action="approval_allow_once"):
        state = self.state()
        return {"event_id": "evt_1", "operator_id": "ou_owner", "chat_id": state["chat_id"],
                "message_id": "om_approval", "action_value": {"action": action,
                "approval_id": state["approval_id"], "nonce": state["nonce"]}}

    def test_sync_pending_and_restart_do_not_duplicate_card(self):
        self.manager.sync(self.client.rows)
        self.manager = approvals.ApprovalManager(self.client, "oc_inbox", "ou_owner", self.root.name)
        self.manager.ready = True
        self.manager.sync(self.client.rows)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.state()["chat_id"], "oc_inbox")
        self.assertEqual(os.stat(self.manager.path(self.state()["approval_id"])).st_mode & 0o777, 0o600)

    def test_old_resolved_entries_never_send_cards(self):
        self.manager.sync([entry("approved"), entry("always"), entry("expired")])
        self.assertFalse(self.sent)

    def test_app_resolution_updates_existing_card_without_decision_rpc(self):
        self.manager.sync(self.client.rows)
        self.client.rows = [entry("approved")]
        self.manager.sync(self.client.rows)
        self.assertEqual(self.state()["native_status"], "approved")
        self.assertFalse(self.manager.handle_card(self.event()))
        self.assertFalse(self.client.decisions)
        self.assertNotIn("允许本次", json.dumps(approvals.build_card(self.state()), ensure_ascii=False))

    def test_allow_once_reaches_exact_native_request_and_replay_is_rejected(self):
        self.manager.sync(self.client.rows)
        event = self.event()
        self.assertTrue(settings.card_allowed(event))
        self.assertTrue(self.manager.handle_card(event))
        self.assertFalse(self.manager.handle_card(event))
        self.assertEqual(len(self.client.decisions), 1)
        self.assertEqual(self.client.decisions[0][0]["request_id"], "req-1")
        self.assertEqual(self.client.decisions[0][1], 1)

    def test_deny_reaches_native_api(self):
        self.manager.sync(self.client.rows)
        self.assertTrue(self.manager.handle_card(self.event("approval_deny")))
        self.assertEqual(self.client.decisions[0][1], 2)

    def test_identity_chat_card_nonce_and_unknown_action_are_bound(self):
        self.manager.sync(self.client.rows)
        ev = self.event()
        for variant in [{"operator_id": "ou_second"}, {"chat_id": "oc_group"},
                        {"message_id": "om_other"}, {"action_value": {**ev["action_value"], "nonce": "0" * 32}},
                        {"action_value": {**ev["action_value"], "action": "approval_always"}}]:
            self.assertFalse(self.manager.handle_card({**ev, **variant}))
        self.assertFalse(self.client.decisions)

    def test_native_changes_before_click_require_new_nonce_and_card(self):
        self.manager.sync(self.client.rows)
        ev = self.event()
        self.client.rows = [entry(command="rm /workspace/changed")]
        self.assertFalse(self.manager.handle_card(ev))
        self.assertNotEqual(ev["action_value"]["nonce"], self.state()["nonce"])
        self.assertFalse(self.client.decisions)
        progress.patch_interactive.assert_called()

    def test_native_missing_entry_never_dispatches(self):
        self.manager.sync(self.client.rows)
        ev = self.event(); self.client.rows = []
        self.assertFalse(self.manager.handle_card(ev))
        self.assertFalse(self.client.decisions)
        key = ev["action_value"]["approval_id"]
        self.assertEqual(self.manager.load(key)["native_status"], "unavailable")


    def test_uncertain_network_decision_is_not_replayed_after_restart(self):
        self.manager.sync(self.client.rows)
        ev = self.event(); self.client.fail = True
        self.assertFalse(self.manager.handle_card(ev))
        self.manager = approvals.ApprovalManager(self.client, "oc_inbox", "ou_owner", self.root.name)
        self.manager.ready = True
        self.assertFalse(self.manager.handle_card(ev))
        self.assertEqual(len(self.client.decisions), 1)
        self.client.rows = [entry("approved")]
        self.manager.sync(self.client.rows)
        self.assertNotIn("error", self.state())

    def test_exact_callback_association_routes_group_and_suspends_timeout(self):
        tid = "aabbccddeeff0011"
        progress.save_task({"task_id": tid, "sender_open_id": "ou_owner", "chat_id": "oc_group",
                            "message_id": "om_orig", "chat_type": "group", "status": "running",
                            "last_stage_at": 1, "created_at": 1})
        self.client.rows = [entry(command="POST https://bridge.test/callback/v1/tasks/" + tid)]
        self.manager.sync(self.client.rows)
        state = self.state()
        self.assertEqual(state["task_id"], tid)
        self.assertEqual(state["chat_id"], "oc_group")
        self.assertTrue(progress.load_task(tid)["pending_approvals"])
        self.assertIsNone(progress._maybe_timeout_unlocked(progress.load_task(tid)))
        progress.reply_interactive.assert_called_once()
        self.assertFalse(progress.reply_interactive.call_args.kwargs["reply_in_thread"])
        self.client.rows = [entry("denied", command=self.client.rows and state["command"])]
        self.manager.sync(self.client.rows)
        self.assertFalse(progress.load_task(tid)["pending_approvals"])

    def test_time_proximity_and_foreign_callback_never_route_to_group(self):
        self.client.rows = [entry(command="POST https://other.test/v1/tasks/aabbccddeeff0011")]
        self.manager.sync(self.client.rows)
        self.assertEqual(self.state()["chat_id"], "oc_inbox")
        self.assertNotIn("task_id", self.state())

    def test_other_native_conversation_cannot_route_callback_or_pause_task(self):
        tid = "aabbccddeeff0011"
        progress.save_task({"task_id": tid, "sender_open_id": "ou_owner", "chat_id": "oc_group",
                            "message_id": "om_orig", "status": "running",
                            "grok_conversation": {"agent_id": "other-agent", "session_id": ""}})
        self.client.rows = [entry(command="POST https://bridge.test/callback/v1/tasks/" + tid)]
        self.manager.sync(self.client.rows)
        self.assertEqual(self.state()["chat_id"], "oc_inbox")
        self.assertNotIn("task_id", self.state())
        self.assertNotIn("pending_approvals", progress.load_task(tid))

    def test_foreign_manager_cannot_resolve_shared_directory_state(self):
        self.manager.sync(self.client.rows)
        other = FakeNative(); other.agent_id = "other-agent"
        manager = approvals.ApprovalManager(other, "oc_inbox", "ou_owner", self.root.name)
        manager.ready = True
        self.assertFalse(manager.handle_card(self.event()))
        self.assertFalse(other.decisions)

    def test_scoped_approval_resolves_in_exact_native_session(self):
        tid = "aabbccddeeff0011"
        progress.save_task({"task_id": tid, "sender_open_id": "ou_owner", "chat_id": "oc_group",
                            "message_id": "om_orig", "status": "running",
                            "grok_conversation": {"agent_id": self.client.agent_id, "session_id": "session-a"}})
        self.client.rows = [entry(command="POST https://bridge.test/callback/v1/tasks/" + tid)]
        self.manager.sync(self.client.rows, "session-a")
        key = approvals.parse_entry(self.client.rows[0], self.client.agent_id, "session-a")["approval_id"]
        state = self.manager.load(key)
        self.assertEqual(state["chat_id"], "oc_group")
        event = {"event_id": "evt_1", "operator_id": "ou_owner", "chat_id": "oc_group",
                 "message_id": "om_approval", "action_value": {"action": "approval_allow_once",
                     "approval_id": key, "nonce": state["nonce"]}}
        self.assertTrue(self.manager.handle_card(event))
        self.assertEqual(self.client.decisions[0][0]["session_id"], "session-a")

    def test_long_command_requires_app_review_and_cannot_be_allowed(self):
        self.client.rows = [entry(command="x" * 6001)]
        self.manager.sync(self.client.rows)
        self.assertNotIn("允许本次", json.dumps(self.sent[0], ensure_ascii=False))
        self.assertFalse(self.manager.handle_card(self.event()))
        self.assertFalse(self.client.decisions)

    def test_decisions_disabled_until_native_owner_verified(self):
        self.manager.sync(self.client.rows)
        self.manager.ready = False
        self.assertFalse(self.manager.handle_card(self.event()))
        self.assertFalse(self.client.decisions)

    def test_resolved_approval_does_not_keep_resetting_task_stale_timer(self):
        tid = "aabbccddeeff0011"
        progress.save_task({"task_id": tid, "sender_open_id": "ou_owner", "chat_id": "oc_group",
                            "message_id": "om_orig", "chat_type": "group", "status": "running"})
        command = "POST https://bridge.test/callback/v1/tasks/" + tid
        self.client.rows = [entry(command=command)]
        self.manager.sync(self.client.rows)
        self.client.rows = [entry("approved", command=command)]
        self.manager.sync(self.client.rows)
        st = progress.load_task(tid); st["last_stage_at"] = 1; progress.save_task(st)
        self.manager.sync(self.client.rows)
        self.assertEqual(progress.load_task(tid)["last_stage_at"], 1)

    def test_display_redacts_credentials(self):
        with mock.patch.dict(os.environ, {"FEISHU_RELAY_WEBHOOK_KEY": "sensitive_value_xyz"}):
            value = approvals.display_text('curl -H "Authorization: Bearer abcd.secret.jwt" '
                                           'token=sensitive_value_xyz password=example-secret')
        for secret in ["abcd.secret.jwt", "sensitive_value_xyz", "example-secret"]:
            self.assertNotIn(secret, value)

    def test_unknown_status_and_invalid_body_are_not_actionable(self):
        self.assertIsNone(approvals.parse_entry(entry("new_unknown_status"), "owned-agent"))
        self.assertIsNone(approvals.parse_entry({"body": "not-json"}, "owned-agent"))

    def test_native_card_never_spawns_routine_or_touches_fifo(self):
        self.manager.sync(self.client.rows)
        with mock.patch.object(relay, "first_time", return_value=True), \
                mock.patch.object(approvals, "_manager", self.manager), \
                mock.patch.object(relay.chat_queue, "post_webhook") as webhook:
            relay.on_card(self.event())
        webhook.assert_not_called()
        self.assertEqual(len(self.client.decisions), 1)

    def test_native_rpc_payload_is_single_use_and_exact_command(self):
        client = approvals.NativeClient("https://native.test", "/unused", "owned-agent")
        request = approvals.parse_entry(entry(), "owned-agent")
        with mock.patch.object(client, "rpc", return_value={"dispatched": True}) as rpc:
            client.resolve(request, 1)
            self.assertEqual(rpc.call_args.args, ("ResolveGrokBotAutoReviewApproval", {
                "agentId": "owned-agent", "requestId": "req-1", "resolution": 1,
                "approvalPlatform": "feishu", "approvedCommand": "ls /workspace"}))
            with self.assertRaises(approvals.NativeError):
                client.resolve(request, 3)

    def test_snapshot_pagination_and_generation_change(self):
        client = approvals.NativeClient("https://native.test", "/unused", "owned-agent")
        page = [{"seq": str(i)} for i in range(201, 401)]
        with mock.patch.object(client, "rpc", side_effect=[{"entries": page, "generation": 1},
                                                          {"entries": [{"seq": "200"}], "generation": 1}]) as rpc:
            self.assertEqual(len(client.snapshot()), 201)
            self.assertEqual(rpc.call_args.args[1]["beforeSeq"], "201")
        with mock.patch.object(client, "rpc", side_effect=[{"entries": page, "generation": 1},
                                                          {"entries": [], "generation": 2}]):
            with self.assertRaises(approvals.NativeError):
                client.snapshot()

    def test_auth_requires_owned_agent_and_current_routine(self):
        client = approvals.NativeClient("https://native.test", "/unused", "owned-agent")
        with mock.patch.object(client, "rpc", return_value={"agents": [{"agentId": "owned-agent", "viewerIsOwner": False}]}):
            with self.assertRaises(approvals.NativeError):client.verify("routine-id")

    def test_transient_owner_read_is_retried_without_any_decision_or_send(self):
        client = approvals.NativeClient("https://native.test", "/unused", "owned-agent")
        with mock.patch.object(client, "rpc", side_effect=[
                approvals.NativeError("native_transport_failed"),
                {"agents": [{"agentId": "owned-agent", "viewerIsOwner": True}]}]) as rpc:
            client.verify_owner()
            self.assertEqual([c.args[0] for c in rpc.call_args_list], ["ListGrokBotAgents", "ListGrokBotAgents"])
        with mock.patch.object(client, "rpc", side_effect=[
                {"agents": [{"agentId": "owned-agent", "viewerIsOwner": True}]}, {"automations": []}]):
            with self.assertRaises(approvals.NativeError):client.verify("routine-id")


if __name__ == "__main__":
    unittest.main()
