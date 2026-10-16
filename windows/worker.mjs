// Loaded by DSH's documented profile patch mechanism. No npm dependencies.
import * as fs from 'node:fs';
import * as fsp from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';

export const name = 'codex-dsh-bridge';
export const protocol = 4;
export const inject = ['sessionController', 'tools', 'agents', 'sessions', 'sessionPersistence', 'permissionPresets'];

function text(value, label, max = 200) {
  if (typeof value !== 'string' || !value.trim() || value.length > max) throw new Error(`Invalid ${label}`);
  return value;
}

function save(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temp = `${file}.${randomBytes(6).toString('hex')}.tmp`;
  const fd = fs.openSync(temp, 'wx', 0o600);
  try { fs.writeFileSync(fd, JSON.stringify(value)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  fs.renameSync(temp, file);
}

export function durableController(ctx) {
  const flush = async sessionId => {
    const agent = ctx.agents.get(sessionId);
    if (!agent || !await ctx.sessions.flush(agent.session)) throw new Error('DSH session has no active durability listener');
  };
  const fullAccess = sessionId => {
    const session = ctx.agents.get(sessionId)?.session;
    if (!session) throw new Error('Created session is not active');
    const preset = 'danger-full-access';
    const spec = ctx.permissionPresets.resolve(preset);
    if (spec.sandbox !== preset || spec.approval !== 'never') throw new Error('Full access preset must disable sandbox restrictions and approval prompts');
    // Called only for an explicitly requested Full access creation. Do not change
    // DSH defaults or permissions on sessions connected through discovery.
    ctx.permissionPresets.set(session, preset);
    // Released DSH exposes events; newer builds expose snapshotEvents(). Reading
    // the durable facts also avoids the changed current(events/session) signature.
    const events = typeof session.snapshotEvents === 'function' ? session.snapshotEvents() : session.events;
    const actual = {
      permission_preset: events.findLast(event => event.type === 'permission/preset')?.data.preset,
      sandbox: events.findLast(event => event.type === 'sandbox/mode')?.data.mode,
      approval: events.findLast(event => event.type === 'approval/policy')?.data.policy,
    };
    if (actual.permission_preset !== preset || actual.sandbox !== preset || actual.approval !== 'never') throw new Error('Full access selection did not take effect');
    return actual;
  };
  return { query: (query, signal) => querySessions(ctx.sessionController, query, signal, flush, fullAccess),
    async prompt(request, signal) {
    const result = await ctx.sessionController.prompt(request, signal);
    await flush(request.sessionId);
    return result;
  } };
}

export async function querySessions(controller, query, signal, flush, fullAccess) {
  if (query.method === 'create') {
    const { workspace, session_id: sessionId } = query.params;
    const preset = query.params.permission_preset;
    if (!['inherit', 'danger-full-access'].includes(preset)) throw new Error('Unknown creation permission choice');
    if (preset === 'danger-full-access' && typeof fullAccess !== 'function') throw new Error('Full access permission service unavailable');
    text(sessionId, 'new session ID');
    if (!path.isAbsolute(workspace) || !(await fsp.stat(workspace)).isDirectory()) throw new Error('Workspace must be an existing absolute directory');
    signal.throwIfAborted();
    const result = await controller.create({ sessionId, cwd: workspace });
    if (result.sessionId !== sessionId) throw new Error('Created session identity mismatch');
    signal.throwIfAborted();
    // Inherit means leave DSH's own defaults untouched; they may themselves be Full access.
    const permissions = preset === 'danger-full-access' ? await fullAccess(sessionId) : { permission_preset: 'inherit' };
    await flush(sessionId);
    return { session_id: result.sessionId, cwd: workspace, created: true, ...permissions };
  }
  // These APIs inspect persisted sessions without activating their models.
  const { items } = await controller.list({}, signal);
  // Ordinary forks also have a parentSessionId; only the origin marks subagents.
  const visible = items.filter(item => item.origin !== 'subagent');
  const rows = visible.map(item => ({ session_id: item.sessionId, title: item.projections?.values?.title ?? '',
    cwd: item.cwd ?? '', running: item.running, updated_at: item.updatedAt }));
  if (query.method === 'sessions') {
    const needle = (query.params.search ?? '').toLowerCase();
    let found = rows.filter(item => !needle || `${item.title}\n${item.cwd}`.toLowerCase().includes(needle));
    if (!found.length && needle && controller.search) {
      const search = await controller.search({ query: query.params.search }, signal);
      const snippets = new Map(search.items.map(item => [item.sessionId, item.snippet]));
      found = rows.filter(item => snippets.has(item.session_id)).map(item => ({ ...item, snippet: snippets.get(item.session_id) }));
    }
    return { items: found.slice(0, 50), has_more: found.length > 50 };
  }
  if (!['read', 'connect'].includes(query.method)) throw new Error('Unsupported session query');
  const row = rows.find(item => item.session_id === query.params.session_id);
  if (!row) throw new Error('Session is not a visible ordinary DSH session');
  const inspection = await controller.inspect(row.session_id, signal);
  const messages = inspection.events.filter(event => ['user/message', 'assistant/message'].includes(event.type)
    && (query.params.before_seq === undefined || event.seq < query.params.before_seq));
  const count = Math.min(20, Math.max(1, query.params.messages ?? 6));
  const selected = messages.slice(-count);
  // Return bounded user/assistant messages, not raw model requests or credential-bearing configs.
  const bounded = selected.map(event => {
    const data = JSON.stringify(event.data);
    return { seq: event.seq, type: event.type, time: event.time,
      data: data.length > 12000 ? data.slice(0, 12000) : event.data, truncated: data.length > 12000 };
  });
  const title = inspection.events.findLast(event => event.type === 'session/title')?.data?.title;
  const turn = inspection.events.findLast(event => event.type === 'turn/end' || event.type === 'turn/start');
  return { ...row, title: title ?? row.title, messages: bounded, has_more: messages.length > count,
    next_before_seq: selected[0]?.seq ?? null, last_turn_event: turn ? { type: turn.type, time: turn.time, data: turn.data } : null };
}

export class Worker {
  constructor(config, directory, controller, transport) {
    this.config = config;
    const url = new URL(config.server);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('Invalid bridge server origin');
    if (!/^[a-f0-9]{64}$/.test(config.token)) throw new Error('Invalid bridge credential');
    if (!Number.isInteger(config.pollMs) || config.pollMs < 1000) throw new Error('pollMs must be >= 1000');
    this.directory = directory;
    this.file = path.join(directory, 'worker-state.json');
    this.state = fs.existsSync(this.file) ? JSON.parse(fs.readFileSync(this.file, 'utf8')) : { sessionId: null, active: null, outbox: null };
    this.controller = controller;
    this.abort = new AbortController();
    this.transport = transport ?? (async (route, body, signal) => {
      const response = await fetch(new URL(route, config.server), {
        method: 'POST', redirect: 'error',
        headers: { 'Authorization': `Bearer ${config.token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: AbortSignal.any([this.abort.signal, AbortSignal.timeout(20000), ...(signal ? [signal] : [])]),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(`Bridge ${response.status}: ${result.error ?? 'request failed'}`);
      return result;
    });
    this.busy = false;
    this.admittedTask = null;
    this.acknowledgedTask = null;
  }

  persist() {
    // Async prompt/flush/file reads may finish after plugin disposal. Never let
    // that old generation overwrite the replacement worker's durable outbox.
    this.abort.signal.throwIfAborted();
    save(this.file, this.state);
  }

  async pair(sessionId, signal) {
    text(sessionId, 'calling session');
    if (this.state.sessionId && this.state.sessionId !== sessionId) throw new Error('Already paired to a different session');
    const result = await this.transport('/v1/pair', { session_id: sessionId }, signal);
    if (result.session_id !== sessionId) throw new Error('Pairing identity mismatch');
    this.state.sessionId = sessionId;
    this.persist();
    return result;
  }

  async tick() {
    if (this.busy || !this.state.sessionId || this.abort.signal.aborted) return;
    this.busy = true;
    try {
      if (this.state.outbox) {
        await this.transport('/v1/result', this.state.outbox);
        this.state.outbox = null;
        this.state.active = null;
        this.persist();
      }
      if (this.controller.query) {
        const { query } = await this.transport('/v2/query/next', { protocol });
        if (query) {
          let result;
          try {
            result = { ok: true, value: await this.controller.query(query,
              AbortSignal.any([this.abort.signal, AbortSignal.timeout(30000)])) };
          } catch (error) { result = { ok: false, error: String(error.message ?? error).slice(0, 2000) }; }
          await this.transport('/v2/query/result', { id: query.id, result });
        }
      }
      const { task } = await this.transport('/v1/next', { protocol });
      if (!task) return;
      text(task.session_id, 'target session');
      if (this.state.active && this.state.active.task.id !== task.id) throw new Error('Refusing to replace an unreported active task');
      if (!this.state.active) {
        this.state.active = { task, accepted: false };
        this.persist();
      }
      if (this.admittedTask !== task.id) {
        const prompt = [
          '【用户授权的 Mac Codex 任务】',
          `task_id: ${task.id}`,
          `结果回传的 Codex 会话: ${task.thread_id}`,
          `Windows 工作目录: ${task.request.workspace || '本次未指定；不得读取或修改业务文件'}`,
          `要求的各仓库 commit SHA: ${JSON.stringify(task.request.revisions)}`,
          task.request.instructions,
          '仅执行上述任务。实际测试前核对 commit SHA；失败、审批阻塞或缺少条件都如实报告，不自动扩大任务。',
          '结束时必须调用 bridge_report，传入本 task_id、passed/failed/blocked、证据摘要、实际 commits 数组和 files 数组。',
          '日志/截图文件须位于指定工作目录内。没有文件或仓库时分别传 []。bridge_report 会结束本轮并回传原 Mac 会话。',
        ].join('\n');
        // Admit once per process, including after restart to resume a cold session.
        // DSH deduplicates this exact requestId in its inbox and persisted log;
        // the controller waits for the durability barrier before returning.
        await this.controller.prompt({ requestId: task.id, sessionId: task.session_id, mode: 'queue',
          content: [{ type: 'text', text: prompt }] }, this.abort.signal);
        this.state.active.accepted = true;
        this.persist();
        this.admittedTask = task.id;
      }
      if (this.acknowledgedTask !== task.id) {
        await this.transport('/v1/accepted', { task_id: task.id, session_id: task.session_id });
        this.acknowledgedTask = task.id;
      }
    } finally { this.busy = false; }
  }

  async report(args, sessionId, signal) {
    const active = this.state.active;
    if (!active || active.task.session_id !== sessionId) throw new Error('No active task for this session');
    if (args.task_id !== active.task.id) throw new Error('Wrong task_id');
    if (!['passed', 'failed', 'blocked'].includes(args.outcome)) throw new Error('Invalid outcome');
    text(args.summary, 'summary', 50000);
    if (!Array.isArray(args.commits) || args.commits.length > 20 || !Array.isArray(args.files) || args.files.length > 10) throw new Error('Invalid commits or files');
    const commits = Object.create(null);
    for (const item of args.commits) {
      text(item.repo, 'repository');
      if (!/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(item.sha) || Object.hasOwn(commits, item.repo)) throw new Error('Invalid or duplicate commit');
      commits[item.repo] = item.sha;
    }
    if (args.outcome === 'passed' && Object.entries(active.task.request.revisions).some(([key, value]) => commits[key] !== value)) throw new Error('Passed result requires the requested commit SHAs');
    const artifacts = [];
    const names = new Set();
    let totalBytes = 0;
    for (const requested of args.files) {
      signal?.throwIfAborted();
      if (!active.task.request.workspace || !path.isAbsolute(active.task.request.workspace) || !path.isAbsolute(requested)) throw new Error('Artifact requires an absolute task workspace and file path');
      const workspace = await fsp.realpath(active.task.request.workspace);
      const file = await fsp.realpath(requested);
      const relative = path.relative(workspace, file);
      if (relative === '' || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) throw new Error('Artifact must be inside the task workspace');
      const name = path.basename(file);
      text(name, 'artifact name');
      if (Buffer.byteLength(name, 'utf8') > 200 || /[\x00-\x1f/\\]/.test(name) || ['.', '..'].includes(name)) throw new Error('Invalid artifact name');
      if (names.has(name.toLowerCase()) || ['result.json', 'task.json'].includes(name.toLowerCase())) throw new Error('Duplicate or reserved artifact name');
      names.add(name.toLowerCase());
      const stat = await fsp.stat(file);
      if (!stat.isFile() || stat.size > 8 * 1024 * 1024) throw new Error('Artifact must be a file of at most 8 MiB');
      const data = await fsp.readFile(file, { signal });
      totalBytes += data.length;
      if (data.length > 8 * 1024 * 1024 || totalBytes > 20 * 1024 * 1024) throw new Error('Artifacts exceed transfer limit');
      artifacts.push({ name, base64: data.toString('base64') });
    }
    const result = { task_id: args.task_id, session_id: sessionId, outcome: args.outcome, summary: args.summary, commits, artifacts };
    if (this.state.outbox && JSON.stringify(this.state.outbox) !== JSON.stringify(result)) throw new Error('A different result has already been saved');
    signal?.throwIfAborted();
    this.state.outbox = result;
    this.persist(); // Commit before acknowledging the tool; network outages do not lose results.
    return { saved_locally: true, task_id: args.task_id, delivery: 'pending' };
  }

  stop() { this.abort.abort(); }
}

export function definitions(worker) {
  const output = { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] };
  return [{
    name: 'bridge_pair', description: '将当前 DSH 会话配对到已配置的 Mac Codex 原会话。只在用户明确要求配对时调用。',
    parameters: { type: 'object', properties: {}, additionalProperties: false }, output,
    async execute(_args, exec) { return JSON.stringify(await worker.pair(exec.agent?.id, exec.signal)); },
  }, {
    name: 'bridge_report', description: '报告已分派的 Mac 任务结果，持久保存并回传 Mac。提交成功后结束本轮。',
    parameters: { type: 'object', additionalProperties: false, required: ['task_id','outcome','summary','commits','files'], properties: {
      task_id: { type: 'string' }, outcome: { type: 'string', enum: ['passed','failed','blocked'] }, summary: { type: 'string' },
      commits: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['repo','sha'], properties: { repo: { type: 'string' }, sha: { type: 'string' } } } },
      files: { type: 'array', items: { type: 'string' }, description: '指定工作目录内的日志或截图绝对路径；没有则传 []。' },
    } }, output,
    async execute(args, exec) {
      const value = await worker.report(args, exec.agent?.id, exec.signal);
      exec.concludeTurn();
      return JSON.stringify(value);
    },
  }];
}

export function apply(ctx) {
  const directory = path.dirname(fileURLToPath(import.meta.url));
  const config = JSON.parse(fs.readFileSync(path.join(directory, 'config.json'), 'utf8'));
  const worker = new Worker(config, directory, durableController(ctx));
  for (const definition of definitions(worker)) ctx.tools.register(definition);
  let lastError = '';
  const poll = () => worker.tick().then(() => { lastError = ''; }).catch(error => {
    if (worker.abort.signal.aborted) return;
    if (error.message !== lastError) {
      lastError = error.message;
      ctx.logger.warn(`Codex bridge: ${error.message}`);
    }
  });
  ctx.effect(() => {
    const timer = setInterval(poll, config.pollMs);
    poll();
    return () => { clearInterval(timer); worker.stop(); };
  });
}
