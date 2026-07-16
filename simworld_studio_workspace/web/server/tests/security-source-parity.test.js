"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

test("source server keeps the loopback and model gates wired", () => {
  const indexPath = path.resolve(__dirname, "../index.js");
  const source = fs.readFileSync(indexPath, "utf8");
  assert.doesNotMatch(source, /require\(["']cors["']\)/);
  assert.doesNotMatch(source, /app\.use\(cors\(/);
  assert.doesNotMatch(source, /app\.listen\([^\n]*["']0\.0\.0\.0["']/);
  assert.match(source, /app\.listen\(PORT,STUDIO_HOST,/);
  assert.match(source, /app\.use\(requestLoopbackGuard\)/);
  assert.match(source, /app\.use\(createAccessGuard\(STUDIO_ACCESS_TOKEN\)\)/);
  assert.match(
    source,
    /app\.use\(createLoopbackBrowserHeaders\(\{signalingPort:VISTA_DEMO_ENABLED\?CIRRUS_HTTP_PORT:null\}\)\)/,
  );
  assert.match(source, /app\.use\(createModelGate\(/);
  assert.match(source, /demoMode:VISTA_DEMO_ENABLED/);
  assert.match(source, /resolveContainedFile\(s\.query\.path,SCREENSHOT_SEARCH_DIRS\)/);
  assert.doesNotMatch(source, /sendFile\(path\.resolve\(t\)\)/);
});

test("browser source has no remote font import and sanitizes signaling to loopback", () => {
  const css = fs.readFileSync(path.resolve(__dirname, "../../src/index.css"), "utf8");
  const player = fs.readFileSync(path.resolve(__dirname, "../../public/ue-player.html"), "utf8");
  assert.doesNotMatch(css, /@import\s+url\(["']?https?:\/\//i);
  assert.doesNotMatch(css, /fonts\.googleapis|fonts\.gstatic/i);
  assert.match(player, /parsed\.hostname\.toLowerCase\(\) === location\.hostname\.toLowerCase\(\)/);
  assert.match(player, /p\.delete\('ss'\)/);
  assert.match(player, /validPort\(cirrusPort\)/);
  assert.doesNotMatch(player, /swSendImmersiveKey|keyCode[^\n]*122|['"]F11['"]/);
});

test("fixed VISTA routes stay behind the shared guards with no dynamic command route", () => {
  const indexPath = path.resolve(__dirname, "../index.js");
  const source = fs.readFileSync(indexPath, "utf8");
  const loopbackGuard = source.indexOf("app.use(requestLoopbackGuard)");
  const accessGuard = source.indexOf("app.use(createAccessGuard(STUDIO_ACCESS_TOKEN))");
  const modelGate = source.indexOf("app.use(createModelGate(");
  const setupRoute = source.indexOf(
    'app.post("/api/vista/setup_vista_play_mode",vistaRuntimeBroker.setupVistaPlayMode)',
  );
  const stopRoute = source.indexOf(
    'app.post("/api/vista/stop_vista_play_mode",vistaRuntimeBroker.stopVistaPlayMode)',
  );
  const stateRoute = source.indexOf(
    'app.get("/api/vista/get_vista_state",vistaRuntimeBroker.getVistaState)',
  );
  for (const route of [setupRoute, stopRoute, stateRoute]) {
    assert.ok(loopbackGuard >= 0 && loopbackGuard < route);
    assert.ok(accessGuard >= 0 && accessGuard < route);
    assert.ok(modelGate >= 0 && modelGate < route);
  }
  assert.ok(setupRoute >= 0);
  assert.ok(stopRoute >= 0);
  assert.ok(stateRoute >= 0);
  assert.match(source, /createVistaRuntimeBroker\(\{ueBroker\}\)/);
  assert.match(source, /String\(req\.query\.cirrus\|\|""\)!==String\(CIRRUS_HTTP_PORT\)/);
  assert.match(source, /req\.query\.ss!==undefined/);
  assert.match(
    source,
    /const fixedVistaRoute=req\.path==="\/api\/vista\/setup_vista_play_mode"\|\|req\.path==="\/api\/vista\/stop_vista_play_mode",bodyForLog=fixedVistaRoute\?"<fixed-empty-contract>"/,
  );
  assert.doesNotMatch(source, /app\.(?:all|post|get)\(["']\/api\/vista\/(?:[:*]|command|execute)/);
});

test("staged model-off workspace pins the fixed broker and exposes no agent MCP", () => {
  const stagePath = path.resolve(__dirname, "../../../../tools/stage_vista_workspace.py");
  const source = fs.readFileSync(stagePath, "utf8");
  assert.match(source, /mcp_config = \{"mcpServers": \{\}\}/);
  assert.match(source, /"web\/mcp\.json": sha256_file\(workspace \/ "web" \/ "mcp\.json"\)/);
  assert.match(source, /"vista-runtime-broker\.js": sha256_file\(/);
  assert.match(source, /workspace \/ "web" \/ "server" \/ "vista-runtime-broker\.js"/);
  assert.doesNotMatch(source, /"simworld": \{\s*"command": "node"/);
});
