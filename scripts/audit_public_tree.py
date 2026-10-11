#!/usr/bin/env python3
"""Conservative public-tree check. Reports locations, never matched values.

This is a helper, not a complete secret scanner or a guarantee of privacy.
With --git-history every tracked blob in every reachable commit is inspected.
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "real-looking platform identifier": re.compile(r"\b(?:ou|oc|om|cli)_(?:x[a-f0-9]{24,}|[a-f0-9]{24,})\b", re.I),
    "private network address": re.compile(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
    "local user directory": re.compile(r"(?:[A-Z]:[\\/]Users[\\/][^\s\\/]+|/home/[a-z_][a-z0-9_-]*/|/Users/[A-Za-z0-9_-]+/)", re.I),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    "email address": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "long literal secret": re.compile(r"(?i)(?:app_secret|webhook_key|access_token)\s*[=:]\s*['\"][A-Za-z0-9_+/=-]{24,}['\"]"),
}
FORBIDDEN_PARTS = {"tasks", "queues", "pending", "processed", "credentials", "secrets", "__pycache__"}


def issues(name, data):
    path = Path(name)
    found = []
    if (set(path.parts) & FORBIDDEN_PARTS or path.name in {".env", "relay.env", "seen_ids.txt"}
            or path.suffix.lower() in {".log", ".ndjson", ".pem", ".key", ".pyc"}):
        found.append((name, 0, "private runtime/config file"))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        found.append((name, 0, "binary file requires manual review"))
        return found
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            if kind == "long literal secret" and "replace-with" in match.group(0):
                continue
            found.append((name, text.count("\n", 0, match.start()) + 1, kind))
    return found


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def candidates(history=False):
    if history:
        # HEAD/index and every reachable commit; historical removed files included.
        for commit in git("rev-list", "--all").decode().splitlines():
            for entry in git("ls-tree", "-r", "-z", commit).split(b"\0"):
                if not entry:
                    continue
                meta, name = entry.split(b"\t", 1)
                mode, kind, oid = meta.split()
                if kind != b"blob":
                    raise ValueError("submodule/non-blob requires manual review")
                yield name.decode(), git("cat-file", "blob", oid.decode())
        for name in git("ls-files", "-z").decode().split("\0"):
            if name:
                yield name, git("show", ":" + name)
    else:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or ".git" in path.relative_to(ROOT).parts:
                continue
            # These are locally generated; tracked variants are always rejected above.
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            yield str(path.relative_to(ROOT)), path.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-history", action="store_true")
    args = parser.parse_args()
    findings = set()
    checked = set()
    try:
        for name, data in candidates(args.git_history):
            key = (name, data)
            if key in checked:
                continue
            checked.add(key)
            findings.update(issues(name, data))
    except (ValueError, subprocess.CalledProcessError) as exc:
        print("Audit failed:", type(exc).__name__, file=sys.stderr)
        return 2
    for name, line, kind in sorted(findings):
        print(f"{name}:{line}: {kind}")
    print(f"Inspected {len(checked)} file versions; {len(findings)} finding(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
