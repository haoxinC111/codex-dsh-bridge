import importlib.util
import json
import pathlib
import tempfile
import threading
import urllib.request
import urllib.error
import subprocess
import unittest
import time
import os

ROOT = pathlib.Path(__file__).resolve().parents[1]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'bridge.py').exists(), '任务桥尚未实现')
        spec = importlib.util.spec_from_file_location('bridge', ROOT / 'bridge.py')
        self.b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.b)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = self.b.Store(pathlib.Path(self.tmp.name))
        self.thread = 'original-codex-thread'
        self.store.configure({'pair_id': 'codex-dsh', 'thread_id': self.thread,
                              'worker_ip': '100.64.0.2', 'host': '127.0.0.1',
                              'port': 18766, 'token': 'a' * 64, 'codex_cli': '/unused'})
        self.store.bind('dsh-session')

    def submit(self, **extra):
        return self.store.submit({'instructions': 'Run the requested test', **extra})

    def report(self, task, **extra):
        return self.store.report({'task_id': task['id'], 'session_id': 'dsh-session',
                                  'outcome': 'passed', 'summary': 'Test passed',
                                  'commits': {}, 'artifacts': [], **extra})

    def test_pair_cannot_be_silently_rebound(self):
        self.assertEqual(self.store.bind('dsh-session')['session_id'], 'dsh-session')
        with self.assertRaises(self.b.BridgeError):
            self.store.bind('another-session')

    def test_one_task_at_a_time_and_restart_preserves_the_claim(self):
        first, second = self.submit(), self.submit()
        self.assertEqual(self.store.next_task()['id'], first['id'])
        restarted = self.b.Store(pathlib.Path(self.tmp.name))
        self.assertEqual(restarted.next_task()['id'], first['id'])
        self.store.accept(first['id'], 'dsh-session')
        self.assertEqual(self.store.next_task()['id'], first['id'])
        self.report(first)
        self.assertEqual(self.store.next_task()['id'], second['id'])

    def test_duplicate_results_are_idempotent_but_conflicts_fail(self):
        task = self.submit()
        self.store.next_task()
        self.assertEqual(self.report(task), self.report(task))
        with self.assertRaises(self.b.BridgeError):
            self.report(task, summary='A different result')

    def test_cannot_report_unclaimed_task_or_from_another_session(self):
        task = self.submit()
        with self.assertRaises(self.b.BridgeError):
            self.report(task)
        self.store.next_task()
        with self.assertRaises(self.b.BridgeError):
            self.report(task, session_id='other')

    def test_passed_requires_requested_revisions(self):
        task = self.submit(revisions={'console': 'a' * 40})
        self.store.next_task()
        with self.assertRaises(self.b.BridgeError):
            self.report(task)
        self.report(task, commits={'console': 'a' * 40})

    def test_artifact_paths_cannot_escape_task_directory(self):
        task = self.submit()
        self.store.next_task()
        with self.assertRaises(self.b.BridgeError):
            self.report(task, artifacts=[{'name': '../outside', 'base64': 'eA=='}])
        result = self.report(task, artifacts=[{'name': 'test.log', 'base64': 'eA=='}])
        self.assertEqual(pathlib.Path(result['artifacts'][0]['path']).read_bytes(), b'x')

    def test_notifier_uses_bound_thread_and_never_auto_retries_uncertain_delivery(self):
        task = self.submit()
        self.store.next_task()
        self.report(task)
        calls = []
        def uncertain(argv):
            calls.append(argv)
            raise TimeoutError('CLI outcome unknown')
        self.b.notify_once(self.store, uncertain)
        self.b.notify_once(self.store, uncertain)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:4], ['queue', '--thread', self.thread])
        self.assertEqual(self.store.get_task(task['id'])['notification'], 'uncertain')

    def test_successful_queue_is_not_ack_and_ack_checks_thread(self):
        task = self.submit()
        self.store.next_task()
        self.report(task)
        self.b.notify_once(self.store, lambda argv: '{"queued":true}')
        self.assertEqual(self.store.get_task(task['id'])['notification'], 'queued')
        with self.assertRaises(self.b.BridgeError):
            self.store.ack(task['id'], 'wrong-thread')
        self.store.ack(task['id'], self.thread)
        self.assertEqual(self.store.get_task(task['id'])['notification'], 'acknowledged')

    def test_task_name_and_stage_are_optional_immutable_request_metadata(self):
        request = {'instructions': 'Read-only evidence', 'name': 'Console 可靠性修复', 'stage': '补充取证'}
        task = self.store.submit(request)
        self.assertEqual(task['request']['name'], request['name'])
        self.assertEqual(task['request']['stage'], request['stage'])
        self.assertEqual(self.store.submit(request, task['id'])['id'], task['id'])
        with self.assertRaises(self.b.BridgeError):
            self.store.submit({**request, 'stage': '复测'}, task['id'])
        legacy = self.submit()
        self.assertNotIn('name', legacy['request'])
        self.assertEqual(self.store.submit(legacy['request'], legacy['id'])['id'], legacy['id'])

    def test_notification_metadata_must_be_short_single_line_text(self):
        for field, value in [('name', ''), ('stage', None), ('name', 'a\nb'),
                             ('stage', 'a\rb'), ('name', 'a\x1bb'),
                             ('name', '名' * 121), ('stage', '阶' * 41)]:
            with self.subTest(field=field, value=value):
                with self.assertRaises(self.b.BridgeError):
                    self.submit(**{field: value})

    def test_notice_labels_each_task_and_requires_receive_before_reading_result(self):
        for outcome, label in [('passed', '通过'), ('failed', '未通过'), ('blocked', '受阻')]:
            with self.subTest(outcome=outcome):
                task = self.submit(name='Console 可靠性修复', stage='补充取证')
                self.store.next_task()
                self.report(task, outcome=outcome, summary='PRIVATE RESULT BODY')
                calls = []
                self.b.notify_once(self.store, lambda argv: calls.append(argv) or 'queued')
                message = calls[0][-1]
                self.assertIn('任务名称：Console 可靠性修复', message)
                self.assertIn('阶段：补充取证', message)
                self.assertIn(f'执行结果：{label}（{outcome}）', message)
                self.assertIn(f"#{task['seq']}", message.splitlines()[0])
                self.assertNotIn('PRIVATE RESULT BODY', message)
                self.assertNotIn('result_file:', message)
                command = json.loads(next(line for line in message.splitlines() if line.startswith('[')))
                self.assertEqual(command[-2:], ['receive', task['id']])
                self.assertIn('action=skip', message)

    def test_legacy_notice_uses_number_without_copying_task_instructions(self):
        task = self.store.submit({'instructions': 'PRIVATE ORIGINAL INSTRUCTIONS'})
        self.store.next_task()
        self.report(task)
        calls = []
        self.b.notify_once(self.store, lambda argv: calls.append(argv) or 'queued')
        self.assertIn(f"任务名称：Windows DSH 任务 #{task['seq']}", calls[0][-1])
        self.assertIn('阶段：执行', calls[0][-1])
        self.assertNotIn('PRIVATE ORIGINAL INSTRUCTIONS', calls[0][-1])

    def test_receive_cli_skips_acknowledged_results_without_reopening_report(self):
        task = self.submit()
        def receive(thread=self.thread):
            return subprocess.run(['python3', str(ROOT / 'bridge.py'), '--state', str(self.store.root),
                                   'receive', task['id']], capture_output=True, text=True,
                                  env={**os.environ, 'CODEX_THREAD_ID': thread})
        self.assertNotEqual(receive().returncode, 0)  # No result yet.
        self.store.next_task()
        self.report(task)
        first = receive()
        self.assertEqual(first.returncode, 0, first.stderr)
        data = json.loads(first.stdout)
        self.assertEqual(data['action'], 'process')
        self.assertEqual(pathlib.Path(data['result_file']).name, 'result.json')
        self.assertEqual(data['ack_command'][-2:], ['ack', task['id']])
        self.assertNotIn('summary', data)
        self.assertEqual(self.store.get_task(task['id'])['notification'], 'pending')
        wrong = receive('another-thread')
        self.assertNotEqual(wrong.returncode, 0)
        self.assertNotIn('result_file', wrong.stdout)
        self.store.ack(task['id'], self.thread)
        self.report(task)  # A repeated Windows upload must preserve the ACK.
        pathlib.Path(data['result_file']).unlink()
        for _ in range(2):
            duplicate = receive()
            self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
            data = json.loads(duplicate.stdout)
            self.assertEqual(data['action'], 'skip')
            self.assertEqual(data['reason'], 'already_acknowledged')
            self.assertNotIn('result_file', data)
            self.assertNotIn('ack_command', data)
            self.assertNotIn('result', data)
        self.assertNotEqual(receive('another-thread').returncode, 0)
        calls = []
        self.assertFalse(self.b.notify_once(self.store, lambda argv: calls.append(argv)))
        self.assertEqual(calls, [])

    def test_ack_during_queue_delivery_cannot_be_overwritten_by_late_receipt(self):
        task = self.submit()
        self.store.next_task()
        self.report(task)
        def queue_and_ack(argv):
            self.store.ack(task['id'], self.thread)
            return 'queued'
        self.b.notify_once(self.store, queue_and_ack)
        self.assertEqual(self.store.get_task(task['id'])['notification'], 'acknowledged')
        self.assertFalse(self.b.notify_once(self.store, queue_and_ack))

    def test_http_requires_both_worker_ip_and_token_and_checks_origin(self):
        self.assertTrue(hasattr(self.b, 'make_server'), 'HTTP transport not implemented')
        config = self.store.config()
        config.update(worker_ip='127.0.0.1', port=0)
        server = self.b.make_server(self.store, config)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:%d/v1/next' % server.server_address[1]
        def call(token, origin=None):
            headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            request = urllib.request.Request(url, data=b'{}', headers=headers)
            return urllib.request.urlopen(request)
        with self.assertRaises(urllib.error.HTTPError) as error:
            call('wrong')
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError):
            call(config['token'], 'https://foreign.example')
        with call(config['token']) as response:
            self.assertEqual(json.load(response), {'task': None})
        config['worker_ip'] = '100.64.0.2'
        with self.assertRaises(urllib.error.HTTPError):
            call(config['token'])

    def test_cli_initialization_preserves_existing_config_and_submits_task(self):
        state = pathlib.Path(self.tmp.name) / 'cli-state'
        def cli(*args):
            return subprocess.run(['python3', str(ROOT / 'bridge.py'), '--state', str(state), *args], capture_output=True, text=True, env={**os.environ, 'CODEX_THREAD_ID': self.thread})
        first = cli('init', '--thread', self.thread, '--host', '127.0.0.1', '--worker-ip', '127.0.0.1')
        self.assertIn('initialized', first.stdout, 'CLI initialization not implemented')
        before = (state / 'config.json').read_bytes()
        self.assertNotEqual(cli('init', '--thread', 'other', '--host', '127.0.0.1', '--worker-ip', '127.0.0.1').returncode, 0)
        self.assertEqual(before, (state / 'config.json').read_bytes())
        task_file = pathlib.Path(self.tmp.name) / 'task.json'
        task_file.write_text(json.dumps({'instructions': 'Read-only smoke test'}))
        result = cli('submit', '--file', str(task_file), '--session', 'dsh-session')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'], 'pending')

    def test_real_node_worker_over_http_delivers_persisted_outbox(self):
        workspace = pathlib.Path(self.tmp.name) / 'workspace'
        workspace.mkdir()
        task = self.submit(workspace=str(workspace), name='HTTP 兼容验证', stage='文件回传')
        query = self.store.create_query('sessions', {'search': 'real HTTP'}, self.thread)
        config = {**self.store.config(), 'worker_ip': '127.0.0.1', 'port': 0}
        server = self.b.make_server(self.store, config)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        result = subprocess.run(['node', str(ROOT / 'tests/http-worker-smoke.mjs'),
                                 'http://127.0.0.1:%d' % server.server_address[1],
                                 str(pathlib.Path(self.tmp.name) / 'worker'), str(workspace)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.store.get_task(task['id'])
        self.assertEqual(saved['notification'], 'pending')
        self.assertEqual(self.store.get_query(query['id'])['result']['value']['items'][0]['session_id'], 'discovered-session')
        self.assertEqual(self.store.worker_status()['protocol'], 5)
        artifact = saved['result']['artifacts'][0]
        self.assertEqual(pathlib.Path(artifact['path']).read_text(), 'actual cross-language artifact')

    def test_bootstrap_expires_and_is_disabled_after_pairing(self):
        store = self.b.Store(pathlib.Path(self.tmp.name) / 'bootstrap')
        store.configure({**self.store.config(), 'worker_ip': '127.0.0.1', 'port': 0})
        server = self.b.make_server(store, store.config())
        self.addCleanup(server.server_close)
        config = {**store.config(), 'port': server.server_address[1]}
        self.b.atomic_write(store.root / 'config.json', self.b.encode(config).encode())
        bundle = self.b.bundle(store)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(bundle['url']) as response:
            self.assertEqual(response.read(), (store.root / 'windows.zip').read_bytes())
        config = {**store.config(), 'bootstrap_expires': time.time() - 1}
        self.b.atomic_write(store.root / 'config.json', self.b.encode(config).encode())
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(bundle['url'])
        bundle = self.b.bundle(store)
        store.bind('dsh-session')
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(bundle['url'])

    def test_tasks_bind_each_origin_and_target_and_reject_route_changes(self):
        request = {'instructions': 'read-only task'}
        first = self.store.submit(request, thread_id='codex-a', session_id='dsh-a')
        second = self.store.submit(request, thread_id='codex-b', session_id='dsh-b')
        with self.assertRaises(self.b.BridgeError):
            self.store.next_task()  # v1 worker must never misroute a v2 task.
        self.assertEqual(self.store.next_task(protocol=2)['session_id'], 'dsh-a')
        with self.assertRaises(self.b.BridgeError):
            self.store.submit(request, first['id'], thread_id='codex-b', session_id='dsh-a')
        self.report(first, session_id='dsh-a')
        self.assertEqual(self.store.next_task(protocol=2)['id'], second['id'])
        self.report(second, session_id='dsh-b')
        calls = []
        for _ in range(2):
            self.b.notify_once(self.store, lambda argv: calls.append(argv) or 'queued')
        self.assertEqual([argv[3] for argv in calls], ['codex-a', 'codex-b'])

    def test_read_only_queries_do_not_wait_for_active_model_tasks(self):
        self.submit(); self.store.next_task()
        query = self.store.create_query('sessions', {'search': 'web-ui'}, 'codex-a')
        self.assertEqual(self.store.next_query()['id'], query['id'])
        result = {'ok': True, 'value': {'items': [{'session_id': 'found'}]}}
        self.store.finish_query(query['id'], result)
        self.store.finish_query(query['id'], result)
        self.assertEqual(self.store.get_query(query['id'])['result'], result)
        self.assertIsNone(self.store.next_query())
        with self.assertRaises(self.b.BridgeError):
            self.store.create_query('shell', {'cmd': 'arbitrary'}, 'codex-a')

    def test_explicit_update_bundle_preserves_existing_pairing(self):
        bundle = self.b.bundle(self.store, update=True)
        self.assertIn('/bootstrap/', bundle['url'])
        self.assertEqual(self.store.pairing()['session_id'], 'dsh-session')

    def test_creation_uses_fixed_identity_and_connects_only_the_initiator(self):
        self.store.heartbeat(5)
        query = self.store.create_query('create', {'workspace': 'D:\\work', 'full_access': True}, 'codex-new')
        sid = query['params']['session_id']
        self.assertTrue(sid.startswith('session-'))
        self.assertEqual(self.store.next_query()['params']['session_id'], sid)
        self.assertIsNone(self.store.connection_for('codex-new'))
        self.assertEqual(query['params']['permission_preset'], 'danger-full-access')
        with self.assertRaises(self.b.BridgeError):
            self.store.finish_query(query['id'], {'ok': True, 'value': {'session_id': sid, 'cwd': 'D:\\work'}})
        self.assertIsNone(self.store.connection_for('codex-new'))
        self.store.finish_query(query['id'], {'ok': True, 'value': {'session_id': sid, 'cwd': 'D:\\work',
            'permission_preset': 'danger-full-access', 'sandbox': 'danger-full-access', 'approval': 'never',
            'workspace_id': 'workspace-real', 'workspace_path': 'D:\\work', 'workspace_title': 'work', 'workspace_attached': True}})
        self.assertEqual(self.store.connection_for('codex-new')['session_id'], sid)
        self.assertIsNone(self.store.connection_for('codex-other'))
        connect = self.store.create_query('connect', {'session_id': 'old'}, 'codex-other')
        with self.assertRaises(self.b.BridgeError):
            self.store.finish_query(connect['id'], {'ok': True, 'value': {'session_id': 'wrong'}})
        self.assertIsNone(self.store.connection_for('codex-other'))

    def test_creation_refuses_old_worker_before_creating_a_remote_query(self):
        for protocol in (2, 3, 4):
            self.store.heartbeat(protocol)
            with self.assertRaisesRegex(self.b.BridgeError, 'protocol 5'):
                self.store.create_query('create', {'workspace': 'D:\\work'}, 'codex-new')
        self.assertIsNone(self.store.next_query())

    def test_creation_defaults_to_inherit_and_requires_matching_permission_receipt(self):
        self.store.heartbeat(5)
        query = self.store.create_query('create', {'workspace': 'D:\\work'}, 'codex-new')
        sid = query['params']['session_id']
        self.assertEqual(query['params']['permission_preset'], 'inherit')
        with self.assertRaises(self.b.BridgeError):
            self.store.finish_query(query['id'], {'ok': True, 'value': {
                'session_id': sid, 'permission_preset': 'danger-full-access'}})
        self.assertIsNone(self.store.connection_for('codex-new'))
        self.store.finish_query(query['id'], {'ok': True, 'value': {
            'session_id': sid, 'permission_preset': 'inherit', 'cwd': 'D:\\work',
            'workspace_id': 'workspace-real', 'workspace_path': 'D:\\work', 'workspace_title': 'work', 'workspace_attached': True}})
        self.assertEqual(self.store.connection_for('codex-new')['session_id'], sid)

    def test_http_old_worker_cannot_claim_creation_queued_before_a_downgrade(self):
        self.store.heartbeat(5)
        discovery = self.store.create_query('sessions', {'search': 'fixture'}, self.thread)
        creation = self.store.create_query('create', {'workspace': 'D:\\work'}, self.thread)
        config = {**self.store.config(), 'worker_ip': '127.0.0.1', 'port': 0}
        server = self.b.make_server(self.store, config)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        def poll(protocol):
            request = urllib.request.Request('http://127.0.0.1:%d/v2/query/next' % server.server_address[1],
                data=json.dumps({'protocol': protocol}).encode(),
                headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request) as response:
                return json.load(response)['query']
        self.assertEqual(poll(4)['id'], discovery['id'])
        self.store.finish_query(discovery['id'], {'ok': True, 'value': {'items': []}})
        for protocol in (2, 3, 4):
            self.assertIsNone(poll(protocol))
        self.assertEqual(poll(5)['id'], creation['id'])

    def test_creation_rejects_non_boolean_full_access_before_persisting(self):
        self.store.heartbeat(5)
        for choice in ('false', 'true', 1, None):
            with self.assertRaises(self.b.BridgeError):
                self.store.create_query('create', {'workspace': 'D:\\work', 'full_access': choice}, self.thread)
        self.assertIsNone(self.store.next_query())

    def test_create_cli_only_selects_full_access_with_explicit_flag(self):
        self.store.heartbeat(5)
        for flags, expected in (([], 'inherit'), (['--full-access'], 'danger-full-access')):
            run = subprocess.run([os.sys.executable, str(ROOT / 'bridge.py'), '--state', str(self.store.root),
                                  'create', '--workspace', 'D:\\work', '--wait', '0', *flags],
                                 env={**os.environ, 'CODEX_THREAD_ID': self.thread}, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['params']['permission_preset'], expected)

    def test_unverified_workspace_creation_preserves_existing_connection(self):
        self.store.heartbeat(5)
        old = self.store.create_query('connect', {'session_id': 'old'}, 'codex-new')
        self.store.finish_query(old['id'], {'ok': True, 'value': {'session_id': 'old'}})
        for full_access in (False, True):
            query = self.store.create_query('create', {'workspace': 'D:\\work', 'full_access': full_access}, 'codex-new')
            value = {'session_id': query['params']['session_id'], 'cwd': 'D:\\work',
                     'permission_preset': query['params']['permission_preset']}
            if full_access:
                value.update(sandbox='danger-full-access', approval='never')
            for fields in ({}, {'workspace_id': 'ws', 'workspace_path': 'D:\\work', 'workspace_attached': False},
                           {'workspace_id': 'ws', 'workspace_path': 'D:\\other', 'workspace_attached': True}):
                with self.assertRaisesRegex(self.b.BridgeError, 'Workspace'):
                    self.store.finish_query(query['id'], {'ok': True, 'value': {**value, **fields}})
                self.assertEqual(self.store.connection_for('codex-new')['session_id'], 'old')

    def test_upgrade_pause_allows_only_the_upgrade_task_to_be_claimed(self):
        upgrade, after = self.submit(), self.submit()
        self.store.pause_dispatch(upgrade['id'])
        self.assertEqual(self.store.next_task()['id'], upgrade['id'])
        self.report(upgrade)
        self.assertIsNone(self.store.next_task())
        self.store.resume_dispatch()
        self.assertEqual(self.store.next_task()['id'], after['id'])


if __name__ == '__main__':
    unittest.main()
