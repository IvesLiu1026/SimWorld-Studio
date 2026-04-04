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
  }

  /** Build the system prompt that tells Claude who this agent is. */
  _systemPrompt() {
    const loc = Array.isArray(this.location)
      ? `(${this.location.map(v => Math.round(v)).join(', ')})`
      : 'unknown';
    return [
      `You are the autonomous controller for agent "${this.agentName}" (class: ${this.agentClass}) in a SimWorld scene.`,
      `Your current location is ${loc}.`,
      '',
      'Your job is to reason about what this agent should do and then execute actions using the SimWorld MCP tools.',
      '',
      'Available actions:',
      '- set_actor_transform — move / rotate this agent. Always use your own name as the actor name.',
      '- take_screenshot — see the world from the viewport.',
      '- get_actors_in_level — perceive nearby objects and other agents.',
      '- find_actors_by_name — search for specific actors.',
      '- execute_python_script — run arbitrary UE Python for advanced control.',
      '',
      'Rules:',
      '- Only control YOUR agent. Never modify other agents or scene objects.',
      '- When given a goal, break it into steps and execute them.',
      '- After moving, take a screenshot to verify your new position.',
      '- Be concise in your reasoning.',
    ].join('\n');
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
        '--dangerously-skip-permissions',
        '--mcp-config', MCP_CONFIG,
        '--append-system-prompt', systemPrompt,
      ];

      if (this.sessionId) {
        args.push('--session-id', this.sessionId);
      }

      const env = { ...process.env };
      delete env.CLAUDECODE;
      delete env.CLAUDE_SESSION_ID;
      delete env.CLAUDE_CODE_ENTRYPOINT;

      const proc = spawn(CLAUDE_BIN, args, {
        cwd: path.resolve(__dirname, '..'),
        env,
        stdio: ['ignore', 'stdout', 'stderr'],
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

      proc.stdout.on('data', (chunk) => {
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
}

module.exports = { AgentController, AgentSession };
