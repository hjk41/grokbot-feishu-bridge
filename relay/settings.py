"""Configuration checks and fail-closed ingress authorization."""
import json
import os
from urllib.parse import urlsplit

_instance_file = None


def acquire_instance(directory):
    """Keep an exclusive relay lock until process exit (also guards history ingest)."""
    import fcntl
    global _instance_file
    os.umask(0o077)
    _instance_file = open(os.path.join(directory, ".relay.lock"), "a")
    try:
        fcntl.flock(_instance_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _instance_file.close()
        raise SystemExit("Another relay/history process owns this state directory")


def _ids(name):
    return {x.strip() for x in os.environ.get(name, "").split(",") if x.strip()}


def principal_allowed(user_id, chat_id):
    return bool(user_id and chat_id and
                user_id in _ids("FEISHU_ALLOWED_USERS") and
                chat_id in _ids("FEISHU_ALLOWED_CHATS"))


def message_allowed(event):
    if not isinstance(event, dict):
        return False
    if not principal_allowed(event.get("sender_id"), event.get("chat_id")):
        return False
    mid = event.get("message_id") or event.get("id")
    if not isinstance(mid, str) or not mid.startswith("om_"):
        return False
    if event.get("sender_type") != "user" or event.get("message_type") != "text":
        return False
    text = event.get("content")
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        return False
    if event.get("chat_type") == "p2p":
        return True
    if event.get("chat_type") not in ("group", "topic"):
        return False
    bot_id = os.environ.get("FEISHU_BOT_OPEN_ID", "").strip()
    return bool(bot_id and any(isinstance(m, dict) and m.get("id") == bot_id
                              for m in event.get("mentions") or []))


def card_allowed(event):
    if not isinstance(event, dict) or not event.get("event_id"):
        return False
    if not principal_allowed(event.get("operator_id"), event.get("chat_id")):
        return False
    value = event.get("action_value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    return (isinstance(value, dict) and bool(value.get("task_id")) and
            value.get("action") in ("stop", "request_stop"))


def validate_startup():
    if not _ids("FEISHU_ALLOWED_USERS") or not _ids("FEISHU_ALLOWED_CHATS"):
        raise SystemExit("Configure FEISHU_ALLOWED_USERS and FEISHU_ALLOWED_CHATS first")
    url = urlsplit(os.environ.get("FEISHU_RELAY_WEBHOOK_URL", "").strip())
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise SystemExit("Configure an HTTPS routine webhook URL without embedded credentials")
    if not os.environ.get("FEISHU_RELAY_WEBHOOK_KEY", "").strip():
        raise SystemExit("Configure FEISHU_RELAY_WEBHOOK_KEY first")
