"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const ARTIFACT_SCHEMA = "vista-import-artifact/v1";
const IDEMPOTENCY_SCHEMA = "vista-import-idempotency/v1";
const ARTIFACT_REVISION = 1;
const DEFAULT_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024;
const ARTIFACT_ID_PATTERN = /^vim_[a-f0-9]{64}$/;
const PRINCIPAL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/;
const PROFILE_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const SAFE_CAUSE_CODE_PATTERN = /^[A-Z][A-Z0-9_]{2,127}$/;
const FORBIDDEN_REQUEST_KEY_PATTERN = /(?:^|[_-])(?:path|file(?:name)?|directory|dir|root)(?:$|[_-])/i;

class VistaImportServiceError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "VistaImportServiceError";
    this.code = code;
    this.statusCode = Number.isInteger(details.statusCode) ? details.statusCode : 500;
    this.retryable = Boolean(details.retryable);
    if (details.causeCode && SAFE_CAUSE_CODE_PATTERN.test(details.causeCode)) {
      this.causeCode = details.causeCode;
    }
  }

  toJSON() {
    return {
      name: this.name,
      code: this.code,
      message: this.message,
      status_code: this.statusCode,
      retryable: this.retryable,
      ...(this.causeCode ? { cause_code: this.causeCode } : {}),
    };
  }
}

function serviceError(code, message, details) {
  return new VistaImportServiceError(code, message, details);
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function safeCauseCode(error) {
  const code = error && typeof error.code === "string" ? error.code.trim().toUpperCase() : "";
  return SAFE_CAUSE_CODE_PATTERN.test(code) ? code : undefined;
}

function isCallerPathKey(key) {
  const value = String(key || "");
  if (FORBIDDEN_REQUEST_KEY_PATTERN.test(value)) return true;
  // Cover camelCase variants such as sourcePath/artifactRoot without treating
  // legitimate fields like profile as filesystem selectors.
  const compact = value.replace(/[_-]/g, "").toLowerCase();
  return compact === "file"
    || compact === "filename"
    || compact.endsWith("path")
    || compact.endsWith("filepath")
    || compact.endsWith("directory")
    || compact.endsWith("root");
}

function isCallerPathValue(value) {
  if (typeof value !== "string") return false;
  const text = value.trim();
  return /^file:/i.test(text)
    || text.startsWith("/")
    || text.startsWith("\\\\")
    || text.startsWith("~/")
    || /^[A-Za-z]:[\\/]/.test(text)
    || /(?:^|[\\/])\.\.(?:[\\/]|$)/.test(text);
}

function wrapDependencyError(error, code, message, statusCode = 422) {
  if (error instanceof VistaImportServiceError) return error;
  const dependencyCode = safeCauseCode(error);
  if (dependencyCode && dependencyCode.startsWith("VISTA_")) {
    const dependencyStatus = [error && error.statusCode, error && error.status]
      .find((value) => Number.isInteger(value) && value >= 400 && value <= 599) || statusCode;
    return serviceError(dependencyCode, String(error && error.message || message), {
      statusCode: dependencyStatus,
      retryable: Boolean(error && error.retryable),
    });
  }
  return serviceError(code, message, {
    statusCode,
    retryable: Boolean(error && error.retryable),
    causeCode: safeCauseCode(error),
  });
}

function assertSafeRequest(request) {
  if (!isPlainObject(request)) {
    throw serviceError("VISTA_IMPORT_REQUEST_INVALID", "Import request must be an object", {
      statusCode: 400,
    });
  }

  // Dataset addressing belongs to the allowlisted source adapter.  Refuse path-
  // shaped input here as a second boundary so neither persistence nor a future
  // importer implementation can accidentally accept a caller-selected file.
  const pending = [{ value: request, depth: 0 }];
  let visited = 0;
  while (pending.length) {
    const { value, depth } = pending.pop();
    if (depth > 12 || ++visited > 10_000) {
      throw serviceError("VISTA_IMPORT_REQUEST_INVALID", "Import request is too deeply nested", {
        statusCode: 400,
      });
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item && typeof item === "object") pending.push({ value: item, depth: depth + 1 });
        if (isCallerPathValue(item)) {
          throw serviceError("VISTA_IMPORT_PATH_FORBIDDEN", "Caller-selected filesystem paths are not allowed", {
            statusCode: 400,
          });
        }
      }
      continue;
    }
    if (!isPlainObject(value)) {
      throw serviceError("VISTA_IMPORT_REQUEST_INVALID", "Import request contains an unsupported value", {
        statusCode: 400,
      });
    }
    for (const [key, item] of Object.entries(value)) {
      if (isCallerPathKey(key)) {
        throw serviceError("VISTA_IMPORT_PATH_FORBIDDEN", "Caller-selected filesystem paths are not allowed", {
          statusCode: 400,
        });
      }
      if (isCallerPathValue(item)) {
        throw serviceError("VISTA_IMPORT_PATH_FORBIDDEN", "Caller-selected filesystem paths are not allowed", {
          statusCode: 400,
        });
      }
      if (item && typeof item === "object") pending.push({ value: item, depth: depth + 1 });
      else if (typeof item === "function" || typeof item === "symbol" || typeof item === "bigint") {
        throw serviceError("VISTA_IMPORT_REQUEST_INVALID", "Import request contains an unsupported value", {
          statusCode: 400,
        });
      }
    }
  }
}

function readString(candidates) {
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return null;
}

function normalizeChecksum(value) {
  const checksum = readString([value]);
  if (!checksum || checksum.length > 256 || /[\x00-\x1f\x7f\s/\\]/.test(checksum)) return null;
  if (/^(?:sha256:)?[a-fA-F0-9]{64}$/.test(checksum)) return checksum.toLowerCase();
  return checksum;
}

function extractIdentity(preview) {
  if (!isPlainObject(preview)) {
    throw serviceError("VISTA_IMPORT_PREVIEW_INVALID", "Importer preview did not return an object", {
      statusCode: 422,
    });
  }
  const source = isPlainObject(preview.source) ? preview.source : {};
  const provenance = isPlainObject(preview.provenance) ? preview.provenance : {};
  const privilege = isPlainObject(preview.privilege) ? preview.privilege : {};

  const sourceChecksum = normalizeChecksum(readString([
    source.source_checksum,
    provenance.source_checksum,
    preview.source_checksum,
  ]));
  const importerVersion = readString([
    provenance.importer_version,
    preview.importer_version,
  ]);
  const profile = (readString([privilege.profile, preview.profile]) || "").toLowerCase();

  if (!sourceChecksum) {
    throw serviceError("VISTA_IMPORT_PROVENANCE_MISSING", "Importer preview is missing a valid source checksum", {
      statusCode: 422,
    });
  }
  if (!importerVersion || importerVersion.length > 128 || /[\x00-\x1f\x7f]/.test(importerVersion)) {
    throw serviceError("VISTA_IMPORT_PROVENANCE_MISSING", "Importer preview is missing a valid importer version", {
      statusCode: 422,
    });
  }
  if (!PROFILE_PATTERN.test(profile)) {
    throw serviceError("VISTA_IMPORT_PROVENANCE_MISSING", "Importer preview is missing a valid privilege profile", {
      statusCode: 422,
    });
  }

  const canonical = JSON.stringify({
    schema: IDEMPOTENCY_SCHEMA,
    source_checksum: sourceChecksum,
    importer_version: importerVersion,
    profile,
  });
  const digest = crypto.createHash("sha256").update(canonical, "utf8").digest("hex");
  return Object.freeze({
    sourceChecksum,
    importerVersion,
    profile,
    digest,
    idempotencyKey: `sha256:${digest}`,
    artifactId: `vim_${digest}`,
    sceneId: `vis_${digest}`,
  });
}

function scopeIdentityToOwner(identity, ownerId) {
  const digest = crypto.createHash("sha256")
    .update("vista-import-owner-scope/v1\0", "utf8")
    .update(ownerId, "utf8")
    .update("\0", "utf8")
    .update(identity.digest, "utf8")
    .digest("hex");
  return Object.freeze({
    ...identity,
    artifactId: `vim_${digest}`,
  });
}

function normalizePrincipal(value, field) {
  const principal = typeof value === "string" ? value.trim() : "";
  if (!PRINCIPAL_PATTERN.test(principal)) {
    throw serviceError("VISTA_IMPORT_ACCESS_CONTEXT_INVALID", `A valid ${field} is required`, {
      statusCode: 400,
    });
  }
  return principal;
}

function normalizeAccessContext(context) {
  if (!isPlainObject(context)) {
    throw serviceError("VISTA_IMPORT_ACCESS_CONTEXT_INVALID", "Import access context is required", {
      statusCode: 400,
    });
  }
  return {
    ownerId: normalizePrincipal(context.ownerId, "ownerId"),
    sessionId: normalizePrincipal(context.sessionId, "sessionId"),
  };
}

function normalizeArtifactId(value) {
  const id = typeof value === "string" ? value.trim() : "";
  if (!ARTIFACT_ID_PATTERN.test(id)) {
    throw serviceError("VISTA_IMPORT_ID_INVALID", "Import artifact id is invalid", { statusCode: 400 });
  }
  return id;
}

function safeArtifactPath(root, artifactId) {
  const filename = `${normalizeArtifactId(artifactId)}.json`;
  const target = path.resolve(root, filename);
  if (path.dirname(target) !== root) {
    throw serviceError("VISTA_IMPORT_ID_INVALID", "Import artifact id is invalid", { statusCode: 400 });
  }
  return target;
}

function isoNow(clock) {
  const value = clock();
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) {
    throw serviceError("VISTA_IMPORT_CLOCK_INVALID", "Artifact clock returned an invalid timestamp", {
      statusCode: 500,
    });
  }
  return date.toISOString();
}

function serializeArtifact(artifact, maxBytes) {
  let json;
  try {
    json = `${JSON.stringify(artifact, null, 2)}\n`;
  } catch (_error) {
    throw serviceError("VISTA_IMPORT_ARTIFACT_INVALID", "Import artifact is not JSON serializable", {
      statusCode: 422,
    });
  }
  if (Buffer.byteLength(json, "utf8") > maxBytes) {
    throw serviceError("VISTA_IMPORT_ARTIFACT_TOO_LARGE", "Import artifact exceeds the configured size limit", {
      statusCode: 413,
    });
  }
  return json;
}

function assertArtifactShape(artifact, expectedId) {
  if (!isPlainObject(artifact)
      || artifact.schema !== ARTIFACT_SCHEMA
      || artifact.revision !== ARTIFACT_REVISION
      || artifact.artifact_id !== expectedId
      || artifact.run_id !== expectedId
      || artifact.status !== "committed"
      || !isPlainObject(artifact.access)
      || !isPlainObject(artifact.idempotency)
      || typeof artifact.created_at !== "string"
      || typeof artifact.updated_at !== "string") {
    throw serviceError("VISTA_IMPORT_ARTIFACT_CORRUPT", "Stored import artifact failed validation", {
      statusCode: 500,
    });
  }
  return artifact;
}

function assertArtifactAccess(artifact, access) {
  // The immutable artifact belongs to the authenticated browser principal, not
  // to one short-lived UE lease.  The creating session remains provenance, but
  // a restarted Studio may reattach the same owner through a new active lease.
  if (artifact.access.owner_id !== access.ownerId) {
    throw serviceError("VISTA_IMPORT_ACCESS_DENIED", "Import artifact is not available to this owner", {
      statusCode: 403,
    });
  }
}

class VistaImportService {
  constructor(options = {}) {
    if (!options.importer || typeof options.importer.preview !== "function") {
      throw new TypeError("VistaImportService requires importer.preview(request)");
    }
    if (typeof options.artifactRoot !== "string" || !options.artifactRoot.trim()) {
      throw new TypeError("VistaImportService requires artifactRoot");
    }
    const artifactRoot = path.resolve(options.artifactRoot);
    if (artifactRoot === path.parse(artifactRoot).root) {
      throw new TypeError("VistaImportService artifactRoot cannot be a filesystem root");
    }

    this.importer = options.importer;
    this.artifactRoot = artifactRoot;
    this.clock = typeof options.clock === "function" ? options.clock : () => new Date();
    this.randomBytes = typeof options.randomBytes === "function" ? options.randomBytes : crypto.randomBytes;
    this.maxArtifactBytes = Number.isSafeInteger(options.maxArtifactBytes) && options.maxArtifactBytes > 0
      ? options.maxArtifactBytes
      : DEFAULT_MAX_ARTIFACT_BYTES;
  }

  async preview(request) {
    assertSafeRequest(request);
    let result;
    try {
      result = await this.importer.preview(request);
    } catch (error) {
      throw wrapDependencyError(
        error,
        "VISTA_IMPORT_PREVIEW_FAILED",
        "VISTA importer could not preview this source",
        422,
      );
    }
    if (!isPlainObject(result)) {
      throw serviceError("VISTA_IMPORT_PREVIEW_INVALID", "Importer preview did not return an object", {
        statusCode: 422,
      });
    }
    return result;
  }

  async exportEvaluationSafe(request) {
    if (typeof this.importer.exportEvaluationSafe !== "function") {
      throw serviceError(
        "VISTA_IMPORT_EVALUATION_EXPORT_UNSUPPORTED",
        "This importer does not support evaluation-safe export",
        { statusCode: 501 },
      );
    }
    const preview = await this.preview(request);
    try {
      return await this.importer.exportEvaluationSafe(preview);
    } catch (error) {
      throw wrapDependencyError(
        error,
        "VISTA_IMPORT_EVALUATION_EXPORT_FAILED",
        "VISTA importer could not create an evaluation-safe export",
        422,
      );
    }
  }

  async commit(request, context = {}) {
    const access = normalizeAccessContext(context);
    const preview = await this.preview(request);
    const identity = scopeIdentityToOwner(extractIdentity(preview), access.ownerId);

    await this._ensureArtifactRoot();
    const existing = await this._readArtifact(identity.artifactId, { allowMissing: true });
    if (existing) {
      this._assertMatchingIdentity(existing, identity);
      assertArtifactAccess(existing, access);
      return { ...existing, created: false };
    }

    let evaluationSafe;
    const includeEvaluationSafe = Boolean(context.includeEvaluationSafe || context.evaluationSafe);
    if (includeEvaluationSafe) {
      if (typeof this.importer.exportEvaluationSafe !== "function") {
        throw serviceError(
          "VISTA_IMPORT_EVALUATION_EXPORT_UNSUPPORTED",
          "This importer does not support evaluation-safe export",
          { statusCode: 501 },
        );
      }
      try {
        evaluationSafe = await this.importer.exportEvaluationSafe(preview);
      } catch (error) {
        throw wrapDependencyError(
          error,
          "VISTA_IMPORT_EVALUATION_EXPORT_FAILED",
          "VISTA importer could not create an evaluation-safe export",
          422,
        );
      }
    }

    const timestamp = isoNow(this.clock);
    const artifact = {
      schema: ARTIFACT_SCHEMA,
      revision: ARTIFACT_REVISION,
      artifact_revision: identity.idempotencyKey,
      artifact_id: identity.artifactId,
      run_id: identity.artifactId,
      scene_id: identity.sceneId,
      status: "committed",
      profile: identity.profile,
      idempotency: {
        schema: IDEMPOTENCY_SCHEMA,
        key: identity.idempotencyKey,
        source_checksum: identity.sourceChecksum,
        importer_version: identity.importerVersion,
        profile: identity.profile,
      },
      access: {
        owner_id: access.ownerId,
        session_id: access.sessionId,
      },
      created_at: timestamp,
      updated_at: timestamp,
      scene_spec: preview,
      ...(evaluationSafe === undefined ? {} : { evaluation_safe: evaluationSafe }),
    };

    const persisted = await this._persistArtifact(artifact);
    const winner = persisted.artifact;
    this._assertMatchingIdentity(winner, identity);
    assertArtifactAccess(winner, access);
    return { ...winner, created: persisted.created };
  }

  async status(artifactId, context = {}) {
    const id = normalizeArtifactId(artifactId);
    const access = normalizeAccessContext(context);
    const artifact = await this._readArtifact(id, { allowMissing: false });
    assertArtifactAccess(artifact, access);
    return artifact;
  }

  _assertMatchingIdentity(artifact, identity) {
    if (artifact.idempotency.key !== identity.idempotencyKey
        || artifact.idempotency.source_checksum !== identity.sourceChecksum
        || artifact.idempotency.importer_version !== identity.importerVersion
        || artifact.idempotency.profile !== identity.profile) {
      throw serviceError("VISTA_IMPORT_ARTIFACT_CORRUPT", "Stored import artifact identity does not match", {
        statusCode: 500,
      });
    }
  }

  async _ensureArtifactRoot() {
    try {
      await fs.promises.mkdir(this.artifactRoot, { recursive: true, mode: 0o700 });
      const stat = await fs.promises.stat(this.artifactRoot);
      if (!stat.isDirectory()) throw new Error("not a directory");
    } catch (error) {
      throw wrapDependencyError(
        error,
        "VISTA_IMPORT_STORAGE_UNAVAILABLE",
        "Import artifact storage is unavailable",
        503,
      );
    }
  }

  async _readArtifact(artifactId, { allowMissing }) {
    const target = safeArtifactPath(this.artifactRoot, artifactId);
    const noFollow = Number(fs.constants.O_NOFOLLOW || 0);
    let handle;
    try {
      handle = await fs.promises.open(target, fs.constants.O_RDONLY | noFollow);
      const stat = await handle.stat();
      if (!stat.isFile() || stat.size > this.maxArtifactBytes) {
        throw serviceError("VISTA_IMPORT_ARTIFACT_CORRUPT", "Stored import artifact failed validation", {
          statusCode: 500,
        });
      }
      const raw = await handle.readFile({ encoding: "utf8" });
      let artifact;
      try {
        artifact = JSON.parse(raw);
      } catch (_error) {
        throw serviceError("VISTA_IMPORT_ARTIFACT_CORRUPT", "Stored import artifact failed validation", {
          statusCode: 500,
        });
      }
      return assertArtifactShape(artifact, artifactId);
    } catch (error) {
      if (error && error.code === "ENOENT" && allowMissing) return null;
      if (error && error.code === "ENOENT") {
        throw serviceError("VISTA_IMPORT_NOT_FOUND", "Import artifact was not found", { statusCode: 404 });
      }
      if (error instanceof VistaImportServiceError) throw error;
      throw wrapDependencyError(
        error,
        "VISTA_IMPORT_STORAGE_UNAVAILABLE",
        "Import artifact storage is unavailable",
        503,
      );
    } finally {
      if (handle) await handle.close().catch(() => {});
    }
  }

  async _persistArtifact(artifact) {
    const json = serializeArtifact(artifact, this.maxArtifactBytes);
    const target = safeArtifactPath(this.artifactRoot, artifact.artifact_id);
    let temporary;
    let handle;

    try {
      for (let attempt = 0; attempt < 4; attempt += 1) {
        const suffix = this.randomBytes(12).toString("hex");
        if (!/^[a-f0-9]{24}$/.test(suffix)) {
          throw serviceError("VISTA_IMPORT_RANDOM_SOURCE_INVALID", "Artifact random source returned invalid bytes", {
            statusCode: 500,
          });
        }
        temporary = path.resolve(this.artifactRoot, `.tmp-${artifact.artifact_id}-${suffix}`);
        if (path.dirname(temporary) !== this.artifactRoot) {
          throw serviceError("VISTA_IMPORT_STORAGE_UNAVAILABLE", "Import artifact storage is unavailable", {
            statusCode: 503,
          });
        }
        try {
          handle = await fs.promises.open(temporary, "wx", 0o600);
          break;
        } catch (error) {
          if (!error || error.code !== "EEXIST" || attempt === 3) throw error;
        }
      }

      await handle.writeFile(json, { encoding: "utf8" });
      await handle.sync();
      await handle.close();
      handle = null;

      // A hard link publishes the fully-written inode without replacing an
      // existing artifact.  EEXIST therefore selects the first concurrent
      // committer as the immutable idempotency winner.
      let created = false;
      try {
        await fs.promises.link(temporary, target);
        created = true;
      } catch (error) {
        if (!error || error.code !== "EEXIST") throw error;
      }
      await fs.promises.unlink(temporary).catch(() => {});
      temporary = null;
      return {
        artifact: await this._readArtifact(artifact.artifact_id, { allowMissing: false }),
        created,
      };
    } catch (error) {
      if (error instanceof VistaImportServiceError) throw error;
      throw wrapDependencyError(
        error,
        "VISTA_IMPORT_STORAGE_UNAVAILABLE",
        "Import artifact storage is unavailable",
        503,
      );
    } finally {
      if (handle) await handle.close().catch(() => {});
      if (temporary) await fs.promises.unlink(temporary).catch(() => {});
    }
  }
}

function createVistaImportService(options) {
  return new VistaImportService(options);
}

module.exports = {
  ARTIFACT_SCHEMA,
  VistaImportServiceError,
  VistaImportService,
  createVistaImportService,
};
