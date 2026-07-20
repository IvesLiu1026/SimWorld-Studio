"use strict";

/**
 * Operator-controlled lifecycle registry. Callers acquire a lease before any
 * stack listener starts, heartbeat it while the owner process is alive, and
 * release it during graceful shutdown. The injected listenerProbe must inspect
 * only the supplied endpoints and return the exact requested endpoints that
 * are listening; this module never scans or terminates host processes.
 */

const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const net = require("node:net");
const path = require("node:path");

const REGISTRY_SCHEMA = "simworld-process-port-registry/v1";
const LOCK_SCHEMA = "simworld-process-port-registry-lock/v1";
const DEFAULT_REGISTRY_FILE = "process-port-registry.json";
const MAX_LEASES = 256;
const MAX_PORTS_PER_LEASE = 32;
const MAX_CONFLICTS = 64;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$/;
const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const SAFE_START_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$/;
const VALID_PROTOCOLS = new Set(["tcp", "udp"]);
const VALID_POLICIES = new Set(["reject", "warn"]);

const ERROR_MESSAGES = Object.freeze({
  PROCESS_PORT_CONFLICT: "The requested stack resources conflict with an active registry lease.",
  PROCESS_PORT_UNMANAGED_LISTENER: "A requested port already has an unmanaged listener.",
  PROCESS_PORT_LISTENER_PROBE_FAILED: "Listener ownership could not be verified.",
  PROCESS_PORT_REGISTRY_BUSY: "The process and port registry is busy.",
  PROCESS_PORT_REGISTRY_CORRUPT: "The process and port registry is invalid.",
  PROCESS_PORT_OWNER_IDENTITY_UNAVAILABLE: "The process start identity could not be verified.",
  PROCESS_PORT_LEASE_ACCESS_DENIED: "The lease owner identity did not match.",
});

class ProcessPortRegistryError extends Error {
  constructor(code, details) {
    super(ERROR_MESSAGES[code] || "The process and port registry operation failed.");
    this.name = "ProcessPortRegistryError";
    this.code = code;
    if (details !== undefined) this.details = details;
  }
}

function assertPlainObject(value, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${field} must be a plain object`);
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) {
    throw new TypeError(`${field} must be a plain object`);
  }
}

function safeId(value, field) {
  const normalized = String(value ?? "").trim();
  if (!SAFE_ID.test(normalized)) throw new TypeError(`${field} is invalid`);
  return normalized;
}

function safeName(value, field) {
  const normalized = String(value ?? "").trim();
  if (!SAFE_NAME.test(normalized)) throw new TypeError(`${field} is invalid`);
  return normalized;
}

function safePid(value, field = "ownerPid") {
  const pid = Number(value);
  if (!Number.isInteger(pid) || pid < 1 || pid > 2_147_483_647) {
    throw new TypeError(`${field} must be a positive integer PID`);
  }
  return pid;
}

function safeStartToken(value, field = "ownerStartToken") {
  const token = String(value ?? "").trim();
  if (!SAFE_START_TOKEN.test(token)) throw new TypeError(`${field} is invalid`);
  return token;
}

function timestamp(now) {
  const value = now();
  const milliseconds = value instanceof Date ? value.getTime() : Number(value);
  if (!Number.isFinite(milliseconds)) throw new TypeError("now() must return a finite timestamp");
  return new Date(milliseconds).toISOString();
}

function fingerprint(value) {
  return crypto.createHash("sha256").update(String(value)).digest("hex").slice(0, 16);
}

function canonicalHost(value) {
  const host = String(value || "127.0.0.1").trim().toLowerCase();
  if (host === "localhost") return "127.0.0.1";
  if (net.isIP(host) === 0) throw new TypeError("port host must be an IP literal or localhost");
  return host;
}

function normalizeEndpoint(raw, field) {
  assertPlainObject(raw, field);
  const port = Number(raw.port);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new TypeError(`${field}.port must be an integer from 1 to 65535`);
  }
  const protocol = String(raw.protocol || "tcp").trim().toLowerCase();
  if (!VALID_PROTOCOLS.has(protocol)) throw new TypeError(`${field}.protocol must be tcp or udp`);
  return Object.freeze({
    name: safeName(raw.name, `${field}.name`),
    host: canonicalHost(raw.host),
    protocol,
    port,
  });
}

function endpointKey(endpoint) {
  return `${endpoint.protocol}:${endpoint.host}:${endpoint.port}`;
}

function normalizeEndpoints(rawPorts, field = "ports") {
  if (!Array.isArray(rawPorts) || rawPorts.length < 1 || rawPorts.length > MAX_PORTS_PER_LEASE) {
    throw new TypeError(`${field} must contain 1 to ${MAX_PORTS_PER_LEASE} endpoints`);
  }
  const endpoints = rawPorts.map((entry, index) => normalizeEndpoint(entry, `${field}[${index}]`));
  const endpointKeys = new Set();
  const names = new Set();
  for (const endpoint of endpoints) {
    const key = endpointKey(endpoint);
    if (endpointKeys.has(key)) throw new TypeError(`${field} contains a duplicate endpoint`);
    if (names.has(endpoint.name)) throw new TypeError(`${field} contains a duplicate name`);
    endpointKeys.add(key);
    names.add(endpoint.name);
  }
  return Object.freeze(endpoints);
}

function normalizeLeaseRequest(raw) {
  assertPlainObject(raw, "lease request");
  return Object.freeze({
    stackId: safeId(raw.stackId, "stackId"),
    slotId: safeId(raw.slotId, "slotId"),
    gpuId: safeId(raw.gpuId, "gpuId"),
    portFamily: safeId(raw.portFamily, "portFamily"),
    ports: normalizeEndpoints(raw.ports),
    ownerPid: safePid(raw.ownerPid),
    ownerStartToken: raw.ownerStartToken == null
      ? null
      : safeStartToken(raw.ownerStartToken),
  });
}

function normalizePolicy(rawPolicy) {
  if (typeof rawPolicy === "string") {
    if (!VALID_POLICIES.has(rawPolicy)) throw new TypeError("policy must be reject or warn");
    return Object.freeze({ conflicts: rawPolicy, unmanagedListeners: rawPolicy, probeFailure: rawPolicy });
  }
  if (rawPolicy === undefined) rawPolicy = {};
  assertPlainObject(rawPolicy, "policy");
  const allowed = new Set(["conflicts", "unmanagedListeners", "probeFailure"]);
  for (const key of Object.keys(rawPolicy)) {
    if (!allowed.has(key)) throw new TypeError(`Unknown process registry policy: ${key}`);
  }
  const result = {};
  for (const key of allowed) {
    const value = rawPolicy[key] === undefined ? "reject" : rawPolicy[key];
    if (!VALID_POLICIES.has(value)) throw new TypeError(`${key} policy must be reject or warn`);
    result[key] = value;
  }
  return Object.freeze(result);
}

function hostsOverlap(left, right) {
  if (left === right) return true;
  if (left === "0.0.0.0" || right === "0.0.0.0") {
    return net.isIP(left) === 4 && net.isIP(right) === 4;
  }
  if (left === "::" || right === "::") {
    return net.isIP(left) === 6 && net.isIP(right) === 6;
  }
  return false;
}

function endpointsOverlap(left, right) {
  return left.protocol === right.protocol
    && left.port === right.port
    && hostsOverlap(left.host, right.host);
}

async function defaultProcessProbe(pid) {
  const normalizedPid = safePid(pid, "pid");
  if (process.platform !== "linux") return { alive: true, startToken: null };
  try {
    // A focused procfs read proves PID liveness and detects PID reuse without
    // sending a signal or enumerating any unrelated host process.
    const stat = await fs.readFile(`/proc/${normalizedPid}/stat`, "utf8");
    const closeParen = stat.lastIndexOf(")");
    const fieldsAfterComm = closeParen >= 0 ? stat.slice(closeParen + 1).trim().split(/\s+/) : [];
    const startTicks = fieldsAfterComm[19];
    return {
      alive: true,
      startToken: /^\d+$/.test(startTicks || "") ? `linux:${startTicks}` : null,
    };
  } catch (error) {
    if (error && error.code === "ENOENT") return { alive: false, startToken: null };
    throw error;
  }
}

function normalizeProbeIdentity(raw) {
  if (typeof raw === "boolean") return { alive: raw, startToken: null };
  assertPlainObject(raw, "process probe result");
  if (typeof raw.alive !== "boolean") throw new TypeError("process probe result.alive must be boolean");
  return {
    alive: raw.alive,
    startToken: raw.startToken == null ? null : safeStartToken(raw.startToken, "process probe startToken"),
  };
}

function publicEndpoint(endpoint) {
  return {
    name: endpoint.name,
    host: endpoint.host,
    protocol: endpoint.protocol,
    port: endpoint.port,
  };
}

function publicLease(record, liveness) {
  return {
    leaseId: record.leaseId,
    stackId: record.stackId,
    slotId: record.slotId,
    gpuId: record.gpuId,
    portFamily: record.portFamily,
    ports: record.ports.map(publicEndpoint),
    owner: {
      pid: record.owner.pid,
      startTokenFingerprint: fingerprint(record.owner.startToken),
    },
    acquiredAt: record.acquiredAt,
    heartbeatAt: record.heartbeatAt,
    liveness: liveness ? liveness.state : "not_checked",
    ...(liveness && liveness.reason ? { livenessReason: liveness.reason } : {}),
  };
}

function normalizeStoredRecord(raw, index) {
  try {
    assertPlainObject(raw, `leases[${index}]`);
    const record = {
      leaseId: safeId(raw.leaseId, `leases[${index}].leaseId`),
      stackId: safeId(raw.stackId, `leases[${index}].stackId`),
      slotId: safeId(raw.slotId, `leases[${index}].slotId`),
      gpuId: safeId(raw.gpuId, `leases[${index}].gpuId`),
      portFamily: safeId(raw.portFamily, `leases[${index}].portFamily`),
      ports: normalizeEndpoints(raw.ports, `leases[${index}].ports`).map((endpoint) => ({ ...endpoint })),
      owner: null,
      acquiredAt: String(raw.acquiredAt || ""),
      heartbeatAt: String(raw.heartbeatAt || ""),
    };
    assertPlainObject(raw.owner, `leases[${index}].owner`);
    record.owner = {
      pid: safePid(raw.owner.pid, `leases[${index}].owner.pid`),
      startToken: safeStartToken(raw.owner.startToken, `leases[${index}].owner.startToken`),
    };
    if (!Number.isFinite(Date.parse(record.acquiredAt)) || !Number.isFinite(Date.parse(record.heartbeatAt))) {
      throw new TypeError("lease timestamps are invalid");
    }
    return record;
  } catch (_error) {
    throw new ProcessPortRegistryError("PROCESS_PORT_REGISTRY_CORRUPT");
  }
}

function newRegistry() {
  return { schema: REGISTRY_SCHEMA, revision: 0, updatedAt: null, leases: [] };
}

class ProcessPortRegistry {
  constructor({
    directory,
    registryFile = DEFAULT_REGISTRY_FILE,
    policy,
    processProbe = defaultProcessProbe,
    listenerProbe,
    listenerProbeTimeoutMs = 750,
    now = () => Date.now(),
    randomUUID = () => crypto.randomUUID(),
    sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
    lockAttempts = 100,
    lockRetryMs = 10,
  } = {}) {
    if (!directory || !path.isAbsolute(directory)) {
      throw new TypeError("directory must be an absolute operator-controlled path");
    }
    if (!SAFE_NAME.test(registryFile)) throw new TypeError("registryFile is invalid");
    if (typeof processProbe !== "function") throw new TypeError("processProbe must be a function");
    if (typeof listenerProbe !== "function") throw new TypeError("listenerProbe must be a function");
    if (typeof now !== "function" || typeof randomUUID !== "function" || typeof sleep !== "function") {
      throw new TypeError("now, randomUUID, and sleep must be functions");
    }
    if (!Number.isInteger(lockAttempts) || lockAttempts < 1 || lockAttempts > 10_000) {
      throw new TypeError("lockAttempts must be an integer from 1 to 10000");
    }
    if (!Number.isInteger(lockRetryMs) || lockRetryMs < 0 || lockRetryMs > 1_000) {
      throw new TypeError("lockRetryMs must be an integer from 0 to 1000");
    }
    if (!Number.isInteger(listenerProbeTimeoutMs) || listenerProbeTimeoutMs < 1 || listenerProbeTimeoutMs > 10_000) {
      throw new TypeError("listenerProbeTimeoutMs must be an integer from 1 to 10000");
    }
    this.directory = directory;
    this.registryPath = path.join(directory, registryFile);
    this.lockPath = `${this.registryPath}.lock`;
    this.policy = normalizePolicy(policy);
    this.processProbe = processProbe;
    this.listenerProbe = listenerProbe;
    this.listenerProbeTimeoutMs = listenerProbeTimeoutMs;
    this.now = now;
    this.randomUUID = randomUUID;
    this.sleep = sleep;
    this.lockAttempts = lockAttempts;
    this.lockRetryMs = lockRetryMs;
  }

  async acquire(rawRequest) {
    const request = normalizeLeaseRequest(rawRequest);
    const ownerStartToken = await this._resolveOwnerStartToken(request.ownerPid, request.ownerStartToken);
    return this._withLock(async () => {
      const registry = await this._readRegistry();
      const classified = await this._classifyRecords(registry.leases);
      const staleIds = new Set(classified.filter((entry) => entry.liveness.state === "stale").map((entry) => entry.record.leaseId));
      const liveEntries = classified.filter((entry) => !staleIds.has(entry.record.leaseId));
      const conflicts = this._findConflicts(request, liveEntries);
      const warnings = [];

      if (conflicts.length > 0) {
        if (this.policy.conflicts === "reject") {
          throw new ProcessPortRegistryError("PROCESS_PORT_CONFLICT", { conflicts });
        }
        warnings.push({ code: "PROCESS_PORT_CONFLICT", conflicts });
      }

      let unmanagedListeners;
      try {
        unmanagedListeners = await this._probeRequestedListeners(request.ports);
      } catch (_error) {
        if (this.policy.probeFailure === "reject") {
          throw new ProcessPortRegistryError("PROCESS_PORT_LISTENER_PROBE_FAILED");
        }
        warnings.push({ code: "PROCESS_PORT_LISTENER_PROBE_FAILED" });
        unmanagedListeners = [];
      }
      if (unmanagedListeners.length > 0) {
        if (this.policy.unmanagedListeners === "reject") {
          throw new ProcessPortRegistryError("PROCESS_PORT_UNMANAGED_LISTENER", {
            listeners: unmanagedListeners,
          });
        }
        warnings.push({ code: "PROCESS_PORT_UNMANAGED_LISTENER", listeners: unmanagedListeners });
      }

      const at = timestamp(this.now);
      const leaseToken = safeName(this.randomUUID(), "generated lease token");
      const record = {
        leaseId: safeName(`lease-${leaseToken}`, "generated leaseId"),
        stackId: request.stackId,
        slotId: request.slotId,
        gpuId: request.gpuId,
        portFamily: request.portFamily,
        ports: request.ports.map(publicEndpoint),
        owner: { pid: request.ownerPid, startToken: ownerStartToken },
        acquiredAt: at,
        heartbeatAt: at,
      };
      registry.leases = registry.leases.filter((lease) => !staleIds.has(lease.leaseId));
      registry.leases.push(record);
      await this._writeRegistry(registry);
      return {
        acquired: true,
        lease: publicLease(record, { state: "alive" }),
        warnings,
        cleanup: { staleLeasesRemoved: staleIds.size },
      };
    });
  }

  async heartbeat(owner) {
    const identity = this._normalizeLeaseOwner(owner);
    return this._withLock(async () => {
      const registry = await this._readRegistry();
      const record = registry.leases.find((lease) => lease.leaseId === identity.leaseId);
      if (!record) return { updated: false };
      this._assertOwner(record, identity);
      record.heartbeatAt = timestamp(this.now);
      await this._writeRegistry(registry);
      return { updated: true, lease: publicLease(record, { state: "alive" }) };
    });
  }

  async release(owner) {
    const identity = this._normalizeLeaseOwner(owner);
    return this._withLock(async () => {
      const registry = await this._readRegistry();
      const index = registry.leases.findIndex((lease) => lease.leaseId === identity.leaseId);
      if (index < 0) return { released: false };
      this._assertOwner(registry.leases[index], identity);
      const [record] = registry.leases.splice(index, 1);
      await this._writeRegistry(registry);
      return { released: true, lease: publicLease(record, { state: "released" }) };
    });
  }

  async status() {
    const registry = await this._readRegistry();
    const classified = await this._classifyRecords(registry.leases);
    return {
      schema: REGISTRY_SCHEMA,
      revision: registry.revision,
      updatedAt: registry.updatedAt,
      leases: classified.map(({ record, liveness }) => publicLease(record, liveness)),
      summary: classified.reduce((summary, entry) => {
        summary.total += 1;
        summary[entry.liveness.state] += 1;
        return summary;
      }, { total: 0, alive: 0, stale: 0, unknown: 0 }),
    };
  }

  async cleanupStale() {
    return this._withLock(async () => {
      const registry = await this._readRegistry();
      const classified = await this._classifyRecords(registry.leases);
      const stale = classified.filter((entry) => entry.liveness.state === "stale");
      const staleIds = new Set(stale.map((entry) => entry.record.leaseId));
      if (staleIds.size > 0) {
        registry.leases = registry.leases.filter((record) => !staleIds.has(record.leaseId));
        await this._writeRegistry(registry);
      }
      return {
        removedCount: stale.length,
        removed: stale.map(({ record, liveness }) => publicLease(record, liveness)),
        retainedCount: classified.length - stale.length,
      };
    });
  }

  async _resolveOwnerStartToken(pid, expectedStartToken = null) {
    let identity;
    try {
      identity = normalizeProbeIdentity(await this.processProbe(pid));
    } catch (_error) {
      throw new ProcessPortRegistryError("PROCESS_PORT_OWNER_IDENTITY_UNAVAILABLE");
    }
    if (!identity.alive || !identity.startToken || (expectedStartToken && identity.startToken !== expectedStartToken)) {
      throw new ProcessPortRegistryError("PROCESS_PORT_OWNER_IDENTITY_UNAVAILABLE");
    }
    return expectedStartToken || identity.startToken;
  }

  _normalizeLeaseOwner(owner) {
    assertPlainObject(owner, "lease owner");
    return {
      leaseId: safeId(owner.leaseId, "leaseId"),
      ownerPid: safePid(owner.ownerPid),
      ownerStartToken: safeStartToken(owner.ownerStartToken),
    };
  }

  _assertOwner(record, identity) {
    const pidMatches = record.owner.pid === identity.ownerPid;
    const left = Buffer.from(record.owner.startToken);
    const right = Buffer.from(identity.ownerStartToken);
    const tokenMatches = left.length === right.length && crypto.timingSafeEqual(left, right);
    if (!pidMatches || !tokenMatches) {
      throw new ProcessPortRegistryError("PROCESS_PORT_LEASE_ACCESS_DENIED");
    }
  }

  async _classifyRecords(records) {
    return Promise.all(records.map(async (record) => {
      try {
        const identity = normalizeProbeIdentity(await this.processProbe(record.owner.pid));
        if (!identity.alive) {
          return { record, liveness: { state: "stale", reason: "OWNER_PID_NOT_RUNNING" } };
        }
        if (!identity.startToken) {
          return { record, liveness: { state: "unknown", reason: "OWNER_START_TOKEN_UNVERIFIED" } };
        }
        if (identity.startToken !== record.owner.startToken) {
          return { record, liveness: { state: "stale", reason: "OWNER_START_TOKEN_MISMATCH" } };
        }
        return { record, liveness: { state: "alive" } };
      } catch (_error) {
        return { record, liveness: { state: "unknown", reason: "OWNER_PROBE_FAILED" } };
      }
    }));
  }

  _findConflicts(request, liveEntries) {
    const conflicts = [];
    for (const { record, liveness } of liveEntries) {
      if (record.slotId === request.slotId) {
        conflicts.push(this._publicConflict("slot", request.slotId, record, liveness));
      }
      if (record.gpuId === request.gpuId) {
        conflicts.push(this._publicConflict("gpu", request.gpuId, record, liveness));
      }
      for (const requestedPort of request.ports) {
        for (const leasedPort of record.ports) {
          if (!endpointsOverlap(requestedPort, leasedPort)) continue;
          conflicts.push(this._publicConflict(
            "port",
            `${requestedPort.protocol}:${requestedPort.host}:${requestedPort.port}`,
            record,
            liveness,
          ));
          if (conflicts.length >= MAX_CONFLICTS) return conflicts;
        }
      }
      if (conflicts.length >= MAX_CONFLICTS) return conflicts;
    }
    return conflicts;
  }

  _publicConflict(resourceType, resource, record, liveness) {
    return {
      resourceType,
      resource,
      conflictingLeaseId: record.leaseId,
      conflictingStackId: record.stackId,
      liveness: liveness.state,
    };
  }

  _normalizeListenerResult(raw, requestedPorts) {
    if (!Array.isArray(raw)) throw new ProcessPortRegistryError("PROCESS_PORT_LISTENER_PROBE_FAILED");
    const requested = new Map(requestedPorts.map((endpoint) => [endpointKey(endpoint), endpoint]));
    const listeners = [];
    const seen = new Set();
    for (let index = 0; index < raw.length; index += 1) {
      let endpoint;
      try {
        endpoint = normalizeEndpoint(raw[index], `listenerProbe[${index}]`);
      } catch (_error) {
        throw new ProcessPortRegistryError("PROCESS_PORT_LISTENER_PROBE_FAILED");
      }
      const key = endpointKey(endpoint);
      if (!requested.has(key)) throw new ProcessPortRegistryError("PROCESS_PORT_LISTENER_PROBE_FAILED");
      if (seen.has(key)) continue;
      seen.add(key);
      listeners.push(publicEndpoint(requested.get(key)));
    }
    return listeners;
  }

  async _probeRequestedListeners(requestedPorts) {
    const controller = new AbortController();
    let timeout;
    const deadline = new Promise((_, reject) => {
      timeout = setTimeout(() => {
        controller.abort();
        reject(new Error("listener probe timeout"));
      }, this.listenerProbeTimeoutMs);
    });
    try {
      const raw = await Promise.race([
        Promise.resolve().then(() => this.listenerProbe({
          ports: requestedPorts.map(publicEndpoint),
          signal: controller.signal,
        })),
        deadline,
      ]);
      return this._normalizeListenerResult(raw, requestedPorts);
    } finally {
      clearTimeout(timeout);
    }
  }

  async _ensureDirectory() {
    await fs.mkdir(this.directory, { recursive: true, mode: 0o700 });
    const stat = await fs.lstat(this.directory);
    if (!stat.isDirectory() || stat.isSymbolicLink()) {
      throw new ProcessPortRegistryError("PROCESS_PORT_REGISTRY_CORRUPT");
    }
  }

  async _withLock(operation) {
    await this._ensureDirectory();
    const lockId = safeName(`lock-${safeName(this.randomUUID(), "generated lock token")}`, "generated lockId");
    let handle = null;
    for (let attempt = 0; attempt < this.lockAttempts; attempt += 1) {
      try {
        handle = await fs.open(this.lockPath, "wx", 0o600);
        await handle.writeFile(`${JSON.stringify({
          schema: LOCK_SCHEMA,
          lockId,
          ownerPid: process.pid,
          acquiredAt: timestamp(this.now),
        })}\n`, "utf8");
        await handle.sync();
        break;
      } catch (error) {
        if (handle) {
          await handle.close().catch(() => {});
          handle = null;
        }
        if (!error || error.code !== "EEXIST") throw error;
        if (attempt + 1 < this.lockAttempts) await this.sleep(this.lockRetryMs);
      }
    }
    if (!handle) throw new ProcessPortRegistryError("PROCESS_PORT_REGISTRY_BUSY");

    try {
      return await operation();
    } finally {
      await handle.close().catch(() => {});
      try {
        const raw = JSON.parse(await fs.readFile(this.lockPath, "utf8"));
        if (raw && raw.lockId === lockId) await fs.unlink(this.lockPath);
      } catch (_error) {
        // A missing/replaced lock fails closed for future writers; never unlink it blindly.
      }
    }
  }

  async _readRegistry() {
    await this._ensureDirectory();
    let parsed;
    try {
      parsed = JSON.parse(await fs.readFile(this.registryPath, "utf8"));
    } catch (error) {
      if (error && error.code === "ENOENT") return newRegistry();
      throw new ProcessPortRegistryError("PROCESS_PORT_REGISTRY_CORRUPT");
    }
    try {
      assertPlainObject(parsed, "registry");
      if (parsed.schema !== REGISTRY_SCHEMA) throw new TypeError("registry schema is invalid");
      if (!Number.isInteger(parsed.revision) || parsed.revision < 0) throw new TypeError("revision is invalid");
      if (parsed.updatedAt !== null && !Number.isFinite(Date.parse(String(parsed.updatedAt)))) {
        throw new TypeError("updatedAt is invalid");
      }
      if (!Array.isArray(parsed.leases) || parsed.leases.length > MAX_LEASES) {
        throw new TypeError("leases are invalid");
      }
      const leases = parsed.leases.map(normalizeStoredRecord);
      if (new Set(leases.map((lease) => lease.leaseId)).size !== leases.length) {
        throw new TypeError("lease ids are not unique");
      }
      return { schema: REGISTRY_SCHEMA, revision: parsed.revision, updatedAt: parsed.updatedAt, leases };
    } catch (error) {
      if (error instanceof ProcessPortRegistryError) throw error;
      throw new ProcessPortRegistryError("PROCESS_PORT_REGISTRY_CORRUPT");
    }
  }

  async _writeRegistry(registry) {
    if (registry.leases.length > MAX_LEASES) throw new ProcessPortRegistryError("PROCESS_PORT_REGISTRY_CORRUPT");
    const next = {
      schema: REGISTRY_SCHEMA,
      revision: registry.revision + 1,
      updatedAt: timestamp(this.now),
      leases: registry.leases,
    };
    const tempToken = safeName(this.randomUUID(), "generated temp token");
    const tempPath = path.join(this.directory, `.${path.basename(this.registryPath)}.${tempToken}.tmp`);
    let handle;
    try {
      handle = await fs.open(tempPath, "wx", 0o600);
      await handle.writeFile(`${JSON.stringify(next, null, 2)}\n`, "utf8");
      await handle.sync();
      await handle.close();
      handle = null;
      await fs.rename(tempPath, this.registryPath);
      registry.revision = next.revision;
      registry.updatedAt = next.updatedAt;
    } finally {
      if (handle) await handle.close().catch(() => {});
      await fs.unlink(tempPath).catch((error) => {
        if (!error || error.code !== "ENOENT") throw error;
      });
    }
  }
}

function createProcessPortRegistry(options) {
  return new ProcessPortRegistry(options);
}

module.exports = {
  REGISTRY_SCHEMA,
  ProcessPortRegistry,
  ProcessPortRegistryError,
  createProcessPortRegistry,
  defaultProcessProbe,
};
