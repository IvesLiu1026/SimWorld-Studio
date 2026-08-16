from __future__ import annotations

import copy
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.blender.vista_playable_home_realism.config import (
    ForgeInputError,
    canonical_json_bytes,
)
from tools.blender.vista_playable_home_realism.external_assets import (
    EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY,
    EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY,
    EXTERNAL_MATERIAL_ALPHA_POLICY_PROPERTY,
    EXTERNAL_MATERIAL_ALPHA_SANITIZATION,
    EXTERNAL_MATERIAL_SEMANTICS_PROPERTY,
    EXTERNAL_MATERIAL_SOURCE_DIGEST_PROPERTY,
    EXTERNAL_MATERIAL_SOURCE_PROPERTY,
    EXTERNAL_MODEL_MATERIAL_CONTRACT_SCHEMA,
    _configure_external_material_alpha_contract,
    _validate_masked_alpha_graph,
    external_material_alpha_policy,
    external_material_name,
    external_material_name_prefix,
)
from tools.blender.vista_playable_home_realism.export import normalized_manifest
from tools.blender.vista_playable_home_realism.inspect import (
    GLB_JSON_CHUNK,
    GLB_MAGIC,
    _validate_external_material_alpha_contract,
    inspect_glb,
    inspect_output,
)


STOVE_SOURCE_ID = "visual.hero.kitchen_stove"
STOVE_SOURCE_DIGEST = "c" * 64
STOVE_SEMANTICS = ["base_color", "metalness", "normal", "opacity", "roughness"]


@dataclass(frozen=True)
class _EmptyDressing:
    pass


@dataclass(frozen=True)
class _ExternalPlacementMarker:
    schema_version: str


class _Sockets(list):
    def get(self, name: str):
        return next((item for item in self if item.name == name), None)


class _Socket:
    def __init__(self, node, name: str, default_value: float = 0.0):
        self.node = node
        self.name = name
        self.default_value = default_value
        self.links: list[_Link] = []


class _Node:
    def __init__(self, node_type: str, *, inputs=(), outputs=()):
        self.type = node_type
        self.name = node_type
        self.label = ""
        self.operation = ""
        self.inputs = _Sockets(_Socket(self, name) for name in inputs)
        self.outputs = _Sockets(_Socket(self, name) for name in outputs)


class _Link:
    def __init__(self, from_socket: _Socket, to_socket: _Socket):
        self.from_socket = from_socket
        self.to_socket = to_socket
        self.from_node = from_socket.node
        self.to_node = to_socket.node


class _Links:
    def new(self, from_socket: _Socket, to_socket: _Socket):
        link = _Link(from_socket, to_socket)
        from_socket.links.append(link)
        to_socket.links.append(link)
        return link

    def remove(self, link: _Link):
        link.from_socket.links.remove(link)
        link.to_socket.links.remove(link)


class _Nodes(list):
    def new(self, node_type: str):
        assert node_type == "ShaderNodeMath"
        node = _Node("MATH", inputs=("Value", "Value"), outputs=("Value",))
        self.append(node)
        return node


class _Tree:
    def __init__(self, nodes):
        self.nodes = _Nodes(nodes)
        self.links = _Links()


class _Material(dict):
    def __init__(self, tree: _Tree):
        super().__init__()
        self.name = "Electric Stove Surface"
        self.node_tree = tree
        self.surface_render_method = "BLENDED"


def _fake_mask_material():
    base = _Node("TEX_IMAGE", outputs=("Color", "Alpha"))
    normal = _Node("TEX_IMAGE", outputs=("Color", "Alpha"))
    roughness = _Node("TEX_IMAGE", outputs=("Color", "Alpha"))
    metalness = _Node("TEX_IMAGE", outputs=("Color", "Alpha"))
    opacity = _Node("TEX_IMAGE", outputs=("Color", "Alpha"))
    shader = _Node(
        "BSDF_PRINCIPLED",
        inputs=("Base Color", "Roughness", "Normal", "Metallic", "Alpha"),
        outputs=("BSDF",),
    )
    tree = _Tree([base, normal, roughness, metalness, opacity, shader])
    tree.links.new(opacity.outputs.get("Color"), shader.inputs.get("Alpha"))
    material = _Material(tree)
    semantics = {
        "base_color": base,
        "normal": normal,
        "roughness": roughness,
        "metalness": metalness,
        "opacity": opacity,
    }
    return material, semantics, opacity


def _source_record() -> dict:
    return {
        "logical_asset_id": STOVE_SOURCE_ID,
        "asset_id": "electric_stove",
        "asset_type": "model",
        "resolution": "4k",
        "provider_files_hash": "a" * 40,
        "source_tree_sha256": STOVE_SOURCE_DIGEST,
        "files": [
            {
                "relative_path": f"electric_stove_{semantic}.png",
                "size_bytes": 1,
                "sha256": str(index + 1) * 64,
                "texture_semantics": [semantic],
                "dimensions_px": [4096, 4096],
            }
            for index, semantic in enumerate(STOVE_SEMANTICS)
        ],
    }


def _material_contract(
    material_name: str,
    *,
    ordinal: int = 0,
    source_material_name: str = "surface",
    semantics: list[str] | None = None,
) -> dict:
    active_semantics = list(STOVE_SEMANTICS if semantics is None else semantics)
    alpha_mode = "MASK" if "opacity" in active_semantics else "OPAQUE"
    return {
        "schema_version": EXTERNAL_MODEL_MATERIAL_CONTRACT_SCHEMA,
        "material_id": material_name,
        "source_logical_asset_id": STOVE_SOURCE_ID,
        "source_tree_sha256": STOVE_SOURCE_DIGEST,
        "source_material_name": source_material_name,
        "material_ordinal": ordinal,
        "active_texture_semantics": active_semantics,
        "alpha_mode": alpha_mode,
        "alpha_cutoff": 0.5 if alpha_mode == "MASK" else None,
        "sanitization_policy": EXTERNAL_MATERIAL_ALPHA_SANITIZATION,
    }


def _manifest_and_record(material_name: str) -> tuple[dict, dict]:
    source = _source_record()
    placement = {
        "placement_id": "hero.kitchen.stove",
        "placement_kind": "semantic_fixed",
        "room_id": "home.r1/room.kitchen_dining",
        "room_kind": "kitchen_dining",
        "category": "stove",
        "realization_mode": "external_blend",
        "semantic_target_id": "home.r1/room.kitchen_dining/entity.stove.01",
        "source_logical_asset_id": STOVE_SOURCE_ID,
    }
    manifest = {
        "export_contract": {
            "custom_properties_exported_as_extras": True,
            "external_material_alpha_policy": external_material_alpha_policy(),
        },
        "materials": [_material_contract(material_name)],
        "external_placement": {
            "placements": [placement],
            "asset_sources": [source],
        },
    }
    record = {
        "room_id": "home.r1/room.kitchen_dining",
        "material_count": 2,
        "material_ids": ["r2.architecture.wall", material_name],
        "external_content": {"asset_sources": [copy.deepcopy(source)]},
    }
    return manifest, record


def _external_material(material_name: str, semantics: list[str]) -> dict:
    alpha_mode = "MASK" if "opacity" in semantics else "OPAQUE"
    result = {
        "name": material_name,
        "extras": {
            EXTERNAL_MATERIAL_SOURCE_PROPERTY: STOVE_SOURCE_ID,
            EXTERNAL_MATERIAL_SOURCE_DIGEST_PROPERTY: STOVE_SOURCE_DIGEST,
            EXTERNAL_MATERIAL_SEMANTICS_PROPERTY: json.dumps(
                semantics, separators=(",", ":")
            ),
            EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY: alpha_mode,
            EXTERNAL_MATERIAL_ALPHA_POLICY_PROPERTY: EXTERNAL_MATERIAL_ALPHA_SANITIZATION,
        },
    }
    if alpha_mode == "MASK":
        # Blender 4.5 intentionally omits glTF's default alphaCutoff=0.5.
        # The material extra below persists the explicit sanitization value.
        result["alphaMode"] = "MASK"
        result["extras"][EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY] = 0.5
    return result


def _stove_material(material_name: str) -> dict:
    return _external_material(material_name, STOVE_SEMANTICS)


def _write_synthetic_glb(
    path: Path,
    stove_material: dict,
    *,
    internal_material: dict | None = None,
    additional_materials: list[dict] | None = None,
) -> None:
    document = {
        "asset": {"version": "2.0"},
        "materials": [
            internal_material or {"name": "r2.architecture.wall"},
            stove_material,
            *(additional_materials or []),
        ],
    }
    chunk = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    chunk += b" " * ((4 - len(chunk) % 4) % 4)
    path.write_bytes(
        struct.pack("<III", GLB_MAGIC, 2, 12 + 8 + len(chunk))
        + struct.pack("<II", len(chunk), GLB_JSON_CHUNK)
        + chunk
    )


def _inspect_and_validate(
    tmp_path: Path,
    stove_material: dict,
    *,
    internal_material: dict | None = None,
    additional_materials: list[dict] | None = None,
    manifest_and_record: tuple[dict, dict] | None = None,
):
    path = tmp_path / "kitchen_dining_presentation_bundle.glb"
    _write_synthetic_glb(
        path,
        stove_material,
        internal_material=internal_material,
        additional_materials=additional_materials,
    )
    inspection = inspect_glb(path, include_external_material_alpha=True)
    manifest, record = manifest_and_record or _manifest_and_record(stove_material["name"])
    _validate_external_material_alpha_contract(manifest, record, inspection)
    return inspection


def test_blender_mask_graph_is_constructed_then_revalidated() -> None:
    material, semantics, opacity = _fake_mask_material()
    asset = SimpleNamespace(
        logical_asset_id=STOVE_SOURCE_ID,
        source_tree_sha256=STOVE_SOURCE_DIGEST,
    )
    _configure_external_material_alpha_contract(material, asset, semantics)
    _validate_masked_alpha_graph(material, opacity)
    assert material.surface_render_method == "DITHERED"
    assert material[EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY] == "MASK"
    assert material[EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY] == 0.5
    assert material[EXTERNAL_MATERIAL_SOURCE_PROPERTY] == STOVE_SOURCE_ID
    clip = opacity.outputs.get("Color").links[0].to_node
    assert clip.type == "MATH"
    assert clip.operation == "GREATER_THAN"
    assert clip.inputs[1].default_value == 0.5
    clip.operation = "MULTIPLY"
    with pytest.raises(RuntimeError, match="exact alpha-clip input"):
        _validate_masked_alpha_graph(material, opacity)


def test_source_material_identity_is_unique_and_preserves_full_prefix() -> None:
    prefix = external_material_name_prefix(STOVE_SOURCE_ID)
    first = external_material_name(STOVE_SOURCE_ID, 0, "Chrome Surface")
    second = external_material_name(STOVE_SOURCE_ID, 1, "Chrome Surface")
    assert first == f"{prefix}00.chrome_surface"
    assert second == f"{prefix}01.chrome_surface"
    assert first != second
    with pytest.raises(RuntimeError, match="too many materials"):
        external_material_name(STOVE_SOURCE_ID, 100, "overflow")


def test_v2_manifest_persists_policy_without_changing_v1_export_contract() -> None:
    common = {
        "forge_id": "forge.test",
        "house_revision": "r1",
        "visual_profile_id": "realistic_interior_r2",
        "seed": 7,
        "source_house_digest": "1" * 64,
        "source_profile_digest": "2" * 64,
        "content_digest": "3" * 64,
        "rooms": (),
        "openings": (),
        "components": (),
        "dressing": _EmptyDressing(),
        "material_plan": (),
    }
    v1 = normalized_manifest(
        SimpleNamespace(schema_version="simworld.vista.playable-home-realism-forge/v1", **common),
        texture_size_px=512,
    )
    v2 = normalized_manifest(
        SimpleNamespace(
            schema_version="simworld.vista.playable-home-realism-forge/v2",
            external_placement=_ExternalPlacementMarker(
                "simworld.vista.playable-home-external-placement/v1"
            ),
            **common,
        ),
        texture_size_px=512,
    )
    assert "external_material_alpha_policy" not in v1["export_contract"]
    assert v2["export_contract"]["external_material_alpha_policy"] == (
        external_material_alpha_policy()
    )


def test_synthetic_glb_binds_stove_receipt_semantics_to_mask_default_cutoff(
    tmp_path: Path,
) -> None:
    name = external_material_name(STOVE_SOURCE_ID, 0, "surface")
    inspection = _inspect_and_validate(tmp_path, _stove_material(name))
    observed = inspection["external_material_alpha_contracts"][1]
    assert observed["source_logical_asset_id"] == STOVE_SOURCE_ID
    assert observed["gltf_alpha_mode"] == "MASK"
    assert observed["gltf_alpha_cutoff"] == 0.5
    assert observed["gltf_alpha_cutoff_explicit"] is False
    # Default inspection stays byte-shape compatible with the v1 path.
    path = tmp_path / "kitchen_dining_presentation_bundle.glb"
    assert "external_material_alpha_contracts" not in inspect_glb(path)


@pytest.mark.parametrize(
    "cutoff",
    [None, True, "0.5", -0.01, float("nan"), float("inf"), float("-inf")],
    ids=("null", "bool", "string", "negative", "nan", "positive-infinity", "negative-infinity"),
)
def test_explicit_invalid_mask_cutoff_is_never_treated_as_default(
    tmp_path: Path,
    cutoff,
) -> None:
    name = external_material_name(STOVE_SOURCE_ID, 0, "surface")
    material = _stove_material(name)
    material["alphaCutoff"] = cutoff
    with pytest.raises(ForgeInputError, match="cutoff must be a finite non-negative number"):
        _inspect_and_validate(tmp_path, material)


@pytest.mark.parametrize(
    "internal_material",
    [
        {"name": "r2.architecture.wall", "alphaMode": "BLEND"},
        {
            "name": "r2.architecture.wall",
            "extras": {EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY: "BLEND"},
        },
    ],
    ids=("gltf-blend", "declared-blend"),
)
def test_any_internal_v2_bundle_blend_material_is_rejected(
    tmp_path: Path,
    internal_material: dict,
) -> None:
    name = external_material_name(STOVE_SOURCE_ID, 0, "surface")
    with pytest.raises(ForgeInputError, match="BLEND is forbidden in every v2 bundle material"):
        _inspect_and_validate(
            tmp_path,
            _stove_material(name),
            internal_material=internal_material,
        )


def test_unmapped_internal_material_cannot_spoof_external_extras(tmp_path: Path) -> None:
    name = external_material_name(STOVE_SOURCE_ID, 0, "surface")
    internal = {
        "name": "r2.architecture.wall",
        "extras": {EXTERNAL_MATERIAL_SOURCE_PROPERTY: STOVE_SOURCE_ID},
    }
    with pytest.raises(ForgeInputError, match="spoofs external alpha extras"):
        _inspect_and_validate(
            tmp_path,
            _stove_material(name),
            internal_material=internal,
        )


def _two_material_contract_fixture() -> tuple[dict, dict, dict, dict]:
    first_semantics = ["base_color", "metalness", "normal", "roughness"]
    second_semantics = ["base_color", "normal", "opacity", "roughness"]
    first_name = external_material_name(STOVE_SOURCE_ID, 0, "body")
    second_name = external_material_name(STOVE_SOURCE_ID, 1, "vent")
    manifest, record = _manifest_and_record(first_name)
    manifest["materials"] = [
        _material_contract(
            first_name,
            ordinal=0,
            source_material_name="body",
            semantics=first_semantics,
        ),
        _material_contract(
            second_name,
            ordinal=1,
            source_material_name="vent",
            semantics=second_semantics,
        ),
    ]
    record["material_count"] = 3
    record["material_ids"] = ["r2.architecture.wall", first_name, second_name]
    return (
        manifest,
        record,
        _external_material(first_name, first_semantics),
        _external_material(second_name, second_semantics),
    )


def test_legitimate_multiple_external_materials_are_closed_per_material(
    tmp_path: Path,
) -> None:
    manifest, record, first, second = _two_material_contract_fixture()
    inspection = _inspect_and_validate(
        tmp_path,
        first,
        additional_materials=[second],
        manifest_and_record=(manifest, record),
    )
    assert [
        item["name"] for item in inspection["external_material_alpha_contracts"][1:]
    ] == [first["name"], second["name"]]


def test_duplicate_material_ordinal_is_rejected_even_with_unique_names(
    tmp_path: Path,
) -> None:
    manifest, record, first, second = _two_material_contract_fixture()
    duplicate_name = external_material_name(STOVE_SOURCE_ID, 0, "vent")
    manifest["materials"][1]["material_id"] = duplicate_name
    manifest["materials"][1]["material_ordinal"] = 0
    record["material_ids"][2] = duplicate_name
    second["name"] = duplicate_name
    with pytest.raises(ForgeInputError, match="ordinals are not unique and contiguous"):
        _inspect_and_validate(
            tmp_path,
            first,
            additional_materials=[second],
            manifest_and_record=(manifest, record),
        )


def test_equal_semantic_union_cannot_hide_per_material_reassignment(
    tmp_path: Path,
) -> None:
    manifest, record, first, second = _two_material_contract_fixture()
    first_semantics = ["base_color", "normal", "roughness"]
    second_semantics = ["base_color", "metalness", "normal", "opacity", "roughness"]
    first["extras"][EXTERNAL_MATERIAL_SEMANTICS_PROPERTY] = json.dumps(
        first_semantics, separators=(",", ":")
    )
    second["extras"][EXTERNAL_MATERIAL_SEMANTICS_PROPERTY] = json.dumps(
        second_semantics, separators=(",", ":")
    )
    with pytest.raises(ForgeInputError, match="semantic extras differ from receipt"):
        _inspect_and_validate(
            tmp_path,
            first,
            additional_materials=[second],
            manifest_and_record=(manifest, record),
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda material: material.__setitem__("alphaMode", "BLEND"), "BLEND is forbidden"),
        (lambda material: material.__setitem__("alphaCutoff", 0.25), "MASK cutoff 0.5"),
        (lambda material: material.pop("extras"), "name and source extras differ"),
        (
            lambda material: material["extras"].pop(EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY),
            "closed per-material contract",
        ),
        (
            lambda material: material["extras"].__setitem__(
                EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY, 0.25
            ),
            "MASK cutoff 0.5",
        ),
        (
            lambda material: material["extras"].pop(EXTERNAL_MATERIAL_SEMANTICS_PROPERTY),
            "semantic extras differ",
        ),
        (
            lambda material: material["extras"].pop(EXTERNAL_MATERIAL_ALPHA_POLICY_PROPERTY),
            "sanitization extras",
        ),
    ],
)
def test_synthetic_glb_rejects_blend_wrong_or_missing_alpha_proof(
    tmp_path: Path,
    mutation,
    error: str,
) -> None:
    name = external_material_name(STOVE_SOURCE_ID, 0, "surface")
    material = _stove_material(name)
    mutation(material)
    with pytest.raises(ForgeInputError, match=error):
        _inspect_and_validate(tmp_path, material)


def test_manifest_policy_and_export_extras_are_required(tmp_path: Path) -> None:
    name = external_material_name(STOVE_SOURCE_ID, 0, "surface")
    path = tmp_path / "policy.glb"
    material = _stove_material(name)
    _write_synthetic_glb(path, material)
    inspection = inspect_glb(path, include_external_material_alpha=True)
    manifest, record = _manifest_and_record(name)
    manifest["export_contract"]["custom_properties_exported_as_extras"] = False
    with pytest.raises(ForgeInputError, match="policy is absent or changed"):
        _validate_external_material_alpha_contract(manifest, record, inspection)
    manifest["export_contract"]["custom_properties_exported_as_extras"] = True
    manifest["export_contract"].pop("external_material_alpha_policy")
    with pytest.raises(ForgeInputError, match="policy is absent or changed"):
        _validate_external_material_alpha_contract(manifest, record, inspection)


def _write_empty_v2_output(root: Path) -> tuple[dict, dict]:
    manifest = {
        "schema_version": "simworld.vista.playable-home-realism-forge/v2",
        "forge_id": "forge.test",
        "house_revision": "r1",
        "visual_profile_id": "realistic_interior_r2",
        "seed": 7,
        "source_house_digest": "1" * 64,
        "source_profile_digest": "2" * 64,
        "forge_plan_digest": "3" * 64,
        "build_quality": {},
        "rooms": [],
        "openings": [],
        "components": [{} for _ in range(60)],
        "dressing": {},
        "materials": [],
        "role_counts": {
            "architecture_shell": 1,
            "architectural_detail": 1,
            "cabinetry": 1,
        },
        "room_component_counts": {},
        "export_contract": {
            "coordinate_system": "Blender metric metres, glTF Y-up export",
            "semantic_policy": "presentation_only_preserve_r1_authority",
            "collision_policy": "presentation_no_collision_use_hidden_r1_proxies",
            "cameras_exported": False,
            "lights_exported": False,
            "custom_properties_exported_as_extras": True,
            "external_material_alpha_policy": external_material_alpha_policy(),
        },
        "ue_import_bundles": [],
        "external_placement": {},
    }
    receipt = {
        "schema_version": "simworld.vista.playable-home-realism-artifacts/v2",
        "artifacts": [],
        "ue_import_bundles": [],
    }
    root.mkdir()
    (root / "normalized-manifest.json").write_bytes(canonical_json_bytes(manifest))
    (root / "artifact-receipt.json").write_bytes(canonical_json_bytes(receipt))
    return manifest, receipt


def test_v2_output_cannot_pass_without_three_external_bundle_arrays(tmp_path: Path) -> None:
    root = tmp_path / "no-bundles"
    _write_empty_v2_output(root)
    with pytest.raises(ForgeInputError, match="requires exactly three identical external"):
        inspect_output(root)


@pytest.mark.parametrize(
    ("document", "missing_key", "error"),
    [
        ("manifest", "export_contract", "v2 manifest fields are not closed"),
        ("manifest", "external_placement", "v2 manifest fields are not closed"),
        ("manifest", "ue_import_bundles", "v2 manifest fields are not closed"),
        ("receipt", "artifacts", "v2 receipt fields or schema are not closed"),
        ("receipt", "ue_import_bundles", "v2 receipt fields or schema are not closed"),
    ],
)
def test_v2_evidence_envelope_rejects_omitted_fields(
    tmp_path: Path,
    document: str,
    missing_key: str,
    error: str,
) -> None:
    root = tmp_path / f"missing-{document}-{missing_key}"
    manifest, receipt = _write_empty_v2_output(root)
    target = manifest if document == "manifest" else receipt
    target.pop(missing_key)
    filename = "normalized-manifest.json" if document == "manifest" else "artifact-receipt.json"
    (root / filename).write_bytes(canonical_json_bytes(target))
    with pytest.raises(ForgeInputError, match=error):
        inspect_output(root)


def test_v2_output_never_backfills_missing_alpha_policy(tmp_path: Path) -> None:
    root = tmp_path / "missing-alpha-policy"
    manifest, _ = _write_empty_v2_output(root)
    manifest["export_contract"].pop("external_material_alpha_policy")
    (root / "normalized-manifest.json").write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(ForgeInputError, match="v2 export contract is absent or changed"):
        inspect_output(root)
