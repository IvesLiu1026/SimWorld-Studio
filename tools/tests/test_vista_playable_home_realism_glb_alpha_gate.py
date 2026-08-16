from __future__ import annotations

import copy
import hashlib
import json
import re
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
    EXTERNAL_MATERIAL_IDENTITY_PROPERTY,
    EXTERNAL_MATERIAL_SEMANTICS_PROPERTY,
    EXTERNAL_MATERIAL_SOURCE_DIGEST_PROPERTY,
    EXTERNAL_MATERIAL_SOURCE_PROPERTY,
    EXTERNAL_MODEL_MATERIAL_CONTRACT_SCHEMA,
    EXTERNAL_SOURCE_SELECTION_POLICIES,
    EXTERNAL_STATICIZATION_DEPSGRAPH_MODE,
    EXTERNAL_STATICIZATION_FRAME,
    EXTERNAL_STATICIZATION_POLICY,
    EXTERNAL_STATICIZATION_SCHEMA,
    EXTERNAL_STATICIZATION_STRIPPED_STATE,
    _ExternalSourceMaterialRegistry,
    _configure_external_material_alpha_contract,
    _external_source_selection_policy_for_identity,
    _runtime_json_sha256,
    _staticization_output_digest_payload,
    _validate_masked_alpha_graph,
    external_staticization_ledger,
    external_material_identity_sha256,
    external_material_alpha_policy,
    external_material_name,
    external_material_name_prefix,
    external_source_material_registry_sha256,
    validate_external_staticization_ledger,
)
from tools.blender.vista_playable_home_realism.export import normalized_manifest
from tools.blender.vista_playable_home_realism.inspect import (
    GLB_JSON_CHUNK,
    GLB_MAGIC,
    _classified_manifest_material_inventory,
    _validate_external_material_alpha_contract,
    _validated_external_model_material_inventory,
    inspect_glb,
    inspect_output,
)
from tools.blender.vista_playable_home_realism.materials import (
    PROJECT_MATERIAL_ID_PROPERTY,
    PROJECT_MATERIAL_PBR_SEMANTICS,
    PROJECT_MATERIAL_RECEIPT_PROPERTY,
    PROJECT_MATERIAL_SEMANTICS_PROPERTY,
    material_plan_manifest,
    project_material_export_name,
)


STOVE_SOURCE_ID = "visual.hero.kitchen_stove"
STOVE_SOURCE_DIGEST = "c" * 64
STOVE_SEMANTICS = ["base_color", "metalness", "normal", "opacity", "roughness"]


def _material_identity(
    *,
    ordinal: int = 0,
    source_material_name: str = "surface",
    semantics: list[str] | None = None,
) -> str:
    return external_material_identity_sha256(
        STOVE_SOURCE_ID,
        STOVE_SOURCE_DIGEST,
        ordinal,
        source_material_name,
        STOVE_SEMANTICS if semantics is None else semantics,
    )


def _material_name(
    *,
    ordinal: int = 0,
    source_material_name: str = "surface",
    semantics: list[str] | None = None,
) -> str:
    return external_material_name(
        STOVE_SOURCE_ID,
        ordinal,
        _material_identity(
            ordinal=ordinal,
            source_material_name=source_material_name,
            semantics=semantics,
        ),
    )


def _project_receipts() -> list[dict]:
    receipts = copy.deepcopy(material_plan_manifest())
    for material_index, receipt in enumerate(receipts):
        for channel_index, channel in enumerate(receipt["channels"].values()):
            channel["sha256"] = f"{material_index * 3 + channel_index + 1:064x}"
    return receipts


def _project_material(material_id: str) -> dict:
    receipt = next(item for item in _project_receipts() if item["material_id"] == material_id)
    result = {
        "name": project_material_export_name(material_id),
        "extras": {
            PROJECT_MATERIAL_ID_PROPERTY: material_id,
            PROJECT_MATERIAL_SEMANTICS_PROPERTY: PROJECT_MATERIAL_PBR_SEMANTICS,
            PROJECT_MATERIAL_RECEIPT_PROPERTY: f"materials/{material_id}",
        },
    }
    if receipt["blend_mode"] == "BLEND":
        result["alphaMode"] = "BLEND"
    return result


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
    identity = _material_identity(
        ordinal=ordinal,
        source_material_name=source_material_name,
        semantics=active_semantics,
    )
    assert material_name == external_material_name(STOVE_SOURCE_ID, ordinal, identity)
    return {
        "schema_version": EXTERNAL_MODEL_MATERIAL_CONTRACT_SCHEMA,
        "material_id": material_name,
        "source_logical_asset_id": STOVE_SOURCE_ID,
        "source_tree_sha256": STOVE_SOURCE_DIGEST,
        "source_material_name": source_material_name,
        "material_ordinal": ordinal,
        "material_identity_sha256": identity,
        "active_texture_semantics": active_semantics,
        "inactive_image_normalizations": [],
        "removed_source_custom_properties": [],
        "alpha_mode": alpha_mode,
        "alpha_cutoff": 0.5 if alpha_mode == "MASK" else None,
        "sanitization_policy": EXTERNAL_MATERIAL_ALPHA_SANITIZATION,
    }


def test_model_material_classifier_ignores_valid_external_texture_contract() -> None:
    model_name = _material_name()
    model_contract = _material_contract(model_name)
    texture_contract = {
        "schema_version": "simworld.vista.playable-home-external-texture-material/v1",
        "material_id": "r2.external.texture.visual_material_white_oa.28b72b7127467c9a",
        "source_logical_asset_id": "visual.material.white_oak_veneer",
        "source_tree_sha256": "d" * 64,
        "material_identity_sha256": "e" * 64,
        "active_texture_semantics": ["base_color", "normal", "roughness"],
        "pbr_source": {"logical_asset_id": "visual.material.white_oak_veneer"},
        "alpha_mode": "OPAQUE",
        "alpha_cutoff": None,
    }
    manifest = {"materials": [texture_contract, model_contract]}
    placements = [
        {
            "realization_mode": "external_blend",
            "source_logical_asset_id": STOVE_SOURCE_ID,
        },
        {
            "realization_mode": "project_authored",
            "source_logical_asset_id": None,
            "material_logical_asset_ids": ["visual.material.white_oak_veneer"],
        },
    ]
    result = _validated_external_model_material_inventory(
        manifest,
        placements,
        [_source_record()],
    )
    assert set(result) == {STOVE_SOURCE_ID}
    assert [item["material_id"] for item in result[STOVE_SOURCE_ID]] == [model_name]


def test_material_classifier_partitions_exact_20_model_2_texture_and_rejects_hybrids() -> None:
    model_template = _material_contract(_material_name())
    model_rows = [
        {
            **model_template,
            "material_id": f"r2.external.fixture_model.{index:02d}",
            "material_identity_sha256": f"{index + 1:064x}",
        }
        for index in range(20)
    ]
    texture_rows = [
        {
            "schema_version": "simworld.vista.playable-home-external-texture-material/v1",
            "material_id": f"r2.external.texture.fixture.{index:02d}",
            "source_logical_asset_id": f"visual.material.fixture_{index}",
            "source_tree_sha256": f"{index + 30:064x}",
            "material_identity_sha256": f"{index + 40:064x}",
            "active_texture_semantics": ["base_color", "normal", "roughness"],
            "pbr_source": {"logical_asset_id": f"visual.material.fixture_{index}"},
            "alpha_mode": "OPAQUE",
            "alpha_cutoff": None,
        }
        for index in range(2)
    ]
    classified = _classified_manifest_material_inventory(
        {"materials": [*model_rows, *texture_rows]}
    )
    assert len(classified["external_model"]) == 20
    assert len(classified["external_texture"]) == 2
    assert classified["project"] == ()

    hybrid = {**texture_rows[0], "source_material_name": "model-spoof"}
    with pytest.raises(ForgeInputError, match="ambiguous or unknown"):
        _classified_manifest_material_inventory({"materials": [hybrid]})
    unknown_opaque = {
        "material_id": "r2.unbound.opaque",
        "alpha_mode": "OPAQUE",
        "alpha_cutoff": None,
    }
    with pytest.raises(ForgeInputError, match="ambiguous or unknown"):
        _classified_manifest_material_inventory({"materials": [unknown_opaque]})


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
        "materials": [*_project_receipts(), _material_contract(material_name)],
        "external_placement": {
            "placements": [placement],
            "asset_sources": [source],
        },
    }
    record = {
        "room_id": "home.r1/room.kitchen_dining",
        "material_count": 2,
        "material_ids": sorted(
            [project_material_export_name("r2.plaster_warm"), material_name]
        ),
        "external_content": {"asset_sources": [copy.deepcopy(source)]},
    }
    return manifest, record


def _external_material(
    material_name: str,
    semantics: list[str],
    *,
    ordinal: int = 0,
    source_material_name: str = "surface",
) -> dict:
    alpha_mode = "MASK" if "opacity" in semantics else "OPAQUE"
    identity = _material_identity(
        ordinal=ordinal,
        source_material_name=source_material_name,
        semantics=semantics,
    )
    assert material_name == external_material_name(STOVE_SOURCE_ID, ordinal, identity)
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
            EXTERNAL_MATERIAL_IDENTITY_PROPERTY: identity,
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
            internal_material or _project_material("r2.plaster_warm"),
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
    identity = _material_identity(source_material_name=material.name)
    _configure_external_material_alpha_contract(
        material,
        asset,
        semantics,
        material_identity_sha256=identity,
    )
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
    first_identity = _material_identity(ordinal=0, source_material_name="Chrome Surface")
    second_identity = _material_identity(ordinal=1, source_material_name="Chrome Surface")
    first = external_material_name(STOVE_SOURCE_ID, 0, first_identity)
    second = external_material_name(STOVE_SOURCE_ID, 1, second_identity)
    assert first == f"{prefix}00.{first_identity[:16]}"
    assert second == f"{prefix}01.{second_identity[:16]}"
    assert first != second
    with pytest.raises(RuntimeError, match="too many materials"):
        external_material_name(STOVE_SOURCE_ID, 100, "a" * 64)


def test_source_material_identity_survives_long_name_truncation() -> None:
    shared = "same prefix " * 16
    first_identity = _material_identity(source_material_name=f"{shared}first")
    second_identity = _material_identity(source_material_name=f"{shared}second")
    first = external_material_name(STOVE_SOURCE_ID, 0, first_identity)
    second = external_material_name(STOVE_SOURCE_ID, 0, second_identity)
    assert len(first) <= 63
    assert len(second) <= 63
    assert first != second


def test_source_namespaces_resist_slug_and_length_collisions() -> None:
    first = "visual.hero.same-name"
    second = "visual.hero.same_name"
    long_source = "visual.hero." + "very_long_source_name_" * 6
    assert len(long_source) <= 160
    assert external_material_name_prefix(first) != external_material_name_prefix(second)
    assert len(external_material_name_prefix("visual.a")) == 44
    assert len(external_material_name_prefix(long_source)) == 44


def test_repeated_source_registry_reuses_only_the_exact_pinned_inventory() -> None:
    contract = _material_contract(_material_name())
    asset = SimpleNamespace(
        logical_asset_id=STOVE_SOURCE_ID,
        source_tree_sha256=STOVE_SOURCE_DIGEST,
    )
    mesh = object()
    registry = _ExternalSourceMaterialRegistry()
    prototype = registry.add(asset, [mesh], (0.5, 0.6, 0.8), [contract])
    assert registry.get(asset) is prototype
    assert prototype.meshes == (mesh,)
    assert prototype.material_registry_sha256 == external_source_material_registry_sha256(
        STOVE_SOURCE_ID,
        STOVE_SOURCE_DIGEST,
        [contract],
    )

    changed_semantics = ["base_color", "normal", "roughness"]
    changed_name = _material_name(semantics=changed_semantics)
    changed_contract = _material_contract(changed_name, semantics=changed_semantics)
    assert external_source_material_registry_sha256(
        STOVE_SOURCE_ID,
        STOVE_SOURCE_DIGEST,
        [changed_contract],
    ) != prototype.material_registry_sha256
    changed_digest_asset = SimpleNamespace(
        logical_asset_id=STOVE_SOURCE_ID,
        source_tree_sha256="d" * 64,
    )
    with pytest.raises(RuntimeError, match="source digest or inventory changed"):
        registry.get(changed_digest_asset)


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
    name = _material_name()
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
    name = _material_name()
    material = _stove_material(name)
    material["alphaCutoff"] = cutoff
    with pytest.raises(ForgeInputError, match="cutoff must be a finite non-negative number"):
        _inspect_and_validate(tmp_path, material)


@pytest.mark.parametrize(
    ("internal_material", "error"),
    [
        (
            {"name": "r2.architecture.wall", "alphaMode": "BLEND"},
            "unmatched.*not receipt-bound",
        ),
        (
            {
                "name": "r2.architecture.wall",
                "extras": {EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY: "BLEND"},
            },
            "spoofs external alpha extras",
        ),
    ],
    ids=("gltf-blend", "declared-blend"),
)
def test_any_internal_v2_bundle_blend_material_is_rejected(
    tmp_path: Path,
    internal_material: dict,
    error: str,
) -> None:
    name = _material_name()
    manifest, record = _manifest_and_record(name)
    record["material_ids"] = sorted([internal_material["name"], name])
    with pytest.raises(ForgeInputError, match=error):
        _inspect_and_validate(
            tmp_path,
            _stove_material(name),
            internal_material=internal_material,
            manifest_and_record=(manifest, record),
        )


def test_receipt_bound_project_window_glass_blend_is_accepted(tmp_path: Path) -> None:
    name = _material_name()
    manifest, record = _manifest_and_record(name)
    glass = _project_material("r2.window_glass")
    record["material_ids"] = sorted([glass["name"], name])
    _inspect_and_validate(
        tmp_path,
        _stove_material(name),
        internal_material=glass,
        manifest_and_record=(manifest, record),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda glass: glass["extras"].pop(PROJECT_MATERIAL_RECEIPT_PROPERTY),
        lambda glass: glass["extras"].__setitem__(
            PROJECT_MATERIAL_ID_PROPERTY, "r2.plaster_warm"
        ),
        lambda glass: glass["extras"].__setitem__(
            EXTERNAL_MATERIAL_SOURCE_PROPERTY, STOVE_SOURCE_ID
        ),
    ],
    ids=("missing-receipt", "wrong-project-id", "mixed-external-spoof"),
)
def test_project_window_glass_requires_exact_receipt_extras(
    tmp_path: Path,
    mutation,
) -> None:
    name = _material_name()
    manifest, record = _manifest_and_record(name)
    glass = _project_material("r2.window_glass")
    mutation(glass)
    record["material_ids"] = sorted([glass["name"], name])
    with pytest.raises(ForgeInputError):
        _inspect_and_validate(
            tmp_path,
            _stove_material(name),
            internal_material=glass,
            manifest_and_record=(manifest, record),
        )


def test_project_wall_cannot_gain_blend_by_tampering_manifest_and_glb(
    tmp_path: Path,
) -> None:
    name = _material_name()
    manifest, record = _manifest_and_record(name)
    wall = _project_material("r2.plaster_warm")
    wall["alphaMode"] = "BLEND"
    receipt = next(
        item for item in manifest["materials"] if item.get("material_id") == "r2.plaster_warm"
    )
    receipt["blend_mode"] = "BLEND"
    with pytest.raises(ForgeInputError, match="differs from the canonical plan"):
        _inspect_and_validate(
            tmp_path,
            _stove_material(name),
            internal_material=wall,
            manifest_and_record=(manifest, record),
        )


def test_unmapped_internal_material_cannot_spoof_external_extras(tmp_path: Path) -> None:
    name = _material_name()
    internal = {
        "name": "r2.architecture.wall",
        "extras": {EXTERNAL_MATERIAL_SOURCE_PROPERTY: STOVE_SOURCE_ID},
    }
    manifest, record = _manifest_and_record(name)
    record["material_ids"] = sorted([internal["name"], name])
    with pytest.raises(ForgeInputError, match="spoofs external alpha extras"):
        _inspect_and_validate(
            tmp_path,
            _stove_material(name),
            internal_material=internal,
            manifest_and_record=(manifest, record),
        )


def _two_material_contract_fixture() -> tuple[dict, dict, dict, dict]:
    first_semantics = ["base_color", "metalness", "normal", "roughness"]
    second_semantics = ["base_color", "normal", "opacity", "roughness"]
    first_name = _material_name(
        ordinal=0,
        source_material_name="body",
        semantics=first_semantics,
    )
    second_name = _material_name(
        ordinal=1,
        source_material_name="vent",
        semantics=second_semantics,
    )
    manifest, record = _manifest_and_record(_material_name())
    manifest["materials"] = [
        *_project_receipts(),
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
    record["material_ids"] = sorted(
        [project_material_export_name("r2.plaster_warm"), first_name, second_name]
    )
    return (
        manifest,
        record,
        _external_material(
            first_name,
            first_semantics,
            ordinal=0,
            source_material_name="body",
        ),
        _external_material(
            second_name,
            second_semantics,
            ordinal=1,
            source_material_name="vent",
        ),
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
    duplicate_name = _material_name(
        ordinal=0,
        source_material_name="vent",
        semantics=json.loads(second["extras"][EXTERNAL_MATERIAL_SEMANTICS_PROPERTY]),
    )
    duplicate_identity = _material_identity(
        ordinal=0,
        source_material_name="vent",
        semantics=json.loads(second["extras"][EXTERNAL_MATERIAL_SEMANTICS_PROPERTY]),
    )
    manifest["materials"][-1]["material_id"] = duplicate_name
    manifest["materials"][-1]["material_ordinal"] = 0
    manifest["materials"][-1]["material_identity_sha256"] = duplicate_identity
    record["material_ids"] = sorted(
        [project_material_export_name("r2.plaster_warm"), first["name"], duplicate_name]
    )
    second["name"] = duplicate_name
    second["extras"][EXTERNAL_MATERIAL_IDENTITY_PROPERTY] = duplicate_identity
    with pytest.raises(ForgeInputError, match="material registry differs"):
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


def test_multiple_rooms_and_sources_close_each_bundle_material_inventory(
    tmp_path: Path,
) -> None:
    coffee_id = "visual.hero.living_coffee_table"
    coffee_digest = "d" * 64
    coffee_semantics = ["base_color", "normal", "roughness"]
    coffee_identity = external_material_identity_sha256(
        coffee_id,
        coffee_digest,
        0,
        "table surface",
        coffee_semantics,
    )
    coffee_name = external_material_name(coffee_id, 0, coffee_identity)
    coffee_source = {
        "logical_asset_id": coffee_id,
        "asset_id": "modern_coffee_table_01",
        "asset_type": "model",
        "resolution": "4k",
        "provider_files_hash": "b" * 40,
        "source_tree_sha256": coffee_digest,
        "files": [
            {
                "relative_path": f"coffee_{semantic}.png",
                "size_bytes": 1,
                "sha256": f"{index + 20:064x}",
                "texture_semantics": [semantic],
                "dimensions_px": [4096, 4096],
            }
            for index, semantic in enumerate(coffee_semantics)
        ],
    }
    coffee_contract = {
        "schema_version": EXTERNAL_MODEL_MATERIAL_CONTRACT_SCHEMA,
        "material_id": coffee_name,
        "source_logical_asset_id": coffee_id,
        "source_tree_sha256": coffee_digest,
        "source_material_name": "table surface",
        "material_ordinal": 0,
        "material_identity_sha256": coffee_identity,
        "active_texture_semantics": coffee_semantics,
        "inactive_image_normalizations": [],
        "removed_source_custom_properties": [],
        "alpha_mode": "OPAQUE",
        "alpha_cutoff": None,
        "sanitization_policy": EXTERNAL_MATERIAL_ALPHA_SANITIZATION,
    }
    coffee_material = {
        "name": coffee_name,
        "extras": {
            EXTERNAL_MATERIAL_SOURCE_PROPERTY: coffee_id,
            EXTERNAL_MATERIAL_SOURCE_DIGEST_PROPERTY: coffee_digest,
            EXTERNAL_MATERIAL_SEMANTICS_PROPERTY: json.dumps(
                coffee_semantics, separators=(",", ":")
            ),
            EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY: "OPAQUE",
            EXTERNAL_MATERIAL_ALPHA_POLICY_PROPERTY: EXTERNAL_MATERIAL_ALPHA_SANITIZATION,
            EXTERNAL_MATERIAL_IDENTITY_PROPERTY: coffee_identity,
        },
    }

    stove_name = _material_name()
    manifest, kitchen_record = _manifest_and_record(stove_name)
    manifest["materials"].append(coffee_contract)
    manifest["external_placement"]["asset_sources"].append(coffee_source)
    manifest["external_placement"]["placements"].append(
        {
            "placement_id": "hero.living.coffee_table",
            "placement_kind": "semantic_fixed",
            "room_id": "home.r1/room.living_room",
            "room_kind": "living_room",
            "category": "coffee_table",
            "realization_mode": "external_blend",
            "semantic_target_id": "home.r1/room.living_room/entity.coffee_table.01",
            "source_logical_asset_id": coffee_id,
        }
    )
    kitchen_path = tmp_path / "kitchen.glb"
    _write_synthetic_glb(kitchen_path, _stove_material(stove_name))
    kitchen_inspection = inspect_glb(
        kitchen_path,
        include_external_material_alpha=True,
    )
    _validate_external_material_alpha_contract(
        manifest,
        kitchen_record,
        kitchen_inspection,
    )

    living_record = {
        "room_id": "home.r1/room.living_room",
        "material_count": 2,
        "material_ids": sorted(
            [project_material_export_name("r2.plaster_warm"), coffee_name]
        ),
        "external_content": {"asset_sources": [copy.deepcopy(coffee_source)]},
    }
    living_path = tmp_path / "living.glb"
    _write_synthetic_glb(living_path, coffee_material)
    living_inspection = inspect_glb(
        living_path,
        include_external_material_alpha=True,
    )
    _validate_external_material_alpha_contract(
        manifest,
        living_record,
        living_inspection,
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
    name = _material_name()
    material = _stove_material(name)
    mutation(material)
    with pytest.raises(ForgeInputError, match=error):
        _inspect_and_validate(tmp_path, material)


def test_manifest_policy_and_export_extras_are_required(tmp_path: Path) -> None:
    name = _material_name()
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


def _staticization_ledger_fixture() -> dict:
    receipts = []
    for source_id, policy in sorted(EXTERNAL_SOURCE_SELECTION_POLICIES.items()):
        selection = _external_source_selection_policy_for_identity(
            source_id,
            policy.source_tree_sha256,
        )
        inventory = [
            {
                "object_name": object_name,
                "object_type": "MESH",
                "data_name": f"{object_name}.mesh",
                "parent_name": None,
                "parent_type": "OBJECT",
                "hide_render": False,
                "hide_viewport": False,
                # More precision than persistent forge JSON retains.  Receipt
                # hashes must bind the normalized representation exactly.
                "matrix_world": [
                    1,
                    0,
                    0,
                    0.123456789,
                    0,
                    1,
                    0,
                    0,
                    0,
                    0,
                    1,
                    0,
                    0,
                    0,
                    0,
                    1,
                ],
                "source_topology": {
                    "vertices": 3,
                    "edges": 3,
                    "loops": 3,
                    "polygons": 1,
                },
                "material_slots": ["Fixture Material"],
                "modifiers": [],
                "constraints": [],
                "action": None,
            }
            for object_name in selection["selected_object_names"]
        ]
        outputs = []
        slug = re.sub(r"[^a-z0-9]+", "_", source_id.lower()).strip("_")
        for index, object_name in enumerate(selection["selected_object_names"]):
            outputs.append(
                {
                    "source_object_name": object_name,
                    "object_name": f"VISTA_External_{slug}_{index:02d}"[:63],
                    "topology": {
                        "vertices": 3,
                        "edges": 3,
                        "loops": 3,
                        "polygons": 1,
                        "uv_layers": 1,
                    },
                    "bounds_m": {
                        "minimum": [0, 0, 0],
                        "maximum": [1, 1, 1],
                        "dimensions": [1, 1, 1],
                    },
                    "material_ids": ["Fixture Material"],
                    "mesh_sha256": hashlib.sha256(object_name.encode()).hexdigest(),
                    "stripped_state": dict(EXTERNAL_STATICIZATION_STRIPPED_STATE),
                }
            )
        output_bounds = {
            "minimum": [0, 0, 0],
            "maximum": [1, 1, 1],
            "dimensions": [1, 1, 1],
        }
        body = {
            "schema_version": EXTERNAL_STATICIZATION_SCHEMA,
            "source_logical_asset_id": source_id,
            "source_tree_sha256": policy.source_tree_sha256,
            "blender_version": [4, 5, 8],
            "frame": EXTERNAL_STATICIZATION_FRAME,
            "depsgraph_mode": EXTERNAL_STATICIZATION_DEPSGRAPH_MODE,
            "evaluation_policy": EXTERNAL_STATICIZATION_POLICY,
            "selection_policy": selection,
            "input_inventory": inventory,
            "input_inventory_sha256": _runtime_json_sha256(
                {"objects": inventory, "actions": []}
            ),
            "input_actions": [],
            "exclusions": [],
            "output_meshes": outputs,
            "output_bounds_m": output_bounds,
            "output_digest": _runtime_json_sha256(
                _staticization_output_digest_payload(
                    source_id,
                    policy.source_tree_sha256,
                    outputs,
                    output_bounds,
                )
            ),
        }
        receipts.append({**body, "content_digest": _runtime_json_sha256(body)})
    return external_staticization_ledger(receipts)


def test_staticization_digest_survives_persistent_json_normalization() -> None:
    ledger = _staticization_ledger_fixture()
    persisted = json.loads(canonical_json_bytes(ledger))
    assert persisted != ledger
    assert validate_external_staticization_ledger(persisted) == persisted


def _write_empty_v2_output(root: Path) -> tuple[dict, dict]:
    staticization = _staticization_ledger_fixture()
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
        "external_staticization": staticization,
    }
    receipt = {
        "schema_version": "simworld.vista.playable-home-realism-artifacts/v2",
        "artifacts": [],
        "ue_import_bundles": [],
    }
    root.mkdir()
    staticization_path = root / "external-staticization-receipt.json"
    staticization_path.write_bytes(canonical_json_bytes(staticization))
    receipt["artifacts"].append(
        {
            "artifact_id": "receipt.external_staticization",
            "relative_path": "external-staticization-receipt.json",
            "media_type": "application/json",
            "sha256": hashlib.sha256(staticization_path.read_bytes()).hexdigest(),
            "size_bytes": staticization_path.stat().st_size,
        }
    )
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
