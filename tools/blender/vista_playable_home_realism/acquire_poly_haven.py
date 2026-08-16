#!/usr/bin/env python3
"""Acquire an explicit, licensed Poly Haven asset set into a fresh NAS root.

The input is intentionally a closed allowlist.  The command never searches for
or substitutes assets, never sends credentials, accepts only the official API
and download hosts, and verifies the size and MD5 published by Poly Haven before
recording an additional SHA-256 digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, BinaryIO, Mapping, Sequence


SCHEMA_VERSION = "simworld.vista.playable-home-poly-haven-acquisition/v1"
RECEIPT_SCHEMA_VERSION = "simworld.vista.playable-home-poly-haven-receipt/v1"
CATALOG_URLS = {
    "model": "https://api.polyhaven.com/assets?t=models",
    "texture": "https://api.polyhaven.com/assets?t=textures",
}
FILES_URL_PREFIX = "https://api.polyhaven.com/files/"
LICENSE_ID = "CC0-1.0"
LICENSE_URL = "https://polyhaven.com/license"
ALLOWED_API_HOST = "api.polyhaven.com"
ALLOWED_DOWNLOAD_HOST = "dl.polyhaven.org"
MAX_ASSETS = 32
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_ASSET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
_LOGICAL_ID_RE = re.compile(r"visual\.[a-z0-9][a-z0-9._-]{0,158}")
_MD5_RE = re.compile(r"[0-9a-f]{32}")


class AcquisitionError(RuntimeError):
    """A closed acquisition, integrity, or provenance gate failed."""


@dataclass(frozen=True)
class RequestedAsset:
    asset_id: str
    logical_asset_id: str
    asset_type: str
    expected_files_hash: str
    resolution: str
    file_variant: str
    room_role: str


@dataclass(frozen=True)
class DownloadFile:
    relative_path: str
    url: str
    expected_size: int
    expected_md5: str


@dataclass(frozen=True)
class DownloadPlan:
    request: RequestedAsset
    catalog_record: Mapping[str, Any]
    files_hash: str
    files: tuple[DownloadFile, ...]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _closed_mapping(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise AcquisitionError(f"{label} fields differ from the closed contract")
    return dict(value)


def _plain_string(value: Any, label: str, *, maximum: int = 192) -> str:
    if type(value) is not str or not value or len(value) > maximum or value != value.strip():
        raise AcquisitionError(f"{label} is not a bounded normalized string")
    if any(character in value for character in ("/", "\\", "\x00")):
        raise AcquisitionError(f"{label} contains path syntax")
    return value


def load_request(path: pathlib.Path) -> tuple[bytes, tuple[RequestedAsset, ...]]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcquisitionError("acquisition manifest is not UTF-8 JSON") from error
    root = _closed_mapping(payload, {"schema_version", "assets"}, "manifest")
    if root["schema_version"] != SCHEMA_VERSION:
        raise AcquisitionError("acquisition manifest schema_version is unsupported")
    rows = root["assets"]
    if type(rows) is not list or not 1 <= len(rows) <= MAX_ASSETS:
        raise AcquisitionError(f"assets must contain 1-{MAX_ASSETS} entries")
    result: list[RequestedAsset] = []
    seen_asset_ids: set[str] = set()
    seen_logical_ids: set[str] = set()
    for index, value in enumerate(rows):
        row = _closed_mapping(
            value,
            {
                "asset_id",
                "logical_asset_id",
                "asset_type",
                "expected_files_hash",
                "resolution",
                "file_variant",
                "room_role",
            },
            f"assets[{index}]",
        )
        asset_id = _plain_string(row["asset_id"], f"assets[{index}].asset_id", maximum=96)
        logical_id = _plain_string(
            row["logical_asset_id"], f"assets[{index}].logical_asset_id", maximum=160
        )
        room_role = _plain_string(row["room_role"], f"assets[{index}].room_role", maximum=96)
        if not _ASSET_ID_RE.fullmatch(asset_id):
            raise AcquisitionError(f"assets[{index}].asset_id is invalid")
        if not _LOGICAL_ID_RE.fullmatch(logical_id):
            raise AcquisitionError(f"assets[{index}].logical_asset_id is invalid")
        expected_files_hash = row["expected_files_hash"]
        if type(expected_files_hash) is not str or not re.fullmatch(
            r"[0-9a-f]{40}", expected_files_hash
        ):
            raise AcquisitionError(f"assets[{index}].expected_files_hash is invalid")
        if row["resolution"] not in {"2k", "4k"}:
            raise AcquisitionError(f"assets[{index}].resolution must be 2k or 4k")
        asset_type = row["asset_type"]
        file_variant = row["file_variant"]
        if asset_type not in CATALOG_URLS:
            raise AcquisitionError(f"assets[{index}].asset_type must be model or texture")
        expected_variant = "blend" if asset_type == "model" else "pbr_jpg"
        if file_variant != expected_variant:
            raise AcquisitionError(
                f"assets[{index}].file_variant must be {expected_variant} for {asset_type}"
            )
        if asset_id in seen_asset_ids or logical_id in seen_logical_ids:
            raise AcquisitionError("asset_id and logical_asset_id must both be unique")
        seen_asset_ids.add(asset_id)
        seen_logical_ids.add(logical_id)
        result.append(
            RequestedAsset(
                asset_id=asset_id,
                logical_asset_id=logical_id,
                asset_type=asset_type,
                expected_files_hash=expected_files_hash,
                resolution=row["resolution"],
                file_variant=file_variant,
                room_role=room_role,
            )
        )
    result.sort(key=lambda item: item.logical_asset_id)
    return raw, tuple(result)


def _safe_https_url(value: Any, *, host: str, label: str) -> str:
    if type(value) is not str or len(value) > 2048:
        raise AcquisitionError(f"{label} is not a bounded URL")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise AcquisitionError(f"{label} is outside the official HTTPS host")
    if host == ALLOWED_DOWNLOAD_HOST:
        if parsed.query or not parsed.path.startswith("/file/ph-assets/"):
            raise AcquisitionError(f"{label} is outside the official download path")
    elif host == ALLOWED_API_HOST:
        is_catalog = parsed.path == "/assets" and parsed.query in {"t=models", "t=textures"}
        is_files = (
            not parsed.query
            and parsed.path.startswith("/files/")
            and _ASSET_ID_RE.fullmatch(parsed.path.removeprefix("/files/")) is not None
        )
        if not (is_catalog or is_files):
            raise AcquisitionError(f"{label} is outside the official API path")
    return value


def _safe_relative_path(value: str, *, label: str) -> str:
    pure = pathlib.PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any("\\" in part or "\x00" in part for part in pure.parts)
    ):
        raise AcquisitionError(f"{label} is not a safe relative path")
    return pure.as_posix()


def _file_record(relative_path: str, value: Any, *, label: str) -> DownloadFile:
    record = _closed_mapping(value, {"size", "url", "md5"}, label)
    size = record["size"]
    md5 = record["md5"]
    if type(size) is not int or not 1 <= size <= MAX_FILE_BYTES:
        raise AcquisitionError(f"{label}.size is invalid")
    if type(md5) is not str or not _MD5_RE.fullmatch(md5):
        raise AcquisitionError(f"{label}.md5 is invalid")
    return DownloadFile(
        relative_path=_safe_relative_path(relative_path, label=f"{label}.path"),
        url=_safe_https_url(record["url"], host=ALLOWED_DOWNLOAD_HOST, label=f"{label}.url"),
        expected_size=size,
        expected_md5=md5,
    )


def build_download_plan(
    request: RequestedAsset,
    catalog: Mapping[str, Any],
    file_catalog: Mapping[str, Any],
) -> DownloadPlan:
    if request.asset_id not in catalog or type(catalog[request.asset_id]) is not dict:
        raise AcquisitionError(f"asset is absent from the official catalog: {request.asset_id}")
    record = dict(catalog[request.asset_id])
    expected_type = 2 if request.asset_type == "model" else 1
    if record.get("type") != expected_type:
        raise AcquisitionError(f"catalog entry has the wrong asset type: {request.asset_id}")
    files_hash = record.get("files_hash")
    if type(files_hash) is not str or not re.fullmatch(r"[0-9a-f]{40}", files_hash):
        raise AcquisitionError(f"catalog files_hash is invalid: {request.asset_id}")
    if files_hash != request.expected_files_hash:
        raise AcquisitionError(f"catalog files_hash changed from the pinned manifest: {request.asset_id}")
    files: list[DownloadFile] = []
    if request.asset_type == "model":
        variants = file_catalog.get(request.file_variant)
        if type(variants) is not dict or request.resolution not in variants:
            raise AcquisitionError(
                f"requested {request.file_variant}/{request.resolution} is unavailable: {request.asset_id}"
            )
        resolution = variants[request.resolution]
        if type(resolution) is not dict or set(resolution) != {request.file_variant}:
            raise AcquisitionError(f"file variant envelope is invalid: {request.asset_id}")
        primary = resolution[request.file_variant]
        if type(primary) is not dict or set(primary) != {"include", "size", "url", "md5"}:
            raise AcquisitionError(f"primary file record is invalid: {request.asset_id}")
        primary_name = pathlib.PurePosixPath(
            urllib.parse.urlsplit(str(primary.get("url", ""))).path
        ).name
        if not primary_name.endswith(".blend"):
            raise AcquisitionError(f"primary file is not a .blend: {request.asset_id}")
        files.append(
            _file_record(
                primary_name,
                {key: primary[key] for key in ("size", "url", "md5")},
                label="primary",
            )
        )
        includes = primary["include"]
        if type(includes) is not dict or not includes:
            raise AcquisitionError(f"asset has no declared texture dependencies: {request.asset_id}")
        for relative_path in sorted(includes):
            files.append(
                _file_record(relative_path, includes[relative_path], label=f"include[{relative_path}]")
            )
    else:
        for semantic in ("Diffuse", "nor_gl", "Rough"):
            resolutions = file_catalog.get(semantic)
            if type(resolutions) is not dict or request.resolution not in resolutions:
                raise AcquisitionError(
                    f"requested {semantic}/{request.resolution} is unavailable: {request.asset_id}"
                )
            formats = resolutions[request.resolution]
            if type(formats) is not dict or "jpg" not in formats:
                raise AcquisitionError(
                    f"requested {semantic}/{request.resolution}/jpg is unavailable: {request.asset_id}"
                )
            file_record = formats["jpg"]
            if type(file_record) is not dict:
                raise AcquisitionError(f"texture file record is invalid: {request.asset_id}")
            basename = pathlib.PurePosixPath(
                urllib.parse.urlsplit(str(file_record.get("url", ""))).path
            ).name
            files.append(_file_record(basename, file_record, label=f"{semantic}.jpg"))
    paths = [item.relative_path for item in files]
    if len(paths) != len(set(paths)):
        raise AcquisitionError(f"asset declares duplicate download paths: {request.asset_id}")
    if sum(item.expected_size for item in files) > MAX_TOTAL_BYTES:
        raise AcquisitionError(f"asset exceeds the acquisition byte budget: {request.asset_id}")
    selected_catalog = {
        key: record.get(key)
        for key in (
            "name",
            "description",
            "date_published",
            "max_resolution",
            "polycount",
            "texel_density",
            "dimensions",
            "files_hash",
        )
    }
    return DownloadPlan(request, selected_catalog, files_hash, tuple(files))


def _request(url: str, *, timeout_seconds: int = 60) -> BinaryIO:
    parsed = urllib.parse.urlsplit(url)
    expected_host = ALLOWED_API_HOST if parsed.hostname == ALLOWED_API_HOST else ALLOWED_DOWNLOAD_HOST
    _safe_https_url(url, host=expected_host, label="request URL")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SimWorld-Studio-VISTA-Asset-Acquirer/1.0",
            "Referer": "https://github.com/IvesLiu1026/SimWorld-Studio",
        },
        method="GET",
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout_seconds)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AcquisitionError(f"official source request failed: {url}") from error
    final_url = response.geturl()
    _safe_https_url(final_url, host=expected_host, label="response URL")
    return response


def _fetch_json(url: str) -> dict[str, Any]:
    with _request(url) as response:
        raw = response.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise AcquisitionError("official JSON response exceeds the byte budget")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcquisitionError("official endpoint did not return UTF-8 JSON") from error
    if type(value) is not dict:
        raise AcquisitionError("official JSON response is not an object")
    return value


def _download_file(item: DownloadFile, destination: pathlib.Path) -> dict[str, Any]:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    try:
        with _request(item.url, timeout_seconds=180) as response, temporary.open("xb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > item.expected_size or size > MAX_FILE_BYTES:
                    raise AcquisitionError(f"download exceeds declared size: {item.relative_path}")
                sha256.update(chunk)
                md5.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if size != item.expected_size or md5.hexdigest() != item.expected_md5:
            raise AcquisitionError(f"download integrity mismatch: {item.relative_path}")
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "relative_path": item.relative_path,
        "url": item.url,
        "size_bytes": size,
        "provider_md5": item.expected_md5,
        "sha256": sha256.hexdigest(),
    }


def _prepare_output_root(path: pathlib.Path) -> pathlib.Path:
    if not path.is_absolute():
        raise AcquisitionError("output root must be absolute")
    if path.exists():
        raise AcquisitionError("output root must be a fresh append-only path")
    path.mkdir(mode=0o700, parents=True)
    if path.is_symlink():
        raise AcquisitionError("output root may not be a symlink")
    return path.resolve(strict=True)


def _resolve_plans(
    manifest_path: pathlib.Path,
) -> tuple[bytes, tuple[DownloadPlan, ...]]:
    manifest_raw, requests = load_request(manifest_path)
    catalogs = {asset_type: _fetch_json(url) for asset_type, url in CATALOG_URLS.items()}
    plans = tuple(
        build_download_plan(
            request,
            catalogs[request.asset_type],
            _fetch_json(f"{FILES_URL_PREFIX}{urllib.parse.quote(request.asset_id, safe='')}")
        )
        for request in requests
    )
    total_expected = sum(item.expected_size for plan in plans for item in plan.files)
    if total_expected > MAX_TOTAL_BYTES:
        raise AcquisitionError("acquisition set exceeds the total byte budget")
    return manifest_raw, plans


def plan_acquisition(manifest_path: pathlib.Path) -> dict[str, Any]:
    manifest_raw, plans = _resolve_plans(manifest_path)
    result: dict[str, Any] = {
        "schema_version": "simworld.vista.playable-home-poly-haven-download-plan/v1",
        "provider": "poly_haven",
        "catalog_urls": dict(CATALOG_URLS),
        "license_id": LICENSE_ID,
        "license_url": LICENSE_URL,
        "manifest_sha256": _sha256_bytes(manifest_raw),
        "asset_count": len(plans),
        "total_expected_size_bytes": sum(
            item.expected_size for plan in plans for item in plan.files
        ),
        "assets": [
            {
                "asset_id": plan.request.asset_id,
                "logical_asset_id": plan.request.logical_asset_id,
                "asset_type": plan.request.asset_type,
                "resolution": plan.request.resolution,
                "file_variant": plan.request.file_variant,
                "room_role": plan.request.room_role,
                "provider_files_hash": plan.files_hash,
                "file_count": len(plan.files),
                "expected_size_bytes": sum(item.expected_size for item in plan.files),
            }
            for plan in plans
        ],
    }
    result["plan_digest"] = _sha256_bytes(_canonical_json(result))
    return result


def acquire(manifest_path: pathlib.Path, output_root: pathlib.Path) -> dict[str, Any]:
    manifest_raw, plans = _resolve_plans(manifest_path)
    root = _prepare_output_root(output_root)
    assets: list[dict[str, Any]] = []
    for plan in plans:
        asset_root = root / "assets" / plan.request.asset_id
        files = [
            _download_file(item, asset_root / item.relative_path)
            for item in plan.files
        ]
        tree_payload = [{key: row[key] for key in ("relative_path", "size_bytes", "sha256")} for row in files]
        assets.append(
            {
                "asset_id": plan.request.asset_id,
                "logical_asset_id": plan.request.logical_asset_id,
                "asset_type": plan.request.asset_type,
                "room_role": plan.request.room_role,
                "resolution": plan.request.resolution,
                "file_variant": plan.request.file_variant,
                "catalog": dict(plan.catalog_record),
                "provider_files_hash": plan.files_hash,
                "source_relative_root": f"assets/{plan.request.asset_id}",
                "primary_relative_path": f"assets/{plan.request.asset_id}/{plan.files[0].relative_path}",
                "files": files,
                "source_tree_sha256": _sha256_bytes(_canonical_json(tree_payload)),
            }
        )
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "provider": "poly_haven",
        "catalog_urls": dict(CATALOG_URLS),
        "license": {
            "license_id": LICENSE_ID,
            "license_url": LICENSE_URL,
            "entitlement_status": "verified",
            "commercial_use": "allowed",
            "redistribution_restriction": "project_policy",
        },
        "manifest_sha256": _sha256_bytes(manifest_raw),
        "acquired_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asset_count": len(assets),
        "total_size_bytes": sum(row["size_bytes"] for asset in assets for row in asset["files"]),
        "assets": assets,
    }
    receipt["receipt_digest"] = _sha256_bytes(_canonical_json(receipt))
    receipt_path = root / "acquisition-receipt.json"
    receipt_path.write_bytes(_canonical_json(receipt))
    receipt_path.chmod(0o600)
    return receipt


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--output-root", type=pathlib.Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run == (args.output_root is not None):
        parser.error("choose exactly one of --dry-run or --output-root")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    result = (
        plan_acquisition(args.manifest)
        if args.dry_run
        else acquire(args.manifest, args.output_root)
    )
    sys.stdout.buffer.write(_canonical_json(result))


if __name__ == "__main__":
    main()
