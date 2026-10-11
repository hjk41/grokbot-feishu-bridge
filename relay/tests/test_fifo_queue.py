#!/usr/bin/env python3
"""FIFO queue, claim races, CLI advance, fail-closed start/update tests."""
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
import chat_queue  # noqa: E402
import relay  # noqa: E402


class FakeFeishu:
    def __init__(self):
        self.calls = []
        self.patch_log = []
        self.lock = threading.Lock()
        self._n = 0
        self.fail_reply = False
        self.reply_delay = 0

    def runner(self, argv, stdin_text=None):
        with self.lock:
            self.calls.append(list(argv))
            if "+messages-reply" in argv:
                if self.reply_delay:
                    time.sleep(self.reply_delay)
                if self.fail_reply:
                    return 1, "", "simulated reply fail"
                self._n += 1
                mid = "om_prog_%04d" % self._n
                return 0, json.dumps({"ok": True, "data": {"message_id": mid}}), ""
            if "messages" in argv and "patch" in argv:
                mid = None
                body = None
                for i, a in enumerate(argv):
                    if a == "--message-id":
                        mid = argv[i + 1]
                    if a == "--data":
                        body = argv[i + 1]
                self.patch_log.append({"message_id": mid, "data": body, "t": time.time()})
                return 0, json.dumps({"ok": True, "data": {}}), ""
            if "+messages-send" in argv:
                self._n += 1
                return 0, json.dumps({"ok": True, "data": {"message_id": "om_send_%04d" % self._n}}), ""
            return 0, json.dumps({"ok": True}), ""


class FifoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tasks = os.path.join(self.tmp.name, "tasks")
        self.pending = os.path.join(self.tmp.name, "pending")
        self.queues = os.path.join(self.tmp.name, "queues")
        os.makedirs(self.tasks)
        os.makedirs(self.pending)
        os.makedirs(self.queues)
        progress.TASKS_DIR = self.tasks
        chat_queue.QUEUES_DIR = self.queues
        chat_queue.PENDING_DIR = self.pending
        chat_queue.ADVANCE_DIR = os.path.join(self.queues, "advance")
        os.makedirs(chat_queue.ADVANCE_DIR, exist_ok=True)
        relay.PENDING_DIR = self.pending
        relay.EVLOG = os.path.join(self.tmp.name, "events.log")
        relay.SEEN_FILE = os.path.join(self.tmp.name, "seen_ids.txt")
        relay.seen.clear()
        auth = mock.patch.dict(os.environ, {"FEISHU_ALLOWED_USERS": "ou_test,ou_owner", "FEISHU_ALLOWED_CHATS": "oc_1,oc_ab,oc_chat1,oc_chat2,oc_cli,oc_d,oc_fail_del,oc_fifo_1,oc_fifo_2,oc_longwait,oc_mp,oc_ord,oc_race_claim,oc_scan_race,oc_wait"})
        auth.start()
        self.addCleanup(auth.stop)
        self.fake = FakeFeishu()
        progress.set_runner(self.fake.runner)
        progress._heartbeats.clear()
        self.posts = []
        self.post_lock = threading.Lock()

        def hook(summary):
            with self.post_lock:
                self.posts.append(dict(summary))
            return True, None

        chat_queue.set_post_hook(hook)

    def tearDown(self):
        for tid in list(progress._heartbeats.keys()):
            progress.stop_heartbeat(tid)
        progress.set_runner(None)
        chat_queue.set_post_hook(None)
        self.tmp.cleanup()

    def _seed(self, tid, chat_id, text, status="ack", **extra):
        now = time.time()
        st = {
            "task_id": tid, "message_id": "om_" + tid, "chat_id": chat_id,
            "chat_type": "p2p", "request_text": text, "status": status,
            "created_at": now, "started_at": now, "last_stage_at": now,
            "progress_message_id": "om_prog_" + tid[:6],
            "queued_count": 0, "executable": False, "seq": 1,
            "sender_open_id": "ou_owner", "stop_requested": False,
            "events": [{"label": "已收到", "done": False, "ts": now}],
        }
        st.update(extra)
        with progress.task_guard(tid):
            progress.save_task(st)
        return st


class FifoDispatchTests(FifoBase):
    def test_abc_only_a_dispatched(self):
        chat = "oc_fifo_1"
        for tid, label in [("taaaaaaaaaaaaaaa", "A"), ("tbbbbbbbbbbbbbbb", "B"), ("tcccccccccccccccc", "C")]:
            self._seed(tid, chat, label)
            progress.create_ack = progress.create_ack  # noqa — use real via seed
        # Use real create via seed already; enqueue + dispatch A only
        chat_queue.enqueue(chat, "taaaaaaaaaaaaaaa")
        chat_queue.enqueue(chat, "tbbbbbbbbbbbbbbb")
        chat_queue.enqueue(chat, "tcccccccccccccccc")
        chat_queue.mark_waiting("tbbbbbbbbbbbbbbb", 1)
        chat_queue.mark_waiting("tcccccccccccccccc", 2)
        out = chat_queue.dispatch_task("taaaaaaaaaaaaaaa", chat_id=chat)
        self.assertEqual(out, "posted")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["task_id"], "taaaaaaaaaaaaaaa")
        # B/C not in pending
        pend = [f for f in os.listdir(self.pending) if f.endswith(".json")]
        self.assertEqual(len(pend), 1)
        st_b = progress.load_task("tbbbbbbbbbbbbbbb")
        self.assertEqual(st_b["status"], "waiting")
        # list_pending style: waiting not present
        self.assertTrue(all("tbbbbbbbbbbbbbbb" not in f for f in pend))

    def test_terminal_advances_via_scan(self):
        chat = "oc_fifo_2"
        self._seed("taaaaaaaaaaaaaaa", chat, "A")
        self._seed("tbbbbbbbbbbbbbbb", chat, "B")
        chat_queue.enqueue(chat, "taaaaaaaaaaaaaaa")
        chat_queue.enqueue(chat, "tbbbbbbbbbbbbbbb")
        chat_queue.mark_waiting("tbbbbbbbbbbbbbbb", 1)
        self.assertEqual(chat_queue.dispatch_task("taaaaaaaaaaaaaaa", chat_id=chat), "posted")
        self.posts.clear()
        # Simulate CLI done: notify only
        with progress.task_guard("taaaaaaaaaaaaaaa"):
            st = progress.load_task("taaaaaaaaaaaaaaa")
            st["status"] = "done"
            st["finished_at"] = time.time()
            progress.save_task(st)
        chat_queue.notify_terminal(chat, "taaaaaaaaaaaaaaa")
        self.assertEqual(len(self.posts), 0)  # CLI did not POST
        # Relay scan advances
        stats = chat_queue.scan_all_queues()
        self.assertGreaterEqual(stats["notices"] + stats["scanned"], 1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["task_id"], "tbbbbbbbbbbbbbbb")
        st_b = progress.load_task("tbbbbbbbbbbbbbbb")
        self.assertIn(st_b["status"], ("queued", "dispatching"))
        self.assertTrue(st_b.get("webhook_ok") or st_b.get("delivered_at"))

    def test_double_claim_one_post(self):
        chat = "oc_race_claim"
        self._seed("theaddeadbeef0001", chat, "H")
        chat_queue.enqueue(chat, "theaddeadbeef0001")
        results = []

        def worker():
            results.append(chat_queue.dispatch_task("theaddeadbeef0001", chat_id=chat))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(sorted(results).count("posted"), 1)
        self.assertIn("skipped", results)

    def test_waiting_start_and_update_refused(self):
        chat = "oc_wait"
        self._seed("twait01aabbccddee", chat, "W", status="waiting")
        chat_queue.enqueue(chat, "tactive01aabbccdd")
        self._seed("tactive01aabbccdd", chat, "A", status="queued",
                   webhook_ok=True, delivered_at=time.time(), executable=True)
        chat_queue.enqueue(chat, "twait01aabbccddee")
        st = progress.start("twait01aabbccddee")
        self.assertTrue(st.get("start_refused"))
        self.assertEqual(st.get("status"), "waiting")
        st2 = progress.update("twait01aabbccddee", action="sneak", status="running")
        self.assertTrue(st2.get("update_refused"))
        self.assertEqual(st2.get("status"), "waiting")

    def test_failed_delivery_scrubs_pending(self):
        chat = "oc_fail_del"
        self._seed("tfaildel01aabbcc", chat, "F")
        chat_queue.enqueue(chat, "tfaildel01aabbcc")

        def fail_hook(summary):
            self.posts.append(summary)
            return False, "HTTP 500"

        chat_queue.set_post_hook(fail_hook)
        out = chat_queue.dispatch_task("tfaildel01aabbcc", chat_id=chat)
        self.assertEqual(out, "failed")
        pend = [f for f in os.listdir(self.pending) if f.endswith(".json")]
        self.assertEqual(pend, [])
        st = progress.load_task("tfaildel01aabbcc")
        self.assertEqual(st.get("status"), "failed")

    def test_queue_wait_no_false_timeout(self):
        chat = "oc_longwait"
        self._seed("tlongwait01aabbcc", chat, "L", status="waiting")
        st = progress.load_task("tlongwait01aabbcc")
        st["created_at"] = time.time() - 9999
        with progress.task_guard("tlongwait01aabbcc"):
            progress.save_task(st)
        reason = None
        with progress.task_guard("tlongwait01aabbcc"):
            st = progress.load_task("tlongwait01aabbcc")
            reason = progress._maybe_timeout_unlocked(st)
        self.assertIsNone(reason)

    def test_escape_quote_no_at_injection(self):
        raw = 'hello <at id="ou_x">@Bob</at> & <link url="http://evil">x</link>'
        esc = progress.escape_lark_md(raw)
        self.assertNotIn("<at", esc.lower().replace("&lt;at", ""))  # tags stripped then escaped
        self.assertNotIn("ou_x", esc)  # at tag removed
        self.assertIn("&amp;", esc)
        card = progress.build_progress_card(
            task_id="tescape01aabbccdd", request_text=raw, status="waiting",
            elapsed_sec=1, queued_count=1,
        )
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("对应消息", blob)
        self.assertNotIn("<at id=", blob)

    def test_unauthorized_stop_not_delivered(self):
        progress.create_ack(
            message_id="om_stop_u", chat_id="oc_1", chat_type="p2p",
            request_text="x", task_id="tstopu01aabbccdd", sender_open_id="ou_owner",
        )
        progress.stop_heartbeat("tstopu01aabbccdd")
        self.posts.clear()
        # clear pending from any prior
        for f in os.listdir(self.pending):
            os.remove(os.path.join(self.pending, f))
        ev = {
            "event_id": "evt_stop_deny2",
            "message_id": "om_card",
            "chat_id": "oc_1",
            "operator_id": "ou_intruder",
            "action_value": {"action": "request_stop", "task_id": "tstopu01aabbccdd"},
        }
        relay.on_card(ev)
        self.assertEqual(len(self.posts), 0)
        pend = [f for f in os.listdir(self.pending) if f.endswith(".json")]
        self.assertEqual(pend, [])

    def test_bypass_slash_detected(self):
        self.assertTrue(chat_queue.is_bypass_text("/stop"))
        self.assertTrue(chat_queue.is_bypass_text("/status now"))
        self.assertTrue(chat_queue.is_bypass_text("/help"))
        self.assertFalse(chat_queue.is_bypass_text("stop please"))
        self.assertFalse(chat_queue.is_bypass_text("/new"))

    def test_mark_waiting_does_not_demote_claimed(self):
        self._seed("tclaim01aabbccdd", "oc_d", "X", status="dispatching",
                   dispatch_claimed_at=time.time(), executable=True)
        st = chat_queue.mark_waiting("tclaim01aabbccdd", 3)
        self.assertEqual(st["status"], "dispatching")
        self.assertTrue(st.get("executable"))

    def test_scan_between_create_and_enqueue_keeps_file(self):
        """Task file exists before enqueue; prune must not drop."""
        chat = "oc_scan_race"
        tid = "tscanrace01aabbcc"
        # create_ack first (as relay now does)
        progress.create_ack(
            message_id="om_scan", chat_id=chat, chat_type="p2p",
            request_text="scan", task_id=tid,
        )
        progress.stop_heartbeat(tid)
        self.assertTrue(os.path.isfile(progress.task_path(tid)))
        # Simulate concurrent scan prune on empty queue then enqueue
        chat_queue.scan_all_queues()
        ahead, order = chat_queue.enqueue(chat, tid)
        self.assertEqual(ahead, 0)
        self.assertIn(tid, order)
        # prune keeps it
        with progress.chat_guard(chat):
            q = chat_queue.load_queue(chat)
            kept = chat_queue._prune_order_unlocked(chat, q["order"])
        self.assertIn(tid, kept)

    def test_parallel_chats_independent(self):
        self._seed("tch1a01aabbccddee", "oc_chat1", "c1")
        self._seed("tch2a01aabbccddee", "oc_chat2", "c2")
        chat_queue.enqueue("oc_chat1", "tch1a01aabbccddee")
        chat_queue.enqueue("oc_chat2", "tch2a01aabbccddee")
        chat_queue.dispatch_task("tch1a01aabbccddee", chat_id="oc_chat1")
        chat_queue.dispatch_task("tch2a01aabbccddee", chat_id="oc_chat2")
        self.assertEqual(len(self.posts), 2)
        ids = {p["task_id"] for p in self.posts}
        self.assertEqual(ids, {"tch1a01aabbccddee", "tch2a01aabbccddee"})


def _mp_claim_worker(tasks_dir, queues_dir, pending_dir, chat, tid, counter_path, result_q):
    import progress as prog
    import chat_queue as fifo
    prog.TASKS_DIR = tasks_dir
    fifo.QUEUES_DIR = queues_dir
    fifo.PENDING_DIR = pending_dir
    fifo.ADVANCE_DIR = os.path.join(queues_dir, "advance")

    def hook(summary):
        # durable counter
        import fcntl
        with open(counter_path, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0)
            try:
                n = int(f.read().strip() or "0")
            except ValueError:
                n = 0
            n += 1
            f.seek(0); f.truncate(); f.write(str(n)); f.flush()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        time.sleep(0.15)  # hold "network" so peer contends
        return True, None

    fifo.set_post_hook(hook)
    prog.set_runner(lambda argv, stdin_text=None: (0, json.dumps({"ok": True, "data": {}}), ""))
    try:
        out = fifo.dispatch_task(tid, chat_id=chat)
        result_q.put(out)
    except Exception as e:
        result_q.put("err:" + str(e))


class TwoProcessClaimTests(unittest.TestCase):
    def test_two_processes_one_post_no_deadlock(self):
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            tasks = os.path.join(tmp, "tasks")
            queues = os.path.join(tmp, "queues")
            pending = os.path.join(tmp, "pending")
            os.makedirs(tasks); os.makedirs(queues); os.makedirs(pending)
            os.makedirs(os.path.join(queues, "advance"))
            progress.TASKS_DIR = tasks
            chat_queue.QUEUES_DIR = queues
            chat_queue.PENDING_DIR = pending
            chat_queue.ADVANCE_DIR = os.path.join(queues, "advance")
            progress.set_runner(lambda argv, stdin_text=None: (0, json.dumps({"ok": True, "data": {}}), ""))
            chat = "oc_mp"
            tid = "tmpclaim01aabbccdd"
            now = time.time()
            with progress.task_guard(tid):
                progress.save_task({
                    "task_id": tid, "message_id": "om_mp", "chat_id": chat,
                    "chat_type": "p2p", "request_text": "mp", "status": "ack",
                    "created_at": now, "started_at": now, "last_stage_at": now,
                    "progress_message_id": "om_p", "queued_count": 0,
                    "executable": False, "seq": 1, "stop_requested": False,
                    "events": [],
                })
            chat_queue.enqueue(chat, tid)
            counter = os.path.join(tmp, "post_count.txt")
            open(counter, "w").write("0")
            q1, q2 = ctx.Queue(), ctx.Queue()
            p1 = ctx.Process(target=_mp_claim_worker,
                             args=(tasks, queues, pending, chat, tid, counter, q1))
            p2 = ctx.Process(target=_mp_claim_worker,
                             args=(tasks, queues, pending, chat, tid, counter, q2))
            p1.start(); p2.start()
            r1 = q1.get(timeout=30)
            r2 = q2.get(timeout=30)
            p1.join(10); p2.join(10)
            self.assertFalse(p1.is_alive()); self.assertFalse(p2.is_alive())
            outcomes = sorted([r1, r2])
            self.assertEqual(outcomes.count("posted"), 1, outcomes)
            self.assertIn("skipped", outcomes)
            with open(counter) as f:
                n = int(f.read().strip())
            self.assertEqual(n, 1)


class CliDoneAdvanceTests(FifoBase):
    def test_cli_done_subprocess_then_scan_delivers_next(self):
        """Simulate CLI process: done writes notice; scan posts next (verify not mock-only)."""
        chat = "oc_cli"
        self._seed("tclia01aabbccddee", chat, "A", status="running",
                   webhook_ok=True, delivered_at=time.time(), executable=True)
        self._seed("tclib01aabbccddee", chat, "B", status="waiting")
        chat_queue.enqueue(chat, "tclia01aabbccddee")
        chat_queue.enqueue(chat, "tclib01aabbccddee")
        self.posts.clear()
        # Real done() path (schedules notify_terminal, no POST)
        st = progress.done("tclia01aabbccddee", summary_md=None, send_summary=False, final_action="完成")
        self.assertEqual(st["status"], "done")
        self.assertEqual(len(self.posts), 0)
        notice = os.path.join(chat_queue.ADVANCE_DIR, chat_queue.chat_hash(chat) + ".json")
        self.assertTrue(os.path.isfile(notice), "advance notice missing")
        chat_queue.scan_all_queues()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["task_id"], "tclib01aabbccddee")


class OnMessageOrderTests(FifoBase):
    def test_on_message_create_before_enqueue_survives_scan(self):
        # Patch create_ack to insert a scan mid-flight before returning
        real_create = progress.create_ack
        scanned = []

        def wrap_create(**kw):
            st = real_create(**kw)
            # task file exists; enqueue not yet — scan should be safe
            chat_queue.scan_all_queues()
            scanned.append(True)
            return st

        ev = {
            "message_id": "om_ord_1_full",
            "event_id": "evt_ord_1",
            "chat_id": "oc_ord",
            "chat_type": "p2p",
            "sender_id": "ou_test",
            "sender_type": "user",
            "message_type": "text",
            "content": "order-test",
            "create_time": "1",
            "mentions": [],
        }
        with mock.patch.object(progress, "create_ack", side_effect=wrap_create):
            with mock.patch.object(relay, "react", lambda mid: None):
                relay.on_message(ev)
        self.assertTrue(scanned)
        tid = progress.make_task_id("om_ord_1_full")
        st = progress.load_task(tid)
        self.assertIsNotNone(st)
        q = chat_queue.load_queue("oc_ord")
        self.assertIn(tid, q.get("order") or [])
        self.assertEqual(len(self.posts), 1)
        progress.stop_heartbeat(tid)

    def test_a_finishes_while_b_creating_card(self):
        chat = "oc_ab"
        # A already running head
        self._seed("taa01aabbccddeeff", chat, "A", status="running",
                   webhook_ok=True, delivered_at=time.time(), executable=True)
        chat_queue.enqueue(chat, "taa01aabbccddeeff")
        self.posts.clear()
        real_create = progress.create_ack
        delay = {"n": 0}

        def slow_create(**kw):
            delay["n"] += 1
            # While B's card is "creating", A finishes and scan advances
            if kw.get("task_id"):
                time.sleep(0.05)
            st = real_create(**kw)
            return st

        # Finish A during B path: use thread
        def finish_a():
            time.sleep(0.02)
            with progress.task_guard("taa01aabbccddeeff"):
                st = progress.load_task("taa01aabbccddeeff")
                st["status"] = "done"
                progress.save_task(st)
            chat_queue.notify_terminal(chat, "taa01aabbccddeeff")
            chat_queue.scan_all_queues()

        th = threading.Thread(target=finish_a)
        ev = {
            "message_id": "om_b_full_id_xyz",
            "event_id": "evt_b_1",
            "chat_id": chat,
            "chat_type": "p2p",
            "sender_id": "ou_test",
            "sender_type": "user",
            "message_type": "text",
            "content": "B-task",
            "create_time": "1",
            "mentions": [],
        }
        with mock.patch.object(progress, "create_ack", side_effect=slow_create):
            with mock.patch.object(relay, "react", lambda mid: None):
                th.start()
                relay.on_message(ev)
                th.join(5)
        tid_b = progress.make_task_id("om_b_full_id_xyz")
        st_b = progress.load_task(tid_b)
        self.assertIsNotNone(st_b)
        # B must not be dropped from queue
        q = chat_queue.load_queue(chat)
        # A terminal pruned; B should be present or already dispatched
        self.assertTrue(
            tid_b in (q.get("order") or [])
            or st_b.get("webhook_ok")
            or st_b.get("dispatch_claimed_at")
            or st_b.get("status") in ("queued", "dispatching", "running", "waiting"),
            st_b,
        )
        # At most one POST for B (A was already delivered)
        b_posts = [p for p in self.posts if p.get("task_id") == tid_b]
        self.assertLessEqual(len(b_posts), 1)
        # If demoted wrongly to waiting after claim — fail
        if st_b.get("dispatch_claimed_at") or st_b.get("webhook_ok"):
            self.assertNotEqual(st_b.get("status"), "waiting")
        progress.stop_heartbeat(tid_b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
