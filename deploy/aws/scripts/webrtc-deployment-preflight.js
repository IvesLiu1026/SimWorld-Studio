#!/usr/bin/env node
"use strict";

const dns = require("node:dns").promises;
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const SNAPSHOT_SCHEMA = "simworld-webrtc-host-snapshot/v1";
const REPORT_SCHEMA = "simworld-webrtc-deployment-preflight/v1";
const PHASES = new Set(["provision", "runtime"]);
const REQUIRED_EXECUTABLES = Object.freeze(["nginx", "turnserver", "openssl", "certbot", "ss"]);
const HOST_RE = /^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/;
const SHA40_RE = /^[a-f0-9]{40}$/;

function fail(message) {
  const error = new Error(message);
  error.code = "WEBRTC_PREFLIGHT_INVALID";
  throw error;
}

function parseArguments(argv) {
  let phase = "provision";
  let skipDns = false;
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--skip-dns") {
      skipDns = true;
      continue;
    }
    if (argument === "--phase") {
      phase = String(argv[index + 1] || "");
      index += 1;
      continue;
    }
    fail("usage: webrtc-deployment-preflight.js [--phase provision|runtime] [--skip-dns]");
  }
  if (!PHASES.has(phase)) fail("--phase must be provision or runtime");
  return { phase, skipDns };
}

function positiveId(value) {
  const text = String(value === undefined ? "" : value).trim();
  if (!/^[1-9][0-9]{0,9}$/.test(text)) return null;
  const number = Number(text);
  return Number.isSafeInteger(number) && number <= 0xffffffff ? number : null;
}

function boundedInteger(value, fallback, minimum, maximum) {
  const text = String(value === undefined || value === "" ? fallback : value).trim();
  if (!/^[0-9]+$/.test(text)) return null;
  const number = Number(text);
  return Number.isSafeInteger(number) && number >= minimum && number <= maximum ? number : null;
}

function normalizedHost(value) {
  const host = String(value || "").trim().toLowerCase();
  return HOST_RE.test(host) ? host : null;
}

function studioHost(origin) {
  try {
    const parsed = new URL(String(origin || ""));
    if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port
      || parsed.pathname !== "/" || parsed.search || parsed.hash || !normalizedHost(parsed.hostname)) return null;
    return parsed.hostname.toLowerCase();
  } catch {
    return null;
  }
}

function publicIp(value) {
  const address = String(value || "").trim().toLowerCase();
  const family = net.isIP(address);
  if (family === 4) {
    const octets = address.split(".").map(Number);
    const [first, second] = octets;
    if (first === 0 || first === 10 || first === 127 || first >= 224) return null;
    if (first === 100 && second >= 64 && second <= 127) return null;
    if (first === 169 && second === 254) return null;
    if (first === 172 && second >= 16 && second <= 31) return null;
    if (first === 192 && (second === 0 || second === 168)) return null;
    if (first === 198 && (second === 18 || second === 19)) return null;
    return address;
  }
  if (family === 6) {
    if (address === "::" || address === "::1" || address.startsWith("fc") || address.startsWith("fd")
      || /^fe[89ab]/.test(address) || address.startsWith("ff")) return null;
    return address;
  }
  return null;
}

function findExecutable(name, searchPath = process.env.PATH || "") {
  for (const directory of String(searchPath).split(path.delimiter)) {
    if (!directory) continue;
    const candidate = path.resolve(directory, name);
    try {
      const stat = fs.statSync(candidate);
      fs.accessSync(candidate, fs.constants.X_OK);
      if (stat.isFile()) return candidate;
    } catch {}
  }
  return null;
}

function getentRecord(database, name) {
  const result = spawnSync("getent", [database, name], {
    encoding: "utf8",
    timeout: 5000,
    maxBuffer: 64 * 1024,
  });
  if (result.status !== 0 || !result.stdout) return null;
  const fields = result.stdout.trim().split(":");
  if (database === "passwd" && fields.length >= 4) {
    return { name: fields[0], uid: Number(fields[2]), gid: Number(fields[3]) };
  }
  if (database === "group" && fields.length >= 3) {
    return { name: fields[0], gid: Number(fields[2]) };
  }
  return null;
}

function processIdentity(fragment) {
  const name = /\(\("([^"\\]{1,80})"/.exec(fragment || "")?.[1] || null;
  const pidText = /pid=(\d{1,10})/.exec(fragment || "")?.[1] || null;
  return {
    name,
    pid: pidText === null ? null : Number(pidText),
  };
}

function splitSocket(endpoint) {
  const text = String(endpoint || "").trim();
  const bracket = /^\[([^\]]+)\]:(\d{1,5})$/.exec(text);
  if (bracket) return { address: bracket[1], port: Number(bracket[2]) };
  const regular = /^(.*):(\d{1,5})$/.exec(text);
  if (!regular) return null;
  return { address: regular[1], port: Number(regular[2]) };
}

function parseSsListeners(stdout) {
  const listeners = [];
  for (const rawLine of String(stdout || "").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const fields = line.split(/\s+/);
    const protocol = fields[0] === "tcp" || fields[0] === "udp" ? fields[0] : null;
    if (!protocol || fields.length < 5) continue;
    const local = splitSocket(fields[4]);
    if (!local || local.port < 1 || local.port > 65535) continue;
    const identity = processIdentity(line);
    listeners.push({
      protocol,
      address: local.address,
      port: local.port,
      process_name: identity.name,
      pid: identity.pid,
    });
  }
  return listeners.sort((left, right) => left.port - right.port
    || left.protocol.localeCompare(right.protocol)
    || left.address.localeCompare(right.address));
}

function collectListeners(ssExecutable) {
  if (!ssExecutable) return { listeners: [], error: "ss_missing" };
  const result = spawnSync(ssExecutable, ["-H", "-lntup"], {
    encoding: "utf8",
    timeout: 10_000,
    maxBuffer: 4 * 1024 * 1024,
  });
  if (result.status !== 0) return { listeners: [], error: "ss_failed" };
  return { listeners: parseSsListeners(result.stdout), error: null };
}

function collectReleaseState(releaseRoot) {
  let stat = null;
  try {
    const value = fs.statSync(releaseRoot);
    stat = { uid: value.uid, gid: value.gid, mode: value.mode & 0o7777, directory: value.isDirectory() };
  } catch {}
  const revisionResult = spawnSync("git", ["-C", releaseRoot, "rev-parse", "HEAD"], {
    encoding: "utf8",
    timeout: 10_000,
    maxBuffer: 64 * 1024,
  });
  const statusResult = spawnSync("git", ["-C", releaseRoot, "status", "--porcelain=v1", "--untracked-files=normal"], {
    encoding: "utf8",
    timeout: 30_000,
    maxBuffer: 4 * 1024 * 1024,
  });
  return {
    root: releaseRoot,
    stat,
    git_revision: revisionResult.status === 0 ? revisionResult.stdout.trim() : null,
    git_status_available: statusResult.status === 0,
    dirty: statusResult.status === 0 ? statusResult.stdout.length > 0 : null,
  };
}

async function resolveHost(host, skipDns) {
  if (!host || skipDns) return [];
  try {
    const records = await dns.lookup(host, { all: true, verbatim: true });
    return [...new Set(records.map((record) => record.address))].sort();
  } catch {
    return [];
  }
}

async function collectSnapshot({ phase, skipDns = false, env = process.env } = {}) {
  if (!PHASES.has(phase)) fail("snapshot phase must be provision or runtime");
  if (process.platform !== "linux") fail("WebRTC deployment preflight is Linux-only");
  const studio = studioHost(env.STUDIO_PUBLIC_ORIGIN);
  const turn = normalizedHost(env.TURN_PUBLIC_HOST);
  const executables = Object.fromEntries(REQUIRED_EXECUTABLES.map((name) => [name, findExecutable(name, env.PATH)]));
  const listenerResult = collectListeners(executables.ss);
  const releaseRoot = path.resolve(__dirname, "../../..");
  return {
    schema: SNAPSHOT_SCHEMA,
    phase,
    captured_at: new Date().toISOString(),
    platform: process.platform,
    effective_uid: typeof process.geteuid === "function" ? process.geteuid() : null,
    effective_gid: typeof process.getegid === "function" ? process.getegid() : null,
    environment: {
      studio_public_origin: String(env.STUDIO_PUBLIC_ORIGIN || "").trim(),
      studio_host: studio,
      turn_public_host: turn,
      turn_external_ip: String(env.TURN_EXTERNAL_IP || "").trim(),
      runtime_uid: positiveId(env.SIMWORLD_RUNTIME_UID),
      runtime_gid: positiveId(env.SIMWORLD_RUNTIME_GID),
      secret_gid: positiveId(env.SIMWORLD_SECRET_GID),
      turn_config_gid: positiveId(env.TURN_CONFIG_GID),
      build_revision: String(env.SIMWORLD_BUILD_REVISION || "").trim(),
      pool_size: boundedInteger(env.UE_POOL_SIZE, 3, 1, 64),
      port_stride: boundedInteger(env.UE_PORT_STRIDE, 2, 1, 100),
      base_mcp_port: boundedInteger(env.UE_BASE_MCP_PORT, 55559, 1024, 65535),
      base_cirrus_http: boundedInteger(env.UE_BASE_CIRRUS_HTTP, 8585, 1024, 65535),
      base_cirrus_ws: boundedInteger(env.UE_BASE_CIRRUS_WS, 8586, 1024, 65535),
      base_cirrus_sfu: boundedInteger(env.UE_BASE_CIRRUS_SFU, 8989, 1024, 65535),
      base_ucv_port: boundedInteger(env.UE_BASE_UCV, 9017, 1024, 65535),
    },
    executables,
    accounts: {
      simworld: getentRecord("passwd", "simworld"),
      simworld_group: getentRecord("group", "simworld"),
      turnserver_group: getentRecord("group", "turnserver"),
    },
    release: collectReleaseState(releaseRoot),
    dns: {
      studio: await resolveHost(studio, skipDns),
      turn: await resolveHost(turn, skipDns),
      skipped: Boolean(skipDns),
    },
    listeners: listenerResult.listeners,
    listener_error: listenerResult.error,
  };
}

function isLoopbackAddress(address) {
  const value = String(address || "").replace(/^\[|\]$/g, "").toLowerCase();
  return value === "127.0.0.1" || value.startsWith("127.") || value === "::1";
}

function isPublicBinding(address) {
  const value = String(address || "").replace(/^\[|\]$/g, "").toLowerCase();
  return value === "*" || value === "0.0.0.0" || value === "::" || !isLoopbackAddress(value);
}

function processMatches(listener, expected) {
  return typeof listener.process_name === "string" && expected.includes(listener.process_name.toLowerCase());
}

function forbiddenControlPorts(environment) {
  const fixed = new Set([3002, 55432, 6333, 6334, 7777]);
  const inputs = [
    environment.pool_size,
    environment.port_stride,
    environment.base_mcp_port,
    environment.base_cirrus_http,
    environment.base_cirrus_ws,
    environment.base_cirrus_sfu,
    environment.base_ucv_port,
  ];
  if (inputs.some((value) => !Number.isSafeInteger(value))) return fixed;
  for (let slot = 0; slot < environment.pool_size; slot += 1) {
    const offset = slot * environment.port_stride;
    for (const base of [
      environment.base_mcp_port,
      environment.base_cirrus_http,
      environment.base_cirrus_ws,
      environment.base_cirrus_sfu,
      environment.base_ucv_port,
    ]) {
      if (base + offset <= 65535) fixed.add(base + offset);
    }
  }
  return fixed;
}

function check(id, status, summary, evidence = {}) {
  return { id, status, summary, evidence };
}

function evaluateSnapshot(snapshot) {
  if (!snapshot || snapshot.schema !== SNAPSHOT_SCHEMA || !PHASES.has(snapshot.phase)) {
    fail("snapshot does not match simworld-webrtc-host-snapshot/v1");
  }
  const environment = snapshot.environment || {};
  const listeners = Array.isArray(snapshot.listeners) ? snapshot.listeners : [];
  const checks = [];

  checks.push(check(
    "linux_host",
    snapshot.platform === "linux" ? "pass" : "blocked",
    snapshot.platform === "linux" ? "Linux host confirmed" : "Deployment tooling requires Linux",
    { platform: snapshot.platform || null },
  ));

  const missingExecutables = REQUIRED_EXECUTABLES.filter((name) => !snapshot.executables?.[name]);
  checks.push(check(
    "required_packages",
    missingExecutables.length === 0 ? "pass" : "blocked",
    missingExecutables.length === 0 ? "Required WebRTC deployment executables are present" : "Administrator packages are missing",
    { missing: missingExecutables },
  ));

  const identityIssues = [];
  for (const [field, value] of [
    ["runtime_uid", environment.runtime_uid],
    ["runtime_gid", environment.runtime_gid],
    ["secret_gid", environment.secret_gid],
    ["turn_config_gid", environment.turn_config_gid],
  ]) if (!positiveId(value)) identityIssues.push(`${field}_invalid`);
  if (snapshot.accounts?.simworld?.uid !== environment.runtime_uid) identityIssues.push("simworld_uid_mismatch");
  if (snapshot.accounts?.simworld?.gid !== environment.runtime_gid) identityIssues.push("simworld_primary_gid_mismatch");
  if (snapshot.accounts?.simworld_group?.gid !== environment.secret_gid) identityIssues.push("simworld_secret_gid_mismatch");
  if (snapshot.accounts?.turnserver_group?.gid !== environment.turn_config_gid) identityIssues.push("turnserver_config_gid_mismatch");
  checks.push(check(
    "numeric_identity",
    identityIssues.length === 0 ? "pass" : "blocked",
    identityIssues.length === 0 ? "Runtime and secret numeric identities match host NSS records" : "Runtime or secret identity is unresolved or mismatched",
    { issues: [...new Set(identityIssues)].sort() },
  ));

  const configuredStudioHost = studioHost(environment.studio_public_origin);
  const configuredTurnHost = normalizedHost(environment.turn_public_host);
  const externalIp = publicIp(environment.turn_external_ip);
  const externalIpValid = externalIp !== null;
  const buildRevisionValid = SHA40_RE.test(String(environment.build_revision || ""));
  const configIssues = [];
  if (!configuredStudioHost || configuredStudioHost !== environment.studio_host) configIssues.push("studio_origin_invalid");
  if (!configuredTurnHost) configIssues.push("turn_host_invalid");
  if (!externalIpValid) configIssues.push("turn_external_ip_invalid");
  if (!buildRevisionValid) configIssues.push("build_revision_invalid");
  if (configuredStudioHost && configuredStudioHost === configuredTurnHost) configIssues.push("studio_turn_dns_not_separated");
  checks.push(check(
    "deployment_identity",
    configIssues.length === 0 ? "pass" : "blocked",
    configIssues.length === 0 ? "Public origins, TURN address, and build revision are pinned" : "Public deployment identity is incomplete or invalid",
    { issues: configIssues },
  ));

  const releaseIssues = [];
  if (!snapshot.release?.stat?.directory) releaseIssues.push("release_root_missing");
  if (snapshot.release?.stat?.uid !== 0) releaseIssues.push("release_root_not_root_owned");
  if (Number.isInteger(snapshot.release?.stat?.mode) && (snapshot.release.stat.mode & 0o022) !== 0) {
    releaseIssues.push("release_root_group_or_world_writable");
  }
  if (!SHA40_RE.test(String(snapshot.release?.git_revision || ""))) releaseIssues.push("release_revision_unavailable");
  if (snapshot.release?.git_revision !== environment.build_revision) releaseIssues.push("release_revision_mismatch");
  if (snapshot.release?.git_status_available !== true) releaseIssues.push("release_status_unavailable");
  if (snapshot.release?.dirty !== false) releaseIssues.push("release_tree_dirty");
  checks.push(check(
    "sealed_release",
    releaseIssues.length === 0 ? "pass" : "blocked",
    releaseIssues.length === 0 ? "Root-owned clean release matches the pinned build revision" : "Release ownership or Git state is not deployment-safe",
    {
      issues: releaseIssues,
      git_revision: snapshot.release?.git_revision || null,
      root_uid: snapshot.release?.stat?.uid ?? null,
      root_mode: snapshot.release?.stat?.mode ?? null,
    },
  ));

  const dnsIssues = [];
  if (snapshot.dns?.skipped) dnsIssues.push("dns_skipped");
  if (!Array.isArray(snapshot.dns?.studio) || snapshot.dns.studio.length === 0) dnsIssues.push("studio_dns_unresolved");
  if (!Array.isArray(snapshot.dns?.turn) || snapshot.dns.turn.length === 0) dnsIssues.push("turn_dns_unresolved");
  if (externalIpValid && Array.isArray(snapshot.dns?.turn) && !snapshot.dns.turn.includes(externalIp)) {
    dnsIssues.push("turn_dns_external_ip_mismatch");
  }
  checks.push(check(
    "public_dns",
    dnsIssues.length === 0 ? "pass" : "blocked",
    dnsIssues.length === 0 ? "Studio and TURN DNS resolve, and TURN matches the pinned external IP" : "Public DNS is not deployment-ready",
    { issues: dnsIssues, studio_addresses: snapshot.dns?.studio || [], turn_addresses: snapshot.dns?.turn || [] },
  ));

  checks.push(check(
    "listener_visibility",
    !snapshot.listener_error && (snapshot.phase !== "runtime" || snapshot.effective_uid === 0) ? "pass" : "blocked",
    !snapshot.listener_error && (snapshot.phase !== "runtime" || snapshot.effective_uid === 0)
      ? "Listener inventory is available at the required privilege"
      : "Run runtime preflight as root with a working ss command",
    { error: snapshot.listener_error || null, effective_uid: snapshot.effective_uid },
  ));

  const exposedControls = listeners.filter((listener) => forbiddenControlPorts(environment).has(listener.port) && isPublicBinding(listener.address));
  checks.push(check(
    "control_plane_loopback",
    exposedControls.length === 0 ? "pass" : "blocked",
    exposedControls.length === 0 ? "Node, asset, Cirrus, MCP, SFU, and UnrealCV ports are not publicly bound" : "A control-plane listener is publicly bound",
    { listeners: exposedControls },
  ));

  const publicTcp = (port) => listeners.filter((entry) => entry.protocol === "tcp" && entry.port === port && isPublicBinding(entry.address));
  const publicUdp = (port) => listeners.filter((entry) => entry.protocol === "udp" && entry.port === port && isPublicBinding(entry.address));
  const ingress = [...publicTcp(80), ...publicTcp(443)];
  const turn = [...publicTcp(3478), ...publicUdp(3478), ...publicTcp(5349)];

  if (snapshot.phase === "provision") {
    checks.push(check(
      "ingress_ownership_decision",
      ingress.length === 0 ? "pass" : "blocked",
      ingress.length === 0 ? "Public TCP 80/443 are available for reviewed ingress" : "Public TCP 80/443 already have an owner; administrator routing decision is required",
      { listeners: ingress },
    ));
    checks.push(check(
      "turn_ports_available",
      turn.length === 0 ? "pass" : "blocked",
      turn.length === 0 ? "TURN TCP/UDP 3478 and TLS 5349 are available" : "A TURN deployment port is already occupied",
      { listeners: turn },
    ));
  } else {
    const ingressIssues = [];
    for (const port of [80, 443]) {
      const matches = publicTcp(port);
      if (matches.length === 0) ingressIssues.push(`tcp_${port}_missing`);
      else if (!matches.some((entry) => processMatches(entry, ["nginx"]))) ingressIssues.push(`tcp_${port}_not_nginx`);
    }
    checks.push(check(
      "public_https_ingress",
      ingressIssues.length === 0 ? "pass" : "blocked",
      ingressIssues.length === 0 ? "Nginx owns public TCP 80/443" : "Public HTTPS ingress topology is incomplete",
      { issues: ingressIssues, listeners: ingress },
    ));

    const turnIssues = [];
    for (const [protocol, port] of [["tcp", 3478], ["udp", 3478], ["tcp", 5349]]) {
      const matches = listeners.filter((entry) => entry.protocol === protocol && entry.port === port && isPublicBinding(entry.address));
      if (matches.length === 0) turnIssues.push(`${protocol}_${port}_missing`);
      else if (!matches.some((entry) => processMatches(entry, ["turnserver", "coturn"]))) turnIssues.push(`${protocol}_${port}_not_coturn`);
    }
    checks.push(check(
      "public_turn_listeners",
      turnIssues.length === 0 ? "pass" : "blocked",
      turnIssues.length === 0 ? "Coturn owns public TCP/UDP 3478 and TCP/TLS 5349" : "Public TURN listener topology is incomplete",
      { issues: turnIssues, listeners: turn },
    ));
  }

  const blocked = checks.filter((entry) => entry.status === "blocked").map((entry) => entry.id);
  return {
    schema: REPORT_SCHEMA,
    phase: snapshot.phase,
    captured_at: snapshot.captured_at,
    ready: blocked.length === 0,
    host_runtime_ready: snapshot.phase === "runtime" && blocked.length === 0,
    production_ready: false,
    blocked_checks: blocked,
    checks,
    external_gates: [
      "administrator_dns_tls_firewall_approval",
      "forced_udp_tcp_tls_relay_matrix",
      "interactive_video_input_reconnect_evidence",
      "signed_webrtc_readiness_receipt",
    ],
  };
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  const snapshot = await collectSnapshot(options);
  process.stdout.write(`${JSON.stringify(evaluateSnapshot(snapshot), null, 2)}\n`);
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`webrtc-deployment-preflight: ${error.message}\n`);
    process.exitCode = 2;
  });
}

module.exports = {
  REPORT_SCHEMA,
  SNAPSHOT_SCHEMA,
  collectSnapshot,
  evaluateSnapshot,
  forbiddenControlPorts,
  isPublicBinding,
  parseArguments,
  parseSsListeners,
  positiveId,
  publicIp,
  studioHost,
};
