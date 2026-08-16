from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tools.blender.vista_playable_home_realism.local_asset_catalog import (
    COMMERCIAL_RELEASE,
    CONTACT_SHEET_PLAN_SCHEMA_VERSION,
    COVERAGE_SCHEMA_VERSION,
    PRIVATE_RESEARCH_DEMO,
    ROOM_ENTRY,
    ROOM_KITCHEN,
    ROOM_LIVING,
    HeroRequirement,
    LocalAssetCandidate,
    LocalAssetCatalogError,
    audit_local_hero_coverage,
    build_contact_sheet_plan,
    evaluate_local_hero_coverage,
)
from tools.blender.vista_playable_home_realism.source_resolver import (
    AllowedSourceRoot,
    AssetSourceSpec,
    PROVIDER_EXISTING_LOCAL,
    PROVIDER_HSSD,
    PROVIDER_POLY_HAVEN,
    PROVIDER_PROJECT_AUTHORED,
    PROVIDER_YCB,
    content_digest,
)


LOCAL_PROVIDERS = (
    PROVIDER_PROJECT_AUTHORED,
    PROVIDER_HSSD,
    PROVIDER_YCB,
    PROVIDER_POLY_HAVEN,
)


def _license(provider: str) -> dict:
    common = {
        "license_id": "Apache-2.0",
        "license_url": "https://spdx.org/licenses/Apache-2.0.html",
        "entitlement_status": "project_owned",
        "entitlement_record": "project-owned://simworld/local-asset-fixture",
        "attribution": "SimWorld fixture author",
        "modification_notice": "Unmodified focused-test fixture.",
        "commercial_use": "allowed",
        "redistribution_restriction": "project_policy",
    }
    if provider == PROVIDER_HSSD:
        common.update(
            {
                "license_id": "CC-BY-NC-4.0",
                "license_url": "https://creativecommons.org/licenses/by-nc/4.0/",
                "entitlement_status": "verified",
                "entitlement_record": "local-audit://hssd/focused-fixture",
                "attribution": "HSSD authors",
                "commercial_use": "prohibited",
                "redistribution_restriction": "noncommercial_attribution_required",
            }
        )
    elif provider == PROVIDER_YCB:
        common.update(
            {
                "license_id": "CC-BY-4.0",
                "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "entitlement_status": "verified",
                "entitlement_record": "local-audit://ycb/focused-fixture",
                "attribution": "YCB authors",
                "redistribution_restriction": "attribution_required",
            }
        )
    elif provider == PROVIDER_POLY_HAVEN:
        common.update(
            {
                "license_id": "CC0-1.0",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "entitlement_status": "verified",
                "entitlement_record": "local-audit://polyhaven/focused-fixture",
                "attribution": "Poly Haven",
            }
        )
    elif provider == PROVIDER_EXISTING_LOCAL:
        common.update(
            {
                "license_id": "MIT",
                "license_url": "https://spdx.org/licenses/MIT.html",
                "entitlement_status": "verified",
                "entitlement_record": "local-audit://generic/focused-fixture",
                "redistribution_restriction": "attribution_required",
            }
        )
    return common


def _material_inventory(
    *,
    semantics: tuple[str, ...] = ("base_color", "normal", "roughness"),
    size: int = 2048,
) -> dict:
    return {
        "slots": [
            {
                "slot_id": "hero_surface",
                "shader_class": "pbr_metallic_roughness",
                "blend_mode": "opaque",
                "texture_semantics": list(semantics),
                "minimum_texture_size_px": size,
            }
        ],
        "texture_count": len(semantics),
        "all_primitives_material_bound": True,
    }


def _roots(tmp_path: Path, providers: tuple[str, ...] = LOCAL_PROVIDERS) -> dict[str, AllowedSourceRoot]:
    result: dict[str, AllowedSourceRoot] = {}
    for provider in providers:
        root = (tmp_path / f"{provider}_root").resolve()
        root.mkdir(parents=True)
        result[provider] = AllowedSourceRoot(
            root_id=f"{provider}_root",
            path=root,
            providers=(provider,),
        )
    return result


def _catalog_scheme(provider: str) -> str:
    return {
        PROVIDER_PROJECT_AUTHORED: "project",
        PROVIDER_HSSD: "hssd",
        PROVIDER_YCB: "ycb",
        PROVIDER_POLY_HAVEN: "polyhaven",
        PROVIDER_EXISTING_LOCAL: "catalog",
    }[provider]


def _candidate(
    roots: dict[str, AllowedSourceRoot],
    *,
    candidate_id: str,
    target_hero_id: str,
    provider: str,
    category: str,
    aliases: tuple[str, ...] = (),
    styles: tuple[str, ...] = ("contemporary", "residential"),
    dimensions: tuple[float, float, float] = (2.1, 0.95, 1.0),
    semantics: tuple[str, ...] = ("base_color", "normal", "roughness"),
    texture_size: int = 2048,
) -> LocalAssetCandidate:
    source = roots[provider].path / "assets" / f"{candidate_id.replace('.', '_')}.glb"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(f"explicit candidate bytes: {candidate_id}".encode())
    source_spec = AssetSourceSpec(
        receipt_id=f"source.{candidate_id}",
        logical_asset_id=f"visual.{candidate_id}",
        provider=provider,
        source_path=source,
        source_version="fixture_v1",
        catalog_identity=f"{_catalog_scheme(provider)}://catalog/{candidate_id}",
        metric_bounds_m={"min_m": [0, 0, 0], "max_m": list(dimensions)},
        license=_license(provider),
        material_inventory=_material_inventory(semantics=semantics, size=texture_size),
        import_policy={
            "nanite": "eligible_static_opaque",
            "mobility": "static",
            "lod_policy": "nanite",
            "collision_policy": "hidden_r1_proxy",
        },
    )
    return LocalAssetCandidate(
        candidate_id=candidate_id,
        target_hero_id=target_hero_id,
        source_spec=source_spec,
        declared_category=category,
        semantic_aliases=aliases,
        style_tags=styles,
    )


def _sofa_requirement() -> HeroRequirement:
    return HeroRequirement(
        hero_id="home.r1/room.living_room/entity.sofa.01",
        room_id=ROOM_LIVING,
        required_category="sofa",
        required_style_tags=("residential", "contemporary"),
        minimum_dimensions_m=(1.8, 0.7, 0.7),
        maximum_dimensions_m=(2.6, 1.3, 1.3),
    )


def _three_room_requirements() -> tuple[HeroRequirement, ...]:
    return (
        HeroRequirement(
            hero_id="home.r1/room.entry_hall/entity.shoe_bench.01",
            room_id=ROOM_ENTRY,
            required_category="shoe_bench",
            required_style_tags=("residential", "contemporary"),
            minimum_dimensions_m=(1.0, 0.35, 0.35),
            maximum_dimensions_m=(1.6, 0.75, 0.75),
        ),
        _sofa_requirement(),
        HeroRequirement(
            hero_id="home.r1/room.kitchen_dining/entity.dining_table.01",
            room_id=ROOM_KITCHEN,
            required_category="dining_table",
            required_style_tags=("residential", "contemporary"),
            minimum_dimensions_m=(1.3, 0.75, 0.65),
            maximum_dimensions_m=(2.2, 1.3, 1.0),
        ),
    )


@pytest.mark.parametrize("provider", LOCAL_PROVIDERS)
def test_explicit_file_candidate_uses_source_resolver_for_each_local_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    roots = _roots(tmp_path, (provider,))
    requirement = _sofa_requirement()
    candidate = _candidate(
        roots,
        candidate_id=f"candidate.{provider}.sofa",
        target_hero_id=requirement.hero_id,
        provider=provider,
        category="sofa",
    )

    # A file candidate must never trigger dataset-wide recursive discovery.
    monkeypatch.setattr(
        Path,
        "rglob",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("recursive scan")),
    )
    matrix = audit_local_hero_coverage([requirement], [candidate], list(roots.values()))

    evaluation = matrix["coverage_rows"][0]["candidate_evaluations"][0]
    assert matrix["schema_version"] == COVERAGE_SCHEMA_VERSION
    assert matrix["summary"]["automated_coverage_status"] == "complete"
    assert matrix["summary"]["visual_review_status"] == "not_performed"
    assert matrix["summary"]["visual_accepted"] is False
    assert evaluation["source_identity"]["provider"] == provider
    assert evaluation["source_identity"]["source_root_id"] == f"{provider}_root"
    assert evaluation["source_identity"]["source_relative_path"].startswith("assets/")


def test_matrix_is_deterministic_across_candidate_requirement_and_root_order(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    requirements = _three_room_requirements()
    dimensions = {
        requirements[0].hero_id: (1.3, 0.5, 0.52),
        requirements[1].hero_id: (2.1, 0.95, 1.0),
        requirements[2].hero_id: (1.7, 1.0, 0.82),
    }
    categories = {
        requirements[0].hero_id: "shoe_bench",
        requirements[1].hero_id: "sofa",
        requirements[2].hero_id: "dining_table",
    }
    providers = (PROVIDER_PROJECT_AUTHORED, PROVIDER_HSSD, PROVIDER_YCB)
    candidates = [
        _candidate(
            roots,
            candidate_id=f"candidate.order.{index}",
            target_hero_id=requirement.hero_id,
            provider=provider,
            category=categories[requirement.hero_id],
            dimensions=dimensions[requirement.hero_id],
        )
        for index, (requirement, provider) in enumerate(zip(requirements, providers), 1)
    ]

    first = audit_local_hero_coverage(requirements, candidates, list(roots.values()))
    second = audit_local_hero_coverage(
        tuple(reversed(requirements)),
        tuple(reversed(candidates)),
        tuple(reversed(list(roots.values()))),
    )

    assert first == second
    assert first["content_digest"] == content_digest(first, "content_digest")
    assert [row["room_id"] for row in first["coverage_rows"]] == [
        ROOM_ENTRY,
        ROOM_LIVING,
        ROOM_KITCHEN,
    ]


@pytest.mark.parametrize(
    ("required", "declared"),
    [
        ("chair", "stool"),
        ("pot", "planter"),
        ("table", "desk"),
        ("slipper", "shoe"),
        ("ladder", "stall_bar"),
    ],
)
def test_known_alias_mismatches_are_rejected_even_when_alias_claims_target(
    tmp_path: Path,
    required: str,
    declared: str,
) -> None:
    roots = _roots(tmp_path, (PROVIDER_HSSD,))
    requirement = replace(_sofa_requirement(), required_category=required)
    candidate = _candidate(
        roots,
        candidate_id=f"candidate.alias.{required}.{declared}",
        target_hero_id=requirement.hero_id,
        provider=PROVIDER_HSSD,
        category=declared,
        aliases=(required,),
    )

    matrix = evaluate_local_hero_coverage([requirement], [candidate], list(roots.values()))
    evaluation = matrix["coverage_rows"][0]["candidate_evaluations"][0]
    assert evaluation["gates"]["category"]["forbidden_alias_substitution"] is True
    assert evaluation["automated_reject_reason_codes"] == [
        "CATEGORY_ALIAS_SUBSTITUTION_FORBIDDEN"
    ]
    with pytest.raises(LocalAssetCatalogError, match="VISTA_LOCAL_ASSET_HERO_COVERAGE_MISSING") as caught:
        audit_local_hero_coverage([requirement], [candidate], list(roots.values()))
    assert caught.value.coverage_matrix == matrix


def test_missing_hero_coverage_fails_closed_with_diagnostic_matrix(tmp_path: Path) -> None:
    roots = _roots(tmp_path, (PROVIDER_PROJECT_AUTHORED,))
    sofa = _sofa_requirement()
    table = HeroRequirement(
        hero_id="home.r1/room.kitchen_dining/entity.dining_table.01",
        room_id=ROOM_KITCHEN,
        required_category="dining_table",
        required_style_tags=("residential", "contemporary"),
        minimum_dimensions_m=(1.3, 0.75, 0.65),
        maximum_dimensions_m=(2.2, 1.3, 1.0),
    )
    candidate = _candidate(
        roots,
        candidate_id="candidate.only.sofa",
        target_hero_id=sofa.hero_id,
        provider=PROVIDER_PROJECT_AUTHORED,
        category="sofa",
    )

    with pytest.raises(LocalAssetCatalogError) as caught:
        audit_local_hero_coverage([sofa, table], [candidate], list(roots.values()))

    assert caught.value.code == "VISTA_LOCAL_ASSET_HERO_COVERAGE_MISSING"
    assert caught.value.coverage_matrix is not None
    assert caught.value.coverage_matrix["summary"]["missing_hero_ids"] == [table.hero_id]
    assert caught.value.coverage_matrix["summary"]["automated_coverage_status"] == "incomplete"


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ({"styles": ("industrial",)}, "STYLE_REQUIRED_TAGS_MISSING"),
        ({"dimensions": (0.4, 0.4, 0.4)}, "DIMENSIONS_OUT_OF_RANGE"),
        ({"semantics": ("base_color",)}, "PBR_REQUIRED_SEMANTICS_MISSING"),
        ({"texture_size": 1024}, "PBR_SCREEN_RESOLUTION_TOO_LOW"),
    ],
)
def test_style_dimensions_and_pbr_screen_gates_are_independent(
    tmp_path: Path,
    mutation: dict,
    expected_reason: str,
) -> None:
    roots = _roots(tmp_path, (PROVIDER_PROJECT_AUTHORED,))
    requirement = _sofa_requirement()
    candidate = _candidate(
        roots,
        candidate_id=f"candidate.gate.{expected_reason.lower()}",
        target_hero_id=requirement.hero_id,
        provider=PROVIDER_PROJECT_AUTHORED,
        category="sofa",
        **mutation,
    )

    matrix = evaluate_local_hero_coverage([requirement], [candidate], list(roots.values()))
    evaluation = matrix["coverage_rows"][0]["candidate_evaluations"][0]
    assert expected_reason in evaluation["automated_reject_reason_codes"]
    assert evaluation["automated_gate_status"] == "rejected"


def test_hssd_license_is_suitable_for_private_demo_but_not_commercial_release(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path, (PROVIDER_HSSD,))
    requirement = _sofa_requirement()
    candidate = _candidate(
        roots,
        candidate_id="candidate.hssd.research.sofa",
        target_hero_id=requirement.hero_id,
        provider=PROVIDER_HSSD,
        category="sofa",
    )

    private = audit_local_hero_coverage(
        [requirement],
        [candidate],
        list(roots.values()),
        use_context=PRIVATE_RESEARCH_DEMO,
    )
    commercial = evaluate_local_hero_coverage(
        [requirement],
        [candidate],
        list(roots.values()),
        use_context=COMMERCIAL_RELEASE,
    )

    assert private["summary"]["automated_coverage_status"] == "complete"
    evaluation = commercial["coverage_rows"][0]["candidate_evaluations"][0]
    assert evaluation["gates"]["license"]["reason_codes"] == [
        "LICENSE_CONTEXT_INCOMPATIBLE"
    ]
    assert commercial["summary"]["automated_coverage_status"] == "incomplete"


def test_contact_sheet_plan_has_only_public_relative_identity_and_pending_review(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path, (PROVIDER_YCB,))
    requirement = _sofa_requirement()
    candidate = _candidate(
        roots,
        candidate_id="candidate.ycb.contact.sofa",
        target_hero_id=requirement.hero_id,
        provider=PROVIDER_YCB,
        category="sofa",
        texture_size=4096,
    )
    matrix = audit_local_hero_coverage([requirement], [candidate], list(roots.values()))

    plan = build_contact_sheet_plan(matrix)
    serialized = json.dumps(plan, sort_keys=True)

    assert plan["schema_version"] == CONTACT_SHEET_PLAN_SCHEMA_VERSION
    assert plan["coverage_digest"] == matrix["content_digest"]
    assert plan["content_digest"] == content_digest(plan, "content_digest")
    assert plan["missing_hero_ids"] == []
    assert plan["promotion_eligible"] is True
    assert plan["visual_review_status"] == "not_performed"
    assert plan["visual_accepted"] is False
    assert len(plan["render_jobs"]) == 1
    job = plan["render_jobs"][0]
    assert job["source_identity"] == {
        "provider": PROVIDER_YCB,
        "source_root_id": "ycb_root",
        "source_relative_path": "assets/candidate_ycb_contact_sofa.glb",
        "source_uri": "ycb://ycb_root/assets/candidate_ycb_contact_sofa.glb",
        "source_digest": hashlib.sha256(candidate.source_spec.source_path.read_bytes()).hexdigest(),
        "catalog_identity": "ycb://catalog/candidate.ycb.contact.sofa",
    }
    assert len(job["source_identity"]["source_digest"]) == 64
    assert job["review"]["status"] == "pending_human_review"
    assert job["review"]["decision"] is None
    assert str(tmp_path) not in serialized
    assert "/home/" not in serialized
    assert "/mnt/" not in serialized
    assert "file://" not in serialized


def test_incomplete_matrix_can_plan_diagnostic_jobs_but_cannot_promote(tmp_path: Path) -> None:
    roots = _roots(tmp_path, (PROVIDER_HSSD,))
    sofa = _sofa_requirement()
    table = HeroRequirement(
        hero_id="home.r1/room.kitchen_dining/entity.dining_table.01",
        room_id=ROOM_KITCHEN,
        required_category="dining_table",
        required_style_tags=("residential", "contemporary"),
        minimum_dimensions_m=(1.3, 0.75, 0.65),
        maximum_dimensions_m=(2.2, 1.3, 1.0),
    )
    rejected_alias = _candidate(
        roots,
        candidate_id="candidate.hssd.desk.for.table",
        target_hero_id=table.hero_id,
        provider=PROVIDER_HSSD,
        category="desk",
        aliases=("dining_table",),
        dimensions=(1.7, 1.0, 0.82),
    )
    matrix = evaluate_local_hero_coverage(
        [sofa, table],
        [rejected_alias],
        list(roots.values()),
    )

    plan = build_contact_sheet_plan(matrix)

    assert plan["promotion_eligible"] is False
    assert plan["missing_hero_ids"] == [sofa.hero_id, table.hero_id]
    assert len(plan["render_jobs"]) == 1
    assert plan["render_jobs"][0]["labels"]["automated_gate_status"] == "rejected"
    assert plan["render_jobs"][0]["review"]["status"] == "pending_human_review"
    with pytest.raises(LocalAssetCatalogError, match="VISTA_LOCAL_ASSET_HERO_COVERAGE_MISSING"):
        audit_local_hero_coverage(
            [sofa, table],
            [rejected_alias],
            list(roots.values()),
        )


def test_requirement_hero_must_belong_to_declared_finished_room(tmp_path: Path) -> None:
    mismatched = replace(_sofa_requirement(), room_id=ROOM_ENTRY)

    with pytest.raises(LocalAssetCatalogError, match="VISTA_LOCAL_ASSET_HERO_ROOM_MISMATCH"):
        evaluate_local_hero_coverage([mismatched], [], [])


def test_generic_existing_local_provider_is_not_silently_admitted(tmp_path: Path) -> None:
    roots = _roots(tmp_path, (PROVIDER_EXISTING_LOCAL,))
    requirement = _sofa_requirement()
    candidate = _candidate(
        roots,
        candidate_id="candidate.generic.sofa",
        target_hero_id=requirement.hero_id,
        provider=PROVIDER_EXISTING_LOCAL,
        category="sofa",
    )

    with pytest.raises(LocalAssetCatalogError, match="VISTA_LOCAL_ASSET_PROVIDER_PROHIBITED"):
        evaluate_local_hero_coverage([requirement], [candidate], list(roots.values()))


def test_unlisted_sibling_assets_are_never_discovered(tmp_path: Path) -> None:
    roots = _roots(tmp_path, (PROVIDER_PROJECT_AUTHORED,))
    sibling = roots[PROVIDER_PROJECT_AUTHORED].path / "assets" / "looks_like_sofa.glb"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"must remain undiscovered")

    matrix = evaluate_local_hero_coverage([_sofa_requirement()], [], list(roots.values()))

    assert matrix["summary"]["resolved_candidate_count"] == 0
    assert matrix["summary"]["missing_hero_ids"] == [_sofa_requirement().hero_id]


def test_missing_explicit_source_path_fails_instead_of_using_sibling(tmp_path: Path) -> None:
    roots = _roots(tmp_path, (PROVIDER_PROJECT_AUTHORED,))
    requirement = _sofa_requirement()
    candidate = _candidate(
        roots,
        candidate_id="candidate.missing.sofa",
        target_hero_id=requirement.hero_id,
        provider=PROVIDER_PROJECT_AUTHORED,
        category="sofa",
    )
    candidate.source_spec.source_path.unlink()
    sibling = roots[PROVIDER_PROJECT_AUTHORED].path / "assets" / "fallback.glb"
    sibling.write_bytes(b"must not become fallback")

    with pytest.raises(LocalAssetCatalogError, match="VISTA_LOCAL_ASSET_SOURCE_REJECTED"):
        evaluate_local_hero_coverage([requirement], [candidate], list(roots.values()))
