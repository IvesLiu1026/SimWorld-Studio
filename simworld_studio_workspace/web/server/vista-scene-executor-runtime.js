"use strict";

const SAFE_REVISION = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const SHA256 = /^[a-f0-9]{64}$/;

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function resolveVistaSceneExecutorConfig(env = process.env, options = {}) {
  const contentRevision = text(env.VISTA_UE_CONTENT_REVISION);
  const verificationRevision = text(env.VISTA_ASSET_VERIFICATION_REVISION);
  const contentReceiptSha256 = text(env.VISTA_UE_CONTENT_RECEIPT_SHA256);
  const configured = [contentRevision, verificationRevision, contentReceiptSha256].filter(Boolean).length;
  if (configured === 0) return Object.freeze({ enabled: false });
  if (configured !== 3) {
    throw new TypeError(
      "VISTA_UE_CONTENT_REVISION, VISTA_ASSET_VERIFICATION_REVISION, and "
      + "VISTA_UE_CONTENT_RECEIPT_SHA256 must be configured together",
    );
  }
  if (!SAFE_REVISION.test(contentRevision) || !SAFE_REVISION.test(verificationRevision)) {
    throw new TypeError("VISTA scene executor revisions are invalid");
  }
  if (!SHA256.test(contentReceiptSha256)) {
    throw new TypeError("VISTA_UE_CONTENT_RECEIPT_SHA256 must be a lowercase SHA-256 digest");
  }

  const assetConfig = options.assetConfig || { enabled: false };
  const production = String(env.NODE_ENV || "").trim().toLowerCase() === "production";
  if (production && assetConfig.enabled !== true) {
    throw new TypeError("Production VISTA scene execution requires the verified semantic asset runtime");
  }
  if (assetConfig.enabled === true && assetConfig.ueContentRevision !== contentRevision) {
    throw new TypeError("VISTA scene executor content revision does not match the verified asset snapshot");
  }
  return Object.freeze({
    enabled: true,
    contentRevision,
    verificationRevision,
    contentReceiptSha256,
  });
}

function createVistaSlotBrokerResolver(options = {}) {
  const studioStreaming = options.studioStreaming;
  const defaultBroker = options.defaultBroker;
  const BrokerClass = options.BrokerClass;
  if (!studioStreaming || typeof studioStreaming.isActiveSessionBinding !== "function") {
    throw new TypeError("studioStreaming.isActiveSessionBinding is required");
  }
  if (!defaultBroker || typeof defaultBroker.send !== "function"
      || !Number.isSafeInteger(defaultBroker.port)) {
    throw new TypeError("defaultBroker with a fixed port is required");
  }
  if (typeof BrokerClass !== "function") throw new TypeError("BrokerClass is required");
  const brokers = new Map();

  return function resolveVistaSlotBroker(context) {
    const identity = {
      ownerId: context && context.ownerId,
      sessionId: context && context.sessionId,
      slotId: context && context.slotId,
      leaseId: context && context.leaseId,
      mcpPort: context && context.mcpPort,
    };
    try {
      if (!studioStreaming.isActiveSessionBinding(identity)) return null;
    } catch (_error) {
      return null;
    }
    const port = Number(identity.mcpPort);
    const slotId = Number(identity.slotId);
    if (!Number.isSafeInteger(port) || port < 1 || port > 65535
        || !Number.isSafeInteger(slotId) || slotId < 0 || slotId > 1023) return null;
    const key = `${slotId}:${port}`;
    if (!brokers.has(key)) {
      brokers.set(key, defaultBroker.port === port
        ? defaultBroker
        : new BrokerClass({ host: "127.0.0.1", port }));
    }
    return brokers.get(key);
  };
}

module.exports = {
  createVistaSlotBrokerResolver,
  resolveVistaSceneExecutorConfig,
};
