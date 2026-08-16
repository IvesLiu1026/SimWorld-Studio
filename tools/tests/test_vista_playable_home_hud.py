from __future__ import annotations

import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_SOURCE = (
    ROOT / "unreal_plugins/VistaPlayableHome/Source/VistaPlayableHome"
)
HUD_CPP = PLUGIN_SOURCE / "Private/VistaPlayableHomeHUD.cpp"
CHARACTER_CPP = PLUGIN_SOURCE / "Private/VistaPlayableHomeCharacter.cpp"
CHARACTER_HEADER = PLUGIN_SOURCE / "Public/VistaPlayableHomeCharacter.h"


class VistaPlayableHomeHudSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hud = HUD_CPP.read_text(encoding="utf-8")
        self.character_cpp = CHARACTER_CPP.read_text(encoding="utf-8")
        self.character_header = CHARACTER_HEADER.read_text(encoding="utf-8")

    def test_context_prompt_and_input_share_one_action_resolver(self) -> None:
        public_section = self.character_header.split("public:", 1)[1].split(
            "protected:", 1
        )[0]
        self.assertIn(
            "EVistaAffordance GetDefaultInteractionAffordance(AActor* Target) const;",
            public_section,
        )
        self.assertNotIn("ChooseDefaultAffordance", self.character_header)
        self.assertIn(
            "TryInteract(GetDefaultInteractionAffordance(Target))",
            self.character_cpp,
        )
        self.assertIn(
            "Character.GetDefaultInteractionAffordance(Target)",
            self.hud,
        )
        self.assertIn("IsValid(Held) && Target != Held", self.hud)

        for label in (
            "Open %s",
            "Close %s",
            "Pick Up %s",
            "Place %s",
            "Turn On %s",
            "Turn Off %s",
            "Sit on %s",
            "Inspect %s",
        ):
            self.assertIn(f'TEXT("{label}")', self.hud)

    def test_player_facing_copy_never_draws_raw_semantic_or_event_ids(self) -> None:
        for obsolete_surface in (
            "GetFocusedSemanticId",
            "GetActiveEventId",
            "E  Interact  |",
            "Held:",
            "Event:",
        ):
            self.assertNotIn(obsolete_surface, self.hud)

        self.assertNotIn("*Held->SemanticId", self.hud)
        self.assertIn(
            "DrawText(FriendlyNameFromSemanticId(Held->SemanticId), Primary,",
            self.hud,
        )
        self.assertNotRegex(
            self.hud,
            re.compile(
                r"DrawText\s*\([^;]*Execute_VistaGetSemanticId",
                re.DOTALL,
            ),
        )

    def test_friendly_names_are_derived_without_event_or_item_cheats(self) -> None:
        self.assertIn("FriendlyNameFromSemanticId", self.hud)
        self.assertIn("FindLastChar(TEXT('/')", self.hud)
        self.assertIn("Leaf.ParseIntoArray(Tokens, TEXT(\"_\"), true)", self.hud)
        self.assertIn("Token.IsNumeric()", self.hud)
        self.assertIn("FChar::ToUpper", self.hud)
        self.assertIn('FString(TEXT("Object"))', self.hud)

        folded = self.hud.casefold()
        self.assertNotIn("mmg_", folded)
        self.assertNotIn('text("keys")', folded)
        self.assertNotIn("event_id", folded)

    def test_objective_uses_public_goal_and_friendly_status_only(self) -> None:
        self.assertIn("Events->GetPublicGoal()", self.hud)
        self.assertIn("Events->GetEventStatus()", self.hud)
        for status_label in (
            "STARTING",
            "IN PROGRESS",
            "COMPLETE",
            "FAILED",
            "TIME EXPIRED",
            "RESETTING",
        ):
            self.assertIn(f'TEXT("{status_label}")', self.hud)
        self.assertIn('DrawText(TEXT("OBJECTIVE")', self.hud)

    def test_visual_surface_is_restrained_and_resolution_aware(self) -> None:
        self.assertIn("Canvas->ClipY / 1080.0f", self.hud)
        self.assertIn("FMath::Clamp", self.hud)
        self.assertGreaterEqual(self.hud.count("DrawRect("), 6)
        self.assertIn('TEXT("[E]  %s")', self.hud)
        self.assertIn('TEXT("CARRYING")', self.hud)
        self.assertIn('TEXT("[Q] DROP")', self.hud)


if __name__ == "__main__":
    unittest.main()
