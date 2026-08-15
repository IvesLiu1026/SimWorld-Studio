from __future__ import annotations

import copy
import json
import math
import pathlib
import tempfile
import unittest

import jsonschema


TOOLS_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOLS_ROOT.parent

from worlds import playable_home as contract  # noqa: E402


PACK_ROOT = REPOSITORY_ROOT / "world_packs" / "vista_playable_home_r1"
HOUSE_PATH = PACK_ROOT / "house.json"
EVENT_ROOT = PACK_ROOT / "events"
SCHEMA_ROOT = REPOSITORY_ROOT / "world_packs" / "schemas"


class PlayableHomeFixtureMixin:
    def setUp(self) -> None:
        self.house = contract.load_json(HOUSE_PATH)
        self.events = {path.stem: contract.load_json(path) for path in sorted(EVENT_ROOT.glob("*.json"))}

    def assert_contract_error(self, code: str, callback) -> contract.PlayableHomeContractError:
        with self.assertRaises(contract.PlayableHomeContractError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    @staticmethod
    def reseal(document: dict) -> dict:
        return contract.seal_document(document)


class PlayableHomeSchemaAndFixtureTests(PlayableHomeFixtureMixin, unittest.TestCase):
    def test_all_schemas_are_closed_and_meta_valid(self) -> None:
        expected = {
            "vista-playable-house-v1.schema.json",
            "vista-playable-event-v1.schema.json",
            "vista-playable-home-build-plan-v1.schema.json",
        }
        self.assertTrue(expected.issubset({path.name for path in SCHEMA_ROOT.glob("*.json")}))

        def assert_closed_objects(node, path: str) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, path)
                for key, child in node.items():
                    assert_closed_objects(child, f"{path}.{key}")
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    assert_closed_objects(child, f"{path}[{index}]")

        for path in sorted(SCHEMA_ROOT.glob("*.json")):
            with self.subTest(path=path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                jsonschema.Draft202012Validator.check_schema(schema)
                self.assertFalse(schema["additionalProperties"])
                assert_closed_objects(schema, path.name)

    def test_house_and_all_seven_verified_events_validate(self) -> None:
        contract.validate_house(self.house)
        self.assertEqual(set(self.events), {"mmg_001", "mmg_013", "mmg_021", "mmg_040", "mmg_044", "mmg_045", "mmg_070"})
        for event_id, event in self.events.items():
            with self.subTest(event_id=event_id):
                contract.validate_event(event, self.house)
                self.assertEqual(event["source"]["verification"], "verified")

    def test_r1_topology_inventory_and_gameplay_minimums(self) -> None:
        self.assertEqual(
            {room["kind"] for room in self.house["rooms"]},
            {"entry_hall", "living_room", "kitchen_dining", "bedroom", "office", "bathroom_laundry"},
        )
        self.assertEqual(len(self.house["portals"]), 5)
        self.assertEqual(len(self.house["entities"]), 34)
        self.assertTrue(all(room["review_cameras"] for room in self.house["rooms"]))
        portable = [entity for entity in self.house["entities"] if "pick_up" in entity["affordances"]]
        doors = [entity for entity in self.house["entities"] if entity["component_role"] == "door"]
        npcs = [entity for entity in self.house["entities"] if entity["component_role"] == "npc"]
        self.assertGreaterEqual(len(portable), 3)
        self.assertGreaterEqual(len(doors), 2)
        self.assertEqual(len(npcs), 1)
        self.assertEqual(contract._reachable_rooms(self.house), {room["room_id"] for room in self.house["rooms"]})
        for entity in self.house["entities"]:
            if entity["component_role"] not in {"door", "npc"}:
                self.assertEqual(entity["asset_ref"], f"asset.prop.{entity['category']}")

    def test_public_fixtures_have_no_private_or_executable_material(self) -> None:
        corpus = json.dumps([self.house, *self.events.values()], sort_keys=True).lower()
        for forbidden in (
            "oracle_assistance_required", "oracle_label", "review_notes", "private_evidence",
            "evaluation_target", "no_assistance_", "execute_python_script", "shell_command",
            "/home/", "/nas/", "postgres_url", "access_token",
        ):
            self.assertNotIn(forbidden, corpus)

    def test_unknown_house_field_fails_closed(self) -> None:
        changed = copy.deepcopy(self.house)
        changed["mystery"] = True
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_SCHEMA_INVALID", lambda: contract.validate_house(changed))

    def test_duplicate_id_is_rejected_semantically(self) -> None:
        changed = copy.deepcopy(self.house)
        duplicate = copy.deepcopy(changed["rooms"][0])
        changed["rooms"].append(duplicate)
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_DUPLICATE_ID", lambda: contract.validate_house(changed))

    def test_invalid_transform_and_nonfinite_number_are_rejected(self) -> None:
        changed = copy.deepcopy(self.house)
        changed["rooms"][0]["transform"]["scale"][0] = 0
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_SCHEMA_INVALID", lambda: contract.validate_house(changed))

        nonfinite = copy.deepcopy(self.house)
        nonfinite["rooms"][0]["transform"]["location_m"][0] = math.nan
        self.assert_contract_error("VISTA_HOME_JSON_NON_FINITE", lambda: contract.validate_house(nonfinite))

    def test_strict_loader_rejects_nan_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "nan.json"
            path.write_text('{"value": NaN}', encoding="utf-8")
            self.assert_contract_error("VISTA_HOME_JSON_NON_FINITE", lambda: contract.load_json(path))

    def test_asset_uri_traversal_is_rejected(self) -> None:
        changed = copy.deepcopy(self.house)
        changed["asset_catalog"][0]["uri"] = "bundle://vista-playable-home-r1/../secret"
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_ASSET_URI_UNSAFE", lambda: contract.validate_house(changed))

    def test_disconnected_graph_is_rejected(self) -> None:
        changed = copy.deepcopy(self.house)
        for portal in changed["portals"]:
            portal["nav_policy"] = "blocked"
            portal["door_entity_id"] = None
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_GRAPH_DISCONNECTED", lambda: contract.validate_house(changed))

    def test_unknown_portal_room_and_state_disagreement_are_rejected(self) -> None:
        changed = copy.deepcopy(self.house)
        changed["portals"][0]["to_room_id"] = "home.r1/room.unknown"
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_PORTAL_ROOM_INVALID", lambda: contract.validate_house(changed))

        changed = copy.deepcopy(self.house)
        changed["portals"][0]["initial_state"] = "open"
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_PORTAL_STATE_MISMATCH", lambda: contract.validate_house(changed))

    def test_unsupported_affordance_is_rejected(self) -> None:
        changed = copy.deepcopy(self.house)
        sofa = next(entity for entity in changed["entities"] if entity["category"] == "sofa")
        sofa["affordances"].append("toggle")
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_AFFORDANCE_INVALID", lambda: contract.validate_house(changed))

    def test_executable_and_oracle_fields_are_rejected(self) -> None:
        for key in ("execute_python_script", "oracle_assistance_required"):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.events["mmg_001"])
                changed[key] = "not allowed"
                changed = self.reseal(changed)
                self.assert_contract_error("VISTA_HOME_SCHEMA_INVALID", lambda changed=changed: contract.validate_event(changed, self.house))

        nested = copy.deepcopy(self.events["mmg_001"])
        nested["initial_operations"][0]["caller_script"] = "do something"
        nested = self.reseal(nested)
        self.assert_contract_error("VISTA_HOME_SCHEMA_INVALID", lambda: contract.validate_event(nested, self.house))

        private_path = copy.deepcopy(self.events["mmg_001"])
        private_path["title"] = "/home/researcher/private-evaluation.json"
        private_path = self.reseal(private_path)
        self.assert_contract_error("VISTA_HOME_PRIVATE_PATH_PROHIBITED", lambda: contract.validate_event(private_path, self.house))

    def test_digest_mismatch_is_rejected(self) -> None:
        changed = copy.deepcopy(self.house)
        changed["seed"] += 1
        self.assert_contract_error("VISTA_HOME_DIGEST_MISMATCH", lambda: contract.validate_house(changed))

    def test_stale_event_revision_is_rejected(self) -> None:
        changed = copy.deepcopy(self.events["mmg_001"])
        changed["compatible_house"]["revision"] = "vista_playable_home_r2"
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_EVENT_STALE_REVISION", lambda: contract.validate_event(changed, self.house))

    def test_unknown_event_target_is_rejected(self) -> None:
        changed = copy.deepcopy(self.events["mmg_044"])
        unknown = "home.r1/room.living_room/entity.keys.99"
        changed["participating_entity_ids"][1] = unknown
        changed["initial_operations"][0]["target_id"] = unknown
        changed["initial_operations"][1]["target_id"] = unknown
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_EVENT_TARGET_UNKNOWN", lambda: contract.validate_event(changed, self.house))

    def test_event_affordance_and_duplicate_operation_fail_closed(self) -> None:
        changed = copy.deepcopy(self.events["mmg_044"])
        changed["failure_conditions"][0]["affordance"] = "toggle"
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_EVENT_AFFORDANCE_INVALID", lambda: contract.validate_event(changed, self.house))

        changed = copy.deepcopy(self.events["mmg_044"])
        changed["initial_operations"][1]["op_id"] = "op.01"
        changed = self.reseal(changed)
        self.assert_contract_error("VISTA_HOME_DUPLICATE_ID", lambda: contract.validate_event(changed, self.house))

    def test_overlay_application_is_atomic_and_resettable(self) -> None:
        original = copy.deepcopy(self.house)
        baseline = contract.baseline_runtime_state(self.house)
        applied = contract.apply_event_overlay(self.house, self.events["mmg_001"])
        stove_id = "home.r1/room.kitchen_dining/entity.stove.01"
        self.assertTrue(applied["entities"][stove_id]["state"]["active"])
        self.assertEqual(applied["active_event_id"], "mmg_001")
        self.assertFalse(baseline["entities"][stove_id]["state"]["active"])
        self.assertEqual(contract.baseline_runtime_state(self.house), baseline)
        self.assertEqual(self.house, original)

        invalid = copy.deepcopy(self.events["mmg_001"])
        invalid["initial_operations"][0]["target_id"] = "home.r1/room.kitchen_dining/entity.stove.99"
        invalid = self.reseal(invalid)
        self.assert_contract_error("VISTA_HOME_EVENT_TARGET_UNKNOWN", lambda: contract.apply_event_overlay(self.house, invalid))
        self.assertEqual(self.house, original)


if __name__ == "__main__":
    unittest.main()
