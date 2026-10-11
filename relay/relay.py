#!/usr/bin/env python3
"""Feishu -> pending-file (+ optional webhook) relay. Consumes events via `feishu event consume` (lark-cli NDJSON over WebSocket).

On each new im.message (after dedup), BEFORE webhook POST:
  1) create a blue Card 2.0 progress card (reply / thread reply)
  2) atomically write pending + tasks/<task_id>.json
  3) POST webhook with progress_message_id + task_id (routine must UPDATE that card)
"""
import atexit, json, os, re, signal, subprocess, sys, threading, time, urllib.request, collections

VALID_RELAY_ROLES = ("full", "inbound", "cloud-worker")
DEFAULT_FEISHU_CLI = DEFAULT_LARK_CLI = "lark-cli"


def resolve_feishu_cli(env=None):
    env = os.environ if env is None else env
    return (env.get("FEISHU_CLI") or env.get("FEISHU_BIN") or DEFAULT_FEISHU_CLI).strip()


def resolve_lark_cli(env=None):
    env = os.environ if env is None else env
    return (env.get("LARK_CLI") or DEFAULT_LARK_CLI).strip()


def parse_relay_role(raw=None, env=None):
    env = os.environ if env is None else env
    role = (env.get("RELAY_ROLE", "full") if raw is None else raw) or "full"
    role = role.strip().lower()
    if role not in VALID_RELAY_ROLES:
        raise ValueError("Invalid RELAY_ROLE; expected full, inbound or cloud-worker")
    return role


RELAY_ROLE = parse_relay_role()
FEISHU = resolve_feishu_cli()
DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
import progress  # noqa: E402
import chat_queue  # noqa: E402
import settings  # noqa: E402
import callback_server  # noqa: E402
import quoted_context  # noqa: E402
import grok_approvals  # noqa: E402

EVLOG = os.path.join(DIR, "events.log")
PENDING_DIR = os.path.join(DIR, "pending")
ACK_EMOJI = os.environ.get("FEISHU_RELAY_ACK_EMOJI", "OnIt")
BOT_OPEN_ID = os.environ.get("FEISHU_BOT_OPEN_ID", "")
BOT_NAME = os.environ.get("FEISHU_BOT_NAME", "Grok")
SEEN_FILE = os.path.join(DIR, "seen_ids.txt")
lock = threading.Lock()
seen = collections.OrderedDict()
_shutdown = threading.Event()
_children = set()
_children_lock = threading.Lock()


def stop_children():
    """Gracefully close consumer stdin and terminate only our own processes."""
    _shutdown.set()
    with _children_lock:
        children = list(_children)
        _children.clear()
    for child in children:
        try:
            if child.stdin:
                child.stdin.close()
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        except Exception as exc:
            log("consumer cleanup incomplete", progress.redact(str(exc)))


def _terminate(signum, frame):
    raise SystemExit(128 + signum)

def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)

def load_seen():
    try:
        with open(SEEN_FILE) as stored:
            for item in stored.read().split()[-5000:]:
                seen[item] = 1
    except FileNotFoundError: pass

def first_time(key):
    with lock:
        if not key or key in seen: return False
        seen[key] = 1
        while len(seen) > 5000: seen.popitem(last=False)
        with open(SEEN_FILE, "a") as f: f.write(key + "\n")
        return True

def react(mid):
    try:
        r = subprocess.run([FEISHU, "api", "POST", f"/open-apis/im/v1/messages/{mid}/reactions",
                            "--data", json.dumps({"reaction_type": {"emoji_type": ACK_EMOJI}}), "--as", "bot"],
                           capture_output=True, text=True, timeout=30)
        log("reaction", mid, "rc=%d" % r.returncode, (r.stdout + r.stderr).strip()[:200].replace("\n", " "))
    except Exception as e:
        log("reaction error", mid, e)

def strip_mentions(text, mentions):
    for m in mentions or []:
        if m.get("id") == BOT_OPEN_ID or m.get("name") == BOT_NAME:
            for tok in (m.get("key"), "@" + (m.get("name") or "")):
                if tok and tok != "@": text = text.replace(tok, "")
    # mget's rendered text can omit the original mentions array.
    text = re.sub(r"(?<!\S)@" + re.escape(BOT_NAME) + r"(?=$|\s)", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def event_from_history_message(msg, *, chat_type="p2p"):
    """Map feishu im +messages-mget item -> im.message.receive_v1-shaped event."""
    mid = msg.get("message_id") or msg.get("id")
    if not mid:
        raise ValueError("history message missing message_id")
    sender = msg.get("sender") or {}
    sender_id = sender.get("id") or msg.get("sender_id")
    sender_type = sender.get("sender_type") or msg.get("sender_type") or "user"
    ct = msg.get("create_time")
    if isinstance(ct, str) and ct and not ct.isdigit():
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            dt = datetime.strptime(ct.strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            ct = str(int(dt.timestamp() * 1000))
        except Exception:
            pass
    return {
        "message_id": mid,
        "id": mid,
        "event_id": "hist:" + str(mid),
        "chat_id": msg.get("chat_id"),
        "chat_type": chat_type,
        "content": msg.get("content") or "",
        "mentions": msg.get("mentions") or [],
        "create_time": ct,
        "message_type": msg.get("msg_type") or msg.get("message_type") or "text",
        "sender_id": sender_id,
        "sender_type": sender_type,
        "reply_to": msg.get("parent_id") or msg.get("reply_to"),
        "root_id": msg.get("root_id"),
        "thread_id": msg.get("thread_id"),
        "ingested_via": "history_backfill",
    }


def ingest_history_message_id(message_id, *, chat_type="p2p"):
    """Fetch one real message via official mget and run on_message (dedup/queue)."""
    mid = str(message_id).strip()
    out = {"message_id": mid, "path": "history_backfill", "ok": False}
    tid = progress.make_task_id(mid)
    try:
        existing = progress.load_task(tid)
    except Exception:
        existing = None
    if existing:
        out.update({
            "ok": True, "skipped": True, "reason": "task_exists", "task_id": tid,
            "progress_message_id": existing.get("progress_message_id"),
            "status": existing.get("status"),
        })
        return out
    if ("msg:" + mid) in seen:
        out.update({"ok": True, "skipped": True, "reason": "seen_message_id", "task_id": tid})
        return out
    r = subprocess.run(
        [FEISHU, "im", "+messages-mget", "--message-ids", mid, "--as", "bot"],
        capture_output=True, text=True, timeout=60,
    )
    try:
        payload = json.loads(r.stdout or "{}")
    except ValueError:
        out["error"] = "mget_non_json"
        out["stderr"] = (r.stderr or "")[:300]
        return out
    if not payload.get("ok"):
        out["error"] = payload.get("error") or "mget_failed"
        return out
    messages = (payload.get("data") or {}).get("messages") or []
    if not messages:
        out["error"] = "mget_empty"
        return out
    msg = messages[0]
    if (msg.get("message_id") or msg.get("id")) != mid:
        out["error"] = "mget_id_mismatch"
        return out
    st = (msg.get("sender") or {}).get("sender_type") or msg.get("sender_type")
    if st and st != "user":
        out["error"] = "not_user_sender"
        out["sender_type"] = st
        return out
    ev = event_from_history_message(msg, chat_type=chat_type)
    log("history_backfill begin", mid, "event_id", ev.get("event_id"))
    on_message(ev)
    st2 = progress.load_task(tid) or {}
    if not st2:
        out["error"] = "message_rejected"
        return out
    out.update({
        "ok": True, "skipped": False, "task_id": tid, "event_id": ev.get("event_id"),
        "progress_message_id": st2.get("progress_message_id"), "status": st2.get("status"),
        "webhook_ok": st2.get("webhook_ok"), "card_create_ok": st2.get("card_create_ok"),
        "ingested_via": "history_backfill", "parent_id": st2.get("parent_id"),
    })
    return out


def write_pending(summary):
    os.makedirs(PENDING_DIR, exist_ok=True)
    name = summary.get("event_id") or summary.get("message_id") or ("ts-%d" % int(time.time() * 1000))
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))
    path = os.path.join(PENDING_DIR, name + ".json")
    tmp = path + ".tmp.%d-%s" % (os.getpid(), __import__("uuid").uuid4().hex[:8])
    with open(tmp, "w") as f:
        json.dump(summary, f, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    log("pending written", path)
    return path

def deliver(summary):
    """Write pending then webhook. On webhook failure, fail the progress card (not stuck blue)."""
    write_pending(summary)
    url = os.environ.get("FEISHU_RELAY_WEBHOOK_URL", "").strip()
    key = os.environ.get("FEISHU_RELAY_WEBHOOK_KEY", "").strip()
    if key.lower().startswith("bearer "): key = key[7:].strip()
    task_id = summary.get("task_id")

    if not url or not key:
        with open(EVLOG, "a") as f: f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        log("webhook env missing; logged to events.log", summary.get("message_id"))
        if task_id:
            try:
                progress.mark_webhook_result(task_id, ok=False, error="任务投递未配置")
            except Exception as e:
                log("progress webhook-miss mark error", progress.redact(str(e)))
        return False

    body = json.dumps(summary, ensure_ascii=False).encode()
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                  "Authorization": "Bearer " + key, "X-Webhook-Key": key})
            with urllib.request.urlopen(req, timeout=30) as r:
                log("webhook ok", r.status, summary.get("message_id"),
                    "task", task_id, "progress", summary.get("progress_message_id"))
                if task_id:
                    try:
                        progress.mark_webhook_result(task_id, ok=True)
                    except Exception as e:
                        log("progress webhook-ok mark error", progress.redact(str(e)))
                return True
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            try:
                detail = e.read()[:200]
            except Exception:
                detail = b""
            log("webhook http", e.code)
            if e.code < 500 and e.code != 429: break
        except Exception as e:
            last_err = str(e)
            log("webhook error", progress.redact(str(e)))
        time.sleep(2 ** attempt)

    with open(EVLOG, "a") as f:
        f.write(json.dumps({"undelivered": True, **summary}, ensure_ascii=False) + "\n")
    if task_id:
        try:
            progress.mark_webhook_result(
                task_id, ok=False,
                error="任务投递失败/超时：%s" % progress.redact(str(last_err or "unknown")),
            )
        except Exception as e:
            log("progress webhook-fail mark error", progress.redact(str(e)))
    return False

def on_message(e):
    mid = e.get("message_id") or e.get("id")
    # Authorize before side effects. Deduplicate by the original message ID.
    if not settings.message_allowed(e):
        log("message rejected by allowlist or schema")
        return
    if not first_time("msg:" + str(mid)):
        log("dup skipped", mid); return

    threading.Thread(target=react, args=(mid,), daemon=True).start()

    user_text, text, quote, notice = quoted_context.prepare_request(e, FEISHU, strip_mentions)
    if notice:
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "Grok · 请补充内容"}},
            "body": {"elements": [{"tag": "markdown", "content": notice}]},
        }
        ok, _, _ = progress.reply_interactive(
            mid, card, reply_in_thread=e.get("chat_type") == "topic" or bool(e.get("thread_id")),
            idempotency_key="empty-" + progress.make_task_id(mid),
        )
        log("empty request notice", "sent", ok)
        return
    chat_id = e.get("chat_id") or ""
    chat_type = e.get("chat_type") or "p2p"
    event_id = e.get("event_id")
    task_id = progress.make_task_id(mid)
    bypass = chat_queue.is_bypass_text(user_text)

    # Build association fields early for task state / summary
    parent_id = e.get("reply_to") or e.get("parent_id")
    summary_base = {
        "kind": "message",
        "event_id": event_id,
        "message_id": mid,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "thread_id": e.get("thread_id"),
        "root_id": e.get("root_id"),
        "parent_id": parent_id,
        "sender_open_id": e.get("sender_id"),
        "sender_type": e.get("sender_type"),
        "msg_type": e.get("message_type"),
        "text": text,
        "user_text": user_text,
        "quoted_context": quote,
        "create_time": e.get("create_time"),
        "task_id": task_id,
    }

    if bypass:
        # /stop /status /help — NOT blocked by FIFO; immediate card + dispatch
        log("bypass slash", text.split()[0] if text else "", "task", task_id)
        try:
            st = progress.create_ack(
                message_id=mid, chat_id=chat_id, chat_type=chat_type,
                request_text=text, task_id=task_id, event_id=event_id,
                sender_open_id=e.get("sender_id"),
                thread_id=e.get("thread_id"),
                queued_count=0, bypass_fifo=True, initial_status="ack",
            )
        except Exception as ex:
            log("bypass create_ack error", progress.redact(str(ex)))
            st = {"task_id": task_id, "progress_message_id": None, "card_create_ok": False}
        pmid = st.get("progress_message_id")
        with progress.task_guard(task_id):
            st2 = progress.load_task(task_id) or st
            st2["bypass_fifo"] = True
            st2["executable"] = True
            st2["parent_id"] = parent_id
            st2["user_text"] = user_text
            st2["quoted_context"] = quote
            st2["thread_id"] = e.get("thread_id")
            st2["root_id"] = e.get("root_id")
            st2["sender_type"] = e.get("sender_type")
            st2["msg_type"] = e.get("message_type")
            st2["create_time"] = e.get("create_time")
            progress.save_task(st2)
        s = dict(summary_base)
        s["progress_message_id"] = pmid
        s["executable"] = True
        s["bypass_fifo"] = True
        s["progress_instruction"] = (
            "UPDATE the existing Feishu progress card (progress_message_id) via "
            "progress.py in the configured relay directory — do NOT create a new progress card."
        )
        s["context"] = (
            "Feishu %s slash from %s: %s | task_id=%s progress_message_id=%s"
            % (chat_type, e.get("sender_id"), text, task_id, pmid)
        )
        # Bypass claim path: write pending + webhook directly (still at-most-once via flags)
        callback_server.attach_to_summary(task_id, s)
        try:
            chat_queue.write_executable_pending(s)
        except Exception:
            progress.mark_webhook_result(task_id, ok=False, error="任务输入或独立会话绑定失败")
            return
        ok, err = chat_queue.post_webhook(s)
        if ok:
            try:
                progress.mark_webhook_result(task_id, ok=True)
                with progress.task_guard(task_id):
                    st3 = progress.load_task(task_id)
                    if st3:
                        st3["delivered_at"] = time.time()
                        st3["executable"] = True
                        progress.save_task(st3)
            except Exception as ex:
                log("bypass mark ok error", progress.redact(str(ex)))
        else:
            try:
                # scrub pending on fail
                for name in os.listdir(chat_queue.PENDING_DIR):
                    if task_id in name or (event_id and str(event_id) in name):
                        try:
                            os.remove(os.path.join(chat_queue.PENDING_DIR, name))
                        except OSError:
                            pass
                progress.mark_webhook_result(task_id, ok=False, error="投递失败：%s" % (err or ""))
            except Exception as ex:
                log("bypass mark fail error", progress.redact(str(ex)))
        return

    # --- Normal FIFO path ---
    # 1) Persist task + create reply card FIRST (scan/prune must not drop missing id).
    # 2) Enqueue after tasks/<id>.json exists.
    # 3) Recalc ahead LAST; never demote already claimed/running.
    log("progress card create begin", "task", task_id, "message", mid)
    try:
        st = progress.create_ack(
            message_id=mid, chat_id=chat_id, chat_type=chat_type,
            request_text=text, task_id=task_id, event_id=event_id,
            sender_open_id=e.get("sender_id"),
            thread_id=e.get("thread_id"),
            queued_count=0, bypass_fifo=False, initial_status="ack",
        )
        pmid = st.get("progress_message_id")
        log("progress card create done", "task", task_id,
            "progress_message_id", pmid, "ok", st.get("card_create_ok"),
            "status", st.get("status"))
    except Exception as ex:
        log("progress card create error", progress.redact(str(ex)))
        pmid = None
        with progress.task_guard(task_id):
            existing = progress.load_task(task_id)
            if not existing:
                now = time.time()
                progress.save_task({
                    "task_id": task_id, "message_id": mid, "event_id": event_id,
                    "chat_id": chat_id, "chat_type": chat_type,
                    "sender_open_id": e.get("sender_id"),
                    "request_text": text, "status": "ack",
                    "created_at": now, "started_at": now, "last_stage_at": now,
                    "progress_message_id": None, "queued_count": 0,
                    "executable": False, "seq": 0, "stop_requested": False,
                    "card_create_ok": False,
                })
        st = {"task_id": task_id, "progress_message_id": None, "card_create_ok": False}

    with progress.task_guard(task_id):
        st2 = progress.load_task(task_id) or {}
        st2["parent_id"] = parent_id
        st2["user_text"] = user_text
        st2["quoted_context"] = quote
        st2["thread_id"] = e.get("thread_id")
        st2["root_id"] = e.get("root_id")
        st2["sender_type"] = e.get("sender_type")
        st2["msg_type"] = e.get("message_type")
        st2["create_time"] = e.get("create_time")
        progress.save_task(st2)

    ahead, order = chat_queue.enqueue(chat_id, task_id)
    log("enqueued", "task", task_id, "ahead", ahead, "order_n", len(order))

    ahead_now = chat_queue.ahead_count(chat_id, task_id)
    head = chat_queue.head_task(chat_id)
    is_head = (head == task_id)

    with progress.task_guard(task_id):
        cur = progress.load_task(task_id) or {}
        already = bool(
            cur.get("dispatch_claimed_at") or cur.get("webhook_ok")
            or cur.get("delivered_at")
            or cur.get("status") in ("dispatching", "queued", "running")
        )
        if already:
            log("post-enqueue: already claimed/running; skip demote", task_id, cur.get("status"))
        elif is_head:
            cur["queued_count"] = 0
            progress.save_task(cur)

    if already:
        log("head already claimed by concurrent path; no re-dispatch", task_id)
        return

    if not is_head:
        try:
            chat_queue.mark_waiting(task_id, ahead_now if ahead_now > 0 else 1)
        except Exception as ex:
            log("mark_waiting error", progress.redact(str(ex)))
        log("queued waiting (not dispatched)", task_id, "ahead", ahead_now)
        return

    s = dict(summary_base)
    s["progress_message_id"] = pmid
    s["executable"] = True
    s["progress_instruction"] = (
        "UPDATE the existing Feishu progress card (progress_message_id) via "
        "progress.py in the configured relay directory — do NOT create a new progress card. "
        "Use: python3 progress.py start|update|done|fail --task-id <task_id> ..."
    )
    s["context"] = (
        "Feishu %s message from %s: %s | task_id=%s progress_message_id=%s "
        "(UPDATE that card; do not create another progress card)"
        % (chat_type, e.get("sender_id"), text, task_id, pmid)
    )
    log("dispatch head begin", "task", task_id)
    outcome = chat_queue.dispatch_task(task_id, chat_id=chat_id, summary=s, re_fire=False)
    log("dispatch head done", "task", task_id, outcome)



def on_card(e):
    if not settings.card_allowed(e):
        log("card rejected by allowlist or schema")
        return
    if not first_time("evt:" + str(e.get("event_id"))): return
    action_value = e.get("action_value")
    if isinstance(action_value, str):
        try:
            action_value = json.loads(action_value)
        except ValueError:
            pass
    if isinstance(action_value, dict) and action_value.get("action") in grok_approvals.ACTIONS:
        # Native approvals are resolved by the ingress API client, never by
        # spawning another routine run or passing a decision through a prompt.
        accepted = grok_approvals.handle_card(e)
        log("native approval decision", "accepted", accepted)
        return
    stop_accepted = False
    is_stop = isinstance(action_value, dict) and action_value.get("action") in ("stop", "request_stop")
    if is_stop:
        tid = action_value.get("task_id")
        if tid:
            try:
                original = progress.load_task(tid) or {}
                if (original.get("chat_id") != e.get("chat_id") or
                        original.get("progress_message_id") != e.get("message_id")):
                    log("request_stop rejected: card/task association mismatch")
                    return
                st = progress.request_stop(tid, operator_id=e.get("operator_id"))
                stop_accepted = bool(st.get("stop_accepted")) and bool(st.get("stop_requested")) and not st.get("stop_denied")
                if not stop_accepted:
                    log("request_stop denied; NOT forwarding to routine", tid,
                        "reason", st.get("stop_deny_reason"))
                    return  # unauthorized stop must not reach routine
            except Exception as ex:
                log("request_stop handler error", progress.redact(str(ex)))
                return
        else:
            log("request_stop missing task_id; drop")
            return

    s = {"kind": "card_action", **{k: e.get(k) for k in ("event_id", "message_id", "chat_id", "operator_id",
         "action_tag", "action_name", "action_value", "form_value", "input_value", "option", "options",
         "checked", "token", "timestamp")}}
    if isinstance(action_value, dict) and action_value.get("task_id"):
        s["task_id"] = action_value.get("task_id")
    s["stop_requested"] = stop_accepted
    s["bypass_fifo"] = True
    s["executable"] = True
    s["context"] = f"Feishu card action by {s['operator_id']}: {s['action_value']}"
    # Card actions bypass FIFO — immediate pending + webhook
    if s.get("task_id"):
        callback_server.attach_to_summary(s["task_id"], s)
    chat_queue.write_executable_pending(s)
    ok, err = chat_queue.post_webhook(s)
    log("card_action deliver", "ok", ok, "stop", stop_accepted, "err", err)


PRIMARY_EVENT_KEY = "im.message.receive_v1"
CARD_EVENT_KEY = "card.action.trigger"
_primary_ready = threading.Event()

def _event_status_text():
    try:
        r = subprocess.run([FEISHU, "event", "status"], capture_output=True, text=True, timeout=15)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return "status_error: %s" % e

def bus_is_running():
    """True when feishu event status reports Bus: running for this app."""
    txt = _event_status_text()
    for line in txt.splitlines():
        if "Bus:" in line and "running" in line:
            return True
    return False

def count_bus_pids():
    """Best-effort count of local lark-cli event _bus processes (should be 0 or 1)."""
    try:
        r = subprocess.run(["pgrep", "-f", r"lark-cli event _bus"], capture_output=True, text=True, timeout=10)
        pids = [x for x in (r.stdout or "").split() if x.strip()]
        return len(pids), pids
    except Exception:
        return -1, []

def wait_bus_running(timeout=90, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if bus_is_running():
            n, pids = count_bus_pids()
            log("bus running", label, "pgrep_bus_count", n, "pids", pids[:4])
            return True
        time.sleep(0.4)
    log("bus wait timeout", label)
    return False

def consume(key, handler, *, is_primary=False):
    """Consume one EventKey. Primary (im.message) starts the bus; secondary waits
    until the bus is already running so we never spawn two bus daemons."""
    while not _shutdown.is_set():
        if not is_primary:
            log("secondary consumer waiting for bus", key)
            _primary_ready.wait(timeout=120)
            if not wait_bus_running(timeout=60, label=key):
                log("secondary: bus still not up; retry", key)
                time.sleep(2)
                continue
        else:
            _primary_ready.clear()

        log("starting consumer", key, "as", "primary" if is_primary else "secondary")
        with _children_lock:
            if _shutdown.is_set():
                return
            proc = subprocess.Popen(
                [FEISHU, "event", "consume", key, "--as", "bot"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=DIR,
            )
            _children.add(proc)
        # stdin kept open on purpose (EOF = stop). Do NOT Timer-terminate consumers;
        # blind 20m kills caused long received=0 windows and missed deliveries.
        # Bus refresh is health-gated in refresh_bus_loop.

        ready_evt = threading.Event()

        def _stderr_reader(p=proc, k=key, ready=ready_evt):
            try:
                for raw in p.stderr:
                    line = raw.rstrip()
                    log("[%s] stderr:" % k, line)
                    if ("ready event_key=" + k) in line:
                        ready.set()
            except Exception as ex:
                log("[%s] stderr reader error:" % k, ex)

        threading.Thread(target=_stderr_reader, daemon=True).start()

        if is_primary:
            if not ready_evt.wait(timeout=60):
                log("primary ready marker timeout", key)
            if wait_bus_running(timeout=45, label="primary-after-ready"):
                n, pids = count_bus_pids()
                if n > 1:
                    # pgrep is host-wide and may include other applications.
                    # Never tear down this app merely because that count is >1.
                    log("multiple host-wide CLI bus pids (diagnostic only)", pids)
                _primary_ready.set()
                log("primary ready; secondary may start", key, "bus_pids", pids)
            else:
                log("primary: bus not running after ready; restarting", key)
                try:
                    proc.terminate()
                except Exception:
                    pass
                time.sleep(2)
                continue

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                log("[%s] non-json:" % key, line[:200])
                continue
            try:
                handler(ev)
            except Exception as ex:
                log("[%s] handler error:" % key, ex)
        rc = proc.wait()
        with _children_lock:
            _children.discard(proc)
        if is_primary:
            _primary_ready.clear()
        log("consumer %s exited rc=%s; restarting in 5s" % (key, rc))
        time.sleep(5)

def _im_receive_stats():
    """Return (uptime_sec, received, dropped) for im.message.receive_v1."""
    try:
        r = subprocess.run([FEISHU, "event", "status", "--json"], capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout or "{}")
        for app in data.get("apps") or []:
            if not app.get("running"):
                continue
            uptime = int(app.get("uptime_sec") or 0)
            for c in app.get("consumers") or []:
                if c.get("event_key") == PRIMARY_EVENT_KEY:
                    return uptime, int(c.get("received") or 0), int(c.get("dropped") or 0)
            return uptime, None, None
    except Exception as e:
        log("event status parse error", e)
    return None, None, None


def refresh_bus_loop():
    """Health-gated single-bus refresh (no extra consumers).

    - Short wall/monotonic poll (default 5s) detects VM wake skew without sleeping 120s.
    - event status CLI kept on a slower cadence (default 120s).
    - received==0 + uptime is only a quiet-chat heuristic, not proof of zombie.
    - wall-monotonic delta > threshold after wake triggers one controlled same-bus refresh.
    """
    skew_poll = float(os.environ.get("FEISHU_BUS_SKEW_POLL_SEC", "5"))
    status_every = float(os.environ.get("FEISHU_BUS_HEALTH_SEC", "120"))
    skew_threshold = float(os.environ.get("FEISHU_BUS_SKEW_SEC", "30"))
    zombie_uptime = float(os.environ.get("FEISHU_BUS_ZOMBIE_UPTIME_SEC", "0"))
    max_uptime = float(os.environ.get("FEISHU_BUS_MAX_UPTIME_SEC", "0"))
    cooldown = float(os.environ.get("FEISHU_BUS_REFRESH_COOLDOWN_SEC", "180"))
    last_status_at = 0.0
    last_refresh_at = 0.0
    prev_wall = time.time()
    prev_mono = time.monotonic()
    while True:
        time.sleep(skew_poll)
        now_wall = time.time()
        now_mono = time.monotonic()
        d_wall = now_wall - prev_wall
        d_mono = now_mono - prev_mono
        # Ignore negative / tiny backward wall (NTP); only large wall>>mono suggests suspend/wake
        skew = d_wall - d_mono if d_wall >= 0 and d_mono >= 0 else 0.0
        wake_skew = skew > skew_threshold
        prev_wall, prev_mono = now_wall, now_mono

        need = False
        reason = None
        uptime = received = dropped = None

        if wake_skew and (now_wall - last_refresh_at) >= cooldown:
            need = True
            reason = "wall_mono_skew_wake"
            log("bus skew detect", "d_wall", round(d_wall, 2), "d_mono", round(d_mono, 2),
                "skew", round(skew, 2))

        if (now_wall - last_status_at) >= status_every or (need and reason == "wall_mono_skew_wake"):
            last_status_at = now_wall
            try:
                uptime, received, dropped = _im_receive_stats()
                log("bus health", "uptime", uptime, "im_received", received, "im_dropped", dropped)
            except Exception as e:
                log("bus health error", e)
            # Quiet-chat heuristic only — not proof of zombie platform state
            if (not need) and zombie_uptime > 0 and uptime is not None and received == 0 and uptime >= zombie_uptime:
                if (now_wall - last_refresh_at) >= cooldown:
                    need = True
                    reason = "quiet_zero_receive_heuristic"
            if (not need) and max_uptime > 0 and uptime is not None and uptime >= max_uptime:
                if (now_wall - last_refresh_at) >= cooldown:
                    need = True
                    reason = "max_uptime"

        if not need:
            continue
        try:
            log("bus refresh triggered", reason, "uptime", uptime, "received", received,
                "skew", round(skew, 2))
            _primary_ready.clear()
            r = subprocess.run([FEISHU, "event", "stop", "--force"], capture_output=True, text=True, timeout=30)
            last_refresh_at = time.time()
            # reset skew baseline after refresh so we do not loop
            prev_wall = time.time()
            prev_mono = time.monotonic()
            log("bus refresh stop rc=%d" % r.returncode, (r.stdout + r.stderr).strip().replace("\n", " ")[:200])
        except Exception as e:
            log("bus refresh error", e)


def queue_scan_loop():
    """Periodic fallback: CLI done/fail only writes advance notices; relay advances."""
    interval = float(os.environ.get("FEISHU_QUEUE_SCAN_SEC", "5"))
    while True:
        time.sleep(interval)
        try:
            stats = chat_queue.scan_all_queues()
            if stats.get("notices") or stats.get("advanced") or stats.get("errors"):
                log("queue scan", stats)
        except Exception as e:
            log("queue scan error", progress.redact(str(e)))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "ingest-history":
    if RELAY_ROLE == "cloud-worker":
        raise SystemExit("History ingestion belongs to the inbound host")
    settings.validate_startup()
    settings.acquire_instance(DIR)
    load_seen()
    mid = sys.argv[2] if len(sys.argv) > 2 else ""
    chat_type = "p2p"
    if "--chat-type" in sys.argv:
        i = sys.argv.index("--chat-type")
        chat_type = sys.argv[i + 1] if i + 1 < len(sys.argv) else "p2p"
    if not mid:
        print(json.dumps({"ok": False, "error": "message_id required"}))
        raise SystemExit(2)
    result = ingest_history_message_id(mid, chat_type=chat_type)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get("ok") else 1)


def role_starts_message_consumer(role=None):
    return (RELAY_ROLE if role is None else role) in ("full", "inbound")


role_starts_card_consumer = role_starts_bus_refresh = role_starts_message_consumer


if __name__ == "__main__":
    if RELAY_ROLE == "cloud-worker":
        print("cloud-worker: invoke worker_progress.py from the routine; no resident listeners or local queue")
        raise SystemExit(0)
    settings.validate_startup()
    settings.acquire_instance(DIR)
    atexit.register(stop_children)
    signal.signal(signal.SIGTERM, _terminate)
    load_seen()
    os.umask(0o077)
    os.makedirs(PENDING_DIR, exist_ok=True)
    os.makedirs(progress.TASKS_DIR, exist_ok=True)
    os.makedirs(chat_queue.QUEUES_DIR, exist_ok=True)
    os.makedirs(chat_queue.ADVANCE_DIR, exist_ok=True)
    if RELAY_ROLE == "inbound":
        # Validate before dispatching restored tasks and accepting worker callbacks.
        callback_server.attach_callback({"task_id": "startup"}, {})
        server = callback_server.start_server(port=int(os.environ.get("BRIDGE_PORT", "8765")))
        atexit.register(server.shutdown)
    log("relay start; webhook configured:", bool(os.environ.get("FEISHU_RELAY_WEBHOOK_URL") and os.environ.get("FEISHU_RELAY_WEBHOOK_KEY")))
    log("progress helpers ready; tasks dir:", progress.TASKS_DIR)
    try:
        restored = chat_queue.restore_after_restart()
        log("queue restore:", {k: restored[k] for k in ("queues", "dispatched") if k in restored},
            "resumed_n", len(restored.get("resumed") or []))
    except Exception as ex:
        log("queue restore error", progress.redact(str(ex)))
        try:
            resumed = progress.resume_heartbeats()
            log("fallback resumed heartbeats:", len(resumed))
        except Exception as ex2:
            log("resume_heartbeats error", progress.redact(str(ex2)))
    threading.Thread(target=refresh_bus_loop, daemon=True).start()
    threading.Thread(target=queue_scan_loop, daemon=True).start()
    try:
        grok_approvals.start(_shutdown)
    except Exception:
        log("native approvals startup unavailable; messaging remains active")
    # Secondary waits until primary brings the bus up — never two bus daemons.
    threading.Thread(
        target=consume, args=(CARD_EVENT_KEY, on_card),
        kwargs={"is_primary": False}, daemon=True,
    ).start()
    consume(PRIMARY_EVENT_KEY, on_message, is_primary=True)
