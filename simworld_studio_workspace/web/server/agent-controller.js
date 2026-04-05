'use strict';

const { spawn } = require('child_process');
const path = require('path');
const net = require('net');

const fs = require('fs');
const log = require('./logger');

const MCP_CONFIG = path.resolve(__dirname, '../mcp.json');
const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';
const UCV_PORT = parseInt(process.env.UCV_PORT || '9000', 10);
const UCV_HOST = process.env.UCV_HOST || '127.0.0.1';
const UCV_MAGIC = 0x9E2B83C1;
const REGISTRY = JSON.parse(fs.readFileSync(path.resolve(__dirname, 'agent-registry.json'), 'utf-8'));

// ---------------------------------------------------------------------------
// UnrealCV helper — one-shot TCP per call (connect → send → recv → close)
// ---------------------------------------------------------------------------

let ucvMsgId = 100; // offset from mcp-server's counter

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

// ---------------------------------------------------------------------------
// Get observation for an agent (position + nearby actors)
// ---------------------------------------------------------------------------

async function getObservation(agentName) {
  try {
    const loc = await ucvCommand(`vget /object/${agentName}/location`);
    const rot = await ucvCommand(`vget /object/${agentName}/rotation`);
    const locParts = loc.trim().split(/\s+/).map(Number);
    const rotParts = rot.trim().split(/\s+/).map(Number);
    return {
      location: locParts.length === 3 ? locParts : null,
      rotation: rotParts.length === 3 ? rotParts : null,
    };
  } catch {
    return { location: null, rotation: null };
  }
}

// ---------------------------------------------------------------------------
// Per-agent session
// ---------------------------------------------------------------------------

class AgentSession {
  constructor({ agentName, agentClass, location }) {
    this.agentName = agentName;
    this.agentClass = agentClass;
    this.location = location;
    this.status = 'idle';
    this.proc = null;
    this.history = [];
    this.inbox = [];
    this.lastReasoning = '';
    this.lastTools = [];
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

    // List available actions from registry
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
      'To message another agent, include @AgentName in your response text.',
      '',
      '## Rules',
      `- Always use agent_name="${this.agentName}"`,
      '- Only control YOUR agent.',
      '- Be concise. Act, then report.',
    );

    // Inject conversation history (last 6 turns for context)
    if (this.history.length > 0) {
      lines.push('', '## Conversation History');
      const recent = this.history.slice(-6);
      for (const h of recent) {
        const prefix = h.role === 'user' ? 'User' : 'You';
        lines.push(`${prefix}: ${h.content.slice(0, 300)}`);
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

  async run(message, onEvent) {
    if (this.status === 'running') {
      throw new Error('Agent is already running');
    }

    this.status = 'running';
    this.lastReasoning = '';
    this.lastTools = [];
    this.history.push({ role: 'user', content: message, timestamp: Date.now() });
    log.agent('info', `${this.agentName} turn start`, { message: message.slice(0, 200), historyLen: this.history.length });

    // Get fresh observation before running
    const obs = await getObservation(this.agentName);
    if (obs.location) this.location = obs.location;
    log.agent('debug', `${this.agentName} observation`, obs);

    const systemPrompt = this._systemPrompt();

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
        timeout: 300_000,
      });
      this.proc = proc;

      let buf = '';
      let assistantText = '';

      const flush = (line) => {
        if (!line.trim()) return;
        let msg;
        try { msg = JSON.parse(line); } catch { return; }

        if (msg.type === 'system' && msg.subtype === 'init') {
          log.agent('info', `${this.agentName} claude session`, { sessionId: msg.session_id });
          onEvent('system', { sessionId: msg.session_id });
        } else if (msg.type === 'stream_event') {
          const ev = msg.event || {};
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'text_delta') {
            assistantText += ev.delta.text;
            onEvent('text', { delta: ev.delta.text });
          }
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'thinking_delta') {
            onEvent('thinking', { delta: ev.delta.thinking });
          }
          if (ev.type === 'content_block_start' && ev.content_block?.type === 'tool_use') {
            const tc = ev.content_block;
            const displayName = tc.name.replace(/^mcp__\w+__/, '');
            this.lastTools.push({ name: displayName, ok: null });
            onEvent('tool_start', { id: tc.id, name: tc.name, displayName });
          }
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'input_json_delta') {
            onEvent('tool_input', { delta: ev.delta.partial_json });
          }
        } else if (msg.type === 'user') {
          for (const p of (msg.message?.content || [])) {
            if (p.type === 'tool_result') {
              const text = Array.isArray(p.content)
                ? p.content.map(c => c.text || '').join('')
                : String(p.content || '');
              const isErr = p.is_error || false;
              // Update last tool status
              const last = this.lastTools[this.lastTools.length - 1];
              if (last) last.ok = !isErr;
              onEvent('tool_result', { toolUseId: p.tool_use_id, result: text.slice(0, 2000), isError: isErr });
            }
          }
        } else if (msg.type === 'result') {
          this.lastReasoning = assistantText;
          this.history.push({ role: 'assistant', content: assistantText, timestamp: Date.now() });
          log.agent('info', `${this.agentName} turn done`, { cost: msg.total_cost_usd, tools: this.lastTools.length, textLen: assistantText.length });
          onEvent('done', {
            isError: msg.is_error || msg.subtype === 'error_during_turn',
            costUsd: msg.total_cost_usd,
            text: assistantText,
          });
        }
      };

      let lastOutput = Date.now();
      const idleTimer = setInterval(() => {
        if (Date.now() - lastOutput > 90000) {
          clearInterval(idleTimer);
          proc.kill('SIGTERM');
          onEvent('error', { message: 'Agent idle timeout (90s)' });
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
        const txt = d.toString().trim();
        if (txt) onEvent('stderr', { text: txt });
      });

      proc.on('close', (code) => {
        clearInterval(idleTimer);
        if (buf.trim()) flush(buf);
        this.status = 'idle';
        this.proc = null;
        log.agent('debug', `${this.agentName} process exited`, { code });
        resolve();
      });

      proc.on('error', err => {
        clearInterval(idleTimer);
        this.status = 'idle';
        this.proc = null;
        reject(err);
      });
    });
  }

  stop() {
    if (this.proc && !this.proc.killed) this.proc.kill('SIGTERM');
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
      lastReasoning: this.lastReasoning?.slice(0, 300) || '',
      lastTools: this.lastTools,
    };
  }
}

// ---------------------------------------------------------------------------
// Controller — manages all agent sessions + communication
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

  /** Parse @mentions from agent output and auto-forward. */
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
