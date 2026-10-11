"""Fixed, owner-verified native conversations for separate Feishu chats.

Current personal Grok accounts cannot create arbitrary sessions under one Bot.
Routes therefore bind each chat to a distinct owned Bot's main session (or an
already provisioned native session). There is deliberately no shared fallback.
"""
from __future__ import annotations

import json
import hashlib
import os
import uuid
from pathlib import Path

import progress
from grok_approvals import NativeClient, NativeError

ROOT = Path(__file__).resolve().parents[1]


def enabled():
    return os.environ.get("GROK_CHAT_ISOLATION_ENABLED") == "1"


def routes():
    if not enabled():
        return {}
    try:
        path = Path(os.environ["GROK_CHAT_ROUTES_FILE"]).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError()
        data = json.loads(path.read_text())
        if data.get("version") != 1:
            raise ValueError()
        rows = data["routes"]
        if not isinstance(rows, dict) or not rows:
            raise ValueError()
        identities = set()
        for chat, row in rows.items():
            if not isinstance(chat, str) or not chat.startswith("oc_") or not isinstance(row, dict):
                raise ValueError()
            agent, session = row["agent_id"], row["session_id"]
            if not isinstance(agent, str) or str(uuid.UUID(agent)) != agent:
                raise ValueError()
            if not isinstance(session, str) or len(session) > 200:
                raise ValueError()
            identity = (agent, session)
            if set(row) != {"agent_id", "session_id"}:
                raise ValueError()
            if identity in identities:
                raise ValueError()  # Two chats must never share a conversation.
            identities.add(identity)
        return rows
    except (OSError, ValueError, KeyError, TypeError):
        raise NativeError("native_routes_invalid") from None


def route(chat_id):
    row = routes().get(chat_id)
    if row is None:
        raise NativeError("native_chat_unmapped")
    return row


def client(agent_id):
    return NativeClient(os.environ.get("GROK_NATIVE_BASE_URL", "https://api2.cursor.sh"),
                        os.environ["GROK_NATIVE_AUTH_FILE"], agent_id)


def validate():
    if not enabled():
        return
    if os.environ.get("RELAY_ROLE", "full").lower() != "inbound":
        raise NativeError("native_isolation_requires_inbound")
    allowed = {c.strip() for c in os.environ.get("FEISHU_ALLOWED_CHATS", "").split(",") if c.strip()}
    rows = routes()
    if not allowed or set(rows) != allowed:
        raise NativeError("native_routes_allowlist_mismatch")
    if not os.environ.get("GROK_NATIVE_AUTH_FILE"):
        raise NativeError("native_auth_unavailable")


def bind(summary):
    """Bind and persist before input hashing/dispatch; never move an old task."""
    if not enabled() or summary.get("kind") != "message":
        return
    row = route(summary.get("chat_id"))
    with progress.task_guard(summary["task_id"]):
        task = progress.load_task(summary["task_id"])
        if not task or task.get("chat_id") != summary.get("chat_id"):
            raise NativeError("native_task_scope_mismatch")
        previous = task.get("grok_conversation")
        supplied = summary.get("grok_conversation")
        if (previous is not None and previous != row) or (supplied is not None and supplied != row):
            raise NativeError("native_task_route_changed")
        task["grok_conversation"] = dict(row)
        progress.save_task(task)
        summary["grok_conversation"] = dict(row)
        # New Bots may have an empty computer. Carry the small, audited callback
        # helper inside authenticated immutable input instead of assuming a repo
        # or copying credentials into that computer.
        source = (ROOT / "relay" / "worker_progress.py").read_text()
        summary["worker_helper"] = {"source": source,
                                    "sha256": hashlib.sha256(source.encode()).hexdigest()}


def post(summary, envelope):
    """Native message IDs survive retries; ACK is transport acceptance only."""
    try:
        row = route(summary.get("chat_id"))
        task = progress.load_task(summary.get("task_id")) or {}
        if summary.get("kind") == "card_action":
            # Authorized stop is already persisted. The running executor polls
            # its own callback; injecting a second native turn can interrupt it.
            if task.get("chat_id") != summary.get("chat_id") or task.get("grok_conversation") != row:
                raise NativeError("native_task_scope_mismatch")
            return True, None
        if (task.get("chat_id") != summary.get("chat_id") or
                summary.get("grok_conversation") != row or task.get("grok_conversation") != row):
            raise NativeError("native_task_scope_mismatch")
        native = client(row["agent_id"])
        native.verify_owner()
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
            "grokbot-feishu-bridge/message/" + row["agent_id"] + "/" + row["session_id"] + "/" + summary["task_id"]))
        instructions = (ROOT / "prompts" / "standalone-routine.md").read_text()
        text = ("Feishu bridge task for this conversation only. Continue this chat's work. "
                "Do not use another chat's transcript as context. Follow the execution protocol "
                "below and report through its callback; do not send Feishu messages directly. "
                "After authenticated payload fetch, payload.worker_helper contains Python source "
                "and its SHA-256. Verify SHA-256 of the UTF-8 source, save it mode 600 as "
                "worker_progress.py in the same private per-run directory, then use THAT helper "
                "for start/update/show/done/fail. No existing worker checkout is required. "
                "A new Bot may have an empty computer: discover project resources and read "
                "handoff files before project work; do not assume previous Bot files or keys "
                "were copied.\n\n" + instructions +
                "\n\nAuthenticated task control envelope:\n" +
                json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
        body = {"agentId": row["agent_id"], "sessionId": row["session_id"],
                "messageId": message_id, "sentAtMs": str(int(task["created_at"] * 1000)), "text": text}
        for attempt in range(2):
            try:
                result = native.rpc("SendGrokBotUserMessage", body)
            except NativeError as error:
                if str(error) not in ("native_transport_failed", "native_http_502", "native_http_503", "native_http_504"):
                    raise
                # A lost ACK may already have started a turn. Query the same ID
                # before retrying; the retry also uses that ID, never a new one.
                result = native.rpc("GetGrokBotSendStatus", {
                    "agentId": row["agent_id"], "sessionId": row["session_id"], "messageId": message_id})
                if result.get("status") in (2, "GROK_BOT_SEND_STATUS_ACCEPTED"):
                    return True, None
                if attempt == 0 and result.get("status") in (1, "GROK_BOT_SEND_STATUS_NOT_FOUND"):
                    continue
                raise NativeError("native_delivery_unconfirmed")
            delivery = result.get("delivery")
            if result.get("refusal") or delivery in (4, "GROK_BOT_USER_MESSAGE_DELIVERY_REFUSED"):
                raise NativeError("native_message_refused")
            if result.get("dispatched") is True or delivery in (3, "GROK_BOT_USER_MESSAGE_DELIVERY_DUPLICATE"):
                progress.log("native message accepted; isolated chat", "task", summary["task_id"])
                return True, None
            raise NativeError("native_message_not_dispatched")
    except (NativeError, KeyError, OSError, ValueError, TypeError) as error:
        label = str(error) if isinstance(error, NativeError) else "native_dispatch_invalid"
        progress.log("native delivery failed", label)
        return False, label
    return False, "native_delivery_unconfirmed"
