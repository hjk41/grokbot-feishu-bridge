#!/usr/bin/env python3
"""Unit + two-process contention tests for progress cards."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIR)

import progress  # noqa: E402
import relay  # noqa: E402


class FakeFeishu:
    def __init__(self):
        self.calls = []
        self.patch_log = []
        self.lock = threading.Lock()
        self._n = 0
        self.fail_reply = False
        self.patch_delay = 0
        self.patch_gate = None  # threading/multiprocessing Event pair

    def runner(self, argv, stdin_text=None):
        with self.lock:
            self.calls.append(list(argv))
            if "+messages-reply" in argv:
                if self.fail_reply:
                    return 1, "", "simulated reply fail"
                self._n += 1
                mid = "om_prog_%04d" % self._n
                return 0, json.dumps({"ok": True, "identity": "bot", "data": {"message_id": mid}}), ""
            if "messages" in argv and "patch" in argv:
                mid = None
                body = None
                for i, a in enumerate(argv):
                    if a == "--message-id":
                        mid = argv[i + 1]
                    if a == "--data":
                        body = argv[i + 1]
                # Optional gate for race tests (in-process)
                if self.patch_gate is not None:
                    entered, release = self.patch_gate
                    entered.set()
                    release.wait(5)
                if self.patch_delay:
                    time.sleep(self.patch_delay)
                self.patch_log.append({"message_id": mid, "data": body, "t": time.time()})
                return 0, json.dumps({"ok": True, "identity": "bot", "data": {}}), ""
            if "+messages-send" in argv:
                self._n += 1
                mid = "om_send_%04d" % self._n
                return 0, json.dumps({"ok": True, "data": {"message_id": mid}}), ""
            return 0, json.dumps({"ok": True}), ""


# ---------- two-process race helpers (fcntl held across PATCH) ----------

def _append_patch_timeline(path, entry):
    import fcntl
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _color_from_patch_data(body):
    """Extract header.template from patch --data JSON."""
    if not body:
        return None
    try:
        outer = json.loads(body)
        content = outer.get("content") or body
        if isinstance(content, str):
            card = json.loads(content)
        else:
            card = content
        return (card.get("header") or {}).get("template")
    except Exception:
        return None


def _race_child_done(tasks_dir, task_id, timeline_path, ready_q, result_q):
    """Process B: call done() — may block on flock until heartbeat finishes PATCH."""
    import progress as prog
    prog.TASKS_DIR = tasks_dir

    def runner(argv, stdin_text=None):
        if "messages" in argv and "patch" in argv:
            body = None
            for i, a in enumerate(argv):
                if a == "--data":
                    body = argv[i + 1]
            _append_patch_timeline(timeline_path, {
                "who": "done",
                "t": time.time(),
                "color": _color_from_patch_data(body),
            })
            return 0, json.dumps({"ok": True, "data": {}}), ""
        if "+messages-reply" in argv or "+messages-send" in argv:
            return 0, json.dumps({"ok": True, "data": {"message_id": "om_sum"}}), ""
        return 0, json.dumps({"ok": True}), ""

    prog.set_runner(runner)
    ready_q.put("b_ready")
    try:
        st = prog.done(task_id, summary_md=None, send_summary=False, final_action="完成")
        result_q.put({"ok": True, "status": st.get("status"), "seq": st.get("seq")})
    except Exception as e:
        result_q.put({"ok": False, "error": str(e)})


def _race_parent_heartbeat(tasks_dir, task_id, timeline_path, patch_delay, result_q):
    """Process A: heartbeat tick with slow PATCH; holds lock the whole time.
    Must NOT wait for child inside lock (that deadlocks)."""
    import progress as prog
    prog.TASKS_DIR = tasks_dir

    def runner(argv, stdin_text=None):
        if "messages" in argv and "patch" in argv:
            body = None
            for i, a in enumerate(argv):
                if a == "--data":
                    body = argv[i + 1]
            _append_patch_timeline(timeline_path, {
                "who": "heartbeat",
                "t": time.time(),
                "color": _color_from_patch_data(body),
                "phase": "patch_begin",
            })
            time.sleep(patch_delay)  # bounded delay; child blocks on flock
            _append_patch_timeline(timeline_path, {
                "who": "heartbeat",
                "t": time.time(),
                "color": _color_from_patch_data(body),
                "phase": "patch_end",
            })
            return 0, json.dumps({"ok": True, "data": {}}), ""
        return 0, json.dumps({"ok": True}), ""

    prog.set_runner(runner)
    try:
        prog._heartbeat_tick(task_id)
        st = prog.load_task(task_id)
        result_q.put({
            "ok": True,
            "status": st.get("status") if st else None,
            "seq": st.get("seq") if st else None,
        })
    except Exception as e:
        result_q.put({"ok": False, "error": str(e)})


def _seed_running_task(tasks_dir, task_id):
    import progress as prog
    prog.TASKS_DIR = tasks_dir

    def runner(argv, stdin_text=None):
        return 0, json.dumps({"ok": True, "data": {}}), ""

    prog.set_runner(runner)
    now = time.time()
    state = {
        "task_id": task_id,
        "message_id": "om_race_orig",
        "chat_id": "oc_race",
        "chat_type": "p2p",
        "request_text": "race",
        "status": "running",
        "current_action": "执行中",
        "detail": "running for race",
        "events": [{"label": "执行中", "done": False, "ts": now}],
        "created_at": now,
        "started_at": now,
        "last_stage_at": now,
        "progress_message_id": "om_prog_race",
        "queued_count": 0,
        "seq": 5,
        "last_patch_at": 0,
        "stop_requested": False,
    }
    with prog.task_guard(task_id):
        prog.save_task(state)
    return state


class TwoProcessRaceTests(unittest.TestCase):
    """HB holds lock through PATCH; done waits; last Feishu PATCH is green."""

    def test_heartbeat_lock_orders_done_green_last(self):
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            tasks = os.path.join(tmp, "tasks")
            os.makedirs(tasks)
            timeline_path = os.path.join(tmp, "patch_timeline.jsonl")
            open(timeline_path, "w").close()
            task_id = "race02deadbeef02"
            _seed_running_task(tasks, task_id)

            ready_b = ctx.Queue()
            result_a, result_b = ctx.Queue(), ctx.Queue()
            patch_delay = 0.4

            pb = ctx.Process(
                target=_race_child_done,
                args=(tasks, task_id, timeline_path, ready_b, result_b),
            )
            pa = ctx.Process(
                target=_race_parent_heartbeat,
                args=(tasks, task_id, timeline_path, patch_delay, result_a),
            )

            # Start child first so it is waiting/contending; then heartbeat holds lock.
            pb.start()
            self.assertEqual(ready_b.get(timeout=5), "b_ready")
            time.sleep(0.05)  # let child reach task_guard / done
            pa.start()

            ares = result_a.get(timeout=15)
            bres = result_b.get(timeout=15)
            pa.join(timeout=5)
            pb.join(timeout=5)

            self.assertTrue(ares.get("ok"), ares)
            self.assertTrue(bres.get("ok"), bres)
            self.assertEqual(bres.get("status"), "done")

            import progress as prog
            prog.TASKS_DIR = tasks
            final = prog.load_task(task_id)
            self.assertEqual(final.get("status"), "done")

            # External PATCH timeline: last color must be green
            with open(timeline_path) as timeline:
                lines = timeline.read().strip().splitlines()
            events = [json.loads(l) for l in lines if l.strip()]
            colored = [e for e in events if e.get("color")]
            self.assertTrue(colored, "no colored patch events: %s" % events)
            last = colored[-1]
            self.assertEqual(
                last.get("color"), "green",
                "last Feishu PATCH must be green, got %s timeline=%s" % (last, events),
            )
            self.assertEqual(last.get("who"), "done")
            # Heartbeat blue must not come after done green
            last_green_t = last["t"]
            for e in colored:
                if e.get("who") == "heartbeat" and e.get("color") == "blue":
                    self.assertLessEqual(
                        e["t"], last_green_t + 1e-6,
                        "stale blue heartbeat PATCH after green: %s" % events,
                    )


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tasks = os.path.join(self.tmp.name, "tasks")
        os.makedirs(self.tasks)
        progress.TASKS_DIR = self.tasks
        self.fake = FakeFeishu()
        progress.set_runner(self.fake.runner)
        progress._heartbeats.clear()

    def tearDown(self):
        for tid in list(progress._heartbeats.keys()):
            progress.stop_heartbeat(tid)
        progress.set_runner(None)
        self.tmp.cleanup()

    def test_task_id_is_stable_hash_16(self):
        a = progress.make_task_id("om_abcdefghijklmnopqrstuvwxyz")
        b = progress.make_task_id("om_abcdefghijklmnopqrstuvwxyz")
        c = progress.make_task_id("om_abcdefghijklmnopqrstuvwxyZ")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)
        self.assertNotEqual(a, c)

    def test_create_ack_and_group_thread(self):
        st = progress.create_ack(
            message_id="om_orig_1", chat_id="oc_1", chat_type="group",
            request_text="hello", task_id="t001aabbccddeeff",
            sender_open_id="ou_sender",
        )
        self.assertTrue(st["card_create_ok"])
        self.assertEqual(st["progress_message_id"], "om_prog_0001")
        reply = [c for c in self.fake.calls if "+messages-reply" in c][0]
        self.assertIn("--reply-in-thread", reply)
        progress.stop_heartbeat(st["task_id"])

    def test_duplicate_create_reuses_card(self):
        st1 = progress.create_ack(
            message_id="om_orig_2", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tdup01aabbccddee",
        )
        n = len([c for c in self.fake.calls if "+messages-reply" in c])
        st2 = progress.create_ack(
            message_id="om_orig_2", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tdup01aabbccddee",
        )
        self.assertEqual(n, len([c for c in self.fake.calls if "+messages-reply" in c]))
        self.assertEqual(st1["progress_message_id"], st2["progress_message_id"])
        progress.stop_heartbeat(st1["task_id"])

    def test_updates_same_message_id(self):
        st = progress.create_ack(
            message_id="om_orig_3", chat_id="oc_1", chat_type="p2p",
            request_text="task", task_id="tup01aabbccddeeff",
        )
        pmid = st["progress_message_id"]
        progress.start("tup01aabbccddeeff", action="执行中")
        progress.update("tup01aabbccddeeff", action="读取文件", detail="reading test.txt")
        for p in self.fake.patch_log:
            self.assertEqual(p["message_id"], pmid)
        self.assertEqual(progress.load_task("tup01aabbccddeeff")["current_action"], "读取文件")
        progress.stop_heartbeat("tup01aabbccddeeff")

    def test_done_refuses_after_fail(self):
        progress.create_ack(
            message_id="om_x", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tterm01aabbccdd",
        )
        progress.fail("tterm01aabbccdd", error="boom", send_error_card=False)
        st = progress.done("tterm01aabbccdd", send_summary=False)
        self.assertEqual(st["status"], "failed")

    def test_fail_refuses_after_done(self):
        progress.create_ack(
            message_id="om_y", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tterm02aabbccdd",
        )
        progress.done("tterm02aabbccdd", send_summary=False, final_action="完成")
        st = progress.fail("tterm02aabbccdd", error="nope", send_error_card=False)
        self.assertEqual(st["status"], "done")

    def test_update_ignored_when_terminal(self):
        progress.create_ack(
            message_id="om_z", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tterm03aabbccdd",
        )
        progress.done("tterm03aabbccdd", send_summary=False)
        st = progress.update("tterm03aabbccdd", action="should-not-apply")
        self.assertEqual(st["status"], "done")
        self.assertNotEqual(st.get("current_action"), "should-not-apply")

    def test_request_stop_does_not_fail(self):
        progress.create_ack(
            message_id="om_s", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tstop01aabbccdd",
            sender_open_id="ou_owner",
        )
        st = progress.request_stop("tstop01aabbccdd", operator_id="ou_owner")
        self.assertTrue(st.get("stop_requested"))
        self.assertEqual(st.get("status"), "ack")  # not failed
        # wrong operator — must deny, never set stop_requested
        st2 = progress.request_stop("tstop01aabbccdd", operator_id="ou_other")
        self.assertTrue(st2.get("stop_denied"))
        # first call already set stop_requested; wrong operator on already-requested is deny-only
        progress.stop_heartbeat("tstop01aabbccdd")

    def test_ux_copy_no_jargon(self):
        st = progress.create_ack(
            message_id="om_ux", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tux01aabbccddeeff",
        )
        progress.start("tux01aabbccddeeff")
        progress.stop_heartbeat("tux01aabbccddeeff")
        progress._heartbeat_tick("tux01aabbccddeeff")
        jargon = ("未发明", "心跳", "Webhook", "webhook", "例程")
        for p in self.fake.patch_log:
            blob = p.get("data") or ""
            for j in jargon:
                self.assertNotIn(j, blob, "jargon %r found in card" % j)
        # detail should mention 已用时 / 上次更新
        self.assertTrue(any("已用时" in (p.get("data") or "") for p in self.fake.patch_log))

    def test_group_followup_uses_thread_reply(self):
        progress.create_ack(
            message_id="om_group_orig", chat_id="oc_g", chat_type="group",
            request_text="g", task_id="tgrp01aabbccddee",
        )
        progress.done("tgrp01aabbccddee", summary_md="总结", send_summary=True, final_action="完成")
        # follow-up should be reply-in-thread, not bare send to chat
        replies = [c for c in self.fake.calls if "+messages-reply" in c and "--reply-in-thread" in c]
        self.assertGreaterEqual(len(replies), 2)  # ack + summary

    def test_card_api_fail_still_returns_state(self):
        self.fake.fail_reply = True
        st = progress.create_ack(
            message_id="om_orig_7", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tfailcardaabbcc",
        )
        self.assertFalse(st["card_create_ok"])
        self.assertIsNone(st["progress_message_id"])



    def test_mark_webhook_does_not_demote_running(self):
        progress.create_ack(
            message_id="om_mw", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tmw01aabbccddeeff",
        )
        # Mark delivered/executable + bypass head check for this unit test
        with progress.task_guard("tmw01aabbccddeeff"):
            st0 = progress.load_task("tmw01aabbccddeeff")
            st0["webhook_ok"] = True
            st0["delivered_at"] = __import__("time").time()
            st0["executable"] = True
            st0["status"] = "queued"
            st0["bypass_fifo"] = True
            progress.save_task(st0)
        progress.start("tmw01aabbccddeeff", action="执行中", detail="real work")
        progress.stop_heartbeat("tmw01aabbccddeeff")
        st = progress.mark_webhook_result("tmw01aabbccddeeff", ok=True)
        self.assertEqual(st.get("status"), "running")
        self.assertEqual(st.get("current_action"), "执行中")
        self.assertTrue(st.get("webhook_ok"))

    def test_mark_webhook_ack_to_queued(self):
        progress.create_ack(
            message_id="om_mw2", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tmw02aabbccddeeff",
        )
        progress.stop_heartbeat("tmw02aabbccddeeff")
        st = progress.mark_webhook_result("tmw02aabbccddeeff", ok=True)
        self.assertEqual(st.get("status"), "queued")
        self.assertEqual(st.get("current_action"), "已入队")

    def test_request_stop_missing_operator_denied(self):
        progress.create_ack(
            message_id="om_s2", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tstop02aabbccdd",
            sender_open_id="ou_owner",
        )
        st = progress.request_stop("tstop02aabbccdd", operator_id=None)
        self.assertTrue(st.get("stop_denied"))
        self.assertFalse(st.get("stop_requested"))
        self.assertFalse(st.get("stop_accepted"))
        progress.stop_heartbeat("tstop02aabbccdd")



class RelayOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tasks = os.path.join(self.tmp.name, "tasks")
        self.pending = os.path.join(self.tmp.name, "pending")
        os.makedirs(self.tasks)
        os.makedirs(self.pending)
        progress.TASKS_DIR = self.tasks
        relay.PENDING_DIR = self.pending
        relay.EVLOG = os.path.join(self.tmp.name, "events.log")
        relay.SEEN_FILE = os.path.join(self.tmp.name, "seen_ids.txt")
        relay.seen.clear()
        auth = mock.patch.dict(os.environ, {"FEISHU_ALLOWED_USERS": "ou_test,ou_owner", "FEISHU_ALLOWED_CHATS": "oc_1,oc_example,oc_g,oc_race,oc_x"})
        auth.start()
        self.addCleanup(auth.stop)
        self.fake = FakeFeishu()
        progress.set_runner(self.fake.runner)
        progress._heartbeats.clear()
        import chat_queue
        chat_queue.QUEUES_DIR = os.path.join(self.tmp.name, "queues")
        chat_queue.PENDING_DIR = self.pending
        chat_queue.ADVANCE_DIR = os.path.join(chat_queue.QUEUES_DIR, "advance")
        os.makedirs(chat_queue.ADVANCE_DIR, exist_ok=True)
        chat_queue.set_post_hook(lambda summary: (True, None))

    def tearDown(self):
        for tid in list(progress._heartbeats.keys()):
            progress.stop_heartbeat(tid)
        progress.set_runner(None)
        import chat_queue
        chat_queue.set_post_hook(None)
        self.tmp.cleanup()

    def test_card_then_webhook_order(self):
        order = []
        real_create = progress.create_ack
        import chat_queue

        def wrap_create(**kw):
            order.append("card")
            return real_create(**kw)

        real_dispatch = chat_queue.dispatch_task

        def wrap_dispatch(*a, **kw):
            order.append("webhook")
            return real_dispatch(*a, **kw)

        ev = {
            "message_id": "om_live_1_full_id_abc",
            "event_id": "evt_live_1",
            "chat_id": "oc_example",
            "chat_type": "p2p",
            "sender_id": "ou_test",
            "sender_type": "user",
            "message_type": "text",
            "content": "unit-test-msg",
            "create_time": "1",
            "mentions": [],
        }
        # Isolate queue dirs for this test
        chat_queue.QUEUES_DIR = os.path.join(self.tmp.name, "queues")
        chat_queue.PENDING_DIR = self.pending
        chat_queue.ADVANCE_DIR = os.path.join(chat_queue.QUEUES_DIR, "advance")
        os.makedirs(chat_queue.ADVANCE_DIR, exist_ok=True)
        chat_queue.set_post_hook(lambda s: (True, None))
        with mock.patch.object(progress, "create_ack", side_effect=wrap_create):
            with mock.patch.object(chat_queue, "dispatch_task", side_effect=wrap_dispatch):
                with mock.patch.object(relay, "react", lambda mid: None):
                    relay.on_message(ev)
        chat_queue.set_post_hook(None)
        self.assertEqual(order, ["card", "webhook"])
        tid = progress.make_task_id("om_live_1_full_id_abc")
        st = progress.load_task(tid)
        self.assertEqual(len(st["task_id"]), 16)
        self.assertIn("progress_message_id", st)

    def test_duplicate_event_no_second_card(self):
        ev = {
            "message_id": "om_dup_1",
            "event_id": "evt_dup_1",
            "chat_id": "oc_x",
            "chat_type": "p2p",
            "sender_id": "ou_test",
            "sender_type": "user",
            "message_type": "text",
            "content": "dup",
            "create_time": "1",
            "mentions": [],
        }
        with mock.patch.object(relay, "deliver", return_value=True):
            with mock.patch.object(relay, "react", lambda mid: None):
                relay.on_message(ev)
                relay.on_message(ev)
        self.assertEqual(len([c for c in self.fake.calls if "+messages-reply" in c]), 1)
        for tid in list(progress._heartbeats.keys()):
            progress.stop_heartbeat(tid)

    def test_on_card_rejects_unauthorized_stop(self):
        progress.create_ack(
            message_id="om_cardstop", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tcardstop01aabb",
            sender_open_id="ou_owner",
        )
        progress.stop_heartbeat("tcardstop01aabb")
        delivered = []
        def capture(summary):
            delivered.append(summary)
            return True
        ev = {
            "event_id": "evt_stop_deny",
            "message_id": "om_card",
            "chat_id": "oc_1",
            "operator_id": "ou_intruder",
            "action_value": {"action": "request_stop", "task_id": "tcardstop01aabb"},
        }
        with mock.patch.object(relay, "deliver", side_effect=capture):
            relay.on_card(ev)
        # Unauthorized stop must not reach routine (no deliver / no pending)
        self.assertEqual(len(delivered), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
