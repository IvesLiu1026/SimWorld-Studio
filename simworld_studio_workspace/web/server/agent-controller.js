'use strict';

const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const log = require('./logger');
const { getBroker } = require('./unreal-bridge');

const MCP_CONFIG = path.resolve(__dirname, '../mcp.json');
const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';
const { SkillRegistry } = require('./skills');
const REGISTRY = JSON.parse(fs.readFileSync(path.resolve(__dirname, 'agent-registry.json'), 'utf-8'));

// Shared skill registry — panel agents get agent-relevant skills auto-injected
const skillRegistry = new SkillRegistry();

// ---------------------------------------------------------------------------
// UnrealCV access — all UCV traffic in this process goes through the singleton
// UcvBroker (see unreal-bridge.js). The old per-call one-shot TCP implementation
// raced with mcp-server subprocesses on port 9000 and silently dropped commands
// when spawn_agent reset the connection. The broker owns one persistent
// connection, serializes commands FIFO, retries on disconnect.
// ---------------------------------------------------------------------------

const broker = getBroker();

/** Compat shim — preserves old `ucvCommand(cmd, timeoutMs)` signature so any
 *  existing call site (e.g. AgentSession.stop) works unchanged. */
function ucvCommand(cmd, timeoutMs = 10000) {
  return broker.send(cmd, { timeoutMs });
}

async function getObservation(agentName) {
  // Broker handles retry+reconnect; we set a generous queue deadline so a brief
  // UCV stall (e.g. another agent spawning) doesn't make us silently return null.
  try {
    const [loc, rot] = await Promise.all([
      broker.send(`vget /object/${agentName}/location`, { timeoutMs: 8000, retries: 3, queueDeadlineMs: 30000 }),
      broker.send(`vget /object/${agentName}/rotation`, { timeoutMs: 8000, retries: 3, queueDeadlineMs: 30000 }),
    ]);
    return {
      location: loc.trim().split(/\s+/).map(Number),
      rotation: rot.trim().split(/\s+/).map(Number),
    };
  } catch (err) {
    log.agent('warn', `getObservation(${agentName}) failed: ${err.message}`);
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

    // Inject agent-relevant skills (movement, navigation, facing, etc.)
    const agentSkills = skillRegistry.search('agent', ['agent', 'movement', 'navigation']);
    if (agentSkills.length > 0) {
      const composed = skillRegistry.compose(agentSkills.map(s => s.id));
      if (composed) {
        lines.push('', '## SKILLS (reference documentation)', composed);
      }
    }

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
      try { onEvent('error', { message: err.message }); } catch {};
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
      // Remove ALL Claude-related env vars to prevent SDK/extension mode interference
      for (const key of Object.keys(env)) {
        if (key.startsWith('CLAUDE')) delete env[key];
      }

      const proc = spawn(CLAUDE_BIN, args, {
        cwd: path.resolve(__dirname, '..'),
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      this.proc = proc;

      let buf = '';
      let assistantText = '';
      let lastOutput = Date.now();
      let toolInProgress = false;   // true while a tool call is executing
      const act = this._currentActivity;

      const safeEvent = (type, data) => {
        try { onEvent(type, data); } catch (err) {
          log.agent('warn', `${this.agentName} onEvent error: ${err.message}`);
        }
      };

      const flush = (line) => {
        if (!line.trim()) return;
        let msg;
        try { msg = JSON.parse(line); } catch { return; }

        if (msg.type === 'system' && msg.subtype === 'init') {
          log.agent('debug', `${this.agentName} session: ${msg.session_id}`);
          safeEvent('system', { sessionId: msg.session_id });

        } else if (msg.type === 'stream_event') {
          const ev = msg.event || {};
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'text_delta') {
            assistantText += ev.delta.text;
            if (act) act.thought += ev.delta.text;
            safeEvent('text', { delta: ev.delta.text });
          }
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'thinking_delta') {
            if (act) act.thought += ev.delta.thinking;
            safeEvent('thinking', { delta: ev.delta.thinking });
          }
          if (ev.type === 'content_block_start' && ev.content_block?.type === 'tool_use') {
            const tc = ev.content_block;
            const displayName = tc.name.replace(/^mcp__\w+__/, '');
            if (act) act.actions.push({ tool: displayName, input: '', result: '', ok: null });
            toolInProgress = true;
            safeEvent('tool_start', { id: tc.id, name: tc.name, displayName });
          }
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'input_json_delta') {
            if (act && act.actions.length > 0) {
              act.actions[act.actions.length - 1].input += ev.delta.partial_json;
            }
            safeEvent('tool_input', { delta: ev.delta.partial_json });
          }

        } else if (msg.type === 'user') {
          toolInProgress = false;   // tool result arrived → no longer in-progress
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
              safeEvent('tool_result', { toolUseId: p.tool_use_id, result: text.slice(0, 2000), isError: isErr });
            }
          }

        } else if (msg.type === 'result') {
          if (act) {
            act.response = assistantText;
            act.cost = msg.total_cost_usd;
          }
          this.history.push({ role: 'assistant', content: assistantText, timestamp: Date.now() });
          log.agent('info', `${this.agentName} done`, { cost: msg.total_cost_usd, actions: act?.actions?.length });
          safeEvent('done', {
            isError: msg.is_error || msg.subtype === 'error_during_turn',
            costUsd: msg.total_cost_usd,
            text: assistantText,
          });
        }
      };

      // Idle timer — kill only when genuinely idle (no tool running)
      // Tool calls (MCP→UE) can easily take 2+ minutes, so skip check while tool is in progress
      const IDLE_LIMIT = 180000; // 3 min with no output AND no tool running
      const idleTimer = setInterval(() => {
        if (toolInProgress) {
          // Tool is executing — reset timer so we don't kill mid-tool
          lastOutput = Date.now();
          return;
        }
        if (Date.now() - lastOutput > IDLE_LIMIT) {
          clearInterval(idleTimer);
          log.agent('warn', `${this.agentName} idle timeout (${IDLE_LIMIT/1000}s, no tool active)`);
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

      proc.on('close', (code, signal) => {
        clearInterval(idleTimer);
        if (buf.trim()) flush(buf);
        log.agent('debug', `${this.agentName} exit code=${code} signal=${signal}`);
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
    // Also stop the agent in UE
    const type = this._resolveType();
    const typeDef = REGISTRY.agentTypes[type];
    const stopCmd = typeDef?.stopCmd || 'StopAgent';
    ucvCommand(`vbp ${this.agentName} ${stopCmd}`).catch(() => {});
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
      // Don't remove agents that are currently running
      const session = this._sessions.get(name);
      if (!seen.has(name) && session?.status !== 'running') this.remove(name);
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
