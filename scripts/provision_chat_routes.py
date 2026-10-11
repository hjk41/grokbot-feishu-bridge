#!/usr/bin/env python3
"""Provision private, stable chat routes using the signed-in Mac owner account.

Load trusted .env with the launch script or shell first. This command does not
enable isolation, change any task, copy credentials or send chat messages.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))
from grok_approvals import NativeClient, NativeError
import progress


def provision(primary_chat, output):
    chats = {c.strip() for c in os.environ["FEISHU_ALLOWED_CHATS"].split(",") if c.strip()}
    if primary_chat not in chats:
        raise NativeError("primary_chat_not_allowed")
    main = os.environ["GROK_APPROVAL_AGENT_ID"]
    client = NativeClient(os.environ.get("GROK_NATIVE_BASE_URL", "https://api2.cursor.sh"),
                          os.environ["GROK_NATIVE_AUTH_FILE"], main)
    client.verify_owner()
    path = Path(output).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise NativeError("native_routes_invalid")
    data = json.loads(path.read_text()) if path.exists() else {"version": 1, "routes": {}}
    if data.get("version") != 1 or set(data["routes"]) - chats:
        raise NativeError("native_routes_invalid")
    primary = {"agent_id": main, "session_id": ""}
    if data["routes"].get(primary_chat, primary) != primary:
        raise NativeError("primary_route_already_bound")
    data["routes"][primary_chat] = primary
    agents = {a["agentId"]: a for a in client.rpc("ListGrokBotAgents", {}).get("agents", [])}
    for chat in sorted(chats):
        if chat in data["routes"]:
            row = data["routes"][chat]
            if row["agent_id"] not in agents or agents[row["agent_id"]].get("viewerIsOwner") is not True:
                raise NativeError("native_agent_owner_mismatch")
            continue
        agent_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "grokbot-feishu-bridge/chat/" + main + "/" + chat))
        if agent_id not in agents:
            description = (
                "Serve one isolated Feishu conversation for its owner, including FinSearch development, "
                "design, presentations, coordination and deployment when requested. Current user task "
                "is authoritative; quoted messages are background. Use shared project files and the "
                "owner's tools as needed, and read handoff documentation before project work. "
                "Follow the authenticated Feishu bridge task protocol included in every message; "
                "fetch complete payload, claim start, send stage and final callbacks. Worker helper "
                "is supplied inside each authenticated task payload. Do not run ingress "
                "listeners, create Feishu groups or send messages directly. Never copy another "
                "conversation's transcript or disclose keys. Keep native human approvals enabled.")
            result = client.rpc("CreateGrokBotTemporalAgent", {"agentId": agent_id,
                "name": "Grok · 飞书 " + chat[-6:], "description": description,
                "harness": 2, "introductionSuppressed": True, "kickstartRequested": False})
            if result.get("agent", {}).get("agentId") != agent_id:
                raise NativeError("created_agent_identity_mismatch")
            agents = {a["agentId"]: a for a in client.rpc("ListGrokBotAgents", {}).get("agents", [])}
        if agents.get(agent_id, {}).get("viewerIsOwner") is not True:
            raise NativeError("native_agent_owner_mismatch")
        data["routes"][chat] = {"agent_id": agent_id, "session_id": ""}
        progress.atomic_write_json(str(path), data)
        os.chmod(path, 0o600)
    identities = {(r["agent_id"], r["session_id"]) for r in data["routes"].values()}
    if len(identities) != len(chats):
        raise NativeError("native_routes_not_isolated")
    progress.atomic_write_json(str(path), data)
    os.chmod(path, 0o600)
    print("Private routes ready:", len(chats), "distinct native conversations; primary Bot preserved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-chat", required=True, help="Allowed chat that keeps the original Grok Bot")
    parser.add_argument("--output", default=str(ROOT / "runtime" / "chat-routes.json"))
    args = parser.parse_args()
    try:
        os.umask(0o077)
        provision(args.primary_chat, args.output)
    except (NativeError, KeyError, OSError, ValueError, TypeError) as error:
        label = str(error) if isinstance(error, NativeError) else "provision_configuration_invalid"
        raise SystemExit(label) from None
