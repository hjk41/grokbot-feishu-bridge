#!/usr/bin/env python3
"""Cloud-side callback client. Only this small file is needed by the worker.

The payload is received through the authenticated Grok routine and contains a
task-scoped capability. Store it in a mode-600 per-run file, never in Git/logs.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call(payload, command, fields=None, *, allow_http=False):
    callback = payload.get('callback') or {}
    url, token = callback.get('url', ''), callback.get('token', '')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' and not (allow_http and parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')):
        raise ValueError('HTTPS callback required')
    task_id = str(payload.get('task_id') or '')
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or not token or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', task_id) or
            not parsed.path.endswith('/v1/tasks/' + task_id)):
        raise ValueError('invalid task callback')
    if command == 'show':
        body = None
    else:
        worker = payload.get('worker_id')
        if not re.fullmatch(r'[a-f0-9]{32}', str(worker or '')):
            raise ValueError('start this run before updating')
        body = json.dumps({'command': command, 'worker_id': worker, **(fields or {})}, ensure_ascii=False).encode()
    request = urllib.request.Request(url, body, {'Authorization': 'Bearer ' + token,
                                                'Content-Type': 'application/json',
                                                'User-Agent': 'grokbot-feishu-bridge/0.1 (+https://github.com/hjk41/grokbot-feishu-bridge)'},
                                     method='GET' if command == 'show' else 'POST')
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(4):
        try:
            with opener.open(request, timeout=30) as response:
                data = json.loads(response.read(131072))
            break
        except urllib.error.HTTPError as exc:
            # Same request/worker identity makes retries safe after a lost reply.
            # Authentication and ownership rejections must fail immediately.
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)
    if data.get('ok') is not True:
        raise ValueError('callback rejected')
    return data['task']


def save_payload(path, payload):
    fd, temporary = tempfile.mkstemp(prefix='.worker-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(payload, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('start', 'update', 'show', 'done', 'fail'))
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--action')
    parser.add_argument('--detail')
    parser.add_argument('--summary')
    parser.add_argument('--error')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.payload.is_symlink():
            raise ValueError('payload symlink refused')
        payload = json.loads(args.payload.read_text(encoding='utf-8'))
        if args.command == 'start' and not payload.get('worker_id'):
            # Save before POST so a lost response can be retried by this same run.
            payload['worker_id'] = uuid.uuid4().hex
            save_payload(args.payload, payload)
        fields = {key: getattr(args, key) for key in ('action', 'detail', 'summary', 'error')
                  if getattr(args, key) is not None}
        state = call(payload, args.command, fields)
        print(json.dumps(state, ensure_ascii=False))
        return 0
    except urllib.error.HTTPError as exc:
        print(json.dumps({'ok': False, 'error': 'callback_http_error', 'status': exc.code}))
        return 3
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
