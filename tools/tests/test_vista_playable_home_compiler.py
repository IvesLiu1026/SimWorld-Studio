from __future__ import annotations

import copy
import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout


TOOLS_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOLS_ROOT.parent

from worlds import playable_home as compiler  # noqa: E402


PACK_ROOT = REPOSITORY_ROOT / "world_packs" / "vista_playable_home_r1"


class PlayableHomeCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.house = compiler.load_json(PACK_ROOT / "house.json")
        self.events = compiler.load_events(PACK_ROOT / "events")

    def assert_contract_error(self, code: str, callback) -> compiler.PlayableHomeContractError:
        with self.assertRaises(compiler.PlayableHomeContractError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def test_two_resolutions_and_reversed_event_inputs_are_identical(self) -> None:
        first = compiler.compile_build_plan(self.house, self.events)
        second = compiler.compile_build_plan(copy.deepcopy(self.house), list(reversed(copy.deepcopy(self.events))))
        self.assertEqual(compiler.canonical_json_bytes(first), compiler.canonical_json_bytes(second))
        self.assertEqual(first["content_digest"], second["content_digest"])
        self.assertEqual(first["content_digest"], compiler.content_digest(first))

    def test_plan_is_closed_schema_valid_and_has_exact_bindings(self) -> None:
        plan = compiler.compile_build_plan(self.house, self.events)
        compiler.validate_build_plan(plan)
        self.assertEqual(plan["schema_version"], compiler.BUILD_PLAN_SCHEMA_VERSION)
        self.assertEqual(plan["house"]["content_digest"], self.house["content_digest"])
        self.assertEqual(plan["unreal"]["content_namespace"], "/Game/VISTA/PlayableHome/vista_playable_home_r1")
        self.assertEqual(plan["unreal"]["stable_tag_prefix"], "VistaSemanticId=")
        self.assertEqual(len(plan["rooms"]), 6)
        self.assertEqual(len(plan["entities"]), 34)
        self.assertEqual(len(plan["event_plans"]), 7)
        self.assertEqual(plan["unreal"]["navigation_bounds_cm"], {"min_cm": [-650, -400, 0], "max_cm": [650, 800, 300]})
        primary = next(event for event in plan["event_plans"] if event["event_id"] == "mmg_001")
        self.assertTrue(primary["public_goals"])
        self.assertTrue(primary["triggers"])
        self.assertTrue(primary["success_conditions"])
        self.assertEqual(primary["reset_policy"]["mode"], "restore_baseline")
        self.assertEqual(primary["source"]["public_reference"], "vista://assist-step/mmg_001")

    def test_room_local_transforms_resolve_to_world_centimeters(self) -> None:
        plan = compiler.compile_build_plan(self.house, self.events)
        entities = {entity["entity_id"]: entity for entity in plan["entities"]}
        coffee_table = entities["home.r1/room.living_room/entity.coffee_table.01"]
        self.assertEqual(coffee_table["world_transform_cm"]["location_cm"], [-400, -170, 0])
        phone = entities["home.r1/room.bedroom/entity.phone.01"]
        self.assertEqual(phone["world_transform_cm"]["location_cm"], [-340, 320, 64])
        player = plan["runtime_profile"]["player_start"]
        self.assertEqual(player["world_transform_cm"]["location_cm"], [0, -250, 10])

    def test_rotation_composition_is_deterministic(self) -> None:
        changed = copy.deepcopy(self.house)
        living = next(room for room in changed["rooms"] if room["kind"] == "living_room")
        living["transform"]["rotation_deg"] = [0, 0, 90]
        changed = compiler.seal_document(changed)
        plan = compiler.compile_build_plan(changed, [])
        table = next(entity for entity in plan["entities"] if entity["category"] == "coffee_table")
        self.assertEqual(table["world_transform_cm"]["location_cm"], [-430, -200, 0])
        self.assertEqual(table["world_transform_cm"]["rotation_deg"], [0, 0, 90])

    def test_event_transform_operations_are_resolved(self) -> None:
        plan = compiler.compile_build_plan(self.house, self.events)
        event = next(item for item in plan["event_plans"] if item["event_id"] == "mmg_044")
        operation = next(item for item in event["operations"] if item["op"] == "set_transform")
        self.assertEqual(operation["world_transform_cm"]["location_cm"], [-435, -170, 50])
        self.assertNotIn("transform", operation)

    def test_public_payload_is_allowlisted_and_stable(self) -> None:
        event = copy.deepcopy(self.events[0])
        event["oracle_assistance_required"] = True
        event["review_notes"] = "must never appear"
        public = compiler.public_event_payload(event)
        serialized = compiler.canonical_json_bytes(public).decode("utf-8")
        self.assertNotIn("oracle", serialized)
        self.assertNotIn("review_notes", serialized)
        original = compiler.public_event_payload(self.events[0])
        self.assertEqual(original["content_digest"], self.events[0]["content_digest"])

    def test_build_plan_unknown_field_and_digest_mismatch_fail_closed(self) -> None:
        plan = compiler.compile_build_plan(self.house, self.events)
        changed = copy.deepcopy(plan)
        changed["host_path"] = "/private/path"
        changed = compiler.seal_document(changed)
        self.assert_contract_error("VISTA_HOME_SCHEMA_INVALID", lambda: compiler.validate_build_plan(changed))

        changed = copy.deepcopy(plan)
        changed["plan_id"] = "home.r1@vista_playable_home_r2"
        self.assert_contract_error("VISTA_HOME_DIGEST_MISMATCH", lambda: compiler.validate_build_plan(changed))

    def test_build_plan_target_and_npc_action_fail_closed(self) -> None:
        plan = compiler.compile_build_plan(self.house, self.events)
        changed = copy.deepcopy(plan)
        changed["event_plans"][0]["participating_entity_ids"][0] = "home.r1/room.entry_hall/entity.unknown.99"
        changed = compiler.seal_document(changed)
        self.assert_contract_error("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", lambda: compiler.validate_build_plan(changed))

        changed = copy.deepcopy(plan)
        changed["event_plans"][0]["operations"].append({
            "op_id": "op.99", "op": "set_npc_queue", "npc_id": "npc.resident",
            "actions": [{"action": "navigate_to"}],
        })
        changed = compiler.seal_document(changed)
        self.assert_contract_error("VISTA_HOME_SCHEMA_INVALID", lambda: compiler.validate_build_plan(changed))

    def test_all_seven_overlays_apply_without_mutating_baseline(self) -> None:
        original = copy.deepcopy(self.house)
        for event in self.events:
            with self.subTest(event_id=event["event_id"]):
                applied = compiler.apply_event_overlay(self.house, event)
                self.assertEqual(applied["active_event_id"], event["event_id"])
        self.assertEqual(self.house, original)

    def test_cli_validate_and_compile_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "plan.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = compiler.main([
                    "validate", "--house", str(PACK_ROOT / "house.json"),
                    "--events-dir", str(PACK_ROOT / "events"),
                ])
            self.assertEqual(code, 0)
            receipt = json.loads(stdout.getvalue())
            self.assertTrue(receipt["ok"])
            self.assertEqual(receipt["event_count"], 7)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = compiler.main([
                    "compile", "--house", str(PACK_ROOT / "house.json"),
                    "--events-dir", str(PACK_ROOT / "events"), "--output", str(output),
                ])
            self.assertEqual(code, 0)
            receipt = json.loads(stdout.getvalue())
            self.assertTrue(receipt["ok"])
            plan = compiler.load_json(output)
            compiler.validate_build_plan(plan)
            self.assertEqual(receipt["content_digest"], plan["content_digest"])
            self.assertEqual(output.read_bytes(), compiler.canonical_json_bytes(plan) + b"\n")


if __name__ == "__main__":
    unittest.main()
