"""Fail-closed validation for VISTA Playable Home visual profiles.

This module is deliberately pure: it performs no Blender, Unreal, network, or
filesystem mutation.  Runtime and asset tools can validate the same pinned
profile before creating an append-only attempt.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import pathlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

import jsonschema


SCHEMA_VERSION = "simworld.vista.playable-home-visual-profile/v1"
SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "vista-playable-home-visual-profile-v1.schema.json"
EXPECTED_FINISHED_ROOMS = frozenset(
    {
        "home.r1/room.entry_hall",
        "home.r1/room.living_room",
        "home.r1/room.kitchen_dining",
    }
)
REQUIRED_EVENT_ENTITIES = frozenset(
    {
        "home.r1/room.kitchen_dining/entity.stove.01",
        "home.r1/room.living_room/entity.keys.01",
        "home.r1/room.bedroom/entity.phone.01",
    }
)
PROHIBITED_KEYS = frozenset(
    {
        "execute_python_script",
        "python_code",
        "shell_command",
        "caller_script",
        "render_script",
        "blueprint_graph",
        "filesystem_write",
        "auth_token",
        "access_token",
        "oracle_label",
        "oracle_assistance_required",
        "private_evidence",
        "review_notes",
    }
)
SAFE_URI_SCHEMES = frozenset(
    {
        "project",
        "catalog",
        "hssd",
        "ycb",
        "polyhaven",
        "fab",
        "registry",
        "project-owned",
        "local-audit",
        "account-entitlement",
    }
)


@dataclass(frozen=True)
class VisualProfileContractError(Exception):
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


def _fail(code: str, path: str, message: str) -> None:
    raise VisualProfileContractError(code, path, message)


def canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VisualProfileContractError(
            "VISTA_VISUAL_CANONICAL_JSON_INVALID", "$", "Document is not finite canonical JSON"
        ) from exc
    return text.encode("utf-8", "strict")


def content_digest(value: Mapping[str, Any], field: str = "content_digest") -> str:
    body = copy.deepcopy(dict(value))
    body.pop(field, None)
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def seal_document(value: Mapping[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(value))
    receipts = sealed.get("asset_source_receipts")
    if isinstance(receipts, list):
        for receipt in receipts:
            if isinstance(receipt, dict):
                receipt["receipt_digest"] = content_digest(receipt, "receipt_digest")
    sealed["content_digest"] = content_digest(sealed)
    return sealed


def _reject_constant(value: str) -> None:
    _fail("VISTA_VISUAL_JSON_NON_FINITE", "$", f"JSON constant {value!r} is prohibited")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("VISTA_VISUAL_DUPLICATE_KEY", "$", "Duplicate JSON object key is prohibited")
        result[key] = value
    return result


def load_json(path: pathlib.Path | str) -> dict[str, Any]:
    source = pathlib.Path(path)
    try:
        parsed = json.loads(
            source.read_text(encoding="utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except VisualProfileContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VisualProfileContractError(
            "VISTA_VISUAL_JSON_INVALID", "$", "Input is not strict UTF-8 JSON"
        ) from exc
    if type(parsed) is not dict:
        _fail("VISTA_VISUAL_JSON_INVALID", "$", "Top-level JSON value must be an object")
    _assert_finite(parsed)
    return parsed


def _assert_finite(value: Any, path: str = "$", depth: int = 0) -> None:
    if depth > 96:
        _fail("VISTA_VISUAL_JSON_TOO_DEEP", path, "JSON nesting exceeds the limit")
    if type(value) is float and not math.isfinite(value):
        _fail("VISTA_VISUAL_JSON_NON_FINITE", path, "Non-finite numbers are prohibited")
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                _fail("VISTA_VISUAL_JSON_INVALID", path, "Object keys must be strings")
            _assert_finite(child, f"{path}.{key}", depth + 1)
    elif type(value) is list:
        for index, child in enumerate(value):
            _assert_finite(child, f"{path}[{index}]", depth + 1)


def _scan_prohibited(value: Any, path: str = "$") -> None:
    if type(value) is dict:
        for key, child in value.items():
            normalized = key.strip().lower().replace("-", "_")
            if normalized in PROHIBITED_KEYS:
                _fail("VISTA_VISUAL_PROHIBITED_FIELD", f"{path}.{key}", "Executable or private field is prohibited")
            _scan_prohibited(child, f"{path}.{key}")
    elif type(value) is list:
        for index, child in enumerate(value):
            _scan_prohibited(child, f"{path}[{index}]")
    elif type(value) is str and value.strip().lower().startswith(("/home/", "/root/", "/mnt/", "/nas/", "file://")):
        _fail("VISTA_VISUAL_PRIVATE_PATH_PROHIBITED", path, "Absolute private path is prohibited")


def _schema() -> dict[str, Any]:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        raise VisualProfileContractError(
            "VISTA_VISUAL_SCHEMA_UNAVAILABLE", "$", "Pinned visual-profile schema is unavailable"
        ) from exc
    return schema


def _json_path(error: jsonschema.ValidationError) -> str:
    path = "$"
    for part in error.absolute_path:
        path += f"[{part}]" if type(part) is int else f".{part}"
    return path


def _validate_schema(profile: Mapping[str, Any]) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(_schema()).iter_errors(profile),
        key=lambda error: (tuple(str(part) for part in error.absolute_path), error.validator or "", error.message),
    )
    if errors:
        error = errors[0]
        _fail("VISTA_VISUAL_SCHEMA_INVALID", _json_path(error), f"Schema constraint {error.validator!r} failed")


def _require_unique(values: Iterable[str], path: str, label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            _fail("VISTA_VISUAL_DUPLICATE_ID", path, f"Duplicate {label}")
        seen.add(value)


def _safe_uri(uri: str, path: str) -> None:
    lowered = uri.lower()
    if "\\" in uri or "%" in uri or "//." in lowered:
        _fail("VISTA_VISUAL_URI_UNSAFE", path, "Encoded, backslash, or hidden path syntax is prohibited")
    parsed = urlsplit(uri)
    if parsed.scheme not in SAFE_URI_SCHEMES or not parsed.netloc or parsed.query or parsed.fragment:
        _fail("VISTA_VISUAL_URI_UNSAFE", path, "URI scheme, authority, query, or fragment is invalid")
    if any(segment in {"", ".", ".."} for segment in [parsed.netloc, *parsed.path.split("/")] if segment != ""):
        _fail("VISTA_VISUAL_URI_UNSAFE", path, "URI traversal is prohibited")


def _validate_receipts(profile: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    receipts = profile["asset_source_receipts"]
    _require_unique((item["receipt_id"] for item in receipts), "$.asset_source_receipts", "source receipt ID")
    _require_unique((item["logical_asset_id"] for item in receipts), "$.asset_source_receipts", "logical asset ID")
    indexed = {item["receipt_id"]: item for item in receipts}
    for index, receipt in enumerate(receipts):
        if receipt["receipt_digest"] != content_digest(receipt, "receipt_digest"):
            _fail("VISTA_VISUAL_RECEIPT_DIGEST_MISMATCH", f"$.asset_source_receipts[{index}].receipt_digest", "Receipt digest mismatch")
        _safe_uri(receipt["source_uri"], f"$.asset_source_receipts[{index}].source_uri")
        _safe_uri(receipt["license"]["entitlement_record"], f"$.asset_source_receipts[{index}].license.entitlement_record")
        bounds = receipt["metric_bounds_m"]
        if any(low >= high for low, high in zip(bounds["min_m"], bounds["max_m"])):
            _fail("VISTA_VISUAL_BOUNDS_INVALID", f"$.asset_source_receipts[{index}].metric_bounds_m", "Bounds minima must be below maxima")
    return indexed


def validate_profile(profile: Mapping[str, Any], house: Mapping[str, Any]) -> None:
    """Validate schema, digest, identity, licensing and presentation-only links."""

    _assert_finite(profile)
    _scan_prohibited(profile)
    _validate_schema(profile)
    if profile["content_digest"] != content_digest(profile):
        _fail("VISTA_VISUAL_DIGEST_MISMATCH", "$.content_digest", "Profile digest mismatch")
    if profile["house_revision"] != house.get("revision"):
        _fail("VISTA_VISUAL_STALE_HOUSE_REVISION", "$.house_revision", "Profile targets a different house revision")
    if profile["provenance"]["source_house_content_digest"] != house.get("content_digest"):
        _fail("VISTA_VISUAL_STALE_HOUSE_DIGEST", "$.provenance.source_house_content_digest", "Profile targets different house content")

    rooms = {room["room_id"]: room for room in house["rooms"]}
    room_ids = set(rooms)
    entity_ids = {entity["entity_id"] for entity in house["entities"]}

    def point_is_inside_room(point_cm: Iterable[float], room_id: str) -> bool:
        room = rooms[room_id]
        location = room["transform"]["location_m"]
        scale = room["transform"]["scale"]
        bounds = room["bounds_m"]
        minimum_cm = [(origin + low * factor) * 100 for origin, low, factor in zip(location, bounds["min_m"], scale)]
        maximum_cm = [(origin + high * factor) * 100 for origin, high, factor in zip(location, bounds["max_m"], scale)]
        return all(low <= value <= high for value, low, high in zip(point_cm, minimum_cm, maximum_cm))

    finished = set(profile["finished_room_ids"])
    compatibility = set(profile["compatibility_room_ids"])
    if finished != EXPECTED_FINISHED_ROOMS or finished & compatibility or finished | compatibility != room_ids:
        _fail("VISTA_VISUAL_ROOM_SCOPE_INVALID", "$.finished_room_ids", "Finished and compatibility rooms must partition r1")
    if set(profile["architecture_profile"]["finished_room_ids"]) != finished:
        _fail("VISTA_VISUAL_ROOM_SCOPE_INVALID", "$.architecture_profile.finished_room_ids", "Architecture room scope differs")

    receipts = _validate_receipts(profile)
    architecture_receipt = profile["architecture_profile"]["source_receipt_id"]
    if architecture_receipt not in receipts:
        _fail("VISTA_VISUAL_SOURCE_UNKNOWN", "$.architecture_profile.source_receipt_id", "Architecture source receipt is unknown")

    bindings = profile["semantic_visual_bindings"]
    _require_unique((item["binding_id"] for item in bindings), "$.semantic_visual_bindings", "binding ID")
    _require_unique((item["target_entity_id"] for item in bindings), "$.semantic_visual_bindings", "binding target")
    for index, binding in enumerate(bindings):
        if binding["target_entity_id"] not in entity_ids:
            _fail("VISTA_VISUAL_TARGET_UNKNOWN", f"$.semantic_visual_bindings[{index}].target_entity_id", "Semantic target is unknown")
        receipt = receipts.get(binding["source_receipt_id"])
        if receipt is None or receipt["logical_asset_id"] != binding["logical_asset_id"]:
            _fail("VISTA_VISUAL_SOURCE_UNKNOWN", f"$.semantic_visual_bindings[{index}].source_receipt_id", "Binding source is missing or mismatched")

    dressing = profile["dressing_instances"]
    _require_unique((item["instance_id"] for item in dressing), "$.dressing_instances", "dressing instance ID")
    for index, instance in enumerate(dressing):
        receipt = receipts.get(instance["source_receipt_id"])
        if instance["room_id"] not in finished:
            _fail("VISTA_VISUAL_DRESSING_ROOM_INVALID", f"$.dressing_instances[{index}].room_id", "r2 dressing must stay in finished rooms")
        if receipt is None or receipt["logical_asset_id"] != instance["logical_asset_id"]:
            _fail("VISTA_VISUAL_SOURCE_UNKNOWN", f"$.dressing_instances[{index}].source_receipt_id", "Dressing source is missing or mismatched")

    shots = profile["review_shots"]
    _require_unique((item["shot_id"] for item in shots), "$.review_shots", "review shot ID")
    for room_id in finished:
        room_shots = [shot for shot in shots if shot["room_id"] == room_id]
        if {shot["purpose"] for shot in room_shots} != {"overview", "hero"}:
            _fail("VISTA_VISUAL_REVIEW_COVERAGE_INVALID", "$.review_shots", "Each finished room requires overview and hero shots")
    known_visual_ids = entity_ids | {item["instance_id"] for item in dressing}
    for index, shot in enumerate(shots):
        if shot["room_id"] not in finished or shot["eye_location_cm"] == shot["look_at_target_cm"]:
            _fail("VISTA_VISUAL_REVIEW_SHOT_INVALID", f"$.review_shots[{index}]", "Review shot room or look vector is invalid")
        for field in ("eye_location_cm", "look_at_target_cm"):
            if not point_is_inside_room(shot[field], shot["room_id"]):
                _fail("VISTA_VISUAL_REVIEW_SHOT_INVALID", f"$.review_shots[{index}].{field}", "Review point is outside its declared room")
        if not set(shot["expected_hero_ids"]).issubset(known_visual_ids):
            _fail("VISTA_VISUAL_REVIEW_SHOT_INVALID", f"$.review_shots[{index}].expected_hero_ids", "Review shot references an unknown hero")
        if shot["purpose"] == "overview" and len(shot["expected_hero_ids"]) < 3:
            _fail("VISTA_VISUAL_REVIEW_COVERAGE_INVALID", f"$.review_shots[{index}].expected_hero_ids", "Overview requires three room-defining assets")

    lights = profile["lighting_rig"]["practical_lights"]
    _require_unique((item["light_id"] for item in lights), "$.lighting_rig.practical_lights", "light ID")
    if {light["room_id"] for light in lights} != finished or {light["type"] for light in lights} == {"point"}:
        _fail("VISTA_VISUAL_LIGHTING_RIG_INVALID", "$.lighting_rig.practical_lights", "Finished rooms need non-uniform physical practical lights")
    for index, light in enumerate(lights):
        if not point_is_inside_room(light["location_cm"], light["room_id"]):
            _fail(
                "VISTA_VISUAL_LIGHTING_RIG_INVALID",
                f"$.lighting_rig.practical_lights[{index}].location_cm",
                "Practical light is outside its declared room",
            )
    exposure = profile["lighting_rig"]["gameplay_exposure"]
    if exposure["min_ev100"] >= exposure["max_ev100"]:
        _fail("VISTA_VISUAL_EXPOSURE_INVALID", "$.lighting_rig.gameplay_exposure", "Exposure range must increase")
    if not REQUIRED_EVENT_ENTITIES.issubset(set(profile["provenance"]["preserved_event_entity_ids"])):
        _fail("VISTA_VISUAL_EVENT_GROUNDING_INVALID", "$.provenance.preserved_event_entity_ids", "Required VISTA event targets are missing")


def load_and_validate_profile(path: pathlib.Path | str, house: Mapping[str, Any]) -> dict[str, Any]:
    profile = load_json(path)
    validate_profile(profile, house)
    return profile
