"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const SERVER_ROOT = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(SERVER_ROOT, "../../..");

function read(relative) {
  return fs.readFileSync(path.join(REPO_ROOT, relative), "utf8");
}

test("server wires one shared journal recorder through readiness and all terminal domains", () => {
  const source = fs.readFileSync(path.join(SERVER_ROOT, "index.js"), "utf8");
  assert.match(source, /createArtifactJournalRuntime\(\{env:process\.env\}\)/);
  assert.match(source, /artifactJournalProbe:artifactJournalRuntime\.readinessProbe/);
  assert.equal((source.match(/artifactRecorder:artifactJournalRuntime\.recorder/g) || []).length, 4);
  assert.match(source, /captureReviewBinding:async\(\{scope,signal\}\)=>/);
  assert.match(source, /broker\.send\("get_actors_in_level"/);
  assert.match(
    source,
    /resolveActiveRuntimeLineage\(scope\.activeLease\)/,
  );
  assert.equal(source.includes('app.use("/api/artifact-journal"'), false);
  assert.ok(source.indexOf("createArtifactJournalRuntime") < source.indexOf("createStudioReadiness({"));
});

test("one mutation arbiter gates Review, scene build, timeline, and vanilla chat in order", () => {
  const source = fs.readFileSync(path.join(SERVER_ROOT, "index.js"), "utf8");
  assert.match(source, /const runtimeMutationArbiter = createRuntimeMutationArbiter\(\)/);
  assert.match(
    source,
    /createRuntimeMutationMiddleware\(\{ arbiter: runtimeMutationArbiter \}\)/,
  );
  assert.equal(
    (source.match(/mutationArbiter:runtimeMutationArbiter/g) || []).length,
    3,
    "Review, scene build, and animation must share the same arbiter instance",
  );
  assert.match(
    source,
    /app\.post\("\/api\/chat",reviewLoopCoordinator\.handleChat\);\s*app\.post\("\/api\/chat",reviewLoopCoordinator\.bindVanillaChat\);\s*app\.post\("\/api\/chat",runtimeMutationMiddleware\);\s*app\.post\("\/api\/chat",_builderRuntimeAuthority\.bind\);/,
  );
});

test("Review evidence is preflighted, served behind global guards, and never exposes the old raw root", () => {
  const source = fs.readFileSync(path.join(SERVER_ROOT, "index.js"), "utf8");
  const coordinator = read("simworld_studio_workspace/web/server/review-loop-coordinator.js");
  const visualLoop = read("simworld_studio_workspace/web/server/scene-loop-visual.js");
  const evidenceRoute = source.indexOf('app.get("/api/review-evidence/:handle"');
  for (const guard of [
    "app.use(createTransportRequestGuard",
    "app.use(createAccessGuard",
    "app.use(createTransportBrowserHeaders",
    "app.use(createModelGate",
    "app.use(createProductionExecutionGuard",
  ]) {
    const guardIndex = source.indexOf(guard);
    assert.ok(guardIndex >= 0 && evidenceRoute > guardIndex, `${guard} must precede evidence route`);
  }
  assert.match(source, /reviewPreflight:reviewEvidenceProbe/);
  const requestedIdentity = coordinator.indexOf("requestedReviewRunId = safeRequestedRunId");
  const preflight = coordinator.indexOf("if (reviewPreflight) {", requestedIdentity);
  const dependencies = coordinator.indexOf("resolvedHandlerDependencies = handlerDependencies", requestedIdentity);
  assert.ok(requestedIdentity >= 0 && preflight > requestedIdentity && dependencies > preflight);
  assert.equal(source.includes('path.join(ARENA_ROOT, "tmp", "visual_loop")'), false);
  assert.equal(visualLoop.includes("/api/screenshot/file?path="), false);
  assert.match(visualLoop, /visualFeedbackImages:\s*\[\]/);
  assert.doesNotMatch(visualLoop, /visualFeedbackImages:\s*lastScreenshots/);
});

test("Review evidence startup, lifecycle ownership, and legacy migration are wired fail closed", () => {
  const source = fs.readFileSync(path.join(SERVER_ROOT, "index.js"), "utf8");
  const critic = read("simworld_studio_workspace/web/server/scene-critic.js");
  const coordinator = read("simworld_studio_workspace/web/server/review-loop-coordinator.js");
  const mcp = read("simworld_studio_workspace/web/server/mcp-server.js");
  const bootstrap = source.indexOf("bootstrapDefaultReviewEvidenceRoots().then");
  const listen = source.indexOf("studioHttpServer.listen(");

  assert.ok(bootstrap >= 0 && listen > bootstrap, "server must not listen before bounded evidence bootstrap");
  assert.match(source, /reviewEvidenceLifecycle=Object\.freeze\(\{/);
  assert.match(source, /reviewEvidenceLifecycle,/);
  assert.match(critic, /deadlineMs = DEFAULT_EVIDENCE_OPERATION_DEADLINE_MS/);
  assert.match(critic, /function finalizeReviewEvidenceAdmission[\s\S]*remainingDeadlineMs/);
  assert.match(coordinator, /reviewEvidenceLifecycle\.admit[\s\S]*responseGate = createTerminalResponseGate/);
  assert.match(coordinator, /reviewEvidenceLifecycle\.finalize[\s\S]*registry\.complete/);
  assert.match(source, /app\.post\('\/api\/vlm-score',[\s\S]{0,240}REVIEW_COORDINATOR_REQUIRED/);
  assert.doesNotMatch(source.slice(source.indexOf("app.post('/api/vlm-score'"), source.indexOf("// ── Agent stop-all")), /runCritic/);
  assert.doesNotMatch(mcp, /name:"verify_scene"/);
  assert.doesNotMatch(mcp, /verify_scene:toolVerifyScene/);
  assert.doesNotMatch(mcp, /const\{runCritic\}=require\("\.\/scene-critic"\)/);
});

test("paid Review abandonment remains an offline operator action", () => {
  const source = fs.readFileSync(path.join(SERVER_ROOT, "index.js"), "utf8");
  assert.doesNotMatch(
    source,
    /app\.(?:use|get|post|put|patch|delete)\([^\n]*(?:abandon|abandonment)/i,
  );
});

test("browser Review retries persist and resend one stable client run id", () => {
  const api = read("simworld_studio_workspace/web/src/api/appApi.js");
  const panel = read("simworld_studio_workspace/web/src/features/chat/ChatPanel.jsx");
  const verifier = read("simworld_studio_workspace/web/src/features/scene/CodingVerifierPanel.jsx");
  const staticTools = read("simworld_studio_workspace/web/src/features/library/staticMcpTools.js");
  const runtime = read("simworld_studio_workspace/web/src/features/chat/chatRuntime.js");
  assert.match(api, /runId: options\?\.runId/);
  assert.match(panel, /review-client-/);
  assert.match(panel, /reviewRequest, reviewRetryAvailable: false/);
  assert.match(panel, /handleSend\(reviewRequest\.prompt, undefined, reviewRequest\)/);
  assert.match(runtime, /simworld\.review\.pending\.v2/);
  assert.match(runtime, /simworld\.review\.pending\.v1/);
  assert.match(runtime, /storage\.removeItem\(LEGACY_REVIEW_PENDING_STORAGE_KEY\)/);
  assert.doesNotMatch(api, /vlm-score|scoreSceneWithVlm|scoreLatestScreenshot/);
  assert.doesNotMatch(verifier, /Run Review|scene-verifier-score|scoreLatestScreenshot/);
  assert.doesNotMatch(staticTools, /verify_scene/);
  const toolsPage = read("simworld_studio_workspace/web/src/features/library/ToolsPage.jsx");
  assert.match(toolsPage, /Curated MCP Tool Reference/);
  assert.match(toolsPage, /runtime catalog is server-authoritative/);
  assert.match(verifier, /Choose Text or Visual in the Chat Review control/);
});

test("production launch surfaces require one persistent private journal root", () => {
  const compose = read("deploy/aws/docker/docker-compose.yml");
  const systemd = read("deploy/aws/systemd/simworld-web.service");
  const bakeAmi = read("deploy/aws/scripts/bake-ami.sh");
  for (const source of [compose, systemd]) {
    assert.match(source, /VISTA_ARTIFACT_JOURNAL_ENABLED=1/);
    assert.match(source, /VISTA_ARTIFACT_JOURNAL_ROOT=\/var\/lib\/simworld\/artifact-journal/);
    assert.match(source, /VISTA_ARTIFACT_JOURNAL_RETENTION_DAYS/);
  }
  assert.match(compose, /\/var\/lib\/simworld:\/var\/lib\/simworld/);
  assert.match(systemd, /ReadWritePaths=\/var\/lib\/simworld\b/);
  assert.match(bakeAmi, /install -d -o simworld -g simworld -m 700[\s\\]+\/var\/lib\/simworld\/artifact-journal/);
});
