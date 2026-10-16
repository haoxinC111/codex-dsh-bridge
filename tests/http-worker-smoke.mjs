// Invoked by Python against its real temporary HTTP server; no DSH model calls.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { Worker } from '../windows/worker.mjs';

const [server, directory, workspace] = process.argv.slice(2);
const requests = [];
const config = { server, token: 'a'.repeat(64), pollMs: 3000 };
const controller = {
  query: async query => ({ items: [{ session_id: 'discovered-session', title: query.params.search }] }),
  prompt: async request => { requests.push(request); return { accepted: true }; },
};
const worker = new Worker(config, directory, controller);
await worker.pair('dsh-session');
await worker.tick();
await worker.tick();
assert.equal(requests.length, 1);
const name = 'x'.repeat(196) + '.log';
const artifact = path.join(workspace, name);
fs.writeFileSync(artifact, 'actual cross-language artifact');
await worker.report({ task_id: requests[0].requestId, outcome: 'passed', summary: 'Loopback HTTP smoke passed',
  commits: [], files: [artifact] }, 'dsh-session');
// Simulate exit after outbox persistence, before the upload.
worker.stop();
const restarted = new Worker(config, directory, controller);
await restarted.tick();
assert.equal(restarted.state.outbox, null);
assert.equal(restarted.state.active, null);
assert.equal(requests.length, 1);
restarted.stop();
console.log('PASS: actual HTTP pairing, task admission, restarted outbox and artifact transfer');
