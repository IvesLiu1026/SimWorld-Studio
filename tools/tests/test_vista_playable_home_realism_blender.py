from __future__ import annotations

import copy
import json
import struct
import sys
from collections import Counter
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.blender.vista_playable_home_realism import build_forge_plan
from tools.blender.vista_playable_home_realism import build as blender_build
from tools.blender.vista_playable_home_realism.config import (
    DEFAULT_TEXTURE_SIZE_PX,
    ForgeInputError,
    canonical_json_bytes,
    content_digest,
    prepare_output_root,
)
from tools.blender.vista_playable_home_realism.dressing import anchors_clear_exclusions
from tools.blender.vista_playable_home_realism.export import build_quality_claims, normalized_manifest
from tools.blender.vista_playable_home_realism.inspect import (
    GLB_JSON_CHUNK,
    GLB_MAGIC,
    inspect_glb,
    inspect_output,
)
from tools.blender.vista_playable_home_realism.materials import material_plan_manifest


HOUSE_PATH = REPO_ROOT / "world_packs" / "vista_playable_home_r1" / "house.json"


@pytest.fixture()
def house() -> dict:
    return json.loads(HOUSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def profile(house: dict) -> dict:
    room_id = {room["kind"]: room["room_id"] for room in house["rooms"]}
    return {
        "schema_version": "simworld.vista.playable-home-visual-profile/v1",
        "visual_profile_id": "realistic_interior_r2",
        "house_revision": house["revision"],
        "seed": 20260816,
        "finished_room_ids": [
            room_id["entry_hall"],
            room_id["living_room"],
            room_id["kitchen_dining"],
        ],
        "compatibility_room_ids": [
            room_id["bedroom"],
            room_id["office"],
            room_id["bathroom_laundry"],
        ],
        "architecture_profile": {
            "profile_id": "architecture.realistic_interior_r2",
            "units": "meters",
            "source_receipt_id": "source.project_authored.architecture.r2",
            "finished_room_ids": [
                room_id["entry_hall"],
                room_id["living_room"],
                room_id["kitchen_dining"],
            ],
            "features": ["wall_thickness", "trim", "windows", "cabinetry"],
            "wall_thickness_m": 0.18,
            "baseboard_height_m": 0.12,
            "portal_topology_policy": "preserve_r1",
            "collision_policy": "hidden_r1_proxies",
        },
        "dressing_instances": [],
    }


def test_plan_is_deterministic_and_bound_to_sources(house: dict, profile: dict) -> None:
    first = build_forge_plan(copy.deepcopy(house), copy.deepcopy(profile))
    second = build_forge_plan(copy.deepcopy(house), copy.deepcopy(profile))
    assert first == second
    assert first.content_digest == second.content_digest
    assert len(first.content_digest) == 64
    assert first.house_revision == "vista_playable_home_r1"
    assert first.source_house_digest == house["content_digest"]
    assert canonical_json_bytes(first) == canonical_json_bytes(second)


def test_three_room_architecture_has_required_construction_roles(house: dict, profile: dict) -> None:
    plan = build_forge_plan(house, profile)
    assert [room.kind for room in plan.rooms] == ["entry_hall", "living_room", "kitchen_dining"]
    counts = Counter(item.role for item in plan.components)
    assert len(plan.components) >= 170
    assert counts["wall_opaque"] >= 30
    assert counts["opening_reveal"] >= 24
    assert counts["baseboard"] >= 18
    assert counts["floor_finish"] == 3
    assert counts["ceiling_finish"] == 3
    assert counts["window_frame"] >= 8
    assert counts["window_glass"] == 2
    assert counts["floor_transition"] == 2
    assert counts["cabinet_carcass"] == 12
    assert counts["cabinet_front"] == 12
    assert counts["countertop"] == 1
    assert counts["backsplash"] == 1
    assert {item.export_role for item in plan.components} == {
        "architecture_shell",
        "architectural_detail",
        "cabinetry",
    }
    assert all(item.collision_policy == "presentation_no_collision" for item in plan.components)
    assert all(item.semantic_policy == "presentation_only" for item in plan.components)


def test_entry_millwork_is_authored_dense_and_keeps_portals_and_navigation_clear(
    house: dict, profile: dict
) -> None:
    first = build_forge_plan(copy.deepcopy(house), copy.deepcopy(profile))
    second = build_forge_plan(copy.deepcopy(house), copy.deepcopy(profile))
    entry = next(room for room in first.rooms if room.kind == "entry_hall")
    millwork = tuple(
        component
        for component in first.components
        if component.room_id == entry.room_id
        and component.component_id.startswith(f"{entry.room_id}/visual.r2/entry_")
    )
    repeated = tuple(
        component
        for component in second.components
        if component.room_id == entry.room_id
        and component.component_id.startswith(f"{entry.room_id}/visual.r2/entry_")
    )

    assert millwork == repeated
    assert len(millwork) == 29
    assert Counter(component.role for component in millwork) == {
        "entry_boot_ledge": 1,
        "entry_coat_hook": 5,
        "entry_coat_panel": 1,
        "entry_coat_rail": 1,
        "entry_coat_shelf": 1,
        "entry_console_carcass": 1,
        "entry_console_front": 2,
        "entry_console_hardware": 3,
        "entry_console_top": 1,
        "entry_feature_batten": 7,
        "entry_feature_panel": 1,
        "entry_focal_frame": 4,
        "entry_focal_panel": 1,
    }
    assert {component.export_role for component in millwork} == {
        "architectural_detail",
        "cabinetry",
    }
    assert {component.material_id for component in millwork} == {
        "r2.cabinet_sage",
        "r2.cabinet_walnut",
        "r2.counter_quartz",
        "r2.hardware_brass",
        "r2.oak_natural",
        "r2.window_frame",
    }
    assert all(
        component.collision_policy == "presentation_no_collision" for component in millwork
    )
    assert all(component.semantic_policy == "presentation_only" for component in millwork)

    # Every box remains inside the entry shell and leaves the protected
    # x=+/-0.62 m corridor plus a 0.35 m visual/furniture buffer untouched.
    for component in millwork:
        half_extents = tuple(value / 2 for value in component.dimensions_m)
        assert all(
            entry.bounds_min_m[axis] <= component.location_m[axis] - half_extents[axis]
            and component.location_m[axis] + half_extents[axis] <= entry.bounds_max_m[axis]
            for axis in range(3)
        )
        x_min = component.location_m[0] - half_extents[0]
        x_max = component.location_m[0] + half_extents[0]
        assert x_max <= -0.97 or x_min >= 0.97
        # Side-wall portals occupy y=[-2.5,-1.5] and [1.5,2.5].
        y_min = component.location_m[1] - half_extents[1]
        y_max = component.location_m[1] + half_extents[1]
        assert y_min > -1.5
        assert y_max < 1.5


def test_portal_topology_and_exterior_openings_are_preserved(house: dict, profile: dict) -> None:
    plan = build_forge_plan(house, profile)
    portal_ids = {portal["portal_id"] for portal in house["portals"]}
    finished_ids = {room.room_id for room in plan.rooms}
    expected_portal_sides = sum(
        int(portal["from_room_id"] in finished_ids) + int(portal["to_room_id"] in finished_ids)
        for portal in house["portals"]
    )
    portal_openings = [item for item in plan.openings if item.source_id in portal_ids]
    assert len(portal_openings) == expected_portal_sides == 7
    assert all(item.opening_kind == "door" for item in portal_openings)
    assert all(item.width_m == pytest.approx(1.0) for item in portal_openings)
    assert all(item.height_m == pytest.approx(2.1) for item in portal_openings)
    assert len([item for item in plan.openings if item.opening_kind == "window"]) == 2
    exit_opening = next(item for item in plan.openings if "entity.exit_door" in item.source_id)
    assert exit_opening.wall_side == "south"
    assert exit_opening.width_m == pytest.approx(1.1)


def test_material_plan_defaults_to_production_resolution_and_full_pbr_semantics() -> None:
    assert DEFAULT_TEXTURE_SIZE_PX == 512
    materials = material_plan_manifest()
    assert len(materials) >= 12
    for material in materials:
        assert material["shader_class"] == "PrincipledBSDF"
        assert material["texel_density_px_per_m"] >= 768
        assert set(material["channels"]) == {"base_color", "normal", "roughness"}
        assert material["channels"]["base_color"]["color_space"] == "sRGB"
        assert material["channels"]["normal"]["color_space"] == "Non-Color"
        assert material["channels"]["roughness"]["color_space"] == "Non-Color"
        assert material["channels"]["base_color"]["dimensions_px"] == [512, 512]


def test_smoke_texture_override_cannot_be_mislabeled(house: dict, profile: dict) -> None:
    plan = build_forge_plan(house, profile)
    production = normalized_manifest(plan, texture_size_px=512)
    smoke = normalized_manifest(plan, texture_size_px=64)
    assert production["build_quality"] == {
        "accepted_as_r2_visual_evidence": False,
        "eligible_as_architecture_source_evidence": True,
        "production_minimum_texture_size_px": 512,
        "quality_class": "production_candidate",
        "r2_visual_acceptance_authority": "downstream_seal_and_human_review",
        "requires_downstream_asset_and_ue_review": True,
        "texture_size_px": 512,
    }
    assert smoke["build_quality"]["quality_class"] == "smoke_only"
    assert smoke["build_quality"]["accepted_as_r2_visual_evidence"] is False
    assert smoke["build_quality"]["eligible_as_architecture_source_evidence"] is False
    assert smoke["build_quality"]["requires_downstream_asset_and_ue_review"] is True
    for texture_size_px in (512, 1024, 2048):
        claims = build_quality_claims(texture_size_px)
        assert claims["quality_class"] == "production_candidate"
        assert claims["eligible_as_architecture_source_evidence"] is True
        assert claims["accepted_as_r2_visual_evidence"] is False
        assert claims["r2_visual_acceptance_authority"] == "downstream_seal_and_human_review"
    args = blender_build.parse_blender_args(
        [
            "--house",
            str(HOUSE_PATH),
            "--visual-profile",
            "/tmp/profile.json",
            "--output-root",
            "/tmp/output",
            "--texture-size-px",
            "64",
        ]
    )
    assert args.texture_size_px == 64


def test_dressing_anchors_are_stable_purposeful_and_clear(house: dict, profile: dict) -> None:
    first = build_forge_plan(house, profile).dressing
    second = build_forge_plan(house, profile).dressing
    assert first == second
    assert anchors_clear_exclusions(first)
    assert len(first.anchors) == 11
    assert len(first.exclusions) >= 10
    assert {item.exclusion_kind for item in first.exclusions} >= {
        "portal_clearance",
        "pawn_and_npc_corridor",
        "event_interaction_clearance",
    }
    assert all(item.allowed_categories for item in first.anchors)
    assert all(-7.5 <= item.deterministic_yaw_deg <= 7.5 for item in first.anchors)


def test_profile_mismatch_and_nonempty_outputs_fail_closed(house: dict, profile: dict, tmp_path: Path) -> None:
    tampered_house = copy.deepcopy(house)
    tampered_house["rooms"][0]["kind"] = "tampered"
    with pytest.raises(ForgeInputError, match="HouseSpec content_digest mismatch"):
        build_forge_plan(tampered_house, profile)
    mismatched = copy.deepcopy(profile)
    mismatched["house_revision"] = "stale"
    with pytest.raises(ForgeInputError, match="does not match"):
        build_forge_plan(house, mismatched)
    with pytest.raises(ForgeInputError, match="absolute"):
        prepare_output_root(Path("relative"))
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep").write_text("evidence", encoding="utf-8")
    with pytest.raises(ForgeInputError, match="non-empty"):
        prepare_output_root(occupied)


def _tiny_role_glb_bytes() -> bytes:
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [
            {
                "name": "component",
                "mesh": 0,
                "extras": {
                    "vista_component_id": "component.01",
                    "vista_export_role": "architecture_shell",
                },
            }
        ],
        "meshes": [{"primitives": []}],
        "materials": [],
    }
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((4 - len(json_chunk) % 4) % 4)
    total = 12 + 8 + len(json_chunk)
    return (
        struct.pack("<III", GLB_MAGIC, 2, total)
        + struct.pack("<II", len(json_chunk), GLB_JSON_CHUNK)
        + json_chunk
    )


def test_independent_glb_inspector_reads_roles_and_rejects_cameras(tmp_path: Path) -> None:
    path = tmp_path / "tiny.glb"
    path.write_bytes(_tiny_role_glb_bytes())
    result = inspect_glb(path)
    assert result["asset_version"] == "2.0"
    assert result["mesh_count"] == 1
    assert result["camera_count"] == 0
    assert result["component_extra_count"] == 1
    assert result["component_roles"] == ["architecture_shell"]


def test_output_inspection_is_root_independent_and_does_not_leak_absolute_paths(tmp_path: Path) -> None:
    receipts = []
    for name in ("attempt-01", "attempt-02"):
        root = tmp_path / name
        (root / "glb").mkdir(parents=True)
        (root / "glb" / "tiny.glb").write_bytes(_tiny_role_glb_bytes())
        (root / "normalized-manifest.json").write_text(
            json.dumps(
                {
                    "forge_plan_digest": "a" * 64,
                    "build_quality": {"quality_class": "smoke_only"},
                    "components": [{"component_id": f"component.{index:02d}"} for index in range(60)],
                    "role_counts": {
                        "architecture_shell": 20,
                        "architectural_detail": 20,
                        "cabinetry": 20,
                    },
                }
            ),
            encoding="utf-8",
        )
        (root / "artifact-receipt.json").write_text(
            json.dumps(
                {
                    "artifacts": [
                        {
                            "artifact_id": "glb.vertical_slice",
                            "relative_path": "glb/tiny.glb",
                            "media_type": "model/gltf-binary",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        receipts.append(inspect_output(root))

    assert receipts[0] == receipts[1]
    assert content_digest(receipts[0]) == content_digest(receipts[1])
    assert receipts[0]["glbs"][0]["relative_path"] == "glb/tiny.glb"
    assert "relative_or_absolute_path" not in receipts[0]["glbs"][0]
    payload = canonical_json_bytes(receipts[0])
    assert str(tmp_path).encode("utf-8") not in payload
    assert b"attempt-01" not in payload
    assert b"attempt-02" not in payload


def test_build_script_remains_importable_without_bpy() -> None:
    source = Path(blender_build.__file__).read_text(encoding="utf-8")
    assert "import bpy" in source
    assert "export_role_aware_glbs" in source
    assert "save_as_mainfile" in source
    assert "--texture-size-px 64" in source
