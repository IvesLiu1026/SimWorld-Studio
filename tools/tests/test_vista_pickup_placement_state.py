from __future__ import annotations

import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
PRIVATE_SOURCE = (
    ROOT
    / "unreal_plugins/VistaPlayableHome/Source/VistaPlayableHome/Private/VistaPickupActor.cpp"
)
PUBLIC_HEADER = (
    ROOT
    / "unreal_plugins/VistaPlayableHome/Source/VistaPlayableHome/Public/VistaPickupActor.h"
)


def function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"unterminated function: {signature}")


def declaration(source: str, signature: str) -> str:
    start = source.index(signature)
    ending = source.index(";", start)
    return source[start:ending]


class VistaPickupPlacementStateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = PRIVATE_SOURCE.read_text(encoding="utf-8")
        cls.header = PUBLIC_HEADER.read_text(encoding="utf-8")

    def test_place_derives_wire_id_from_unique_stable_anchor_actor(self) -> None:
        resolver = function_body(
            self.source,
            "bool StablePlacementAnchorSemanticId(",
        )
        self.assertIn('TEXT("VistaSemanticId=")', self.source)
        self.assertIn('TEXT("VistaOwner=")', self.source)
        self.assertIn('TEXT("/anchor.")', self.source)
        self.assertIn('OwnerSemanticId.Contains(TEXT("/entity.")', self.source)
        self.assertIn("PlacementAnchor != Owner->GetRootComponent()", resolver)
        self.assertIn("Owner->Tags", resolver)
        self.assertIn("IsUniqueStablePlacementAnchor(Owner->GetWorld(), Match, Owner)", resolver)
        self.assertNotIn("Request", resolver)

        release = function_body(
            self.source,
            "FVistaInteractionResult AVistaPickupActor::ReleaseFromCarrier(",
        )
        validation = release.index("StablePlacementAnchorSemanticId(")
        mutation = release.index("DetachFromActor(")
        self.assertLess(validation, mutation)
        self.assertIn('TEXT("PLACEMENT_ANCHOR_NOT_STABLE")', release)
        self.assertIn(
            'RuntimeStateValues.Add(TEXT("placed_at"), PlacementAnchorSemanticId)',
            release,
        )
        release_declaration = declaration(
            self.header,
            "FVistaInteractionResult ReleaseFromCarrier(",
        )
        self.assertIn("USceneComponent* PlacementAnchor", release_declaration)
        self.assertNotIn("FString", release_declaration)

    def test_pickup_and_drop_remove_stale_placement(self) -> None:
        attach = function_body(
            self.source,
            "FVistaInteractionResult AVistaPickupActor::TryAttachTo(",
        )
        self.assertIn('RuntimeStateValues.Remove(TEXT("placed_at"))', attach)
        self.assertLess(
            attach.index("VistaTryClaimItem"),
            attach.index('RuntimeStateValues.Remove(TEXT("placed_at"))'),
        )

        release = function_body(
            self.source,
            "FVistaInteractionResult AVistaPickupActor::ReleaseFromCarrier(",
        )
        drop_branch = release[release.index("if (!IsValid(PlacementAnchor))") :]
        self.assertIn('RuntimeStateValues.Remove(TEXT("placed_at"))', drop_branch)

        state = function_body(
            self.source,
            "FVistaEntityRuntimeState AVistaPickupActor::VistaGetRuntimeState_Implementation() const",
        )
        self.assertIn("if (IsValid(HeldBy))", state)
        self.assertIn('State.Values.Remove(TEXT("placed_at"))', state)

    def test_baseline_compact_id_is_normalized_and_resettable(self) -> None:
        canonicalizer = function_body(
            self.source,
            "bool CanonicalizePlacementAnchorSemanticId(",
        )
        self.assertIn("FindChar(TEXT('#'), CompactDelimiterIndex)", canonicalizer)
        self.assertIn("PlacementAnchorDelimiter", canonicalizer)
        self.assertIn("IsStablePlacementAnchorSemanticId(Candidate)", canonicalizer)

        apply_state = function_body(
            self.source,
            "FVistaInteractionResult AVistaPickupActor::VistaApplyRuntimeState_Implementation(",
        )
        self.assertIn("NormalizeStoredPlacementAnchor", apply_state)
        self.assertIn('TEXT("BASELINE_PLACEMENT_ANCHOR_NOT_FOUND")', apply_state)
        self.assertIn(
            'RuntimeStateValues.Add(TEXT("placed_at"), NormalizedPlacement)',
            apply_state,
        )
        self.assertIn('RuntimeStateValues.Remove(TEXT("placed_at"))', apply_state)
        self.assertIn("Mesh->SetSimulatePhysics(bPortable && !bRestorePlacement)", apply_state)

        begin_play = function_body(
            self.source,
            "void AVistaPickupActor::BeginPlay()",
        )
        self.assertIn("Super::BeginPlay()", begin_play)
        self.assertIn("NormalizePlacementState()", begin_play)
        normalizer = function_body(
            self.source,
            "void AVistaPickupActor::NormalizePlacementState()",
        )
        self.assertIn("Mesh->SetSimulatePhysics(false)", normalizer)

    def test_every_home_baseline_placement_resolves_to_a_materialized_anchor(self) -> None:
        house = json.loads(
            (ROOT / "world_packs/vista_playable_home_r1/house.json").read_text(
                encoding="utf-8"
            )
        )
        anchors = {
            f'{entity["entity_id"]}/anchor.{anchor["anchor_id"]}'
            for entity in house["entities"]
            for anchor in entity["placement_anchors"]
        }
        baseline_placements = {
            entity["entity_id"]: entity["initial_state"].get("placed_at")
            for entity in house["entities"]
            if entity["initial_state"].get("placed_at") is not None
        }
        self.assertGreaterEqual(len(baseline_placements), 3)
        for item_id, compact_id in baseline_placements.items():
            with self.subTest(item_id=item_id):
                owner_id, separator, anchor_id = compact_id.partition("#")
                self.assertEqual(separator, "#")
                self.assertIn(f"{owner_id}/anchor.{anchor_id}", anchors)


if __name__ == "__main__":
    unittest.main()
