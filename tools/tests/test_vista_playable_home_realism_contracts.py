from __future__ import annotations

import copy
import json
import math
import pathlib
import tempfile
import unittest

import jsonschema

from world_packs.vista_playable_home_r1.visual_profiles import contract


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPOSITORY_ROOT / "world_packs" / "schemas" / "vista-playable-home-visual-profile-v1.schema.json"
HOUSE_PATH = REPOSITORY_ROOT / "world_packs" / "vista_playable_home_r1" / "house.json"
PROFILE_PATH = (
    REPOSITORY_ROOT
    / "world_packs"
    / "vista_playable_home_r1"
    / "visual_profiles"
    / "realistic_interior_r2.json"
)


class RealisticInteriorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.house = contract.load_json(HOUSE_PATH)
        self.profile = contract.load_json(PROFILE_PATH)

    def assert_contract_error(self, code: str, callback) -> contract.VisualProfileContractError:
        with self.assertRaises(contract.VisualProfileContractError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    @staticmethod
    def reseal(document: dict) -> dict:
        return contract.seal_document(document)

    def test_schema_is_meta_valid_and_every_object_is_closed(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        self.assertFalse(schema["additionalProperties"])

        def assert_closed(node, path: str) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, path)
                for key, value in node.items():
                    assert_closed(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    assert_closed(value, f"{path}[{index}]")

        assert_closed(schema, "$")

    def test_fixture_validates_and_pins_three_room_scope(self) -> None:
        contract.validate_profile(self.profile, self.house)
        self.assertEqual(self.profile["schema_version"], contract.SCHEMA_VERSION)
        self.assertEqual(set(self.profile["finished_room_ids"]), contract.EXPECTED_FINISHED_ROOMS)
        self.assertEqual(
            set(self.profile["compatibility_room_ids"]),
            {room["room_id"] for room in self.house["rooms"]} - contract.EXPECTED_FINISHED_ROOMS,
        )
        self.assertEqual(self.profile["architecture_profile"]["portal_topology_policy"], "preserve_r1")
        self.assertEqual(self.profile["architecture_profile"]["collision_policy"], "hidden_r1_proxies")

    def test_profile_and_receipt_digests_are_canonical_and_repeatable(self) -> None:
        expected = "ba7283f04ebacc2e3dc157980af2a20e9be62bbc8232de60b6da6a206c2e9d32"
        self.assertEqual(self.profile["content_digest"], expected)
        self.assertEqual(contract.content_digest(self.profile), expected)
        first = self.reseal(self.profile)
        second = self.reseal(first)
        self.assertEqual(first, second)

        reverse_key_order = {key: self.profile[key] for key in reversed(list(self.profile))}
        self.assertEqual(contract.content_digest(reverse_key_order), expected)
        for receipt in self.profile["asset_source_receipts"]:
            self.assertEqual(receipt["receipt_digest"], contract.content_digest(receipt, "receipt_digest"))

    def test_external_hero_receipts_pin_truthful_poly_haven_sources(self) -> None:
        receipts = {
            receipt["logical_asset_id"]: receipt
            for receipt in self.profile["asset_source_receipts"]
        }
        expected = {
            "visual.hero.living_coffee_table": {
                "source_uri": "polyhaven://models/modern_coffee_table_01",
                "source_digest": "cf5fac22ac00b8725f91ad4565ddaa32dc5f10b213a0938a92de9e2432c1ddfe",
                "source_version": "files-31772c0aab6f930a18de82606146c0a97f08b7d0",
                "min_m": [-0.6009150147438049, -0.30000004172325134, 0],
                "max_m": [0.6009150147438049, 0.30000004172325134, 0.38999998569488525],
                "blend_mode": "opaque",
                "texture_semantics": ["base_color", "normal", "roughness"],
                "texture_count": 3,
                "entitlement_record": "local-audit://poly-haven-cc0-20260816/modern_coffee_table_01",
                "attribution": "Modern Coffee Table 01 by Poly Haven, provided under CC0 1.0.",
                "modification_notice": (
                    "The Poly Haven source is floor-centered, uniformly scaled, and exported as an "
                    "identity-root presentation bundle; source geometry and textures are otherwise retained."
                ),
                "receipt_digest": "b1ab6a246f9e80c94c29e2fc4d08be6f2dfed5d19561c9ba8825e387700996d8",
            },
            "visual.hero.kitchen_stove": {
                "source_uri": "polyhaven://models/electric_stove",
                "source_digest": "c55acbd188af4674ce5c1c8605f2447c5fb830a05b1650b0d03296b419b38795",
                "source_version": "files-750ee10bdfe78eb6b0b620ef7b5a898e436fb696",
                "min_m": [-0.25129741430282593, -0.3238105922937393, 0],
                "max_m": [0.25129741430282593, 0.3238105922937393, 0.8586971759796143],
                "blend_mode": "masked",
                "texture_semantics": ["base_color", "normal", "roughness", "metalness", "opacity"],
                "texture_count": 5,
                "entitlement_record": "local-audit://poly-haven-cc0-20260816/electric_stove",
                "attribution": "Electric Stove by Poly Haven, provided under CC0 1.0.",
                "modification_notice": (
                    "The Poly Haven source is floor-centered and exported as an identity-root presentation "
                    "bundle. Its receipt-bound opacity texture is preserved; the direct opacity-to-Principled "
                    "Alpha link is sanitized in Blender 4.5.8 to a GREATER_THAN 0.5 clip graph so glTF exports "
                    "alphaMode MASK (effective alphaCutoff 0.5), and VISTA source/digest/active-semantic/"
                    "alpha-policy material extras are added. Geometry and other receipt-bound PBR texture "
                    "semantics are otherwise retained."
                ),
                "receipt_digest": "2f053518117c2738a9f98379ffacfe11f78e2e4d8e5d34b0cd0c9e0a36a8d339",
            },
        }

        for logical_asset_id, pinned in expected.items():
            receipt = receipts[logical_asset_id]
            self.assertEqual(receipt["source_kind"], "existing_local")
            self.assertEqual(receipt["source_uri"], pinned["source_uri"])
            self.assertEqual(receipt["source_digest"], pinned["source_digest"])
            self.assertEqual(receipt["source_version"], pinned["source_version"])
            self.assertEqual(receipt["metric_bounds_m"]["min_m"], pinned["min_m"])
            self.assertEqual(receipt["metric_bounds_m"]["max_m"], pinned["max_m"])
            self.assertEqual(receipt["license"]["license_id"], "CC0-1.0")
            self.assertEqual(
                receipt["license"]["license_url"],
                "https://creativecommons.org/publicdomain/zero/1.0/",
            )
            self.assertEqual(receipt["license"]["entitlement_status"], "verified")
            self.assertEqual(receipt["license"]["entitlement_record"], pinned["entitlement_record"])
            self.assertEqual(receipt["license"]["attribution"], pinned["attribution"])
            self.assertEqual(
                receipt["license"]["modification_notice"],
                pinned["modification_notice"],
            )
            self.assertEqual(receipt["license"]["commercial_use"], "allowed")
            self.assertEqual(receipt["license"]["redistribution_restriction"], "project_policy")
            slot = receipt["material_inventory"]["slots"][0]
            self.assertEqual(slot["blend_mode"], pinned["blend_mode"])
            self.assertEqual(slot["texture_semantics"], pinned["texture_semantics"])
            self.assertEqual(slot["minimum_texture_size_px"], 4096)
            self.assertEqual(receipt["material_inventory"]["texture_count"], pinned["texture_count"])
            self.assertEqual(receipt["import_policy"]["nanite"], "disabled_ineligible")
            self.assertEqual(receipt["import_policy"]["mobility"], "static")
            self.assertEqual(receipt["import_policy"]["lod_policy"], "single_mesh_measured")
            self.assertEqual(receipt["import_policy"]["collision_policy"], "hidden_r1_proxy")
            self.assertEqual(receipt["receipt_digest"], pinned["receipt_digest"])

    def test_unknown_and_executable_fields_fail_closed(self) -> None:
        unknown = copy.deepcopy(self.profile)
        unknown["mystery"] = True
        unknown = self.reseal(unknown)
        self.assert_contract_error(
            "VISTA_VISUAL_SCHEMA_INVALID", lambda: contract.validate_profile(unknown, self.house)
        )

        executable = copy.deepcopy(self.profile)
        executable["renderer_profile"]["execute_python_script"] = "do not run"
        executable = self.reseal(executable)
        self.assert_contract_error(
            "VISTA_VISUAL_PROHIBITED_FIELD", lambda: contract.validate_profile(executable, self.house)
        )

        nested = copy.deepcopy(self.profile)
        nested["dressing_instances"][0]["caller_script"] = "unsafe"
        nested = self.reseal(nested)
        self.assert_contract_error(
            "VISTA_VISUAL_PROHIBITED_FIELD", lambda: contract.validate_profile(nested, self.house)
        )

    def test_stale_house_revision_and_digest_fail_closed(self) -> None:
        stale_house = copy.deepcopy(self.house)
        stale_house["revision"] = "vista_playable_home_r2"
        self.assert_contract_error(
            "VISTA_VISUAL_STALE_HOUSE_REVISION",
            lambda: contract.validate_profile(self.profile, stale_house),
        )

        stale_digest = copy.deepcopy(self.house)
        stale_digest["content_digest"] = "f" * 64
        self.assert_contract_error(
            "VISTA_VISUAL_STALE_HOUSE_DIGEST",
            lambda: contract.validate_profile(self.profile, stale_digest),
        )

        schema_stale = copy.deepcopy(self.profile)
        schema_stale["house_revision"] = "vista_playable_home_r2"
        schema_stale = self.reseal(schema_stale)
        self.assert_contract_error(
            "VISTA_VISUAL_SCHEMA_INVALID",
            lambda: contract.validate_profile(schema_stale, self.house),
        )

    def test_duplicate_binding_targets_and_ids_fail_closed(self) -> None:
        duplicate_target = copy.deepcopy(self.profile)
        second = copy.deepcopy(duplicate_target["semantic_visual_bindings"][0])
        second["binding_id"] = "binding.entry_shoe_bench.02"
        duplicate_target["semantic_visual_bindings"].append(second)
        duplicate_target = self.reseal(duplicate_target)
        self.assert_contract_error(
            "VISTA_VISUAL_DUPLICATE_ID",
            lambda: contract.validate_profile(duplicate_target, self.house),
        )

        duplicate_source = copy.deepcopy(self.profile)
        source = copy.deepcopy(duplicate_source["asset_source_receipts"][0])
        source["receipt_id"] = "source.architecture.duplicate"
        duplicate_source["asset_source_receipts"].append(source)
        duplicate_source = self.reseal(duplicate_source)
        self.assert_contract_error(
            "VISTA_VISUAL_DUPLICATE_ID",
            lambda: contract.validate_profile(duplicate_source, self.house),
        )

    def test_strict_loader_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "duplicate.json"
            path.write_text('{"schema_version":"a","schema_version":"b"}', encoding="utf-8")
            self.assert_contract_error("VISTA_VISUAL_DUPLICATE_KEY", lambda: contract.load_json(path))

    def test_nonfinite_and_invalid_transforms_fail_closed(self) -> None:
        nonfinite = copy.deepcopy(self.profile)
        nonfinite["semantic_visual_bindings"][0]["transform_offset"]["location_cm"][0] = math.nan
        self.assert_contract_error(
            "VISTA_VISUAL_JSON_NON_FINITE",
            lambda: contract.validate_profile(nonfinite, self.house),
        )

        invalid_scale = copy.deepcopy(self.profile)
        invalid_scale["dressing_instances"][0]["transform"]["scale"][0] = 0
        invalid_scale = self.reseal(invalid_scale)
        self.assert_contract_error(
            "VISTA_VISUAL_SCHEMA_INVALID",
            lambda: contract.validate_profile(invalid_scale, self.house),
        )

    def test_path_escape_and_private_absolute_paths_fail_closed(self) -> None:
        traversal = copy.deepcopy(self.profile)
        traversal["asset_source_receipts"][0]["source_uri"] = (
            "project://vista-playable-home-realism/../private/architecture"
        )
        traversal = self.reseal(traversal)
        self.assert_contract_error(
            "VISTA_VISUAL_URI_UNSAFE", lambda: contract.validate_profile(traversal, self.house)
        )

        private_path = copy.deepcopy(self.profile)
        private_path["asset_source_receipts"][0]["license"]["attribution"] = (
            "/mnt/private/license-receipt.json"
        )
        private_path = self.reseal(private_path)
        self.assert_contract_error(
            "VISTA_VISUAL_PRIVATE_PATH_PROHIBITED",
            lambda: contract.validate_profile(private_path, self.house),
        )

    def test_missing_or_incompatible_licensing_fails_closed(self) -> None:
        missing = copy.deepcopy(self.profile)
        del missing["asset_source_receipts"][0]["license"]["entitlement_record"]
        missing = self.reseal(missing)
        self.assert_contract_error(
            "VISTA_VISUAL_SCHEMA_INVALID", lambda: contract.validate_profile(missing, self.house)
        )

        hssd_mislabeled = copy.deepcopy(self.profile)
        receipt = hssd_mislabeled["asset_source_receipts"][0]
        receipt["source_kind"] = "hssd"
        receipt["source_uri"] = "hssd://objects/example"
        receipt["license"]["entitlement_status"] = "verified"
        hssd_mislabeled = self.reseal(hssd_mislabeled)
        self.assert_contract_error(
            "VISTA_VISUAL_SCHEMA_INVALID",
            lambda: contract.validate_profile(hssd_mislabeled, self.house),
        )

    def test_receipt_digest_and_binding_source_mismatch_fail_closed(self) -> None:
        receipt_drift = copy.deepcopy(self.profile)
        receipt_drift["asset_source_receipts"][0]["source_digest"] = "a" * 64
        receipt_drift["content_digest"] = contract.content_digest(receipt_drift)
        self.assert_contract_error(
            "VISTA_VISUAL_RECEIPT_DIGEST_MISMATCH",
            lambda: contract.validate_profile(receipt_drift, self.house),
        )

        mismatched = copy.deepcopy(self.profile)
        mismatched["semantic_visual_bindings"][0]["source_receipt_id"] = (
            "source.hero.living_sofa"
        )
        mismatched = self.reseal(mismatched)
        self.assert_contract_error(
            "VISTA_VISUAL_SOURCE_UNKNOWN",
            lambda: contract.validate_profile(mismatched, self.house),
        )

    def test_review_shots_use_look_at_not_caller_euler_rotation(self) -> None:
        for shot in self.profile["review_shots"]:
            self.assertIn("eye_location_cm", shot)
            self.assertIn("look_at_target_cm", shot)
            self.assertNotIn("rotation_deg", shot)
            self.assertGreaterEqual(shot["near_field_clearance_cm"], 25)

        euler = copy.deepcopy(self.profile)
        euler["review_shots"][0]["rotation_deg"] = [-10, 0, 90]
        euler = self.reseal(euler)
        self.assert_contract_error(
            "VISTA_VISUAL_SCHEMA_INVALID", lambda: contract.validate_profile(euler, self.house)
        )

        outside_room = copy.deepcopy(self.profile)
        outside_room["review_shots"][0]["eye_location_cm"] = [-650, -180, 165]
        outside_room = self.reseal(outside_room)
        self.assert_contract_error(
            "VISTA_VISUAL_REVIEW_SHOT_INVALID",
            lambda: contract.validate_profile(outside_room, self.house),
        )

    def test_practical_lights_use_world_coordinates_inside_declared_rooms(self) -> None:
        locations = {
            light["light_id"]: light["location_cm"]
            for light in self.profile["lighting_rig"]["practical_lights"]
        }
        self.assertEqual(locations["light.entry_hall.01"], [0, -80, 245])
        self.assertEqual(locations["light.living_room.01"], [-520, -180, 165])
        self.assertEqual(locations["light.kitchen_dining.01"], [400, -180, 250])

        room_local_mistake = copy.deepcopy(self.profile)
        room_local_mistake["lighting_rig"]["practical_lights"][1]["location_cm"] = [120, -180, 165]
        room_local_mistake = self.reseal(room_local_mistake)
        self.assert_contract_error(
            "VISTA_VISUAL_LIGHTING_RIG_INVALID",
            lambda: contract.validate_profile(room_local_mistake, self.house),
        )

    def test_public_fixture_contains_no_private_paths_or_unresolved_fallback(self) -> None:
        corpus = json.dumps(self.profile, sort_keys=True).lower()
        for prohibited in (
            "/home/",
            "/root/",
            "/mnt/",
            "file://",
            "execute_python_script",
            "shell_command",
            "fallback_cube",
            "access_token",
        ):
            self.assertNotIn(prohibited, corpus)
        self.assertEqual(self.profile["material_quality_tier"]["default_material_policy"], "reject")


if __name__ == "__main__":
    unittest.main()
