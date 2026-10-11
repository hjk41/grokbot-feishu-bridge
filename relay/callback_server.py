"""Task-scoped HTTP callbacks; bind loopback and put HTTPS in front for deployment."""
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

import progress

_locks = {}
_registry = threading.Lock()
PUBLIC_FIELDS = ('task_id', 'status', 'message_id', 'chat_id', 'sender_open_id',
                 'progress_message_id', 'current_action', 'stop_requested',
                 'elapsed_sec', 'seq', 'events', 'summary_message_id', 'error_message_id')


@contextmanager
def operation_guard(task_id):
    with _registry:
        lock = _locks.setdefault(task_id, threading.RLock())
    with lock:
        yield


def attach_callback(state, summary):
    """Called under task_guard before a dispatch is persisted; no network."""
    if os.environ.get('RELAY_ROLE', 'full').lower() != 'inbound':
        return
    base = os.environ.get('BRIDGE_PUBLIC_URL', '').rstrip('/')
    parsed = urlsplit(base)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError('BRIDGE_PUBLIC_URL must be a public HTTPS base URL')
    state.setdefault('callback_token', secrets.token_urlsafe(32))
    state.setdefault('callback_expires_at', time.time() + 86400)
    summary['callback'] = {'url': base + '/v1/tasks/' + state['task_id'],
                           'token': state['callback_token']}
    summary['progress_instruction'] = 'Use worker_progress.py with this webhook payload; do not read inbound local task files or patch cards directly.'


def attach_to_summary(task_id, summary):
    if os.environ.get('RELAY_ROLE', 'full').lower() != 'inbound':
        return
    with progress.task_guard(task_id):
        state = progress.load_task(task_id)
        if not state:
            raise ValueError('unknown task')
        attach_callback(state, summary)
        progress.save_task(state)


def visible(state):
    return {key: state.get(key) for key in PUBLIC_FIELDS}


class CallbackHandler(BaseHTTPRequestHandler):
    server_version = 'GrokFeishuBridge'

    def log_message(self, fmt, *args):
        # Never log URL paths, request bodies, tokens or arbitrary exception text.
        pass

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def task(self):
        match = re.fullmatch(r'/v1/tasks/([A-Za-z0-9_-]{1,64})', self.path)
        if not match:
            return None
        return match.group(1)

    def authorized(self, state):
        token = str(state.get('callback_token') or '')
        supplied = self.headers.get('Authorization', '')
        return (bool(token) and time.time() < float(state.get('callback_expires_at') or 0)
                and hmac.compare_digest(('Bearer ' + token).encode(), supplied.encode()))

    def do_GET(self):
        if self.path == '/health':
            self.reply(200, {'ok': True, 'role': 'inbound'})
            return
        tid = self.task()
        state = progress.load_task(tid) if tid else None
        if not state or not self.authorized(state):
            self.reply(401, {'ok': False, 'error': 'unauthorized'})
            return
        self.reply(200, {'ok': True, 'task': visible(state)})

    def do_POST(self):
        tid = self.task()
        if not tid:
            self.reply(404, {'ok': False, 'error': 'unknown_endpoint'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536 or self.headers.get('Transfer-Encoding'):
                self.reply(413, {'ok': False, 'error': 'invalid_length'})
                return
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('object required')
            with operation_guard(tid):
                state = progress.load_task(tid)
                if not state or not self.authorized(state):
                    self.reply(401, {'ok': False, 'error': 'unauthorized'})
                    return
                command = data.get('command')
                worker = data.get('worker_id')
                if command not in ('start', 'update', 'done', 'fail') or not isinstance(worker, str) or not re.fullmatch(r'[a-f0-9]{32}', worker):
                    self.reply(400, {'ok': False, 'error': 'invalid_operation'})
                    return
                current = state.get('worker_id')
                for key in ('action', 'detail', 'summary', 'error'):
                    if data.get(key) is not None and (not isinstance(data[key], str) or len(data[key]) > 16000):
                        raise ValueError('invalid field')
                if current and current != worker:
                    self.reply(409, {'ok': False, 'error': 'already_claimed'})
                    return
                if progress.is_terminal(state.get('status')):
                    # Retry of the same terminal callback is idempotent, not reexecution.
                    expected = 'done' if command == 'done' else 'failed' if command == 'fail' else None
                    if current == worker and state.get('status') == expected:
                        self.reply(200, {'ok': True, 'task': visible(state)})
                    else:
                        self.reply(409, {'ok': False, 'error': 'terminal'})
                    return
                if command == 'start':
                    if state.get('status') == 'waiting' or not state.get('executable'):
                        self.reply(409, {'ok': False, 'error': 'not_executable'})
                        return
                    with progress.task_guard(tid):
                        state = progress.load_task(tid)
                        state['worker_id'] = worker
                        progress.save_task(state)
                    state = progress.start(tid, action=str(data.get('action') or '执行中'), detail=data.get('detail'))
                    if state.get('status') != 'running':
                        self.reply(409, {'ok': False, 'error': 'start_refused'})
                        return
                else:
                    if current != worker or state.get('status') != 'running':
                        self.reply(409, {'ok': False, 'error': 'start_required'})
                        return
                    if command == 'update':
                        state = progress.update(tid, action=data.get('action'), detail=data.get('detail'))
                    elif command == 'done':
                        state = progress.done(tid, summary_md=data.get('summary'), send_summary=bool(data.get('summary')))
                    else:
                        state = progress.fail(tid, error=data.get('error') or '任务失败', detail=data.get('detail'))
                self.reply(200, {'ok': True, 'task': visible(state)})
        except (ValueError, TypeError, json.JSONDecodeError):
            self.reply(400, {'ok': False, 'error': 'invalid_request'})
        except Exception:
            self.reply(500, {'ok': False, 'error': 'operation_failed'})


class CallbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


def start_server(host='127.0.0.1', port=8765):
    server = CallbackServer((host, port), CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
