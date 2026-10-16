import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

let module;
try { module = await import('../windows/worker.mjs'); } catch (e) { if (e.code !== 'ERR_MODULE_NOT_FOUND') throw e; }
function fixture(t, prompt = async () => ({ accepted: true })) {
  assert.ok(module, 'Windows worker is not implemented');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-bridge-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const task = { id: 'a-task', session_id: 'session-1', request: { instructions: 'Read only', workspace: dir, revisions: {} } };
  const requests = [];
  let offline = false;
  let reported = false;
  const transport = async (route, body) => {
    requests.push({ route, body });
    if (offline) throw new Error('offline');
    if (route === '/v1/pair') return { session_id: body.session_id, thread_id: 'mac-thread' };
    if (route === '/v1/next') return { task: reported ? null : structuredClone(task) };
    if (route === '/v1/result') reported = true;
    return { accepted: true };
  };
  const config = { server: 'http://100.64.0.1:18766', token: 'a'.repeat(64), pollMs: 3000 };
  const make = () => new module.Worker(config, dir, { prompt }, transport);
  return { dir, task, requests, make, offline: value => { offline = value; } };
}

test('polling admits once, restart resumes with the same requestId', async t => {
  const calls = [];
  const f = fixture(t, async (...args) => { calls.push(args); return { accepted: true }; });
  const worker = f.make();
  await worker.pair('session-1');
  await worker.tick();
  await worker.tick();
  await f.make().tick();
  assert.equal(calls.length, 2);
  assert.equal(calls[0][0].requestId, calls[1][0].requestId);
  assert.equal(calls[0][0].requestId, f.task.id);
  assert.equal(calls[0][0].mode, 'queue');
  assert.ok(calls[0][1] instanceof AbortSignal);
});

test('DSH admission must cross the persistence barrier before acceptance', async () => {
  const order = [], session = {};
  const controller = module.durableController({
    sessionController: { prompt: async () => { order.push('prompt'); return { accepted: true }; } },
    agents: { get: () => ({ session }) },
    sessions: { flush: async value => { assert.equal(value, session); order.push('flush'); return true; } },
  });
  assert.deepEqual(await controller.prompt({ sessionId: 'one' }, new AbortController().signal), { accepted: true });
  assert.deepEqual(order, ['prompt', 'flush']);
  const failing = module.durableController({
    sessionController: { prompt: async () => ({ accepted: true }) }, agents: { get: () => ({ session }) },
    sessions: { flush: async () => { throw new Error('disk full'); } },
  });
  await assert.rejects(failing.prompt({ sessionId: 'one' }), /disk full/);
});

test('invalid artifact names fail before saving an outbox and can be corrected', async t => {
  const f = fixture(t), worker = f.make();
  await worker.pair('session-1'); await worker.tick();
  const long = path.join(f.dir, 'x'.repeat(201)); fs.writeFileSync(long, 'log');
  const args = { task_id: f.task.id, outcome: 'passed', summary: 'OK', commits: [], files: [long] };
  await assert.rejects(worker.report(args, 'session-1'), /artifact name/i);
  assert.equal(worker.state.outbox, null);
  const valid = path.join(f.dir, 'run.log'); fs.renameSync(long, valid);
  assert.equal((await worker.report({ ...args, files: [valid] }, 'session-1')).saved_locally, true);
});

test('ambiguous prompt retries keep the original requestId', async t => {
  const calls = [];
  const f = fixture(t, async request => { calls.push(request); if (calls.length === 1) throw new Error('lost receipt'); return { accepted: true }; });
  const worker = f.make();
  await worker.pair('session-1');
  await assert.rejects(worker.tick(), /lost receipt/);
  await f.make().tick();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].requestId, calls[1].requestId);
});

test('result cannot be submitted by an unrelated session', async t => {
  const f = fixture(t), worker = f.make();
  await worker.pair('session-1'); await worker.tick();
  await assert.rejects(worker.report({ task_id: f.task.id, outcome: 'passed', summary: 'OK', commits: [], files: [] }, 'other'), /session/i);
});

test('offline result survives restart and contains actual artifact bytes', async t => {
  const f = fixture(t), worker = f.make();
  await worker.pair('session-1'); await worker.tick();
  const log = path.join(f.dir, 'run.log'); fs.writeFileSync(log, 'actual log');
  f.offline(true);
  const result = await worker.report({ task_id: f.task.id, outcome: 'passed', summary: 'OK', commits: [], files: [log] }, 'session-1');
  assert.equal(result.saved_locally, true);
  await assert.rejects(worker.tick(), /offline/);
  f.offline(false);
  await f.make().tick();
  const report = f.requests.findLast(r => r.route === '/v1/result').body;
  assert.equal(Buffer.from(report.artifacts[0].base64, 'base64').toString(), 'actual log');
});

test('artifact symlinks outside the task workspace are rejected', async t => {
  const f = fixture(t), worker = f.make();
  await worker.pair('session-1'); await worker.tick();
  fs.symlinkSync('/etc/hosts', path.join(f.dir, 'outside.log'));
  await assert.rejects(worker.report({ task_id: f.task.id, outcome: 'passed', summary: 'OK', commits: [], files: [path.join(f.dir, 'outside.log')] }, 'session-1'), /workspace/);
});

test('worker refuses a different task while the current task is unreported', async t => {
  const f = fixture(t), worker = f.make();
  await worker.pair('session-1'); await worker.tick();
  f.task.id = 'another-task';
  await assert.rejects(worker.tick(), /active task/);
});

test('tasks may target existing sessions other than the initial pairing', async t => {
  const calls = [], f = fixture(t, async request => { calls.push(request); return { accepted: true }; });
  const worker = f.make();
  await worker.pair('session-1');
  f.task.session_id = 'existing-session-2';
  await worker.tick();
  assert.equal(calls[0].sessionId, 'existing-session-2');
  await assert.rejects(worker.report({ task_id: f.task.id, outcome: 'passed', summary: 'OK', commits: [], files: [] }, 'session-1'), /session/);
  assert.equal((await worker.report({ task_id: f.task.id, outcome: 'passed', summary: 'OK', commits: [], files: [] }, 'existing-session-2')).saved_locally, true);
});

test('session discovery and history do not prompt or resume the target model', async () => {
  let prompts = 0;
  const controller = {
    prompt: () => { prompts++; throw new Error('must not run'); },
    list: async () => ({ items: [
      { sessionId: 'old', cwd: 'D:/projects', running: false, updatedAt: 123, projections: { values: { title: '请检查 api-service、web-ui' } } },
      { sessionId: 'fork', parentSessionId: 'old', cwd: 'D:/projects', running: false, projections: { values: { title: 'ordinary fork' } } },
      { sessionId: 'child', parentSessionId: 'old', origin: 'subagent', projections: { values: { title: 'web-ui' } } },
    ] }),
    inspect: async id => { assert.equal(id, 'old'); return { events: [
      { seq: 1, type: 'user/message', time: 100, data: { content: [{ type: 'text', text: 'pull the repos' }] } },
      { seq: 2, type: 'assistant/message', time: 101, data: { content: [{ type: 'text', text: 'Finished at exact SHAs' }] } },
      { seq: 3, type: 'turn/end', time: 102, data: { outcome: 'completed' } },
    ] }; },
  };
  const found = await module.querySessions(controller, { method: 'sessions', params: { search: 'web-ui' } }, new AbortController().signal);
  assert.equal(found.items.length, 1);
  assert.equal(found.items[0].session_id, 'old');
  const fork = await module.querySessions(controller, { method: 'sessions', params: { search: 'ordinary fork' } }, new AbortController().signal);
  assert.equal(fork.items[0]?.session_id, 'fork');
  const read = await module.querySessions(controller, { method: 'read', params: { session_id: 'old', messages: 1 } }, new AbortController().signal);
  assert.equal(read.messages.length, 1);
  assert.equal(read.messages[0].type, 'assistant/message');
  assert.equal(read.has_more, true);
  assert.equal(read.running, false);
  assert.equal(prompts, 0);
  await assert.rejects(module.querySessions(controller, { method: 'read', params: { session_id: 'child', messages: 1 } }, new AbortController().signal), /ordinary|visible/);
});

test('new session creation sets Full access before flushing and acknowledging its stable identity', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-create-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const calls = [];
  const controller = { create: async request => { calls.push(request); return { sessionId: request.sessionId }; } };
  const flushes = [];
  const permission = { permission_preset: 'danger-full-access', sandbox: 'danger-full-access', approval: 'never' };
  const query = { method: 'create', params: { workspace: dir, session_id: 'session-fixed', permission_preset: 'danger-full-access' } };
  const result = await module.querySessions(controller, query, new AbortController().signal,
    async id => { assert.deepEqual(flushes, ['permissions:session-fixed']); flushes.push(id); },
    async id => { assert.equal(calls.length, 1); flushes.push('permissions:' + id); return permission; });
  assert.equal(result.session_id, 'session-fixed');
  assert.equal(result.permission_preset, 'danger-full-access');
  assert.equal(result.approval, 'never');
  assert.deepEqual(calls, [{ cwd: dir, sessionId: 'session-fixed' }]);
  assert.deepEqual(flushes, ['permissions:session-fixed', 'session-fixed']);
  await assert.rejects(module.querySessions(controller, { ...query, params: { ...query.params, workspace: path.join(dir, 'missing') } }, new AbortController().signal, async () => {}, async () => permission), /ENOENT/);
  assert.equal(calls.length, 1);
});

test('Full access selection uses the session-local service and fails when the configured preset still asks', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-permission-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const session = { events: [] }, order = [];
  let approval = 'never';
  const controller = module.durableController({
    sessionController: { create: async ({ sessionId }) => ({ sessionId }) },
    agents: { get: () => ({ session }) },
    permissionPresets: {
      resolve: () => ({ sandbox: 'danger-full-access', approval }),
      set: (value, preset) => {
        assert.equal(value, session); order.push('permissions');
        session.events = [
          { type: 'permission/preset', data: { preset } },
          { type: 'sandbox/mode', data: { mode: preset } },
          { type: 'approval/policy', data: { policy: approval } },
        ];
      },
    },
    sessions: { flush: async value => { assert.equal(value, session); order.push('flush'); return true; } },
  });
  const query = { method: 'create', params: { workspace: dir, session_id: 'session-fixed', permission_preset: 'danger-full-access' } };
  const result = await controller.query(query, new AbortController().signal);
  assert.equal(result.permission_preset, 'danger-full-access');
  assert.deepEqual(order, ['permissions', 'flush']);
  approval = 'ask';
  await assert.rejects(controller.query(query, new AbortController().signal), /Full access/);
});

test('a stopped worker cannot overwrite a replacement worker outbox after a late flush', async t => {
  let release, entered;
  const started = new Promise(resolve => { entered = resolve; });
  const f = fixture(t, async () => { entered(); await new Promise(resolve => { release = resolve; }); return { accepted: true }; });
  const old = f.make(); await old.pair('session-1');
  const pending = old.tick(); await started;
  old.stop();
  const replacement = f.make();
  await replacement.report({ task_id: f.task.id, outcome: 'passed', summary: 'saved by replacement', commits: [], files: [] }, 'session-1');
  release();
  await assert.rejects(pending, /abort/i);
  const disk = JSON.parse(fs.readFileSync(path.join(f.dir, 'worker-state.json'), 'utf8'));
  assert.equal(disk.outbox.summary, 'saved by replacement');
});


test('inherit creation never calls the permission setter and persists before returning', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-inherit-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const calls = [];
  const controller = { create: async request => { calls.push('create'); return { sessionId: request.sessionId }; } };
  const query = { method: 'create', params: { workspace: dir, session_id: 'session-inherit', permission_preset: 'inherit' } };
  const result = await module.querySessions(controller, query, new AbortController().signal,
    async () => { calls.push('flush'); }, () => { throw new Error('must not change permissions'); });
  assert.deepEqual(calls, ['create', 'flush']);
  assert.equal(result.permission_preset, 'inherit');
  assert.equal(result.session_id, 'session-inherit');
  assert.equal(result.approval, undefined);
});

test('unknown or missing creation permission fails before creating a session', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-invalid-permission-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  let created = 0;
  const controller = { create: async () => { created++; } };
  for (const permission_preset of [undefined, 'unknown']) {
    await assert.rejects(module.querySessions(controller, {
      method: 'create', params: { workspace: dir, session_id: 'new', permission_preset },
    }, new AbortController().signal, async () => {}), /permission/i);
  }
  assert.equal(created, 0);
  assert.equal(module.protocol, 4);
});
