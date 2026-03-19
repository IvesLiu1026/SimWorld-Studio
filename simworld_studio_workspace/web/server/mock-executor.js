"use strict";
/**
 * Mock Executor: actually runs MCP tool calls against UE during mock replay.
 * Spawns mcp-server.js as a subprocess and communicates via JSON-RPC stdio.
 */
const { spawn } = require("child_process");
const path = require("path");
const readline = require("readline");

class MockExecutor {
    constructor() {
        this._proc = null;
        this._pending = new Map(); // id -> {resolve, reject}
        this._nextId = 1;
        this._initialized = false;
        this._initPromise = null;
    }

    _start() {
        if (this._initPromise) return this._initPromise;
        this._initPromise = new Promise((resolve, reject) => {
            const mcpPath = path.join(__dirname, "mcp-server.js");
            this._proc = spawn("node", [mcpPath], {
                stdio: ["pipe", "pipe", "inherit"],
                env: process.env,
            });

            const rl = readline.createInterface({ input: this._proc.stdout, terminal: false });
            rl.on("line", (line) => {
                if (!line.trim()) return;
                try {
                    const msg = JSON.parse(line);
                    if (msg.id !== undefined && this._pending.has(msg.id)) {
                        const { resolve: res, reject: rej } = this._pending.get(msg.id);
                        this._pending.delete(msg.id);
                        if (msg.error) rej(new Error(msg.error.message));
                        else res(msg.result);
                    }
                } catch (e) {}
            });

            this._proc.on("error", reject);
            this._proc.on("exit", (code) => {
                // Reject all pending calls so they don't hang
                for (const { reject: rej } of this._pending.values()) {
                    rej(new Error(`mcp-server exited (code ${code})`));
                }
                this._pending.clear();
                this._proc = null;
                this._initialized = false;
                this._initPromise = null;
            });

            // Send initialize
            this._send("initialize", {
                protocolVersion: "2024-11-05",
                capabilities: {},
                clientInfo: { name: "mock-executor", version: "1.0" },
            }).then(() => {
                // notifications/initialized is a notification - no response expected, write directly
                this._proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized", params: {} }) + "\n");
                this._initialized = true;
                resolve();
            }).catch(reject);
        });
        return this._initPromise;
    }

    _send(method, params) {
        const id = this._nextId++;
        return new Promise((resolve, reject) => {
            this._pending.set(id, { resolve, reject });
            const msg = JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n";
            this._proc.stdin.write(msg);
            // Timeout after 12s (shorter than UE's 30s so UE errors propagate first)
            setTimeout(() => {
                if (this._pending.has(id)) {
                    this._pending.delete(id);
                    reject(new Error(`MCP call ${method} timed out`));
                }
            }, 12000);
        });
    }

    async execute(toolName, toolInput) {
        try {
            await this._start();
            // Strip mcp__simworld__ prefix if present
            const name = toolName.replace(/^mcp__[a-zA-Z0-9_]+__/, "");
            const result = await this._send("tools/call", { name, arguments: toolInput || {} });
            // Extract text content from MCP response
            if (result && result.content) {
                const text = result.content.map(c => c.text || "").join("");
                try { return JSON.parse(text); } catch { return text; }
            }
            return result;
        } catch (e) {
            console.error(`[mock-executor] Tool ${toolName} failed: ${e.message}`);
            return { status: "error", message: e.message };
        }
    }

    stop() {
        if (this._proc) {
            this._proc.kill();
            this._proc = null;
            this._initialized = false;
            this._initPromise = null;
        }
    }
}

// Singleton
const instance = new MockExecutor();
module.exports = { mockExecutor: instance };
