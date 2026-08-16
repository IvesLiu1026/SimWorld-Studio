from __future__ import annotations

import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_SOURCE = (
    ROOT
    / "unreal_plugins"
    / "VistaPlayableHome"
    / "Source"
    / "VistaPlayableHome"
)
CHARACTER_HEADER = PLUGIN_SOURCE / "Public" / "VistaPlayableHomeCharacter.h"
CHARACTER_SOURCE = PLUGIN_SOURCE / "Private" / "VistaPlayableHomeCharacter.cpp"
SPRING_ARM_HEADER = PLUGIN_SOURCE / "Public" / "VistaIndoorSpringArmComponent.h"
SPRING_ARM_SOURCE = PLUGIN_SOURCE / "Private" / "VistaIndoorSpringArmComponent.cpp"
VISUAL_PROFILE = (
    ROOT
    / "world_packs"
    / "vista_playable_home_r1"
    / "visual_profiles"
    / "realistic_interior_r2.json"
)


def _function_body(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


def _assigned_float(source: str, field: str) -> float:
    match = re.search(rf"Profile\.{re.escape(field)}\s*=\s*([0-9.]+)f;", source)
    if match is None:
        raise AssertionError(f"missing closed camera field {field}")
    return float(match.group(1))


def _finterp_to(current: float, target: float, delta_s: float, speed: float) -> float:
    """Scalar equivalent of the FMath::FInterpTo branch used by the component."""

    if speed <= 0.0:
        return target
    distance = target - current
    if distance * distance < 1.0e-8:
        return target
    delta_move = distance * min(max(delta_s * speed, 0.0), 1.0)
    return current + delta_move


def _recovery_step(
    current: float,
    collision_fraction: float | None,
    delta_s: float,
    speed: float,
) -> float:
    target = 1.0 if collision_fraction is None else collision_fraction
    if collision_fraction is not None and collision_fraction <= current:
        return collision_fraction
    return _finterp_to(current, target, max(delta_s, 0.0), speed)


class RealisticIndoorCameraSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.header = CHARACTER_HEADER.read_text(encoding="utf-8")
        cls.character = CHARACTER_SOURCE.read_text(encoding="utf-8")
        cls.spring_header = SPRING_ARM_HEADER.read_text(encoding="utf-8")
        cls.spring = SPRING_ARM_SOURCE.read_text(encoding="utf-8")
        cls.profile = json.loads(VISUAL_PROFILE.read_text(encoding="utf-8"))

    def test_r2_profile_is_measured_and_bound_to_the_closed_visual_profile_id(self) -> None:
        self.assertEqual(self.profile["visual_profile_id"], "realistic_interior_r2")
        self.assertIn(
            'RealisticInteriorR2CameraProfileId(TEXT("realistic_interior_r2"))',
            self.character,
        )

        target_boom = _assigned_float(self.character, "TargetBoomLengthCm")
        field_of_view = _assigned_float(self.character, "FieldOfViewDegrees")
        self.assertGreaterEqual(target_boom, 180.0)
        self.assertLessEqual(target_boom, 240.0)
        self.assertGreaterEqual(field_of_view, 75.0)
        self.assertLessEqual(field_of_view, 85.0)
        self.assertEqual(target_boom, 220.0)
        self.assertEqual(field_of_view, 80.0)
        self.assertEqual(_assigned_float(self.character, "CollisionProbeSizeCm"), 18.0)
        self.assertEqual(_assigned_float(self.character, "CollisionRecoverySpeed"), 8.0)

    def test_r1_constructor_values_remain_the_no_profile_default(self) -> None:
        constructor = _function_body(
            self.character,
            "AVistaPlayableHomeCharacter::AVistaPlayableHomeCharacter()",
            "void AVistaPlayableHomeCharacter::BeginPlay()",
        )
        self.assertIn("CameraBoom->TargetArmLength = 320.0f;", constructor)
        self.assertIn(
            "CameraBoom->SocketOffset = FVector(0.0f, 55.0f, 65.0f);",
            constructor,
        )
        self.assertIn("CameraBoom->CameraLagSpeed = 12.0f;", constructor)
        self.assertNotIn("SetFieldOfView", constructor)
        self.assertIn('ActiveCameraProfileId = TEXT("legacy_r1")', self.header)

        # The subclass is behaviorally inert until an r2 profile is applied.
        self.assertIn("if (!bIndoorCollisionRecoveryEnabled)", self.spring)
        self.assertIn("return StockLocation;", self.spring)
        self.assertIn("Super::BlendLocations(", self.spring)

    def test_profile_selection_is_explicit_and_unknown_values_fail_closed(self) -> None:
        begin_play = _function_body(
            self.character,
            "void AVistaPlayableHomeCharacter::BeginPlay()",
            "void AVistaPlayableHomeCharacter::ApplyRequestedCameraProfile()",
        )
        selection = _function_body(
            self.character,
            "void AVistaPlayableHomeCharacter::ApplyRequestedCameraProfile()",
            "bool AVistaPlayableHomeCharacter::ApplyIndoorCameraProfile(",
        )
        self.assertIn("ApplyRequestedCameraProfile();", begin_play)
        self.assertIn('TEXT("VistaCameraProfile=")', self.character)
        self.assertIn("if (!FParse::Value(", selection)
        self.assertIn("keeping legacy_r1", selection)
        self.assertLess(
            selection.index("return;", selection.index("if (!FParse::Value(")),
            selection.index("ApplyIndoorCameraProfile("),
        )

    def test_invalid_settings_are_rejected_before_any_camera_mutation(self) -> None:
        validation = _function_body(
            self.character,
            "bool FVistaIndoorCameraProfile::IsValid(FString& OutReason) const",
            "AVistaPlayableHomeCharacter::AVistaPlayableHomeCharacter()",
        )
        apply_profile = _function_body(
            self.character,
            "bool AVistaPlayableHomeCharacter::ApplyIndoorCameraProfile(",
            "void AVistaPlayableHomeCharacter::EndPlay(",
        )
        self.assertIn("FMath::IsFinite(TargetBoomLengthCm)", validation)
        self.assertIn("TargetBoomLengthCm < 180.0f", validation)
        self.assertIn("TargetBoomLengthCm > 240.0f", validation)
        self.assertIn("FieldOfViewDegrees < 75.0f", validation)
        self.assertIn("FieldOfViewDegrees > 85.0f", validation)
        self.assertIn("required_safety_feature_disabled", validation)

        guard = apply_profile.index("if (!Profile.IsValid(ValidationReason))")
        first_mutation = apply_profile.index(
            "IndoorBoom->TargetArmLength = Profile.TargetBoomLengthCm;"
        )
        component_guard = apply_profile.index("if (!IsValid(IndoorBoom)")
        self.assertLess(guard, component_guard)
        self.assertLess(component_guard, first_mutation)
        self.assertIn("ProbeChannel = ECC_Camera;", apply_profile)
        self.assertIn("bDoCollisionTest = Profile.bEnableCameraCollision;", apply_profile)
        self.assertIn("CameraLagMaxTimeStep = 1.0f / 60.0f;", apply_profile)

    def test_wall_hit_snaps_inward_and_clear_path_recovers_without_overshoot(self) -> None:
        self.assertIn("TargetFraction <= RecoveryArmFraction", self.spring)
        self.assertIn("RecoveryArmFraction = TargetFraction;", self.spring)
        self.assertIn("RecoveryArmFraction = FMath::FInterpTo(", self.spring)
        self.assertIn(
            "return FMath::Lerp(ArmOrigin, DesiredArmLocation, RecoveryArmFraction);",
            self.spring,
        )

        # A new close wall must retract in one step; outward motion is monotonic
        # and bounded by the current collision-tested segment.
        fraction = _recovery_step(1.0, 0.30, 1.0 / 60.0, 8.0)
        self.assertEqual(fraction, 0.30)
        outward_hit = _recovery_step(fraction, 0.58, 1.0 / 60.0, 8.0)
        self.assertGreater(outward_hit, fraction)
        self.assertLessEqual(outward_hit, 0.58)
        closer_doorframe = _recovery_step(outward_hit, 0.24, 1.0 / 60.0, 8.0)
        self.assertEqual(closer_doorframe, 0.24)

        samples: list[float] = []
        fraction = closer_doorframe
        for _ in range(90):
            fraction = _recovery_step(fraction, None, 1.0 / 60.0, 8.0)
            samples.append(fraction)
        self.assertTrue(all(left <= right <= 1.0 for left, right in zip(samples, samples[1:])))
        self.assertGreater(samples[-1], 0.999)

    def test_recovery_component_contract_has_no_tick_or_visibility_side_effects(self) -> None:
        # Recovery is isolated to the spring-arm collision blend.  It neither
        # hides scene actors nor adds a second per-frame camera trace.
        self.assertIn("virtual FVector BlendLocations(", self.spring_header)
        for prohibited in (
            "SetActorHiddenInGame",
            "SetVisibility",
            "TickComponent",
            "LineTrace",
            "SweepSingle",
        ):
            self.assertNotIn(prohibited, self.spring + self.spring_header)


if __name__ == "__main__":
    unittest.main()
