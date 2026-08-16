"""Provider-neutral, fail-closed source resolution for realistic home assets.

The resolver has two deliberately separate outputs:

* ``asset_source_receipt`` is the closed AssetSourceReceipt v1 payload accepted
  by the VISTA Playable Home VisualProfile contract.
* the surrounding resolution receipt records byte/tree size, the allowlisted
  source-root identity and provenance without serializing a private absolute
  filesystem path.

No provider adapter downloads, purchases, substitutes, or mutates an asset.
The requested local source must already exist beneath an explicitly allowlisted
root.  In particular, an unverified Fab entitlement fails instead of falling
back to another provider or placeholder geometry.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import pathlib
import re
import stat
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


RESOLUTION_SCHEMA_VERSION = "simworld.vista.playable-home-asset-source-resolution/v1"

PROVIDER_PROJECT_AUTHORED = "project_authored"
PROVIDER_EXISTING_LOCAL = "existing_local"
PROVIDER_HSSD = "hssd"
PROVIDER_YCB = "ycb"
PROVIDER_POLY_HAVEN = "poly_haven"
PROVIDER_FAB_GATED = "fab_gated"

SUPPORTED_PROVIDERS = frozenset(
    {
        PROVIDER_PROJECT_AUTHORED,
        PROVIDER_EXISTING_LOCAL,
        PROVIDER_HSSD,
        PROVIDER_YCB,
        PROVIDER_POLY_HAVEN,
        PROVIDER_FAB_GATED,
    }
)

_PROVIDER_SOURCE_KIND = {
    PROVIDER_PROJECT_AUTHORED: "project_authored",
    PROVIDER_EXISTING_LOCAL: "existing_local",
    PROVIDER_HSSD: "hssd",
    PROVIDER_YCB: "existing_local",
    PROVIDER_POLY_HAVEN: "existing_local",
    PROVIDER_FAB_GATED: "fab",
}
_PROVIDER_URI_SCHEME = {
    PROVIDER_PROJECT_AUTHORED: "project",
    PROVIDER_EXISTING_LOCAL: "catalog",
    PROVIDER_HSSD: "hssd",
    PROVIDER_YCB: "ycb",
    PROVIDER_POLY_HAVEN: "polyhaven",
    PROVIDER_FAB_GATED: "fab",
}

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ROOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_RELATIVE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_PATH_MARKERS = ("/home/", "/root/", "/mnt/", "/nas/", "file://")

_LICENSE_KEYS = frozenset(
    {
        "license_id",
        "license_url",
        "entitlement_status",
        "entitlement_record",
        "attribution",
        "modification_notice",
        "commercial_use",
        "redistribution_restriction",
    }
)
_MATERIAL_INVENTORY_KEYS = frozenset(
    {"slots", "texture_count", "all_primitives_material_bound"}
)
_MATERIAL_SLOT_KEYS = frozenset(
    {
        "slot_id",
        "shader_class",
        "blend_mode",
        "texture_semantics",
        "minimum_texture_size_px",
    }
)
_IMPORT_POLICY_KEYS = frozenset(
    {"nanite", "mobility", "lod_policy", "collision_policy"}
)
_TEXTURE_SEMANTICS = frozenset(
    {
        "base_color",
        "normal",
        "roughness",
        "metalness",
        "ao",
        "emissive",
        "opacity",
        "transmission",
    }
)


@dataclass(frozen=True)
class AssetSourceResolutionError(Exception):
    """A source, entitlement, provenance, or receipt gate failed closed."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _fail(code: str, message: str) -> None:
    raise AssetSourceResolutionError(code, message)


@dataclass(frozen=True)
class AllowedSourceRoot:
    """One private filesystem root and the providers authorized to use it."""

    root_id: str
    path: pathlib.Path
    providers: tuple[str, ...]


@dataclass(frozen=True)
class AssetSourceSpec:
    """Closed caller input for resolving one already-local presentation asset."""

    receipt_id: str
    logical_asset_id: str
    provider: str
    source_path: pathlib.Path
    source_version: str
    catalog_identity: str
    metric_bounds_m: Mapping[str, Any]
    license: Mapping[str, Any]
    material_inventory: Mapping[str, Any]
    import_policy: Mapping[str, Any]


@dataclass(frozen=True)
class AssetSourceResolution:
    """Immutable canonical JSON backing for one normalized resolution receipt."""

    _canonical_json: bytes

    @property
    def normalized_receipt(self) -> dict[str, Any]:
        return json.loads(self._canonical_json.decode("utf-8"))

    @property
    def asset_source_receipt(self) -> dict[str, Any]:
        return copy.deepcopy(self.normalized_receipt["asset_source_receipt"])

    @property
    def source_evidence(self) -> dict[str, Any]:
        return copy.deepcopy(self.normalized_receipt["source_evidence"])


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize finite JSON with the same canonical policy as VisualProfile."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, OverflowError, UnicodeError) as error:
        raise AssetSourceResolutionError(
            "VISTA_SOURCE_CANONICAL_JSON_INVALID",
            "source metadata is not finite canonical JSON",
        ) from error


def content_digest(value: Mapping[str, Any], digest_field: str) -> str:
    body = copy.deepcopy(dict(value))
    body.pop(digest_field, None)
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _closed_mapping(value: Mapping[str, Any], keys: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("VISTA_SOURCE_METADATA_INVALID", f"{label} must be a plain object")
    actual = frozenset(value)
    if actual != keys:
        _fail(
            "VISTA_SOURCE_METADATA_INVALID",
            f"{label} fields differ from the closed contract",
        )
    return copy.deepcopy(dict(value))


def _public_string(value: Any, label: str, *, minimum: int = 1, maximum: int = 500) -> str:
    if type(value) is not str or not minimum <= len(value) <= maximum or value != value.strip():
        _fail("VISTA_SOURCE_METADATA_INVALID", f"{label} is not a bounded normalized string")
    lowered = value.lower()
    if "\\" in value or any(marker in lowered for marker in _PRIVATE_PATH_MARKERS):
        _fail("VISTA_SOURCE_PRIVATE_PATH_PROHIBITED", f"{label} contains private path syntax")
    return value


def _safe_uri(
    value: Any,
    label: str,
    allowed_schemes: frozenset[str],
    *,
    maximum: int = 288,
) -> str:
    uri = _public_string(value, label, maximum=maximum)
    if "%" in uri or "@" in uri:
        _fail("VISTA_SOURCE_URI_UNSAFE", f"{label} contains encoded or account syntax")
    parsed = urlsplit(uri)
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or not _ROOT_ID_RE.fullmatch(parsed.netloc)
    ):
        _fail("VISTA_SOURCE_URI_UNSAFE", f"{label} is not an allowlisted opaque URI")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        _fail("VISTA_SOURCE_URI_UNSAFE", f"{label} contains traversal")
    if segments and not _RELATIVE_PATH_RE.fullmatch("/".join(segments)):
        _fail("VISTA_SOURCE_URI_UNSAFE", f"{label} has a non-normalized path")
    return uri


def _https_url(value: Any, label: str) -> str:
    url = _public_string(value, label, maximum=512)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or any(character.isspace() for character in url)
    ):
        _fail("VISTA_SOURCE_LICENSE_INVALID", f"{label} must be a public HTTPS URL")
    return url


def _validate_bounds(value: Mapping[str, Any]) -> dict[str, list[int | float]]:
    bounds = _closed_mapping(value, frozenset({"min_m", "max_m"}), "metric_bounds_m")

    def vector(field: str) -> list[int | float]:
        raw = bounds[field]
        if type(raw) is not list or len(raw) != 3:
            _fail("VISTA_SOURCE_BOUNDS_INVALID", f"metric_bounds_m.{field} must contain three numbers")
        result: list[int | float] = []
        for item in raw:
            if (
                type(item) not in {int, float}
                or not math.isfinite(float(item))
                or not -1_000_000 <= float(item) <= 1_000_000
            ):
                _fail("VISTA_SOURCE_BOUNDS_INVALID", f"metric_bounds_m.{field} is non-finite")
            result.append(item)
        return result

    minimum = vector("min_m")
    maximum = vector("max_m")
    if any(low >= high for low, high in zip(minimum, maximum)):
        _fail("VISTA_SOURCE_BOUNDS_INVALID", "metric bounds must have positive extent")
    return {"min_m": minimum, "max_m": maximum}


def _validate_license(provider: str, value: Mapping[str, Any]) -> dict[str, Any]:
    license_receipt = _closed_mapping(value, _LICENSE_KEYS, "license")
    license_receipt["license_id"] = _public_string(
        license_receipt["license_id"],
        "license.license_id",
        maximum=96,
    )
    for key in ("attribution", "modification_notice"):
        license_receipt[key] = _public_string(license_receipt[key], f"license.{key}")
    license_receipt["license_url"] = _https_url(license_receipt["license_url"], "license.license_url")
    entitlement_status = license_receipt["entitlement_status"]
    if entitlement_status not in {"project_owned", "verified"}:
        _fail("VISTA_SOURCE_ENTITLEMENT_INVALID", "entitlement status is not accepted")
    entitlement_record = _safe_uri(
        license_receipt["entitlement_record"],
        "license.entitlement_record",
        frozenset({"registry", "project-owned", "local-audit", "account-entitlement"}),
    )
    license_receipt["entitlement_record"] = entitlement_record
    record_scheme = urlsplit(entitlement_record).scheme
    commercial_use = license_receipt["commercial_use"]
    if commercial_use not in {"allowed", "prohibited", "requires_separate_permission"}:
        _fail("VISTA_SOURCE_LICENSE_INVALID", "commercial use policy is invalid")
    redistribution = license_receipt["redistribution_restriction"]
    if redistribution not in {
        "project_policy",
        "attribution_required",
        "noncommercial_attribution_required",
        "no_standalone_asset_redistribution",
    }:
        _fail("VISTA_SOURCE_LICENSE_INVALID", "redistribution policy is invalid")

    if provider == PROVIDER_PROJECT_AUTHORED:
        if entitlement_status != "project_owned" or record_scheme != "project-owned":
            _fail("VISTA_SOURCE_ENTITLEMENT_INVALID", "project-authored source is not project-owned")
    elif provider == PROVIDER_EXISTING_LOCAL:
        allowed_records = {"project-owned"} if entitlement_status == "project_owned" else {"local-audit", "registry"}
        if record_scheme not in allowed_records:
            _fail("VISTA_SOURCE_ENTITLEMENT_INVALID", "existing-local source lacks matching local evidence")
    elif provider == PROVIDER_HSSD:
        if (
            license_receipt["license_id"] != "CC-BY-NC-4.0"
            or entitlement_status != "verified"
            or record_scheme not in {"local-audit", "registry"}
            or commercial_use not in {"prohibited", "requires_separate_permission"}
            or redistribution != "noncommercial_attribution_required"
        ):
            _fail("VISTA_SOURCE_LICENSE_INVALID", "HSSD requires verified CC-BY-NC-4.0 restrictions")
    elif provider == PROVIDER_YCB:
        if (
            license_receipt["license_id"] != "CC-BY-4.0"
            or entitlement_status != "verified"
            or record_scheme not in {"local-audit", "registry"}
            or commercial_use != "allowed"
            or redistribution != "attribution_required"
        ):
            _fail("VISTA_SOURCE_LICENSE_INVALID", "YCB requires verified CC-BY-4.0 attribution")
    elif provider == PROVIDER_POLY_HAVEN:
        if (
            license_receipt["license_id"] != "CC0-1.0"
            or entitlement_status != "verified"
            or record_scheme not in {"local-audit", "registry"}
            or commercial_use != "allowed"
            or redistribution != "project_policy"
        ):
            _fail("VISTA_SOURCE_LICENSE_INVALID", "Poly Haven requires verified CC0-1.0 evidence")
    elif provider == PROVIDER_FAB_GATED:
        if (
            entitlement_status != "verified"
            or record_scheme != "account-entitlement"
            or redistribution != "no_standalone_asset_redistribution"
        ):
            _fail("VISTA_SOURCE_ENTITLEMENT_REQUIRED", "Fab entitlement is unverified")
    else:  # Defensive; provider validation normally rejects this earlier.
        _fail("VISTA_SOURCE_PROVIDER_UNSUPPORTED", "provider is not supported")
    return license_receipt


def _validate_material_inventory(value: Mapping[str, Any]) -> dict[str, Any]:
    inventory = _closed_mapping(value, _MATERIAL_INVENTORY_KEYS, "material_inventory")
    slots = inventory["slots"]
    if type(slots) is not list or not 1 <= len(slots) <= 128:
        _fail("VISTA_SOURCE_MATERIAL_INVALID", "material inventory requires 1-128 slots")
    normalized_slots: list[dict[str, Any]] = []
    seen_slot_ids: set[str] = set()
    for index, value_slot in enumerate(slots):
        slot = _closed_mapping(value_slot, _MATERIAL_SLOT_KEYS, f"material_inventory.slots[{index}]")
        slot_id = _public_string(slot["slot_id"], f"material_inventory.slots[{index}].slot_id", maximum=96)
        if not _SLOT_ID_RE.fullmatch(slot_id) or slot_id in seen_slot_ids:
            _fail("VISTA_SOURCE_MATERIAL_INVALID", "material slot ID is invalid or duplicated")
        seen_slot_ids.add(slot_id)
        if slot["shader_class"] not in {"pbr_metallic_roughness", "glass", "emissive"}:
            _fail("VISTA_SOURCE_MATERIAL_INVALID", "shader class is invalid")
        if slot["blend_mode"] not in {"opaque", "masked", "translucent"}:
            _fail("VISTA_SOURCE_MATERIAL_INVALID", "blend mode is invalid")
        semantics = slot["texture_semantics"]
        if (
            type(semantics) is not list
            or not 1 <= len(semantics) <= 8
            or len(set(semantics)) != len(semantics)
            or any(type(item) is not str or item not in _TEXTURE_SEMANTICS for item in semantics)
        ):
            _fail("VISTA_SOURCE_MATERIAL_INVALID", "texture semantics are invalid")
        texture_size = slot["minimum_texture_size_px"]
        if type(texture_size) is not int or not 256 <= texture_size <= 8192:
            _fail("VISTA_SOURCE_MATERIAL_INVALID", "minimum texture size is invalid")
        normalized_slots.append(slot)
    texture_count = inventory["texture_count"]
    if type(texture_count) is not int or not 1 <= texture_count <= 4096:
        _fail("VISTA_SOURCE_MATERIAL_INVALID", "texture count is invalid")
    if inventory["all_primitives_material_bound"] is not True:
        _fail("VISTA_SOURCE_MATERIAL_INVALID", "every primitive must have a material")
    inventory["slots"] = normalized_slots
    return inventory


def _validate_import_policy(value: Mapping[str, Any], material_inventory: Mapping[str, Any]) -> dict[str, Any]:
    policy = _closed_mapping(value, _IMPORT_POLICY_KEYS, "import_policy")
    if policy["nanite"] not in {"eligible_static_opaque", "disabled_ineligible"}:
        _fail("VISTA_SOURCE_IMPORT_POLICY_INVALID", "Nanite policy is invalid")
    if policy["mobility"] not in {"static", "movable"}:
        _fail("VISTA_SOURCE_IMPORT_POLICY_INVALID", "mobility is invalid")
    if policy["lod_policy"] not in {"nanite", "authored_lods", "single_mesh_measured"}:
        _fail("VISTA_SOURCE_IMPORT_POLICY_INVALID", "LOD policy is invalid")
    if policy["collision_policy"] not in {"no_collision", "hidden_r1_proxy", "coarse_static_proxy"}:
        _fail("VISTA_SOURCE_IMPORT_POLICY_INVALID", "collision policy is invalid")
    if policy["nanite"] == "eligible_static_opaque":
        if policy["mobility"] != "static" or policy["lod_policy"] != "nanite":
            _fail("VISTA_SOURCE_IMPORT_POLICY_INVALID", "Nanite assets must be static with Nanite LOD policy")
        if any(slot["blend_mode"] != "opaque" for slot in material_inventory["slots"]):
            _fail("VISTA_SOURCE_IMPORT_POLICY_INVALID", "translucent or masked assets are not Nanite eligible")
    elif policy["lod_policy"] == "nanite":
        _fail("VISTA_SOURCE_IMPORT_POLICY_INVALID", "Nanite LOD policy requires Nanite eligibility")
    return policy


def _validate_spec(spec: AssetSourceSpec) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if spec.provider not in SUPPORTED_PROVIDERS:
        _fail("VISTA_SOURCE_PROVIDER_UNSUPPORTED", "provider is not supported")
    if (
        len(spec.receipt_id) > 160
        or not _ID_RE.fullmatch(spec.receipt_id)
        or not spec.receipt_id.startswith("source.")
    ):
        _fail("VISTA_SOURCE_ID_INVALID", "receipt_id is invalid")
    if (
        len(spec.logical_asset_id) > 160
        or not _ID_RE.fullmatch(spec.logical_asset_id)
        or not spec.logical_asset_id.startswith("visual.")
    ):
        _fail("VISTA_SOURCE_ID_INVALID", "logical_asset_id is invalid")
    if not _VERSION_RE.fullmatch(spec.source_version):
        _fail("VISTA_SOURCE_VERSION_INVALID", "source_version is invalid")
    _safe_uri(
        spec.catalog_identity,
        "catalog_identity",
        frozenset({_PROVIDER_URI_SCHEME[spec.provider]}),
    )
    bounds = _validate_bounds(spec.metric_bounds_m)
    license_receipt = _validate_license(spec.provider, spec.license)
    material_inventory = _validate_material_inventory(spec.material_inventory)
    import_policy = _validate_import_policy(spec.import_policy, material_inventory)
    return bounds, license_receipt, material_inventory, import_policy


def _lexical_absolute(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.fspath(path)))


def _validate_roots(roots: Sequence[AllowedSourceRoot]) -> list[tuple[AllowedSourceRoot, pathlib.Path]]:
    if not roots:
        _fail("VISTA_SOURCE_ROOT_INVALID", "at least one source root is required")
    normalized: list[tuple[AllowedSourceRoot, pathlib.Path]] = []
    seen_ids: set[str] = set()
    seen_paths: set[pathlib.Path] = set()
    for root in roots:
        if not _ROOT_ID_RE.fullmatch(root.root_id) or root.root_id in seen_ids:
            _fail("VISTA_SOURCE_ROOT_INVALID", "source root ID is invalid or duplicated")
        path = pathlib.Path(root.path)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            _fail("VISTA_SOURCE_ROOT_INVALID", "source root must be an absolute regular directory")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise AssetSourceResolutionError("VISTA_SOURCE_ROOT_INVALID", "source root is unavailable") from error
        if _lexical_absolute(path) != resolved:
            _fail("VISTA_SOURCE_ROOT_INVALID", "source root may not traverse a symlink")
        providers = tuple(root.providers)
        if (
            not providers
            or len(set(providers)) != len(providers)
            or any(item not in SUPPORTED_PROVIDERS for item in providers)
        ):
            _fail("VISTA_SOURCE_ROOT_INVALID", "source root provider allowlist is invalid")
        if resolved in seen_paths:
            _fail("VISTA_SOURCE_ROOT_INVALID", "one private path may not have multiple root identities")
        seen_ids.add(root.root_id)
        seen_paths.add(resolved)
        normalized.append((root, resolved))
    return normalized


def _select_root(
    source_path: pathlib.Path,
    provider: str,
    roots: Sequence[tuple[AllowedSourceRoot, pathlib.Path]],
) -> tuple[AllowedSourceRoot, pathlib.Path, pathlib.Path]:
    path = pathlib.Path(source_path)
    if not path.is_absolute() or path.is_symlink() or not path.exists():
        _fail("VISTA_SOURCE_PATH_INVALID", "source must be an absolute existing non-symlink path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise AssetSourceResolutionError("VISTA_SOURCE_PATH_INVALID", "source is unavailable") from error
    if _lexical_absolute(path) != resolved:
        _fail("VISTA_SOURCE_PATH_INVALID", "source path may not traverse a symlink")

    matches: list[tuple[AllowedSourceRoot, pathlib.Path]] = []
    for root, root_path in roots:
        try:
            resolved.relative_to(root_path)
        except ValueError:
            continue
        matches.append((root, root_path))
    if not matches:
        _fail("VISTA_SOURCE_OUTSIDE_ALLOWLIST", "source is outside all allowlisted roots")
    matches.sort(key=lambda item: (-len(item[1].parts), item[0].root_id))
    root, root_path = matches[0]
    if provider not in root.providers:
        _fail("VISTA_SOURCE_PROVIDER_NOT_ALLOWED", "provider is not allowed for the matched source root")
    relative = resolved.relative_to(root_path)
    if not relative.parts:
        _fail("VISTA_SOURCE_PATH_INVALID", "an asset source must be below, not equal to, its source root")
    relative_text = relative.as_posix()
    if (
        not _RELATIVE_PATH_RE.fullmatch(relative_text)
        or any(segment in {"", ".", ".."} for segment in relative.parts)
    ):
        _fail("VISTA_SOURCE_PATH_INVALID", "source relative path is not portable and normalized")
    return root, root_path, relative


def _sha256_file(path: pathlib.Path) -> tuple[str, int]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        _fail("VISTA_SOURCE_PATH_INVALID", "source tree contains a non-regular file")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise AssetSourceResolutionError("VISTA_SOURCE_READ_FAILED", "source bytes could not be read") from error
    after = path.lstat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or size != after.st_size:
        _fail("VISTA_SOURCE_CHANGED_DURING_HASH", "source changed while it was hashed")
    return digest.hexdigest(), size


def _inspect_source(source: pathlib.Path) -> dict[str, Any]:
    mode = source.lstat().st_mode
    if stat.S_ISREG(mode):
        digest, size = _sha256_file(source)
        return {
            "content_kind": "file",
            "digest_algorithm": "sha256",
            "source_digest": digest,
            "size_bytes": size,
            "file_count": 1,
            "directory_count": 0,
        }
    if not stat.S_ISDIR(mode):
        _fail("VISTA_SOURCE_PATH_INVALID", "source must be a regular file or directory tree")

    entries: list[dict[str, Any]] = []
    total_size = 0
    file_count = 0
    directory_count = 0
    try:
        descendants = sorted(
            source.rglob("*"),
            key=lambda item: item.relative_to(source).as_posix().encode("utf-8"),
        )
    except OSError as error:
        raise AssetSourceResolutionError("VISTA_SOURCE_READ_FAILED", "source tree could not be enumerated") from error
    for descendant in descendants:
        relative = descendant.relative_to(source).as_posix()
        if not _RELATIVE_PATH_RE.fullmatch(relative) or any(
            part in {"", ".", ".."} for part in pathlib.PurePosixPath(relative).parts
        ):
            _fail("VISTA_SOURCE_PATH_INVALID", "source tree contains a non-portable relative path")
        info = descendant.lstat()
        if stat.S_ISLNK(info.st_mode):
            _fail("VISTA_SOURCE_PATH_INVALID", "source tree contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path": relative, "type": "directory"})
            directory_count += 1
        elif stat.S_ISREG(info.st_mode):
            digest, size = _sha256_file(descendant)
            entries.append({"path": relative, "type": "file", "size_bytes": size, "sha256": digest})
            file_count += 1
            total_size += size
        else:
            _fail("VISTA_SOURCE_PATH_INVALID", "source tree contains a non-regular entry")
    tree_digest = hashlib.sha256(canonical_json_bytes({"entries": entries})).hexdigest()
    return {
        "content_kind": "tree",
        "digest_algorithm": "sha256-tree-v1",
        "source_digest": tree_digest,
        "size_bytes": total_size,
        "file_count": file_count,
        "directory_count": directory_count,
    }


def resolve_asset_source(
    spec: AssetSourceSpec,
    allowed_roots: Sequence[AllowedSourceRoot],
) -> AssetSourceResolution:
    """Resolve one explicit local source without download or provider fallback."""

    bounds, license_receipt, material_inventory, import_policy = _validate_spec(spec)
    roots = _validate_roots(allowed_roots)
    root, root_path, relative = _select_root(spec.source_path, spec.provider, roots)
    source = root_path / relative
    evidence = _inspect_source(source)
    source_uri = f"{_PROVIDER_URI_SCHEME[spec.provider]}://{root.root_id}/{relative.as_posix()}"
    if len(source_uri) > 288:
        _fail("VISTA_SOURCE_URI_UNSAFE", "normalized source URI exceeds the VisualProfile contract")

    asset_receipt: dict[str, Any] = {
        "receipt_id": spec.receipt_id,
        "logical_asset_id": spec.logical_asset_id,
        "source_kind": _PROVIDER_SOURCE_KIND[spec.provider],
        "source_uri": source_uri,
        "source_digest": evidence["source_digest"],
        "source_version": spec.source_version,
        "metric_bounds_m": bounds,
        "license": license_receipt,
        "material_inventory": material_inventory,
        "import_policy": import_policy,
    }
    asset_receipt["receipt_digest"] = content_digest(asset_receipt, "receipt_digest")

    normalized: dict[str, Any] = {
        "schema_version": RESOLUTION_SCHEMA_VERSION,
        "provider": spec.provider,
        "asset_source_receipt": asset_receipt,
        "source_evidence": {
            "source_root_id": root.root_id,
            "source_relative_path": relative.as_posix(),
            **evidence,
        },
        "provenance": {
            "catalog_identity": spec.catalog_identity,
            "acquisition_record": license_receipt["entitlement_record"],
            "license_id": license_receipt["license_id"],
            "entitlement_status": license_receipt["entitlement_status"],
            "modification_notice": license_receipt["modification_notice"],
            "redistribution_restriction": license_receipt["redistribution_restriction"],
        },
    }
    normalized["resolution_digest"] = content_digest(normalized, "resolution_digest")
    serialized = canonical_json_bytes(normalized)
    lowered = serialized.decode("utf-8").lower()
    if any(marker in lowered for marker in _PRIVATE_PATH_MARKERS):
        _fail("VISTA_SOURCE_PRIVATE_PATH_PROHIBITED", "normalized receipt leaked a private path")
    if not _SHA256_RE.fullmatch(asset_receipt["source_digest"]):
        _fail("VISTA_SOURCE_DIGEST_INVALID", "source digest is not SHA-256")
    return AssetSourceResolution(serialized)


__all__ = [
    "AllowedSourceRoot",
    "AssetSourceResolution",
    "AssetSourceResolutionError",
    "AssetSourceSpec",
    "PROVIDER_EXISTING_LOCAL",
    "PROVIDER_FAB_GATED",
    "PROVIDER_HSSD",
    "PROVIDER_POLY_HAVEN",
    "PROVIDER_PROJECT_AUTHORED",
    "PROVIDER_YCB",
    "RESOLUTION_SCHEMA_VERSION",
    "SUPPORTED_PROVIDERS",
    "canonical_json_bytes",
    "content_digest",
    "resolve_asset_source",
]
