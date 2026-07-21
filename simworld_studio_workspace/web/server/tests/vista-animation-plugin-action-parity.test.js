"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const animationContract = require("../vista-animation-contract");
const animationReadiness = require("../vista-animation-ue-readiness");
const repositoryRoot = path.resolve(__dirname, "../../../..");
const pluginRoot = path.join(
  repositoryRoot,
  "unreal_plugins/VistaAnimationContentApi",
);

function readPlugin(relativePath) {
  return fs.readFileSync(path.join(pluginRoot, relativePath));
}

test("portable plugin action set exactly matches the server action contract", () => {
  const portable = JSON.parse(readPlugin(
    "Contract/vista-animation-content-api-v1.json",
  ));
  const expected = Object.entries(animationContract.ACTION_DEFINITIONS).map(
    ([action, definition]) => ({
      action,
      bridge_action_id: definition.bridge_action_id,
      target_policy: definition.target_policy,
    }),
  );
  assert.deepEqual(portable.actions, expected);
});

test("MMG040 r2 exposes only the exact typed pick-up attachment policy", () => {
  const profile = JSON.parse(readPlugin(
    "ContentProfiles/vista-mmg040-project-profile-source-v2.json",
  ));
  assert.equal(profile.profile_revision, "mmg040_project_content_r2");
  assert.equal(profile.current_readiness.ready, false);
  assert.equal(profile.assets.length, 14);
  assert.equal(profile.actions.length, 8);

  const pickUp = profile.actions.find((action) => action.action === "pick_up");
  assert.ok(pickUp);
  assert.equal(pickUp.adapter_id, "vista_pick_up_ik_v1");
  assert.equal(pickUp.bridge_action_id, "vista_pick_up_ik_v1");
  assert.equal(pickUp.completion_signal, "vista_pick_up_attached");
  assert.deepEqual(pickUp.actor_capabilities, [
    "upper_body_ik",
    "object_attachment",
  ]);
  assert.deepEqual(pickUp.target_capabilities, [
    "pickupable",
    "hand_contact_target",
  ]);
  assert.deepEqual(pickUp.anchor_kinds, ["hand_contact"]);
  assert.deepEqual(pickUp.live_checks, [
    "hand_contact",
    "object_attached",
    "completion_notify",
  ]);
  assert.deepEqual(pickUp.parameter_contract, {
    duration_sec: 2,
    distance_cm: null,
    height_cm: null,
    hand: "right",
    foot: null,
    direction: null,
  });

  const pickUpMontage = profile.assets.find(
    (asset) => asset.asset_id === "pick_up_montage",
  );
  assert.deepEqual(pickUpMontage.required_notifies, [
    "vista_pick_up_attached",
  ]);
  assert.equal(pickUpMontage.root_motion_policy, "forbidden");
  assert.notEqual(pickUp.completion_signal, "EndHandTrace");
  assert.equal(
    pickUpMontage.required_notifies.includes("EndHandTrace"),
    false,
  );
});

test("typed C++ surface dispatches PickUp and preserves immutable r1 bytes", () => {
  const driverInterface = readPlugin(
    "Source/VistaAnimationContentApi/Public/VistaAnimationContentDriver.h",
  ).toString("utf8");
  const projectHeader = readPlugin(
    "Source/VistaAnimationContentApi/Public/VistaMmg040ContentDriver.h",
  ).toString("utf8");
  const projectSource = readPlugin(
    "Source/VistaAnimationContentApi/Private/VistaMmg040ContentDriver.cpp",
  ).toString("utf8");
  const subsystemSource = readPlugin(
    "Source/VistaAnimationContentApi/Private/VistaAnimationContentApiSubsystem.cpp",
  ).toString("utf8");

  assert.match(driverInterface, /\bPickUp\b/);
  assert.match(projectHeader, /\bPickUpMontage\b/);
  assert.match(projectHeader, /virtual bool StartPickUp\(/);
  assert.match(projectHeader, /bool bObjectAttachmentVerified = false;/);
  assert.match(projectSource, /Backend->StartPickUp\(/);
  assert.match(projectSource, /TEXT\("vista_pick_up_attached"\)/);
  assert.match(projectSource, /TEXT\("object_attachment"\)/);
  assert.match(projectSource, /TEXT\("object_attached"\)/);
  assert.match(projectSource, /LegacyHandTraceCompletionSignal/);
  assert.match(subsystemSource, /EVistaAnimationAction::PickUp/);
  assert.match(subsystemSource, /TEXT\("vista_pick_up_ik_v1"\)/);

  const r1Hash = crypto
    .createHash("sha256")
    .update(readPlugin(
      "ContentProfiles/vista-mmg040-project-profile-source-v1.json",
    ))
    .digest("hex");
  assert.equal(
    r1Hash,
    "1b0aa6e48d251cb8dbeac4f34528ca8fa6084fb330fc2d150ef341f630528b1c",
  );
});

test("MMG040 r2 stays candidate-only until an exact compatibility receipt exists", () => {
  assert.deepEqual(
    animationReadiness.CONTENT_PROFILE_PLUGIN_COMPATIBILITY[
      "vista_mmg040/mmg040_project_content_r1"
    ],
    {
      plugin_version: "1.1.0",
      engine_version: "5.3.2",
      target_platform: "linux-x86_64",
    },
  );
  assert.equal(
    Object.hasOwn(
      animationReadiness.CONTENT_PROFILE_PLUGIN_COMPATIBILITY,
      "vista_mmg040/mmg040_project_content_r2",
    ),
    false,
  );
});
