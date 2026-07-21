"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const {
  ACTION_DEFINITIONS,
  ANIMATION_CONTENT_PROFILE_SCHEMA,
  digest,
} = require("../vista-animation-contract");
const {
  ANIMATION_UE_MARKER_SCHEMA,
} = require("../vista-animation-ue-adapter");
const {
  ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
  ANIMATION_UE_CAPABILITY_OPERATION_ID,
  ANIMATION_UE_CAPABILITY_PROBE_SCHEMA,
  ANIMATION_UE_CAPABILITY_SCHEMA,
  ANIMATION_UE_PLUGIN_API_SCHEMA,
  ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
  ANIMATION_UE_PLUGIN_NAME,
  ANIMATION_UE_SOURCE_MANIFEST_SHA256,
  EXPECTED_PLUGIN_SOURCE_MANIFEST,
  EXPECTED_PLUGIN_SOURCE_FILES,
  MAX_PLUGIN_SOURCE_FILE_BYTES,
  OPERATION_SET,
  SECURITY_POLICY,
  VistaAnimationUeReadinessError,
  createVistaAnimationUeReadinessProbe,
  inspectVistaAnimationUePluginSource,
} = require("../vista-animation-ue-readiness");
const capabilityProbeSchema = require("../schemas/vista-animation-ue-capability-probe-v1.schema.json");
const capabilitySchema = require("../schemas/vista-animation-ue-capability-v1.schema.json");

const NONCE = "a".repeat(32);
const CONTENT_DIGEST = "b".repeat(64);
const BINARY_DIGEST = "c".repeat(64);
const REPOSITORY_ROOT = path.resolve(__dirname, "../../../..");
const PLUGIN_SOURCE_ROOT = path.join(REPOSITORY_ROOT, "unreal_plugins/VistaAnimationContentApi");

const PINNED_SOURCE_MANIFEST = [
  ["VistaAnimationContentApi.uplugin", "bc9fc7c0f227722221e709e65b91dc2cbdef8c21c91b4a9df775ad5766c965af"],
  ["Config/FilterPlugin.ini", "5bb06a2a79c30f12f891befbd34294914b4bdf1b634577d03c1c14c251b56b72"],
  ["ContentProfiles/vista-mmg040-project-profile-source-v1.json", "1b0aa6e48d251cb8dbeac4f34528ca8fa6084fb330fc2d150ef341f630528b1c"],
  ["Contract/vista-animation-content-api-v1.json", "b43d7ea45ad5cb8ff8bf645e52fb8a155462c71b47d8f625d45529a61400bd2e"],
  ["Contract/vista-animation-content-inspection-receipt-v1.schema.json", "919ba41b8effd621b88844be7a786cc2595627f14e8bb38f69d3e8279e11b463"],
  ["Contract/vista-animation-project-profile-source-v1.schema.json", "0c875748d29d76b8a7eaae1e6196a445631faee69a337b4b3567753dc0b5365c"],
  ["Source/VistaAnimationContentApi/VistaAnimationContentApi.Build.cs", "54d899c87f5121bacbb0ac18b29f320855ce564e54ee286e6c340f3aec7a91ed"],
  ["Source/VistaAnimationContentApi/Public/VistaAnimationContentApiModule.h", "3569a537793faec46bb3240ac53575b30e73d9f212ac05f7bbd90a57311b9535"],
  ["Source/VistaAnimationContentApi/Private/VistaAnimationContentApiModule.cpp", "fd27a21e49eea87bbfdf7bc21d6f9ed14f4cae074ad9e69fb749e4859d71e623"],
  ["Source/VistaAnimationContentApi/Public/VistaAnimationContentApiSubsystem.h", "ddadedfb967a397f04416295eb40c689d964414e22ba40f39746d83fff289e77"],
  ["Source/VistaAnimationContentApi/Private/VistaAnimationContentApiSubsystem.cpp", "6ecb19ad80ea769712c29bff32924abbf675e4e617d67563ca1681e8506232f5"],
  ["Source/VistaAnimationContentApi/Public/VistaAnimationContentDriver.h", "92ec98da354baf76e2aa39e6d81e016a65fed73abcd030ef7eb60f6842585b9f"],
  ["Source/VistaAnimationContentApi/Private/VistaAnimationStrictJson.h", "8259e25e01b156b985744d556e00830b199aec6272e96272658cc1311e524838"],
  ["Source/VistaAnimationContentApi/Private/VistaAnimationStrictJson.cpp", "104d838750191764b0451ba12f86889867ad732c8c5fef7e72740c130bcd0ca4"],
  ["Source/VistaAnimationContentApi/Public/VistaMmg040ContentDriver.h", "e88a9d498c75fb21740307a98f2f7f46c8f0f01c6a7a5168ee893be8b99832b3"],
  ["Source/VistaAnimationContentApi/Private/VistaMmg040ContentDriver.cpp", "c69dd60ca28119858124951d2ae77a3463092902bc845a94f107078867df4302"],
].map(([relativePath, sha256]) => ({
  path: `Plugins/VistaAnimationContentApi/${relativePath}`,
  sha256,
}));

function makeInstalledPluginFixture() {
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), "vista-animation-source-audit-"));
  const installedPluginRoot = path.join(projectRoot, "Plugins/VistaAnimationContentApi");
  fs.mkdirSync(path.dirname(installedPluginRoot), { recursive: true });
  fs.cpSync(PLUGIN_SOURCE_ROOT, installedPluginRoot, { recursive: true });
  return { projectRoot, installedPluginRoot };
}

function removeFixture(fixture) {
  fs.rmSync(fixture.projectRoot, { recursive: true, force: true });
}

function installedManifestPath(fixture, entry) {
  return path.resolve(fixture.projectRoot, ...entry.path.split("/"));
}

function openPathTargets(filepath, target) {
  try {
    return path.resolve(fs.realpathSync(filepath)) === path.resolve(target);
  } catch {
    return false;
  }
}

function mismatchFor(audit, relativePath) {
  return audit.mismatched_files.find((entry) => entry.path === relativePath);
}

function makeProfile() {
  return {
    schema: ANIMATION_CONTENT_PROFILE_SCHEMA,
    profile_id: "vista_hands_ik_v1",
    revision: "vista_hands_ik_2026_r1",
    content_revision: "simworld-content-2026.07.21-r1",
    content_digest: CONTENT_DIGEST,
    pawn_class_path: "/Game/VISTA/Characters/BP_VistaFirstPerson.BP_VistaFirstPerson_C",
    skeleton_path: "/Game/VISTA/Characters/SK_VistaHuman.SK_VistaHuman",
    verification: {
      status: "verified",
      receipt_id: "ue-content-receipt:2026-07-21:001",
      verified_at: "2026-07-21T00:00:00.000Z",
    },
    actions: [{
      action: "brace",
      adapter_id: ACTION_DEFINITIONS.brace.adapter_id,
      version: "1.0.0",
      bridge_action_id: ACTION_DEFINITIONS.brace.bridge_action_id,
      implementation_asset: "/Game/VISTA/Animations/ABP_brace.ABP_brace",
      completion_signal: "vista_brace_complete",
      timeout_ms: 5000,
    }],
  };
}

function makeArtifact(overrides = {}) {
  return {
    schema: ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
    plugin_name: ANIMATION_UE_PLUGIN_NAME,
    plugin_version: "1.0.0",
    plugin_build_id: "vista-animation-linux-ue5.3-build001",
    binary_sha256: BINARY_DIGEST,
    engine_version: "5.3.2",
    target_platform: "linux-x86_64",
    api_schema: ANIMATION_UE_PLUGIN_API_SCHEMA,
    ...overrides,
  };
}

function makeSlotBinding(overrides = {}) {
  return {
    owner_id: "owner:ives",
    session_id: "session:animation001",
    slot_id: "slot:ue-primary",
    scene_revision: "mmg_040@aaaaaaaaaaaaaaaa",
    ...overrides,
  };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function responseFor(request, mutate = () => {}) {
  const response = {
    schema: ANIMATION_UE_CAPABILITY_SCHEMA,
    status: "ready",
    operation_id: ANIMATION_UE_CAPABILITY_OPERATION_ID,
    operation_fingerprint: ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
    nonce_marker: clone(request.nonce_marker),
    challenge_digest: request.challenge_digest,
    slot_binding: clone(request.slot_binding),
    plugin_artifact: makeArtifact(),
    process_instance_id: "ue-process:boot001",
    content_proof: clone(request.content_proof),
    security: clone(SECURITY_POLICY),
    operation_allowlist_digest: OPERATION_SET.operation_allowlist_digest,
    operations: clone(OPERATION_SET.operations),
  };
  mutate(response);
  return JSON.stringify(response);
}

function makeTransport({ mutate, fail, hang = false } = {}) {
  const calls = [];
  return {
    calls,
    async probeAnimationContentApi(requestJson, options) {
      calls.push({ requestJson, options });
      if (hang) return new Promise(() => {});
      if (fail) throw fail;
      return responseFor(JSON.parse(requestJson), mutate);
    },
  };
}

function makeProbe(transport, overrides = {}) {
  return createVistaAnimationUeReadinessProbe({
    transport,
    expectedArtifact: makeArtifact(),
    contentProfile: makeProfile(),
    slotBinding: makeSlotBinding(),
    nonceFactory: () => NONCE,
    clock: () => Date.parse("2026-07-21T12:00:00.000Z"),
    ...overrides,
  });
}

test("capability JSON schema pins the exact live operation contract", () => {
  assert.equal(capabilityProbeSchema.properties.schema.const, ANIMATION_UE_CAPABILITY_PROBE_SCHEMA);
  assert.equal(capabilityProbeSchema.additionalProperties, false);
  assert.equal(
    capabilityProbeSchema.properties.operation_fingerprint.const,
    ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
  );
  assert.equal(
    capabilityProbeSchema.properties.operation_allowlist_digest.const,
    OPERATION_SET.operation_allowlist_digest,
  );
  assert.equal(capabilitySchema.properties.schema.const, ANIMATION_UE_CAPABILITY_SCHEMA);
  assert.equal(
    capabilitySchema.properties.operation_fingerprint.const,
    ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
  );
  assert.equal(
    capabilitySchema.properties.operation_allowlist_digest.const,
    OPERATION_SET.operation_allowlist_digest,
  );
  assert.equal(capabilitySchema.properties.operations.minItems, 7);
  assert.equal(capabilitySchema.properties.operations.maxItems, 7);
  assert.deepEqual(
    capabilitySchema.properties.operations.prefixItems.map((entry) => entry.const),
    OPERATION_SET.operations,
  );
  assert.equal(capabilitySchema.additionalProperties, false);
  assert.equal(capabilitySchema.$defs.pluginArtifact.additionalProperties, false);
  assert.equal(capabilitySchema.$defs.slotBinding.additionalProperties, false);
});

test("the checked-out Studio repository does not contain a compilable plugin source tree", () => {
  const audit = inspectVistaAnimationUePluginSource(REPOSITORY_ROOT);

  assert.equal(audit.schema, "vista-animation-ue-source-audit/v1");
  assert.equal(audit.plugin_name, ANIMATION_UE_PLUGIN_NAME);
  assert.equal(audit.source_manifest_sha256, ANIMATION_UE_SOURCE_MANIFEST_SHA256);
  assert.equal(audit.source_tree_complete, false);
  assert.deepEqual(audit.expected_files, EXPECTED_PLUGIN_SOURCE_FILES);
  assert.deepEqual(audit.present_files, []);
  assert.deepEqual(audit.missing_files, EXPECTED_PLUGIN_SOURCE_FILES);
  assert.deepEqual(audit.mismatched_files, []);
  assert.deepEqual(audit.unexpected_entries, []);
});

test("source manifest pins exactly the 16 production files and excludes tooling", () => {
  assert.deepEqual(EXPECTED_PLUGIN_SOURCE_MANIFEST, PINNED_SOURCE_MANIFEST);
  assert.deepEqual(EXPECTED_PLUGIN_SOURCE_FILES, PINNED_SOURCE_MANIFEST.map((entry) => entry.path));
  assert.equal(Object.isFrozen(EXPECTED_PLUGIN_SOURCE_MANIFEST), true);
  assert.ok(EXPECTED_PLUGIN_SOURCE_MANIFEST.every(Object.isFrozen));
  assert.equal(
    ANIMATION_UE_SOURCE_MANIFEST_SHA256,
    "bdd97f8f967aff67569de708f7c4f18475c54c68371e791e4af4b4c5b09e5b71",
  );
  assert.ok(EXPECTED_PLUGIN_SOURCE_FILES.every((entry) => !(
    entry.includes("/Scripts/")
    || entry.includes("/Tests/")
    || entry.endsWith("/README.md")
    || entry.endsWith("/.gitignore")
  )));
});

test("real copied plugin source passes exact hash, type, and tree policy", (t) => {
  const fixture = makeInstalledPluginFixture();
  t.after(() => removeFixture(fixture));
  const audit = inspectVistaAnimationUePluginSource(fixture.projectRoot);

  assert.equal(audit.source_tree_complete, true);
  assert.equal(audit.source_manifest_sha256, ANIMATION_UE_SOURCE_MANIFEST_SHA256);
  assert.deepEqual(audit.expected_manifest, PINNED_SOURCE_MANIFEST);
  assert.deepEqual(audit.present_files, EXPECTED_PLUGIN_SOURCE_FILES);
  assert.deepEqual(audit.missing_files, []);
  assert.deepEqual(audit.mismatched_files, []);
  assert.deepEqual(audit.unexpected_entries, []);
  assert.deepEqual(audit.policy_violations, []);
  assert.deepEqual(audit.allowed_nonproduction_entries, [
    "Plugins/VistaAnimationContentApi/.gitignore",
    "Plugins/VistaAnimationContentApi/README.md",
    "Plugins/VistaAnimationContentApi/Scripts",
    "Plugins/VistaAnimationContentApi/Tests",
  ]);
});

test("unrelated shared-ancestor metadata churn does not masquerade as a path replacement", (t) => {
  const fixture = makeInstalledPluginFixture();
  t.after(() => removeFixture(fixture));
  const sharedAncestor = path.resolve(os.tmpdir());
  const churnFs = Object.create(fs);
  let ancestorReads = 0;
  churnFs.lstatSync = (filepath) => {
    const status = fs.lstatSync(filepath);
    if (path.resolve(filepath) !== sharedAncestor) return status;
    ancestorReads += 1;
    return {
      dev: status.dev,
      ino: status.ino,
      mode: status.mode,
      nlink: status.nlink,
      size: status.size,
      mtimeMs: status.mtimeMs + ancestorReads,
      ctimeMs: status.ctimeMs + ancestorReads,
      isDirectory: () => status.isDirectory(),
      isFile: () => status.isFile(),
      isSymbolicLink: () => status.isSymbolicLink(),
    };
  };

  const audit = inspectVistaAnimationUePluginSource(fixture.projectRoot, { fsImpl: churnFs });
  assert.ok(ancestorReads > 1);
  assert.equal(audit.source_tree_complete, true);
});

test("every missing or byte-tampered production file fails closed with exact diagnostics", () => {
  for (const manifestEntry of PINNED_SOURCE_MANIFEST) {
    const missingFixture = makeInstalledPluginFixture();
    try {
      fs.rmSync(installedManifestPath(missingFixture, manifestEntry));
      const audit = inspectVistaAnimationUePluginSource(missingFixture.projectRoot);
      assert.equal(audit.source_tree_complete, false, manifestEntry.path);
      assert.deepEqual(audit.missing_files, [manifestEntry.path], manifestEntry.path);
      assert.equal(audit.mismatched_files.length, 0, manifestEntry.path);
    } finally {
      removeFixture(missingFixture);
    }

    const tamperedFixture = makeInstalledPluginFixture();
    try {
      fs.appendFileSync(installedManifestPath(tamperedFixture, manifestEntry), "\nsource-audit-tamper\n");
      const audit = inspectVistaAnimationUePluginSource(tamperedFixture.projectRoot);
      const mismatch = mismatchFor(audit, manifestEntry.path);
      assert.equal(audit.source_tree_complete, false, manifestEntry.path);
      assert.equal(audit.missing_files.length, 0, manifestEntry.path);
      assert.equal(mismatch.reason, "hash_mismatch", manifestEntry.path);
      assert.equal(mismatch.expected_sha256, manifestEntry.sha256, manifestEntry.path);
      assert.match(mismatch.actual_sha256, /^[a-f0-9]{64}$/, manifestEntry.path);
      assert.notEqual(mismatch.actual_sha256, manifestEntry.sha256, manifestEntry.path);
    } finally {
      removeFixture(tamperedFixture);
    }
  }
});

test("final and parent symlinks never satisfy the source manifest", (t) => {
  const finalFixture = makeInstalledPluginFixture();
  const parentFixture = makeInstalledPluginFixture();
  t.after(() => removeFixture(finalFixture));
  t.after(() => removeFixture(parentFixture));

  const finalEntry = PINNED_SOURCE_MANIFEST[0];
  const finalPath = installedManifestPath(finalFixture, finalEntry);
  fs.rmSync(finalPath);
  fs.symlinkSync(path.join(PLUGIN_SOURCE_ROOT, "VistaAnimationContentApi.uplugin"), finalPath);
  const finalAudit = inspectVistaAnimationUePluginSource(finalFixture.projectRoot);
  assert.equal(finalAudit.source_tree_complete, false);
  assert.equal(mismatchFor(finalAudit, finalEntry.path).reason, "symlink");

  const publicDirectory = path.join(
    parentFixture.installedPluginRoot,
    "Source/VistaAnimationContentApi/Public",
  );
  const displacedPublic = path.join(parentFixture.projectRoot, "displaced-public");
  fs.renameSync(publicDirectory, displacedPublic);
  fs.symlinkSync(displacedPublic, publicDirectory, "dir");
  const parentAudit = inspectVistaAnimationUePluginSource(parentFixture.projectRoot);
  assert.equal(parentAudit.source_tree_complete, false);
  const publicEntries = PINNED_SOURCE_MANIFEST.filter((entry) => entry.path.includes("/Public/"));
  for (const entry of publicEntries) {
    assert.equal(mismatchFor(parentAudit, entry.path).reason, "ancestor_symlink", entry.path);
  }
  assert.ok(parentAudit.policy_violations.some((entry) => (
    entry.path.endsWith("/Public") && entry.reason === "ancestor_symlink"
  )));
});

test("a symlinked project-root ancestor fails before any source is trusted", (t) => {
  const fixture = makeInstalledPluginFixture();
  const alias = `${fixture.projectRoot}-alias`;
  t.after(() => {
    fs.rmSync(alias, { force: true });
    removeFixture(fixture);
  });
  fs.symlinkSync(fixture.projectRoot, alias, "dir");

  const audit = inspectVistaAnimationUePluginSource(alias);
  assert.equal(audit.source_tree_complete, false);
  assert.deepEqual(audit.present_files, []);
  assert.deepEqual(audit.missing_files, EXPECTED_PLUGIN_SOURCE_FILES);
  assert.deepEqual(audit.policy_violations, [{ path: ".", reason: "ancestor_symlink" }]);
});

test("hardlinks, FIFOs, directories, unreadable files, and oversized files fail closed", (t) => {
  const targetEntry = PINNED_SOURCE_MANIFEST[0];
  const fixtures = [];
  t.after(() => fixtures.forEach(removeFixture));
  const nextFixture = () => {
    const fixture = makeInstalledPluginFixture();
    fixtures.push(fixture);
    return fixture;
  };

  const hardlinkFixture = nextFixture();
  const hardlinkTarget = installedManifestPath(hardlinkFixture, targetEntry);
  const hardlinkSource = path.join(hardlinkFixture.projectRoot, "hardlink-source");
  fs.copyFileSync(hardlinkTarget, hardlinkSource);
  fs.rmSync(hardlinkTarget);
  fs.linkSync(hardlinkSource, hardlinkTarget);
  assert.equal(
    mismatchFor(inspectVistaAnimationUePluginSource(hardlinkFixture.projectRoot), targetEntry.path).reason,
    "hardlink",
  );

  const fifoFixture = nextFixture();
  const fifoTarget = installedManifestPath(fifoFixture, targetEntry);
  fs.rmSync(fifoTarget);
  const fifo = spawnSync("mkfifo", [fifoTarget], { encoding: "utf8" });
  assert.equal(fifo.status, 0, fifo.stderr);
  assert.equal(
    mismatchFor(inspectVistaAnimationUePluginSource(fifoFixture.projectRoot), targetEntry.path).reason,
    "not_regular_file",
  );

  const directoryFixture = nextFixture();
  const directoryTarget = installedManifestPath(directoryFixture, targetEntry);
  fs.rmSync(directoryTarget);
  fs.mkdirSync(directoryTarget);
  assert.equal(
    mismatchFor(inspectVistaAnimationUePluginSource(directoryFixture.projectRoot), targetEntry.path).reason,
    "not_regular_file",
  );

  const unreadableFixture = nextFixture();
  const unreadableTarget = installedManifestPath(unreadableFixture, targetEntry);
  const unreadableFs = Object.create(fs);
  unreadableFs.openSync = (filepath, ...args) => {
    if (openPathTargets(filepath, unreadableTarget)) {
      const error = new Error("injected unreadable source");
      error.code = "EACCES";
      throw error;
    }
    return fs.openSync(filepath, ...args);
  };
  assert.equal(
    mismatchFor(inspectVistaAnimationUePluginSource(unreadableFixture.projectRoot, {
      fsImpl: unreadableFs,
    }), targetEntry.path).reason,
    "unreadable",
  );

  const oversizedFixture = nextFixture();
  const oversizedTarget = installedManifestPath(oversizedFixture, targetEntry);
  fs.truncateSync(oversizedTarget, MAX_PLUGIN_SOURCE_FILE_BYTES + 1);
  assert.equal(
    mismatchFor(inspectVistaAnimationUePluginSource(oversizedFixture.projectRoot), targetEntry.path).reason,
    "oversize",
  );
});

test("open/read/fstat/lstat identity checks reject a same-byte path replacement", (t) => {
  const fixture = makeInstalledPluginFixture();
  t.after(() => removeFixture(fixture));
  const targetEntry = PINNED_SOURCE_MANIFEST[0];
  const target = installedManifestPath(fixture, targetEntry);
  const displaced = path.join(fixture.projectRoot, "displaced-source-file");
  const raceFs = Object.create(fs);
  let targetDescriptor = null;
  let replaced = false;
  raceFs.openSync = (filepath, ...args) => {
    const descriptor = fs.openSync(filepath, ...args);
    if (openPathTargets(filepath, target)) targetDescriptor = descriptor;
    return descriptor;
  };
  raceFs.readSync = (descriptor, ...args) => {
    const bytesRead = fs.readSync(descriptor, ...args);
    if (descriptor === targetDescriptor && bytesRead > 0 && !replaced) {
      replaced = true;
      fs.renameSync(target, displaced);
      fs.copyFileSync(displaced, target);
    }
    return bytesRead;
  };

  const audit = inspectVistaAnimationUePluginSource(fixture.projectRoot, { fsImpl: raceFs });
  assert.equal(replaced, true);
  assert.equal(audit.source_tree_complete, false);
  assert.equal(mismatchFor(audit, targetEntry.path).reason, "identity_changed");
});

test("post-hash snapshots reject an expected directory replaced during hashing", (t) => {
  const fixture = makeInstalledPluginFixture();
  t.after(() => removeFixture(fixture));
  const triggerEntry = PINNED_SOURCE_MANIFEST[0];
  const triggerTarget = installedManifestPath(fixture, triggerEntry);
  const privateDirectory = path.join(
    fixture.installedPluginRoot,
    "Source/VistaAnimationContentApi/Private",
  );
  const displacedDirectory = path.join(fixture.projectRoot, "displaced-private");
  const privateRelativePath = "Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Private";
  const raceFs = Object.create(fs);
  let triggerDescriptor = null;
  let anchoredOpenSeen = false;
  let replaced = false;
  raceFs.openSync = (filepath, ...args) => {
    const descriptor = fs.openSync(filepath, ...args);
    if (openPathTargets(filepath, triggerTarget)) {
      triggerDescriptor = descriptor;
      anchoredOpenSeen = String(filepath).startsWith("/proc/self/fd/");
    }
    return descriptor;
  };
  raceFs.readSync = (descriptor, ...args) => {
    const bytesRead = fs.readSync(descriptor, ...args);
    if (descriptor === triggerDescriptor && bytesRead > 0 && !replaced) {
      replaced = true;
      fs.renameSync(privateDirectory, displacedDirectory);
      fs.cpSync(displacedDirectory, privateDirectory, { recursive: true });
    }
    return bytesRead;
  };

  const audit = inspectVistaAnimationUePluginSource(fixture.projectRoot, { fsImpl: raceFs });
  assert.equal(anchoredOpenSeen, true);
  assert.equal(replaced, true);
  assert.equal(audit.source_tree_complete, false);
  assert.deepEqual(audit.present_files, EXPECTED_PLUGIN_SOURCE_FILES);
  assert.ok(audit.policy_violations.some((entry) => (
    entry.path === privateRelativePath && entry.reason === "identity_changed"
  )));
  assert.equal(JSON.stringify(audit).includes(fixture.projectRoot), false);
  assert.equal(JSON.stringify(audit).includes("displaced-private"), false);
});

test("post-hash snapshots reject an unexpected file created during hashing", (t) => {
  const fixture = makeInstalledPluginFixture();
  t.after(() => removeFixture(fixture));
  const triggerEntry = PINNED_SOURCE_MANIFEST[0];
  const triggerTarget = installedManifestPath(fixture, triggerEntry);
  const unexpectedRelativePath = (
    "Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Private/Injected.cpp"
  );
  const unexpectedPath = path.resolve(
    fixture.projectRoot,
    ...unexpectedRelativePath.split("/"),
  );
  const raceFs = Object.create(fs);
  let triggerDescriptor = null;
  let created = false;
  raceFs.openSync = (filepath, ...args) => {
    const descriptor = fs.openSync(filepath, ...args);
    if (openPathTargets(filepath, triggerTarget)) triggerDescriptor = descriptor;
    return descriptor;
  };
  raceFs.readSync = (descriptor, ...args) => {
    const bytesRead = fs.readSync(descriptor, ...args);
    if (descriptor === triggerDescriptor && bytesRead > 0 && !created) {
      created = true;
      fs.writeFileSync(unexpectedPath, "unexpected during source audit\n");
    }
    return bytesRead;
  };

  const audit = inspectVistaAnimationUePluginSource(fixture.projectRoot, { fsImpl: raceFs });
  assert.equal(created, true);
  assert.equal(audit.source_tree_complete, false);
  assert.deepEqual(audit.present_files, EXPECTED_PLUGIN_SOURCE_FILES);
  assert.deepEqual(audit.missing_files, []);
  assert.deepEqual(audit.mismatched_files, []);
  assert.deepEqual(audit.unexpected_entries, [unexpectedRelativePath]);
});

test("missing Linux directory-descriptor traversal fails closed", (t) => {
  const fixture = makeInstalledPluginFixture();
  t.after(() => removeFixture(fixture));
  const unsupportedFs = Object.create(fs);
  unsupportedFs.openSync = (filepath, ...args) => {
    if (String(filepath).startsWith("/proc/self/fd/") && String(filepath).endsWith("/.")) {
      const error = new Error("internal mount detail token=secret");
      error.code = "ENOENT";
      throw error;
    }
    return fs.openSync(filepath, ...args);
  };

  const audit = inspectVistaAnimationUePluginSource(fixture.projectRoot, { fsImpl: unsupportedFs });
  assert.equal(audit.source_tree_complete, false);
  assert.deepEqual(audit.present_files, []);
  assert.deepEqual(audit.missing_files, EXPECTED_PLUGIN_SOURCE_FILES);
  assert.deepEqual(audit.mismatched_files, []);
  assert.ok(audit.policy_violations.some((entry) => (
    entry.path === "." && entry.reason === "platform_unsupported"
  )));
  assert.equal(JSON.stringify(audit).includes("secret"), false);
  assert.equal(JSON.stringify(audit).includes(fixture.projectRoot), false);
});

test("exact recursive allowlist rejects unexpected source, config, contract, and profile entries", (t) => {
  const fixture = makeInstalledPluginFixture();
  t.after(() => removeFixture(fixture));
  const unexpected = [
    "Plugins/VistaAnimationContentApi/Config/Extra.ini",
    "Plugins/VistaAnimationContentApi/ContentProfiles/extra-profile.json",
    "Plugins/VistaAnimationContentApi/Contract/extra-contract.json",
    "Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Private/Backdoor.cpp",
  ];
  for (const relativePath of unexpected) {
    fs.writeFileSync(path.resolve(fixture.projectRoot, ...relativePath.split("/")), "unexpected\n");
  }

  const audit = inspectVistaAnimationUePluginSource(fixture.projectRoot);
  assert.equal(audit.source_tree_complete, false);
  assert.deepEqual(audit.present_files, EXPECTED_PLUGIN_SOURCE_FILES);
  assert.deepEqual(audit.missing_files, []);
  assert.deepEqual(audit.mismatched_files, []);
  assert.deepEqual(audit.unexpected_entries, [...unexpected].sort());
});

test("a live plugin challenge proves artifact, slot, content, allowlist, and no-retry policy", async () => {
  const transport = makeTransport();
  const result = await makeProbe(transport)();

  assert.equal(result.status, "ready");
  assert.deepEqual(result.causes, []);
  assert.equal(result.revision.verification, "live_plugin_challenge");
  assert.equal(result.revision.process_instance_id, "ue-process:boot001");
  assert.equal(result.revision.binary_sha256, BINARY_DIGEST);
  assert.equal(transport.calls.length, 1);

  const { requestJson, options } = transport.calls[0];
  const request = JSON.parse(requestJson);
  assert.equal(request.schema, ANIMATION_UE_CAPABILITY_PROBE_SCHEMA);
  assert.equal(request.operation_id, ANIMATION_UE_CAPABILITY_OPERATION_ID);
  assert.equal(request.nonce_marker.schema, ANIMATION_UE_MARKER_SCHEMA);
  assert.equal(request.nonce_marker.nonce, NONCE);
  const unsigned = clone(request);
  delete unsigned.challenge_digest;
  assert.equal(request.challenge_digest, digest(unsigned));
  const slotIdentity = clone(request.slot_binding);
  delete slotIdentity.binding_digest;
  assert.equal(request.slot_binding.binding_digest, digest(slotIdentity));
  assert.equal(request.operation_allowlist_digest, OPERATION_SET.operation_allowlist_digest);
  assert.equal(options.mutation, false);
  assert.equal(options.maxAttempts, 1);
  assert.equal(options.challengeDigest, request.challenge_digest);
  assert.equal(options.slotBindingDigest, request.slot_binding.binding_digest);
  assert.equal(options.timeoutMs, options.queueDeadlineMs);
  assert.equal(options.signal.aborted, false);

  assert.equal(requestJson.includes("/Game/"), false);
  assert.equal(/python|execute[_-]?console|\bvbp\b|\.py\b/i.test(requestJson), false);
  assert.equal(Object.hasOwn(request, "operations"), false);
  assert.equal(Object.hasOwn(request, "expected_plugin_artifact"), false);
  assert.equal(Object.hasOwn(request, "script"), false);
  assert.equal(Object.hasOwn(request, "asset_path"), false);
  assert.equal(OPERATION_SET.operations.filter((entry) => entry.mutation).length, 4);
  assert.ok(OPERATION_SET.operations.filter((entry) => entry.mutation).every((entry) => entry.max_attempts === 1));
});

test("generic MCP, Python, and invoke-only transports cannot satisfy readiness", async () => {
  const genericTransports = [
    null,
    { send: async () => "{}" },
    { execute_python_script: async () => "{}" },
    { invokeAnimationContentApi: async () => "{}" },
  ];

  for (const transport of genericTransports) {
    const result = await makeProbe(transport)();
    assert.equal(result.status, "not_ready");
    assert.equal(result.causes[0].code, "ANIMATION_UE_PLUGIN_TRANSPORT_MISSING");
  }
});

test("legacy timeline environment flags are not accepted as capability evidence", () => {
  assert.throws(
    () => createVistaAnimationUeReadinessProbe({
      transport: null,
      expectedArtifact: makeArtifact(),
      contentProfile: makeProfile(),
      slotBinding: makeSlotBinding(),
      env: { TIMELINE_AUTOMATION_VERIFIED: "true" },
    }),
    (error) => error instanceof VistaAnimationUeReadinessError
      && error.code === "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
});

const mismatchCases = [
  {
    name: "unknown response field",
    code: "ANIMATION_UE_PLUGIN_PROTOCOL_INVALID",
    mutate: (response) => { response.python = "print('unsafe')"; },
  },
  {
    name: "stale nonce",
    code: "ANIMATION_UE_PLUGIN_CORRELATION_MISMATCH",
    mutate: (response) => { response.nonce_marker.nonce = "d".repeat(32); },
  },
  {
    name: "stale challenge digest",
    code: "ANIMATION_UE_PLUGIN_CORRELATION_MISMATCH",
    mutate: (response) => { response.challenge_digest = "d".repeat(64); },
  },
  {
    name: "different plugin binary",
    code: "ANIMATION_UE_PLUGIN_ARTIFACT_MISMATCH",
    mutate: (response) => { response.plugin_artifact.binary_sha256 = "d".repeat(64); },
  },
  {
    name: "different slot",
    code: "ANIMATION_UE_PLUGIN_SLOT_MISMATCH",
    mutate: (response) => { response.slot_binding.slot_id = "slot:other"; },
  },
  {
    name: "different content",
    code: "ANIMATION_UE_PLUGIN_CONTENT_MISMATCH",
    mutate: (response) => { response.content_proof.content_digest = "d".repeat(64); },
  },
  {
    name: "caller Python enabled",
    code: "ANIMATION_UE_PLUGIN_SECURITY_POLICY_MISMATCH",
    mutate: (response) => { response.security.caller_python = true; },
  },
  {
    name: "mutation retries enabled",
    code: "ANIMATION_UE_PLUGIN_SECURITY_POLICY_MISMATCH",
    mutate: (response) => { response.security.mutation_max_attempts = 2; },
  },
  {
    name: "operation omitted",
    code: "ANIMATION_UE_PLUGIN_OPERATION_MISMATCH",
    mutate: (response) => { response.operations.pop(); },
  },
  {
    name: "operation fingerprint changed",
    code: "ANIMATION_UE_PLUGIN_OPERATION_MISMATCH",
    mutate: (response) => { response.operations[0].operation_fingerprint = "d".repeat(64); },
  },
];

for (const entry of mismatchCases) {
  test(`capability probe fails closed for ${entry.name}`, async () => {
    const result = await makeProbe(makeTransport({ mutate: entry.mutate }))();
    assert.equal(result.status, "not_ready");
    assert.equal(result.causes[0].code, entry.code);
    assert.equal(result.causes[0].dependency, "vista_animation_ue_plugin");
    assert.equal(JSON.stringify(result).includes("print('unsafe')"), false);
  });
}

test("invalid and oversized capability responses fail closed", async () => {
  const invalid = makeProbe({ probeAnimationContentApi: async () => "not-json" });
  const invalidResult = await invalid();
  assert.equal(invalidResult.status, "not_ready");
  assert.equal(invalidResult.causes[0].code, "ANIMATION_UE_PLUGIN_PROTOCOL_INVALID");

  const oversized = makeProbe({
    probeAnimationContentApi: async () => `{"padding":"${"x".repeat(131_073)}"}`,
  }, { nonceFactory: () => "e".repeat(32) });
  const oversizedResult = await oversized();
  assert.equal(oversizedResult.status, "not_ready");
  assert.equal(oversizedResult.causes[0].code, "ANIMATION_UE_PLUGIN_PROTOCOL_INVALID");
});

test("transport failures are sanitized and do not expose credentials", async () => {
  const result = await makeProbe(makeTransport({
    fail: new Error("postgres://user:password@internal/db token=secret"),
  }))();
  assert.equal(result.status, "not_ready");
  assert.equal(result.causes[0].code, "ANIMATION_UE_PLUGIN_PROBE_UNAVAILABLE");
  assert.equal(JSON.stringify(result).includes("password"), false);
  assert.equal(JSON.stringify(result).includes("secret"), false);
});

test("timeout remains bounded even when a transport ignores abort", async () => {
  const started = Date.now();
  const result = await makeProbe(makeTransport({ hang: true }), {
    timeoutMs: 10,
  })();
  assert.equal(result.status, "not_ready");
  assert.equal(result.causes[0].code, "ANIMATION_UE_PLUGIN_PROBE_TIMEOUT");
  assert.ok(Date.now() - started < 500);
});

test("caller cancellation remains bounded even when a transport ignores abort", async () => {
  const controller = new AbortController();
  const probe = makeProbe(makeTransport({ hang: true }), { timeoutMs: 1000 });
  const pending = probe({ signal: controller.signal });
  setImmediate(() => controller.abort());
  const result = await pending;
  assert.equal(result.status, "not_ready");
  assert.equal(result.causes[0].code, "ANIMATION_UE_PLUGIN_PROBE_ABORTED");
});

test("nonce reuse is rejected instead of treating a replay as a fresh capability", async () => {
  const transport = makeTransport();
  const probe = makeProbe(transport);
  assert.equal((await probe()).status, "ready");
  const replay = await probe();
  assert.equal(replay.status, "not_ready");
  assert.equal(replay.causes[0].code, "ANIMATION_UE_PLUGIN_NONCE_INVALID");
  assert.equal(transport.calls.length, 1);
});

test("artifact, slot, profile, and timeout configuration are exact and bounded", () => {
  assert.throws(
    () => makeProbe(null, { expectedArtifact: makeArtifact({ arbitrary_path: "/tmp/plugin" }) }),
    (error) => error.code === "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
  assert.throws(
    () => makeProbe(null, { slotBinding: makeSlotBinding({ lease_token: "secret" }) }),
    (error) => error.code === "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
  assert.throws(
    () => makeProbe(null, { timeoutMs: 10_001 }),
    (error) => error.code === "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
  assert.throws(
    () => makeProbe(null, { contentProfile: { schema: ANIMATION_CONTENT_PROFILE_SCHEMA } }),
    (error) => error.code === "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
});
