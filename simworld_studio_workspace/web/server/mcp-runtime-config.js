"use strict";

const fs = require("node:fs");
const path = require("node:path");

const SAFE_PORT = /^(?:[1-9][0-9]{0,4})$/;

function requireAbsoluteTarget(value, baseDir, port) {
  const raw = String(value || "").trim();
  const target = raw || path.resolve(baseDir, "../.runtime", `mcp-${port}.json`);
  if (!path.isAbsolute(target)) throw new TypeError("SIMWORLD_MCP_CONFIG must be an absolute path");
  const resolved = path.resolve(target);
  if (resolved === path.parse(resolved).root) throw new TypeError("SIMWORLD_MCP_CONFIG cannot be a filesystem root");
  return resolved;
}

function normalizePort(value) {
  const text = String(value || "55559").trim();
  if (!SAFE_PORT.test(text)) throw new TypeError("UNREAL_PORT must be a valid TCP port");
  const port = Number(text);
  if (port < 1 || port > 65535) throw new TypeError("UNREAL_PORT must be a valid TCP port");
  return String(port);
}

function buildMcpRuntimeConfig(env = process.env, baseDir = __dirname) {
  const serverScript = path.resolve(baseDir, "mcp-server.js");
  return Object.freeze({
    mcpServers: Object.freeze({
      simworld: Object.freeze({
        command: process.execPath,
        args: Object.freeze([serverScript]),
        env: Object.freeze({
          UNREAL_HOST: String(env.UNREAL_HOST || "127.0.0.1").trim() || "127.0.0.1",
          UNREAL_PORT: normalizePort(env.UNREAL_PORT),
        }),
      }),
    }),
  });
}

function ensureMcpRuntimeConfig(options = {}) {
  const env = options.env || process.env;
  const fsImpl = options.fsImpl || fs;
  const baseDir = path.resolve(options.baseDir || __dirname);
  const config = buildMcpRuntimeConfig(env, baseDir);
  const port = String(env.PORT || "3002").trim();
  if (!SAFE_PORT.test(port) || Number(port) > 65535) throw new TypeError("PORT must be a valid TCP port");
  const target = requireAbsoluteTarget(env.SIMWORLD_MCP_CONFIG, baseDir, port);
  const directory = path.dirname(target);
  fsImpl.mkdirSync(directory, { recursive: true, mode: 0o700 });
  try { fsImpl.chmodSync(directory, 0o700); } catch (_error) {}

  const serialized = `${JSON.stringify(config, null, 2)}\n`;
  let unchanged = false;
  try {
    const stat = fsImpl.lstatSync(target);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new TypeError("SIMWORLD_MCP_CONFIG must be a regular file");
    unchanged = fsImpl.readFileSync(target, "utf8") === serialized;
  } catch (error) {
    if (error && error.code !== "ENOENT") throw error;
  }
  if (!unchanged) {
    const temporary = `${target}.${process.pid}.tmp`;
    fsImpl.writeFileSync(temporary, serialized, { encoding: "utf8", mode: 0o600, flag: "wx" });
    try {
      fsImpl.renameSync(temporary, target);
    } catch (error) {
      try { fsImpl.unlinkSync(temporary); } catch (_cleanupError) {}
      throw error;
    }
  }
  fsImpl.chmodSync(target, 0o600);
  return Object.freeze({ path: target, config });
}

module.exports = {
  buildMcpRuntimeConfig,
  ensureMcpRuntimeConfig,
};
