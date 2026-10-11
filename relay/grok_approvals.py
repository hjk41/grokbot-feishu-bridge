"""Native Grok approvals, observed and resolved by the Mac ingress.

Uses the installed client's Connect JSON RPC contract. Only single-use allow
and deny are exposed; a webhook key is never used as an account credential.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import progress
import settings

STATE_DIR = Path(__file__).resolve().parent / "approvals"
ACTIONS = {"approval_allow_once": 1, "approval_deny": 2}
KINDS = {
    "auto-review-approval": "ResolveGrokBotAutoReviewApproval",
    "local-tool-permission": "ResolveGrokBotLocalToolPermission",
}
RESOLVED = {"approved", "denied", "always", "never", "expired", "cancelled",
            "canceled", "allow-once", "allow_once", "allowed"}
_manager = None
_managers = {}


class NativeError(Exception):
    """Only safe, fixed error labels may be logged or placed on cards."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NativeClient:
    def __init__(self, base_url, auth_file, agent_id):
        url = urllib.parse.urlsplit(base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise NativeError("invalid_native_origin")
        if url.path not in ("", "/") or url.query or url.fragment:
            raise NativeError("invalid_native_origin")
        self.base = base_url.rstrip("/")
        self.auth_file = Path(auth_file).expanduser()
        self.agent_id = agent_id
        self.opener = urllib.request.build_opener(NoRedirect())

    def rpc(self, method, body):
        # Reload the existing Mac client auth file on every request: no copied
        # token, no secret in argv, and client credential rotation is respected.
        try:
            token = json.loads(self.auth_file.read_text())["accessToken"]
            if not isinstance(token, str) or not token:
                raise ValueError()
        except Exception:
            raise NativeError("native_auth_unavailable") from None
        req = urllib.request.Request(
            self.base + "/aiserver.v1.GrokBotService/" + method,
            data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json", "Connect-Protocol-Version": "1"})
        try:
            with self.opener.open(req, timeout=12) as response:
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise NativeError("native_response_too_large")
                value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except urllib.error.HTTPError as ex:
            raise NativeError("native_http_%s" % ex.code) from None
        except NativeError:
            raise
        except Exception:
            raise NativeError("native_transport_failed") from None

    def verify_owner(self):
        # Ownership reads are safe to retry; a transient auth transport failure
        # must not discard a task before its message is even sent.
        for attempt in range(2):
            try:
                agents = self.rpc("ListGrokBotAgents", {}).get("agents", [])
                break
            except NativeError as error:
                if attempt or str(error) not in ("native_transport_failed", "native_http_502", "native_http_503", "native_http_504"):
                    raise
        if not any(a.get("agentId") == self.agent_id and a.get("viewerIsOwner") is True
                   for a in agents):
            raise NativeError("native_agent_owner_mismatch")

    def verify(self, automation_id):
        self.verify_owner()
        rows = self.rpc("ListGrokBotAgentAutomations", {"agentId": self.agent_id})
        if not any(a.get("automationId") == automation_id for a in rows.get("automations", [])):
            raise NativeError("native_routine_mismatch")

    def snapshot(self, session_id=""):
        # Paginate instead of losing a still-pending older request. A failed or
        # capped scan never turns an unseen request into an implicit approval.
        entries, before, generation = [], None, None
        deadline = time.monotonic() + 30
        for _ in range(50):
            args = {"agentId": self.agent_id, "sessionId": session_id, "limit": 200}
            if before is not None:
                args["beforeSeq"] = str(before)
                args["generation"] = generation
            page = self.rpc("ListGrokBotTranscriptEntries", args)
            if generation is not None and page.get("generation", 0) != generation:
                raise NativeError("native_generation_changed")
            generation = page.get("generation", 0)
            rows = page.get("entries", [])
            if not isinstance(rows, list):
                raise NativeError("native_invalid_page")
            entries.extend(rows)
            if len(rows) < 200:
                return entries
            next_before = min(int(e["seq"]) for e in rows)
            if before is not None and next_before >= before:
                raise NativeError("native_pagination_stalled")
            before = next_before
            if time.monotonic() >= deadline:
                break
        raise NativeError("native_scan_incomplete")

    def resolve(self, request, resolution):
        if resolution not in (1, 2):
            raise NativeError("invalid_decision")
        body = {"agentId": self.agent_id, "requestId": request["request_id"],
                "resolution": resolution}
        if request.get("session_id"):
            body["sessionId"] = request["session_id"]
        if request["kind"] == "local-tool-permission":
            body["entryId"] = request["entry_id"]
        else:
            body["approvalPlatform"] = "feishu"
            if resolution == 1 and request.get("command"):
                body["approvedCommand"] = request["command"]
        result = self.rpc(KINDS[request["kind"]], body)
        if result.get("dispatched") is not True or result.get("refusal"):
            raise NativeError("native_decision_not_dispatched")
        return result


def parse_entry(entry, agent_id, session_id=""):
    try:
        body = json.loads(base64.b64decode(entry["body"], validate=True))
        msg = body.get("message", {})
        kind = msg.get("type")
        if kind not in KINDS:
            return None
        data = msg.get("approval") if kind == "auto-review-approval" else msg.get("permission")
        if not isinstance(data, dict):
            return None
        request_id = data.get("requestId")
        entry_id = entry.get("entryId") or body.get("id")
        status = data.get("status")
        if (not isinstance(request_id, str) or not request_id or len(request_id) > 200
                or not isinstance(entry_id, str) or not entry_id or len(entry_id) > 200):
            return None
        # Unrecognized native states never create actionable cards.
        if status != "pending" and status not in RESOLVED:
            return None
        source = [agent_id, session_id, kind, request_id, entry_id]
        key = hashlib.sha256(json.dumps(source).encode()).hexdigest()[:32]
        immutable = {k: data.get(k) for k in ("requestId", "surface", "summary", "reason",
                                              "command", "tool", "machineId", "args")}
        digest = hashlib.sha256(json.dumps(immutable, sort_keys=True).encode()).hexdigest()
        return {"approval_id": key, "agent_id": agent_id, "session_id": session_id,
                "kind": kind, "request_id": request_id, "entry_id": entry_id,
                "native_status": status, "digest": digest,
                "summary": str(data.get("summary") or "Grok 请求执行一项操作")[:4000],
                "reason": str(data.get("reason") or "")[:4000],
                "command": str(data.get("command") or ""),
                "surface": str(data.get("surface") or data.get("tool") or kind)[:200],
                "timestamp_ms": int(body.get("timestampMs") or 0)}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def display_text(text, limit=6000):
    # Approval commands can carry callback credentials. Keep operation/path
    # visible, redact credential values, and never render a credential link.
    for key, value in os.environ.items():
        if len(value) >= 8 and any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "KEY")):
            text = text.replace(value, "[已隐藏]")
    text = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)((?:token|password|secret|api[_-]?key|webhook[_-]?key)"
                  r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", r"\1[已隐藏]", text)
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                  "[私钥已隐藏]", text, flags=re.S)
    return text[:limit] + ("\n[内容较长，余下请在 Grok App 查看]" if len(text) > limit else "")


def build_card(state):
    pending = state.get("native_status") == "pending" and not state.get("decision")
    labels = {"approved": "已允许", "denied": "已拒绝", "always": "已在 App 授权",
              "expired": "请求已过期", "cancelled": "请求已取消", "canceled": "请求已取消",
              "never": "已在 App 拒绝", "allow_once": "已允许", "allow-once": "已允许",
              "allowed": "已允许", "unavailable": "请求已不可用，请在 Grok App 查看"}
    label = "等待你审批" if pending else labels.get(state.get("native_status"), "审批已处理")
    if state.get("decision") and state.get("native_status") == "pending":
        label = "决定已提交，等待 Grok 确认"
    elements = [{"tag": "div", "text": {"tag": "plain_text", "content": "%s\n%s" % (label, display_text(state["summary"]))}}]
    if state.get("command"):
        # Plain text prevents commands from manufacturing card markdown/links.
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": display_text(state["command"])}})
    if state.get("reason"):
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": "原因：" + display_text(state["reason"], 3000)}})
    if state.get("error"):
        elements.append({"tag": "markdown", "content": "提交未确认：%s。请在 Grok App 核对；这里不会自动重试批准。" % state["error"]})
    if pending and not state.get("error"):
        columns = []
        for action, title, style in [("approval_allow_once", "允许本次", "primary"),
                                      ("approval_deny", "拒绝", "danger")]:
            if action == "approval_allow_once" and len(state.get("command", "")) > 6000:
                continue  # Never approve a partially displayed long operation.
            columns.append({"tag": "column", "width": "auto", "elements": [{
                "tag": "button", "text": {"tag": "plain_text", "content": title}, "type": style,
                "behaviors": [{"type": "callback", "value": {"action": action,
                    "approval_id": state["approval_id"], "nonce": state["nonce"]}}]}]})
        elements.append({"tag": "column_set", "flex_mode": "none", "columns": columns})
    elements.append({"tag": "markdown", "text_size": "notation", "content":
                     "Grok 原生请求 · %s · #%s%s" % (state["surface"], state["approval_id"][:8],
                        " · 任务 #%s" % state["task_id"][:6] if state.get("task_id") else "")})
    return {"schema": "2.0", "config": {"update_multi": True, "summary": {"content": "Grok · " + label}},
            "header": {"title": {"tag": "plain_text", "content": "Grok · " + label},
                       "template": "orange" if pending else "grey"}, "body": {"elements": elements}}


class ApprovalManager:
    def __init__(self, client, inbox_chat, inbox_user, state_dir=None):
        if not settings.principal_allowed(inbox_user, inbox_chat):
            raise NativeError("approval_inbox_not_allowed")
        self.client, self.inbox_chat, self.inbox_user = client, inbox_chat, inbox_user
        self.directory = Path(state_dir or STATE_DIR)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.ready = False

    def path(self, key):
        if not re.fullmatch(r"[a-f0-9]{32}", str(key)):
            raise NativeError("invalid_approval_id")
        return self.directory / (key + ".json")

    def load(self, key):
        try:
            return json.loads(self.path(key).read_text())
        except FileNotFoundError:
            return None

    def save(self, state):
        progress.atomic_write_json(str(self.path(state["approval_id"])), state)
        os.chmod(self.path(state["approval_id"]), 0o600)

    def route(self, request):
        # An exact task callback URL in the proposed command is useful evidence;
        # time proximity / the most recently active chat is not an association.
        prefix = os.environ.get("BRIDGE_PUBLIC_URL", "").rstrip("/") + "/v1/tasks/"
        matches = set(re.findall(re.escape(prefix) + r"([a-f0-9]{16})(?![a-f0-9])", request["command"])) if prefix.startswith("https://") else set()
        if len(matches) == 1:
            task = progress.load_task(next(iter(matches))) or {}
            if (task.get("sender_open_id") == self.inbox_user and
                    settings.principal_allowed(self.inbox_user, task.get("chat_id")) and
                    self.task_scope_matches(task, request) and
                    not progress.is_terminal(task.get("status"))):
                return task
        return {"chat_id": self.inbox_chat, "sender_open_id": self.inbox_user}

    def task_scope_matches(self, task, request):
        scope = task.get("grok_conversation")
        if scope is not None:
            return scope == {"agent_id": self.client.agent_id, "session_id": request["session_id"]}
        # Historical unbound tasks belong only to the original default Bot.
        if os.environ.get("GROK_CHAT_ISOLATION_ENABLED") == "1":
            return (self.client.agent_id == os.environ.get("GROK_APPROVAL_AGENT_ID")
                    and request["session_id"] == "")
        return True

    def task_wait(self, state, waiting):
        tid = state.get("task_id")
        if not tid:
            return
        with progress.task_guard(tid):
            task = progress.load_task(tid)
            if not task or progress.is_terminal(task.get("status")):
                return
            pending = task.setdefault("pending_approvals", {})
            if waiting:
                if pending.get(state["approval_id"]) == state["request_id"]:
                    return
                pending[state["approval_id"]] = state["request_id"]
            else:
                if pending.pop(state["approval_id"], None) is None:
                    return
                if not pending:
                    task["last_stage_at"] = time.time()
            progress.save_task(task)

    def sync(self, entries, session_id=""):
        with self.lock:
            observed = set()
            for entry in entries:
                native = parse_entry(entry, self.client.agent_id, session_id)
                if not native:
                    continue
                observed.add(native["approval_id"])
                state = self.load(native["approval_id"])
                if state is None:
                    if native["native_status"] != "pending":
                        continue  # Never replay old approvals / expired cards.
                    route = self.route(native)
                    state = {**native, "chat_id": route["chat_id"], "operator_id": self.inbox_user,
                             "nonce": uuid.uuid4().hex, "created_at": time.time()}
                    if route.get("task_id"):
                        state.update({k: route.get(k) for k in ("task_id", "message_id", "chat_type", "thread_id")})
                    self.save(state)  # Persist before external delivery.
                elif state["digest"] != native["digest"]:
                    # A changed proposed operation requires a fresh explicit click.
                    state.update(native)
                    state.update(nonce=uuid.uuid4().hex, decision=None, error=None, needs_patch=True)
                    self.save(state)
                changed = state.get("native_status") != native["native_status"]
                state["native_status"] = native["native_status"]
                if changed:
                    state["needs_patch"] = True
                    if native["native_status"] != "pending":
                        state.pop("error", None)
                    self.save(state)
                pending = state["native_status"] == "pending"
                if not state.get("card_message_id") and pending:
                    card = build_card(state)
                    if state.get("message_id"):
                        ok, mid, _ = progress.reply_interactive(state["message_id"], card,
                            reply_in_thread=state.get("chat_type") == "topic" or bool(state.get("thread_id")),
                            idempotency_key="approval-" + state["approval_id"])
                    else:
                        # Use create's UUID deduplication for crash/lost ACK retries.
                        ok, mid, _ = send_inbox(state, card)
                    if ok:
                        state["card_message_id"] = mid
                        state["needs_patch"] = False
                        self.save(state)
                if state.get("card_message_id") and state.get("needs_patch"):
                    ok, _, _ = progress.patch_interactive(state["card_message_id"], build_card(state), force=True)
                    if ok:
                        state["needs_patch"] = False
                        self.save(state)
                self.task_wait(state, pending)
            # Only a complete paginated snapshot reaches this method.
            # Deleted/cleared entries cannot remain actionable indefinitely.
            for path in self.directory.glob("*.json"):
                state = self.load(path.stem)
                if (state and state.get("agent_id") == self.client.agent_id and
                        state.get("session_id") == session_id and state["approval_id"] not in observed and
                        (state.get("native_status") == "pending" or
                         state.get("native_status") == "unavailable" and state.get("needs_patch"))):
                    state["native_status"] = "unavailable"
                    state["needs_patch"] = True
                    self.save(state)
                    if state.get("card_message_id"):
                        ok, _, _ = progress.patch_interactive(state["card_message_id"], build_card(state), force=True)
                        if ok:
                            state["needs_patch"] = False
                            self.save(state)
                    self.task_wait(state, False)

    def handle_card(self, event):
        value = event.get("action_value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return False
        if not isinstance(value, dict) or value.get("action") not in ACTIONS:
            return False
        if not self.ready:
            return False
        with self.lock:
            try:
                state = self.load(value.get("approval_id"))
                if (not state or state.get("agent_id") != self.client.agent_id or
                        event.get("operator_id") != state["operator_id"] or
                        not settings.principal_allowed(event.get("operator_id"), event.get("chat_id")) or
                        event.get("chat_id") != state["chat_id"] or
                        event.get("message_id") != state.get("card_message_id") or
                        value.get("nonce") != state["nonce"] or state.get("decision") or state.get("error")):
                    return False
                if value["action"] == "approval_allow_once" and len(state.get("command", "")) > 6000:
                    return False
                # Re-read the exact native entry immediately before resolving.
                rows = self.client.snapshot(state["session_id"])
                latest = next((r for e in rows if (r := parse_entry(e, self.client.agent_id, state["session_id"]))
                               and r["approval_id"] == state["approval_id"]), None)
                if not latest or latest["native_status"] != "pending" or latest["digest"] != state["digest"]:
                    self.sync(rows, state["session_id"])
                    return False
                # Persist intent first. On timeout, never blindly replay approval;
                # subsequent native snapshots reconcile actual app-side state.
                state["decision"] = ACTIONS[value["action"]]
                state["decision_at"] = time.time()
                state["needs_patch"] = True
                self.save(state)
                try:
                    self.client.resolve(latest, state["decision"])
                except NativeError as ex:
                    state["error"] = str(ex)
                    self.save(state)
                ok, _, _ = progress.patch_interactive(state["card_message_id"], build_card(state), force=True)
                if ok:
                    state["needs_patch"] = False
                    self.save(state)
                return not bool(state.get("error"))
            except NativeError:
                return False


def send_inbox(state, card):
    args = [progress.FEISHU, "im", "+messages-send", "--chat-id", state["chat_id"],
            "--msg-type", "interactive", "--content", json.dumps(card, ensure_ascii=False),
            "--idempotency-key", "approval-" + state["approval_id"]]
    rc, out, _ = progress._runner(args, None)
    raw = progress._parse_json_out(out) if out else {}
    mid = progress._extract_message_id(raw)
    return rc == 0 and bool(mid), mid, raw


def start(shutdown):
    global _manager, _managers
    if os.environ.get("GROK_APPROVALS_ENABLED") != "1":
        return None
    webhook = urllib.parse.urlsplit(os.environ["FEISHU_RELAY_WEBHOOK_URL"])
    match = re.fullmatch(r"/automations/webhook/([^/]+)", webhook.path)
    if not match:
        raise NativeError("native_webhook_path_invalid")
    client = NativeClient(os.environ.get("GROK_NATIVE_BASE_URL") or
                          "%s://%s" % (webhook.scheme, webhook.netloc),
                          os.environ["GROK_NATIVE_AUTH_FILE"], os.environ["GROK_APPROVAL_AGENT_ID"])
    _manager = ApprovalManager(client, os.environ["GROK_APPROVAL_INBOX_CHAT"],
                               os.environ["GROK_APPROVAL_OPERATOR_ID"])
    _managers = {client.agent_id: _manager}

    def observe():
        verified, last_error, last_logged = False, None, 0
        while not shutdown.is_set():
            try:
                if not verified:
                    client.verify(match.group(1))
                    verified = True
                    _manager.ready = True
                    progress.log("native approvals connected; single-use decisions enabled")
                import grok_sessions
                scopes = {(client.agent_id, "")}  # Retain old App/routine approvals.
                if grok_sessions.enabled():
                    scopes.update((r["agent_id"], r["session_id"]) for r in grok_sessions.routes().values())
                for agent_id, session_id in sorted(scopes):
                    if shutdown.is_set():
                        break
                    manager = _managers.get(agent_id)
                    if manager is None:
                        manager = ApprovalManager(grok_sessions.client(agent_id),
                            os.environ["GROK_APPROVAL_INBOX_CHAT"], os.environ["GROK_APPROVAL_OPERATOR_ID"])
                        _managers[agent_id] = manager
                    try:
                        if not manager.ready:
                            manager.client.verify_owner()
                            manager.ready = True
                        manager.sync(manager.client.snapshot(session_id), session_id)
                    except NativeError as error:
                        if str(error) in ("native_http_401", "native_http_403", "native_agent_owner_mismatch"):
                            manager.ready = False
                        progress.log("native conversation approvals unavailable", str(error))
                if last_error:
                    progress.log("native approvals recovered")
                last_error = None
            except Exception as ex:
                error = str(ex) if isinstance(ex, NativeError) else "approval_sync_failed"
                if error != last_error or time.time() - last_logged > 300:
                    progress.log("native approvals unavailable", error)
                    last_logged = time.time()
                last_error = error
                if error in ("native_http_401", "native_http_403"):
                    verified = False
                    for manager in _managers.values():
                        manager.ready = False
            shutdown.wait(max(3, float(os.environ.get("GROK_APPROVAL_POLL_SEC", "8"))))

    thread = threading.Thread(target=observe, name="grok-approvals", daemon=True)
    thread.start()
    return thread


def handle_card(event):
    # Managers share a private state directory; each rejects foreign agent IDs.
    managers = list(_managers.values()) if _managers else ([_manager] if _manager else [])
    return any(manager.handle_card(event) for manager in managers)
