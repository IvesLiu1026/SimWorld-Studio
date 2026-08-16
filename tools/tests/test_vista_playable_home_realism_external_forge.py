from __future__ import annotations

import copy
import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest


from tools.blender.vista_playable_home_realism.architecture import (
    build_external_forge_plan,
    build_forge_plan,
)
from tools.blender.vista_playable_home_realism.config import (
    ForgeInputError,
    canonical_json_bytes,
    content_digest,
)
from tools.blender.vista_playable_home_realism.export import (
    normalized_manifest,
    ue_bundle_contract,
)
from tools.blender.vista_playable_home_realism.external_assets import (
    ACQUISITION_RECEIPT_SCHEMA,
    AUTHORED_RECIPE_MATERIAL_IDS,
    AcquiredAsset,
    AcquiredFile,
    ExternalAssetSet,
    _apply_metric_box_uv,
    _canonical_acquisition_json,
    _metric_box_uv,
    _validate_authored_recipe_material_use,
    _validate_normalized_mesh_state,
    _validate_runtime_material_images,
    _validate_static_source,
    load_external_asset_set,
)
from tools.blender.vista_playable_home_realism.inspect import (
    _validate_bundle_glb,
    _validate_bundle_record,
    _validate_external_manifest_binding,
)
from tools.blender.vista_playable_home_realism.placement import (
    NORMALIZATION_POLICY,
    PLACEMENT_SCHEMA_VERSION,
    placement_manifest_document,
)


REPO_ROOT = Path(__file__).parents[2]
HOUSE_PATH = REPO_ROOT / "world_packs" / "vista_playable_home_r1" / "house.json"
PROFILE_PATH = (
    REPO_ROOT
    / "world_packs"
    / "vista_playable_home_r1"
    / "visual_profiles"
    / "realistic_interior_r2.json"
)
PLACEMENT_PATH = (
    REPO_ROOT
    / "world_packs"
    / "vista_playable_home_r1"
    / "visual_profiles"
    / "realistic_interior_r2_external_placement.json"
)


def _sources(
    logical_id: str,
    asset_type: str,
    resolution: str,
    dimensions,
    *,
    asset_id: str | None = None,
    provider_hash: str | None = None,
    source_tree: str | None = None,
    semantics=("base_color", "normal", "roughness"),
) -> AcquiredAsset:
    resolved_asset_id = asset_id or logical_id.rsplit(".", 1)[-1]
    texture_files = tuple(
        AcquiredFile(
            relative_path=f"textures/{logical_id.rsplit('.', 1)[-1]}_{semantic}.png",
            size_bytes=24,
            sha256=hashlib.sha256(f"{logical_id}:{semantic}".encode()).hexdigest(),
            semantic=(semantic,),
            dimensions_px=(4096, 4096) if resolution == "4k" else (2048, 2048),
        )
        for semantic in semantics
    )
    primary_file = AcquiredFile(
        relative_path="fixture.blend",
        size_bytes=24,
        sha256=hashlib.sha256(f"{logical_id}:blend".encode()).hexdigest(),
        semantic=(),
        dimensions_px=None,
    )
    files = (primary_file, *texture_files) if asset_type == "model" else texture_files
    return AcquiredAsset(
        asset_id=resolved_asset_id,
        logical_asset_id=logical_id,
        asset_type=asset_type,
        room_role="fixture",
        resolution=resolution,
        file_variant="blend" if asset_type == "model" else "pbr_jpg",
        provider_files_hash=provider_hash or hashlib.sha1(logical_id.encode()).hexdigest(),
        source_relative_root=f"assets/{resolved_asset_id}",
        primary_relative_path=(
            f"assets/{resolved_asset_id}/fixture.blend"
            if asset_type == "model"
            else f"assets/{resolved_asset_id}/{texture_files[0].relative_path}"
        ),
        source_tree_sha256=source_tree or hashlib.sha256(f"tree:{logical_id}".encode()).hexdigest(),
        catalog_dimensions_m=dimensions,
        files=files,
    )


def _asset_set(tmp_path: Path) -> ExternalAssetSet:
    assets = (
        _sources("visual.material.white_oak_veneer", "texture", "4k", None),
        _sources("visual.material.poly_wool_herringbone", "texture", "4k", None),
        _sources(
            "visual.hero.living_coffee_table",
            "model",
            "4k",
            (1.2018300294876099, 0.6000000834465027, 0.38999998569488525),
            asset_id="modern_coffee_table_01",
            provider_hash="31772c0aab6f930a18de82606146c0a97f08b7d0",
            source_tree="cf5fac22ac00b8725f91ad4565ddaa32dc5f10b213a0938a92de9e2432c1ddfe",
        ),
        _sources(
            "visual.hero.kitchen_stove",
            "model",
            "4k",
            (0.5025948286056519, 0.6476211845874786, 0.8586971759796143),
            asset_id="electric_stove",
            provider_hash="750ee10bdfe78eb6b0b620ef7b5a898e436fb696",
            source_tree="c55acbd188af4674ce5c1c8605f2447c5fb830a05b1650b0d03296b419b38795",
            semantics=("base_color", "metalness", "normal", "opacity", "roughness"),
        ),
        _sources("visual.dressing.entry.rubber_boots", "model", "2k", (0.4, 0.2, 0.4)),
    )
    return ExternalAssetSet(
        root=tmp_path,
        receipt_digest="1" * 64,
        receipt_file_sha256="2" * 64,
        acquisition_manifest_sha256="3" * 64,
        assets=tuple(sorted(assets, key=lambda item: item.logical_asset_id)),
    )


def _row(
    placement_id: str,
    kind: str,
    room_kind: str,
    category: str,
    mode: str,
    *,
    target: str | None = None,
    anchor: str | None = None,
    support: str | None = None,
    source: str | None = None,
    recipe: str | None = None,
    materials=(),
    offset=(0, 0, 0),
    rotation=(0, 0, 0),
    scale=1,
    dimensions=None,
) -> dict:
    return {
        "placement_id": placement_id,
        "placement_kind": kind,
        "room_kind": room_kind,
        "category": category,
        "realization_mode": mode,
        "semantic_target_id": target,
        "anchor_id": anchor,
        "support_placement_id": support,
        "source_logical_asset_id": source,
        "geometry_recipe": recipe,
        "material_logical_asset_ids": list(materials),
        "location_offset_m": list(offset),
        "rotation_offset_deg": list(rotation),
        "uniform_scale": scale,
        "authored_dimensions_m": list(dimensions) if dimensions else None,
    }


def _placement_payload(asset_set: ExternalAssetSet) -> dict:
    rows = [
        _row(
            "hero.entry.shoe_bench", "semantic_fixed", "entry_hall", "shoe_bench",
            "project_authored",
            target="home.r1/room.entry_hall/entity.shoe_bench.01",
            recipe="contemporary_shoe_bench_v1",
            materials=("visual.material.white_oak_veneer", "visual.material.poly_wool_herringbone"),
            dimensions=(1, 0.38, 0.55),
        ),
        _row(
            "hero.living.sofa", "semantic_fixed", "living_room", "sofa",
            "project_authored",
            target="home.r1/room.living_room/entity.sofa.01",
            recipe="contemporary_sofa_v1",
            materials=("visual.material.white_oak_veneer", "visual.material.poly_wool_herringbone"),
            dimensions=(1.6, 0.82, 0.78),
        ),
        _row(
            "hero.living.coffee_table", "semantic_fixed", "living_room", "coffee_table",
            "external_blend",
            target="home.r1/room.living_room/entity.coffee_table.01",
            source="visual.hero.living_coffee_table",
        ),
        _row(
            "hero.kitchen.stove", "semantic_fixed", "kitchen_dining", "stove",
            "external_blend",
            target="home.r1/room.kitchen_dining/entity.stove.01",
            source="visual.hero.kitchen_stove",
        ),
        _row(
            "hero.kitchen.dining_table", "semantic_fixed", "kitchen_dining", "dining_table",
            "project_authored",
            target="home.r1/room.kitchen_dining/entity.dining_table.01",
            recipe="contemporary_dining_table_v1",
            materials=("visual.material.white_oak_veneer",),
            dimensions=(1.6, 0.9, 0.76),
        ),
        _row(
            "dress.entry.rubber_boots", "dressing", "entry_hall", "shoe",
            "external_blend",
            anchor="home.r1/room.entry_hall/dressing_anchor.shoe_drop",
            support="hero.entry.shoe_bench",
            source="visual.dressing.entry.rubber_boots",
        ),
    ]
    payload = {
        "schema_version": PLACEMENT_SCHEMA_VERSION,
        "placement_id": "fixture.external.placement",
        "acquisition": {
            **asset_set.receipt_reference(),
            "receipt_filename": "acquisition-receipt.json",
        },
        "placements": rows,
    }
    payload["content_digest"] = content_digest(payload)
    return payload


def _plan(tmp_path: Path, payload: dict | None = None):
    house = json.loads(HOUSE_PATH.read_text(encoding="utf-8"))
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    assets = _asset_set(tmp_path)
    return build_external_forge_plan(
        house,
        profile,
        assets,
        placement_manifest_document(payload or _placement_payload(assets)),
    )


def _redigest(payload: dict) -> dict:
    payload = copy.deepcopy(payload)
    payload.pop("content_digest", None)
    payload["content_digest"] = content_digest(payload)
    return payload


def test_external_plan_uses_room_local_meters_and_keeps_world_room_offset(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    coffee = next(item for item in plan.external_placement.placements if item.category == "coffee_table")
    living = next(item for item in plan.rooms if item.kind == "living_room")
    assert coffee.location_m == (0, 0.3, 0)
    assert living.location_m == (-4, -2, 0)
    assert coffee.room_local_aabb.min_m == pytest.approx((-0.600915, 0, 0))
    assert coffee.room_local_aabb.max_m == pytest.approx((0.600915, 0.6, 0.39))
    contract = ue_bundle_contract(
        plan,
        living,
        exported_material_names=("r2.external.coffee", "r2.external.wool"),
    )
    assert contract["expected_world_transform_cm"]["location_cm"] == [-400, -200, 0]
    assert contract["material_ids"] == ["r2.external.coffee", "r2.external.wool"]
    assert contract["external_content"]["acquisition_receipt"]["receipt_file_sha256"] == "2" * 64
    assert contract["external_content"]["semantic_target_ids"] == [
        "home.r1/room.living_room/entity.coffee_table.01",
        "home.r1/room.living_room/entity.sofa.01",
    ]


def test_external_bundle_receipts_are_bound_back_to_top_level_plan(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bundles = [
        ue_bundle_contract(
            plan,
            room,
            exported_material_names=(f"r2.{room.kind}.a", f"r2.{room.kind}.b"),
        )
        for room in plan.rooms
    ]
    manifest = normalized_manifest(plan, texture_size_px=512)
    _validate_external_manifest_binding(manifest, bundles)
    tampered = copy.deepcopy(bundles)
    tampered[0]["external_content"]["asset_sources"][0]["files"][0]["sha256"] = "0" * 64
    with pytest.raises(ForgeInputError, match="differs from normalized manifest"):
        _validate_external_manifest_binding(manifest, tampered)


def test_external_plan_rejects_overlap_room_escape_and_movable_target(tmp_path: Path) -> None:
    assets = _asset_set(tmp_path)
    overlap = _placement_payload(assets)
    boots = next(item for item in overlap["placements"] if item["placement_id"] == "dress.entry.rubber_boots")
    boots["support_placement_id"] = None
    boots["location_offset_m"] = [0, 0.2, -0.56]
    with pytest.raises(ForgeInputError, match="AABBs overlap"):
        _plan(tmp_path, _redigest(overlap))

    outside = _placement_payload(assets)
    bench = next(item for item in outside["placements"] if item["placement_id"] == "hero.entry.shoe_bench")
    bench["authored_dimensions_m"] = [3.2, 0.38, 0.55]
    with pytest.raises(ForgeInputError, match="leaves room-local bounds"):
        _plan(tmp_path, _redigest(outside))

    movable = _placement_payload(assets)
    coffee = next(item for item in movable["placements"] if item["placement_id"] == "hero.living.coffee_table")
    coffee["semantic_target_id"] = "home.r1/room.living_room/entity.keys.01"
    with pytest.raises(ForgeInputError, match="movable/forbidden"):
        _plan(tmp_path, _redigest(movable))


@pytest.mark.parametrize(
    "recipe,materials",
    (
        (
            "contemporary_shoe_bench_v1",
            ("visual.material.poly_wool_herringbone", "visual.material.white_oak_veneer"),
        ),
        ("contemporary_sofa_v1", ("visual.material.white_oak_veneer",)),
        (
            "contemporary_dining_table_v1",
            ("visual.material.white_oak_veneer", "visual.material.poly_wool_herringbone"),
        ),
    ),
)
def test_project_authored_recipe_requires_exact_material_logical_ids(
    tmp_path: Path,
    recipe: str,
    materials: tuple[str, ...],
) -> None:
    assets = _asset_set(tmp_path)
    payload = _placement_payload(assets)
    row = next(item for item in payload["placements"] if item["geometry_recipe"] == recipe)
    row["material_logical_asset_ids"] = list(materials)
    with pytest.raises(ForgeInputError, match="project-authored placement source is invalid"):
        _plan(tmp_path, _redigest(payload))


def test_project_authored_recipe_rejects_non_string_recipe_without_type_leak(tmp_path: Path) -> None:
    assets = _asset_set(tmp_path)
    payload = _placement_payload(assets)
    row = next(item for item in payload["placements"] if item["geometry_recipe"] is not None)
    row["geometry_recipe"] = ["contemporary_sofa_v1"]
    with pytest.raises(ForgeInputError, match="project-authored placement source is invalid"):
        _plan(tmp_path, _redigest(payload))


def test_authored_recipe_contract_is_explicit_and_complete() -> None:
    assert AUTHORED_RECIPE_MATERIAL_IDS == {
        "contemporary_shoe_bench_v1": (
            "visual.material.white_oak_veneer",
            "visual.material.poly_wool_herringbone",
        ),
        "contemporary_sofa_v1": (
            "visual.material.white_oak_veneer",
            "visual.material.poly_wool_herringbone",
        ),
        "contemporary_dining_table_v1": ("visual.material.white_oak_veneer",),
    }


def _png_header(size: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", size, size)


def _write_acquisition(root: Path) -> dict:
    asset_root = root / "assets" / "fixture_model"
    texture_root = asset_root / "textures"
    texture_root.mkdir(parents=True)
    rows = []
    file_payloads = [("fixture_model_2k.blend", b"BLENDER-v300")]
    file_payloads += [
        (f"textures/fixture_{name}_2k.png", _png_header(2048))
        for name in ("diff", "nor_gl", "rough")
    ]
    for relative, data in file_payloads:
        path = asset_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rows.append(
            {
                "relative_path": relative,
                "url": f"https://dl.polyhaven.org/file/ph-assets/{Path(relative).name}",
                "size_bytes": len(data),
                "provider_md5": hashlib.md5(data).hexdigest(),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    tree = [{key: row[key] for key in ("relative_path", "size_bytes", "sha256")} for row in rows]
    asset = {
        "asset_id": "fixture_model",
        "logical_asset_id": "visual.dressing.fixture_model",
        "asset_type": "model",
        "room_role": "fixture",
        "resolution": "2k",
        "file_variant": "blend",
        "catalog": {"dimensions": [400, 200, 500]},
        "provider_files_hash": "a" * 40,
        "source_relative_root": "assets/fixture_model",
        "primary_relative_path": "assets/fixture_model/fixture_model_2k.blend",
        "files": rows,
        "source_tree_sha256": hashlib.sha256(_canonical_acquisition_json(tree)).hexdigest(),
    }
    receipt = {
        "schema_version": ACQUISITION_RECEIPT_SCHEMA,
        "provider": "poly_haven",
        "catalog_urls": {},
        "license": {
            "license_id": "CC0-1.0",
            "entitlement_status": "verified",
            "commercial_use": "allowed",
        },
        "manifest_sha256": "b" * 64,
        "acquired_at_utc": "2026-08-16T00:00:00Z",
        "asset_count": 1,
        "total_size_bytes": sum(row["size_bytes"] for row in rows),
        "assets": [asset],
    }
    receipt["receipt_digest"] = hashlib.sha256(_canonical_acquisition_json(receipt)).hexdigest()
    (root / "acquisition-receipt.json").write_bytes(_canonical_acquisition_json(receipt))
    return receipt


def test_acquisition_root_verifies_sha_resolution_and_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    receipt = _write_acquisition(root)
    assets = load_external_asset_set(root)
    assert assets.receipt_digest == receipt["receipt_digest"]
    assert assets.asset("visual.dressing.fixture_model").pbr_semantics == {
        "base_color", "normal", "roughness"
    }
    texture = root / "assets/fixture_model/textures/fixture_diff_2k.png"
    data = bytearray(texture.read_bytes())
    data[-1] ^= 1
    texture.write_bytes(data)
    with pytest.raises(ForgeInputError, match="SHA-256 mismatch"):
        load_external_asset_set(root)

    link = tmp_path / "linked-attempt"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ForgeInputError, match="non-symlink"):
        load_external_asset_set(link)


class _FakeMaterial:
    def __init__(self, name: str, nodes=(), *, source: str | None = None):
        self.name = name
        self.use_nodes = True
        self.animation_data = None
        self.node_tree = SimpleNamespace(nodes=list(nodes), animation_data=None)
        self._properties = {}
        if source is not None:
            self._properties["vista_external_material_source"] = source

    def get(self, key: str, default=None):
        return self._properties.get(key, default)


class _FakeImage:
    def __init__(self, path: Path, dimensions: tuple[int, int], library: object):
        self.source = "FILE"
        self.filepath_raw = str(path)
        self.filepath = "//shared.png"
        self.library = library
        self.packed_file = None
        self.packed_files = ()
        self.size = dimensions

    def reload(self) -> None:
        return None


class _FakeBpyPath:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def abspath(self, raw: str, *, library=None) -> str:
        self.calls.append((raw, library))
        return raw


def _runtime_material_fixture(tmp_path: Path):
    root = tmp_path / "runtime-acquisition"
    source_root = root / "assets" / "fixture_model"
    specs = (
        ("textures/base/shared.png", b"base-color", (17, 19), ("base_color",)),
        ("textures/normal/shared.png", b"normal-map", (23, 29), ("normal",)),
        ("textures/rough/shared.png", b"roughness-map", (31, 37), ("roughness",)),
    )
    files = []
    images = []
    library = object()
    for relative, payload, dimensions, semantics in specs:
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        files.append(
            AcquiredFile(
                relative_path=relative,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                semantic=semantics,
                dimensions_px=dimensions,
            )
        )
        images.append(_FakeImage(path.resolve(), dimensions, library))
    asset = AcquiredAsset(
        asset_id="fixture_model",
        logical_asset_id="visual.dressing.fixture_model",
        asset_type="model",
        room_role="fixture",
        resolution="2k",
        file_variant="blend",
        provider_files_hash="a" * 40,
        source_relative_root="assets/fixture_model",
        primary_relative_path="assets/fixture_model/fixture.blend",
        source_tree_sha256="b" * 64,
        catalog_dimensions_m=(1.0, 1.0, 1.0),
        files=tuple(files),
    )
    asset_set = ExternalAssetSet(
        root=root.resolve(),
        receipt_digest="1" * 64,
        receipt_file_sha256="2" * 64,
        acquisition_manifest_sha256="3" * 64,
        assets=(asset,),
    )
    material = _FakeMaterial(
        "FixtureMaterial",
        nodes=[SimpleNamespace(image=image) for image in images],
    )
    mesh = SimpleNamespace(material_slots=[SimpleNamespace(material=material)])
    path_api = _FakeBpyPath()
    bpy = SimpleNamespace(path=path_api)
    return bpy, path_api, mesh, asset_set, asset, images, library


def test_runtime_material_images_bind_full_paths_and_library_context(tmp_path: Path) -> None:
    bpy, path_api, mesh, asset_set, asset, images, library = _runtime_material_fixture(tmp_path)
    # All three receipt textures intentionally share a basename. Their
    # dimensions differ, so a basename-keyed implementation cannot pass.
    _validate_runtime_material_images(bpy, [mesh], asset_set, asset)
    assert path_api.calls == [
        call
        for image in images
        for call in ((image.filepath_raw, library), (image.filepath_raw, library))
    ]


@pytest.mark.parametrize(
    "mutation,error",
    (
        (lambda image, _tmp: setattr(image, "source", "TILED"), "must be FILE"),
        (lambda image, _tmp: setattr(image, "source", "SEQUENCE"), "must be FILE"),
        (lambda image, _tmp: setattr(image, "source", "GENERATED"), "must be FILE"),
        (lambda image, _tmp: setattr(image, "packed_file", object()), "may not be packed"),
        (lambda image, _tmp: setattr(image, "packed_files", (object(),)), "may not be packed"),
    ),
)
def test_runtime_material_images_reject_non_file_and_packed_sources(
    tmp_path: Path,
    mutation,
    error: str,
) -> None:
    bpy, _path_api, mesh, asset_set, asset, images, _library = _runtime_material_fixture(tmp_path)
    mutation(images[0], tmp_path)
    with pytest.raises(RuntimeError, match=error):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)


def test_runtime_material_images_reject_symlink_outside_path_and_changed_bytes(tmp_path: Path) -> None:
    bpy, _path_api, mesh, asset_set, asset, images, _library = _runtime_material_fixture(tmp_path)
    original = Path(images[0].filepath_raw)
    link = tmp_path / "linked-shared.png"
    link.symlink_to(original)
    images[0].filepath_raw = str(link)
    with pytest.raises(RuntimeError, match="symbolic links"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)

    bpy, _path_api, mesh, asset_set, asset, images, _library = _runtime_material_fixture(
        tmp_path / "outside-case"
    )
    outside = tmp_path / "outside" / "shared.png"
    outside.parent.mkdir()
    outside.write_bytes(Path(images[0].filepath_raw).read_bytes())
    images[0].filepath_raw = str(outside.resolve())
    with pytest.raises(RuntimeError, match="outside its verified receipt"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)

    bpy, _path_api, mesh, asset_set, asset, images, _library = _runtime_material_fixture(
        tmp_path / "changed-case"
    )
    Path(images[0].filepath_raw).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="size differs from receipt|SHA-256 differs from receipt"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)


def _static_mesh_fixture():
    tree = SimpleNamespace(nodes=[], animation_data=None)
    material = SimpleNamespace(name="StaticMaterial", animation_data=None, node_tree=tree)
    data = SimpleNamespace(
        name="StaticMeshData",
        animation_data=None,
        shape_keys=None,
        polygons=[SimpleNamespace(material_index=0)],
    )
    obj = SimpleNamespace(
        name="StaticMesh",
        type="MESH",
        modifiers=[],
        constraints=[],
        instance_type="NONE",
        instance_collection=None,
        rotation_mode="XYZ",
        delta_location=(0.0, 0.0, 0.0),
        delta_rotation_euler=(0.0, 0.0, 0.0),
        delta_scale=(1.0, 1.0, 1.0),
        parent=None,
        animation_data=None,
        data=data,
        material_slots=[SimpleNamespace(material=material)],
    )
    return obj, material, tree


@pytest.mark.parametrize(
    "case,error",
    (
        ("modifier", "contains modifiers"),
        ("constraint", "contains constraints"),
        ("object_driver", "animations or drivers"),
        ("data_driver", "animations or drivers"),
        ("material_driver", "material contains animations or drivers"),
        ("node_driver", "material nodes contain animations or drivers"),
        ("rotation", "rotation mode is not deterministic XYZ"),
        ("delta", "non-identity delta transforms"),
        ("drawable", "unsupported drawable object types"),
        ("instancing", "unsupported instancing"),
    ),
)
def test_static_external_source_rejects_nondeterministic_blender_state(case: str, error: str) -> None:
    obj, material, tree = _static_mesh_fixture()
    if case == "modifier":
        obj.modifiers.append(object())
    elif case == "constraint":
        obj.constraints.append(object())
    elif case == "object_driver":
        obj.animation_data = SimpleNamespace(drivers=[object()])
    elif case == "data_driver":
        obj.data.animation_data = SimpleNamespace(drivers=[object()])
    elif case == "material_driver":
        material.animation_data = SimpleNamespace(drivers=[object()])
    elif case == "node_driver":
        tree.animation_data = SimpleNamespace(drivers=[object()])
    elif case == "rotation":
        obj.rotation_mode = "QUATERNION"
    elif case == "delta":
        obj.delta_scale = (1.0, 2.0, 1.0)
    elif case == "drawable":
        obj.type = "CURVE"
    elif case == "instancing":
        obj.instance_type = "COLLECTION"
    with pytest.raises(RuntimeError, match=error):
        _validate_static_source(SimpleNamespace(), [obj], [])


def test_static_external_source_accepts_only_plain_static_mesh_and_local_helper_parent() -> None:
    obj, _material, _tree = _static_mesh_fixture()
    helper = SimpleNamespace(
        name="Helper",
        type="EMPTY",
        modifiers=[],
        constraints=[],
        instance_type="NONE",
        instance_collection=None,
        rotation_mode="XYZ",
        delta_location=(0.0, 0.0, 0.0),
        delta_rotation_euler=(0.0, 0.0, 0.0),
        delta_scale=(1.0, 1.0, 1.0),
        parent=None,
        animation_data=None,
        data=None,
        material_slots=[],
    )
    obj.parent = helper
    assert _validate_static_source(SimpleNamespace(), [obj, helper], []) == [obj]
    obj.parent = SimpleNamespace()
    with pytest.raises(RuntimeError, match="parent outside"):
        _validate_static_source(SimpleNamespace(), [obj, helper], [])


def test_normalized_external_mesh_state_rejects_parent_and_matrix_residue() -> None:
    identity = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    obj = SimpleNamespace(
        name="NormalizedMesh",
        parent=None,
        rotation_mode="XYZ",
        location=(0.0, 0.0, 0.0),
        rotation_euler=(0.0, 0.0, 0.0),
        scale=(1.0, 1.0, 1.0),
        delta_location=(0.0, 0.0, 0.0),
        delta_rotation_euler=(0.0, 0.0, 0.0),
        delta_scale=(1.0, 1.0, 1.0),
        matrix_basis=identity,
        matrix_local=identity,
        matrix_parent_inverse=identity,
        matrix_world=identity,
    )
    _validate_normalized_mesh_state(obj)
    obj.matrix_local = (
        (1.0, 0.0, 0.0, 0.25),
        *identity[1:],
    )
    with pytest.raises(RuntimeError, match="matrix_local influence"):
        _validate_normalized_mesh_state(obj)
    obj.matrix_local = identity
    obj.parent = object()
    with pytest.raises(RuntimeError, match="parent helper"):
        _validate_normalized_mesh_state(obj)


def test_metric_box_uv_scales_in_metres_and_uses_deterministic_axis_ties() -> None:
    assert _metric_box_uv((0.0, 0.0, 0.0), (0.0, 0.0, 1.0)) == (0.0, 0.0)
    assert _metric_box_uv((2.0, 3.0, 0.5), (0.0, 0.0, 1.0)) == (2.0, 3.0)
    assert _metric_box_uv((2.0, 3.0, 0.5), (0.0, 0.0, 1.0), meters_per_tile=0.5) == (
        4.0,
        6.0,
    )
    assert _metric_box_uv((2.0, 3.0, 0.5), (1.0, 1.0, 0.0)) == (-3.0, 0.5)


def test_metric_box_uv_replaces_primitive_uv_layer_with_metric_coordinates() -> None:
    class UVLayers(list):
        active = None

        def new(self, *, name: str):
            layer = SimpleNamespace(
                name=name,
                data=[SimpleNamespace(uv=None) for _ in range(4)],
                active_render=False,
            )
            self.append(layer)
            return layer

    uv_layers = UVLayers([SimpleNamespace(name="UVMap")])
    mesh = SimpleNamespace(
        uv_layers=uv_layers,
        polygons=[SimpleNamespace(normal=(0.0, 0.0, 1.0), loop_indices=(0, 1, 2, 3))],
        loops=[SimpleNamespace(vertex_index=index) for index in range(4)],
        vertices=[
            SimpleNamespace(co=(0.0, 0.0, 0.0)),
            SimpleNamespace(co=(2.0, 0.0, 0.0)),
            SimpleNamespace(co=(2.0, 3.0, 0.0)),
            SimpleNamespace(co=(0.0, 3.0, 0.0)),
        ],
        update=lambda: None,
    )

    class FakeObject(dict):
        data = mesh

    obj = FakeObject()
    _apply_metric_box_uv(obj)
    assert [item.uv for item in uv_layers[0].data] == [
        (0.0, 0.0),
        (2.0, 0.0),
        (2.0, 3.0),
        (0.0, 3.0),
    ]
    assert uv_layers[0].name == "VISTA_MetricUV"
    assert uv_layers[0].active_render is True
    assert obj["vista_uv_mapping"] == "metric_box_v1"


def test_authored_material_provenance_must_match_actual_mesh_use() -> None:
    oak_id = "visual.material.white_oak_veneer"
    wool_id = "visual.material.poly_wool_herringbone"
    oak = _FakeMaterial("Oak", source=oak_id)
    wool = _FakeMaterial("Wool", source=wool_id)
    mesh = SimpleNamespace(
        name="SofaPart",
        data=SimpleNamespace(polygons=[SimpleNamespace(material_index=0), SimpleNamespace(material_index=1)]),
        material_slots=[SimpleNamespace(material=oak), SimpleNamespace(material=wool)],
    )
    assert _validate_authored_recipe_material_use(
        "contemporary_sofa_v1",
        [mesh],
        {oak_id: oak, wool_id: wool},
    ) == tuple(sorted((oak_id, wool_id)))

    impostor = _FakeMaterial("Impostor", source=oak_id)
    mesh.material_slots[0].material = impostor
    with pytest.raises(RuntimeError, match="wrong datablock"):
        _validate_authored_recipe_material_use(
            "contemporary_sofa_v1",
            [mesh],
            {oak_id: oak, wool_id: wool},
        )


def _external_content() -> dict:
    return {
        "schema_version": PLACEMENT_SCHEMA_VERSION,
        "normalization_policy": NORMALIZATION_POLICY,
        "acquisition_receipt": {
            "provider": "poly_haven",
            "receipt_schema_version": ACQUISITION_RECEIPT_SCHEMA,
            "receipt_digest": "1" * 64,
            "receipt_file_sha256": "2" * 64,
            "acquisition_manifest_sha256": "3" * 64,
        },
        "placement_manifest_sha256": "4" * 64,
        "placement_plan_sha256": "5" * 64,
        "semantic_target_ids": ["home.r1/room.entry_hall/entity.shoe_bench.01"],
        "dressing_ids": ["dress.entry.boots"],
        "asset_sources": [
            {
                "logical_asset_id": "visual.dressing.entry.boots",
                "asset_id": "boots",
                "asset_type": "model",
                "resolution": "2k",
                "provider_files_hash": "a" * 40,
                "source_tree_sha256": "6" * 64,
                "files": [
                    {
                        "relative_path": "boots.blend",
                        "size_bytes": 10,
                        "sha256": "7" * 64,
                        "texture_semantics": [],
                        "dimensions_px": None,
                    }
                ],
            }
        ],
    }


def test_v2_bundle_allows_shared_packed_textures_but_exact_material_names() -> None:
    house = json.loads(HOUSE_PATH.read_text(encoding="utf-8"))
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    plan = build_forge_plan(house, profile)
    room = next(item for item in plan.rooms if item.kind == "entry_hall")
    record = ue_bundle_contract(plan, room)
    record["external_content"] = _external_content()
    record.update(
        {
            "sha256": "8" * 64,
            "size_bytes": 100,
            "mesh_count": 1,
            "material_count": 4,
            "pbr_complete_material_count": 4,
            "texture_count": 3,
            "material_ids": ["r2.mat.a", "r2.mat.b", "r2.mat.c", "r2.mat.d"],
        }
    )
    assert _validate_bundle_record(record) is record
    metadata = {
        "vista_bundle_contract": "one_room_one_mesh_v2",
        "vista_artifact_id": record["artifact_id"],
        "vista_target_asset_id": record["target_asset_id"],
        "vista_room_id": record["room_id"],
        "vista_room_kind": record["room_kind"],
        "vista_root_transform_policy": record["root_transform_policy"],
        "vista_semantic_policy": record["semantic_policy"],
        "vista_collision_policy": record["collision_policy"],
        "vista_unreal_collision_profile": record["unreal_collision_profile"],
        "vista_source_house_sha256": record["source_hashes"]["house_sha256"],
        "vista_source_visual_profile_sha256": record["source_hashes"]["visual_profile_sha256"],
        "vista_source_forge_plan_sha256": record["source_hashes"]["forge_plan_sha256"],
        "vista_expected_world_transform_cm_json": json.dumps(record["expected_world_transform_cm"]),
        "vista_material_ids_json": json.dumps(record["material_ids"]),
        "vista_external_content_json": json.dumps(record["external_content"]),
    }
    inspection = {
        "bundle_metadata": metadata,
        "sha256": record["sha256"],
        "size_bytes": record["size_bytes"],
        "mesh_count": 1,
        "mesh_node_count": 1,
        "bundle_node_count": 1,
        "bundle_root_is_identity": True,
        "material_count": 4,
        "material_names": list(record["material_ids"]),
        "pbr_complete_material_count": 4,
        "texture_count": 3,
        "camera_count": 0,
        "light_count": 0,
    }
    _validate_bundle_glb(record, inspection)
    inspection["material_names"] = ["r2.mat.a", "r2.mat.b", "r2.mat.c", "wrong"]
    with pytest.raises(ForgeInputError, match="structure differs"):
        _validate_bundle_glb(record, inspection)


def test_no_external_v1_path_is_byte_stable_and_runtime_source_is_fail_closed() -> None:
    house = json.loads(HOUSE_PATH.read_text(encoding="utf-8"))
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    plan = build_forge_plan(house, profile)
    manifest_bytes = canonical_json_bytes(normalized_manifest(plan, texture_size_px=512))
    assert plan.content_digest == "56d07c9664dfe6d054124ca08acac007f6aef7338f0fdbaa81377fcaff34f008"
    assert hashlib.sha256(canonical_json_bytes(plan)).hexdigest() == "8f9ef316cbc5a20fd1ccb5413eac704ef3d39a58bf48bd2d3cca995491c26b13"
    assert hashlib.sha256(manifest_bytes).hexdigest() == "9e11ad06fe7f7bff581a097e451eba02e500ae7c8af386fb79ae52fcc7b5ac8a"
    assert len(manifest_bytes) == 116578

    import tools.blender.vista_playable_home_realism.external_assets as runtime

    source = Path(runtime.__file__).read_text(encoding="utf-8")
    for required in (
        'obj.type in {"ARMATURE", "CAMERA", "LIGHT"}',
        'getattr(obj.data, "shape_keys", None)',
        "external source contains animations",
        "measured normalized placement bounds differ from plan",
        "seat_cushion_",
        "back_cushion_",
    ):
        assert required in source


def test_checked_in_manifest_uses_acquired_assets_without_baking_movable_targets() -> None:
    payload = json.loads(PLACEMENT_PATH.read_text(encoding="utf-8"))
    body = {key: payload[key] for key in payload if key != "content_digest"}
    assert payload["content_digest"] == content_digest(body)
    assert len(payload["placements"]) == 22
    assert sum(item["placement_kind"] == "semantic_fixed" for item in payload["placements"]) == 5
    assert {
        "visual.dressing.kitchen.wooden_plate",
        "visual.dressing.kitchen.wooden_spoon",
    }.issubset({item["source_logical_asset_id"] for item in payload["placements"]})
    serialized = json.dumps(payload)
    for forbidden in ("entity.keys", "entity.coffee_cup", "entity.resident", "entity.exit_door"):
        assert forbidden not in serialized
