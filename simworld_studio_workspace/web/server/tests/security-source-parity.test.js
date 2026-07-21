"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

test("source server keeps the resolved transport and model gates wired", () => {
  const indexPath = path.resolve(__dirname, "../index.js");
  const source = fs.readFileSync(indexPath, "utf8");
  assert.doesNotMatch(source, /require\(["']cors["']\)/);
  assert.doesNotMatch(source, /app\.use\(cors\(/);
  assert.doesNotMatch(source, /app\.listen\([^\n]*["']0\.0\.0\.0["']/);
  assert.match(source, /studioHttpServer\.listen\(PORT,STUDIO_HOST,/);
  assert.match(source, /app\.use\(createTransportRequestGuard\(STUDIO_TRANSPORT\)\)/);
  assert.match(source, /app\.use\(createAccessGuard\(STUDIO_ACCESS_TOKEN,\{transport:STUDIO_TRANSPORT\}\)\)/);
  assert.match(
    source,
    /app\.use\(createTransportBrowserHeaders\(STUDIO_TRANSPORT,\{signalingPort:VISTA_DEMO_ENABLED\?CIRRUS_HTTP_PORT:null\}\)\)/,
  );
  assert.match(source, /studioStreaming\.attach\(studioHttpServer\)/);
  assert.match(source, /app\.use\(createModelGate\(/);
  assert.match(source, /demoMode:VISTA_DEMO_ENABLED/);
  assert.match(source, /resolveContainedFile\(s\.query\.path,SCREENSHOT_SEARCH_DIRS\)/);
  assert.doesNotMatch(source, /sendFile\(path\.resolve\(t\)\)/);
});

test("browser source has no remote font import and binds signalling to an opaque same-origin path", () => {
  const css = fs.readFileSync(path.resolve(__dirname, "../../src/index.css"), "utf8");
  const player = fs.readFileSync(path.resolve(__dirname, "../../public/ue-player.html"), "utf8");
  assert.doesNotMatch(css, /@import\s+url\(["']?https?:\/\//i);
  assert.doesNotMatch(css, /fonts\.googleapis|fonts\.gstatic/i);
  assert.match(player, /parsed\.host\.toLowerCase\(\) === location\.host\.toLowerCase\(\)/);
  assert.match(player, /parsed\.pathname === endpoint/);
  assert.match(player, /validEndpoint\(endpoint\)/);
  assert.match(player, /location\.host \+ endpoint/);
  assert.doesNotMatch(player, /validPort\(cirrusPort\)|location\.hostname \+ ':' \+ cirrusPort/);
  assert.doesNotMatch(player, /swSendImmersiveKey|keyCode[^\n]*122|['"]F11['"]/);
});

test("selected-candidate telemetry is guarded, lease-bound, and redacted before upload", () => {
  const index = fs.readFileSync(path.resolve(__dirname, "../index.js"), "utf8");
  const player = fs.readFileSync(path.resolve(__dirname, "../../public/ue-player.html"), "utf8");
  const accessGuard = index.indexOf("app.use(createAccessGuard(STUDIO_ACCESS_TOKEN,{transport:STUDIO_TRANSPORT}))");
  const telemetryGet = index.indexOf('app.get("/api/pixel-streaming-telemetry",studioStreaming.readTelemetry)');
  const telemetryPost = index.indexOf('app.post("/api/pixel-streaming-telemetry",studioStreaming.reportTelemetry)');
  assert.ok(accessGuard >= 0 && accessGuard < telemetryGet);
  assert.ok(accessGuard < telemetryPost);
  assert.match(player, /crypto\.subtle\.digest\('SHA-256'/);
  assert.match(player, /candidate_fingerprint: fingerprint/);
  assert.match(player, /address_redacted: true/);
  assert.match(player, /fetch\('\/api\/pixel-streaming-telemetry'/);
  assert.doesNotMatch(player, /candidate_sdp|local\.(?:address|ip|port)|remote\.(?:address|ip|port)/i);
  assert.doesNotMatch(player, /console\.(?:log|warn|error)\([^\n]*(?:candidate|credential|turnTransport)/i);
});

test("fixed VISTA routes stay behind the shared guards with no dynamic command route", () => {
  const indexPath = path.resolve(__dirname, "../index.js");
  const source = fs.readFileSync(indexPath, "utf8");
  const transportGuard = source.indexOf("app.use(createTransportRequestGuard(STUDIO_TRANSPORT))");
  const accessGuard = source.indexOf("app.use(createAccessGuard(STUDIO_ACCESS_TOKEN,{transport:STUDIO_TRANSPORT}))");
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
    assert.ok(transportGuard >= 0 && transportGuard < route);
    assert.ok(accessGuard >= 0 && accessGuard < route);
    assert.ok(modelGate >= 0 && modelGate < route);
  }
  assert.ok(setupRoute >= 0);
  assert.ok(stopRoute >= 0);
  assert.ok(stateRoute >= 0);
  assert.match(source, /createVistaRuntimeBroker\(\{ueBroker\}\)/);
  assert.match(source, /req\.query\.cirrus!==undefined/);
  assert.match(source, /req\.query\.ss!==undefined&&req\.query\.ss!==expectedSignallingUrl/);
  assert.match(source, /app\.get\("\/api\/pixel-streaming-url",studioStreaming\.issueEndpoint\)/);
  assert.match(
    source,
    /const fixedVistaRoute=req\.path==="\/api\/vista\/setup_vista_play_mode"\|\|req\.path==="\/api\/vista\/stop_vista_play_mode",bodyForLog=fixedVistaRoute\?"<fixed-empty-contract>"/,
  );
  assert.doesNotMatch(source, /app\.(?:all|post|get)\(["']\/api\/vista\/(?:[:*]|command|execute)/);
});

test("animation timeline is mounted through the lease-bound dedicated UE transport", () => {
  const indexPath = path.resolve(__dirname, "../index.js");
  const source = fs.readFileSync(indexPath, "utf8");
  const accessGuard = source.indexOf("app.use(createAccessGuard(STUDIO_ACCESS_TOKEN,{transport:STUDIO_TRANSPORT}))");
  const animationMount = source.indexOf(
    'app.use("/api/vista/imports",createVistaAnimationTimelineRouter({',
  );
  assert.ok(accessGuard >= 0 && accessGuard < animationMount);
  assert.ok(animationMount >= 0);
  assert.match(source, /createVistaAnimationDedicatedTransportResolver\(\{\s*resolveUeBroker:_resolveVistaSlotBroker/);
  assert.match(source, /transportResolver:_vistaAnimationTransportResolver/);
  assert.match(
    source,
    /isActiveSessionBinding:\(identity\)=>studioStreaming\.isActiveSessionBinding\(identity\)/,
  );
  assert.match(source, /_vistaAnimationUeProbe=vistaAnimationTimelineRuntime\.animationUeProbe/);
  assert.match(source, /animationUeProbe:\(options\)=>typeof _vistaAnimationUeProbe==="function"/);
  assert.doesNotMatch(source, /vista_animation_(?:content_api|capabilities|engine_time|evidence_capture)["']\s*\+/);
});

test("Review mode, UE evidence, and vanilla subprocesses stay lease-scoped in public profiles", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../index.js"), "utf8");
  const coordinatorRoute = source.indexOf('app.post("/api/chat",reviewLoopCoordinator.handleChat)');
  const authorityRoute = source.indexOf('app.post("/api/chat",reviewLoopCoordinator.bindVanillaChat)');
  const builderAuthorityRoute = source.indexOf('app.post("/api/chat",_builderRuntimeAuthority.bind)');
  const legacyRoute = source.indexOf('app.post("/api/chat",async(s,e)=>');
  assert.ok(coordinatorRoute >= 0 && coordinatorRoute < authorityRoute);
  assert.ok(authorityRoute < builderAuthorityRoute && builderAuthorityRoute < legacyRoute);
  assert.match(source, /createScopedReviewModeStore\(\{\s*transportProfile:STUDIO_TRANSPORT\.profile/);
  assert.match(source, /defaultMode:\(\{scope\}\)=>reviewModeStore\.get\(scope\)/);
  assert.match(
    source,
    /createLeaseBoundReviewBroker\(\{scope,resolveUeBroker:_resolveVistaSlotBroker\}\)/,
  );
  assert.match(source, /isActiveSessionBinding:\(identity\)=>studioStreaming\.isActiveSessionBinding\(identity\)/);
  assert.match(source, /if\(!requestedRunId\|\|cancelled\)\{/);
  assert.match(source, /const _procKey=_builderRuntime\?_builderRuntime\.scopeId:\(n\|\|"_global"\)/);
  assert.match(source, /if\(_chatProcs\.get\(_procKey\)===g\)_chatProcs\.delete\(_procKey\)/);
  assert.doesNotMatch(source, /let sceneLoopMode|let sceneLoopEnabled/);
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
