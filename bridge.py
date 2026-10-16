#!/usr/bin/env python3
"""A single-device bridge with per-task session routing. Python 3.9+, standard library only."""
import argparse
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import ntpath
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_STATE = Path.home() / '.local/share/codex-dsh-bridge'
DEFAULT_CODEX = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
MAX_BODY = 32 * 1024 * 1024


class BridgeError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def require_text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise BridgeError('Invalid ' + name)
    return value


def revisions(value):
    if not isinstance(value, dict) or len(value) > 20:
        raise BridgeError('revisions/commits must be an object')
    for name, sha in value.items():
        require_text(name, 'repository name')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha):
            raise BridgeError('Use full lowercase commit SHAs')
    return value


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class Store:
    def __init__(self, directory):
        self.root = Path(directory).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / 'bridge.sqlite3'
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS pairing (singleton INTEGER PRIMARY KEY CHECK(singleton=1), session_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                  request TEXT NOT NULL, thread_id TEXT NOT NULL, session_id TEXT,
                  state TEXT NOT NULL DEFAULT 'pending', claimed INTEGER NOT NULL DEFAULT 0,
                  result TEXT, result_hash TEXT, notification TEXT NOT NULL DEFAULT 'none',
                  notification_detail TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS queries (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                  method TEXT NOT NULL, params TEXT NOT NULL, thread_id TEXT NOT NULL,
                  result TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker (
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1), protocol INTEGER, seen_at TEXT);
                CREATE TABLE IF NOT EXISTS connections (
                  thread_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, metadata TEXT NOT NULL, updated_at TEXT NOT NULL);
            ''')
        os.chmod(self.db, 0o600)

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.db), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def configure(self, config):
        target = self.root / 'config.json'
        if target.exists():
            raise BridgeError('Already initialized; existing pairing and credentials were preserved')
        atomic_write(target, encode(config).encode())

    def config(self):
        try:
            return json.loads((self.root / 'config.json').read_text())
        except FileNotFoundError:
            raise BridgeError('Run init first')

    def pairing(self):
        with self.connection() as db:
            row = db.execute('SELECT session_id FROM pairing WHERE singleton=1').fetchone()
        return {'pair_id': self.config()['pair_id'], 'thread_id': self.config()['thread_id'],
                'session_id': row['session_id'] if row else None}

    def bind(self, session_id):
        require_text(session_id, 'session_id')
        with self.connection() as db:
            row = db.execute('SELECT session_id FROM pairing WHERE singleton=1').fetchone()
            if row and row['session_id'] != session_id:
                raise BridgeError('Already paired to another DSH session; refusing to retarget tasks')
            db.execute('INSERT OR IGNORE INTO pairing VALUES (1, ?)', (session_id,))
        return self.pairing()

    def submit(self, request, task_id=None, thread_id=None, session_id=None):
        if not isinstance(request, dict) or set(request) - {'instructions', 'workspace', 'revisions', 'name', 'stage'}:
            raise BridgeError('Task fields: instructions, workspace, revisions, name, stage')
        require_text(request.get('instructions'), 'instructions', 50000)
        for key, limit in (('name', 120), ('stage', 40)):
            if key in request:
                value = require_text(request[key], key, limit)
                if not value.isprintable():
                    raise BridgeError(key + ' must be single-line printable text')
        # Keep omitted metadata absent so retries of pre-metadata tasks still match.
        request = {**request, 'revisions': revisions(request.get('revisions', {})),
                   'workspace': request.get('workspace', '')}
        if not isinstance(request['workspace'], str) or len(request['workspace']) > 2000:
            raise BridgeError('Invalid Windows workspace')
        task_id = task_id or str(uuid.uuid4())
        try:
            if str(uuid.UUID(task_id)) != task_id:
                raise ValueError()
        except (ValueError, TypeError):
            raise BridgeError('task_id must be a canonical UUID')
        pairing = self.pairing()
        thread_id = require_text(thread_id or pairing['thread_id'], 'reply thread')
        session_id = session_id or pairing['session_id']
        if session_id is not None:
            require_text(session_id, 'target session')
        with self.connection() as db:
            existing = db.execute('SELECT request,thread_id,session_id FROM tasks WHERE id=?', (task_id,)).fetchone()
            if existing and (existing['request'] != encode(request) or existing['thread_id'] != thread_id
                             or existing['session_id'] != session_id):
                raise BridgeError('task_id already refers to different instructions or session routing')
            db.execute('INSERT OR IGNORE INTO tasks (id,request,thread_id,session_id,created_at) VALUES (?,?,?,?,?)',
                       (task_id, encode(request), thread_id, session_id, now()))
        return self.get_task(task_id)

    @staticmethod
    def task_row(row):
        if row is None:
            raise BridgeError('Task not found')
        data = dict(row)
        data['request'] = json.loads(data['request'])
        data['result'] = json.loads(data['result']) if data['result'] else None
        return data

    def get_task(self, task_id):
        with self.connection() as db:
            return self.task_row(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def list_tasks(self):
        with self.connection() as db:
            return [self.task_row(row) for row in db.execute('SELECT * FROM tasks ORDER BY seq')]

    def next_task(self, protocol=1):
        with self.connection() as db:
            pair = db.execute('SELECT session_id FROM pairing WHERE singleton=1').fetchone()
            if not pair:
                return None
            row = db.execute("SELECT * FROM tasks WHERE state IN ('pending','accepted') ORDER BY seq LIMIT 1").fetchone()
            if not row:
                return None
            paused = self.dispatch_pause()
            if paused and paused.get('allow_task') != row['id']:
                return None
            if protocol < 2 and row['session_id'] not in (None, pair['session_id']):
                raise BridgeError('Upgrade Windows bridge to protocol 2 for other DSH sessions')
            # A lost HTTP response returns the same task. Never lease-expire an execution with possible external side effects.
            db.execute('UPDATE tasks SET claimed=1,session_id=COALESCE(session_id,?) WHERE id=?',
                       (pair['session_id'], row['id']))
            return self.task_row(db.execute('SELECT * FROM tasks WHERE id=?', (row['id'],)).fetchone())

    def dispatch_pause(self):
        path = self.root / 'dispatch-pause.json'
        return json.loads(path.read_text()) if path.exists() else None

    def pause_dispatch(self, allow_task=None):
        if allow_task:
            self.get_task(allow_task)
        atomic_write(self.root / 'dispatch-pause.json', encode({'allow_task': allow_task, 'at': now()}).encode())

    def resume_dispatch(self):
        (self.root / 'dispatch-pause.json').unlink(missing_ok=True)

    def heartbeat(self, protocol):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO worker VALUES (1, ?, ?)', (protocol, now()))

    def worker_status(self):
        with self.connection() as db:
            row = db.execute('SELECT protocol,seen_at FROM worker WHERE singleton=1').fetchone()
            return dict(row) if row else {'protocol': None, 'seen_at': None}

    def create_query(self, method, params, thread_id):
        require_text(thread_id, 'calling Codex thread')
        if method not in ('sessions', 'read', 'connect', 'create') or not isinstance(params, dict):
            raise BridgeError('Only sessions/read/connect/create operations are supported')
        if method == 'sessions':
            if set(params) - {'search'} or not isinstance(params.get('search', ''), str) or len(params.get('search', '')) > 500:
                raise BridgeError('Invalid session search')
        elif method == 'create':
            if (set(params) - {'workspace', 'full_access'} or not isinstance(params.get('workspace'), str)
                    or not ntpath.isabs(params['workspace'])):
                raise BridgeError('create requires an absolute Windows workspace')
            require_text(params['workspace'], 'workspace', 2000)
            if type(params.get('full_access', False)) is not bool:
                raise BridgeError('full_access must be an explicit boolean')
            if (self.worker_status().get('protocol') or 0) < 4:
                raise BridgeError('Upgrade Windows bridge to protocol 4 before creating a session')
            # Persist the identity before any network request. A lost reply adopts the same session.
            # Older workers always elevated new sessions, so inheritance requires protocol 4.
            params = {'workspace': params['workspace'], 'session_id': 'session-' + str(uuid.uuid4()),
                      'permission_preset': 'danger-full-access' if params.get('full_access') else 'inherit'}
        else:
            if set(params) - {'session_id', 'messages', 'before_seq'}:
                raise BridgeError('Invalid read fields')
            require_text(params.get('session_id'), 'session_id')
            if type(params.get('messages', 6)) is not int or not 1 <= params.get('messages', 6) <= 20:
                raise BridgeError('messages must be 1..20')
            if 'before_seq' in params and (type(params['before_seq']) is not int or params['before_seq'] < 0):
                raise BridgeError('Invalid before_seq')
        query_id = str(uuid.uuid4())
        with self.connection() as db:
            db.execute('INSERT INTO queries (id,method,params,thread_id,created_at) VALUES (?,?,?,?,?)',
                       (query_id, method, encode(params), thread_id, now()))
        return self.get_query(query_id)

    def connection_for(self, thread_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM connections WHERE thread_id=?', (thread_id,)).fetchone()
            return {**dict(row), 'metadata': json.loads(row['metadata'])} if row else None

    @staticmethod
    def query_row(row):
        if row is None:
            raise BridgeError('Query not found')
        return {**dict(row), 'params': json.loads(row['params']),
                'result': json.loads(row['result']) if row['result'] else None}

    def get_query(self, query_id):
        with self.connection() as db:
            return self.query_row(db.execute('SELECT * FROM queries WHERE id=?', (query_id,)).fetchone())

    def next_query(self):
        with self.connection() as db:
            row = db.execute('SELECT * FROM queries WHERE result IS NULL ORDER BY seq LIMIT 1').fetchone()
            return self.query_row(row) if row else None

    def finish_query(self, query_id, result):
        if not isinstance(result, dict) or type(result.get('ok')) is not bool or len(encode(result).encode()) > 1024 * 1024:
            raise BridgeError('Invalid query result')
        with self.connection() as db:
            row = db.execute('SELECT * FROM queries WHERE id=?', (query_id,)).fetchone()
            if row is None:
                raise BridgeError('Query not found')
            if row['result'] is not None:
                return {'received': True}
            if row['method'] in ('create', 'connect') and result['ok']:
                params = json.loads(row['params'])
                value = result.get('value', {})
                if not isinstance(value, dict) or value.get('session_id') != params['session_id']:
                    raise BridgeError('Session connection identity mismatch')
                if row['method'] == 'create':
                    expected = {'permission_preset': params['permission_preset']}
                    if params['permission_preset'] == 'danger-full-access':
                        expected.update(sandbox='danger-full-access', approval='never')
                    if any(value.get(key) != val for key, val in expected.items()):
                        raise BridgeError('Requested creation permissions were not verified; connection unchanged')
                db.execute('INSERT OR REPLACE INTO connections VALUES (?,?,?,?)',
                           (row['thread_id'], params['session_id'], encode(value), now()))
            # Read-only retries may observe newer session state. Keep the first returned snapshot.
            db.execute('UPDATE queries SET result=? WHERE id=? AND result IS NULL', (encode(result), query_id))
        return {'received': True}

    @staticmethod
    def check_owner(row, session_id):
        if not row or not row['claimed'] or row['session_id'] != session_id:
            raise BridgeError('Task not claimed by this session')

    def accept(self, task_id, session_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            self.check_owner(row, session_id)
            db.execute("UPDATE tasks SET state='accepted' WHERE id=? AND state='pending'", (task_id,))
        return {'accepted': True}

    def report(self, report):
        if not isinstance(report, dict) or set(report) != {'task_id','session_id','outcome','summary','commits','artifacts'}:
            raise BridgeError('Invalid result fields')
        require_text(report['summary'], 'summary', 50000)
        if report['outcome'] not in ('passed', 'failed', 'blocked'):
            raise BridgeError('Invalid outcome')
        revisions(report['commits'])
        files = report['artifacts']
        if not isinstance(files, list) or len(files) > 10:
            raise BridgeError('At most 10 artifacts')
        decoded, names = [], set()
        for file in files:
            if not isinstance(file, dict) or set(file) != {'name','base64'}:
                raise BridgeError('Invalid artifact')
            name = require_text(file['name'], 'artifact name', 200)
            if (len(name.encode('utf-8')) > 200 or name.lower() in ('.','..','result.json','task.json')
                    or any(c in name for c in '/\\') or any(ord(c)<32 for c in name) or name.lower() in names):
                raise BridgeError('Invalid or duplicate artifact name')
            names.add(name.lower())
            try:
                blob = base64.b64decode(file['base64'], validate=True)
            except (ValueError, TypeError):
                raise BridgeError('Invalid base64')
            if len(blob) > 8 * 1024 * 1024:
                raise BridgeError('Artifact exceeds 8 MiB')
            decoded.append((name, blob))
        digest = hashlib.sha256(encode(report).encode()).hexdigest()
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (report['task_id'],)).fetchone()
            self.check_owner(row, report['session_id'])
            if row['result_hash']:
                if row['result_hash'] != digest:
                    raise BridgeError('Conflicting result for this task; existing result preserved')
                return json.loads(row['result'])
            task = self.task_row(row)
            if report['outcome'] == 'passed' and any(report['commits'].get(k) != v for k,v in task['request']['revisions'].items()):
                raise BridgeError('Passed result does not match requested commit SHAs')
            directory = self.root / 'results' / task['id']
            artifacts = []
            for name, blob in decoded:
                path = directory / name
                atomic_write(path, blob)
                artifacts.append({'name': name, 'path': str(path), 'sha256': hashlib.sha256(blob).hexdigest()})
            result = {**report, 'artifacts': artifacts, 'received_at': now()}
            atomic_write(directory / 'task.json', encode(task).encode())
            atomic_write(directory / 'result.json', encode(result).encode())
            db.execute("UPDATE tasks SET state='reported',result=?,result_hash=?,notification='pending' WHERE id=?",
                       (encode(result), digest, task['id']))
        return result

    def claim_notification(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE notification='pending' ORDER BY seq LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE tasks SET notification='delivering' WHERE id=?", (row['id'],))
            return self.task_row(row)

    def finish_notification(self, task_id, state, detail):
        with self.connection() as db:
            db.execute("UPDATE tasks SET notification=?,notification_detail=? WHERE id=? AND notification='delivering'",
                       (state, detail[:12000], task_id))

    def recover_notifications(self):
        with self.connection() as db:
            db.execute("UPDATE tasks SET notification='uncertain',notification_detail='Service stopped during delivery; inspect chat before retry' WHERE notification='delivering'")

    def retry_notification(self, task_id):
        with self.connection() as db:
            row = db.execute('SELECT notification FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['notification'] not in ('queued','uncertain'):
                raise BridgeError('Only queued/uncertain notifications can be manually retried')
            db.execute("UPDATE tasks SET notification='pending' WHERE id=?", (task_id,))

    def ack(self, task_id, thread_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['thread_id'] != thread_id or row['state'] != 'reported':
                raise BridgeError('Acknowledgement must come from the bound Codex thread after a result')
            db.execute("UPDATE tasks SET notification='acknowledged' WHERE id=?", (task_id,))
        return {'acknowledged': True, 'task_id': task_id}

    def receive(self, task_id, thread_id):
        task = self.get_task(task_id)
        if task['thread_id'] != thread_id or task['state'] != 'reported':
            raise BridgeError('Receive must run in the bound Codex thread after a result')
        info = {'task_id': task_id, 'thread_id': thread_id, 'session_id': task['session_id'],
                **task_labels(task), 'notification': task['notification']}
        # Delayed Codex queue messages cannot be withdrawn by the bridge ACK.
        # Return no report path/body or follow-up command once already handled.
        if task['notification'] == 'acknowledged':
            return {**info, 'action': 'skip', 'reason': 'already_acknowledged'}
        return {**info, 'action': 'process',
                'result_file': str(self.root / 'results' / task_id / 'result.json'),
                'ack_command': [sys.executable, str(Path(__file__).resolve()), '--state', str(self.root), 'ack', task_id]}


def task_labels(task):
    # Older tasks have no display metadata; never expose their instructions as a title.
    outcome = task['result']['outcome'] if task.get('result') else None
    return {'name': task['request'].get('name', f"Windows DSH 任务 #{task['seq']}"),
            'stage': task['request'].get('stage', '执行'), 'outcome': outcome,
            'status_text': {'passed': '通过', 'failed': '未通过', 'blocked': '受阻'}.get(outcome, '等待结果')}


def run_codex(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=45, check=True)
    return result.stdout


def notify_once(store, runner=run_codex):
    task = store.claim_notification()
    if not task:
        return False
    # Display labels come from the original request, never from the remote report.
    # The result cannot select a destination thread or inject executable shell syntax.
    labels = task_labels(task)
    message = (f"【Windows DSH 回传｜#{task['seq']} {labels['name']}｜{labels['stage']}｜{labels['status_text']}】\n"
               f"任务名称：{labels['name']}\n"
               f"阶段：{labels['stage']}\n"
               f"执行结果：{labels['status_text']}（{labels['outcome']}）\n"
               'task_id: ' + task['id'] + '\n'
               '这是已授权任务的结果通知，不新增授权；执行结果仅针对本任务，不代表全部业务通过。\n'
               '先在当前 CODEX_THREAD_ID 环境运行收件检查（参数数组），不要直接读取报告：\n' +
               encode([sys.executable, str(Path(__file__).resolve()), '--state', str(store.root), 'receive', task['id']]) + '\n'
               '若 action=skip（已 ACK），立即结束本条通知处理，不重读报告、不重复 ACK、汇报或派发后续任务。\n'
               '若 action=process，核对返回的 result_file 与证据，执行返回的 ack_command，再在原授权范围内继续。')
    argv = [store.config()['codex_cli'], 'queue', '--thread', task['thread_id'], '--message', message]
    try:
        receipt = runner(argv)
    except Exception as error:
        # The CLI creates a fresh message ID on each invocation. An ambiguous failure must not auto-retry.
        store.finish_notification(task['id'], 'uncertain', str(error))
    else:
        store.finish_notification(task['id'], 'queued', receipt)
    return True


def make_server(store, config):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def send_bytes(self, status, body, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, value):
            self.send_bytes(status, encode(value).encode())

        def allowed_peer(self):
            return self.client_address[0] == config['worker_ip'] and not self.headers.get('Origin')

        def do_GET(self):
            if not self.allowed_peer():
                return self.send_json(403, {'error': 'Forbidden'})
            if self.path == '/health':
                return self.send_json(200, {'ok': True, 'service': 'codex-dsh-bridge',
                                           'dispatch_paused': store.dispatch_pause() is not None})
            current = store.config()
            path = '/bootstrap/' + current.get('bootstrap_code', '') + '/windows.zip'
            if (self.path == path and time.time() < current.get('bootstrap_expires', 0)
                    and (current.get('bootstrap_update') or store.pairing()['session_id'] is None)
                    and (store.root / 'windows.zip').exists()):
                return self.send_bytes(200, (store.root / 'windows.zip').read_bytes(), 'application/zip')
            self.send_json(404, {'error': 'Not found or bootstrap expired/already paired'})

        def do_POST(self):
            if not self.allowed_peer() or not hmac.compare_digest(
                    self.headers.get('Authorization', ''), 'Bearer ' + config['token']):
                return self.send_json(403, {'error': 'Forbidden'})
            try:
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise BridgeError('Expected application/json')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                    raise BridgeError('Invalid body size or transfer encoding')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise BridgeError('Expected JSON object')
                if self.path == '/v1/pair':
                    result = store.bind(payload.get('session_id'))
                elif self.path == '/v1/next':
                    protocol = payload.get('protocol') if payload.get('protocol') in (2, 3, 4) else 1
                    store.heartbeat(protocol)
                    result = {'task': store.next_task(protocol)}
                elif self.path == '/v2/query/next':
                    store.heartbeat(payload.get('protocol') if payload.get('protocol') in (3, 4) else 2)
                    result = {'query': store.next_query()}
                elif self.path == '/v2/query/result':
                    result = store.finish_query(payload.get('id'), payload.get('result'))
                elif self.path == '/v1/accepted':
                    result = store.accept(payload.get('task_id'), payload.get('session_id'))
                elif self.path == '/v1/result':
                    result = store.report(payload)
                else:
                    return self.send_json(404, {'error': 'Unknown endpoint'})
                self.send_json(200, result)
                if self.path not in ('/v1/next', '/v2/query/next'):
                    print(encode({'event': self.path, 'peer': self.client_address[0], 'at': now()}), flush=True)
            except (BridgeError, ValueError, TypeError) as error:
                self.send_json(400, {'error': str(error)})
            except Exception as error:
                print(encode({'event': 'server_error', 'error': str(error)}), file=sys.stderr, flush=True)
                self.send_json(500, {'error': 'Internal error; retry using the same task_id'})

        def log_message(self, *_args):
            pass  # Never log bootstrap URLs or Authorization headers.

    server = ThreadingHTTPServer((config['host'], config['port']), Handler)
    server.daemon_threads = True
    return server


def serve(store):
    import fcntl
    import signal
    # One notifier owns the CLI queue side effect. Do not recover another live notifier's claim.
    with (store.root / 'serve.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError('Bridge is already running')
        config = store.config()
        server = make_server(store, config)
        server.timeout = 1
        stop = threading.Event()
        store.recover_notifications()
        def notify_loop():
            while not stop.is_set():
                try:
                    notify_once(store)
                except Exception as error:
                    print(encode({'event': 'notifier_error', 'error': str(error)}), file=sys.stderr, flush=True)
                stop.wait(2)
        def terminate(*_args):
            stop.set()
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        worker = threading.Thread(target=notify_loop)
        worker.start()
        print(encode({'event': 'ready', 'host': config['host'], 'port': config['port']}), flush=True)
        try:
            while not stop.is_set():
                server.handle_request()
        finally:
            stop.set()
            server.server_close()
            worker.join()


def bundle(store, update=False):
    config = store.config()
    if store.pairing()['session_id'] and not update:
        raise BridgeError('Already paired; bootstrap downloads are disabled')
    source = Path(__file__).resolve().parent / 'windows'
    config['bootstrap_code'] = secrets.token_urlsafe(24)
    config['bootstrap_expires'] = time.time() + 3600
    config['bootstrap_update'] = update
    worker_config = {'server': 'http://%s:%s' % (config['host'], config['port']),
                     'token': config['token'], 'pairId': config['pair_id'], 'pollMs': 3000}
    target = store.root / 'windows.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('worker.mjs','setup.ps1'):
            archive.write(source / name, name)
        if update:
            archive.write(source / 'update-when-idle.ps1', 'update-when-idle.ps1')
        archive.writestr('config.json', encode(worker_config))
    os.chmod(target, 0o600)
    atomic_write(store.root / 'config.json', encode(config).encode())
    return {'url': worker_config['server'] + '/bootstrap/' + config['bootstrap_code'] + '/windows.zip',
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'expires_in_seconds': 3600}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=str(DEFAULT_STATE))
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--thread', default=os.environ.get('CODEX_THREAD_ID'))
    init.add_argument('--host', required=True)
    init.add_argument('--worker-ip', required=True)
    init.add_argument('--port', type=int, default=18766)
    init.add_argument('--pair-id', default='codex-dsh')
    init.add_argument('--codex-cli', default=DEFAULT_CODEX)
    sub.add_parser('serve')
    sub.add_parser('status').add_argument('--task')
    sub.add_parser('pause').add_argument('--allow-task')
    sub.add_parser('resume')
    sub.add_parser('bundle').add_argument('--update', action='store_true')
    submit = sub.add_parser('submit')
    submit.add_argument('--file', required=True)
    submit.add_argument('--task-id')
    submit.add_argument('--session', help='Existing Windows DSH session ID')
    submit.add_argument('--reply-thread', default=os.environ.get('CODEX_THREAD_ID'))
    sessions = sub.add_parser('sessions', help='Read-only Windows session discovery')
    sessions.add_argument('--search', default='')
    read = sub.add_parser('read', help='Read an existing session without running its model')
    read.add_argument('--session', required=True)
    read.add_argument('--messages', type=int, default=6)
    read.add_argument('--before-seq', type=int)
    connect = sub.add_parser('connect', help='Bind this Codex chat to an existing Windows session')
    connect.add_argument('--session', required=True)
    create = sub.add_parser('create', help='Create a new Windows session and bind this Codex chat')
    create.add_argument('--workspace', required=True)
    create.add_argument('--full-access', action='store_true',
                        help='Explicitly enable Full access for this new session; otherwise inherit DSH defaults')
    for command in (sessions, read, connect, create):
        command.add_argument('--wait', type=float, default=20)
    sub.add_parser('query-status').add_argument('query_id')
    for command in ('receive','ack','retry-notification'):
        sub.add_parser(command).add_argument('task_id')
    args = parser.parse_args(argv)
    store = Store(Path(args.state))
    if args.command == 'init':
        require_text(args.thread, 'thread ID')
        require_text(args.pair_id, 'pair ID')
        for address in (args.host, args.worker_ip):
            ip = ipaddress.ip_address(address)
            if not (ip.is_loopback or ip in ipaddress.ip_network('100.64.0.0/10')):
                raise BridgeError('Use a Tailscale IPv4 address or loopback')
        if not 1024 <= args.port <= 65535:
            raise BridgeError('Invalid port')
        store.configure({'pair_id': args.pair_id, 'thread_id': args.thread, 'host': args.host,
                         'worker_ip': args.worker_ip, 'port': args.port, 'token': secrets.token_hex(32),
                         'codex_cli': str(Path(args.codex_cli).resolve())})
        result = {'initialized': True, 'state': str(store.root), 'pairing': store.pairing()}
    elif args.command == 'serve':
        serve(store)
        return
    elif args.command == 'status':
        result = store.get_task(args.task) if args.task else {
            'pairing': store.pairing(), 'worker': store.worker_status(),
            'dispatch_pause': store.dispatch_pause(),
            'connection': store.connection_for(os.environ.get('CODEX_THREAD_ID')),
            'tasks': [{**{k: task[k] for k in ('id','thread_id','session_id','state','notification','created_at')},
                       **task_labels(task)} for task in store.list_tasks()]}
    elif args.command == 'bundle':
        result = bundle(store, update=args.update)
    elif args.command == 'pause':
        store.pause_dispatch(args.allow_task)
        result = {'paused': store.dispatch_pause()}
    elif args.command == 'resume':
        store.resume_dispatch()
        result = {'resumed': True}
    elif args.command == 'submit':
        require_text(args.reply_thread, 'reply thread (run inside Codex or specify --reply-thread)')
        connection = store.connection_for(args.reply_thread)
        session_id = args.session or (connection['session_id'] if connection else None)
        if not session_id:
            raise BridgeError('No DSH connection: use create --workspace or connect --session first')
        result = store.submit(json.loads(Path(args.file).read_text()), args.task_id, args.reply_thread, session_id)
    elif args.command in ('sessions', 'read', 'connect', 'create'):
        if not 0 <= args.wait <= 60:
            raise BridgeError('wait must be between 0 and 60 seconds')
        if args.command == 'sessions':
            params = {'search': args.search}
        elif args.command == 'create':
            params = {'workspace': args.workspace, 'full_access': args.full_access}
        else:
            params = {'session_id': args.session, 'messages': args.messages if args.command == 'read' else 1}
        if args.command == 'read' and args.before_seq is not None:
            params['before_seq'] = args.before_seq
        result = store.create_query(args.command, params, os.environ.get('CODEX_THREAD_ID'))
        deadline = time.monotonic() + args.wait
        while result['result'] is None and time.monotonic() < deadline:
            time.sleep(0.25)
            result = store.get_query(result['id'])
        if result['result'] is None:
            result['waiting_for_windows'] = True
            result['worker'] = store.worker_status()
    elif args.command == 'query-status':
        result = store.get_query(args.query_id)
    elif args.command == 'receive':
        result = store.receive(args.task_id, os.environ.get('CODEX_THREAD_ID'))
    elif args.command == 'ack':
        result = store.ack(args.task_id, os.environ.get('CODEX_THREAD_ID'))
    else:
        store.retry_notification(args.task_id)
        result = {'retry_requested': True}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (BridgeError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
