#!/usr/bin/env python3
"""Feishu progress-card helpers (cc-connect style).

Creates / patches Card 2.0 progress cards via `feishu` (lark-cli as bot).
Restart-safe task state lives under tasks/<task_id>.json.

Cross-process safety:
  Every load→render→PATCH→save holds an fcntl.flock on tasks/<task_id>.lock
  (plus an in-process RLock for thread reentrancy). seq / last_patch_at are
  persisted on disk so a relay heartbeat cannot overwrite a CLI done/fail.

IMPORTANT — tool-event bridging:
  This Grok Bot / webhook-routine environment does NOT expose a real-time
  tool-start/tool-end event bus to the relay. Callers (the Feishu routine)
  MUST call start/update/done/fail (or this module's CLI) with real stage
  labels. Heartbeats only refresh elapsed time and never invent actions.
  The relay process is the SOLE heartbeat owner (create_ack / resume_heartbeats).
  Stage CLI (start/update/done/fail) must NOT spawn heartbeat threads.

Stop button:
  Sets stop_requested=True and updates the card footer. It does NOT mark the
  task failed/cancelled — only the executor confirming the request (or a new
  task_id retry) ends work. Card terminal ≠ execution cancelled.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

import model_resolve  # noqa: E402  — Grok Bot model footer resolution

def _resolve_feishu_bin(env=None):
    env = os.environ if env is None else env
    return env.get("FEISHU_CLI") or env.get("FEISHU_BIN") or "lark-cli"


FEISHU = _resolve_feishu_bin()
DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_DIR = os.path.join(DIR, "tasks")
CARDS_DIR = os.path.join(os.path.dirname(DIR), "cards")
BOT_NAME = os.environ.get("FEISHU_BOT_NAME", "Grok Bot")
HEARTBEAT_SEC = float(os.environ.get("FEISHU_PROGRESS_HEARTBEAT_SEC", "12"))
PATCH_MIN_INTERVAL = float(os.environ.get("FEISHU_PROGRESS_PATCH_MIN_SEC", "1.0"))
MAX_STAGE_EVENTS = 8
# Never leave cards blue forever.
# QUEUED_TIMEOUT applies ONLY to delivered-but-not-started (webhook_ok / delivered_at).
# Pure FIFO queue wait (status=waiting) must NEVER use this timeout.
QUEUED_TIMEOUT_SEC = float(os.environ.get("FEISHU_PROGRESS_QUEUED_TIMEOUT_SEC", "600"))
RUNNING_STALE_SEC = float(os.environ.get("FEISHU_PROGRESS_RUNNING_STALE_SEC", "1800"))  # no stage update

TERMINAL = frozenset(("done", "failed", "timeout"))

_thread_locks: Dict[str, threading.RLock] = {}
_chat_thread_locks: Dict[str, threading.RLock] = {}
_registry_lock = threading.Lock()
_heartbeats: Dict[str, "_Heartbeat"] = {}

FeishuRunner = Callable[[List[str], Optional[str]], Tuple[int, str, str]]


def log(*a: Any) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[progress]", *a, flush=True)


def redact(s: str) -> str:
    if not s:
        return s
    s = re.sub(r"(Bearer\s+)\S+", r"\1[REDACTED]", s, flags=re.I)
    s = re.sub(r"(tenant_access_token[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+", r"\1[REDACTED]", s, flags=re.I)
    s = re.sub(r"(app_secret[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+", r"\1[REDACTED]", s, flags=re.I)
    s = re.sub(r"(webhook[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+", r"\1[REDACTED]", s, flags=re.I)
    s = re.sub(r"https?://[^\s\"']+", "[URL]", s)
    return s[:400]


def is_terminal(status: Optional[str]) -> bool:
    return (status or "") in TERMINAL


def make_task_id(message_id: Optional[str] = None) -> str:
    """Stable ≥16-hex id from full message_id (not last-8-chars)."""
    if message_id:
        return hashlib.sha256(str(message_id).encode("utf-8")).hexdigest()[:16]
    return uuid.uuid4().hex[:16]


def task_path(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task_id))
    return os.path.join(TASKS_DIR, safe + ".json")


def lock_path(task_id: str) -> str:
    return task_path(task_id) + ".lock"


def chat_lock_path(chat_id: str) -> str:
    h = hashlib.sha256(str(chat_id or "unknown").encode()).hexdigest()[:16]
    return os.path.join(TASKS_DIR, ".chat-%s.lock" % h)


def _unique_tmp(path: str) -> str:
    return "%s.tmp.%d-%d-%s" % (path, os.getpid(), threading.get_ident(), uuid.uuid4().hex[:8])


def atomic_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = _unique_tmp(path)
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _thread_lock(key: str, store: Dict[str, threading.RLock]) -> threading.RLock:
    with _registry_lock:
        if key not in store:
            store[key] = threading.RLock()
        return store[key]


class _FileLock:
    """Exclusive fcntl.flock; not reentrant across nested acquire on new fds.
    Always pair with a threading.RLock and do not nest task_guard."""

    def __init__(self, path: str):
        self.path = path
        self._fd: Optional[Any] = None

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._fd = open(self.path, "a+")
        fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            finally:
                try:
                    self._fd.close()
                except Exception:
                    pass
                self._fd = None


@contextmanager
def task_guard(task_id: str) -> Generator[None, None, None]:
    """Cross-process exclusive lock for one task. Hold across load→PATCH→save."""
    tl = _thread_lock("t:" + task_id, _thread_locks)
    fl = _FileLock(lock_path(task_id))
    tl.acquire()
    try:
        fl.acquire()
        try:
            yield
        finally:
            fl.release()
    finally:
        tl.release()


@contextmanager
def chat_guard(chat_id: str) -> Generator[None, None, None]:
    """Serialize card creates / follow-ups per chat across processes."""
    tl = _thread_lock("c:" + (chat_id or "unknown"), _chat_thread_locks)
    fl = _FileLock(chat_lock_path(chat_id or "unknown"))
    tl.acquire()
    try:
        fl.acquire()
        try:
            yield
        finally:
            fl.release()
    finally:
        tl.release()


def load_task(task_id: str) -> Optional[dict]:
    path = task_path(task_id)
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log("load_task error", task_id, redact(str(e)))
        return None


def save_task(state: dict) -> str:
    """Caller MUST hold task_guard. Persists seq / last_patch_at / status."""
    tid = state["task_id"]
    path = task_path(tid)
    state["updated_at"] = time.time()
    atomic_write_json(path, state)
    return path


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return "%02d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def receive_time_label(ts: Optional[float] = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts or time.time()))


def escape_lark_md(text: str, *, keep_newlines: bool = False) -> str:
    """Escape user text for Feishu lark-im markdown so it stays literal.

    HTML-escapes & < > " ' and neutralizes <at>/<link> style tags so user content
    cannot inject @mentions, links, or executable card actions.
    """
    if not text:
        return ""
    s = str(text)
    # Neutralize at/link tags before HTML escape (case-insensitive)
    s = re.sub(r"</?\s*at\b[^>]*>", "", s, flags=re.I)
    s = re.sub(r"</?\s*link\b[^>]*>", "", s, flags=re.I)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace('"', "&quot;").replace("'", "&#39;")
    if not keep_newlines:
        s = s.replace("\n", " ").replace("\r", " ")
    return s


def _truncate_req(text: str, n: int = 200) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) > n:
        return text[: n - 1] + "…"
    return text or "(空消息)"


def _request_quote_line(request_text: str, task_id: str, *, n: int = 160) -> str:
    """Single-line escaped 对应消息摘要 + task number."""
    short_id = (task_id or "")[:6]
    raw = _truncate_req(request_text, n)
    return "**对应消息** · 任务 #%s\n> %s" % (short_id, escape_lark_md(raw, keep_newlines=False))


def _request_elements(request_text: str, task_id: str) -> List[dict]:
    """Visible escaped quote of original request + short task number; long text collapsible."""
    full_raw = (request_text or "").strip() or "(空消息)"
    short_id = (task_id or "")[:6]
    elements: List[dict] = [
        {
            "tag": "markdown",
            "content": _request_quote_line(request_text, task_id, n=160),
            "element_id": "req",
        },
    ]
    if len(full_raw) > 160 or "\n" in (request_text or ""):
        elements.append({
            "tag": "collapsible_panel",
            "element_id": "req_full_panel",
            "expanded": False,
            "header": {"title": {"tag": "plain_text", "content": "完整请求原文"}},
            "border": {"color": "grey"},
            "vertical_spacing": "8px",
            "padding": "4px 8px",
            "elements": [{
                "tag": "markdown",
                "content": escape_lark_md(full_raw[:4000], keep_newlines=True),
                "element_id": "req_full",
            }],
        })
    return elements


def _steps_markdown(events: List[dict], current: Optional[str], status: str) -> str:
    lines: List[str] = []
    shown = events[-MAX_STAGE_EVENTS:]
    omitted = len(events) - len(shown)
    if omitted > 0:
        lines.append("…另有 %d 步已完成" % omitted)
    for i, ev in enumerate(shown):
        label = ev.get("label") or ev.get("action") or "步骤"
        done = bool(ev.get("done", True))
        is_last = i == len(shown) - 1
        if status in ("done", "failed", "timeout") or done:
            lines.append("✅ %s" % label)
        elif is_last and status in ("running", "queued", "ack", "waiting"):
            lines.append("⏳ **%s**" % label)
        else:
            lines.append("✅ %s" % label)
    if status in ("ack", "queued", "waiting") and not events:
        lines.append("⏳ **已收到**")
        lines.append("⬜ 等待启动")
    return "\n".join(lines) if lines else "⏳ **处理中**"


def build_progress_card(
    *,
    task_id: str,
    request_text: str,
    status: str,
    elapsed_sec: float,
    events: Optional[List[dict]] = None,
    current_action: Optional[str] = None,
    detail: Optional[str] = None,
    footer: Optional[str] = None,
    receive_label: Optional[str] = None,
    error: Optional[str] = None,
    queued_count: int = 0,
    stop_requested: bool = False,
    model_id: Optional[str] = None,
    model_name: Optional[str] = None,
    model_source: Optional[str] = None,
    model_kind: Optional[str] = None,
) -> dict:
    events = list(events or [])
    elapsed = format_elapsed(elapsed_sec)
    short = task_id[:6]
    show_stop = False
    show_detail = False

    if status == "done":
        title = "%s · 已完成" % BOT_NAME
        template, tag = "green", ("完成", "green")
        subtitle = "任务 #%s · 用时 %s" % (short, elapsed)
        default_footer = "本进度卡已停止更新，结果见下一条总结卡。"
    elif status in ("failed", "timeout"):
        title = "%s · 失败" % BOT_NAME
        template, tag = "red", ("失败" if status == "failed" else "超时", "red")
        subtitle = "任务 #%s · 用时 %s" % (short, elapsed)
        default_footer = "本进度卡已停止更新。"
        show_detail = bool(error or detail)
    elif status == "waiting":
        title = "%s · 排队中" % BOT_NAME
        template, tag = "blue", ("排队中", "blue")
        recv = receive_label or receive_time_label()
        subtitle = "任务 #%s · 收到 %s · 已用时 %s" % (short, recv, elapsed)
        ahead = max(0, int(queued_count or 0))
        default_footer = "前面还有 %d 条，等待轮到本任务" % ahead if ahead else "排队等待"
        show_stop = True
        show_detail = True
        if not current_action:
            current_action = "排队等待"
        if not detail:
            detail = "消息已收到（%s）。前面还有 %d 条任务，轮到后再开始。" % (recv, ahead)
    elif status in ("ack", "queued", "dispatching"):
        title = "%s · 已收到" % BOT_NAME
        template, tag = "blue", ("等待启动", "blue")
        recv = receive_label or receive_time_label()
        subtitle = "任务 #%s · 收到 %s · 已用时 %s" % (short, recv, elapsed)
        default_footer = "已受理，等待开始"
        show_stop = True
        show_detail = True
        if not current_action:
            current_action = "等待启动"
        if not detail:
            detail = "消息已收到（%s），已进入执行槽，等待开始…" % recv
    else:  # running
        title = "%s · 进行中" % BOT_NAME
        template, tag = "blue", ("运行中", "blue")
        subtitle = "任务 #%s · 已用时 %s" % (short, elapsed)
        default_footer = "阶段变化时更新"
        show_stop = True
        show_detail = True

    if stop_requested and status not in TERMINAL:
        tag = ("停止请求中", "orange")
        default_footer = "已收到停止请求，等待确认"

    if queued_count > 0 and status not in TERMINAL:
        default_footer = "已排队 %d 条 · %s" % (queued_count, default_footer)

    model_info = {
        "model_id": model_id,
        "model_name": model_name,
        "model_source": model_source,
        "model_kind": model_kind,
    }
    # Status footer stays status-only; model_footer omitted (temporary hide).
    footer = footer if footer is not None else default_footer

    steps = _steps_markdown(events, current_action, status)
    if status in ("failed", "timeout") and error:
        steps = (steps + "\n" if steps else "") + "❌ **失败**：%s" % error[:200]

    elements: List[dict] = _request_elements(request_text, task_id)
    elements += [
        {"tag": "hr"},
        {"tag": "markdown", "content": steps, "element_id": "steps"},
    ]
    if show_detail and (detail or current_action):
        elements.append({
            "tag": "collapsible_panel",
            "element_id": "detail_panel",
            "expanded": status in ("ack", "queued", "running", "waiting"),
            "header": {"title": {"tag": "plain_text", "content": "当前步骤详情"}},
            "border": {"color": "grey"},
            "vertical_spacing": "8px",
            "padding": "4px 8px",
            "elements": [{
                "tag": "markdown",
                "content": detail or ("当前：%s" % current_action),
                "element_id": "detail",
            }],
        })
    if show_stop and not stop_requested:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "8px",
            "columns": [{
                "tag": "column",
                "width": "auto",
                "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "请求停止"},
                    "type": "danger",
                    "behaviors": [{
                        "type": "callback",
                        "value": {"action": "request_stop", "task_id": task_id},
                    }],
                }],
            }],
        })
    elements.append({
        "tag": "markdown",
        "content": footer or default_footer,
        "element_id": "footer",
        "text_size": "notation",
    })
    _append_model_footer(elements, model_info)
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill", "summary": {"content": title}},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": template,
            "text_tag_list": [{
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": tag[0]},
                "color": tag[1],
            }],
        },
        "body": {"elements": elements},
    }


def build_summary_card(*, task_id: str, elapsed_sec: float, body_md: str,
                       request_text: str = "",
                       model_id: Optional[str] = None,
                       model_name: Optional[str] = None,
                       model_source: Optional[str] = None,
                       model_kind: Optional[str] = None) -> dict:
    elapsed = format_elapsed(elapsed_sec)
    title = "%s · 结果" % BOT_NAME
    model_info = {
        "model_id": model_id, "model_name": model_name,
        "model_source": model_source, "model_kind": model_kind,
    }
    elements: List[dict] = [
        {"tag": "markdown", "content": _request_quote_line(request_text, task_id), "element_id": "req"},
        {"tag": "hr"},
        {"tag": "markdown", "content": body_md[:8000], "element_id": "main_text"},
        {"tag": "hr"},
        {"tag": "markdown", "content": "%s · 进度卡已结束" % BOT_NAME,
         "element_id": "footer", "text_size": "notation"},
    ]
    _append_model_footer(elements, model_info)
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill", "summary": {"content": title}},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": "任务 #%s · 用时 %s" % (task_id[:6], elapsed)},
            "template": "green",
        },
        "body": {"elements": elements},
    }


def build_error_card(*, task_id: str, elapsed_sec: float, what: str, detail: str = "",
                     request_text: str = "",
                     model_id: Optional[str] = None,
                     model_name: Optional[str] = None,
                     model_source: Optional[str] = None,
                     model_kind: Optional[str] = None) -> dict:
    elapsed = format_elapsed(elapsed_sec)
    title = "%s · 出错了" % BOT_NAME
    model_info = {
        "model_id": model_id, "model_name": model_name,
        "model_source": model_source, "model_kind": model_kind,
    }
    elements: List[dict] = [
        {"tag": "markdown", "content": _request_quote_line(request_text, task_id), "element_id": "req"},
        {"tag": "hr"},
        {"tag": "markdown", "content": "**发生了什么**：%s" % escape_lark_md(what[:500])},
    ]
    if detail:
        elements.append({
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {"title": {"tag": "plain_text", "content": "错误详情"}},
            "border": {"color": "grey"},
            "padding": "4px 8px",
            "elements": [{"tag": "markdown", "content": "```\n%s\n```" % detail[:1500]}],
        })
    elements += [
        {"tag": "hr"},
        {"tag": "markdown",
         "content": "任务 #%s · 用时 %s · 发送 /new 开始新会话" % (task_id[:6], elapsed),
         "element_id": "footer", "text_size": "notation"},
    ]
    _append_model_footer(elements, model_info)
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill", "summary": {"content": title}},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": "任务 #%s · 失败" % task_id[:6]},
            "template": "red",
        },
        "body": {"elements": elements},
    }


# --------------- Feishu API I/O ---------------

def _default_runner(argv: List[str], stdin_text: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        argv = list(argv)
        if "--as" not in argv:
            argv.extend(["--as", "bot"])
        r = subprocess.run(argv, input=stdin_text, capture_output=True, text=True, timeout=45, cwd=DIR)
        return r.returncode, r.stdout or "", r.stderr or ""
    except Exception as e:
        return 1, "", str(e)


_runner: FeishuRunner = _default_runner


def set_runner(fn: Optional[FeishuRunner]) -> None:
    global _runner
    _runner = fn or _default_runner


def _parse_json_out(stdout: str) -> dict:
    stdout = (stdout or "").strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except ValueError:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
    return {"raw": stdout[:500]}


def _extract_message_id(raw: dict) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    for key in ("message_id", "msg_id"):
        if data.get(key):
            return str(data[key])
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    if inner.get("message_id"):
        return str(inner["message_id"])
    msg = inner.get("message") if isinstance(inner.get("message"), dict) else {}
    if msg.get("message_id"):
        return str(msg["message_id"])
    return None


def _api_code(raw: dict) -> Optional[int]:
    if not isinstance(raw, dict):
        return None
    if "code" in raw and isinstance(raw["code"], int):
        return raw["code"]
    error = raw.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), int):
        return error["code"]
    data = raw.get("data")
    if isinstance(data, dict) and isinstance(data.get("code"), int):
        return data["code"]
    return None


def reply_interactive(
    message_id: str,
    card: dict,
    *,
    reply_in_thread: bool = False,
    idempotency_key: Optional[str] = None,
) -> Tuple[bool, Optional[str], dict]:
    # --content is a JSON value, not an @file argument (CLI 1.0.96).
    # subprocess receives a single literal argument, without shell interpolation.
    argv = [
        FEISHU, "im", "+messages-reply",
        "--message-id", message_id,
        "--msg-type", "interactive",
        "--content", json.dumps(card, ensure_ascii=False),
    ]
    if reply_in_thread:
        argv.append("--reply-in-thread")
    if idempotency_key:
        argv.extend(["--idempotency-key", idempotency_key[:50]])
    rc, out, err = _runner(argv, None)
    raw = _parse_json_out(out) if out else ({"stderr": redact(err)} if err else {})
    mid = _extract_message_id(raw)
    ok = rc == 0 and mid is not None
    if not ok:
        log("reply_interactive fail", message_id, "rc=%d" % rc, redact(out + " " + err))
    else:
        log("reply_interactive ok", message_id, "->", mid)
    return bool(ok), mid, raw


def patch_interactive(
    progress_message_id: str,
    card: dict,
    *,
    force: bool = False,
    last_patch_at: float = 0.0,
) -> Tuple[bool, dict, float]:
    """PATCH card. Returns (ok, raw, new_last_patch_at). Throttle uses persisted timestamp."""
    if not progress_message_id:
        return False, {"error": "no progress_message_id"}, last_patch_at
    now = time.time()
    if not force and last_patch_at and now - last_patch_at < PATCH_MIN_INTERVAL:
        time.sleep(PATCH_MIN_INTERVAL - (now - last_patch_at))

    body = json.dumps({"content": json.dumps(card, ensure_ascii=False)}, ensure_ascii=False)
    argv = [FEISHU, "im", "messages", "patch", "--message-id", progress_message_id, "--data", body]
    delay = 1.0
    last_raw: dict = {}
    for attempt in range(4):
        rc, out, err = _runner(argv, None)
        raw = _parse_json_out(out or err) if out or err else {}
        last_raw = raw or {"stderr": redact(err), "rc": rc}
        code = _api_code(raw)
        ok = rc == 0 and (raw.get("ok") is True or code == 0 or (code is None and rc == 0 and not err))
        if ok or code == 0:
            ts = time.time()
            log("patch ok", progress_message_id, "attempt", attempt + 1)
            return True, raw, ts
        if code == 230020:
            log("patch rate-limit", progress_message_id, "sleep", delay)
            time.sleep(delay)
            delay = min(delay * 2, 8)
            continue
        if code in (230031, 230011):
            log("patch permanent fail", progress_message_id, "code", code)
            return False, raw, last_patch_at
        log("patch fail", progress_message_id, "rc=%d" % rc, "code", code, redact(out + " " + err))
        if attempt < 3:
            time.sleep(delay)
            delay = min(delay * 2, 8)
            continue
        return False, last_raw, last_patch_at
    return False, last_raw, last_patch_at


def send_interactive(chat_id: str, card: dict) -> Tuple[bool, Optional[str], dict]:
    argv = [
        FEISHU, "im", "+messages-send",
        "--chat-id", chat_id,
        "--msg-type", "interactive",
        "--content", json.dumps(card, ensure_ascii=False),
    ]
    rc, out, err = _runner(argv, None)
    raw = _parse_json_out(out) if out else {}
    mid = _extract_message_id(raw)
    ok = rc == 0 and mid is not None
    if not ok:
        log("send_interactive fail", chat_id, redact(out + " " + err))
    return ok, mid, raw


def deliver_followup(state: dict, card: dict) -> Tuple[bool, Optional[str], dict]:
    """Summary/error card MUST reply to the original message_id (same association).

    Never silently fall back to bare send. Report API failures to caller/logs.
    """
    chat_type = (state.get("chat_type") or "").lower()
    mid = state.get("message_id") or ""
    if not mid.startswith("om_"):
        err = {"error": "missing original message_id; refuse bare send", "message_id": mid}
        log("deliver_followup refuse bare send", state.get("task_id"), redact(json.dumps(err)))
        return False, None, err
    # Keep replies in an existing topic, without creating one for ordinary groups.
    reply_in_thread = chat_type == "topic" or bool(state.get("thread_id"))
    ok, smid, raw = reply_interactive(mid, card, reply_in_thread=reply_in_thread)
    if not ok:
        log("deliver_followup reply API failed", state.get("task_id"), mid, redact(json.dumps(raw)[:300]))
    return ok, smid, raw


# --------------- Heartbeat ---------------

class _Heartbeat:
    def __init__(self, task_id: str, interval: Optional[float] = None):
        self.task_id = task_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hb-%s" % self.task_id, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while True:
            interval = self.interval if self.interval is not None else HEARTBEAT_SEC
            if self._stop.wait(interval):
                break
            try:
                _heartbeat_tick(self.task_id)
            except Exception as e:
                log("heartbeat error", self.task_id, redact(str(e)))


def start_heartbeat(task_id: str) -> None:
    with _registry_lock:
        hb = _heartbeats.get(task_id)
        if hb is None:
            hb = _Heartbeat(task_id)
            _heartbeats[task_id] = hb
        hb.start()


def stop_heartbeat(task_id: str) -> None:
    with _registry_lock:
        hb = _heartbeats.pop(task_id, None)
    if hb:
        hb.stop()




def _append_model_footer(elements: List[dict], state_or_model: Optional[dict] = None,
                         **model_fields: Any) -> List[dict]:
    """Temporarily omit model_footer from public cards (authorized hide).

    Internal model_* metadata may still be persisted via model_resolve; public
    rendering must not show model names, routing slots, or configured-model labels.
    """
    return elements


def _model_kwargs(state: Optional[dict] = None, **overrides: Any) -> dict:
    """model_* kwargs for build_progress_card from task state / overrides."""
    st = state or {}
    return {
        "model_id": overrides.get("model_id", st.get("model_id")),
        "model_name": overrides.get("model_name", st.get("model_name")),
        "model_source": overrides.get("model_source", st.get("model_source")),
        "model_kind": overrides.get("model_kind", st.get("model_kind")),
    }


def _ensure_model_on_state(
    state: dict,
    *,
    override: Optional[str] = None,
    model_source: Optional[str] = None,
    model_kind: Optional[str] = None,
    refresh: bool = False,
) -> dict:
    """Persist task-bound model_* snapshot; freeze unless override/refresh.

    Official-routing (null id/name) is a valid frozen snapshot — heartbeat /
    update / done must NOT re-resolve Statsig / global env / routing cache.
    Only an explicit --model override or refresh=True may change the snapshot.
    """
    if override or refresh:
        model_resolve.apply_model_to_state(
            state,
            override=override,
            model_source=model_source,
            model_kind=model_kind,
            prefer_automation=True,
            refresh=True if refresh else False,
        )
        return state
    # Already snapshotted (including official-routing with null id/name)
    if state.get("model_bound_to_task") or state.get("model_source"):
        return state
    model_resolve.apply_model_to_state(
        state,
        override=None,
        model_source=model_source,
        model_kind=model_kind,
        prefer_automation=True,
    )
    return state


def _footer_for(state: dict, heartbeat: bool = False) -> str:
    status = state.get("status")
    if state.get("stop_requested") and not is_terminal(status):
        base = "已收到停止请求，等待确认"
    elif state.get("pending_approvals") and not is_terminal(status):
        base = "等待权限审批 · 请处理权限卡或在 Grok App 审批"
    elif status == "waiting":
        ahead = int(state.get("queued_count") or 0)
        base = "前面还有 %d 条，排队等待" % ahead if ahead else "排队等待"
    elif status in ("ack", "queued"):
        base = "已受理，等待开始"
    elif status == "running":
        base = "执行中"
    else:
        base = ""
    if heartbeat and not is_terminal(status):
        last_u = state.get("last_stage_at") or state.get("updated_at")
        if last_u:
            base = (base + " · " if base else "") + "上次更新 %s" % time.strftime(
                "%H:%M:%S", time.localtime(float(last_u)))
    q = int(state.get("queued_count") or 0)
    if q > 0 and not is_terminal(status):
        base = "已排队 %d 条 · %s" % (q, base)
    base = base or "阶段变化时更新"
    return base


def _elapsed(state: dict) -> float:
    started = float(state.get("started_at") or state.get("created_at") or time.time())
    return time.time() - started


def _bump_seq_unlocked(state: dict) -> int:
    state["seq"] = int(state.get("seq") or 0) + 1
    return state["seq"]


def _patch_state_card(state: dict, card: dict, *, force: bool) -> bool:
    """PATCH + update last_patch_at on state. Caller holds task_guard."""
    pmid = state.get("progress_message_id")
    if not pmid:
        return False
    ok, raw, ts = patch_interactive(
        pmid, card, force=force, last_patch_at=float(state.get("last_patch_at") or 0),
    )
    state["last_patch_ok"] = ok
    if ok:
        state["last_patch_at"] = ts
    else:
        state["last_patch_error"] = redact(json.dumps(raw, ensure_ascii=False)[:300])
    return ok


def _maybe_timeout_unlocked(state: dict) -> Optional[str]:
    """If timed out, mutate state toward timeout and return reason; else None.
    Caller holds lock; caller must PATCH+save after.

    Queue wait (status=waiting / not yet delivered) must NEVER use QUEUED_TIMEOUT.
    Startup timeout only for delivered-but-not-started (webhook_ok or delivered_at).
    """
    if is_terminal(state.get("status")):
        return None
    if state.get("pending_approvals"):
        return None  # Observed native pending request; human wait is not a stall.
    now = time.time()
    created = float(state.get("created_at") or now)
    last_stage = float(state.get("last_stage_at") or state.get("started_at") or created)
    status = state.get("status")
    if status == "waiting":
        return None  # FIFO queue wait — no 600s false timeout
    delivered = bool(state.get("webhook_ok") or state.get("delivered_at"))
    claimed = bool(state.get("dispatch_claimed_at")) or status == "dispatching"
    if status in ("ack", "queued", "dispatching") and (delivered or claimed):
        start_ref = float(
            state.get("delivered_at")
            or state.get("dispatch_claimed_at")
            or state.get("created_at")
            or created
        )
        if now - start_ref >= QUEUED_TIMEOUT_SEC:
            return "已投递但未启动超时（%ss）" % int(QUEUED_TIMEOUT_SEC)
    elif status in ("ack", "queued") and not delivered and not claimed:
        return None
    if status == "running" and now - last_stage >= RUNNING_STALE_SEC:
        return "执行中过久无阶段更新（%ss）" % int(RUNNING_STALE_SEC)
    return None


def _apply_timeout_unlocked(state: dict, reason: str) -> dict:
    """Flip to timeout terminal under lock. Does not reopen done/failed."""
    if is_terminal(state.get("status")):
        return state
    state["status"] = "timeout"
    state["error"] = reason
    state["detail"] = reason
    state["finished_at"] = time.time()
    state["elapsed_sec"] = _elapsed(state)
    _bump_seq_unlocked(state)
    card = build_progress_card(
        task_id=state["task_id"],
        request_text=state.get("request_text") or "",
        status="timeout",
        elapsed_sec=state["elapsed_sec"],
        events=state.get("events") or [],
        error=reason,
        detail=reason,
        receive_label=state.get("receive_label"),
        **_model_kwargs(state),
    )
    _patch_state_card(state, card, force=True)
    save_task(state)
    state["_schedule_next"] = True  # caller / heartbeat schedules outside task lock
    return state


def _heartbeat_tick(task_id: str) -> None:
    """Refresh elapsed only.

    Holds the cross-process task lock across load→render→PATCH→save so a
    concurrent done()/fail() waits for this in-flight Feishu PATCH to finish,
    then applies green/red afterward. Releasing during PATCH would let a stale
    blue PATCH land after green and leave the card stuck blue forever.
    """
    schedule_chat = None
    schedule_tid = None
    with task_guard(task_id):
        state = load_task(task_id)
        if not state:
            stop_heartbeat(task_id)
            return
        if is_terminal(state.get("status")):
            stop_heartbeat(task_id)
            return

        _ensure_model_on_state(state)

        reason = _maybe_timeout_unlocked(state)
        if reason:
            log("heartbeat timeout", task_id, reason)
            _apply_timeout_unlocked(state, reason)
            stop_heartbeat(task_id)
            schedule_chat = state.get("chat_id")
            schedule_tid = task_id
        else:
            pmid = state.get("progress_message_id")
            if not pmid:
                return

            action = state.get("current_action") or "处理中"
            last_u = state.get("last_stage_at") or state.get("last_heartbeat_at") or state.get("created_at")
            last_label = time.strftime("%H:%M:%S", time.localtime(float(last_u))) if last_u else "—"
            elapsed = _elapsed(state)
            detail = "已用时 %s · 上次更新 %s" % (format_elapsed(elapsed), last_label)
            card = build_progress_card(
                task_id=task_id,
                request_text=state.get("request_text") or "",
                status=state.get("status") or "running",
                elapsed_sec=elapsed,
                events=state.get("events") or [],
                current_action=action,
                detail=detail,
                receive_label=state.get("receive_label"),
                queued_count=int(state.get("queued_count") or 0),
                stop_requested=bool(state.get("stop_requested")),
                footer=_footer_for(state, heartbeat=True),
        **_model_kwargs(state),
    )
            # Hold flock through network PATCH (bounded by subprocess timeout).
            ok = _patch_state_card(state, card, force=False)
            if is_terminal(state.get("status")):
                stop_heartbeat(task_id)
                return
            state2 = load_task(task_id)
            if not state2 or is_terminal(state2.get("status")):
                stop_heartbeat(task_id)
                return
            if ok:
                state2["last_heartbeat_at"] = time.time()
                state2["last_patch_at"] = state.get("last_patch_at")
                state2["elapsed_sec"] = _elapsed(state2)
                save_task(state2)
            return

    if schedule_chat and schedule_tid:
        _schedule_next_after_terminal({"chat_id": schedule_chat, "task_id": schedule_tid})


def _schedule_next_after_terminal(state: dict) -> None:
    """After terminal: durable FIFO advance notice only (NO webhook from CLI).

    Must run WITHOUT holding task_guard (notify acquires chat lock).
    Relay periodic scan / wake owns network dispatch of the next head.
    """
    if not state or not state.get("chat_id"):
        return
    if state.get("bypass_fifo"):
        return
    try:
        import chat_queue as fifo
        fifo.notify_terminal(state.get("chat_id") or "", state.get("task_id") or "")
    except Exception as e:
        log("schedule_next/notify error", redact(str(e)))


def resume_heartbeats() -> List[str]:
    """After relay restart: resume HB for non-terminal tasks; timeout stale ones.

    Prefer chat_queue.restore_after_restart() from relay — this helper remains for
    CLI / legacy. Waiting tasks are never startup-timed-out.
    """
    resumed: List[str] = []
    to_schedule: List[dict] = []
    if not os.path.isdir(TASKS_DIR):
        return resumed
    for name in os.listdir(TASKS_DIR):
        if not name.endswith(".json") or name.startswith("."):
            continue
        tid = name[:-5]
        with task_guard(tid):
            state = load_task(tid)
            if not state or is_terminal(state.get("status")):
                continue
            reason = _maybe_timeout_unlocked(state)
            if reason:
                log("resume: timeout stale task", tid, reason)
                _apply_timeout_unlocked(state, reason)
                to_schedule.append({"chat_id": state.get("chat_id"), "task_id": tid,
                                    "bypass_fifo": state.get("bypass_fifo")})
                continue
            if state.get("progress_message_id"):
                start_heartbeat(tid)
                resumed.append(tid)
                log("resume heartbeat", tid, "status", state.get("status"))
    for item in to_schedule:
        _schedule_next_after_terminal(item)
    return resumed


# --------------- Public stage API ---------------

def create_ack(
    *,
    message_id: str,
    chat_id: str,
    chat_type: str,
    request_text: str,
    task_id: Optional[str] = None,
    event_id: Optional[str] = None,
    sender_open_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    queued_count: int = 0,
    bypass_fifo: bool = False,
    initial_status: str = "ack",
    model: Optional[str] = None,
    model_source: Optional[str] = None,
    model_kind: Optional[str] = None,
) -> dict:
    tid = task_id or make_task_id(message_id)
    now = time.time()
    recv = receive_time_label(now)
    status0 = initial_status if initial_status in ("ack", "waiting") else "ack"
    ahead = max(0, int(queued_count or 0))
    if status0 == "waiting":
        action0 = "排队等待"
        detail0 = (
            "消息已收到（%s）。前面还有 %d 条任务，轮到后再开始。" % (recv, ahead)
            if ahead else "消息已收到（%s），排队等待…" % recv
        )
        events = [{"label": "排队等待", "done": False, "ts": now}]
    else:
        action0 = "已收到 / 等待启动"
        detail0 = "消息已收到（%s），等待开始…" % recv
        events = [{"label": "已收到", "done": False, "ts": now}]
    state = {
        "task_id": tid,
        "message_id": message_id,
        "event_id": event_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "thread_id": thread_id,
        "sender_open_id": sender_open_id,
        "request_text": request_text,
        "status": status0,
        "current_action": action0,
        "detail": detail0,
        "events": events,
        "receive_label": recv,
        "created_at": now,
        "started_at": now,
        "last_stage_at": now,
        "progress_message_id": None,
        "queued_count": ahead,
        "executable": False,
        "bypass_fifo": bool(bypass_fifo),
        "seq": 0,
        "last_patch_at": 0.0,
        "stop_requested": False,
    }
    model_resolve.apply_model_to_state(
        state, override=model, model_source=model_source,
        model_kind=model_kind, prefer_automation=True,
    )
    card = build_progress_card(
        task_id=tid,
        request_text=request_text,
        status=status0,
        elapsed_sec=0,
        events=events,
        current_action=action0,
        detail=detail0,
        receive_label=recv,
        queued_count=ahead,
        **_model_kwargs(state),
    )

    with chat_guard(chat_id or "unknown"):
        with task_guard(tid):
            existing = load_task(tid)
            if existing and existing.get("message_id") == message_id and existing.get("progress_message_id"):
                log("create_ack reuse existing card", tid, existing["progress_message_id"])
                if not is_terminal(existing.get("status")):
                    start_heartbeat(tid)
                return existing
            if existing and is_terminal(existing.get("status")):
                # Never reopen a terminal task — caller should use a new task_id (new message).
                log("create_ack refused: task already terminal", tid, existing.get("status"))
                return existing

            reply_in_thread = (chat_type or "").lower() == "topic" or bool(thread_id)
            ok, pmid, raw = reply_interactive(
                message_id, card,
                reply_in_thread=reply_in_thread,
                idempotency_key=("prog-" + tid)[:50],
            )
            if ok and pmid:
                state["progress_message_id"] = pmid
                state["card_create_ok"] = True
                state["seq"] = 1
            else:
                state["card_create_ok"] = False
                state["card_create_error"] = redact(json.dumps(raw, ensure_ascii=False)[:300])
                log("create_ack card API failed; continuing without card", tid)
            save_task(state)
            if state.get("progress_message_id"):
                start_heartbeat(tid)
            return state


def start(task_id: str, action: str = "执行中", detail: Optional[str] = None,
         model: Optional[str] = None, model_source: Optional[str] = None,
         model_kind: Optional[str] = None) -> dict:
    """Mark running. Fail-closed: must be delivered/executable head (not waiting).

    On head-check errors → refuse (do not continue).
    """
    with task_guard(task_id):
        state = load_task(task_id)
        if not state:
            raise FileNotFoundError("unknown task_id: %s" % task_id)
        if is_terminal(state.get("status")):
            log("start ignored; terminal", task_id, state.get("status"))
            return state
        status = state.get("status")
        if status == "waiting":
            log("start refused: waiting", task_id)
            state["start_refused"] = True
            state["start_refuse_reason"] = "waiting_in_fifo"
            save_task(state)
            return state
        # Executable only if bypass, or webhook delivered, or actively dispatching/queued/running
        ok_exec = bool(
            state.get("bypass_fifo")
            or state.get("webhook_ok")
            or state.get("delivered_at")
            or status in ("queued", "running", "dispatching")
        )
        if not ok_exec:
            log("start refused: not executable/delivered", task_id, status)
            state["start_refused"] = True
            state["start_refuse_reason"] = "not_executable"
            save_task(state)
            return state
        snap = {
            "chat_id": state.get("chat_id"),
            "bypass_fifo": bool(state.get("bypass_fifo")),
            "status": status,
        }
    if not snap["bypass_fifo"] and snap.get("chat_id"):
        try:
            import chat_queue as fifo
            if not fifo.is_executable_head(snap["chat_id"], task_id):
                with task_guard(task_id):
                    st2 = load_task(task_id) or {}
                    # Still allow if already running (idempotent start)
                    if st2.get("status") == "running":
                        return st2
                    st2["start_refused"] = True
                    st2["start_refuse_reason"] = "not_queue_head"
                    save_task(st2)
                    log("start refused: not queue head", task_id)
                    return st2
        except Exception as e:
            # Fail-closed: do not continue on head-check errors
            log("start head-check error; refuse", redact(str(e)))
            with task_guard(task_id):
                st2 = load_task(task_id) or {}
                st2["start_refused"] = True
                st2["start_refuse_reason"] = "head_check_error"
                save_task(st2)
                return st2
    return update(
        task_id,
        action=action,
        detail=detail or "已开始执行…",
        status="running",
        mark_prev_done=True,
        model=model,
        model_source=model_source,
        model_kind=model_kind,
    )


def update(
    task_id: str,
    *,
    action: Optional[str] = None,
    detail: Optional[str] = None,
    status: Optional[str] = None,
    mark_prev_done: bool = True,
    queued_count: Optional[int] = None,
    model: Optional[str] = None,
    model_source: Optional[str] = None,
    model_kind: Optional[str] = None,
) -> dict:
    with task_guard(task_id):
        state = load_task(task_id)
        if not state:
            raise FileNotFoundError("unknown task_id: %s" % task_id)
        if is_terminal(state.get("status")):
            log("update ignored; terminal", task_id, state.get("status"))
            return state

        _ensure_model_on_state(
            state, override=model, model_source=model_source, model_kind=model_kind,
        )

        # Waiting: reject ordinary updates that change status or invent stages
        if state.get("status") == "waiting":
            if status is not None and status != "waiting":
                log("update refused: cannot change waiting status to", status, task_id)
                state["update_refused"] = True
                state["update_refuse_reason"] = "waiting_status_immutable"
                save_task(state)
                return state
            if action:
                log("update refused: waiting cannot take stage action", task_id)
                state["update_refused"] = True
                state["update_refuse_reason"] = "waiting_not_executable"
                save_task(state)
                return state
            # Cosmetic only: queued_count / detail
            if queued_count is not None:
                state["queued_count"] = queued_count
            if detail is not None:
                state["detail"] = detail
            state["elapsed_sec"] = _elapsed(state)
            _bump_seq_unlocked(state)
            card = build_progress_card(
                task_id=task_id,
                request_text=state.get("request_text") or "",
                status="waiting",
                elapsed_sec=state["elapsed_sec"],
                events=state.get("events") or [],
                current_action=state.get("current_action"),
                detail=state.get("detail"),
                receive_label=state.get("receive_label"),
                queued_count=int(state.get("queued_count") or 0),
                stop_requested=bool(state.get("stop_requested")),
        **_model_kwargs(state),
    )
            _patch_state_card(state, card, force=True)
            save_task(state)
            return state

        if status:
            if is_terminal(status):
                log("update refused terminal status via update(); use done/fail", task_id)
            else:
                state["status"] = status
        elif state.get("status") in ("ack", "queued", "dispatching"):
            state["status"] = "running"

        if queued_count is not None:
            state["queued_count"] = queued_count

        if action:
            events = list(state.get("events") or [])
            if mark_prev_done and events and not events[-1].get("done", True):
                events[-1]["done"] = True
            if not events or events[-1].get("label") != action:
                events.append({"label": action, "done": False, "ts": time.time()})
            else:
                events[-1]["done"] = False
                events[-1]["ts"] = time.time()
            state["events"] = events[-MAX_STAGE_EVENTS * 2:]
            state["current_action"] = action
            state["last_stage_at"] = time.time()
        if detail is not None:
            state["detail"] = detail

        state["elapsed_sec"] = _elapsed(state)
        _bump_seq_unlocked(state)

        card = build_progress_card(
            task_id=task_id,
            request_text=state.get("request_text") or "",
            status=state.get("status") or "running",
            elapsed_sec=state["elapsed_sec"],
            events=state.get("events") or [],
            current_action=state.get("current_action"),
            detail=state.get("detail"),
            receive_label=state.get("receive_label"),
            queued_count=int(state.get("queued_count") or 0),
            stop_requested=bool(state.get("stop_requested")),
        **_model_kwargs(state),
    )
        _patch_state_card(state, card, force=True)
        save_task(state)
        # Do NOT start_heartbeat here — relay is the sole heartbeat owner
        # (create_ack / resume_heartbeats). CLI stage updates must not spawn HB.
        return state


def done(
    task_id: str,
    *,
    summary_md: Optional[str] = None,
    send_summary: bool = True,
    final_action: Optional[str] = None,
    model: Optional[str] = None,
    model_source: Optional[str] = None,
    model_kind: Optional[str] = None,
) -> dict:
    """Green terminal. Refuses if already failed/timeout. Irreversible."""
    with task_guard(task_id):
        state = load_task(task_id)
        if not state:
            raise FileNotFoundError("unknown task_id: %s" % task_id)
        cur = state.get("status")
        if cur == "done":
            return state
        if cur in ("failed", "timeout"):
            log("done refused; already terminal", task_id, cur)
            return state

        _ensure_model_on_state(
            state, override=model, model_source=model_source, model_kind=model_kind,
        )
        stop_heartbeat(task_id)
        events = list(state.get("events") or [])
        for ev in events:
            ev["done"] = True
        if final_action:
            if not events or events[-1].get("label") != final_action:
                events.append({"label": final_action, "done": True, "ts": time.time()})
            else:
                events[-1]["done"] = True
        state["events"] = events
        state["status"] = "done"
        state["current_action"] = final_action or state.get("current_action") or "已完成"
        state["elapsed_sec"] = _elapsed(state)
        state["finished_at"] = time.time()
        state["last_stage_at"] = time.time()
        _bump_seq_unlocked(state)

        card = build_progress_card(
            task_id=task_id,
            request_text=state.get("request_text") or "",
            status="done",
            elapsed_sec=state["elapsed_sec"],
            events=events,
            receive_label=state.get("receive_label"),
        **_model_kwargs(state),
    )
        _patch_state_card(state, card, force=True)

        if send_summary and summary_md:
            scard = build_summary_card(
                task_id=task_id, elapsed_sec=state["elapsed_sec"], body_md=summary_md,
                request_text=state.get("request_text") or "",
                **_model_kwargs(state),
            )
            sok, smid, sraw = deliver_followup(state, scard)
            state["summary_message_id"] = smid
            state["summary_ok"] = sok
            if not sok:
                state["summary_error"] = redact(json.dumps(sraw, ensure_ascii=False)[:300])

        save_task(state)
        out = dict(state)
    _schedule_next_after_terminal(out)
    return out


def fail(
    task_id: str,
    *,
    error: str,
    detail: Optional[str] = None,
    send_error_card: bool = True,
    status: str = "failed",
    model: Optional[str] = None,
    model_source: Optional[str] = None,
    model_kind: Optional[str] = None,
) -> dict:
    """Red terminal. Refuses if already done. Irreversible. Retries need a new task_id."""
    if status not in ("failed", "timeout"):
        status = "failed"
    with task_guard(task_id):
        state = load_task(task_id)
        if not state:
            raise FileNotFoundError("unknown task_id: %s" % task_id)
        cur = state.get("status")
        if cur == "done":
            log("fail refused; already done", task_id)
            return state
        if cur in ("failed", "timeout") and state.get("error") == error:
            return state
        if cur in ("failed", "timeout"):
            # Already terminal — do not flip colors / reopen; keep first failure.
            log("fail ignored; already terminal", task_id, cur)
            return state

        _ensure_model_on_state(
            state, override=model, model_source=model_source, model_kind=model_kind,
        )
        stop_heartbeat(task_id)
        state["status"] = status
        state["error"] = error
        state["detail"] = detail or error
        state["elapsed_sec"] = _elapsed(state)
        state["finished_at"] = time.time()
        _bump_seq_unlocked(state)

        card = build_progress_card(
            task_id=task_id,
            request_text=state.get("request_text") or "",
            status=status,
            elapsed_sec=state["elapsed_sec"],
            events=state.get("events") or [],
            error=error,
            detail=detail or error,
            receive_label=state.get("receive_label"),
        **_model_kwargs(state),
    )
        _patch_state_card(state, card, force=True)

        if send_error_card:
            ecard = build_error_card(
                task_id=task_id,
                elapsed_sec=state["elapsed_sec"],
                what=error,
                detail=detail or "",
                request_text=state.get("request_text") or "",
                **_model_kwargs(state),
            )
            eok, emid, eraw = deliver_followup(state, ecard)
            state["error_message_id"] = emid
            state["error_card_ok"] = eok
            if not eok:
                state["error_card_error"] = redact(json.dumps(eraw, ensure_ascii=False)[:300])

        save_task(state)
        out = dict(state)
    _schedule_next_after_terminal(out)
    return out


def request_stop(task_id: str, *, operator_id: Optional[str] = None) -> dict:
    """Record stop request only. Does NOT cancel execution or mark failed.

    Verifies operator is the original sender when sender_open_id is known.
    Executor must poll stop_requested and call done/fail itself.
    """
    with task_guard(task_id):
        state = load_task(task_id)
        if not state:
            raise FileNotFoundError("unknown task_id: %s" % task_id)
        if is_terminal(state.get("status")):
            log("request_stop ignored; terminal", task_id, state.get("status"))
            return state

        sender = state.get("sender_open_id")
        # Reject if sender known and operator missing or mismatched
        if sender and (not operator_id or operator_id != sender):
            reason = "缺少操作者" if not operator_id else "仅发起人可请求停止"
            log("request_stop denied;", reason, task_id, "op", operator_id, "sender", sender)
            state["stop_denied"] = True
            state["stop_accepted"] = False
            state["stop_deny_reason"] = reason
            # Do NOT set stop_requested
            save_task(state)
            return state

        state["stop_requested"] = True
        state["stop_accepted"] = True
        state["stop_denied"] = False
        state["stop_requested_at"] = time.time()
        state["stop_requested_by"] = operator_id
        state["detail"] = "已收到停止请求，等待确认。"
        _bump_seq_unlocked(state)
        state["elapsed_sec"] = _elapsed(state)
        card = build_progress_card(
            task_id=task_id,
            request_text=state.get("request_text") or "",
            status=state.get("status") or "running",
            elapsed_sec=state["elapsed_sec"],
            events=state.get("events") or [],
            current_action=state.get("current_action"),
            detail=state["detail"],
            receive_label=state.get("receive_label"),
            queued_count=int(state.get("queued_count") or 0),
            stop_requested=True,
        **_model_kwargs(state),
    )
        _patch_state_card(state, card, force=True)
        save_task(state)
        log("request_stop recorded", task_id, "by", operator_id)
        return state


def mark_webhook_result(task_id: str, *, ok: bool, error: Optional[str] = None) -> dict:
    """Record webhook delivery. Never demotes running→queued on late ACK.
    Uses its own task_guard; does not call update() while holding a lock (no nested deadlock).
    """
    if not ok:
        return fail(
            task_id,
            error=error or "任务投递失败",
            detail=error or "任务投递失败（已重试）。",
            send_error_card=True,
        )

    with task_guard(task_id):
        state = load_task(task_id)
        if not state:
            raise FileNotFoundError("unknown task_id: %s" % task_id)
        if is_terminal(state.get("status")):
            return state

        state["webhook_ok"] = True
        cur = state.get("status")
        # Late delivery ACK after routine already started: keep real current_action
        if cur == "running":
            save_task(state)
            log("mark_webhook_result: already running; skip queued demote", task_id,
                "action", state.get("current_action"))
            return state

        # ack → queued (inline; do not call update() — would re-enter task_guard)
        events = list(state.get("events") or [])
        if events and not events[-1].get("done", True):
            events[-1]["done"] = True
        if not events or events[-1].get("label") != "已入队":
            events.append({"label": "已入队", "done": False, "ts": time.time()})
        state["events"] = events
        state["status"] = "queued"
        state["current_action"] = "已入队"
        # from dispatching/ack
        state["detail"] = "已入队，等待开始…"
        state["last_stage_at"] = time.time()
        state["elapsed_sec"] = _elapsed(state)
        _bump_seq_unlocked(state)
        card = build_progress_card(
            task_id=task_id,
            request_text=state.get("request_text") or "",
            status="queued",
            elapsed_sec=state["elapsed_sec"],
            events=events,
            current_action="已入队",
            detail=state["detail"],
            receive_label=state.get("receive_label"),
            queued_count=int(state.get("queued_count") or 0),
            stop_requested=bool(state.get("stop_requested")),
        **_model_kwargs(state),
    )
        _patch_state_card(state, card, force=True)
        save_task(state)
        return state


# --------------- CLI ---------------

def _cli() -> int:
    p = argparse.ArgumentParser(description="Feishu progress card helpers")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_tid(sp):
        sp.add_argument("--task-id", required=True)

    def add_model_args(sp):
        sp.add_argument("--model", default=None, help="optional internal metadata; never displayed on cards")
        sp.add_argument("--model-source", default=None, dest="model_source",
                        help="optional model_source label (e.g. explicit, task-turn)")
        sp.add_argument("--model-kind", default=None, dest="model_kind",
                        help="optional model_kind: config (--model), runtime (Statsig), turn-resolved")

    sp = sub.add_parser("start"); add_tid(sp)
    sp.add_argument("--action", default="执行中"); sp.add_argument("--detail", default=None)
    add_model_args(sp)

    sp = sub.add_parser("update"); add_tid(sp)
    sp.add_argument("--action", required=True); sp.add_argument("--detail", default=None)
    sp.add_argument("--queued-count", type=int, default=None)
    add_model_args(sp)

    sp = sub.add_parser("done"); add_tid(sp)
    sp.add_argument("--summary", default=None); sp.add_argument("--final-action", default=None)
    sp.add_argument("--no-summary-card", action="store_true")
    add_model_args(sp)

    sp = sub.add_parser("fail"); add_tid(sp)
    sp.add_argument("--error", required=True); sp.add_argument("--detail", default=None)
    sp.add_argument("--no-error-card", action="store_true")
    add_model_args(sp)

    sp = sub.add_parser("request-stop"); add_tid(sp)
    sp.add_argument("--operator-id", default=None)

    sp = sub.add_parser("show"); add_tid(sp)
    sp = sub.add_parser("resume-heartbeats")

    args = p.parse_args()
    if args.cmd == "start":
        st = start(args.task_id, action=args.action, detail=args.detail,
                   model=args.model, model_source=args.model_source, model_kind=args.model_kind)
    elif args.cmd == "update":
        st = update(args.task_id, action=args.action, detail=args.detail,
                    queued_count=args.queued_count, model=args.model,
                    model_source=args.model_source, model_kind=args.model_kind)
    elif args.cmd == "done":
        st = done(args.task_id, summary_md=args.summary,
                  send_summary=not args.no_summary_card and bool(args.summary),
                  final_action=args.final_action, model=args.model,
                  model_source=args.model_source, model_kind=args.model_kind)
    elif args.cmd == "fail":
        st = fail(args.task_id, error=args.error, detail=args.detail,
                  send_error_card=not args.no_error_card, model=args.model,
                  model_source=args.model_source, model_kind=args.model_kind)
    elif args.cmd == "request-stop":
        st = request_stop(args.task_id, operator_id=args.operator_id)
    elif args.cmd == "show":
        st = load_task(args.task_id) or {}
    elif args.cmd == "resume-heartbeats":
        print(json.dumps({"resumed": resume_heartbeats()}, ensure_ascii=False))
        return 0
    else:
        return 2
    print(json.dumps({
        "task_id": st.get("task_id"),
        "status": st.get("status"),
        "progress_message_id": st.get("progress_message_id"),
        "current_action": st.get("current_action"),
        "elapsed_sec": st.get("elapsed_sec"),
        "seq": st.get("seq"),
        "stop_requested": st.get("stop_requested"),
        "events": st.get("events"),
        "model_id": st.get("model_id"),
        "model_name": st.get("model_name"),
        "model_source": st.get("model_source"),
        "model_kind": st.get("model_kind"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
