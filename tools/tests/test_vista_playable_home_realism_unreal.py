from __future__ import annotations

import copy
import pathlib
import py_compile

import pytest

from tools.ue.vista_playable_home import build_home, capture_review_views, planning
from tools.worlds import playable_home as world_contract


ROOT = pathlib.Path(__file__).resolve().parents[2]
PACK = ROOT / "world_packs/vista_playable_home_r1"
FINISHED_KINDS = {"entry_hall", "living_room", "kitchen_dining"}


def build_plan() -> dict:
    house = world_contract.load_json(PACK / "house.json")
    return world_contract.compile_build_plan(house, world_contract.load_events(PACK / "events"))


def renderer_profile() -> dict:
    return {
        "profile_id": "realistic_interior_r2_high",
        "platform": "linux",
        "rhi": "vulkan",
        "feature_level": "sm6",
        "shading_path": "deferred",
        "dynamic_gi": "lumen",
        "reflections": "lumen",
        "shadow_method": "virtual_shadow_maps",
        "anti_aliasing": "tsr",
        "nanite_policy": "eligible_static_opaque_only",
        "hardware_ray_tracing": False,
        "extended_luminance_range": True,
        "pre_exposure": True,
        "screen_percentage": 100,
        "texture_pool_mb": 8192,
        "scalability": {key: 3 for key in build_home.RENDERER_SCALABILITY_KEYS},
    }


def visual_profile(plan: dict) -> dict:
    rooms = {room["kind"]: room for room in plan["rooms"]}
    finished = [rooms[kind]["room_id"] for kind in sorted(FINISHED_KINDS)]
    compatibility = sorted(
        room["room_id"] for room in plan["rooms"] if room["kind"] not in FINISHED_KINDS
    )
    shots = []
    apertures = []
    practicals = []
    for kind in sorted(FINISHED_KINDS):
        room = rooms[kind]
        room_id = room["room_id"]
        bounds = room["world_bounds_cm"]
        center = [
            (bounds["min_cm"][axis] + bounds["max_cm"][axis]) / 2.0
            for axis in range(3)
        ]
        for purpose, x_fraction in (("overview", 0.15), ("hero", 0.75)):
            eye_x = bounds["min_cm"][0] + (
                bounds["max_cm"][0] - bounds["min_cm"][0]
            ) * x_fraction
            target_x = bounds["min_cm"][0] + (
                bounds["max_cm"][0] - bounds["min_cm"][0]
            ) * (0.75 if purpose == "overview" else 0.25)
            shots.append(
                {
                    "shot_id": f"{kind}.{purpose}",
                    "room_id": room_id,
                    "purpose": purpose,
                    "eye_location_cm": [eye_x, center[1], 170.0],
                    "look_at_target_cm": [target_x, center[1], 100.0],
                    "horizontal_fov_deg": 74.0 if purpose == "overview" else 64.0,
                    "near_field_clearance_cm": 25.0,
                    "allowed_visibility_layers": ["architecture", "hero_props"],
                    "exposure": {
                        "mode": "pinned_physical_camera",
                        "aperture_fstop": 4.0,
                        "shutter_speed_s": 1.0 / 60.0,
                        "iso": 400,
                        "exposure_compensation_ev": 0.0,
                    },
                    "expected_hero_ids": [
                        f"home.r1/hero.{kind}.01",
                        f"home.r1/hero.{kind}.02",
                        f"home.r1/hero.{kind}.03",
                    ],
                    "forbidden_foreground_ids": [f"home.r1/blocker.{kind}.01"],
                }
            )
        apertures.append(
            {
                "aperture_id": f"aperture.{kind}.01",
                "room_id": room_id,
                "visible_geometry_required": True,
            }
        )
        practicals.append(
            {
                "light_id": f"light.{kind}.01",
                "room_id": room_id,
                "type": "rect",
                "location_cm": [center[0], center[1], 250.0],
                "direction": [0.0, 0.0, -1.0],
                "intensity": 1200.0,
                "unit": "lumens",
                "temperature_k": 3500.0,
                "visible_fixture_id": f"home.r1/fixture.{kind}.01",
            }
        )
    return {
        "schema_version": planning.VISUAL_PROFILE_SCHEMA,
        "visual_profile_id": "realistic_interior_r2",
        "house_revision": plan["house"]["revision"],
        "finished_room_ids": finished,
        "compatibility_room_ids": compatibility,
        "renderer_profile": renderer_profile(),
        "lighting_rig": {
            "rig_id": "realistic_interior_r2.neutral_day",
            "profile": "neutral_day",
            "sun": {
                "direction": [0.4, -0.3, -0.8],
                "illuminance_lux": 65000.0,
                "temperature_k": 5600.0,
            },
            "sky": {"source": "dynamic_sky", "sky_intensity": 1.0},
            "apertures": apertures,
            "practical_lights": practicals,
            "gameplay_exposure": {
                "metering_mode": "histogram",
                "min_ev100": 2.0,
                "max_ev100": 12.0,
                "speed_up": 3.0,
                "speed_down": 1.0,
            },
        },
        "review_shots": shots,
        "content_digest": "a" * 64,
    }


def test_look_at_maps_downward_view_to_pitch_with_zero_roll() -> None:
    rotation = planning.look_at_rotation_deg([0.0, 0.0, 180.0], [200.0, 0.0, 80.0])

    assert rotation[0] == 0.0
    assert rotation[1] < 0.0
    assert rotation[2] == 0.0
    assert rotation != [-10.0, 0.0, 0.0]
    with pytest.raises(planning.VistaPlayableHomePlanError, match="LOOK_AT_INVALID"):
        planning.look_at_rotation_deg([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    with pytest.raises(planning.VistaPlayableHomePlanError, match="VECTOR_INVALID"):
        planning.look_at_rotation_deg([float("nan"), 0.0, 0.0], [1.0, 0.0, 0.0])


def test_review_shot_preflight_fails_closed_on_eye_and_clearance() -> None:
    plan = build_plan()
    profile = visual_profile(plan)
    shot = profile["review_shots"][0]
    room = next(room for room in plan["rooms"] if room["room_id"] == shot["room_id"])
    eye = shot["eye_location_cm"]
    far_blocker = {
        "semantic_id": "home.r1/blocker.far",
        "bounds": {
            "min_cm": [eye[0] + 100.0, eye[1] + 100.0, eye[2] + 100.0],
            "max_cm": [eye[0] + 120.0, eye[1] + 120.0, eye[2] + 120.0],
        },
    }
    operation = planning.compile_look_at_review_shot(
        shot,
        room_bounds=room["world_bounds_cm"],
        blocking_bounds=[far_blocker],
    )

    assert operation["transform"]["rotation_deg"][0] == 0.0
    assert operation["preflight"]["eye_containment"] == "preflight_passed"
    assert operation["preflight"]["near_field_clearance"] == "preflight_passed"
    close_blocker = copy.deepcopy(far_blocker)
    close_blocker["semantic_id"] = "home.r1/blocker.close"
    close_blocker["bounds"] = {
        "min_cm": [eye[0] + 10.0, eye[1] - 5.0, eye[2] - 5.0],
        "max_cm": [eye[0] + 20.0, eye[1] + 5.0, eye[2] + 5.0],
    }
    with pytest.raises(planning.VistaPlayableHomePlanError, match="NEAR_FIELD_BLOCKED"):
        planning.compile_look_at_review_shot(
            shot, room_bounds=room["world_bounds_cm"], blocking_bounds=[close_blocker]
        )
    outside = copy.deepcopy(shot)
    outside["eye_location_cm"] = [9999.0, 9999.0, 9999.0]
    with pytest.raises(planning.VistaPlayableHomePlanError, match="OUTSIDE_ALLOWED_BOUNDS"):
        planning.compile_look_at_review_shot(outside, room_bounds=room["world_bounds_cm"])
    caller_euler = copy.deepcopy(shot)
    caller_euler["rotation_deg"] = [-10.0, 0.0, 90.0]
    with pytest.raises(planning.VistaPlayableHomePlanError, match="CALLER_EULER_REFUSED"):
        planning.compile_look_at_review_shot(caller_euler)


def test_r2_composition_is_additive_and_r1_default_is_byte_stable() -> None:
    plan = build_plan()
    r1 = planning.build_composition_spec(plan)
    assert r1.raw == planning.build_composition_spec(copy.deepcopy(plan), None).raw
    expected_r1_ini = "\n".join(
        [
            "[/Script/EngineSettings.GameMapsSettings]",
            f"GameDefaultMap={plan['unreal']['map_path']}",
            f"EditorStartupMap={plan['unreal']['map_path']}",
            "GlobalDefaultGameMode=/Script/VistaPlayableHome.VistaPlayableHomeGameMode",
            "",
            "[/Script/NavigationSystem.RecastNavMesh]",
            "RuntimeGeneration=Dynamic",
            "",
            "[/Script/Engine.RendererSettings]",
            "r.AllowStaticLighting=False",
            "",
        ]
    ).encode()
    assert build_home.default_engine_ini(plan) == expected_r1_ini

    profile = visual_profile(plan)
    r2 = planning.build_composition_spec(plan, profile)
    cameras = [op for op in r2.value["operations"] if op["kind"] == "place_review_camera"]
    assert len(cameras) == 6
    assert all(op["transform"]["rotation_deg"][0] == 0.0 for op in cameras)
    assert not any(op["kind"] == "place_lighting" for op in r2.value["operations"])
    assert sum(op["kind"] == "place_realistic_lighting" for op in r2.value["operations"]) == 1
    assert r2.value["visual_profile_id"] == "realistic_interior_r2"


def test_renderer_config_and_observation_contract_are_explicit() -> None:
    profile = renderer_profile()
    first = build_home.compile_renderer_profile(profile)
    second = build_home.compile_renderer_profile(copy.deepcopy(profile))
    assert first.content_digest == second.content_digest
    assert first.observation_contract["config_is_runtime_proof"] is False
    assert first.observation_contract["status"] == "runtime_observation_required"
    assert "-VulkanTargetedShaderFormats=SF_VULKAN_SM5" in first.linux_target_lines
    assert "+VulkanTargetedShaderFormats=SF_VULKAN_SM6" in first.linux_target_lines
    for line in (
        "r.DynamicGlobalIlluminationMethod=1",
        "r.ReflectionMethod=1",
        "r.Shadow.Virtual.Enable=1",
        "r.AntiAliasingMethod=4",
        "r.Nanite.ProjectEnabled=True",
        "r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange=True",
        "r.UsePreExposure=True",
        "r.Lumen.HardwareRayTracing=0",
    ):
        assert line in first.renderer_lines
    generated_ini = build_home.default_engine_ini(
        build_plan(), {"renderer_profile": profile}
    ).decode()
    assert "[/Script/LinuxTargetPlatform.LinuxTargetSettings]" in generated_ini
    assert "+VulkanTargetedShaderFormats=SF_VULKAN_SM6" in generated_ini
    assert "sg.GlobalIlluminationQuality=3" in generated_ini
    observations = {
        item["name"]: item["expected"]
        for item in first.observation_contract["required_runtime_observations"]
    }
    assert build_home.evaluate_renderer_observations(first, observations)["runtime_proof"] is True
    observations["feature_level"] = "SM5"
    rejected = build_home.evaluate_renderer_observations(first, observations)
    assert rejected["runtime_proof"] is False
    assert rejected["failures"][0]["name"] == "feature_level"

    invalid = copy.deepcopy(profile)
    invalid["feature_level"] = "sm5"
    with pytest.raises(build_home.BuildHomeError, match="feature_level must be sm6"):
        build_home.compile_renderer_profile(invalid)


def test_capture_contract_is_1080p_zero_roll_and_observation_gated() -> None:
    plan = build_plan()
    profile = visual_profile(plan)
    bounds = {room["room_id"]: room["world_bounds_cm"] for room in plan["rooms"]}
    cameras = capture_review_views.compile_realistic_cameras(
        profile, capture_review_views.EXPECTED_MAP_PATH, room_bounds_by_id=bounds
    )
    assert len(cameras) == 6
    assert all((camera["width"], camera["height"]) == (1920, 1080) for camera in cameras)
    assert all(camera["expected_transform"]["rotation_deg"][0] == 0.0 for camera in cameras)
    camera = next(item for item in cameras if item["purpose"] == "overview")
    accepted = capture_review_views.validate_realistic_camera_observation(
        camera,
        {
            "nearest_blocker_id": "home.r1/blocker.far",
            "nearest_blocker_clearance_cm": 100.0,
            "foreground_occlusion_fraction": 0.1,
            "visible_hero_ids": camera["expected_hero_ids"],
            "foreground_semantic_ids": [],
        },
    )
    assert accepted["status"] == "accepted_observation"
    rejected = capture_review_views.validate_realistic_camera_observation(
        camera,
        {
            "nearest_blocker_id": camera["forbidden_foreground_ids"][0],
            "nearest_blocker_clearance_cm": 10.0,
            "foreground_occlusion_fraction": 0.5,
            "visible_hero_ids": [],
            "foreground_semantic_ids": camera["forbidden_foreground_ids"],
        },
    )
    assert rejected["status"] == "rejected_observation"
    assert {item["gate"] for item in rejected["failures"]} == {
        "near_field_clearance",
        "overview_foreground_occlusion",
        "expected_hero_visibility",
        "forbidden_foreground",
    }


def test_modified_unreal_sources_compile_without_launching_unreal() -> None:
    for relative in (
        "tools/ue/vista_playable_home/planning.py",
        "tools/ue/vista_playable_home/build_home.py",
        "tools/ue/vista_playable_home/capture_review_views.py",
        "tools/ue/vista_playable_home/compose_home_commandlet.py",
    ):
        py_compile.compile(str(ROOT / relative), doraise=True)
