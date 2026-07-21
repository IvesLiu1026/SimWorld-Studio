#!/usr/bin/env python3
"""Validate and optionally acquire the pinned CC0 VISTA bootstrap assets."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, ContextManager, Iterator, Mapping, Sequence


SCHEMA = "simworld-external-asset-source/v1"
RESULT_SCHEMA = "simworld-external-asset-acquisition-result/v1"
RECEIPT_SCHEMA = "simworld-external-asset-acquisition-receipt/v1"
EXPECTED_LICENSE = "CC0-1.0"
DOWNLOAD_PREFIX = "https://dl.polyhaven.org/file/ph-assets/Models/"
SOURCE_PREFIX = "https://polyhaven.com/a/"
USER_AGENT = "VISTA-SimWorld-Research/0.1 (academic asset acquisition)"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 128
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MD5_RE = re.compile(r"^[a-f0-9]{32}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class AcquisitionError(RuntimeError):
    def __init__(self, code: str, message: str, *, pointer: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.pointer = pointer

    def public_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.pointer:
            result["pointer"] = self.pointer
        return result


def fail(code: str, message: str, *, pointer: str | None = None) -> None:
    raise AcquisitionError(code, message, pointer=pointer)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def exact_object(value: Any, allowed: set[str], required: set[str], pointer: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("ASSET_SOURCE_SCHEMA_INVALID", "Expected an object", pointer=pointer)
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown or missing:
        fail(
            "ASSET_SOURCE_SCHEMA_INVALID",
            "Object has unsupported or missing fields",
            pointer=pointer,
        )
    return value


def safe_string(value: Any, pointer: str, *, maximum: int = 512, pattern: re.Pattern[str] | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        fail("ASSET_SOURCE_SCHEMA_INVALID", "Expected a bounded non-empty string", pointer=pointer)
    if pattern and not pattern.fullmatch(value):
        fail("ASSET_SOURCE_SCHEMA_INVALID", "String has an invalid format", pointer=pointer)
    return value


def safe_relative_path(value: Any, pointer: str) -> str:
    text = safe_string(value, pointer)
    if text.startswith("/") or "\\" in text or urllib.parse.urlsplit(text).scheme:
        fail("ASSET_SOURCE_PATH_INVALID", "File path must be relative POSIX syntax", pointer=pointer)
    pure = PurePosixPath(text)
    if pure.as_posix() != text or any(part in {"", ".", ".."} for part in pure.parts):
        fail("ASSET_SOURCE_PATH_INVALID", "File path escapes or aliases the output root", pointer=pointer)
    return text


def validate_url(value: Any, pointer: str, prefix: str) -> str:
    url = safe_string(value, pointer, maximum=2048)
    parsed = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(prefix)
    decoded_path = urllib.parse.unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != expected.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
        or "\\" in decoded_path
        or not decoded_path.startswith(expected.path)
        or any(part in {"", ".", ".."} for part in PurePosixPath(decoded_path).parts[1:])
    ):
        fail("ASSET_SOURCE_URL_INVALID", "Source URL is not on the pinned HTTPS origin", pointer=pointer)
    return url


def _reject_symlink_components(path: Path, pointer: str, *, missing_ok: bool = False) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if missing_ok:
                return
            fail("ASSET_SOURCE_PATH_UNAVAILABLE", "Required path does not exist", pointer=pointer)
        if stat.S_ISLNK(metadata.st_mode):
            fail("ASSET_SOURCE_SYMLINK_REJECTED", "Symlinks are not accepted", pointer=pointer)


def read_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    _reject_symlink_components(path, "--manifest")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("ASSET_SOURCE_MANIFEST_INVALID", "Manifest cannot be opened safely", pointer="--manifest")
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1 or metadata.st_size > MAX_MANIFEST_BYTES:
                fail("ASSET_SOURCE_MANIFEST_INVALID", "Manifest must be a bounded regular file", pointer="--manifest")
            value = handle.read(MAX_MANIFEST_BYTES + 1)
            after = os.fstat(handle.fileno())
    finally:
        os.close(descriptor)
    if (
        len(value) != metadata.st_size
        or after.st_dev != metadata.st_dev
        or after.st_ino != metadata.st_ino
        or after.st_size != metadata.st_size
        or after.st_mtime_ns != metadata.st_mtime_ns
    ):
        fail("ASSET_SOURCE_CHANGED", "Manifest changed while being read", pointer="--manifest")
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        fail("ASSET_SOURCE_MANIFEST_INVALID", "Manifest is not valid UTF-8 JSON", pointer="--manifest")
    return validate_manifest(parsed), value


def validate_manifest(value: Any) -> dict[str, Any]:
    manifest = exact_object(
        value,
        {"schema", "revision", "provider", "provider_url", "license", "license_url", "attribution", "assets"},
        {"schema", "revision", "provider", "provider_url", "license", "license_url", "attribution", "assets"},
        "$",
    )
    if manifest["schema"] != SCHEMA or manifest["license"] != EXPECTED_LICENSE:
        fail("ASSET_SOURCE_SCHEMA_INVALID", "Unsupported source schema or license", pointer="$")
    revision = safe_string(manifest["revision"], "$/revision", pattern=ID_RE)
    if manifest["provider"] != "Poly Haven" or manifest["provider_url"] != "https://polyhaven.com/":
        fail("ASSET_SOURCE_PROVIDER_INVALID", "Provider identity is not pinned", pointer="$/provider")
    if manifest["license_url"] != "https://creativecommons.org/publicdomain/zero/1.0/":
        fail("ASSET_SOURCE_LICENSE_INVALID", "License URL is not pinned", pointer="$/license_url")
    attribution = safe_string(manifest["attribution"], "$/attribution", maximum=256)
    assets = manifest["assets"]
    if not isinstance(assets, list) or not assets or len(assets) > 32:
        fail("ASSET_SOURCE_SCHEMA_INVALID", "Assets must be a bounded non-empty array", pointer="$/assets")
    normalized_assets: list[dict[str, Any]] = []
    seen_assets: set[str] = set()
    seen_paths: set[str] = set()
    seen_urls: set[str] = set()
    file_count = 0
    total_bytes = 0
    for asset_index, raw_asset in enumerate(assets):
        pointer = f"$/assets/{asset_index}"
        asset = exact_object(
            raw_asset,
            {"asset_id", "source_asset_id", "source_page", "semantic_roles", "format", "files"},
            {"asset_id", "source_asset_id", "source_page", "semantic_roles", "format", "files"},
            pointer,
        )
        asset_id = safe_string(asset["asset_id"], f"{pointer}/asset_id", pattern=ID_RE)
        source_id = safe_string(asset["source_asset_id"], f"{pointer}/source_asset_id", pattern=ID_RE)
        if asset_id in seen_assets:
            fail("ASSET_SOURCE_DUPLICATE", "Asset IDs must be unique", pointer=f"{pointer}/asset_id")
        seen_assets.add(asset_id)
        source_page = validate_url(asset["source_page"], f"{pointer}/source_page", SOURCE_PREFIX)
        if source_page != SOURCE_PREFIX + source_id:
            fail("ASSET_SOURCE_IDENTITY_MISMATCH", "Source page and asset ID differ", pointer=f"{pointer}/source_page")
        roles = asset["semantic_roles"]
        if not isinstance(roles, list) or not roles or len(roles) > 16:
            fail("ASSET_SOURCE_SCHEMA_INVALID", "Semantic roles must be bounded", pointer=f"{pointer}/semantic_roles")
        normalized_roles = [safe_string(role, f"{pointer}/semantic_roles", pattern=ID_RE) for role in roles]
        if len(set(normalized_roles)) != len(normalized_roles) or asset["format"] != "gltf-2.0":
            fail("ASSET_SOURCE_SCHEMA_INVALID", "Semantic roles or format are invalid", pointer=pointer)
        files = asset["files"]
        if not isinstance(files, list) or not files:
            fail("ASSET_SOURCE_SCHEMA_INVALID", "Asset files must be non-empty", pointer=f"{pointer}/files")
        normalized_files: list[dict[str, Any]] = []
        for file_index, raw_file in enumerate(files):
            file_pointer = f"{pointer}/files/{file_index}"
            entry = exact_object(
                raw_file,
                {"path", "url", "bytes", "md5", "sha256"},
                {"path", "url", "bytes", "md5", "sha256"},
                file_pointer,
            )
            relative = safe_relative_path(entry["path"], f"{file_pointer}/path")
            url = validate_url(entry["url"], f"{file_pointer}/url", DOWNLOAD_PREFIX)
            size = entry["bytes"]
            if isinstance(size, bool) or not isinstance(size, int) or size < 1 or size > MAX_FILE_BYTES:
                fail("ASSET_SOURCE_SIZE_INVALID", "File size is outside the allowed range", pointer=f"{file_pointer}/bytes")
            md5 = safe_string(entry["md5"], f"{file_pointer}/md5", pattern=MD5_RE, maximum=32)
            sha256 = safe_string(entry["sha256"], f"{file_pointer}/sha256", pattern=SHA256_RE, maximum=64)
            if relative in seen_paths or url in seen_urls:
                fail("ASSET_SOURCE_DUPLICATE", "File paths and URLs must be unique", pointer=file_pointer)
            if PurePosixPath(relative).parts[0] != source_id:
                fail("ASSET_SOURCE_IDENTITY_MISMATCH", "File path is outside its source asset directory", pointer=f"{file_pointer}/path")
            seen_paths.add(relative)
            seen_urls.add(url)
            file_count += 1
            total_bytes += size
            normalized_files.append({"path": relative, "url": url, "bytes": size, "md5": md5, "sha256": sha256})
        normalized_assets.append(
            {
                "asset_id": asset_id,
                "source_asset_id": source_id,
                "source_page": source_page,
                "semantic_roles": normalized_roles,
                "format": "gltf-2.0",
                "files": normalized_files,
            }
        )
    if file_count > MAX_FILES or total_bytes > MAX_TOTAL_BYTES:
        fail("ASSET_SOURCE_SIZE_INVALID", "Source set exceeds acquisition limits", pointer="$/assets")
    return {
        "schema": SCHEMA,
        "revision": revision,
        "provider": "Poly Haven",
        "provider_url": "https://polyhaven.com/",
        "license": EXPECTED_LICENSE,
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "attribution": attribution,
        "assets": normalized_assets,
    }


@dataclass(frozen=True)
class AcquisitionPlan:
    manifest: dict[str, Any]
    manifest_bytes: bytes
    output_dir: Path
    entries: tuple[dict[str, Any], ...]
    total_bytes: int
    manifest_sha256: str


def validate_output(output_dir: Path) -> Path:
    if not output_dir.is_absolute() or output_dir == Path(output_dir.anchor) or not ID_RE.fullmatch(output_dir.name):
        fail("ASSET_SOURCE_OUTPUT_INVALID", "Output must be an absolute non-root safe directory", pointer="--output-dir")
    parent = output_dir.parent
    _reject_symlink_components(parent, "--output-dir")
    metadata = os.lstat(parent)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        fail("ASSET_SOURCE_OUTPUT_INVALID", "Output parent must be current-user-owned and private", pointer="--output-dir")
    return output_dir


def build_plan(manifest_path: Path, output_dir: Path) -> AcquisitionPlan:
    manifest, _raw = read_manifest(manifest_path)
    manifest_bytes = canonical_json(manifest)
    entries = tuple(entry for asset in manifest["assets"] for entry in asset["files"])
    return AcquisitionPlan(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        output_dir=validate_output(output_dir),
        entries=entries,
        total_bytes=sum(entry["bytes"] for entry in entries),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


class PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        validate_url(newurl, "download/redirect", DOWNLOAD_PREFIX)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@contextlib.contextmanager
def network_stream(url: str) -> Iterator[BinaryIO]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
    opener = urllib.request.build_opener(PinnedRedirectHandler())
    try:
        response = opener.open(request, timeout=30)
    except (urllib.error.URLError, TimeoutError, OSError):
        fail("ASSET_SOURCE_DOWNLOAD_FAILED", "Pinned asset download failed")
    try:
        validate_url(response.geturl(), "download/final_url", DOWNLOAD_PREFIX)
        yield response
    finally:
        response.close()


StreamOpener = Callable[[str], ContextManager[BinaryIO]]


def _write_download(entry: Mapping[str, Any], destination: Path, stream_opener: StreamOpener) -> None:
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output, stream_opener(str(entry["url"])) as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > int(entry["bytes"]):
                    fail("ASSET_SOURCE_INTEGRITY_FAILED", "Downloaded file exceeds its pinned size", pointer=str(entry["path"]))
                md5.update(chunk)
                sha256.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)
    if total != entry["bytes"] or md5.hexdigest() != entry["md5"] or sha256.hexdigest() != entry["sha256"]:
        fail("ASSET_SOURCE_INTEGRITY_FAILED", "Downloaded file does not match pinned evidence", pointer=str(entry["path"]))
    os.chmod(destination, 0o600, follow_symlinks=False)


def _tree_digest(entries: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256(b"simworld-external-asset-tree/v1\0")
    for entry in sorted(entries, key=lambda item: str(item["path"])):
        digest.update(str(entry["path"]).encode("utf-8") + b"\0")
        digest.update(str(entry["bytes"]).encode("ascii") + b"\0")
        digest.update(bytes.fromhex(str(entry["sha256"])))
        digest.update(b"\0")
    return digest.hexdigest()


def _write_private(path: Path, value: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _hash_regular(path: Path, expected_size: int) -> str:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        fail("ASSET_SOURCE_OUTPUT_CONFLICT", "Existing output contains an unsafe file", pointer=str(path))
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(chunk)
            digest.update(chunk)
    if total != expected_size:
        fail("ASSET_SOURCE_OUTPUT_CONFLICT", "Existing output file has a different size", pointer=str(path))
    return digest.hexdigest()


def verify_existing(plan: AcquisitionPlan) -> bool:
    try:
        root_meta = os.lstat(plan.output_dir)
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISDIR(root_meta.st_mode)
        or root_meta.st_uid != os.geteuid()
        or stat.S_IMODE(root_meta.st_mode) != 0o700
    ):
        fail("ASSET_SOURCE_OUTPUT_CONFLICT", "Existing output root is not private", pointer="--output-dir")
    expected = {entry["path"]: entry for entry in plan.entries}
    expected["source-manifest.json"] = {
        "path": "source-manifest.json",
        "bytes": len(plan.manifest_bytes),
        "sha256": plan.manifest_sha256,
    }
    expected_paths = set(expected) | {"acquisition-receipt.json"}
    observed_files: set[str] = set()
    for current, directories, files in os.walk(plan.output_dir, followlinks=False):
        current_path = Path(current)
        current_meta = os.lstat(current_path)
        if current_meta.st_uid != os.geteuid() or stat.S_IMODE(current_meta.st_mode) != 0o700:
            fail("ASSET_SOURCE_OUTPUT_CONFLICT", "Existing output contains an unsafe directory", pointer=str(current_path))
        for directory in directories:
            metadata = os.lstat(current_path / directory)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                fail("ASSET_SOURCE_OUTPUT_CONFLICT", "Existing output contains a symlink", pointer=directory)
        for filename in files:
            observed_files.add((current_path / filename).relative_to(plan.output_dir).as_posix())
    if observed_files != expected_paths:
        fail("ASSET_SOURCE_OUTPUT_CONFLICT", "Existing output file set differs from the pinned source set")
    for relative, entry in expected.items():
        if _hash_regular(plan.output_dir.joinpath(*PurePosixPath(relative).parts), int(entry["bytes"])) != entry["sha256"]:
            fail("ASSET_SOURCE_OUTPUT_CONFLICT", "Existing output content differs from the pinned source set", pointer=relative)
    receipt_path = plan.output_dir / "acquisition-receipt.json"
    receipt_meta = os.lstat(receipt_path)
    if (
        not stat.S_ISREG(receipt_meta.st_mode)
        or receipt_meta.st_nlink != 1
        or receipt_meta.st_uid != os.geteuid()
        or stat.S_IMODE(receipt_meta.st_mode) != 0o600
        or receipt_meta.st_size < 1
        or receipt_meta.st_size > MAX_MANIFEST_BYTES
    ):
        fail("ASSET_SOURCE_OUTPUT_CONFLICT", "Existing acquisition receipt is unsafe")
    receipt_raw = receipt_path.read_bytes()
    try:
        receipt = json.loads(receipt_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        fail("ASSET_SOURCE_OUTPUT_CONFLICT", "Existing acquisition receipt is invalid")
    exact_object(
        receipt,
        {"schema", "revision", "provider", "license", "attribution", "manifest_sha256", "file_count", "total_bytes", "tree_sha256", "acquired_at_utc"},
        {"schema", "revision", "provider", "license", "attribution", "manifest_sha256", "file_count", "total_bytes", "tree_sha256", "acquired_at_utc"},
        "acquisition-receipt.json",
    )
    expected_receipt = {
        "schema": RECEIPT_SCHEMA,
        "revision": plan.manifest["revision"],
        "provider": plan.manifest["provider"],
        "license": EXPECTED_LICENSE,
        "attribution": plan.manifest["attribution"],
        "manifest_sha256": plan.manifest_sha256,
        "file_count": len(plan.entries),
        "total_bytes": plan.total_bytes,
        "tree_sha256": _tree_digest(plan.entries),
    }
    acquired_at = receipt.get("acquired_at_utc")
    try:
        parsed_acquired_at = dt.datetime.fromisoformat(acquired_at) if isinstance(acquired_at, str) else None
    except ValueError:
        parsed_acquired_at = None
    if (
        {key: receipt.get(key) for key in expected_receipt} != expected_receipt
        or parsed_acquired_at is None
        or parsed_acquired_at.tzinfo is None
    ):
        fail("ASSET_SOURCE_OUTPUT_CONFLICT", "Existing acquisition receipt is invalid")
    return True


def apply_plan(plan: AcquisitionPlan, *, stream_opener: StreamOpener = network_stream) -> str:
    if verify_existing(plan):
        return "idempotent"
    lock = plan.output_dir.parent / f".{plan.output_dir.name}.lock"
    lock_fd: int | None = None
    owns_lock = False
    temp_root: Path | None = None
    try:
        lock_fd = os.open(
            lock,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        owns_lock = True
        os.close(lock_fd)
        lock_fd = None
        temp_root = Path(tempfile.mkdtemp(prefix=f".{plan.output_dir.name}.tmp-", dir=plan.output_dir.parent))
        os.chmod(temp_root, 0o700)
        for entry in plan.entries:
            destination = temp_root.joinpath(*PurePosixPath(entry["path"]).parts)
            _write_download(entry, destination, stream_opener)
        _write_private(temp_root / "source-manifest.json", plan.manifest_bytes)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "revision": plan.manifest["revision"],
            "provider": plan.manifest["provider"],
            "license": EXPECTED_LICENSE,
            "attribution": plan.manifest["attribution"],
            "manifest_sha256": plan.manifest_sha256,
            "file_count": len(plan.entries),
            "total_bytes": plan.total_bytes,
            "tree_sha256": _tree_digest(plan.entries),
            "acquired_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        _write_private(temp_root / "acquisition-receipt.json", canonical_json(receipt))
        for current, directories, _files in os.walk(temp_root):
            for directory in directories:
                os.chmod(Path(current) / directory, 0o700)
        os.rename(temp_root, plan.output_dir)
        temp_root = None
        return "created"
    except FileExistsError:
        if verify_existing(plan):
            return "idempotent"
        fail("ASSET_SOURCE_OUTPUT_BUSY", "Another acquisition owns the output lock")
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
        if owns_lock:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass


def result(plan: AcquisitionPlan, status: str) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "valid": True,
        "status": status,
        "revision": plan.manifest["revision"],
        "license": EXPECTED_LICENSE,
        "attribution": plan.manifest["attribution"],
        "output_dir": str(plan.output_dir),
        "file_count": len(plan.entries),
        "total_bytes": plan.total_bytes,
        "manifest_sha256": plan.manifest_sha256,
        "tree_sha256": _tree_digest(plan.entries),
        "network_used": status == "created",
    }


class SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        fail("ASSET_SOURCE_ARGUMENT_INVALID", "CLI arguments are missing or invalid; consult --help")


def build_parser() -> argparse.ArgumentParser:
    parser = SafeParser(description=__doc__)
    default_manifest = Path(__file__).resolve().parent / "assets" / "vista_mmg_040_cc0_bootstrap.json"
    parser.add_argument("--manifest", default=str(default_manifest))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--accept-license", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        args = build_parser().parse_args(argv)
        if args.apply and args.accept_license != EXPECTED_LICENSE:
            fail("ASSET_SOURCE_LICENSE_NOT_ACCEPTED", "--apply requires --accept-license CC0-1.0")
        plan = build_plan(Path(args.manifest), Path(args.output_dir))
        status = apply_plan(plan) if args.apply else "dry_run"
        print(compact_json(result(plan, status)))
        return 0
    except AcquisitionError as error:
        print(compact_json({"schema": RESULT_SCHEMA, "valid": False, "status": "failed", "error": error.public_dict()}), file=sys.stderr)
        return 2
    except Exception:
        print(
            compact_json(
                {
                    "schema": RESULT_SCHEMA,
                    "valid": False,
                    "status": "failed",
                    "error": {"code": "ASSET_SOURCE_INTERNAL_ERROR", "message": "Acquisition failed before a safe result was produced"},
                }
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
