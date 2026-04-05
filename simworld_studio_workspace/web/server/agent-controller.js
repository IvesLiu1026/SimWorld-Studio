'use strict';

const { spawn } = require('child_process');
const path = require('path');

const MCP_CONFIG = path.resolve(__dirname, '../mcp.json');
const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';

// ---------------------------------------------------------------------------
// Per-agent session
// ---------------------------------------------------------------------------

class AgentSession {
  constructor({ agentName, agentClass, location }) {
    this.agentName = agentName;
    this.agentClass = agentClass;
    this.location = location;
    this.sessionId = null;   // Claude session_id once known
    this.status = 'idle';    // idle | running | stopped
    this.proc = null;        // child process
    this.history = [];       // { role, content, timestamp }
    this.inbox = [];         // Messages from other agents (inter-agent communication)
  }

  /** Build the system prompt that tells Claude who this agent is. */
  _systemPrompt() {
    const loc = Array.isArray(this.location)
      ? `(${this.location.map(v => Math.round(v)).join(', ')})`
      : 'unknown';
    const isHumanoid = /humanoid|user_agent|robot/i.test(this.agentClass || '');
    const agentType = isHumanoid ? 'humanoid' : 'pedestrian';
    const lines = [
      `You are the controller for agent "${this.agentName}" (class: ${this.agentClass}, type: ${agentType}) in a SimWorld scene.`,
      `Your current location is ${loc}.`,
      '',
      '## Available Movement Tools',
      `- agent_move_forward(agent_name="${this.agentName}") — start moving forward continuously`,
      `- agent_stop(agent_name="${this.agentName}", agent_type="${agentType}") — stop movement`,
      `- agent_rotate(agent_name="${this.agentName}", angle=90, direction="right", agent_type="${agentType}") — turn`,
      `- agent_set_speed(agent_name="${this.agentName}", speed=200) — set speed (100=slow, 200=normal, 400=run)`,
      `- agent_step_forward(agent_name="${this.agentName}", duration=2) — move forward for N seconds then stop`,
      '',
      '## Available Actions',
      `- agent_action(agent_name="${this.agentName}", action="sit_down") — sit, stand_up, wave, discuss, listen, pick_up, drop_off`,
      '',
      '## Perception',
      `- get_agent_state(agent_name="${this.agentName}") — get your position and rotation`,
      '- get_actors_in_level() — see all objects and agents in the scene',
      '- take_screenshot() — see the viewport',
      '',
      '## Rules',
      `- Always use agent_name="${this.agentName}" in all commands`,
      '- Only control YOUR agent. Never modify other agents or scene objects.',
      '- Break goals into steps: move, verify position, adjust.',
      '- Be concise.',
    ];
    // Add messages from other agents
    if (this.inbox && this.inbox.length > 0) {
      lines.push('', '## Messages From Other Agents');
      for (const msg of this.inbox) {
        lines.push(`- @${msg.from}: "${msg.text}" (${new Date(msg.timestamp).toLocaleTimeString()})`);
      }
      this.inbox = []; // Clear after injecting
    }
    return lines.join('\n');
  }

  /**
   * Send a message to this agent. Returns an event emitter-like callback
   * approach: call `onEvent(type, data)` for each SSE event.
   *
   * @param {string} message   User instruction or "auto" for autonomous step
   * @param {function} onEvent (type: string, data: object) => void
   * @returns {Promise<void>}  resolves when turn finishes
   */
  run(message, onEvent) {
    if (this.status === 'running') {
      return Promise.reject(new Error('Agent is already running'));
    }

    this.status = 'running';
    this.history.push({ role: 'user', content: message, timestamp: Date.now() });

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

      const CLAUDE_MODEL = process.env.CLAUDE_MODEL || '';
      if (CLAUDE_MODEL) args.push('--model', CLAUDE_MODEL);

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
        const type = msg.type;

        if (type === 'system' && msg.subtype === 'init' && msg.session_id) {
          this.sessionId = msg.session_id;
          onEvent('system', { sessionId: msg.session_id });
        } else if (type === 'stream_event') {
          const ev = msg.event || {};
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'text_delta') {
            onEvent('text', { delta: ev.delta.text });
            assistantText += ev.delta.text;
          }
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'thinking_delta') {
            onEvent('thinking', { delta: ev.delta.thinking });
          }
          if (ev.type === 'content_block_start' && ev.content_block?.type === 'tool_use') {
            const tc = ev.content_block;
            onEvent('tool_start', {
              id: tc.id,
              name: tc.name,
              displayName: tc.name.replace(/^mcp__\w+__/, ''),
            });
          }
          if (ev.type === 'content_block_delta' && ev.delta?.type === 'input_json_delta') {
            onEvent('tool_input', { delta: ev.delta.partial_json });
          }
        } else if (type === 'user') {
          const parts = msg.message?.content || [];
          for (const p of parts) {
            if (p.type === 'tool_result') {
              const text = Array.isArray(p.content)
                ? p.content.map(c => c.text || '').join('')
                : String(p.content || '');
              onEvent('tool_result', {
                toolUseId: p.tool_use_id,
                result: text.slice(0, 2000),
                isError: p.is_error || false,
              });
            }
          }
        } else if (type === 'result') {
          if (msg.session_id) this.sessionId = msg.session_id;
          this.history.push({
            role: 'assistant',
            content: assistantText,
            timestamp: Date.now(),
          });
          onEvent('done', {
            sessionId: this.sessionId,
            isError: msg.is_error || msg.subtype === 'error_during_turn',
            costUsd: msg.total_cost_usd,
          });
        }
      };

      let lastOutput = Date.now();
      const idleTimer = setInterval(() => {
        if (Date.now() - lastOutput > 90000) {
          clearInterval(idleTimer);
          proc.kill('SIGTERM');
          onEvent('error', { message: 'Agent idle timeout (90s no output)' });
        }
      }, 10000);

      proc.stdout.on('data', (chunk) => {
        lastOutput = Date.now();
        buf += chunk.toString();
        const lines = buf.split('\n');
        buf = lines.pop() ?? '';
        for (const ln of lines) flush(ln);
      });

      proc.stderr.on('data', (d) => {
        const txt = d.toString().trim();
        if (txt) onEvent('stderr', { text: txt });
      });

      proc.on('close', (code) => {
        clearInterval(idleTimer);
        if (buf.trim()) flush(buf);
        this.status = 'idle';
        this.proc = null;
        resolve();
      });

      proc.on('error', (err) => {
        this.status = 'idle';
        this.proc = null;
        reject(err);
      });
    });
  }

  /** Kill the running process. */
  stop() {
    if (this.proc && !this.proc.killed) {
      this.proc.kill('SIGTERM');
    }
    this.status = 'idle';
    this.proc = null;
  }

  toJSON() {
    return {
      agentName: this.agentName,
      agentClass: this.agentClass,
      location: this.location,
      sessionId: this.sessionId,
      status: this.status,
      historyLength: this.history.length,
    };
  }
}

// ---------------------------------------------------------------------------
// Controller — manages all agent sessions
// ---------------------------------------------------------------------------

class AgentController {
  constructor() {
    /** @type {Map<string, AgentSession>} keyed by agent name */
    this._sessions = new Map();
  }

  /** Get or create a session for the given agent. */
  getOrCreate(agentName, agentClass, location) {
    if (!this._sessions.has(agentName)) {
      this._sessions.set(
        agentName,
        new AgentSession({ agentName, agentClass, location })
      );
    }
    const s = this._sessions.get(agentName);
    // Update location in case it moved
    if (location) s.location = location;
    if (agentClass) s.agentClass = agentClass;
    return s;
  }

  get(agentName) {
    return this._sessions.get(agentName) || null;
  }

  /** List all sessions. */
  list() {
    return [...this._sessions.values()].map(s => s.toJSON());
  }

  /** Stop a specific agent. */
  stop(agentName) {
    const s = this._sessions.get(agentName);
    if (s) s.stop();
  }

  /** Stop all agents. */
  stopAll() {
    for (const s of this._sessions.values()) s.stop();
  }

  /** Remove session entirely. */
  remove(agentName) {
    this.stop(agentName);
    this._sessions.delete(agentName);
  }

  /** Sync with ContextManager — add/remove sessions based on scene state. */
  syncWithContext(contextState) {
    if (!contextState) return;
    const sceneAgents = new Set();
    for (const a of contextState.agents || []) {
      sceneAgents.add(a.name);
      this.getOrCreate(a.name, a.cls, a.location);
    }
    // Remove sessions for agents no longer in scene
    for (const name of this._sessions.keys()) {
      if (!sceneAgents.has(name)) {
        this.remove(name);
      }
    }
  }

  // ── Inter-agent communication ──────────────────────────────────────────

  /** Public message log visible to all */
  _publicChat = [];

  /** Send a message from one agent to another (or broadcast). */
  sendMessage(from, to, text) {
    const msg = { from, to: to || 'all', text, timestamp: Date.now() };
    this._publicChat.push(msg);
    // Keep last 100 messages
    if (this._publicChat.length > 100) this._publicChat.splice(0, this._publicChat.length - 100);
    // Deliver to target agent's inbox
    if (to && to !== 'all') {
      const target = this._sessions.get(to);
      if (target) {
        target.inbox = target.inbox || [];
        target.inbox.push(msg);
      }
    } else {
      // Broadcast to all agents except sender
      for (const [name, session] of this._sessions) {
        if (name !== from) {
          session.inbox = session.inbox || [];
          session.inbox.push(msg);
        }
      }
    }
    return msg;
  }

  /** Get public chat log. */
  getPublicChat(since = 0) {
    return this._publicChat.filter(m => m.timestamp > since);
  }
}

module.exports = { AgentController, AgentSession };
