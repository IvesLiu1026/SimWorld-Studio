"""Pure configuration and normalization helpers for the r2 Blender forge."""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping, Sequence


EXPECTED_BLENDER_VERSION = (4, 5, 8)
FORGE_SCHEMA_VERSION = "simworld.vista.playable-home-realism-forge/v1"
EXPECTED_HOUSE_REVISION = "vista_playable_home_r1"
FINISHED_ROOM_KINDS = ("entry_hall", "living_room", "kitchen_dining")
DEFAULT_WALL_THICKNESS_M = 0.18
DEFAULT_BASEBOARD_HEIGHT_M = 0.12
DEFAULT_BASEBOARD_DEPTH_M = 0.025
DEFAULT_TRIM_WIDTH_M = 0.075
# Production manifests default to 512 px.  Headless smoke tests may explicitly
# request 64 px, and receipts label that output ``smoke_only``.
DEFAULT_TEXTURE_SIZE_PX = 512
GRID_M = 0.005


class ForgeInputError(ValueError):
    """Raised when a source contract cannot safely drive the forge."""


def _normalize(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ForgeInputError("normalized values must be finite")
        rounded = round(value, 6)
        if rounded == 0.0:
            return 0
        return int(rounded) if rounded.is_integer() else rounded
    return value


def normalized(value: Any) -> Any:
    """Return a recursively sorted, finite, six-decimal representation."""

    return _normalize(value)


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def vector3(values: Sequence[Any], *, field: str) -> tuple[float, float, float]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or len(values) != 3:
        raise ForgeInputError(f"{field} must contain exactly three numbers")
    result = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in result):
        raise ForgeInputError(f"{field} must be finite")
    return result  # type: ignore[return-value]


def require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ForgeInputError(f"{field} must be an object")
    return value


def load_json_object(path: pathlib.Path, *, label: str) -> dict[str, Any]:
    if not path.is_absolute():
        raise ForgeInputError(f"{label} path must be absolute")
    if path.is_symlink() or not path.is_file():
        raise ForgeInputError(f"{label} must be a regular non-symlink file: {path}")
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ForgeInputError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, json.JSONDecodeError) as exc:
        raise ForgeInputError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ForgeInputError(f"{label} must contain one JSON object")
    return value


def validate_source_contracts(house: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
    for label, source in (("HouseSpec", house), ("VisualProfile", profile)):
        declared = source.get("content_digest")
        if declared is None:
            continue
        if not isinstance(declared, str) or len(declared) != 64:
            raise ForgeInputError(f"{label} content_digest must be a SHA-256 hex digest")
        body = {key: source[key] for key in source if key != "content_digest"}
        actual = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if declared != actual:
            raise ForgeInputError(f"{label} content_digest mismatch")
    revision = house.get("revision")
    if revision != EXPECTED_HOUSE_REVISION:
        raise ForgeInputError(
            f"HouseSpec revision must be {EXPECTED_HOUSE_REVISION!r}, got {revision!r}"
        )
    if profile.get("house_revision") != revision:
        raise ForgeInputError("VisualProfile house_revision does not match HouseSpec")
    if not isinstance(profile.get("seed"), int) or isinstance(profile.get("seed"), bool):
        raise ForgeInputError("VisualProfile seed must be an integer")
    rooms = house.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        raise ForgeInputError("HouseSpec rooms must be a non-empty list")
    room_ids = {room.get("room_id") for room in rooms if isinstance(room, Mapping)}
    kind_to_id = {
        room.get("kind"): room.get("room_id")
        for room in rooms
        if isinstance(room, Mapping) and isinstance(room.get("kind"), str)
    }
    missing_kinds = [kind for kind in FINISHED_ROOM_KINDS if kind not in kind_to_id]
    if missing_kinds:
        raise ForgeInputError(f"HouseSpec is missing finished room kinds: {missing_kinds}")
    finished = profile.get("finished_room_ids")
    if not isinstance(finished, list) or any(not isinstance(item, str) for item in finished):
        raise ForgeInputError("VisualProfile finished_room_ids must be a string list")
    required = {kind_to_id[kind] for kind in FINISHED_ROOM_KINDS}
    unknown = sorted(set(finished) - room_ids)
    if unknown:
        raise ForgeInputError(f"VisualProfile contains unknown finished room IDs: {unknown}")
    if not required.issubset(set(finished)):
        raise ForgeInputError("VisualProfile does not finish entry hall, living room, and kitchen/dining")
    architecture_profile = profile.get("architecture_profile", {})
    if not isinstance(architecture_profile, Mapping):
        raise ForgeInputError("VisualProfile architecture_profile must be an object")


def prepare_output_root(path: pathlib.Path) -> pathlib.Path:
    """Create one empty, absolute, non-symlink append-only output root."""

    if not path.is_absolute():
        raise ForgeInputError("--output-root must be absolute")
    if path.is_symlink():
        raise ForgeInputError("--output-root may not be a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if any(resolved.iterdir()):
        raise ForgeInputError(f"refusing non-empty append-only output root: {resolved}")
    return resolved


def profile_value(
    profile: Mapping[str, Any],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    architecture = require_mapping(profile.get("architecture_profile", {}), field="architecture_profile")
    raw = architecture.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ForgeInputError(f"architecture_profile.{key} must be numeric") from exc
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ForgeInputError(
            f"architecture_profile.{key} must be between {minimum} and {maximum}"
        )
    return value
