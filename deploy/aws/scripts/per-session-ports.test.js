"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  current,
  currentEnv,
  middleware,
  sessionTokenFromCookie,
  setSessionManager,
} = require("./per-session-ports");

const TOKEN = "c".repeat(64);

test("per-session routing accepts only one exact HttpOnly-cookie token shape", () => {
  assert.equal(sessionTokenFromCookie(`vista_stream_session=${TOKEN}`), TOKEN);
  assert.equal(sessionTokenFromCookie(`a=1; vista_stream_session=${TOKEN}; b=2`), TOKEN);
  assert.equal(sessionTokenFromCookie(`vista_stream_session=${TOKEN}; vista_stream_session=${TOKEN}`), "");
  assert.equal(sessionTokenFromCookie("vista_stream_session=short"), "");
  assert.equal(sessionTokenFromCookie(""), "");
});

test("middleware scopes internal ports without propagating the bearer into child environments", async () => {
  const record = {
    token: TOKEN,
    slotId: 2,
    userId: "browser-test",
    mcpReady: true,
    uePorts: { mcpPort: 55563, ucvPort: 9019 },
  };
  setSessionManager({ touch: (token) => token === TOKEN ? record : null });
  let observed = null;
  middleware()(
    {
      headers: {
        cookie: `vista_stream_session=${TOKEN}`,
        "x-session-token": "d".repeat(64),
      },
      query: { sessionToken: "e".repeat(64) },
    },
    {},
    () => {
      observed = { context: current(), env: currentEnv({ PATH: "/bin" }) };
    },
  );
  assert.equal(observed.context.slotId, 2);
  assert.equal(observed.context.userId, "browser-test");
  assert.equal(Object.hasOwn(observed.context, "token"), false);
  assert.equal(observed.env.UNREAL_PORT, "55563");
  assert.equal(observed.env.UCV_PORT, "9019");
  assert.equal(Object.hasOwn(observed.env, "SIMWORLD_SESSION_TOKEN"), false);
  assert.equal(observed.env.SIMWORLD_SLOT_ID, "2");
});
