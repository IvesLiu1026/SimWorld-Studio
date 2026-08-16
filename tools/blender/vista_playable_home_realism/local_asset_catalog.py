"""Deterministic, local-only hero-asset coverage and contact-sheet contracts.

This module deliberately does not discover files.  The caller supplies every
candidate path, its source metadata, and a finite allowlist of source roots.
Each candidate is resolved by :mod:`source_resolver`, then evaluated against a
closed hero requirement.  An automated pass means only "eligible for a human
contact-sheet review"; it is never emitted as visual acceptance.

The authoritative private path remains in the caller's root registry.  Public
coverage/contact-sheet documents contain only opaque root IDs, relative source
identities, hashes, and labels, so they can be retained without leaking NAS or
home-directory paths.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .source_resolver import (
    AllowedSourceRoot,
    AssetSourceResolutionError,
    AssetSourceSpec,
    PROVIDER_HSSD,
    PROVIDER_POLY_HAVEN,
    PROVIDER_PROJECT_AUTHORED,
    PROVIDER_YCB,
    canonical_json_bytes,
    content_digest,
    resolve_asset_source,
)


COVERAGE_SCHEMA_VERSION = "simworld.vista.playable-home-local-hero-coverage/v1"
CONTACT_SHEET_PLAN_SCHEMA_VERSION = (
    "simworld.vista.playable-home-local-hero-contact-sheet-plan/v1"
)

PRIVATE_RESEARCH_DEMO = "private_research_demo"
COMMERCIAL_RELEASE = "commercial_release"
SUPPORTED_USE_CONTEXTS = frozenset({PRIVATE_RESEARCH_DEMO, COMMERCIAL_RELEASE})

ROOM_ENTRY = "home.r1/room.entry_hall"
ROOM_LIVING = "home.r1/room.living_room"
ROOM_KITCHEN = "home.r1/room.kitchen_dining"
FINISHED_ROOM_IDS = (ROOM_ENTRY, ROOM_LIVING, ROOM_KITCHEN)

LOCAL_ONLY_PROVIDERS = frozenset(
    {
        PROVIDER_PROJECT_AUTHORED,
        PROVIDER_HSSD,
        PROVIDER_YCB,
        PROVIDER_POLY_HAVEN,
    }
)

_PUBLIC_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$")
_PRIVATE_PATH_MARKERS = ("/home/", "/root/", "/mnt/", "/nas/", "file://")
_PBR_BASELINE = frozenset({"base_color", "normal", "roughness"})

# These are known failure modes from the r1 HSSD selection pass.  They are
# intentionally category pairs, not aliases: a semantic alias can nominate a
# candidate for inspection, but can never turn one of these into an exact hero
# match.  Table variants are listed explicitly because a generic "table" alias
# previously admitted desk-shaped geometry.
_FORBIDDEN_CATEGORY_SUBSTITUTIONS = frozenset(
    {
        frozenset({"chair", "stool"}),
        frozenset({"rolling_chair", "stool"}),
        frozenset({"pot", "planter"}),
        frozenset({"table", "desk"}),
        frozenset({"coffee_table", "desk"}),
        frozenset({"dining_table", "desk"}),
        frozenset({"slipper", "shoe"}),
        frozenset({"ladder", "stall_bar"}),
    }
)


class LocalAssetCatalogError(ValueError):
    """A local-only catalog or coverage gate failed closed."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        coverage_matrix: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.coverage_matrix = (
            copy.deepcopy(dict(coverage_matrix)) if coverage_matrix is not None else None
        )


def _fail(
    code: str,
    message: str,
    *,
    coverage_matrix: Mapping[str, Any] | None = None,
) -> None:
    raise LocalAssetCatalogError(code, message, coverage_matrix=coverage_matrix)


@dataclass(frozen=True)
class HeroRequirement:
    """Closed automated gate for one required vertical-slice hero."""

    hero_id: str
    room_id: str
    required_category: str
    required_style_tags: tuple[str, ...]
    minimum_dimensions_m: tuple[float, float, float]
    maximum_dimensions_m: tuple[float, float, float]
    minimum_texture_size_px: int = 2048
    required_texture_semantics: tuple[str, ...] = (
        "base_color",
        "normal",
        "roughness",
    )


@dataclass(frozen=True)
class LocalAssetCandidate:
    """One explicitly nominated local source; no discovery is performed."""

    candidate_id: str
    target_hero_id: str
    source_spec: AssetSourceSpec
    declared_category: str
    semantic_aliases: tuple[str, ...]
    style_tags: tuple[str, ...]


def _public_id(value: Any, label: str, *, maximum: int = 180) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or not _PUBLIC_ID_RE.fullmatch(value)
    ):
        _fail("VISTA_LOCAL_ASSET_METADATA_INVALID", f"{label} is not a normalized public ID")
    return value


def _tag(value: Any, label: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 64 or not _TAG_RE.fullmatch(value):
        _fail("VISTA_LOCAL_ASSET_METADATA_INVALID", f"{label} is not a normalized tag")
    return value


def _unique_tags(value: Any, label: str, *, minimum: int = 0) -> tuple[str, ...]:
    if type(value) is not tuple or not minimum <= len(value) <= 32:
        _fail("VISTA_LOCAL_ASSET_METADATA_INVALID", f"{label} must be a bounded tuple")
    normalized = tuple(_tag(item, f"{label}[{index}]") for index, item in enumerate(value))
    if len(set(normalized)) != len(normalized):
        _fail("VISTA_LOCAL_ASSET_METADATA_INVALID", f"{label} contains duplicate tags")
    return tuple(sorted(normalized))


def _vector3(value: Any, label: str) -> tuple[float, float, float]:
    if type(value) is not tuple or len(value) != 3:
        _fail("VISTA_LOCAL_ASSET_METADATA_INVALID", f"{label} must be a three-number tuple")
    result: list[float] = []
    for item in value:
        if type(item) not in {int, float} or not math.isfinite(float(item)) or float(item) <= 0:
            _fail("VISTA_LOCAL_ASSET_METADATA_INVALID", f"{label} must contain finite positive values")
        result.append(float(item))
    return tuple(result)  # type: ignore[return-value]


def _validate_requirements(
    requirements: Sequence[HeroRequirement],
) -> tuple[HeroRequirement, ...]:
    if not requirements:
        _fail("VISTA_LOCAL_ASSET_REQUIREMENTS_EMPTY", "at least one hero requirement is required")
    seen: set[str] = set()
    normalized: list[HeroRequirement] = []
    for index, requirement in enumerate(requirements):
        if type(requirement) is not HeroRequirement:
            _fail("VISTA_LOCAL_ASSET_METADATA_INVALID", f"requirements[{index}] has the wrong type")
        hero_id = _public_id(requirement.hero_id, f"requirements[{index}].hero_id")
        room_id = _public_id(requirement.room_id, f"requirements[{index}].room_id")
        if room_id not in FINISHED_ROOM_IDS:
            _fail("VISTA_LOCAL_ASSET_ROOM_OUT_OF_SCOPE", f"{room_id} is not an r2 finished room")
        if not hero_id.startswith(f"{room_id}/entity."):
            _fail(
                "VISTA_LOCAL_ASSET_HERO_ROOM_MISMATCH",
                f"{hero_id} is not contained by its declared room",
            )
        if hero_id in seen:
            _fail("VISTA_LOCAL_ASSET_DUPLICATE_HERO", f"duplicate hero requirement: {hero_id}")
        seen.add(hero_id)
        category = _tag(requirement.required_category, f"requirements[{index}].required_category")
        styles = _unique_tags(
            requirement.required_style_tags,
            f"requirements[{index}].required_style_tags",
            minimum=1,
        )
        minimum = _vector3(
            requirement.minimum_dimensions_m,
            f"requirements[{index}].minimum_dimensions_m",
        )
        maximum = _vector3(
            requirement.maximum_dimensions_m,
            f"requirements[{index}].maximum_dimensions_m",
        )
        if any(low > high for low, high in zip(minimum, maximum)):
            _fail("VISTA_LOCAL_ASSET_DIMENSIONS_INVALID", f"{hero_id} has inverted dimension limits")
        size = requirement.minimum_texture_size_px
        if type(size) is not int or not 256 <= size <= 8192:
            _fail("VISTA_LOCAL_ASSET_PBR_INVALID", f"{hero_id} has an invalid texture threshold")
        semantics = _unique_tags(
            requirement.required_texture_semantics,
            f"requirements[{index}].required_texture_semantics",
            minimum=1,
        )
        if not _PBR_BASELINE.issubset(semantics):
            _fail(
                "VISTA_LOCAL_ASSET_PBR_INVALID",
                f"{hero_id} must require base colour, normal, and roughness",
            )
        normalized.append(
            HeroRequirement(
                hero_id=hero_id,
                room_id=room_id,
                required_category=category,
                required_style_tags=styles,
                minimum_dimensions_m=minimum,
                maximum_dimensions_m=maximum,
                minimum_texture_size_px=size,
                required_texture_semantics=semantics,
            )
        )
    room_order = {room_id: index for index, room_id in enumerate(FINISHED_ROOM_IDS)}
    return tuple(sorted(normalized, key=lambda item: (room_order[item.room_id], item.hero_id)))


def _validate_candidates(
    candidates: Sequence[LocalAssetCandidate],
    requirement_ids: frozenset[str],
) -> tuple[LocalAssetCandidate, ...]:
    if not candidates:
        return ()
    seen: set[str] = set()
    normalized: list[LocalAssetCandidate] = []
    for index, candidate in enumerate(candidates):
        if type(candidate) is not LocalAssetCandidate:
            _fail("VISTA_LOCAL_ASSET_METADATA_INVALID", f"candidates[{index}] has the wrong type")
        if type(candidate.source_spec) is not AssetSourceSpec:
            _fail(
                "VISTA_LOCAL_ASSET_METADATA_INVALID",
                f"candidates[{index}].source_spec has the wrong type",
            )
        candidate_id = _public_id(candidate.candidate_id, f"candidates[{index}].candidate_id")
        hero_id = _public_id(candidate.target_hero_id, f"candidates[{index}].target_hero_id")
        if candidate_id in seen:
            _fail("VISTA_LOCAL_ASSET_DUPLICATE_CANDIDATE", f"duplicate candidate: {candidate_id}")
        seen.add(candidate_id)
        if hero_id not in requirement_ids:
            _fail("VISTA_LOCAL_ASSET_TARGET_UNKNOWN", f"candidate targets unknown hero: {hero_id}")
        category = _tag(candidate.declared_category, f"candidates[{index}].declared_category")
        aliases = _unique_tags(candidate.semantic_aliases, f"candidates[{index}].semantic_aliases")
        styles = _unique_tags(
            candidate.style_tags,
            f"candidates[{index}].style_tags",
            minimum=1,
        )
        provider = candidate.source_spec.provider
        if provider not in LOCAL_ONLY_PROVIDERS:
            _fail(
                "VISTA_LOCAL_ASSET_PROVIDER_PROHIBITED",
                f"{candidate_id} uses non-local-only provider {provider}",
            )
        normalized.append(
            LocalAssetCandidate(
                candidate_id=candidate_id,
                target_hero_id=hero_id,
                source_spec=candidate.source_spec,
                declared_category=category,
                semantic_aliases=aliases,
                style_tags=styles,
            )
        )
    return tuple(sorted(normalized, key=lambda item: item.candidate_id))


def _dimensions(receipt: Mapping[str, Any]) -> tuple[float, float, float]:
    bounds = receipt["metric_bounds_m"]
    return tuple(
        round(float(high) - float(low), 6)
        for low, high in zip(bounds["min_m"], bounds["max_m"])
    )  # type: ignore[return-value]


def _is_forbidden_substitution(required: str, declared: str) -> bool:
    return frozenset({required, declared}) in _FORBIDDEN_CATEGORY_SUBSTITUTIONS


def _category_gate(requirement: HeroRequirement, candidate: LocalAssetCandidate) -> dict[str, Any]:
    exact = candidate.declared_category == requirement.required_category
    alias_claimed = requirement.required_category in candidate.semantic_aliases
    forbidden = _is_forbidden_substitution(
        requirement.required_category,
        candidate.declared_category,
    )
    if exact:
        reasons: list[str] = []
    elif forbidden:
        reasons = ["CATEGORY_ALIAS_SUBSTITUTION_FORBIDDEN"]
    elif alias_claimed:
        reasons = ["CATEGORY_ALIAS_ONLY_NOT_ACCEPTED"]
    else:
        reasons = ["CATEGORY_EXACT_MATCH_REQUIRED"]
    return {
        "required_category": requirement.required_category,
        "declared_category": candidate.declared_category,
        "semantic_aliases": list(candidate.semantic_aliases),
        "exact_match": exact,
        "forbidden_alias_substitution": forbidden,
        "suitable": exact,
        "reason_codes": reasons,
    }


def _style_gate(requirement: HeroRequirement, candidate: LocalAssetCandidate) -> dict[str, Any]:
    missing = sorted(set(requirement.required_style_tags) - set(candidate.style_tags))
    return {
        "required_tags": list(requirement.required_style_tags),
        "candidate_tags": list(candidate.style_tags),
        "missing_tags": missing,
        "suitable": not missing,
        "reason_codes": [] if not missing else ["STYLE_REQUIRED_TAGS_MISSING"],
    }


def _dimension_gate(requirement: HeroRequirement, receipt: Mapping[str, Any]) -> dict[str, Any]:
    actual = _dimensions(receipt)
    suitable = all(
        low <= value <= high
        for low, value, high in zip(
            requirement.minimum_dimensions_m,
            actual,
            requirement.maximum_dimensions_m,
        )
    )
    return {
        "actual_dimensions_m": list(actual),
        "minimum_dimensions_m": list(requirement.minimum_dimensions_m),
        "maximum_dimensions_m": list(requirement.maximum_dimensions_m),
        "suitable": suitable,
        "reason_codes": [] if suitable else ["DIMENSIONS_OUT_OF_RANGE"],
    }


def _license_gate(
    use_context: str,
    provider: str,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    license_receipt = receipt["license"]
    commercial_use = license_receipt["commercial_use"]
    suitable = use_context == PRIVATE_RESEARCH_DEMO or commercial_use == "allowed"
    return {
        "use_context": use_context,
        "provider": provider,
        "license_id": license_receipt["license_id"],
        "entitlement_status": license_receipt["entitlement_status"],
        "commercial_use": commercial_use,
        "redistribution_restriction": license_receipt["redistribution_restriction"],
        "suitable": suitable,
        "reason_codes": [] if suitable else ["LICENSE_CONTEXT_INCOMPATIBLE"],
    }


def _pbr_gate(requirement: HeroRequirement, receipt: Mapping[str, Any]) -> dict[str, Any]:
    slots = receipt["material_inventory"]["slots"]
    opaque_slots = [slot for slot in slots if slot["blend_mode"] == "opaque"]
    required = set(requirement.required_texture_semantics)
    incomplete = sorted(
        slot["slot_id"]
        for slot in opaque_slots
        if not required.issubset(slot["texture_semantics"])
    )
    undersized = sorted(
        slot["slot_id"]
        for slot in opaque_slots
        if slot["minimum_texture_size_px"] < requirement.minimum_texture_size_px
    )
    reasons: list[str] = []
    if not opaque_slots:
        reasons.append("PBR_OPAQUE_SLOT_REQUIRED")
    if incomplete:
        reasons.append("PBR_REQUIRED_SEMANTICS_MISSING")
    if undersized:
        reasons.append("PBR_SCREEN_RESOLUTION_TOO_LOW")
    return {
        "required_texture_semantics": list(requirement.required_texture_semantics),
        "minimum_texture_size_px": requirement.minimum_texture_size_px,
        "opaque_slot_ids": sorted(slot["slot_id"] for slot in opaque_slots),
        "incomplete_slot_ids": incomplete,
        "undersized_slot_ids": undersized,
        "all_primitives_material_bound": receipt["material_inventory"][
            "all_primitives_material_bound"
        ],
        "suitable": not reasons,
        "reason_codes": reasons,
    }


def _evaluate_candidate(
    requirement: HeroRequirement,
    candidate: LocalAssetCandidate,
    allowed_roots: Sequence[AllowedSourceRoot],
    use_context: str,
) -> dict[str, Any]:
    try:
        resolution = resolve_asset_source(candidate.source_spec, allowed_roots)
    except AssetSourceResolutionError as error:
        raise LocalAssetCatalogError(
            "VISTA_LOCAL_ASSET_SOURCE_REJECTED",
            f"{candidate.candidate_id} failed source resolution: {error.code}",
        ) from error
    normalized = resolution.normalized_receipt
    receipt = normalized["asset_source_receipt"]
    gates = {
        "category": _category_gate(requirement, candidate),
        "style": _style_gate(requirement, candidate),
        "dimensions": _dimension_gate(requirement, receipt),
        "license": _license_gate(use_context, candidate.source_spec.provider, receipt),
        "pbr_screen": _pbr_gate(requirement, receipt),
    }
    eligible = all(gate["suitable"] is True for gate in gates.values())
    reasons = sorted(
        reason
        for gate in gates.values()
        for reason in gate["reason_codes"]
    )
    source_evidence = normalized["source_evidence"]
    return {
        "candidate_id": candidate.candidate_id,
        "target_hero_id": candidate.target_hero_id,
        "source_identity": {
            "provider": candidate.source_spec.provider,
            "source_root_id": source_evidence["source_root_id"],
            "source_relative_path": source_evidence["source_relative_path"],
            "source_uri": receipt["source_uri"],
            "source_digest": receipt["source_digest"],
            "catalog_identity": normalized["provenance"]["catalog_identity"],
        },
        "gates": gates,
        "automated_gate_status": "eligible_for_visual_review" if eligible else "rejected",
        "automated_reject_reason_codes": reasons,
        "visual_review_status": "not_performed",
        "visual_accepted": False,
    }


def _assert_public_document(document: Mapping[str, Any], label: str) -> None:
    serialized = canonical_json_bytes(document).decode("utf-8").lower()
    if any(marker in serialized for marker in _PRIVATE_PATH_MARKERS):
        _fail("VISTA_LOCAL_ASSET_PRIVATE_PATH_LEAK", f"{label} contains a private path")

    def visit(value: Any) -> None:
        if type(value) is str and (value.startswith("/") or "\\" in value):
            _fail("VISTA_LOCAL_ASSET_PRIVATE_PATH_LEAK", f"{label} contains path syntax")
        if type(value) is dict:
            for nested in value.values():
                visit(nested)
        elif type(value) is list:
            for nested in value:
                visit(nested)

    visit(document)


def evaluate_local_hero_coverage(
    requirements: Sequence[HeroRequirement],
    candidates: Sequence[LocalAssetCandidate],
    allowed_roots: Sequence[AllowedSourceRoot],
    *,
    use_context: str = PRIVATE_RESEARCH_DEMO,
) -> dict[str, Any]:
    """Return a deterministic diagnostic matrix without weakening promotion.

    Use :func:`audit_local_hero_coverage` for the fail-closed promotion gate.
    This diagnostic form exists so an incomplete local library can still yield
    an honest missing-coverage report before any external acquisition request.
    """

    if use_context not in SUPPORTED_USE_CONTEXTS:
        _fail("VISTA_LOCAL_ASSET_USE_CONTEXT_INVALID", "unsupported use context")
    normalized_requirements = _validate_requirements(requirements)
    by_id = {requirement.hero_id: requirement for requirement in normalized_requirements}
    normalized_candidates = _validate_candidates(candidates, frozenset(by_id))
    evaluations_by_hero: dict[str, list[dict[str, Any]]] = {
        requirement.hero_id: [] for requirement in normalized_requirements
    }
    for candidate in normalized_candidates:
        evaluations_by_hero[candidate.target_hero_id].append(
            _evaluate_candidate(
                by_id[candidate.target_hero_id],
                candidate,
                allowed_roots,
                use_context,
            )
        )

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    eligible_total = 0
    for requirement in normalized_requirements:
        evaluations = evaluations_by_hero[requirement.hero_id]
        eligible_ids = sorted(
            evaluation["candidate_id"]
            for evaluation in evaluations
            if evaluation["automated_gate_status"] == "eligible_for_visual_review"
        )
        eligible_total += len(eligible_ids)
        if not eligible_ids:
            missing.append(requirement.hero_id)
        rows.append(
            {
                "room_id": requirement.room_id,
                "hero_id": requirement.hero_id,
                "requirement": {
                    "category": requirement.required_category,
                    "style_tags": list(requirement.required_style_tags),
                    "minimum_dimensions_m": list(requirement.minimum_dimensions_m),
                    "maximum_dimensions_m": list(requirement.maximum_dimensions_m),
                    "minimum_texture_size_px": requirement.minimum_texture_size_px,
                    "required_texture_semantics": list(
                        requirement.required_texture_semantics
                    ),
                },
                "candidate_evaluations": evaluations,
                "eligible_for_visual_review_candidate_ids": eligible_ids,
                "automated_coverage_status": (
                    "ready_for_visual_review" if eligible_ids else "missing"
                ),
                "visual_review_status": "not_performed",
                "visual_accepted_candidate_id": None,
            }
        )

    matrix: dict[str, Any] = {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "use_context": use_context,
        "finished_room_ids": list(FINISHED_ROOM_IDS),
        "coverage_rows": rows,
        "summary": {
            "required_hero_count": len(rows),
            "resolved_candidate_count": len(normalized_candidates),
            "eligible_for_visual_review_candidate_count": eligible_total,
            "missing_hero_ids": missing,
            "automated_coverage_status": "complete" if not missing else "incomplete",
            "visual_review_status": "not_performed",
            "visual_accepted": False,
        },
    }
    matrix["content_digest"] = content_digest(matrix, "content_digest")
    _assert_public_document(matrix, "coverage matrix")
    return matrix


def _validate_coverage_matrix(matrix: Mapping[str, Any]) -> list[str]:
    if type(matrix) is not dict or matrix.get("schema_version") != COVERAGE_SCHEMA_VERSION:
        _fail("VISTA_LOCAL_ASSET_COVERAGE_INVALID", "coverage matrix schema is invalid")
    digest = matrix.get("content_digest")
    if type(digest) is not str or digest != content_digest(matrix, "content_digest"):
        _fail("VISTA_LOCAL_ASSET_COVERAGE_INVALID", "coverage matrix digest is invalid")
    missing = matrix.get("summary", {}).get("missing_hero_ids")
    if (
        type(missing) is not list
        or any(type(item) is not str for item in missing)
        or len(missing) != len(set(missing))
    ):
        _fail("VISTA_LOCAL_ASSET_COVERAGE_INVALID", "coverage summary is invalid")
    rows = matrix.get("coverage_rows")
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        _fail("VISTA_LOCAL_ASSET_COVERAGE_INVALID", "coverage rows are invalid")
    expected_missing = [
        row.get("hero_id")
        for row in rows
        if row.get("automated_coverage_status") == "missing"
    ]
    if missing != expected_missing:
        _fail("VISTA_LOCAL_ASSET_COVERAGE_INVALID", "missing hero summary disagrees with rows")
    _assert_public_document(matrix, "coverage matrix")
    return missing


def require_complete_coverage(matrix: Mapping[str, Any]) -> None:
    """Fail closed unless every required hero has an automated candidate."""

    missing = _validate_coverage_matrix(matrix)
    if missing:
        _fail(
            "VISTA_LOCAL_ASSET_HERO_COVERAGE_MISSING",
            "one or more required heroes lack an automated-gate candidate",
            coverage_matrix=matrix,
        )


def audit_local_hero_coverage(
    requirements: Sequence[HeroRequirement],
    candidates: Sequence[LocalAssetCandidate],
    allowed_roots: Sequence[AllowedSourceRoot],
    *,
    use_context: str = PRIVATE_RESEARCH_DEMO,
) -> dict[str, Any]:
    """Build the deterministic matrix and enforce complete hero coverage."""

    matrix = evaluate_local_hero_coverage(
        requirements,
        candidates,
        allowed_roots,
        use_context=use_context,
    )
    require_complete_coverage(matrix)
    return matrix


def build_contact_sheet_plan(matrix: Mapping[str, Any]) -> dict[str, Any]:
    """Compile public diagnostic jobs while retaining promotion as fail-closed.

    An incomplete matrix may still render its explicitly listed candidates so
    the gap report has visual evidence.  ``promotion_eligible`` remains false,
    and :func:`audit_local_hero_coverage` / :func:`require_complete_coverage`
    still reject that same matrix.
    """

    missing = _validate_coverage_matrix(matrix)
    jobs: list[dict[str, Any]] = []
    for row in matrix["coverage_rows"]:
        for evaluation in row["candidate_evaluations"]:
            gates = evaluation["gates"]
            jobs.append(
                {
                    "job_id": f"contact.{evaluation['candidate_id']}",
                    "room_id": row["room_id"],
                    "hero_id": row["hero_id"],
                    "candidate_id": evaluation["candidate_id"],
                    "source_identity": copy.deepcopy(evaluation["source_identity"]),
                    "views": ["front", "three_quarter", "side"],
                    "background": "neutral_midgray",
                    "labels": {
                        "required_category": gates["category"]["required_category"],
                        "declared_category": gates["category"]["declared_category"],
                        "style_tags": gates["style"]["candidate_tags"],
                        "actual_dimensions_m": gates["dimensions"]["actual_dimensions_m"],
                        "license_id": gates["license"]["license_id"],
                        "pbr_opaque_slot_ids": gates["pbr_screen"]["opaque_slot_ids"],
                        "automated_gate_status": evaluation["automated_gate_status"],
                        "automated_reject_reason_codes": evaluation[
                            "automated_reject_reason_codes"
                        ],
                    },
                    "review": {
                        "status": "pending_human_review",
                        "required_checks": [
                            "category",
                            "style",
                            "dimensions",
                            "material_response",
                            "screen_suitability",
                        ],
                        "decision": None,
                    },
                }
            )
    jobs.sort(key=lambda job: (job["room_id"], job["hero_id"], job["candidate_id"]))
    plan: dict[str, Any] = {
        "schema_version": CONTACT_SHEET_PLAN_SCHEMA_VERSION,
        "coverage_digest": matrix["content_digest"],
        "render_jobs": jobs,
        "missing_hero_ids": copy.deepcopy(missing),
        "promotion_eligible": not missing,
        "visual_review_status": "not_performed",
        "visual_accepted": False,
    }
    plan["content_digest"] = content_digest(plan, "content_digest")
    _assert_public_document(plan, "contact-sheet plan")
    return plan


__all__ = [
    "COMMERCIAL_RELEASE",
    "CONTACT_SHEET_PLAN_SCHEMA_VERSION",
    "COVERAGE_SCHEMA_VERSION",
    "FINISHED_ROOM_IDS",
    "HeroRequirement",
    "LOCAL_ONLY_PROVIDERS",
    "LocalAssetCandidate",
    "LocalAssetCatalogError",
    "PRIVATE_RESEARCH_DEMO",
    "ROOM_ENTRY",
    "ROOM_KITCHEN",
    "ROOM_LIVING",
    "audit_local_hero_coverage",
    "build_contact_sheet_plan",
    "evaluate_local_hero_coverage",
    "require_complete_coverage",
]
