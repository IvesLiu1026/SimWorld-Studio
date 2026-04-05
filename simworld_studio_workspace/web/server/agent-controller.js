'use strict';

const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');
const log = require('./logger');

const MCP_CONFIG = path.resolve(__dirname, '../mcp.json');
const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';
const UCV_PORT = parseInt(process.env.UCV_PORT || '9000', 10);
const UCV_HOST = process.env.UCV_HOST || '127.0.0.1';
const UCV_MAGIC = 0x9E2B83C1;
const REGISTRY = JSON.parse(fs.readFileSync(path.resolve(__dirname, 'agent-registry.json'), 'utf-8'));

// ---------------------------------------------------------------------------
// UnrealCV helper — one-shot TCP per call
// ---------------------------------------------------------------------------

let ucvMsgId = 200;

function ucvCommand(cmd, timeout = 8000) {
  return new Promise((resolve, reject) => {
    const sock = new net.Socket();
    const timer = setTimeout(() => { sock.destroy(); reject(new Error('UCV timeout')); }, timeout);
    let buf = Buffer.alloc(0);
    let gotBanner = false;
    const id = ucvMsgId++;

    function parse(b) {
      if (b.length < 8) return null;
      if (b.readUInt32LE(0) !== UCV_MAGIC) return null;
      const sz = b.readUInt32LE(4);
      if (b.length < 8 + sz) return null;
      return { payload: b.slice(8, 8 + sz).toString('utf-8'), remaining: b.slice(8 + sz) };
    }
    function sendMsg(msg) {
      const p = Buffer.from(msg, 'utf-8');
      const h = Buffer.alloc(8);
      h.writeUInt32LE(UCV_MAGIC, 0);
      h.writeUInt32LE(p.length, 4);
      sock.write(Buffer.concat([h, p]));
    }

    sock.connect(UCV_PORT, UCV_HOST, () => {});
    sock.on('data', d => {
      buf = Buffer.concat([buf, d]);
      let p;
      while ((p = parse(buf)) !== null) {
        buf = p.remaining;
        if (!gotBanner) {
          gotBanner = true;
          sendMsg(`${id}:${cmd}`);
        } else {
          clearTimeout(timer);
          sock.destroy();
          let result = p.payload;
          const ci = result.indexOf(':');
          if (ci > 0 && ci < 6) result = result.slice(ci + 1);
          resolve(result);
          return;
        }
      }
    });
    sock.on('end', () => { clearTimeout(timer); resolve(''); });
    sock.on('error', e => { clearTimeout(timer); reject(e); });
  });
}

async function getObservation(agentName) {
  try {
    const loc = await ucvCommand(`vget /object/${agentName}/location`);
    const rot = await ucvCommand(`vget /object/${agentName}/rotation`);
    return {
      location: loc.trim().split(/\s+/).map(Number),
      rotation: rot.trim().split(/\s+/).map(Number),
    };
  } catch {
    return { location: null, rotation: null };
  }
}

// ---------------------------------------------------------------------------
// ReAct activity log entry
// ---------------------------------------------------------------------------

/**
 * Activity log entry — one per agent turn, captures the full ReAct cycle.
 * { thought, actions: [{tool, input, result, ok}], response, timestamp, cost }
 */

// ---------------------------------------------------------------------------
// Per-agent session
// ---------------------------------------------------------------------------

class AgentSession {
  constructor({ agentName, agentClass, location }) {
    this.agentName = agentName;
    this.agentClass = agentClass;
    this.location = location;
    this.status = 'idle';    // idle | running
    this.proc = null;
    this.history = [];       // conversation history
    this.inbox = [];         // inter-agent messages
    this.activity = [];      // ReAct activity log (last N turns)
    this._currentActivity = null; // in-progress activity
  }

  _resolveType() {
    const cls = (this.agentClass || '').toLowerCase();
    for (const [typeName, def] of Object.entries(REGISTRY.agentTypes)) {
      if (def.namePatterns.some(p => cls.includes(p))) return typeName;
    }
    return 'pedestrian';
  }

  _systemPrompt() {
    const loc = Array.isArray(this.location)
      ? `(${this.location.map(v => Math.round(v)).join(', ')})`
      : 'unknown';
    const type = this._resolveType();
    const typeDef = REGISTRY.agentTypes[type];

    const lines = [
      `You control agent "${this.agentName}" (${type}) at ${loc}.`,
      '',
      '## Actions (use agent_action tool)',
    ];

    if (typeDef?.actions) {
      for (const [name, def] of Object.entries(typeDef.actions)) {
        const paramStr = def.params ? `, params: {${def.params.join(', ')}}` : '';
        lines.push(`- agent_action(agent_name="${this.agentName}", action="${name}", agent_type="${type}"${paramStr}) — ${def.description}`);
      }
    }

    lines.push(
      '',
      '## Other Tools',
      `- agent_stop(agent_name="${this.agentName}", agent_type="${type}")`,
      `- agent_rotate(agent_name="${this.agentName}", angle=N, direction="left"|"right", agent_type="${type}")`,
      `- get_agent_state(agent_name="${this.agentName}")`,
      '- get_actors_in_level()',
      '- take_screenshot()',
      '',
      '## Communication',
      'To message another agent, include @AgentName in your response.',
      '',
      '## Rules',
      `- Always use agent_name="${this.agentName}"`,
      '- Only control YOUR agent.',
      '- Think step by step: observe → think → act → verify.',
      '- Be concise.',
    );

    if (this.history.length > 0) {
      lines.push('', '## Recent History');
      for (const h of this.history.slice(-6)) {
        lines.push(`${h.role === 'user' ? 'User' : 'You'}: ${h.content.slice(0, 300)}`);
      }
    }

    if (this.inbox.length > 0) {
      lines.push('', '## Incoming Messages');
      for (const msg of this.inbox) {
        lines.push(`- ${msg.from}: "${msg.text}"`);
      }
      this.inbox = [];
    }

    return lines.join('\n');
  }

  /**
   * Run a turn. Streams ReAct events to onEvent callback.
   * ALWAYS resets status to 'idle' when done, even on error.
   */
  async run(message, onEvent) {
    // Force-reset if stuck (safety valve)
    if (this.status === 'running' && this.proc) {
      log.agent('warn', `${this.agentName} force-killing stuck process`);
      try { this.proc.kill('SIGTERM'); } catch {}
      this.proc = null;
    }

    this.status = 'running';
    this.history.push({ role: 'user', content: message, timestamp: Date.now() });

    // Init activity entry for this turn
    this._currentActivity = {
      thought: '',
      actions: [],
      response: '',
      timestamp: Date.now(),
      cost: null,
    };

    log.agent('info', `${this.agentName} turn start`, { message: message.slice(0, 200) });

    // Get observation
    const obs = await getObservation(this.agentName);
    if (obs.location) this.location = obs.location;
    log.agent('debug', `${this.agentName} obs`, obs);

    const systemPrompt = this._systemPrompt();

    try {
      await this._spawnClaude(message, systemPrompt, onEvent);
    } catch (err) {
      log.agent('error', `${this.agentName} run error: ${err.message}`);
      onEvent('error', { message: err.message });
    } finally {
      // ALWAYS reset status
      this.status = 'idle';
      this.proc = null;

      // Finalize activity
      if (this._currentActivity) {
        this.activity.push(this._currentActivity);
        if (this.activity.length > 20) this.activity.splice(0, this.activity.length - 20);
        this._currentActivity = null;
      }
    }
  }

  _spawnClaude(message, systemPrompt, onEvent) {
    return new Promise((resolve, reject) => {
      const args = [
        '-p', message,
        '--output-format', 'stream-json',
        '--include-partial-messages',
        '--verbose',
        '--dangerously-skip-permissions',
        '--mcp-config', MCP_CONFIG,
        '--append-system-prompt', systemPrompt,
      ];
      const model = process.env.CLAUDE_MODEL || '';
      if (model) args.push('--model', model);

      const env = { ...process.env };
      delete env.CLAUDECODE;
      delete env.CLAUDE_SESSION_ID;
      delete env.CLAUDE_CODE_ENTRYPOINT;

      const proc = spawn(CLAUDE_BIN, args, {
        cwd: path.resolve(__dirname, '..'),
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      this.proc = proc;

      let buf = '';
      let assistantText = '';
      let lastOutput = Date.now();
      const act = this._currentActivity;

      const flush = (line) => {
        if (!line.trim()) return;
        let msg;
        try { msg = JSON.parse(line); } catch { return; }

        if (msg.type === 'system' && msg.subtype === 'init') {
          log.agent('debug', `${this.agentName} session: ${msg.session_id}`);
          onEvent('system', { sessionId: msg.session_id });

        } else if (msg.type === 'stream_event') {
          const ev = msg.event || {};
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'text_delta') {
            assistantText += ev.delta.text;
            if (act) act.thought += ev.delta.text;
            onEvent('text', { delta: ev.delta.text });
          }
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'thinking_delta') {
            if (act) act.thought += ev.delta.thinking;
            onEvent('thinking', { delta: ev.delta.thinking });
          }
          if (ev.type === 'content_block_start' && ev.content_block?.type === 'tool_use') {
            const tc = ev.content_block;
            const displayName = tc.name.replace(/^mcp__\w+__/, '');
            if (act) act.actions.push({ tool: displayName, input: '', result: '', ok: null });
            onEvent('tool_start', { id: tc.id, name: tc.name, displayName });
          }
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'input_json_delta') {
            if (act && act.actions.length > 0) {
              act.actions[act.actions.length - 1].input += ev.delta.partial_json;
            }
            onEvent('tool_input', { delta: ev.delta.partial_json });
          }

        } else if (msg.type === 'user') {
          for (const p of (msg.message?.content || [])) {
            if (p.type === 'tool_result') {
              const text = Array.isArray(p.content)
                ? p.content.map(c => c.text || '').join('')
                : String(p.content || '');
              const isErr = p.is_error || false;
              if (act && act.actions.length > 0) {
                const last = act.actions[act.actions.length - 1];
                last.result = text.slice(0, 500);
                last.ok = !isErr;
              }
              onEvent('tool_result', { toolUseId: p.tool_use_id, result: text.slice(0, 2000), isError: isErr });
            }
          }

        } else if (msg.type === 'result') {
          if (act) {
            act.response = assistantText;
            act.cost = msg.total_cost_usd;
          }
          this.history.push({ role: 'assistant', content: assistantText, timestamp: Date.now() });
          log.agent('info', `${this.agentName} done`, { cost: msg.total_cost_usd, actions: act?.actions?.length });
          onEvent('done', {
            isError: msg.is_error || msg.subtype === 'error_during_turn',
            costUsd: msg.total_cost_usd,
            text: assistantText,
          });
        }
      };

      // Idle timer — 90s no output = kill
      const idleTimer = setInterval(() => {
        if (Date.now() - lastOutput > 90000) {
          clearInterval(idleTimer);
          log.agent('warn', `${this.agentName} idle timeout`);
          proc.kill('SIGTERM');
        }
      }, 10000);

      proc.stdout.on('data', chunk => {
        lastOutput = Date.now();
        buf += chunk.toString();
        const lines = buf.split('\n');
        buf = lines.pop() ?? '';
        for (const ln of lines) flush(ln);
      });

      proc.stderr.on('data', d => {
        lastOutput = Date.now(); // stderr counts as activity
        const txt = d.toString().trim();
        if (txt) log.agent('debug', `${this.agentName} stderr: ${txt.slice(0, 200)}`);
      });

      proc.on('close', (code) => {
        clearInterval(idleTimer);
        if (buf.trim()) flush(buf);
        log.agent('debug', `${this.agentName} exit code=${code}`);
        resolve();
      });

      proc.on('error', (err) => {
        clearInterval(idleTimer);
        reject(err);
      });
    });
  }

  stop() {
    if (this.proc && !this.proc.killed) {
      this.proc.kill('SIGTERM');
      log.agent('info', `${this.agentName} stopped by user`);
    }
    this.status = 'idle';
    this.proc = null;
  }

  toJSON() {
    return {
      agentName: this.agentName,
      agentClass: this.agentClass,
      location: this.location,
      status: this.status,
      historyLength: this.history.length,
      // Last activity for display
      lastActivity: this.activity.length > 0 ? this.activity[this.activity.length - 1] : null,
      activityCount: this.activity.length,
    };
  }
}

// ---------------------------------------------------------------------------
// Controller
// ---------------------------------------------------------------------------

class AgentController {
  constructor() {
    this._sessions = new Map();
    this._publicChat = [];
  }

  getOrCreate(name, cls, location) {
    if (!this._sessions.has(name)) {
      this._sessions.set(name, new AgentSession({ agentName: name, agentClass: cls, location }));
    }
    const s = this._sessions.get(name);
    if (location) s.location = location;
    if (cls) s.agentClass = cls;
    return s;
  }

  get(name) { return this._sessions.get(name) || null; }
  list() { return [...this._sessions.values()].map(s => s.toJSON()); }
  stop(name) { const s = this._sessions.get(name); if (s) s.stop(); }
  stopAll() { for (const s of this._sessions.values()) s.stop(); }
  remove(name) { this.stop(name); this._sessions.delete(name); }

  /** Get full activity log for an agent */
  getActivity(name) {
    const s = this._sessions.get(name);
    return s ? s.activity : [];
  }

  syncWithContext(contextState) {
    if (!contextState) return;
    const seen = new Set();
    for (const a of contextState.agents || []) {
      seen.add(a.name);
      this.getOrCreate(a.name, a.cls, a.location);
    }
    for (const name of this._sessions.keys()) {
      if (!seen.has(name)) this.remove(name);
    }
  }

  // ── Communication ──

  sendMessage(from, to, text) {
    const msg = { from, to: to || 'all', text, timestamp: Date.now() };
    log.agent('info', `msg ${from} → ${to || 'all'}`, { text: text.slice(0, 100) });
    this._publicChat.push(msg);
    if (this._publicChat.length > 200) this._publicChat.splice(0, this._publicChat.length - 200);

    if (to && to !== 'all') {
      const target = this._sessions.get(to);
      if (target) target.inbox.push(msg);
    } else {
      for (const [name, session] of this._sessions) {
        if (name !== from) session.inbox.push(msg);
      }
    }
    return msg;
  }

  parseAndForwardMentions(fromAgent, text) {
    const mentions = text.match(/@(\w+)/g);
    if (!mentions) return [];
    const forwarded = [];
    for (const m of mentions) {
      const targetName = m.slice(1);
      if (this._sessions.has(targetName) && targetName !== fromAgent) {
        this.sendMessage(fromAgent, targetName, text);
        forwarded.push(targetName);
      }
    }
    return forwarded;
  }

  getPublicChat(since = 0) {
    return this._publicChat.filter(m => m.timestamp > since);
  }
}

module.exports = { AgentController, AgentSession };
