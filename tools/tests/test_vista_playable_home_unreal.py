from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import pathlib
import py_compile
import re
import struct
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.ue.vista_playable_home import contract, planning  # noqa: E402
from tools.ue.vista_playable_home.commandlet_common import (  # noqa: E402
    BUILTIN_URI_ALLOWLIST,
    derived_asset_path,
)


def asset(asset_id: str, uri: str) -> dict:
    return {
        "asset_id": asset_id,
        "source_kind": "builtin",
        "uri": uri,
        "source_digest": hashlib.sha256(uri.encode()).hexdigest(),
        "license": "project-owned",
    }


def world_transform(x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict:
    return {
        "location_cm": [x, y, z],
        "rotation_deg": [0.0, 0.0, 0.0],
        "scale": [1.0, 1.0, 1.0],
    }


def commandlet_glb_texture_counter():
    """Load only the commandlet's pure GLB helpers without importing Unreal."""

    path = ROOT / "tools/ue/vista_playable_home/import_assets_commandlet.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {"_integer", "load_glb_texture_graph", "declared_core_texture_count"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"json": json, "os": os, "struct": struct}

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    namespace["require"] = require
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["declared_core_texture_count"]


def commandlet_material_texture_inspector(unreal_namespace):
    """Load the commandlet's reflected material helper against a fake UE API."""

    path = ROOT / "tools/ue/vista_playable_home/import_assets_commandlet.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {"property_or_none", "_texture2d_path", "_material_texture2d_paths"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"unreal": unreal_namespace}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_material_texture2d_paths"]


def glb_bytes(graph: dict, binary: bytes) -> bytes:
    json_raw = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode("utf-8")
    json_raw += b" " * ((-len(json_raw)) % 4)
    binary_raw = binary + b"\x00" * ((-len(binary)) % 4)
    chunks = struct.pack("<I4s", len(json_raw), b"JSON") + json_raw
    chunks += struct.pack("<I4s", len(binary_raw), b"BIN\x00") + binary_raw
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


def build_plan() -> dict:
    cube = asset("asset.room.shell", "builtin://engine/basic-shapes/cube")
    pawn = asset("asset.runtime.player", "builtin://vista/playable-home/pawn")
    game_mode = asset("asset.runtime.game_mode", "builtin://vista/playable-home/game-mode")
    npc = asset("asset.runtime.npc", "builtin://vista/playable-home/npc")
    room_id = "home.r1/room.living_room"
    npc_id = "home.r1/room.living_room/entity.person.01"
    plan = {
        "schema_version": planning.BUILD_PLAN_SCHEMA,
        "plan_id": "home.demo@vista_playable_home_r1",
        "house": {
            "house_id": "home.demo",
            "revision": "vista_playable_home_r1",
            "content_digest": "1" * 64,
        },
        "units": "centimeters",
        "assets": [cube, pawn, game_mode, npc],
        "rooms": [{
            "room_id": room_id,
            "kind": "living_room",
            "label": "Living room",
            "bundle": cube,
            "world_transform_cm": world_transform(),
            "world_bounds_cm": {"min_cm": [-400.0, -300.0, 0.0], "max_cm": [400.0, 300.0, 300.0]},
            "anchor_world_cm": [0.0, 0.0, 100.0],
            "review_cameras": [{
                "camera_id": "overview",
                "world_transform_cm": world_transform(0.0, -250.0, 180.0),
                "fov_deg": 70.0,
            }],
            "semantic_inventory": [npc_id],
        }],
        "portals": [],
        "entities": [{
            "entity_id": npc_id,
            "room_id": room_id,
            "category": "person",
            "asset": npc,
            "world_transform_cm": world_transform(0.0, 0.0, 0.0),
            "tags": ["resident"],
            "component_role": "npc",
            "mobility": "movable",
            "collision_policy": "pawn",
            "nav_obstacle": False,
            "affordances": [],
            "baseline_state": {"status": "idle"},
            "placement_anchors": [{
                "anchor_id": "right_hand",
                "world_transform_cm": world_transform(20.0, 0.0, 120.0),
            }],
        }],
        "relations": [],
        "runtime_profile": {
            "player_start": {"room_id": room_id, "world_transform_cm": world_transform(100.0, 0.0, 10.0)},
            "pawn": pawn,
            "game_mode": game_mode,
            "controls": {"move": True, "look": True, "jump": True, "sprint": True, "crouch": True, "interact": True},
            "interaction_distance_cm": 250.0,
            "navigation_agent": {"radius_cm": 34.0, "height_cm": 192.0, "max_step_height_cm": 45.0, "max_slope_deg": 44.0},
            "npc_profiles": [{
                "npc_id": "npc.resident",
                "entity_id": npc_id,
                "home_room_id": room_id,
                "patrol_room_ids": [room_id],
                "action_timeout_s": 10.0,
            }],
            "remote_surface": "game_only",
        },
        "event_plans": [],
        "unreal": {
            "content_namespace": "/Game/VISTA/PlayableHome/VistaPlayableHomeR1",
            "map_path": "/Game/VISTA/PlayableHome/VistaPlayableHomeR1/Maps/VistaPlayableHome",
            "composition_order": list(planning.EXPECTED_COMPOSITION_ORDER),
            "navigation_bounds_cm": {"min_cm": [-500.0, -400.0, -100.0], "max_cm": [500.0, 400.0, 400.0]},
            "room_graph_portal_ids": [],
            "stable_tag_prefix": "VistaSemanticId=",
        },
        "provenance": {
            "compiler_version": "vista-playable-home-compiler/1",
            "source_commit": "a" * 40,
            "house_digest": "1" * 64,
            "event_digests": [],
        },
        "content_digest": "2" * 64,
    }
    return plan


class PlayableHomePlanningTests(unittest.TestCase):
    def test_composition_is_deterministic_and_maps_gameplay_roles(self) -> None:
        first = planning.build_composition_spec(build_plan())
        second = planning.build_composition_spec(copy.deepcopy(build_plan()))
        self.assertEqual(first.raw, second.raw)
        self.assertEqual(first.sha256, second.sha256)
        kinds = [operation["kind"] for operation in first.value["operations"]]
        self.assertIn("place_player_start", kinds)
        self.assertIn("configure_game_mode", kinds)
        self.assertIn("place_navmesh_bounds", kinds)
        npc = next(operation for operation in first.value["operations"]
                   if operation.get("component_role") == "npc")
        self.assertEqual(npc["actor_class"], "/Script/VistaPlayableHome.VistaHomeNpcCharacter")
        self.assertEqual(npc["collision"]["profile"], "Pawn")
        self.assertEqual(npc["transform"]["location_cm"][2], 96.0)
        self.assertEqual(npc["floor_contact_offset_cm"], 96.0)
        self.assertEqual(
            npc["npc_profile"]["patrol_target_semantic_ids"],
            ["home.r1/room.living_room/anchor.room_center"],
        )
        player_start = next(operation for operation in first.value["operations"]
                            if operation["kind"] == "place_player_start")
        self.assertEqual(player_start["transform"]["location_cm"][2], 106.0)
        lighting = next(operation for operation in first.value["operations"]
                        if operation["kind"] == "place_lighting")
        self.assertEqual(len(lighting["indoor_lights"]), 1)
        self.assertEqual(lighting["profile"], "vista_playable_home_neutral_day_v2")
        self.assertEqual(lighting["light_mobility"], "movable")
        self.assertEqual(lighting["exposure"], {
            "method": "manual",
            "bias": -6.0,
            "apply_physical_camera_exposure": False,
        })
        placement_anchor = next(operation for operation in first.value["operations"]
                                if operation["kind"] == "place_placement_anchor")
        self.assertEqual(
            placement_anchor["semantic_id"],
            npc["semantic_id"] + "/anchor.right_hand",
        )
        self.assertTrue(all(operation["operation_id"].startswith("ueop-")
                            for operation in first.value["operations"]))

    def test_namespace_role_collision_and_unknown_fields_fail_closed(self) -> None:
        invalid = build_plan()
        invalid["unreal"]["map_path"] = "/Game/VISTA/Other/Map"
        with self.assertRaisesRegex(planning.VistaPlayableHomePlanError, "NAMESPACE_INVALID"):
            planning.build_composition_spec(invalid)

        invalid = build_plan()
        invalid["entities"][0]["collision_policy"] = "world_static"
        with self.assertRaisesRegex(planning.VistaPlayableHomePlanError, "ROLE_INVALID"):
            planning.build_composition_spec(invalid)

        invalid = build_plan()
        invalid["execute_python_script"] = "bad"
        with self.assertRaisesRegex(planning.VistaPlayableHomePlanError, "SHAPE_INVALID"):
            planning.build_composition_spec(invalid)

    def test_builtin_paths_are_fixed_and_nonbuiltin_paths_are_derived(self) -> None:
        plan = build_plan()
        namespace = plan["unreal"]["content_namespace"]
        self.assertEqual(
            derived_asset_path(namespace, plan["runtime_profile"]["pawn"]),
            BUILTIN_URI_ALLOWLIST["builtin://vista/playable-home/pawn"]["object_path"],
        )
        generated = dict(plan["assets"][0])
        generated.update({
            "asset_id": "asset.hero.coffee-cup",
            "source_kind": "bundle",
            "uri": "bundle://vista/home/coffee-cup.glb",
        })
        self.assertEqual(
            derived_asset_path(namespace, generated),
            namespace + "/Assets/asset_hero_coffee_cup/asset_hero_coffee_cup.asset_hero_coffee_cup",
        )

    def test_execution_manifest_has_no_caller_ue_path_or_class(self) -> None:
        plan = build_plan()
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "attempt-r1"
            root.mkdir()
            plan_path = root / "build-plan.json"
            plan_path.write_bytes(planning.canonical_json(plan))
            project = root / "VistaHome.uproject"
            project.write_text('{"FileVersion":3}\n', encoding="utf-8")
            bindings = [{
                "asset_id": item["asset_id"],
                "source_file": None,
                "source_file_sha256": None,
                "source_binding_digest": item["source_digest"],
            } for item in plan["assets"]]
            result = contract.build_execution_manifest(
                build_plan_path=plan_path,
                build_plan=plan,
                project_file=project,
                attempt_root=root,
                artifact_bindings=bindings,
                import_receipt=root / "import-receipt.json",
                scene_receipt=root / "scene-receipt.json",
            )
            self.assertEqual(result.value["schema_version"], contract.EXECUTION_SCHEMA)
            self.assertNotIn("ue_object_path", json.dumps(result.value["artifact_bindings"]))
            self.assertNotIn("expected_class", json.dumps(result.value["artifact_bindings"]))
            self.assertFalse(result.value["policy"]["replace_existing"])


class PlayableHomeSourceContractTests(unittest.TestCase):
    def test_character_capsules_match_navigation_and_doorway_contract(self) -> None:
        house = json.loads(
            (ROOT / "world_packs/vista_playable_home_r1/house.json").read_text(
                encoding="utf-8"
            )
        )
        navigation_radius_cm = (
            house["runtime_profile"]["navigation_agent"]["radius_m"] * 100.0
        )
        doorway_widths_cm = [
            portal["clearance"]["width_m"] * 100.0
            for portal in house["portals"]
        ]
        narrowest_doorway_cm = min(doorway_widths_cm)
        total_lateral_margin_cm = narrowest_doorway_cm - 2.0 * navigation_radius_cm

        self.assertEqual(navigation_radius_cm, 34.0)
        self.assertEqual(narrowest_doorway_cm, 100.0)
        self.assertEqual(total_lateral_margin_cm, 32.0)

        source_root = (
            ROOT
            / "unreal_plugins/VistaPlayableHome/Source/VistaPlayableHome/Private"
        )
        sources = {
            "player": (
                source_root / "VistaPlayableHomeCharacter.cpp"
            ).read_text(encoding="utf-8"),
            "npc": (
                source_root / "VistaHomeNpcCharacter.cpp"
            ).read_text(encoding="utf-8"),
        }
        capsule_pattern = re.compile(
            r"InitCapsuleSize\(\s*([0-9.]+)f,\s*([0-9.]+)f\s*\)"
        )
        for role, source in sources.items():
            with self.subTest(role=role):
                match = capsule_pattern.search(source)
                self.assertIsNotNone(match)
                radius_cm, half_height_cm = (
                    float(value) for value in match.groups()
                )
                self.assertEqual(radius_cm, navigation_radius_cm)
                self.assertEqual(half_height_cm, 96.0)
                self.assertIn("retains 32 cm of total lateral clearance", source)
                self.assertIn(
                    "GetMesh()->SetRelativeLocation(FVector(0.0f, 0.0f, -96.0f))",
                    source,
                )

    def test_glb_core_texture_counter_requires_embedded_png_or_jpeg(self) -> None:
        counter = commandlet_glb_texture_counter()
        png_payload = b"synthetic-png"
        jpeg_payload = b"synthetic-jpeg"
        payload = png_payload + jpeg_payload
        graph = {
            "asset": {"version": "2.0"},
            "buffers": [{"byteLength": len(payload)}],
            "bufferViews": [
                {"buffer": 0, "byteOffset": 0, "byteLength": len(png_payload)},
                {"buffer": 0, "byteOffset": len(png_payload), "byteLength": len(jpeg_payload)},
            ],
            "images": [
                {"bufferView": 0, "mimeType": "image/png"},
                {"bufferView": 1, "mimeType": "image/jpeg"},
            ],
            "textures": [{"source": 0}, {"source": 1}],
        }
        basis_only = copy.deepcopy(graph)
        basis_only["textures"] = [{"extensions": {"KHR_texture_basisu": {"source": 0}}}]
        invalid = copy.deepcopy(graph)
        invalid["images"][0].pop("bufferView")
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            valid_path = root / "valid.glb"
            valid_path.write_bytes(glb_bytes(graph, payload))
            self.assertEqual(counter(str(valid_path)), 2)

            basis_path = root / "basis-only.glb"
            basis_path.write_bytes(glb_bytes(basis_only, payload))
            self.assertEqual(counter(str(basis_path)), 0)

            invalid_path = root / "invalid.glb"
            invalid_path.write_bytes(glb_bytes(invalid, payload))
            with self.assertRaisesRegex(RuntimeError, "not embedded"):
                counter(str(invalid_path))

    def test_material_texture_inspector_resolves_base_and_instance_override(self) -> None:
        class Texture2D:
            def __init__(self, path):
                self.path = path

            def get_path_name(self):
                return self.path

        class Material:
            def __init__(self, textures):
                self.textures = textures

            def get_base_material(self):
                return self

        class MaterialInstanceConstant:
            def __init__(self, base, override):
                self.base = base
                self.override = override

            def get_base_material(self):
                return self.base

            def get_texture_parameter_value(self, _name):
                return self.override

            def get_editor_property(self, _name):
                raise AttributeError

        class MaterialEditingLibrary:
            @staticmethod
            def get_used_textures(material):
                return material.textures

            @staticmethod
            def get_texture_parameter_names(_material):
                return ["BaseColor"]

        class FakeUnreal:
            pass

        FakeUnreal.Texture2D = Texture2D
        FakeUnreal.Material = Material
        FakeUnreal.MaterialInstanceConstant = MaterialInstanceConstant
        FakeUnreal.MaterialEditingLibrary = MaterialEditingLibrary
        inspector = commandlet_material_texture_inspector(FakeUnreal)
        default = Texture2D("/Engine/T_Default.T_Default")
        imported = Texture2D("/Game/VISTA/T_BaseColor.T_BaseColor")
        material = Material([default])
        instance = MaterialInstanceConstant(material, imported)

        self.assertEqual(
            inspector(instance),
            ["/Engine/T_Default.T_Default", "/Game/VISTA/T_BaseColor.T_BaseColor"],
        )

    def test_plugin_manifest_and_gameplay_contract_are_complete(self) -> None:
        plugin = ROOT / "unreal_plugins/VistaPlayableHome"
        descriptor = json.loads((plugin / "VistaPlayableHome.uplugin").read_text())
        self.assertEqual(descriptor["Modules"][0]["Type"], "Runtime")
        self.assertIn(
            {"Name": "VistaPlayableHomeEditor", "Type": "Editor", "LoadingPhase": "Default"},
            descriptor["Modules"],
        )
        build = (plugin / "Source/VistaPlayableHome/VistaPlayableHome.Build.cs").read_text()
        for dependency in ("EnhancedInput", "AIModule", "NavigationSystem", "Sockets", "Networking", "Json"):
            self.assertIn(f'"{dependency}"', build)
        source = "\n".join(path.read_text(encoding="utf-8")
                           for path in sorted((plugin / "Source").rglob("*")) if path.suffix in {".h", ".cpp"})
        for token in (
            "AVistaPlayableHomeGameMode", "AVistaPlayableHomeCharacter",
            "UVistaInteractionComponent", "IVistaInteractable", "AVistaPickupActor",
            "AVistaDoorActor", "AVistaContainerActor", "AVistaStatefulApplianceActor",
            "AVistaHomeNpcController", "UVistaEventSubsystem",
            "UVistaPlayableHomeRuntimeSubsystem", "AVistaPlayableHomeHUD",
        ):
            self.assertIn(token, source)
        for action in ("NavigateTo", "LookAt", "PickUp", "Place", "OpenDoor",
                       "CloseDoor", "Sit", "Wait", "Speak"):
            self.assertIn(action, source)
        self.assertIn("DOOR_SWEEP_OBSTRUCTED", source)
        self.assertIn("CommitCommandGeneration", source)
        self.assertIn("SESSION_GENERATION_MISMATCH", source)
        self.assertIn("SKM_Manny.SKM_Manny", source)
        editor_build = (
            plugin / "Source/VistaPlayableHomeEditor/VistaPlayableHomeEditor.Build.cs"
        ).read_text()
        for dependency in ("AssetTools", "Json", "MaterialEditor"):
            self.assertIn(f'"{dependency}"', editor_build)
        for token in (
            "UVistaPlayableHomeNaniteLibrary",
            "FinalizeNanitePolicies",
            "DuplicateAsset",
            "SetParentEditorOnly",
            "SetMaterialUsage",
            "HasMaterialUsage",
            "SetNaniteSettings",
            "SavePackage",
            "simworld.vista.playable-home-native-nanite/v1",
        ):
            self.assertIn(token, source)
        self.assertNotIn("FPlatformMisc::GetSHA256Signature", source)
        self.assertIn("0x428a2f98U", source)
        self.assertIn('TEXT("%08x%08x")', source)
        self.assertIn("ABP_Manny", source)
        self.assertIn("ConfigureJambPivot", source)
        self.assertIn("GetBoundingBox().Min.X", source)
        self.assertIn("DoorwayLink->SetEnabled(bOpen)", source)
        self.assertIn(
            "DoorwayLink->AddNavigationObstacle(",
            source,
        )
        self.assertIn("FVector(55.0f, 20.0f, 100.0f)", source)
        self.assertIn("DoorMesh->SetCanEverAffectNavigation(false)", source)
        self.assertNotIn("CreateDefaultSubobject<UNavModifierComponent>", source)
        self.assertIn("DoorwayLink->SetMoveReachedLink", source)
        self.assertIn("FinishUsingCustomLink", source)
        self.assertIn("TraversalDestination", source)
        self.assertIn("ETeleportType::TeleportPhysics", source)
        self.assertIn("ECollisionEnabled::NoCollision", source)
        self.assertIn("UpdateActorInNavOctree", source)
        self.assertIn("OverlapMultiByObjectType", source)
        self.assertIn("SweepSteps = 24", source)
        self.assertIn("ConfigurePatrol", source)
        self.assertIn("PatrolTargetSemanticIds", source)
        self.assertIn("OnMoveCompleted", source)
        self.assertIn("NAVIGATION_GOAL_NOT_REACHED", source)
        self.assertIn("BASELINE_CARRIER_NOT_FOUND", source)
        self.assertIn("Snapshot every resettable semantic actor", source)
        self.assertIn('TEXT("VistaSemanticId=")', source)
        self.assertIn('RuntimeStateValues.Find(TEXT("visible"))', source)
        self.assertIn("SessionGeneration = 0", source)
        commandlet = (ROOT / "tools/ue/vista_playable_home/compose_home_commandlet.py").read_text()
        self.assertIn("force_no_precomputed_lighting", commandlet)
        self.assertIn("unreal.ComponentMobility.MOVABLE", commandlet)
        self.assertIn("unreal.PostProcessVolume", commandlet)
        self.assertIn("unreal.AutoExposureMethod.AEM_MANUAL", commandlet)
        self.assertIn('"dynamic_lighting_verified"', commandlet)
        self.assertIn('"deterministic_exposure_verified"', commandlet)
        self.assertIn('"input_mappings_verified"', commandlet)

    def test_loopback_transport_is_fixed_and_bounded(self) -> None:
        source = (ROOT / "unreal_plugins/VistaPlayableHome/Source/VistaPlayableHome/Private/VistaWorldTcpAdapter.cpp").read_text()
        module = (ROOT / "unreal_plugins/VistaPlayableHome/Source/VistaPlayableHome/Private/VistaPlayableHomeModule.cpp").read_text()
        self.assertIn("FIPv4Address::InternalLoopback", source)
        self.assertIn("MaxRequestBytes = 64 * 1024", source)
        self.assertIn("DispatchTimeoutMilliseconds", source)
        self.assertIn('TEXT("vista_world_action")', source)
        self.assertIn('Operation == TEXT("status")', source)
        self.assertIn('Operation == TEXT("health")', source)
        self.assertIn('TEXT("world_revision")', source)
        self.assertIn('TEXT("event_status")', source)
        self.assertIn('TEXT("active_event")', source)
        self.assertIn("FJsonValueNull", source)
        self.assertIn("Rotation.Roll", source)
        self.assertIn("Rotation.Pitch", source)
        self.assertIn("Rotation.Yaw", source)
        self.assertLess(source.index("Rotation.Roll"), source.index("Rotation.Pitch"))
        self.assertLess(source.index("Rotation.Pitch"), source.index("Rotation.Yaw"))
        self.assertIn("Runtime->GetStatus", source)
        self.assertIn("bIncludeAuthoritativeStatus", source)
        self.assertIn("Runtime->GetStatus(FName(*CommandId)), true", source)
        self.assertIn("ExactKeys", source)
        self.assertIn("IsAsciiAlnum", source)
        self.assertIn("Number > 300.0", source)
        self.assertIn("Command.PlacementAnchorSemanticId", source)
        self.assertIn("AsyncTask(ENamedThreads::GameThread", source)
        self.assertIn('TEXT("VistaWorldPort=")', module)
        self.assertNotIn("execute_python_script", source)
        self.assertNotIn("ConsoleCommand", source)
        self.assertNotIn("LoadObject", source)

    def test_commandlets_compile_and_keep_fresh_revision_guards(self) -> None:
        directory = ROOT / "tools/ue/vista_playable_home"
        import_path = directory / "import_assets_commandlet.py"
        compose_path = directory / "compose_home_commandlet.py"
        for path in (import_path, compose_path):
            py_compile.compile(str(path), doraise=True)
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("execute_python_script", source)
            self.assertNotIn("import socket", source)
            self.assertIn("unreal.log(marker)", source)
            self.assertIn("print(marker, flush=True)", source)
            self.assertIn("write_exclusive_receipt(", source)
            self.assertIn('execution["attempt_root"]', source)
        common_source = (directory / "commandlet_common.py").read_text(encoding="utf-8")
        self.assertIn('{"import", "compose", "common"}', common_source)
        self.assertIn("sha256_file(__file__)", common_source)
        import_source = import_path.read_text(encoding="utf-8")
        self.assertIn("InterchangeManager.get_interchange_manager_scripted", import_source)
        self.assertIn("InterchangeManager.create_source_data", import_source)
        self.assertIn("ImportAssetParameters", import_source)
        self.assertIn('parameters.set_editor_property("replace_existing", False)', import_source)
        self.assertIn("EditorAssetLibrary.rename_asset", import_source)
        self.assertIn("declared_core_texture_count", import_source)
        self.assertIn("MaterialEditingLibrary", import_source)
        self.assertIn("get_used_textures", import_source)
        self.assertIn("get_texture_parameter_names", import_source)
        self.assertIn("get_texture_parameter_value", import_source)
        self.assertIn("returned_texture2d_paths", import_source)
        self.assertIn("material_texture2d_paths", import_source)
        self.assertIn("core_textures_imported_and_used", import_source)
        self.assertIn("NANITE_POLICY_RESULT_SCHEMA", import_source)
        self.assertIn("VistaPlayableHomeNaniteLibrary", import_source)
        self.assertIn("finalize_nanite_policies", import_source)
        self.assertIn("finalize_nanite_policies(namespace, imported)", import_source)
        self.assertNotIn("private_nanite_base_material", import_source)
        self.assertNotIn("set_material_usage", import_source)
        self.assertIn('and item["inspection"]["nanite_enabled"] is True', import_source)
        self.assertIn("nanite_material_policy_verified", import_source)
        self.assertNotIn("AssetImportTask", import_source)
        self.assertNotIn(".import_asset_tasks(", import_source)
        self.assertIn("derived_asset_path(namespace, asset)", import_source)
        self.assertIn("fresh revision namespace", import_source)
        self.assertIn("quarantined", import_source)
        self.assertIn("CTF_USE_COMPLEX_AS_SIMPLE", import_source)
        self.assertIn("room_shell", import_source)
        compose_source = compose_path.read_text(encoding="utf-8")
        self.assertIn(
            "unreal.Rotator(pitch=values[1], yaw=values[2], roll=values[0])",
            compose_source,
        )
        self.assertIn(
            "unreal.Rotator(pitch=-35.0, yaw=-45.0, roll=0.0)",
            compose_source,
        )
        self.assertIn("navigation_system.on_navigation_bounds_updated(nav)", compose_source)
        self.assertNotIn("NavigationSystemV1.build_navigation", compose_source)
        self.assertIn('"stage": stage', compose_source)
        self.assertIn("verify_legacy_input_mappings", compose_source)
        self.assertIn("LEGACY_AXIS_MAPPINGS.issubset", compose_source)
        self.assertIn("LEGACY_ACTION_MAPPINGS.issubset", compose_source)
        self.assertIn('get_editor_property("key_name")', compose_source)
        self.assertNotIn("str(item.key)", compose_source)
        self.assertNotIn("settings.save_key_mappings()", compose_source)
        self.assertNotIn("settings.save_config()", compose_source)
        self.assertIn('"phase": "configure_game_mode_input"', compose_source)
        self.assertNotIn(
            "unreal.Rotator(pitch=values[0], yaw=values[1], roll=values[2])",
            compose_source,
        )
        self.assertIn("level_subsystem.new_level(map_path)", compose_source)
        self.assertIn("level_subsystem.load_level(map_path)", compose_source)
        self.assertIn("VistaSemanticId=", (directory / "planning.py").read_text())
        self.assertIn("unreal.NavMeshBoundsVolume", compose_source)
        self.assertIn("VISTA_PlayerStart", compose_source)
        self.assertIn("default_game_mode", compose_source)
        self.assertIn('event_plan["public_goals"]', compose_source)
        self.assertIn('goal["description"]', compose_source)
        for key in ("MoveForward", "MoveRight", "MouseX", "MouseY", "SpaceBar",
                    "LeftShift", "Crouch", "Interact", "Drop"):
            self.assertIn(key, compose_source)
        self.assertIn("partial_saved_quarantined", compose_source)
        self.assertIn("unreal.PointLight", compose_source)
        self.assertIn("patrol_target_semantic_ids", compose_source)
        self.assertIn('"generate_overlap_events"', compose_source)
        self.assertNotIn("set_generate_overlap_events(", compose_source)


if __name__ == "__main__":
    unittest.main()
