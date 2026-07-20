"use strict";

const crypto = require("crypto");

function cleanId(value) {
  const id = String(value || "").trim();
  return id || null;
}

function abortError(reason) {
  const error = new Error(reason || "review run cancelled");
  error.name = "AbortError";
  error.code = "REVIEW_RUN_CANCELLED";
  return error;
}

class ReviewRunRegistry {
  constructor({ randomUUID = () => crypto.randomUUID() } = {}) {
    this.randomUUID = randomUUID;
    this.runs = new Map();
    this.currentByScope = new Map();
  }

  start({ scopeId, runId } = {}) {
    const scope = cleanId(scopeId) || "_global";
    const id = cleanId(runId) || this.randomUUID();
    if (this.runs.has(id)) {
      const error = new Error(`review run already exists: ${id}`);
      error.code = "REVIEW_RUN_CONFLICT";
      throw error;
    }

    const previousId = this.currentByScope.get(scope);
    if (previousId) this.cancel({ scopeId: scope, runId: previousId, reason: "superseded" });

    const controller = new AbortController();
    const record = {
      runId: id,
      scopeId: scope,
      controller,
      signal: controller.signal,
      startedAt: Date.now(),
    };
    this.runs.set(id, record);
    this.currentByScope.set(scope, id);
    return record;
  }

  get({ scopeId, runId } = {}) {
    const scope = cleanId(scopeId);
    const id = cleanId(runId) || (scope && this.currentByScope.get(scope));
    if (!id) return null;
    const record = this.runs.get(id) || null;
    if (record && scope && record.scopeId !== scope) return null;
    return record;
  }

  cancel({ scopeId, runId, reason = "user_stop" } = {}) {
    const record = this.get({ scopeId, runId });
    if (!record) return null;
    if (!record.signal.aborted) record.controller.abort(abortError(reason));
    return record;
  }

  complete({ scopeId, runId } = {}) {
    const record = this.get({ scopeId, runId });
    if (!record) return false;
    this.runs.delete(record.runId);
    if (this.currentByScope.get(record.scopeId) === record.runId) {
      this.currentByScope.delete(record.scopeId);
    }
    return true;
  }

  get size() {
    return this.runs.size;
  }
}

module.exports = { ReviewRunRegistry, abortError };
