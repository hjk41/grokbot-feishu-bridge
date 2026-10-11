"""Verify ingress rejects before cards/webhooks, plus safe pending archiving."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive_pending
import progress
import relay
import settings


class IngressTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {
            "FEISHU_ALLOWED_USERS": "ou_owner",
            "FEISHU_ALLOWED_CHATS": "oc_allowed",
            "FEISHU_BOT_OPEN_ID": "ou_bot",
        }, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.event = {"message_id": "om_original", "chat_id": "oc_allowed",
                      "chat_type": "p2p", "sender_id": "ou_owner",
                      "sender_type": "user", "message_type": "text", "content": "hello"}

    def test_empty_allowlists_deny(self):
        for key in ("FEISHU_ALLOWED_USERS", "FEISHU_ALLOWED_CHATS"):
            with mock.patch.dict(os.environ, {key: ""}):
                self.assertFalse(settings.message_allowed(self.event))

    def test_bad_sender_chat_and_schema_rejected_before_side_effects(self):
        variants = [{"sender_id": "ou_other"}, {"chat_id": "oc_other"},
                    {"sender_type": "bot"}, {"message_id": ""},
                    {"message_type": "file"}, {"content": ""}, {"content": "x" * 16001}]
        with mock.patch.object(progress, "create_ack") as card, \
                mock.patch.object(relay, "react") as reaction, \
                mock.patch.object(relay, "first_time") as dedup:
            for changes in variants:
                relay.on_message(dict(self.event, **changes))
            card.assert_not_called()
            reaction.assert_not_called()
            dedup.assert_not_called()

    def test_group_requires_bot_id_not_display_name(self):
        event = dict(self.event, chat_type="group", mentions=[{"id": "ou_other", "name": "Grok"}])
        self.assertFalse(settings.message_allowed(event))
        event["mentions"] = [{"id": "ou_bot", "name": "different display name"}]
        self.assertTrue(settings.message_allowed(event))

    def test_duplicate_message_id_ignores_event_id_change_and_missing_event_id(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(relay, "SEEN_FILE", os.path.join(tmp, "seen")), \
                mock.patch.object(progress, "create_ack", side_effect=RuntimeError("sentinel")) as card:
            relay.seen.clear()
            # Abort after the card attempt so no network or task file is created.
            with mock.patch.object(progress, "task_guard", side_effect=RuntimeError("abort")), \
                    mock.patch.object(relay, "react"):
                for changes in ({}, {"event_id": "redelivery"}):
                    try:
                        relay.on_message(dict(self.event, **changes))
                    except RuntimeError:
                        pass
            self.assertEqual(card.call_count, 1)
            relay.seen.clear()

    def test_card_only_accepts_known_action_and_allowed_identity(self):
        card = {"event_id": "evt_example", "operator_id": "ou_owner", "chat_id": "oc_allowed",
                "action_value": json.dumps({"action": "request_stop", "task_id": "example"})}
        self.assertTrue(settings.card_allowed(card))
        self.assertFalse(settings.card_allowed(dict(card, operator_id="ou_other")))
        self.assertFalse(settings.card_allowed(dict(card, action_value='{"action":"execute","task_id":"example"}')))
        self.assertFalse(settings.card_allowed(dict(card, action_value="invalid")))
        self.assertFalse(settings.card_allowed(dict(card, event_id=None)))

    def test_startup_requires_https_and_credentials(self):
        with self.assertRaises(SystemExit):
            settings.validate_startup()
        with mock.patch.dict(os.environ, {"FEISHU_RELAY_WEBHOOK_KEY": "example",
                                          "FEISHU_RELAY_WEBHOOK_URL": "http://example.invalid"}):
            with self.assertRaises(SystemExit):
                settings.validate_startup()
            os.environ["FEISHU_RELAY_WEBHOOK_URL"] = "https://example.invalid/webhook"
            settings.validate_startup()

    def test_real_transport_explicitly_uses_bot_identity(self):
        with mock.patch.object(progress.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"ok":true}', stderr="")
            progress._default_runner(["lark-cli", "im", "+messages-reply"])
            self.assertEqual(run.call_args.args[0][-2:], ["--as", "bot"])

    def test_reply_card_file_is_relative_to_cli_cwd(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(progress, "DIR", tmp), \
                mock.patch.object(progress, "TASKS_DIR", os.path.join(tmp, "tasks")):
            calls = []
            def runner(argv, stdin_text=None):
                calls.append(argv)
                ref = argv[argv.index("--content") + 1][1:]
                self.assertFalse(os.path.isabs(ref))
                self.assertTrue((Path(tmp) / ref).is_file())
                return 0, '{"ok":true,"data":{"message_id":"om_example"}}', ""
            with mock.patch.object(progress, "_runner", runner):
                ok, mid, raw = progress.reply_interactive("om_original", {"schema": "2.0"})
            self.assertTrue(ok)
            self.assertEqual(len(calls), 1)
            self.assertEqual(list((Path(tmp) / "tasks").iterdir()), [])

    def test_stop_callback_is_bound_to_card_chat_and_task(self):
        event = {"event_id": "evt_stop_example", "operator_id": "ou_owner",
                 "chat_id": "oc_allowed", "message_id": "om_card",
                 "action_value": {"action": "request_stop", "task_id": "task_example"}}
        task = {"chat_id": "oc_allowed", "progress_message_id": "om_card"}
        with mock.patch.object(relay, "first_time", return_value=True), \
                mock.patch.object(progress, "load_task", return_value=task), \
                mock.patch.object(progress, "request_stop", return_value={"stop_accepted": True,
                                                                         "stop_requested": True}) as stop, \
                mock.patch.object(relay.chat_queue, "write_executable_pending") as pending, \
                mock.patch.object(relay.chat_queue, "post_webhook", return_value=(True, None)) as post:
            relay.on_card(dict(event, message_id="om_other_card"))
            stop.assert_not_called()
            post.assert_not_called()
            relay.on_card(event)
            stop.assert_called_once_with("task_example", operator_id="ou_owner")
            self.assertTrue(pending.call_args.args[0]["stop_requested"])
            post.assert_called_once()

    def test_cli_error_envelope_from_stderr_is_permanent_failure(self):
        error = '{"ok":false,"error":{"code":230031,"message":"unavailable"}}'
        with mock.patch.object(progress, "_runner", return_value=(1, "", error)) as runner:
            ok, raw, timestamp = progress.patch_interactive("om_example", {}, force=True)
            self.assertFalse(ok)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(progress._api_code(raw), 230031)

    def test_shutdown_closes_owned_consumers(self):
        child = mock.Mock()
        child.poll.return_value = None
        with mock.patch.object(relay, "_children", {child}), \
                mock.patch.object(relay, "_shutdown") as shutdown:
            relay.stop_children()
            shutdown.set.assert_called_once()
            child.stdin.close.assert_called_once()
            child.terminate.assert_called_once()
            child.wait.assert_called_once_with(timeout=5)


class ArchiveTests(unittest.TestCase):
    def test_archive_only_pending_json_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pending, processed = root / "pending", root / "processed"
            pending.mkdir()
            outside = root / "unrelated.json"
            outside.write_text("{}")
            with mock.patch.object(archive_pending, "PENDING", pending), \
                    mock.patch.object(archive_pending, "PROCESSED", processed):
                with self.assertRaises(ValueError):
                    archive_pending.archive(outside)
                good = pending / "event.json"
                good.write_text("{}")
                self.assertEqual(archive_pending.archive(good), processed / "event.json")
                good.write_text("{}")
                with self.assertRaises(ValueError):
                    archive_pending.archive(good)
                link = pending / "link.json"
                link.symlink_to(outside)
                with self.assertRaises(ValueError):
                    archive_pending.archive(link)


if __name__ == "__main__":
    unittest.main()
