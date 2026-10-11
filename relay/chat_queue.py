#!/usr/bin/env python3
"""Per-chat durable FIFO (one active/executable slot).

Architecture
------------
- Waiting tasks live in tasks/ + queues/<chat>.json but are NEVER in pending/
  and never get a webhook until they become the chat head and are claimed.
- Relay alone owns network dispatch (pending write + webhook POST).
- CLI progress.done/fail only marks terminal + writes a durable advance notice;
  relay's periodic scan (and immediate notify) advances the queue.
- Cross-process atomic claim: under chat→task locks, verify head, persist
  dispatch_claimed_at / status=dispatching, release locks, THEN network.
  A second process seeing the claim must NOT POST again.
- HTTP is NOT exactly-once: claim prevents concurrent double-POST; after an
  uncertain network/restart, we trust durable claim/delivered flags and do
  not re-fire already-claimed or webhook_ok tasks (at-most-once preference).

Lock order (deadlock avoidance): always chat_guard → task_guard when both needed.
Never hold task_guard across fail/done that re-acquires chat lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

DIR = os.path.dirname(os.path.abspath(__file__))
QUEUES_DIR = os.path.join(DIR, "queues")
PENDING_DIR = os.path.join(DIR, "pending")
ADVANCE_DIR = os.path.join(QUEUES_DIR, "advance")

_progress = None

# Optional injectable POST for tests (counts real process posts)
_post_hook = None  # Callable[[dict], Tuple[bool, Optional[str]]]


def _prog():
    global _progress
    if _progress is None:
        import progress as _progress  # noqa: WPS433
    return _progress


def set_post_hook(fn) -> None:
    global _post_hook
    _post_hook = fn


def log(*a: Any) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[queue]", *a, flush=True)


def chat_hash(chat_id: str) -> str:
    return hashlib.sha256(str(chat_id or "unknown").encode()).hexdigest()[:16]


def queue_path(chat_id: str) -> str:
    return os.path.join(QUEUES_DIR, chat_hash(chat_id) + ".json")


def advance_path(chat_id: str) -> str:
    os.makedirs(ADVANCE_DIR, exist_ok=True)
    return os.path.join(ADVANCE_DIR, chat_hash(chat_id) + ".json")


def load_queue(chat_id: str) -> dict:
    path = queue_path(chat_id)
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("chat_id", chat_id)
        data.setdefault("order", [])
        return data
    except FileNotFoundError:
        return {"chat_id": chat_id, "order": [], "updated_at": 0}
    except Exception as e:
        log("load_queue error", _prog().redact(str(e)))
        return {"chat_id": chat_id, "order": [], "updated_at": 0}


def save_queue(chat_id: str, data: dict) -> str:
    """Caller MUST hold chat_guard(chat_id)."""
    os.makedirs(QUEUES_DIR, exist_ok=True)
    data = dict(data)
    data["chat_id"] = chat_id
    data["updated_at"] = time.time()
    path = queue_path(chat_id)
    _prog().atomic_write_json(path, data)
    return path


def _prune_order_unlocked(chat_id: str, order: List[str]) -> List[str]:
    """Drop missing/terminal task ids. Caller holds chat_guard.

    If tasks/<id>.json exists but is briefly unreadable, KEEP the id (do not
    orphan an in-flight create_ack). Only drop when the file is absent.
    """
    prog = _prog()
    kept: List[str] = []
    for tid in order:
        path = prog.task_path(tid)
        st = prog.load_task(tid)
        if not st:
            if os.path.isfile(path):
                # Mid-write / transient — keep reservation
                kept.append(tid)
            continue
        if prog.is_terminal(st.get("status")):
            continue
        kept.append(tid)
    return kept


def write_advance_notice(chat_id: str, finished_task_id: str) -> str:
    """Durable notice for relay scan. No network. Safe from CLI process."""
    os.makedirs(ADVANCE_DIR, exist_ok=True)
    path = advance_path(chat_id)
    payload = {
        "chat_id": chat_id,
        "finished_task_id": finished_task_id,
        "ts": time.time(),
        "pid": os.getpid(),
    }
    tmp = path + ".tmp.%d-%s" % (os.getpid(), uuid.uuid4().hex[:8])
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    log("advance notice written", chat_hash(chat_id), "finished", finished_task_id)
    return path


def clear_advance_notice(chat_id: str) -> None:
    path = advance_path(chat_id)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass



def ahead_count(chat_id: str, task_id: str) -> int:
    """How many non-terminal tasks are ahead of task_id in this chat's FIFO."""
    prog = _prog()
    with prog.chat_guard(chat_id):
        q = load_queue(chat_id)
        order = _prune_order_unlocked(chat_id, list(q.get("order") or []))
        if order != q.get("order"):
            q["order"] = order
            save_queue(chat_id, q)
        try:
            return order.index(task_id)
        except ValueError:
            return len(order)


def enqueue(chat_id: str, task_id: str) -> Tuple[int, List[str]]:
    """Append task_id if absent. Returns (ahead_count, order)."""
    prog = _prog()
    with prog.chat_guard(chat_id):
        q = load_queue(chat_id)
        order = _prune_order_unlocked(chat_id, list(q.get("order") or []))
        if task_id not in order:
            order.append(task_id)
        q["order"] = order
        save_queue(chat_id, q)
        return order.index(task_id), list(order)


def remove_task(chat_id: str, task_id: str) -> List[str]:
    prog = _prog()
    with prog.chat_guard(chat_id):
        q = load_queue(chat_id)
        order = [t for t in (q.get("order") or []) if t != task_id]
        order = _prune_order_unlocked(chat_id, order)
        q["order"] = order
        save_queue(chat_id, q)
        return list(order)


def head_task(chat_id: str) -> Optional[str]:
    prog = _prog()
    with prog.chat_guard(chat_id):
        q = load_queue(chat_id)
        order = _prune_order_unlocked(chat_id, list(q.get("order") or []))
        if order != q.get("order"):
            q["order"] = order
            save_queue(chat_id, q)
        return order[0] if order else None


def is_executable_head(chat_id: str, task_id: str) -> bool:
    return head_task(chat_id) == task_id


def _already_claimed_or_delivered(st: dict) -> bool:
    if st.get("webhook_ok") or st.get("delivered_at"):
        return True
    if st.get("dispatch_claimed_at"):
        return True
    if st.get("status") in ("dispatching", "queued", "running"):
        return True
    return False


def write_executable_pending(summary: dict) -> str:
    os.makedirs(PENDING_DIR, exist_ok=True)
    name = summary.get("event_id") or summary.get("message_id") or summary.get("task_id") or (
        "ts-%d" % int(time.time() * 1000)
    )
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))
    path = os.path.join(PENDING_DIR, name + ".json")
    tmp = path + ".tmp.%d-%s" % (os.getpid(), uuid.uuid4().hex[:8])
    with open(tmp, "w") as f:
        json.dump(summary, f, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    log("executable pending written", os.path.basename(path))
    return path


def post_webhook(summary: dict) -> Tuple[bool, Optional[str]]:
    """POST webhook. Never logs secrets. Test hook may replace transport."""
    if _post_hook is not None:
        return _post_hook(summary)
    prog = _prog()
    url = os.environ.get("FEISHU_RELAY_WEBHOOK_URL", "").strip()
    key = os.environ.get("FEISHU_RELAY_WEBHOOK_KEY", "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if not url or not key:
        return False, "任务投递未配置"
    body = json.dumps(summary, ensure_ascii=False).encode()
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url, body,
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + key,
                    "X-Webhook-Key": key,
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                log("webhook ok", r.status, "task", summary.get("task_id"))
                return True, None
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            try:
                e.read(200)
            except Exception:
                pass
            log("webhook http", e.code)
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:
            last_err = str(e)
            log("webhook error", prog.redact(str(e)))
        time.sleep(min(2 ** attempt, 8))
    return False, prog.redact(str(last_err or "unknown"))


def build_summary_from_task(state: dict) -> dict:
    tid = state["task_id"]
    pmid = state.get("progress_message_id")
    text = state.get("request_text") or ""
    return {
        "kind": "message",
        "event_id": state.get("event_id"),
        "message_id": state.get("message_id"),
        "chat_id": state.get("chat_id"),
        "chat_type": state.get("chat_type"),
        "thread_id": state.get("thread_id"),
        "root_id": state.get("root_id"),
        "parent_id": state.get("parent_id"),
        "sender_open_id": state.get("sender_open_id"),
        "sender_type": state.get("sender_type"),
        "msg_type": state.get("msg_type") or "text",
        "text": text,
        "create_time": state.get("create_time"),
        "task_id": tid,
        "progress_message_id": pmid,
        "queue_position": int(state.get("queued_count") or 0),
        "executable": True,
        "progress_instruction": (
            "UPDATE the existing Feishu progress card (progress_message_id) via "
            "progress.py in the configured relay directory — do NOT create a new progress card. "
            "Use: python3 progress.py start|update|done|fail --task-id <task_id> ..."
        ),
        "context": (
            "Feishu %s message from %s: %s | task_id=%s progress_message_id=%s "
            "(UPDATE that card; do not create another progress card)"
            % (state.get("chat_type"), state.get("sender_open_id"), text, tid, pmid)
        ),
    }


def mark_waiting(task_id: str, ahead: int) -> dict:
    prog = _prog()
    with prog.task_guard(task_id):
        st = prog.load_task(task_id)
        if not st:
            raise FileNotFoundError("unknown task_id: %s" % task_id)
        if prog.is_terminal(st.get("status")):
            return st
        # Do not demote an already-claimed/dispatched task back to waiting
        if _already_claimed_or_delivered(st):
            return st
        ahead = max(0, int(ahead))
        st["status"] = "waiting"
        st["executable"] = False
        st["queued_count"] = ahead
        st["current_action"] = "排队等待"
        st["detail"] = (
            "前面还有 %d 条任务，轮到本任务后才会开始执行…" % ahead
            if ahead > 0 else "排队等待…"
        )
        events = list(st.get("events") or [])
        if events and not events[-1].get("done", True):
            events[-1]["done"] = True
        if not events or events[-1].get("label") != "排队等待":
            events.append({"label": "排队等待", "done": False, "ts": time.time()})
        st["events"] = events
        st["elapsed_sec"] = prog._elapsed(st)
        prog._bump_seq_unlocked(st)
        if st.get("progress_message_id"):
            card = prog.build_progress_card(
                task_id=task_id,
                request_text=st.get("request_text") or "",
                status="waiting",
                elapsed_sec=st["elapsed_sec"],
                events=st["events"],
                current_action=st["current_action"],
                detail=st["detail"],
                receive_label=st.get("receive_label"),
                queued_count=ahead,
                stop_requested=bool(st.get("stop_requested")),
            )
            prog._patch_state_card(st, card, force=True)
        prog.save_task(st)
        return st


def refresh_queued_counts(chat_id: str) -> None:
    """Update queued_count on non-terminal tasks. Lock order: chat then each task."""
    prog = _prog()
    with prog.chat_guard(chat_id):
        q = load_queue(chat_id)
        order = _prune_order_unlocked(chat_id, list(q.get("order") or []))
        if order != q.get("order"):
            q["order"] = order
            save_queue(chat_id, q)
        snapshots = list(enumerate(order))
    for ahead, tid in snapshots:
        try:
            with prog.task_guard(tid):
                st = prog.load_task(tid)
                if not st or prog.is_terminal(st.get("status")):
                    continue
                if int(st.get("queued_count") or 0) == ahead:
                    continue
                st["queued_count"] = ahead
                if st.get("progress_message_id") and st.get("status") in (
                    "waiting", "ack", "queued", "running", "dispatching",
                ):
                    card = prog.build_progress_card(
                        task_id=tid,
                        request_text=st.get("request_text") or "",
                        status=st.get("status") if st.get("status") != "dispatching" else "queued",
                        elapsed_sec=prog._elapsed(st),
                        events=st.get("events") or [],
                        current_action=st.get("current_action"),
                        detail=st.get("detail"),
                        receive_label=st.get("receive_label"),
                        queued_count=ahead,
                        stop_requested=bool(st.get("stop_requested")),
                    )
                    prog._patch_state_card(st, card, force=False)
                prog.save_task(st)
        except Exception as e:
            log("refresh_queued_counts error", tid, _prog().redact(str(e)))


def claim_dispatch(chat_id: str, task_id: str, *, summary: Optional[dict] = None
                   ) -> Tuple[str, Optional[dict]]:
    """Atomically claim head for dispatch under chat→task locks.

    Returns (result, summary_or_none) where result is:
      'claimed'   — this caller owns network POST
      'skip'      — already claimed/delivered/running; do NOT POST
      'not_head'  — task is not FIFO head
      'missing' / 'terminal'
    Persist claim BEFORE returning so a concurrent claimer sees it.
    """
    prog = _prog()
    with prog.chat_guard(chat_id):
        q = load_queue(chat_id)
        order = _prune_order_unlocked(chat_id, list(q.get("order") or []))
        if order != q.get("order"):
            q["order"] = order
            save_queue(chat_id, q)
        if not order or order[0] != task_id:
            return "not_head", None
        with prog.task_guard(task_id):
            st = prog.load_task(task_id)
            if not st:
                return "missing", None
            if prog.is_terminal(st.get("status")):
                return "terminal", None
            if _already_claimed_or_delivered(st):
                log("claim skip already claimed/delivered", task_id,
                    "status", st.get("status"),
                    "claimed", bool(st.get("dispatch_claimed_at")),
                    "webhook_ok", bool(st.get("webhook_ok")))
                return "skip", None
            if summary is None:
                summary = build_summary_from_task(st)
            summary = dict(summary)
            summary["executable"] = True
            summary["task_id"] = task_id
            summary["progress_message_id"] = st.get("progress_message_id")
            # Durable claim — status leaves waiting/ack so start() can proceed
            # once webhook_ok is set; mid-flight status=dispatching.
            st["dispatch_claimed_at"] = time.time()
            st["dispatch_claim_pid"] = os.getpid()
            st["dispatch_claim_id"] = uuid.uuid4().hex[:12]
            st["status"] = "dispatching"
            st["executable"] = True
            st["queued_count"] = 0
            st["current_action"] = "投递中"
            st["detail"] = "正在投递到执行队列…"
            prog.save_task(st)
            log("dispatch claimed", task_id, "claim", st["dispatch_claim_id"])
            return "claimed", summary


def _finalize_dispatch_ok(task_id: str, pending_path: str) -> None:
    prog = _prog()
    # mark_webhook_result acquires its own task_guard — do not hold ours
    prog.mark_webhook_result(task_id, ok=True)
    with prog.task_guard(task_id):
        st = prog.load_task(task_id)
        if st and not prog.is_terminal(st.get("status")):
            st["delivered_at"] = time.time()
            st["executable"] = True
            st["queued_count"] = 0
            st["pending_path"] = pending_path
            if st.get("status") == "dispatching":
                st["status"] = "queued"
            prog.save_task(st)


def _finalize_dispatch_fail(task_id: str, pending_path: Optional[str], err: Optional[str]) -> None:
    prog = _prog()
    if pending_path:
        try:
            if os.path.isfile(pending_path):
                os.remove(pending_path)
                log("removed failed-delivery pending", os.path.basename(pending_path))
        except OSError as ex:
            log("pending remove error", prog.redact(str(ex)))
    # fail() schedules advance notice — do NOT hold locks here
    prog.mark_webhook_result(
        task_id, ok=False,
        error="任务投递失败/超时：%s" % (err or "unknown"),
    )


def dispatch_task(task_id: str, *, chat_id: Optional[str] = None,
                  summary: Optional[dict] = None, re_fire: bool = False) -> str:
    """Claim then (if claimed) write pending + POST. Relay-only network path.

    re_fire=True is reserved; normal/restart paths use False (at-most-once).
    Returns: 'posted' | 'skipped' | 'failed' | 'not_head' | ...
    """
    prog = _prog()
    if not chat_id:
        st0 = prog.load_task(task_id)
        chat_id = (st0 or {}).get("chat_id") or ""
    if not chat_id:
        return "missing_chat"

    if re_fire:
        # Explicit re-fire still goes through claim after clearing? Refuse by default.
        log("dispatch_task: re_fire refused (at-most-once policy)", task_id)
        return "re_fire_refused"

    result, summary = claim_dispatch(chat_id, task_id, summary=summary)
    if result == "skip":
        return "skipped"
    if result != "claimed":
        return result

    # Locks released — network I/O
    pending_path = write_executable_pending(summary or {})
    ok, err = post_webhook(summary or {})
    if ok:
        try:
            _finalize_dispatch_ok(task_id, pending_path)
        except Exception as e:
            log("finalize ok error", task_id, prog.redact(str(e)))
        return "posted"
    try:
        _finalize_dispatch_fail(task_id, pending_path, err)
    except Exception as e:
        log("finalize fail error", task_id, prog.redact(str(e)))
    return "failed"


def try_dispatch_head(chat_id: str, *, summary_for: Optional[Dict[str, dict]] = None) -> Optional[str]:
    """Dispatch chat head if unclaimed. Idempotent across processes."""
    head = head_task(chat_id)
    if not head:
        return None
    summary = (summary_for or {}).get(head)
    outcome = dispatch_task(head, chat_id=chat_id, summary=summary, re_fire=False)
    log("try_dispatch_head", head, outcome)
    return head


def notify_terminal(chat_id: str, task_id: str) -> None:
    """CLI-safe: remove from FIFO + durable advance notice. NO network.

    Relay scan / wake will claim+POST the new head. Never call fail/done here.
    """
    if not chat_id or not task_id:
        return
    try:
        order = remove_task(chat_id, task_id)
        write_advance_notice(chat_id, task_id)
        log("notify_terminal", chat_hash(chat_id), "finished", task_id, "remaining", len(order))
        # Cosmetic queue-count refresh (no network dispatch)
        try:
            refresh_queued_counts(chat_id)
        except Exception as e:
            log("notify refresh error", _prog().redact(str(e)))
    except Exception as e:
        log("notify_terminal error", _prog().redact(str(e)))
        # Still try to leave a notice so relay scan recovers
        try:
            write_advance_notice(chat_id, task_id)
        except Exception:
            pass


def advance_chat(chat_id: str) -> Optional[str]:
    """Relay-owned: prune terminals, dispatch new head if needed. Returns head or None."""
    if not chat_id:
        return None
    prog = _prog()
    with prog.chat_guard(chat_id):
        q = load_queue(chat_id)
        order = _prune_order_unlocked(chat_id, list(q.get("order") or []))
        if order != q.get("order"):
            q["order"] = order
            save_queue(chat_id, q)
        head = order[0] if order else None
    clear_advance_notice(chat_id)
    if not head:
        return None
    outcome = dispatch_task(head, chat_id=chat_id, re_fire=False)
    log("advance_chat", chat_hash(chat_id), "head", head, outcome)
    try:
        refresh_queued_counts(chat_id)
    except Exception:
        pass
    return head


def scan_all_queues() -> dict:
    """Periodic relay fallback: process advance notices + undelivered heads.

    Observable: logs each chat outcome. Recoverable: re-run on next interval.
    """
    os.makedirs(QUEUES_DIR, exist_ok=True)
    os.makedirs(ADVANCE_DIR, exist_ok=True)
    stats = {"notices": 0, "advanced": 0, "scanned": 0, "errors": 0}
    # 1) Explicit advance notices from CLI done/fail
    for name in list(os.listdir(ADVANCE_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(ADVANCE_DIR, name)
        try:
            with open(path) as f:
                notice = json.load(f)
            cid = notice.get("chat_id")
            stats["notices"] += 1
            if cid:
                advance_chat(cid)
                stats["advanced"] += 1
            else:
                os.remove(path)
        except Exception as e:
            stats["errors"] += 1
            log("scan notice error", _prog().redact(str(e)))
    # 2) All queue files — catch missed notices / restart gaps
    for name in list(os.listdir(QUEUES_DIR)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        path = os.path.join(QUEUES_DIR, name)
        try:
            with open(path) as f:
                q = json.load(f)
            cid = q.get("chat_id")
            if not cid:
                continue
            stats["scanned"] += 1
            advance_chat(cid)
        except Exception as e:
            stats["errors"] += 1
            log("scan queue error", _prog().redact(str(e)))
    return stats


def rebuild_queues_from_tasks() -> Dict[str, List[str]]:
    prog = _prog()
    by_chat: Dict[str, List[Tuple[float, str]]] = {}
    if not os.path.isdir(prog.TASKS_DIR):
        return {}
    for name in os.listdir(prog.TASKS_DIR):
        if not name.endswith(".json") or name.startswith("."):
            continue
        tid = name[:-5]
        st = prog.load_task(tid)
        if not st or prog.is_terminal(st.get("status")):
            continue
        if st.get("bypass_fifo"):
            continue
        cid = st.get("chat_id") or "unknown"
        by_chat.setdefault(cid, []).append((float(st.get("created_at") or 0), tid))
    result: Dict[str, List[str]] = {}
    for cid, items in by_chat.items():
        items.sort(key=lambda x: x[0])
        discovered = [t for _, t in items]
        with prog.chat_guard(cid):
            q = load_queue(cid)
            existing = [t for t in (q.get("order") or []) if t in set(discovered)]
            for t in discovered:
                if t not in existing:
                    existing.append(t)
            existing = _prune_order_unlocked(cid, existing)
            q["order"] = existing
            save_queue(cid, q)
            result[cid] = list(existing)
        log("rebuild queue", chat_hash(cid), "n", len(result[cid]))
    return result


def restore_after_restart() -> dict:
    """Rebuild queues + resume HB. Do NOT re-fire claimed/delivered tasks.

    Undelivered unclaimed heads get one claim+dispatch attempt.
    """
    prog = _prog()
    rebuilt = rebuild_queues_from_tasks()
    resumed = []
    dispatched = []
    for cid, order in rebuilt.items():
        refresh_queued_counts(cid)
        for tid in order:
            with prog.task_guard(tid):
                st = prog.load_task(tid)
                if not st or prog.is_terminal(st.get("status")):
                    continue
                delivered = _already_claimed_or_delivered(st)
                if not delivered and st.get("status") not in ("waiting",):
                    # Unclaimed non-head leftovers → waiting
                    if tid != order[0]:
                        st["status"] = "waiting"
                        st["executable"] = False
                        prog.save_task(st)
                if st.get("progress_message_id"):
                    prog.start_heartbeat(tid)
                    resumed.append(tid)
        if order:
            head = order[0]
            outcome = dispatch_task(head, chat_id=cid, re_fire=False)
            if outcome == "posted":
                dispatched.append(head)
            log("restore head", head, outcome)
    return {"queues": {k: len(v) for k, v in rebuilt.items()},
            "resumed": resumed, "dispatched": dispatched}


BYPASS_SLASH = re.compile(r"^/(stop|status|help)(\s|$)", re.I)


def is_bypass_text(text: str) -> bool:
    return bool(BYPASS_SLASH.match((text or "").strip()))
