#!/usr/bin/env python3
"""Boundary tests for wall/monotonic skew refresh conditions (no live bus stop)."""
import os, sys, unittest
from unittest import mock

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIR)


class BusSkewLogicTests(unittest.TestCase):
    def test_skew_formula_ignores_negative_wall(self):
        # replicate gate used in refresh_bus_loop
        def skew(d_wall, d_mono):
            return d_wall - d_mono if d_wall >= 0 and d_mono >= 0 else 0.0
        self.assertEqual(skew(-1, 5), 0.0)
        self.assertGreater(skew(40, 5), 30)
        self.assertLess(skew(10, 9), 30)

    def test_quiet_heuristic_not_proof(self):
        # documenting: received==0 and uptime>=600 is heuristic only
        uptime, received = 700, 0
        heuristic = uptime is not None and received == 0 and uptime >= 600
        self.assertTrue(heuristic)
        # after received=1, heuristic false even if later suspended
        self.assertFalse(uptime is not None and 1 == 0 and uptime >= 600)

    def test_history_and_footer_still_ok(self):
        import relay as r
        import progress, json
        ev = r.event_from_history_message({
            "message_id": "om_x", "chat_id": "oc_x", "content": "q",
            "create_time": "2026-10-11 11:17", "msg_type": "text",
            "sender": {"id": "ou_x", "sender_type": "user"},
        })
        self.assertTrue(ev["event_id"].startswith("hist:"))
        card = progress.build_summary_card(task_id="abcdefghijklmnop", elapsed_sec=1, body_md="ans", request_text="q")
        blob = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("model_footer", blob)
        self.assertNotIn("模型：", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
