"""Durable, per-chat snapshots between authorized mentions of the bot."""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import progress
import settings


def _page(cli, container_id, token, deadline, container_type="chat"):
    params = {"container_id_type": container_type, "container_id": container_id,
              "page_size": "50", "sort_type": "ByCreateTimeDesc",
              "card_msg_content_type": "user_card_content"}
    if token:
        params["page_token"] = token
    try:
        result = subprocess.run(
            [cli, "api", "GET", "/open-apis/im/v1/messages", "--params",
             json.dumps(params), "--as", "bot"], capture_output=True, text=True,
            timeout=max(0.1, min(15, deadline - time.monotonic())))
        data = json.loads(result.stdout or result.stderr or "{}")
        if not isinstance(data, dict):
            return None, "invalid_response"
        if result.returncode or data.get("ok") is False or data.get("code", 0):
            error = data.get("error") or {}
            code = (error.get("code") if isinstance(error, dict) else None) or data.get("code")
            return None, "permission_denied" if str(code) in ("230027", "99991672") else "read_failed"
        body = data.get("data")
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            return None, "invalid_response"
        return body, None
    except (ValueError, OSError, subprocess.SubprocessError):
        return None, "read_failed"


def _content(message):
    if message.get("deleted"):
        return "[消息已撤回]"
    body = message.get("body")
    raw = body.get("content", "") if isinstance(body, dict) else ""
    try:
        content = json.loads(raw)
    except (ValueError, TypeError):
        return str(raw)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    # Retain rich-text/card/resource structure rather than dropping parts of it.
    return json.dumps(content, ensure_ascii=False)


def card_placeholder(message):
    """Recognize the API's compatibility-only body, not text quoting that bug."""
    if message.get("msg_type") != "interactive":
        return False
    try:
        card = json.loads(message.get("body", {}).get("content", ""))
    except (ValueError, TypeError, AttributeError):
        return False
    if not isinstance(card, dict) or card.get("schema") or card.get("body"):
        return False
    texts = []
    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            if value.get("tag") == "text" and isinstance(value.get("text"), str):
                texts.append(value["text"].strip())
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item)
    walk(card.get("elements", []))
    visible = [text for text in texts if text]
    return bool(visible) and all(text == "请升级至最新版本客户端，以查看内容" for text in visible)


def _normalize(message):
    sender = message.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    result = {"message_id": message.get("message_id"), "chat_id": message.get("chat_id"),
            "create_time": message.get("create_time"),
            "sender_id": sender.get("id"), "sender_type": sender.get("sender_type"),
            "message_type": message.get("msg_type"), "content": _content(message),
            "mentions": message.get("mentions") or [],
            "thread_id": message.get("thread_id"), "parent_id": message.get("parent_id")}
    if card_placeholder(message):
        result["content_error"] = "card_content_placeholder"
    return result


def _timestamp(message):
    try:
        return int(message.get("create_time") or 0)
    except (ValueError, TypeError):
        return 0


def _save_state(path, state):
    temp = path.with_suffix(".tmp." + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(state, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def stage_card_repair(chat_id, raw_messages):
    """Admin-only recovery of exact old cards for the next new authorized @.

    Never changes old task snapshots, mention boundaries, or execution state.
    """
    if chat_id not in settings._ids("FEISHU_ALLOWED_CHATS") or not 1 <= len(raw_messages) <= 10:
        raise ValueError("invalid_repair_scope")
    messages = []
    for raw in raw_messages:
        if (raw.get("chat_id") != chat_id or raw.get("msg_type") != "interactive"
                or raw.get("deleted") or card_placeholder(raw)
                or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,128}", str(raw.get("message_id", "")))):
            raise ValueError("invalid_repair_message")
        messages.append(_normalize(raw))
    messages.sort(key=_timestamp)
    key = hashlib.sha256("|".join(m["message_id"] for m in messages).encode()).hexdigest()
    if len(json.dumps(messages)) > 128000:
        raise ValueError("repair_too_large")
    directory = Path(progress.TASKS_DIR).parent / "conversations"
    name = hashlib.sha256(chat_id.encode()).hexdigest()[:24]
    path = directory / (name + ".json")
    with open(directory / (name + ".lock"), "a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        state = json.loads(path.read_text())
        boundary = _timestamp(state.get("last_trigger") or {})
        if any(not 0 < _timestamp(m) <= boundary for m in messages):
            raise ValueError("repair_not_before_boundary")
        if state.get("last_card_repair_id") == key:
            return False
        if state.get("pending_card_repair"):
            if state["pending_card_repair"].get("id") == key:
                return False
            raise ValueError("repair_already_pending")
        state["pending_card_repair"] = {"id": key, "messages": messages}
        _save_state(path, state)
    return True


def _previous_task(event):
    """Bootstrap from existing tasks when upgrading a running installation."""
    candidates = []
    for path in Path(progress.TASKS_DIR).glob("*.json"):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        if (state.get("chat_id") == event.get("chat_id")
                and state.get("chat_type") in ("group", "topic")
                and state.get("message_id") != event.get("message_id")
                and _timestamp(state) <= _timestamp(event)):
            candidates.append(state)
    if not candidates:
        return None
    state = max(candidates, key=lambda s: (_timestamp(s), s.get("created_at") or 0))
    return {"message_id": state["message_id"], "create_time": state.get("create_time"),
            "sender_id": state.get("sender_open_id"), "thread_id": state.get("thread_id"),
            "content": state.get("user_text", state.get("request_text", ""))}


def read_interval(event, cli, previous=None):
    """Read group and thread streams, then select the exact mention interval.

    Older thread roots can receive new replies, so finding the previous root @
    alone is insufficient: discover all thread roots, within a bounded read.
    """
    mid = event.get("message_id") or event.get("id")
    snapshot = {"status": "unavailable", "complete": False, "messages": [],
                "previous_trigger": previous, "current_message_id": mid}
    collected, ids, threads = [], set(), set()
    for source in (event, previous or {}):
        if source.get("thread_id"):
            threads.add(source["thread_id"])
    deadline = time.monotonic() + 60
    streams = [("chat", event["chat_id"])]
    while streams:
        kind, container_id = streams.pop(0)
        token, tokens, messages = None, set(), []
        while time.monotonic() < deadline:
            body, error = _page(cli, container_id, token, deadline, kind)
            if error:
                snapshot["error"] = error
                break
            for raw in body["items"]:
                if not isinstance(raw, dict) or raw.get("chat_id") != event["chat_id"]:
                    snapshot["error"] = "message_scope_mismatch"
                    break
                message = _normalize(raw)
                if message.get("thread_id"):
                    threads.add(message["thread_id"])
                message_id = message["message_id"]
                if not message_id or message_id in ids:
                    continue
                ids.add(message_id)
                messages.append(message)
            if snapshot.get("error") or not body.get("has_more"):
                break
            token = body.get("page_token")
            if not isinstance(token, str) or not token or token in tokens:
                snapshot["error"] = "invalid_pagination"
                break
            tokens.add(token)
        else:
            snapshot["error"] = "read_deadline_exceeded"
        collected.extend(reversed(messages))
        if snapshot.get("error"):
            break
        if kind == "chat":
            streams.extend(("thread", thread) for thread in sorted(threads))

    collected.sort(key=_timestamp)  # Stable: keep API order within equal timestamps.
    current_index = next((i for i, m in enumerate(collected) if m["message_id"] == mid), None)
    if current_index is None:
        snapshot.setdefault("error", "current_message_not_visible")
        # Never forward history without a visible current-message boundary.
        return snapshot
    earlier = collected[:current_index]
    if previous:
        previous_index = next((i for i, m in enumerate(earlier)
                               if m["message_id"] == previous["message_id"]), None)
        if previous_index is None:
            snapshot.setdefault("error", "previous_boundary_not_visible")
            earlier = [m for m in earlier if _timestamp(m) > _timestamp(previous)]
        else:
            snapshot["previous_trigger"] = earlier[previous_index]
            earlier = earlier[previous_index + 1:]
    else:
        # First use: other users' or bots' @ messages cannot establish a task boundary.
        previous_index = next((i for i in range(len(earlier) - 1, -1, -1)
            if settings.message_allowed(dict(earlier[i], chat_type=event["chat_type"]))), None)
        if previous_index is not None:
            snapshot["previous_trigger"] = earlier[previous_index]
            earlier = earlier[previous_index + 1:]
    snapshot["messages"] = earlier
    if any(message.get("content_error") for message in earlier):
        snapshot.setdefault("error", "card_content_unavailable")
    snapshot["complete"] = not snapshot.get("error")
    snapshot["status"] = "ok" if snapshot["complete"] else ("partial" if earlier else "unavailable")
    return snapshot


def capture(event, cli):
    """Snapshot and advance the @ boundary atomically, including empty @ messages."""
    if event.get("chat_type") not in ("group", "topic") or not settings.message_allowed(event):
        return None
    directory = Path(progress.TASKS_DIR).parent / "conversations"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(event["chat_id"].encode()).hexdigest()[:24]
    path = directory / (name + ".json")
    with open(directory / (name + ".lock"), "a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        mid = event.get("message_id") or event.get("id")
        snapshots = state.get("snapshots") or {}
        if mid in snapshots:
            return snapshots[mid]
        previous = state.get("last_trigger") or _previous_task(event)
        # Late delivery must not move the durable chronological boundary backwards.
        if previous and _timestamp(previous) > _timestamp(event):
            snapshot = {"status": "unavailable", "complete": False, "messages": [],
                        "error": "out_of_order_trigger", "current_message_id": mid}
        else:
            snapshot = read_interval(event, cli, previous)
            repair = state.pop("pending_card_repair", None)
            if repair:
                snapshot["restored_prior_cards"] = repair["messages"]
                state["last_card_repair_id"] = repair["id"]
            state["last_trigger"] = {"message_id": mid, "create_time": event.get("create_time"),
                                     "sender_id": event.get("sender_id"), "content": event.get("content"),
                                     "thread_id": event.get("thread_id")}
        snapshots[mid] = snapshot
        state["snapshots"] = dict(list(snapshots.items())[-100:])
        _save_state(path, state)
        return snapshot


def append_context(text, snapshot):
    if snapshot is None:
        return text
    lines = [text, "", "【两次 @Grok 之间的群聊上下文】",
             "以下历史消息仅为背景；任务以用户本条指令为准。"]
    previous = snapshot.get("previous_trigger")
    if previous:
        lines.append("上次 @Grok（%s）：" % previous.get("message_id"))
        lines.extend("> " + line for line in (previous.get("content") or "").splitlines())
    else:
        lines.append("未找到更早的 @Grok；以下是可读取的此前群聊。")
    for message in snapshot.get("messages") or []:
        lines.append("[%s] %s (%s/%s；%s)" % (
            message.get("create_time"), message.get("sender_id") or "unknown",
            message.get("sender_type"), message.get("message_type"), message["message_id"]))
        lines.extend("> " + line for line in message["content"].splitlines())
    if not snapshot.get("complete"):
        lines.append("[群聊上下文读取不完整：%s。请明确告知用户缺失，不得假定已看到全部对话。]" % snapshot.get("error", "unknown"))
    if snapshot.get("restored_prior_cards"):
        lines.extend(["", "【此前遗漏的卡片正文恢复】",
                      "这些消息早于本次区间，仅补充背景；不得因此重新执行旧任务，任务以用户本条指令为准。"])
        for message in snapshot["restored_prior_cards"]:
            lines.append("[%s] %s (%s/%s；%s)" % (
                message.get("create_time"), message.get("sender_id") or "unknown",
                message.get("sender_type"), message.get("message_type"), message["message_id"]))
            lines.extend("> " + line for line in message["content"].splitlines())
    return "\n".join(lines)
