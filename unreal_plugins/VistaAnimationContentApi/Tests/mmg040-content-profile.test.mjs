import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  chmodSync,
  linkSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import test from "node:test";

import {
  PINNED_SOURCE_CONTRACT_SHA256_BY_REVISION,
  PINNED_SOURCE_CONTRACT_SHA256,
  ContentProfileContractError,
  buildBlockedPreflight,
  buildServerContentProfile,
  computeContentDigest,
  parseStrictJson,
  readSecureJson,
  sha256Bytes,
  validateInspectionReceipt,
  validateSourceContract,
} from "../Scripts/prepare-content-profile.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const pluginRoot = path.resolve(here, "..");
const repositoryRoot = path.resolve(pluginRoot, "../..");
const contractPath = path.join(
  pluginRoot,
  "ContentProfiles/vista-mmg040-project-profile-source-v2.json",
);
const r1ContractPath = path.join(
  pluginRoot,
  "ContentProfiles/vista-mmg040-project-profile-source-v1.json",
);
const scriptPath = path.join(pluginRoot, "Scripts/prepare-content-profile.mjs");
const sourceBytes = readFileSync(contractPath);
const sourceSha = sha256Bytes(sourceBytes);
const sourceContract = validateSourceContract(parseStrictJson(sourceBytes), sourceSha);
const r1SourceBytes = readFileSync(r1ContractPath);
const r1SourceSha = sha256Bytes(r1SourceBytes);
const r1SourceContract = validateSourceContract(
  parseStrictJson(r1SourceBytes),
  r1SourceSha,
);
const require = createRequire(import.meta.url);
const animationContract = require(path.join(
  repositoryRoot,
  "simworld_studio_workspace/web/server/vista-animation-contract.js",
));

function digest(label) {
  return createHash("sha256").update(label).digest("hex");
}

function makeReceipt(
  profileContract = sourceContract,
  profileContractSha = sourceSha,
) {
  const isR2 = profileContract.profile_revision === "mmg040_project_content_r2";
  const receipt = {
    schema: "vista-animation-content-inspection-receipt/v1",
    source_contract_sha256: profileContractSha,
    profile_id: profileContract.profile_id,
    profile_revision: profileContract.profile_revision,
    content_revision: isR2
      ? "gym-citynav-mmg040-content-r2"
      : "gym-citynav-mmg040-content-r1",
    content_digest: "0".repeat(64),
    project: {
      project_name: profileContract.target.project_name,
      engine_version: profileContract.target.engine_version,
      project_revision: "gym-citynav-project-r1",
      project_descriptor_sha256: digest("gym-citynav.uproject"),
    },
    verification: {
      status: "verified",
      receipt_id: isR2
        ? "mmg040-live-inspection-r2"
        : "mmg040-live-inspection-r1",
      verified_at: "2026-07-21T08:00:00Z",
      operator_id: "content-owner-test",
      method: profileContract.receipt_requirements.method,
    },
    assets: profileContract.assets.map((asset) => ({
      asset_id: asset.asset_id,
      object_path: asset.object_path,
      observed_class: asset.expected_class,
      package_sha256: digest(`package:${asset.asset_id}`),
      loaded: true,
      skeleton_asset_id: asset.skeleton_asset_id,
      notify_names: [...asset.required_notifies],
      root_motion_enabled: asset.root_motion_policy === "required",
    })),
    actions: profileContract.actions.map((action) => {
      const actionReceipt = {
        action: action.action,
        implementation_asset_id: action.implementation_asset_id,
        completion_signal: action.completion_signal,
        implementation_matches: true,
        skeleton_matches: true,
        completion_signal_observed: true,
        behavior_evidence_sha256: digest(`behavior:${action.action}`),
        observed_live_checks: [...action.live_checks],
        verified_parameters: clone(action.parameter_contract),
        ik_contact_verified: ["pick_up", "brace", "drag", "lift_foot"].includes(action.action),
        root_motion_verified: ["drag", "fall", "recover"].includes(action.action),
        collision_verified: action.action === "fall",
        recovery_alignment_verified: action.action === "recover",
      };
      if (isR2) {
        actionReceipt.object_attachment_verified = action.action === "pick_up";
      }
      return actionReceipt;
    }),
    checks: Object.fromEntries(
      profileContract.receipt_requirements.required_checks.map((key) => [key, true]),
    ),
  };
  receipt.content_digest = computeContentDigest(profileContractSha, receipt);
  return receipt;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function expectCode(callback, code) {
  assert.throws(callback, (error) => {
    assert.ok(error instanceof ContentProfileContractError);
    assert.equal(error.code, code);
    return true;
  });
}

function revisionBranch(schema, profileRevision) {
  return schema.allOf.find((entry) => (
    entry.if?.properties?.profile_revision?.const === profileRevision
  ))?.then;
}

test("project profile source is byte-pinned, project-owned, and explicitly blocked", () => {
  assert.equal(
    sha256Bytes(readFileSync(r1ContractPath)),
    "1b0aa6e48d251cb8dbeac4f34528ca8fa6084fb330fc2d150ef341f630528b1c",
    "r1 source bytes are immutable",
  );
  assert.equal(
    sourceSha,
    PINNED_SOURCE_CONTRACT_SHA256_BY_REVISION.mmg040_project_content_r2,
  );
  assert.equal(sourceContract.profile_revision, "mmg040_project_content_r2");
  assert.equal(sourceContract.current_readiness.ready, false);
  assert.equal(sourceContract.assets.length, 14);
  assert.equal(sourceContract.actions.length, 8);
  for (const asset of sourceContract.assets) {
    assert.ok(asset.object_path.startsWith("/Game/VISTA/MMG040/"));
    assert.equal(asset.object_path.includes(".."), false);
  }
  assert.deepEqual(new Set(sourceContract.actions.map((entry) => entry.action)), new Set([
    "look_at", "pick_up", "brace", "drag", "lift_foot", "pause", "fall", "recover",
  ]));
  const pickUp = sourceContract.actions.find((entry) => entry.action === "pick_up");
  assert.deepEqual(clone(pickUp), {
    action: "pick_up",
    adapter_id: "vista_pick_up_ik_v1",
    bridge_action_id: "vista_pick_up_ik_v1",
    implementation_asset_id: "pick_up_montage",
    supporting_asset_ids: ["anim_blueprint", "control_rig", "ik_rig"],
    completion_signal: "vista_pick_up_attached",
    timeout_ms: 8000,
    target_policy: "required",
    actor_capabilities: ["upper_body_ik", "object_attachment"],
    target_capabilities: ["pickupable", "hand_contact_target"],
    anchor_kinds: ["hand_contact"],
    parameter_contract: { duration_sec: 2, distance_cm: null, height_cm: null, hand: "right", foot: null, direction: null },
    live_checks: ["hand_contact", "object_attached", "completion_notify"],
  });

  const report = buildBlockedPreflight(sourceContract, sourceSha);
  assert.equal(report.ready, false);
  assert.equal(report.start_allowed, false);
  assert.ok(report.reason_codes.includes("ANIMATION_MMG040_PROJECT_CONTENT_NOT_AUTHORED"));
  assert.ok(report.reason_codes.includes("ANIMATION_MMG040_LIVE_ASSET_RECEIPT_MISSING"));
});

test("legacy r1 source and receipt shape remain byte-pinned and fully supported", () => {
  assert.equal(PINNED_SOURCE_CONTRACT_SHA256, r1SourceSha);
  assert.equal(
    r1SourceSha,
    PINNED_SOURCE_CONTRACT_SHA256_BY_REVISION.mmg040_project_content_r1,
  );
  assert.equal(r1SourceContract.profile_revision, "mmg040_project_content_r1");
  assert.equal(r1SourceContract.assets.length, 13);
  assert.equal(r1SourceContract.actions.length, 7);
  assert.equal(
    r1SourceContract.actions.some((action) => action.action === "pick_up"),
    false,
  );

  const receipt = makeReceipt(r1SourceContract, r1SourceSha);
  assert.ok(receipt.actions.every((action) => (
    !Object.hasOwn(action, "object_attachment_verified")
  )));
  assert.equal(
    validateInspectionReceipt(r1SourceContract, r1SourceSha, receipt),
    receipt,
  );
});

test("revision-aware validators reject mixed counts and attachment shapes", () => {
  const r1WithR2Asset = clone(r1SourceContract);
  r1WithR2Asset.assets.push(clone(
    sourceContract.assets.find((asset) => asset.asset_id === "pick_up_montage"),
  ));
  expectCode(
    () => validateSourceContract(r1WithR2Asset, r1SourceSha),
    "ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH",
  );

  const r2WithR1ActionCount = clone(sourceContract);
  r2WithR1ActionCount.actions = r2WithR1ActionCount.actions.filter(
    (action) => action.action !== "pick_up",
  );
  expectCode(
    () => validateSourceContract(r2WithR1ActionCount, sourceSha),
    "ANIMATION_MMG040_SOURCE_CONTRACT_MISMATCH",
  );

  const r1WithR2AssetReceipt = makeReceipt(r1SourceContract, r1SourceSha);
  r1WithR2AssetReceipt.assets.push(clone(
    makeReceipt().assets.find((asset) => asset.asset_id === "pick_up_montage"),
  ));
  expectCode(
    () => validateInspectionReceipt(
      r1SourceContract,
      r1SourceSha,
      r1WithR2AssetReceipt,
    ),
    "ANIMATION_MMG040_ASSET_RECEIPT_INCOMPLETE",
  );

  const r2WithR1ActionReceiptCount = makeReceipt();
  r2WithR1ActionReceiptCount.actions = r2WithR1ActionReceiptCount.actions.filter(
    (action) => action.action !== "pick_up",
  );
  expectCode(
    () => validateInspectionReceipt(
      sourceContract,
      sourceSha,
      r2WithR1ActionReceiptCount,
    ),
    "ANIMATION_MMG040_ACTION_RECEIPT_INCOMPLETE",
  );

  const legacyWithAttachment = makeReceipt(r1SourceContract, r1SourceSha);
  legacyWithAttachment.actions[0].object_attachment_verified = false;
  expectCode(
    () => validateInspectionReceipt(
      r1SourceContract,
      r1SourceSha,
      legacyWithAttachment,
    ),
    "ANIMATION_MMG040_RECEIPT_INVALID",
  );

  const r2WithoutAttachment = makeReceipt();
  delete r2WithoutAttachment.actions.find(
    (action) => action.action === "pick_up",
  ).object_attachment_verified;
  expectCode(
    () => validateInspectionReceipt(sourceContract, sourceSha, r2WithoutAttachment),
    "ANIMATION_MMG040_RECEIPT_INVALID",
  );
});

test("shared JSON schemas condition exact r1 and r2 source and receipt shapes", () => {
  const sourceSchema = JSON.parse(readFileSync(path.join(
    pluginRoot,
    "Contract/vista-animation-project-profile-source-v1.schema.json",
  ), "utf8"));
  const receiptSchema = JSON.parse(readFileSync(path.join(
    pluginRoot,
    "Contract/vista-animation-content-inspection-receipt-v1.schema.json",
  ), "utf8"));
  const revisions = [
    "mmg040_project_content_r1",
    "mmg040_project_content_r2",
  ];
  assert.deepEqual(sourceSchema.properties.profile_revision.enum, revisions);
  assert.deepEqual(receiptSchema.properties.profile_revision.enum, revisions);

  const sourceR1 = revisionBranch(sourceSchema, revisions[0]);
  const sourceR2 = revisionBranch(sourceSchema, revisions[1]);
  assert.deepEqual(
    [sourceR1.properties.assets.minItems, sourceR1.properties.assets.maxItems],
    [13, 13],
  );
  assert.deepEqual(
    [sourceR1.properties.actions.minItems, sourceR1.properties.actions.maxItems],
    [7, 7],
  );
  assert.equal(
    sourceR1.properties.actions.items.allOf[1].properties.action.enum.includes("pick_up"),
    false,
  );
  assert.deepEqual(
    [sourceR2.properties.assets.minItems, sourceR2.properties.assets.maxItems],
    [14, 14],
  );
  assert.deepEqual(
    [sourceR2.properties.actions.minItems, sourceR2.properties.actions.maxItems],
    [8, 8],
  );

  const receiptR1 = revisionBranch(receiptSchema, revisions[0]);
  const receiptR2 = revisionBranch(receiptSchema, revisions[1]);
  assert.deepEqual(
    [receiptR1.properties.assets.minItems, receiptR1.properties.assets.maxItems],
    [13, 13],
  );
  assert.deepEqual(
    [receiptR1.properties.actions.minItems, receiptR1.properties.actions.maxItems],
    [7, 7],
  );
  assert.deepEqual(
    receiptR1.properties.actions.items.allOf[2].not.required,
    ["object_attachment_verified"],
  );
  assert.deepEqual(
    [receiptR2.properties.assets.minItems, receiptR2.properties.assets.maxItems],
    [14, 14],
  );
  assert.deepEqual(
    [receiptR2.properties.actions.minItems, receiptR2.properties.actions.maxItems],
    [8, 8],
  );
  assert.deepEqual(
    receiptR2.properties.actions.items.allOf[1].required,
    ["object_attachment_verified"],
  );
});

test("an exact live receipt produces the existing server profile and canonical digest", () => {
  const receipt = makeReceipt();
  assert.equal(validateInspectionReceipt(sourceContract, sourceSha, receipt), receipt);
  const profile = buildServerContentProfile(sourceContract, receipt);
  const validated = animationContract.validateContentProfile(profile);
  assert.equal(validated.profile_id, "vista_mmg040");
  assert.equal(validated.content_digest, receipt.content_digest);
  assert.deepEqual(new Set(validated.actions.map((entry) => entry.action)), new Set([
    "look_at", "pick_up", "brace", "drag", "lift_foot", "pause", "fall", "recover",
  ]));
  assert.equal(validated.actions.find((entry) => entry.action === "drag").implementation_asset,
    "/Game/VISTA/MMG040/Montages/AM_MMG040_DragChair.AM_MMG040_DragChair");

  const reordered = makeReceipt();
  reordered.actions.reverse();
  reordered.assets.reverse();
  for (const action of reordered.actions) action.observed_live_checks.reverse();
  assert.equal(computeContentDigest(sourceSha, reordered), receipt.content_digest);
});

test("profile preparation CLI is non-mutating and fails closed without a receipt", () => {
  const result = spawnSync(process.execPath, [scriptPath, "--contract", contractPath], { encoding: "utf8" });
  assert.equal(result.status, 3, result.stderr);
  const report = JSON.parse(result.stdout);
  assert.equal(report.ready, false);
  assert.equal(report.start_allowed, false);
  assert.equal(report.source_contract_sha256, sourceSha);

  const legacyResult = spawnSync(
    process.execPath,
    [scriptPath, "--contract", r1ContractPath],
    { encoding: "utf8" },
  );
  assert.equal(legacyResult.status, 3, legacyResult.stderr);
  const legacyReport = JSON.parse(legacyResult.stdout);
  assert.equal(legacyReport.profile_revision, "mmg040_project_content_r1");
  assert.equal(legacyReport.ready, false);
  assert.equal(legacyReport.start_allowed, false);
  assert.equal(legacyReport.source_contract_sha256, r1SourceSha);
});

test("profile preparation CLI accepts only protected, exact receipt files", (t) => {
  const temporary = mkdtempSync(path.join(os.homedir(), ".mmg040-profile-test-"));
  t.after(() => rmSync(temporary, { recursive: true, force: true }));
  chmodSync(temporary, 0o700);
  const receiptPath = path.join(temporary, "receipt.json");
  writeFileSync(receiptPath, `${JSON.stringify(makeReceipt(), null, 2)}\n`, { mode: 0o600 });
  const digestCandidate = makeReceipt();
  digestCandidate.content_digest = "0".repeat(64);
  writeFileSync(receiptPath, `${JSON.stringify(digestCandidate, null, 2)}\n`, { mode: 0o600 });
  const digestResult = spawnSync(process.execPath, [
    scriptPath,
    "--contract", contractPath,
    "--receipt", receiptPath,
    "--mode", "digest",
  ], { encoding: "utf8" });
  assert.equal(digestResult.status, 0, digestResult.stderr);
  const digestReport = JSON.parse(digestResult.stdout);
  assert.equal(digestReport.ready, false);
  assert.equal(digestReport.content_digest, makeReceipt().content_digest);

  writeFileSync(receiptPath, `${JSON.stringify(makeReceipt(), null, 2)}\n`, { mode: 0o600 });
  const result = spawnSync(process.execPath, [
    scriptPath,
    "--contract", contractPath,
    "--receipt", receiptPath,
  ], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  const profile = JSON.parse(result.stdout);
  assert.equal(animationContract.validateContentProfile(profile).content_digest, makeReceipt().content_digest);

  chmodSync(receiptPath, 0o666);
  const weak = spawnSync(process.execPath, [scriptPath, "--contract", contractPath, "--receipt", receiptPath], { encoding: "utf8" });
  assert.equal(weak.status, 2);
  assert.match(weak.stderr, /ANIMATION_MMG040_RECEIPT_PATH_INVALID/);
});

test("asset, skeleton, montage notify, root motion, behavior, and digest mismatches are rejected", () => {
  const cases = [
    ["ANIMATION_MMG040_PINNED_ASSET_MISMATCH", (receipt) => {
      receipt.assets.find((entry) => entry.asset_id === "pawn_class").object_path = "/Game/Human_Avatar/BP_Human.BP_Human_C";
    }],
    ["ANIMATION_MMG040_PINNED_ASSET_MISMATCH", (receipt) => {
      receipt.assets.find((entry) => entry.asset_id === "drag_montage").skeleton_asset_id = null;
    }],
    ["ANIMATION_MMG040_NOTIFY_MISMATCH", (receipt) => {
      receipt.assets.find((entry) => entry.asset_id === "fall_montage").notify_names = [];
    }],
    ["ANIMATION_MMG040_ROOT_MOTION_MISMATCH", (receipt) => {
      receipt.assets.find((entry) => entry.asset_id === "drag_montage").root_motion_enabled = false;
    }],
    ["ANIMATION_MMG040_BEHAVIOR_RECEIPT_MISMATCH", (receipt) => {
      receipt.actions.find((entry) => entry.action === "brace").ik_contact_verified = false;
    }],
    ["ANIMATION_MMG040_BEHAVIOR_RECEIPT_MISMATCH", (receipt) => {
      receipt.actions.find((entry) => entry.action === "pick_up").object_attachment_verified = false;
    }],
    ["ANIMATION_MMG040_BEHAVIOR_RECEIPT_MISMATCH", (receipt) => {
      const brace = receipt.actions.find((entry) => entry.action === "brace");
      brace.observed_live_checks = brace.observed_live_checks.filter((entry) => entry !== "feet_planted");
    }],
    ["ANIMATION_MMG040_BEHAVIOR_RECEIPT_MISMATCH", (receipt) => {
      receipt.actions.find((entry) => entry.action === "drag").observed_live_checks.push("bogus_check");
    }],
    ["ANIMATION_MMG040_BEHAVIOR_RECEIPT_MISMATCH", (receipt) => {
      receipt.actions.find((entry) => entry.action === "drag").verified_parameters.distance_cm = 121;
    }],
    ["ANIMATION_MMG040_BEHAVIOR_RECEIPT_MISMATCH", (receipt) => {
      receipt.actions.find((entry) => entry.action === "fall").verified_parameters.direction = "backward";
    }],
    ["ANIMATION_MMG040_ACTION_RECEIPT_MISMATCH", (receipt) => {
      receipt.actions.find((entry) => entry.action === "recover").completion_signal_observed = false;
    }],
    ["ANIMATION_MMG040_ACTION_RECEIPT_MISMATCH", (receipt) => {
      receipt.actions.find((entry) => entry.action === "fall").completion_signal = "timer_elapsed";
    }],
    ["ANIMATION_MMG040_ACTION_RECEIPT_MISMATCH", (receipt) => {
      receipt.actions.find((entry) => entry.action === "pick_up").completion_signal = "EndHandTrace";
    }],
    ["ANIMATION_MMG040_CONTENT_DIGEST_MISMATCH", (receipt) => {
      receipt.content_digest = "f".repeat(64);
    }],
  ];
  for (const [code, mutate] of cases) {
    const receipt = makeReceipt();
    mutate(receipt);
    expectCode(() => validateInspectionReceipt(sourceContract, sourceSha, receipt), code);
  }
});

test("production mmg_040 action parameters are pinned to server defaults", () => {
  const parameters = clone(Object.fromEntries(sourceContract.actions.map((action) => [action.action, action.parameter_contract])));
  for (const action of sourceContract.actions) {
    assert.deepEqual(clone(action.parameter_contract), {
      duration_sec: null,
      distance_cm: null,
      height_cm: null,
      hand: null,
      foot: null,
      direction: null,
      ...animationContract.ACTION_DEFINITIONS[action.action].defaults,
    });
  }
  assert.deepEqual(parameters, {
    look_at: { duration_sec: 1, distance_cm: null, height_cm: null, hand: null, foot: null, direction: null },
    pick_up: { duration_sec: 2, distance_cm: null, height_cm: null, hand: "right", foot: null, direction: null },
    brace: { duration_sec: 2, distance_cm: null, height_cm: null, hand: "both", foot: null, direction: null },
    drag: { duration_sec: 2, distance_cm: 120, height_cm: null, hand: "right", foot: null, direction: null },
    lift_foot: { duration_sec: 2, distance_cm: null, height_cm: 35, hand: null, foot: "left", direction: null },
    pause: { duration_sec: 3, distance_cm: null, height_cm: null, hand: null, foot: null, direction: null },
    fall: { duration_sec: null, distance_cm: null, height_cm: null, hand: null, foot: null, direction: "forward" },
    recover: { duration_sec: null, distance_cm: null, height_cm: null, hand: null, foot: null, direction: "forward" },
  });
});

test("strict JSON and protected-file reader reject duplicate keys, links, and aliases", (t) => {
  expectCode(
    () => parseStrictJson('{"schema":"one","schema":"two"}'),
    "ANIMATION_MMG040_JSON_DUPLICATE_KEY",
  );
  expectCode(
    () => parseStrictJson(Buffer.from([0x7b, 0x22, 0x78, 0x22, 0x3a, 0xff, 0x7d])),
    "ANIMATION_MMG040_JSON_INVALID",
  );
  expectCode(
    () => parseStrictJson('{\u00a0"schema":"one"}'),
    "ANIMATION_MMG040_JSON_INVALID",
  );

  const temporary = mkdtempSync(path.join(os.homedir(), ".mmg040-path-test-"));
  t.after(() => rmSync(temporary, { recursive: true, force: true }));
  chmodSync(temporary, 0o700);
  const receiptPath = path.join(temporary, "receipt.json");
  writeFileSync(receiptPath, `${JSON.stringify(makeReceipt())}\n`, { mode: 0o600 });
  assert.equal(readSecureJson(receiptPath, "receipt").value.schema,
    "vista-animation-content-inspection-receipt/v1");

  const linked = path.join(temporary, "linked.json");
  symlinkSync(receiptPath, linked);
  expectCode(() => readSecureJson(linked, "receipt"), "ANIMATION_MMG040_RECEIPT_PATH_INVALID");

  const hardlinked = path.join(temporary, "hardlinked.json");
  linkSync(receiptPath, hardlinked);
  expectCode(() => readSecureJson(receiptPath, "receipt"), "ANIMATION_MMG040_RECEIPT_PATH_INVALID");
});

test("packaged plugin keeps source contract, schemas, and preparation helper", () => {
  const filter = readFileSync(path.join(pluginRoot, "Config/FilterPlugin.ini"), "utf8");
  assert.match(filter, /^\/Contract\/\.\.\.$/m);
  assert.match(filter, /^\/ContentProfiles\/\.\.\.$/m);
  assert.match(filter, /^\/Scripts\/prepare-content-profile\.mjs$/m);
  for (const relative of [
    "Contract/vista-animation-project-profile-source-v1.schema.json",
    "Contract/vista-animation-content-inspection-receipt-v1.schema.json",
    "ContentProfiles/vista-mmg040-project-profile-source-v1.json",
    "ContentProfiles/vista-mmg040-project-profile-source-v2.json",
    "Scripts/prepare-content-profile.mjs",
  ]) assert.doesNotThrow(() => readFileSync(path.join(pluginRoot, relative)));
});
