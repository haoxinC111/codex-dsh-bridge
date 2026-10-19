// Optional local compatibility check against an already built DSH checkout. No model calls.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import { definitions, durableController } from '../windows/worker.mjs';

const root = process.argv[2];
assert.ok(root, 'Pass a built DSH checkout path');
const require = createRequire(path.join(root, 'packages/core/tools/package.json'));
const { Context } = await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')));
const { default: SystemPrompt } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-system-prompt')));
const { default: ToolRuntime } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')));
const ctx = new Context();
try {
  await ctx.plugin(SystemPrompt);
  await ctx.plugin(ToolRuntime);
  const worker = { pair: async () => ({ session_id: 'fixture', thread_id: 'fixture-mac' }) };
  for (const definition of definitions(worker)) ctx.tools.register(definition);
  const schemas = ctx.tools.schemas();
  assert.deepEqual(schemas.map(s => s.name).sort(), ['bridge_pair', 'bridge_report']);
  const result = await ctx.tools.execute({ name: 'bridge_pair', callId: 'fixture-call', arguments: {}, signal: new AbortController().signal });
  assert.equal(result.isError, false, JSON.stringify(result));
  assert.match(JSON.stringify(result.content), /fixture-mac/);
} finally { await ctx.fiber.dispose(); }
console.log('PASS: plugin tool registration, schema and rendering against actual DSH ToolRuntime');

// Exercise the real preset service and session projections, without a model,
// shell execution, global settings writes, or the user's persisted sessions.
const permissionRequire = createRequire(path.join(root, 'packages/interaction/permission-presets/package.json'));
const { default: SessionStore } = await import(pathToFileURL(permissionRequire.resolve('@deepseek-ai/dsh-session')));
const { default: Projections } = await import(pathToFileURL(permissionRequire.resolve('@deepseek-ai/dsh-session-projection')));
const { default: Permissions } = await import(pathToFileURL(permissionRequire.resolve('@deepseek-ai/dsh-permission-presets')));
const permissionCtx = new Context();
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-real-permission-'));
try {
  await permissionCtx.plugin(SessionStore);
  await permissionCtx.plugin(Projections);
  permissionCtx.provide('shell', { sandboxMode: 'workspace-write' });
  permissionCtx.provide('approval', { config: { policy: 'ask' } });
  await permissionCtx.plugin(Permissions, {});
  const selectedPreset = session => (typeof session.snapshotEvents === 'function' ? session.snapshotEvents() : session.events)
    .findLast(event => event.type === 'permission/preset')?.data.preset;
  const existing = permissionCtx.sessions.create('existing-user-session');
  const created = permissionCtx.sessions.create('session-full-access-fixture', { meta: { cwd: directory } });
  const inherited = permissionCtx.sessions.create('session-inherit-fixture', { meta: { cwd: directory } });
  const workspace = { id: 'workspace-fixture', path: fs.realpathSync(directory), title: 'fixture', sessionIds: [created.id, inherited.id] };
  permissionCtx.provide('workspaceRegistry', { create: async () => workspace, get: () => workspace });
  assert.equal(selectedPreset(created), 'workspace-write');
  const beforeInheritance = inherited.seq;
  const inheritController = durableController({
    get: name => permissionCtx.get(name),
    sessionController: { create: async ({ sessionId }) => ({ sessionId }) },
    agents: { get: () => ({ session: inherited }) },
    permissionPresets: permissionCtx.permissionPresets,
    sessions: { flush: async () => { assert.equal(selectedPreset(inherited), 'workspace-write'); return true; } },
  });
  const inheritedResult = await inheritController.query({ method: 'create', params: {
    workspace: directory, session_id: inherited.id, permission_preset: 'inherit',
  } }, new AbortController().signal);
  assert.equal(inheritedResult.permission_preset, 'inherit');
  assert.equal(inherited.seq, beforeInheritance, 'inherit must not append any permission-changing events');
  let flushes = 0;
  const controller = durableController({
    get: name => permissionCtx.get(name),
    sessionController: { create: async ({ sessionId }) => ({ sessionId }) },
    agents: { get: () => ({ session: created }) },
    permissionPresets: permissionCtx.permissionPresets,
    sessions: { flush: async () => { flushes++; assert.equal(selectedPreset(created), 'danger-full-access'); return true; } },
  });
  const query = { method: 'create', params: { workspace: directory, session_id: created.id, permission_preset: 'danger-full-access' } };
  const result = await controller.query(query, new AbortController().signal);
  assert.equal(result.approval, 'never');
  assert.equal(result.sandbox, 'danger-full-access');
  const beforeRetry = created.seq;
  await controller.query(query, new AbortController().signal);
  assert.equal(created.seq, beforeRetry, 'retry must not add duplicate permission events');
  assert.equal(flushes, 2);
  assert.equal(selectedPreset(existing), 'workspace-write');
  assert.equal(permissionCtx.permissionPresets.defaultPreset, 'workspace-write');
} finally {
  await permissionCtx.fiber.dispose();
  fs.rmSync(directory, { recursive: true, force: true });
}
console.log('PASS: real DSH inherit and explicit Full access, idempotence, persistence ordering and unchanged existing-session/default permissions');
