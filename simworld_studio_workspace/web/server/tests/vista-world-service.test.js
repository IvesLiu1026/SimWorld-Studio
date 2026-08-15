"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  createVistaWorldFileCatalog,
  createVistaWorldService,
} = require("../vista-world-service");
const { createVistaWorldUeAdapter, TOOL_NAME } = require("../vista-world-ue-adapter");

const IDENTITY = Object.freeze({
  ownerId: "owner-world",
  sessionId: "studio-world",
  leaseId: "lease-world",
  slotId: 2,
  mcpPort: 55571,
});

function fixtureCatalog() {
  return {
    revision(revision) {
      if (revision !== "r1") throw Object.assign(new Error("missing"), { code: "VISTA_WORLD_REVISION_NOT_FOUND", status: 404 });
      return {
        house_id: "home.r1",
        revision: "r1",
        content_digest: "a".repeat(64),
        rooms: [{ id: "home.r1/room.living_room" }],
        portals: [{}],
        entities: [{}, {}, {}],
      };
    },
    event(eventId, revision) {
      if (eventId !== "mmg_044" || revision !== "r1") throw new Error("missing");
      return { event_id: eventId, compatible_revision: revision };
    },
  };
}

function fixtureTransport() {
  const calls = [];
  return {
    calls,
    async send(payload) {
      calls.push(payload);
      return {
        command_id: payload.command_id,
        status: "completed",
        code: "VISTA_WORLD_OK",
        session_generation: payload.session_generation + 1,
        target_semantic_id: payload.target_semantic_id,
        state: { applied: true },
      };
    },
  };
}

test("service binds a lease, enforces generation, and sends only typed interactions", async () => {
  const transport = fixtureTransport();
  const service = createVistaWorldService({ catalog: fixtureCatalog(), transport });
  const session = await service.createSession({ revision: "r1" }, IDENTITY);
  assert.match(session.session_id, /^vws-[a-f0-9]{24}$/);
  const result = await service.action(session.session_id, {
    kind: "interaction",
    generation: 0,
    requester_semantic_id: "home.r1/player.01",
    target_semantic_id: "home.r1/portal.entry-living.01",
    affordance: "open",
  }, IDENTITY);
  assert.equal(result.generation, 1);
  assert.equal(transport.calls[0].operation, "interaction");
  assert.equal(transport.calls[0].expected_revision, "r1");
  assert.deepEqual(Object.keys(transport.calls[0]).sort(), [
    "affordance", "command_id", "expected_revision", "operation",
    "requester_semantic_id", "session_generation", "target_semantic_id",
  ]);
  await assert.rejects(
    service.action(session.session_id, {
      kind: "interaction",
      generation: 0,
      requester_semantic_id: "home.r1/player.01",
      target_semantic_id: "home.r1/portal.entry-living.01",
      affordance: "close",
    }, IDENTITY),
    { code: "VISTA_WORLD_GENERATION_STALE" },
  );
});

test("service validates bounded NPC queues and rejects arbitrary executable fields", async () => {
  const transport = fixtureTransport();
  const service = createVistaWorldService({ catalog: fixtureCatalog(), transport });
  const session = await service.createSession({ revision: "r1" }, IDENTITY);
  const result = await service.action(session.session_id, {
    kind: "npc_queue",
    generation: 0,
    npc_semantic_id: "home.r1/npc.resident.01",
    replace: true,
    actions: [
      { action_id: "walk-kitchen", type: "navigate_to", target_semantic_id: "home.r1/room.kitchen_dining" },
      { action_id: "wait-one", type: "wait", duration_sec: 1, timeout_sec: 2 },
    ],
  }, IDENTITY);
  assert.equal(result.generation, 1);
  assert.equal(transport.calls[0].operation, "npc_queue");
  await assert.rejects(
    service.action(session.session_id, {
      kind: "npc_queue",
      generation: 1,
      npc_semantic_id: "home.r1/npc.resident.01",
      replace: true,
      actions: [{ action_id: "bad", type: "execute_python", script: "open('/secret')" }],
    }, IDENTITY),
    { code: "VISTA_WORLD_INPUT_INVALID" },
  );
  assert.equal(JSON.stringify(transport.calls).includes("script"), false);
});

test("verified event start and reset use the fixed event transport", async () => {
  const transport = fixtureTransport();
  const service = createVistaWorldService({ catalog: fixtureCatalog(), transport });
  const session = await service.createSession({ revision: "r1" }, IDENTITY);
  const started = await service.startEvent(session.session_id, "mmg_044", { generation: 0 }, IDENTITY);
  assert.equal(started.active_event, "mmg_044");
  assert.equal(transport.calls[0].event_operation, "start_event");
  const reset = await service.resetEvent(session.session_id, { generation: 1 }, IDENTITY);
  assert.equal(reset.active_event, null);
  assert.equal(transport.calls[1].event_operation, "reset_event");
});

test("session identity cannot be caller-swapped", async () => {
  const service = createVistaWorldService({ catalog: fixtureCatalog(), transport: fixtureTransport() });
  const session = await service.createSession({ revision: "r1" }, IDENTITY);
  await assert.rejects(
    service.status(session.session_id, { ...IDENTITY, leaseId: "other-lease" }),
    { code: "VISTA_WORLD_SESSION_STALE" },
  );
});

test("file catalog is contained and excludes incompatible events", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-world-catalog-"));
  fs.mkdirSync(path.join(root, "events"));
  fs.writeFileSync(path.join(root, "house.json"), JSON.stringify({ house_id: "home.r1", revision: "r1" }));
  fs.writeFileSync(path.join(root, "events", "mmg_044.json"), JSON.stringify({
    event_id: "mmg_044",
    compatible_house: { house_id: "home.r1", revision: "r1" },
  }));
  const catalog = createVistaWorldFileCatalog({ root });
  assert.equal(catalog.revision("r1").house_id, "home.r1");
  assert.equal(catalog.event("mmg_044", "r1").event_id, "mmg_044");
  assert.throws(() => catalog.event("../private", "r1"), { code: "VISTA_WORLD_INPUT_INVALID" });
  fs.rmSync(root, { recursive: true, force: true });
});

test("UE adapter uses one fixed tool and forwards no caller-selected command name", async () => {
  const calls = [];
  const adapter = createVistaWorldUeAdapter({
    resolveUeBroker: () => ({
      async send(name, payload, options) {
        calls.push({ name, payload, options });
        return { command_id: payload.command_id, status: "completed" };
      },
    }),
  });
  await adapter.send({ operation: "event", command_id: "vwc-" + "a".repeat(24) }, IDENTITY);
  assert.equal(calls[0].name, TOOL_NAME);
  assert.equal(calls[0].name, "vista_world_action");
  await assert.rejects(adapter.send({ operation: "execute_python_script" }, IDENTITY));
});
