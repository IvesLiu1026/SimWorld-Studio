from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from tools.blender.vista_playable_home_realism.source_resolver import (
    AllowedSourceRoot,
    AssetSourceResolutionError,
    AssetSourceSpec,
    PROVIDER_EXISTING_LOCAL,
    PROVIDER_FAB_GATED,
    PROVIDER_HSSD,
    PROVIDER_POLY_HAVEN,
    PROVIDER_PROJECT_AUTHORED,
    PROVIDER_YCB,
    RESOLUTION_SCHEMA_VERSION,
    content_digest,
    resolve_asset_source,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VISUAL_PROFILE_SCHEMA = json.loads(
    (
        REPOSITORY_ROOT
        / "world_packs"
        / "schemas"
        / "vista-playable-home-visual-profile-v1.schema.json"
    ).read_text(encoding="utf-8")
)
ASSET_SOURCE_RECEIPT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": VISUAL_PROFILE_SCHEMA["$defs"],
    "$ref": "#/$defs/assetSourceReceipt",
}


def _license(provider: str) -> dict:
    common = {
        "license_id": "MIT",
        "license_url": "https://spdx.org/licenses/MIT.html",
        "entitlement_status": "verified",
        "entitlement_record": "local-audit://fixture/assets",
        "attribution": "Fixture author",
        "modification_notice": "No modifications.",
        "commercial_use": "allowed",
        "redistribution_restriction": "attribution_required",
    }
    if provider == PROVIDER_PROJECT_AUTHORED:
        common.update(
            {
                "license_id": "Apache-2.0",
                "license_url": "https://spdx.org/licenses/Apache-2.0.html",
                "entitlement_status": "project_owned",
                "entitlement_record": "project-owned://simworld/fixture",
                "attribution": "SimWorld project authors",
                "modification_notice": "Original project-authored source.",
                "redistribution_restriction": "project_policy",
            }
        )
    elif provider == PROVIDER_HSSD:
        common.update(
            {
                "license_id": "CC-BY-NC-4.0",
                "license_url": "https://creativecommons.org/licenses/by-nc/4.0/",
                "commercial_use": "prohibited",
                "redistribution_restriction": "noncommercial_attribution_required",
            }
        )
    elif provider == PROVIDER_YCB:
        common.update(
            {
                "license_id": "CC-BY-4.0",
                "license_url": "https://creativecommons.org/licenses/by/4.0/",
            }
        )
    elif provider == PROVIDER_POLY_HAVEN:
        common.update(
            {
                "license_id": "CC0-1.0",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "redistribution_restriction": "project_policy",
            }
        )
    elif provider == PROVIDER_FAB_GATED:
        common.update(
            {
                "license_id": "Fab-Standard-License",
                "license_url": "https://www.fab.com/eula",
                "entitlement_record": "account-entitlement://fab/opaque-receipt-01",
                "attribution": "Fab publisher from pinned catalog record",
                "redistribution_restriction": "no_standalone_asset_redistribution",
            }
        )
    return common


def _material_inventory() -> dict:
    return {
        "slots": [
            {
                "slot_id": "fixture_surface",
                "shader_class": "pbr_metallic_roughness",
                "blend_mode": "opaque",
                "texture_semantics": ["base_color", "normal", "roughness"],
                "minimum_texture_size_px": 2048,
            }
        ],
        "texture_count": 3,
        "all_primitives_material_bound": True,
    }


def _import_policy() -> dict:
    return {
        "nanite": "eligible_static_opaque",
        "mobility": "static",
        "lod_policy": "nanite",
        "collision_policy": "hidden_r1_proxy",
    }


def _catalog_identity(provider: str) -> str:
    schemes = {
        PROVIDER_PROJECT_AUTHORED: "project",
        PROVIDER_EXISTING_LOCAL: "catalog",
        PROVIDER_HSSD: "hssd",
        PROVIDER_YCB: "ycb",
        PROVIDER_POLY_HAVEN: "polyhaven",
        PROVIDER_FAB_GATED: "fab",
    }
    return f"{schemes[provider]}://catalog/fixture-chair"


def _fixture(
    tmp_path: Path,
    provider: str,
    *,
    payload: bytes = b"fixture glb bytes",
) -> tuple[AssetSourceSpec, list[AllowedSourceRoot], Path]:
    root = tmp_path / f"{provider}_root"
    source = root / "assets" / "fixture.glb"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    spec = AssetSourceSpec(
        receipt_id="source.fixture",
        logical_asset_id="visual.fixture",
        provider=provider,
        source_path=source.resolve(),
        source_version="fixture_v1",
        catalog_identity=_catalog_identity(provider),
        metric_bounds_m={"min_m": [-0.5, -0.5, 0], "max_m": [0.5, 0.5, 1]},
        license=_license(provider),
        material_inventory=_material_inventory(),
        import_policy=_import_policy(),
    )
    roots = [AllowedSourceRoot(root_id=f"{provider}_root", path=root.resolve(), providers=(provider,))]
    return spec, roots, source


@pytest.mark.parametrize(
    ("provider", "source_kind", "scheme"),
    [
        (PROVIDER_PROJECT_AUTHORED, "project_authored", "project"),
        (PROVIDER_EXISTING_LOCAL, "existing_local", "catalog"),
        (PROVIDER_HSSD, "hssd", "hssd"),
        (PROVIDER_YCB, "existing_local", "ycb"),
        (PROVIDER_POLY_HAVEN, "existing_local", "polyhaven"),
        (PROVIDER_FAB_GATED, "fab", "fab"),
    ],
)
def test_all_providers_normalize_to_one_profile_receipt_contract(
    tmp_path: Path,
    provider: str,
    source_kind: str,
    scheme: str,
) -> None:
    payload = f"{provider} exact bytes".encode()
    spec, roots, _source = _fixture(tmp_path, provider, payload=payload)

    resolution = resolve_asset_source(spec, roots)
    normalized = resolution.normalized_receipt
    receipt = resolution.asset_source_receipt

    jsonschema.Draft202012Validator(ASSET_SOURCE_RECEIPT_SCHEMA).validate(receipt)
    assert normalized["schema_version"] == RESOLUTION_SCHEMA_VERSION
    assert normalized["provider"] == provider
    assert receipt["source_kind"] == source_kind
    assert receipt["source_uri"] == f"{scheme}://{provider}_root/assets/fixture.glb"
    assert receipt["source_digest"] == hashlib.sha256(payload).hexdigest()
    assert normalized["source_evidence"] == {
        "content_kind": "file",
        "digest_algorithm": "sha256",
        "directory_count": 0,
        "file_count": 1,
        "size_bytes": len(payload),
        "source_digest": hashlib.sha256(payload).hexdigest(),
        "source_relative_path": "assets/fixture.glb",
        "source_root_id": f"{provider}_root",
    }
    assert normalized["provenance"]["catalog_identity"] == _catalog_identity(provider)
    assert normalized["provenance"]["entitlement_status"] == spec.license["entitlement_status"]
    assert normalized["provenance"]["redistribution_restriction"] == spec.license[
        "redistribution_restriction"
    ]
    assert receipt["receipt_digest"] == content_digest(receipt, "receipt_digest")
    assert normalized["resolution_digest"] == content_digest(normalized, "resolution_digest")


def test_resolution_is_repeatable_and_contains_no_private_absolute_path(tmp_path: Path) -> None:
    spec, roots, _source = _fixture(tmp_path, PROVIDER_YCB)
    first = resolve_asset_source(spec, roots).normalized_receipt
    second = resolve_asset_source(spec, tuple(reversed(roots))).normalized_receipt

    assert first == second
    serialized = json.dumps(first, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert str(spec.source_path) not in serialized
    assert "/home/" not in serialized
    assert "/mnt/" not in serialized


def test_directory_tree_hash_is_ordered_and_sensitive_to_bytes_and_names(tmp_path: Path) -> None:
    spec, roots, source_file = _fixture(tmp_path, PROVIDER_PROJECT_AUTHORED)
    source_file.unlink()
    tree = source_file.parent / "kit"
    (tree / "textures").mkdir(parents=True)
    (tree / "empty").mkdir()
    (tree / "meshes").mkdir()
    (tree / "textures" / "surface.png").write_bytes(b"texture")
    (tree / "meshes" / "shell.glb").write_bytes(b"mesh")
    spec = replace(spec, source_path=tree.resolve())

    first = resolve_asset_source(spec, roots).normalized_receipt
    second = resolve_asset_source(spec, roots).normalized_receipt
    assert first == second
    assert first["source_evidence"]["content_kind"] == "tree"
    assert first["source_evidence"]["digest_algorithm"] == "sha256-tree-v1"
    assert first["source_evidence"]["file_count"] == 2
    assert first["source_evidence"]["directory_count"] == 3
    assert first["source_evidence"]["size_bytes"] == len(b"texturemesh")

    (tree / "meshes" / "shell.glb").write_bytes(b"mesh changed")
    changed_bytes = resolve_asset_source(spec, roots).normalized_receipt
    assert changed_bytes["source_evidence"]["source_digest"] != first["source_evidence"]["source_digest"]
    assert changed_bytes["source_evidence"]["size_bytes"] != first["source_evidence"]["size_bytes"]

    (tree / "textures" / "surface.png").rename(tree / "textures" / "finish.png")
    changed_name = resolve_asset_source(spec, roots).normalized_receipt
    assert changed_name["source_evidence"]["source_digest"] != changed_bytes["source_evidence"]["source_digest"]


@pytest.mark.parametrize(
    "provider",
    [PROVIDER_HSSD, PROVIDER_YCB, PROVIDER_POLY_HAVEN],
)
def test_provider_license_mismatch_fails_closed(tmp_path: Path, provider: str) -> None:
    spec, roots, _source = _fixture(tmp_path, provider)
    bad_license = copy.deepcopy(spec.license)
    bad_license["license_id"] = "MIT"
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_LICENSE_INVALID"):
        resolve_asset_source(replace(spec, license=bad_license), roots)


@pytest.mark.parametrize(
    ("status", "record"),
    [
        ("project_owned", "project-owned://simworld/fab-copy"),
        ("verified", "local-audit://fixture/fab-copy"),
    ],
)
def test_fab_requires_verified_account_entitlement(
    tmp_path: Path,
    status: str,
    record: str,
) -> None:
    spec, roots, _source = _fixture(tmp_path, PROVIDER_FAB_GATED)
    license_receipt = copy.deepcopy(spec.license)
    license_receipt["entitlement_status"] = status
    license_receipt["entitlement_record"] = record
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_ENTITLEMENT_REQUIRED"):
        resolve_asset_source(replace(spec, license=license_receipt), roots)


def test_missing_requested_fab_source_never_falls_back_to_project_asset(tmp_path: Path) -> None:
    spec, roots, source = _fixture(tmp_path, PROVIDER_FAB_GATED)
    source.unlink()
    project_root = tmp_path / "project_root"
    fallback = project_root / "assets" / "fixture.glb"
    fallback.parent.mkdir(parents=True)
    fallback.write_bytes(b"must not be selected")
    roots.append(
        AllowedSourceRoot(
            root_id="project_root",
            path=project_root.resolve(),
            providers=(PROVIDER_PROJECT_AUTHORED,),
        )
    )

    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_PATH_INVALID"):
        resolve_asset_source(spec, roots)


def test_source_must_be_absolute_existing_and_inside_allowlisted_root(tmp_path: Path) -> None:
    spec, roots, source = _fixture(tmp_path, PROVIDER_EXISTING_LOCAL)
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_PATH_INVALID"):
        resolve_asset_source(replace(spec, source_path=Path("assets/fixture.glb")), roots)

    missing = source.parent / "missing.glb"
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_PATH_INVALID"):
        resolve_asset_source(replace(spec, source_path=missing), roots)

    outside = tmp_path / "outside.glb"
    outside.write_bytes(b"outside")
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_OUTSIDE_ALLOWLIST"):
        resolve_asset_source(replace(spec, source_path=outside.resolve()), roots)


def test_provider_is_bound_to_the_most_specific_allowlisted_root(tmp_path: Path) -> None:
    spec, roots, source = _fixture(tmp_path, PROVIDER_YCB)
    broad = AllowedSourceRoot(
        root_id="broad_root",
        path=tmp_path.resolve(),
        providers=(PROVIDER_YCB,),
    )
    specific_wrong_provider = AllowedSourceRoot(
        root_id="specific_root",
        path=source.parent.resolve(),
        providers=(PROVIDER_HSSD,),
    )
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_PROVIDER_NOT_ALLOWED"):
        resolve_asset_source(spec, [broad, specific_wrong_provider, *roots])


def test_symlink_source_and_symlink_tree_entry_are_rejected(tmp_path: Path) -> None:
    spec, roots, source = _fixture(tmp_path, PROVIDER_EXISTING_LOCAL)
    source_link = source.parent / "linked.glb"
    try:
        source_link.symlink_to(source)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_PATH_INVALID"):
        resolve_asset_source(replace(spec, source_path=source_link), roots)

    tree = source.parent / "tree"
    tree.mkdir()
    (tree / "mesh.glb").write_bytes(b"mesh")
    (tree / "escaped.glb").symlink_to(tmp_path / "outside.glb")
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_PATH_INVALID"):
        resolve_asset_source(replace(spec, source_path=tree.resolve()), roots)


def test_private_metadata_and_catalog_traversal_are_rejected(tmp_path: Path) -> None:
    spec, roots, _source = _fixture(tmp_path, PROVIDER_YCB)
    private_license = copy.deepcopy(spec.license)
    private_license["attribution"] = "audited at /mnt/private/license.json"
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_PRIVATE_PATH_PROHIBITED"):
        resolve_asset_source(replace(spec, license=private_license), roots)

    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_URI_UNSAFE"):
        resolve_asset_source(replace(spec, catalog_identity="ycb://catalog/../private"), roots)


def test_closed_metadata_and_import_policy_fail_before_resolution(tmp_path: Path) -> None:
    spec, roots, _source = _fixture(tmp_path, PROVIDER_PROJECT_AUTHORED)
    license_with_secret = {**spec.license, "access_token": "secret"}
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_METADATA_INVALID"):
        resolve_asset_source(replace(spec, license=license_with_secret), roots)

    invalid_import = {**spec.import_policy, "mobility": "movable"}
    with pytest.raises(AssetSourceResolutionError, match="VISTA_SOURCE_IMPORT_POLICY_INVALID"):
        resolve_asset_source(replace(spec, import_policy=invalid_import), roots)


def test_source_digest_and_size_change_only_when_exact_source_changes(tmp_path: Path) -> None:
    spec, roots, source = _fixture(tmp_path, PROVIDER_EXISTING_LOCAL, payload=b"abc")
    original = resolve_asset_source(spec, roots).normalized_receipt
    os.utime(source, None)
    touched = resolve_asset_source(spec, roots).normalized_receipt
    assert touched == original

    source.write_bytes(b"abcd")
    changed = resolve_asset_source(spec, roots).normalized_receipt
    assert changed["source_evidence"]["source_digest"] == hashlib.sha256(b"abcd").hexdigest()
    assert changed["source_evidence"]["size_bytes"] == 4
    assert changed["asset_source_receipt"]["receipt_digest"] != original["asset_source_receipt"][
        "receipt_digest"
    ]
