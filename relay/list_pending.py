#!/usr/bin/env python3
"""Print one line per *executable* pending event file: path and a short summary.

Waiting (FIFO non-head) tasks are NEVER written here — only chat queue-head
dispatches create pending/*.json. Routine / list_pending must not see waiting work.
Failed-delivery pendings are scrubbed by chat_queue before fail() completes.
Exit 0.
"""
import json, os, sys
DIR = os.path.dirname(os.path.abspath(__file__))
PENDING = os.path.join(DIR, "pending")

def main():
    if not os.path.isdir(PENDING):
        return 0
    for name in sorted(os.listdir(PENDING)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(PENDING, name)
        try:
            with open(path) as f:
                s = json.load(f)
            # Defense: skip non-executable if somehow present
            if s.get("executable") is False:
                continue
            if s.get("status") == "waiting":
                continue
            kind = s.get("kind", "?")
            mid = s.get("message_id") or ""
            tid = s.get("task_id") or ""
            text = str(s.get("text") or s.get("action_value") or s.get("context") or "")[:80]
            text = str(text).replace("\n", " ")
            print(f"{path}\t{kind}\t{mid}\t{tid}\t{text}")
        except Exception as e:
            print(f"{path}\terror\t\t\t{e}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
