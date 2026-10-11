#!/usr/bin/env python3
import json, os, sys, tempfile, unittest
from unittest import mock

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIR)
import relay as relay_mod
import progress


class HistoryIngestTests(unittest.TestCase):
    def test_event_from_history_message_maps_fields(self):
        msg = {
            "message_id": "om_hist_test_001",
            "chat_id": "oc_abc",
            "content": "hello model?",
            "create_time": "2026-10-11 11:17",
            "msg_type": "text",
            "sender": {"id": "ou_user", "sender_type": "user"},
        }
        ev = relay_mod.event_from_history_message(msg, chat_type="p2p")
        self.assertEqual(ev["message_id"], "om_hist_test_001")
        self.assertEqual(ev["event_id"], "hist:om_hist_test_001")
        self.assertEqual(ev["content"], "hello model?")
        self.assertEqual(ev["sender_id"], "ou_user")
        self.assertEqual(ev["sender_type"], "user")
        self.assertEqual(ev["ingested_via"], "history_backfill")
        self.assertTrue(str(ev["create_time"]).isdigit())

    def test_ingest_skips_existing_task(self):
        mid = "om_hist_skip_existing"
        tid = progress.make_task_id(mid)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        old = progress.TASKS_DIR
        progress.TASKS_DIR = td.name
        try:
            progress.save_task({"task_id": tid, "message_id": mid, "status": "done",
                                "progress_message_id": "om_prog_x"})
            out = relay_mod.ingest_history_message_id(mid)
            self.assertTrue(out["ok"])
            self.assertTrue(out["skipped"])
            self.assertEqual(out["reason"], "task_exists")
        finally:
            progress.TASKS_DIR = old

    def test_cards_still_omit_model_footer(self):
        card = progress.build_progress_card(
            task_id="abcdefghijklmnop", request_text="t", status="running",
            elapsed_sec=1, model_id="my-cfg", model_kind="config", model_source="explicit",
        )
        blob = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("model_footer", blob)
        self.assertNotIn("模型：", blob)
        self.assertNotIn("配置模型：", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
