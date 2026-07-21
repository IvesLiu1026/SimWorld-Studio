#!/usr/bin/env python3
"""Shared fail-closed configuration helpers for semantic asset tooling.

The production tools accept secrets from environment-backed files so database
credentials never need to appear in argv.  Direct environment values remain a
deliberate escape hatch for bounded administrator commands, but callers can
require file-backed delivery for deployment preflights.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
from collections.abc import Mapping
from urllib.parse import urlsplit


SAFE_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,159}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
SHA256_REVISION = re.compile(r"^sha256:([0-9a-f]{64})$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
UNPINNED = {"dev", "latest", "main", "master", "unknown", "unversioned"}


class AssetStackConfigError(ValueError):
    """Typed public configuration failure without secret material."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require_text(value: object, field: str, *, limit: int = 1024) -> str:
    text = str(value or "").strip()
    if not text:
        raise AssetStackConfigError("ASSET_CONFIG_MISSING", f"{field} is required")
    if len(text) > limit or re.search(r"[\x00-\x1f\x7f]", text):
        raise AssetStackConfigError("ASSET_CONFIG_INVALID", f"{field} is invalid")
    return text


def require_immutable_revision(value: object, field: str) -> str:
    revision = require_text(value, field, limit=160)
    if not SAFE_REVISION.fullmatch(revision) or revision.casefold() in UNPINNED:
        raise AssetStackConfigError(
            "ASSET_REVISION_NOT_IMMUTABLE",
            f"{field} must identify an immutable revision",
        )
    return revision


def require_safe_name(value: object, field: str) -> str:
    name = require_text(value, field, limit=128)
    if not SAFE_NAME.fullmatch(name) or name.casefold() in UNPINNED:
        raise AssetStackConfigError(
            "ASSET_CONFIG_INVALID", f"{field} must be a pinned safe name"
        )
    return name


def require_digest_image(value: object, field: str) -> str:
    image = require_text(value, field, limit=512)
    if not IMAGE_DIGEST.fullmatch(image):
        raise AssetStackConfigError(
            "ASSET_IMAGE_NOT_PINNED",
            f"{field} must use an immutable @sha256 image digest",
        )
    return image


def require_sha256_revision(value: object, field: str) -> str:
    revision = require_text(value, field, limit=71)
    if not SHA256_REVISION.fullmatch(revision):
        raise AssetStackConfigError(
            "ASSET_MODEL_REVISION_NOT_CONTENT_PINNED",
            f"{field} must be sha256:<artifact-manifest-digest>",
        )
    return revision


def require_absolute_path(value: object, field: str) -> pathlib.Path:
    path = pathlib.Path(require_text(value, field, limit=4096)).expanduser()
    if not path.is_absolute():
        raise AssetStackConfigError(
            "ASSET_PATH_NOT_ABSOLUTE", f"{field} must be an absolute path"
        )
    return path


def _open_secret(path: pathlib.Path, field: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise AssetStackConfigError(
            "ASSET_SECRET_FILE_UNAVAILABLE", f"{field} is unavailable"
        ) from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AssetStackConfigError(
                "ASSET_SECRET_FILE_INVALID", f"{field} must be a regular file"
            )
        if info.st_mode & 0o077:
            raise AssetStackConfigError(
                "ASSET_SECRET_FILE_PERMISSIONS",
                f"{field} must not be accessible by group or other users",
            )
        if info.st_uid not in {0, os.geteuid()}:
            raise AssetStackConfigError(
                "ASSET_SECRET_FILE_OWNER",
                f"{field} must be owned by root or the current service user",
            )
        if info.st_size <= 0 or info.st_size > 16_384:
            raise AssetStackConfigError(
                "ASSET_SECRET_FILE_INVALID", f"{field} has an invalid size"
            )
        chunks: list[bytes] = []
        remaining = 16_385
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if len(payload) != info.st_size or any(
            getattr(info, name) != getattr(after, name) for name in stable_fields
        ):
            raise AssetStackConfigError(
                "ASSET_SECRET_FILE_CHANGED", f"{field} changed while it was read"
            )
    finally:
        os.close(fd)
    if len(payload) > 16_384:
        raise AssetStackConfigError(
            "ASSET_SECRET_FILE_INVALID", f"{field} exceeds the size limit"
        )
    try:
        decoded = payload.decode("utf-8")
    except UnicodeError as error:
        raise AssetStackConfigError(
            "ASSET_SECRET_FILE_INVALID", f"{field} must contain UTF-8 text"
        ) from error
    if not re.fullmatch(r"[^\r\n]*(?:\r?\n)?", decoded):
        raise AssetStackConfigError(
            "ASSET_SECRET_FILE_INVALID", f"{field} must contain one non-empty line"
        )
    value = re.sub(r"\r?\n$", "", decoded)
    if not value or re.search(r"[\x00-\x1f\x7f]", value):
        raise AssetStackConfigError(
            "ASSET_SECRET_FILE_INVALID", f"{field} must contain one non-empty line"
        )
    return value


def require_secret_minimum_bytes(value: str, field: str, minimum: int = 32) -> str:
    if len(value.encode("utf-8")) < minimum:
        raise AssetStackConfigError(
            "ASSET_SECRET_TOO_SHORT", f"{field} must contain at least {minimum} bytes"
        )
    return value


def load_secret(
    env: Mapping[str, str],
    value_name: str,
    file_name: str,
    *,
    required: bool = True,
    require_file: bool = False,
) -> tuple[str, str]:
    """Load one secret and return ``(value, source_variable)``.

    Both sources being configured is rejected so an operator cannot audit one
    value while the runtime silently consumes the other.
    """

    inline = str(env.get(value_name, "") or "").strip()
    file_value = str(env.get(file_name, "") or "").strip()
    if inline and file_value:
        raise AssetStackConfigError(
            "ASSET_SECRET_SOURCE_CONFLICT",
            f"set exactly one of {value_name} or {file_name}",
        )
    if require_file and inline:
        raise AssetStackConfigError(
            "ASSET_SECRET_FILE_REQUIRED",
            f"{file_name} is required for this production preflight",
        )
    if file_value:
        path = require_absolute_path(file_value, file_name)
        return _open_secret(path, file_name), file_name
    if inline:
        return require_text(inline, value_name, limit=16_384), value_name
    if required:
        expected = file_name if require_file else f"{file_name} or {value_name}"
        raise AssetStackConfigError(
            "ASSET_CONFIG_MISSING", f"{expected} is required"
        )
    return "", ""


def require_loopback_http_url(value: object, field: str) -> str:
    raw = require_text(value, field, limit=2048)
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AssetStackConfigError("ASSET_URL_INVALID", f"{field} is invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AssetStackConfigError(
            "ASSET_URL_CONTAINS_SECRET",
            f"{field} must not contain credentials, query parameters, or fragments",
        )
    if parsed.hostname.casefold() not in {"localhost", "127.0.0.1", "::1"}:
        raise AssetStackConfigError(
            "ASSET_URL_NOT_LOOPBACK", f"{field} must use a loopback host"
        )
    return raw.rstrip("/")


def validate_postgres_dsn(
    dsn: str,
    *,
    expected_database: str,
    require_loopback: bool = True,
) -> None:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise AssetStackConfigError(
            "ASSET_POSTGRES_DSN_INVALID", "POSTGRES_URL_FILE contains an invalid DSN"
        )
    if require_loopback and parsed.hostname.casefold() not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise AssetStackConfigError(
            "ASSET_POSTGRES_NOT_LOOPBACK",
            "POSTGRES_URL_FILE must target a loopback host",
        )
    if parsed.path.lstrip("/") != expected_database:
        raise AssetStackConfigError(
            "ASSET_POSTGRES_DATABASE_MISMATCH",
            "POSTGRES_URL_FILE database does not match POSTGRES_DB",
        )


def _hash_regular_file(path: pathlib.Path, field: str) -> tuple[str, int, bytes | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise AssetStackConfigError(
            "ASSET_MODEL_ARTIFACT_UNAVAILABLE", f"{field} is unavailable"
        ) from error
    capture = bytearray() if path.name == "artifact-manifest.json" else None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise AssetStackConfigError(
                "ASSET_MODEL_ARTIFACT_INVALID", f"{field} must be a non-empty file"
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if capture is not None:
                if len(capture) + len(chunk) > 1_000_000:
                    raise AssetStackConfigError(
                        "ASSET_MODEL_MANIFEST_INVALID",
                        f"{field} exceeds the manifest size limit",
                    )
                capture.extend(chunk)
        after = os.fstat(fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if before_identity != after_identity or total != after.st_size:
            raise AssetStackConfigError(
                "ASSET_MODEL_ARTIFACT_CHANGED", f"{field} changed while hashing"
            )
        return digest.hexdigest(), total, bytes(capture) if capture is not None else None
    finally:
        os.close(fd)


def verify_embedding_model_artifact(
    root: pathlib.Path,
    *,
    model_id: str,
    revision: str,
    kind: str,
    dense_size: int | None = None,
) -> dict[str, object]:
    """Verify an exact local FastEmbed model directory without network fallback."""

    model_id = require_text(model_id, f"{kind}_model_id", limit=240)
    revision = require_sha256_revision(revision, f"{kind}_revision")
    if kind not in {"dense", "sparse"}:
        raise AssetStackConfigError(
            "ASSET_MODEL_ARTIFACT_INVALID", "model artifact kind is invalid"
        )
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise AssetStackConfigError(
            "ASSET_MODEL_ARTIFACT_UNAVAILABLE",
            f"{kind} model directory must be an absolute real directory",
        )
    manifest_path = root / "artifact-manifest.json"
    manifest_digest, _manifest_size, payload = _hash_regular_file(
        manifest_path, f"{kind} artifact manifest"
    )
    if revision != f"sha256:{manifest_digest}":
        raise AssetStackConfigError(
            "ASSET_MODEL_REVISION_MISMATCH",
            f"{kind} model revision does not match artifact-manifest.json",
        )
    try:
        manifest = json.loads(payload or b"")
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AssetStackConfigError(
            "ASSET_MODEL_MANIFEST_INVALID", f"{kind} artifact manifest is invalid JSON"
        ) from error
    required_keys = {"schema", "kind", "model_id", "files"}
    if kind == "dense":
        required_keys.add("dense_size")
    if not isinstance(manifest, dict) or set(manifest) != required_keys:
        raise AssetStackConfigError(
            "ASSET_MODEL_MANIFEST_INVALID", f"{kind} artifact manifest fields are invalid"
        )
    if (
        manifest.get("schema") != "simworld-embedding-model-artifact/v1"
        or manifest.get("kind") != kind
        or manifest.get("model_id") != model_id
    ):
        raise AssetStackConfigError(
            "ASSET_MODEL_MANIFEST_MISMATCH",
            f"{kind} artifact manifest does not match configured model",
        )
    if kind == "dense" and (
        isinstance(manifest.get("dense_size"), bool)
        or not isinstance(manifest.get("dense_size"), int)
        or manifest.get("dense_size") != dense_size
    ):
        raise AssetStackConfigError(
            "ASSET_MODEL_MANIFEST_MISMATCH",
            "dense artifact dimension does not match EMBED_DENSE_SIZE",
        )
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise AssetStackConfigError(
            "ASSET_MODEL_MANIFEST_INVALID", f"{kind} artifact file list is empty"
        )
    declared: list[str] = []
    total_size = 0
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            raise AssetStackConfigError(
                "ASSET_MODEL_MANIFEST_INVALID", f"{kind} artifact entry is invalid"
            )
        relative = pathlib.PurePosixPath(str(entry.get("path") or ""))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise AssetStackConfigError(
                "ASSET_MODEL_PATH_ESCAPE", f"{kind} artifact path is invalid"
            )
        relative_text = relative.as_posix()
        if relative_text == "artifact-manifest.json":
            raise AssetStackConfigError(
                "ASSET_MODEL_MANIFEST_INVALID", "artifact manifest may not list itself"
            )
        expected_digest = entry.get("sha256")
        expected_size = entry.get("size")
        if (
            not isinstance(expected_digest, str)
            or not SHA256_HEX.fullmatch(expected_digest)
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
        ):
            raise AssetStackConfigError(
                "ASSET_MODEL_MANIFEST_INVALID", f"{kind} artifact checksum is invalid"
            )
        artifact = root.joinpath(*relative.parts)
        try:
            resolved = artifact.resolve(strict=True)
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise AssetStackConfigError(
                "ASSET_MODEL_PATH_ESCAPE", f"{kind} artifact escapes its directory"
            ) from error
        if artifact.is_symlink():
            raise AssetStackConfigError(
                "ASSET_MODEL_SYMLINK_REJECTED", f"{kind} artifact may not be a symlink"
            )
        observed_digest, observed_size, _payload = _hash_regular_file(
            artifact, f"{kind} artifact {relative_text}"
        )
        if observed_digest != expected_digest or observed_size != expected_size:
            raise AssetStackConfigError(
                "ASSET_MODEL_CHECKSUM_MISMATCH",
                f"{kind} artifact does not match its manifest",
            )
        declared.append(relative_text)
        total_size += observed_size
    if declared != sorted(set(declared)):
        raise AssetStackConfigError(
            "ASSET_MODEL_MANIFEST_INVALID",
            f"{kind} artifact paths must be unique and sorted",
        )
    observed: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise AssetStackConfigError(
                "ASSET_MODEL_SYMLINK_REJECTED", f"{kind} artifact tree has a symlink"
            )
        if path.is_file() and path != manifest_path:
            observed.append(path.relative_to(root).as_posix())
        elif not path.is_dir() and path != manifest_path:
            raise AssetStackConfigError(
                "ASSET_MODEL_ARTIFACT_INVALID", f"{kind} artifact tree is invalid"
            )
    if sorted(observed) != declared:
        raise AssetStackConfigError(
            "ASSET_MODEL_FILE_SET_MISMATCH",
            f"{kind} artifact directory does not exactly match its manifest",
        )
    return {
        "model_id": model_id,
        "revision": revision,
        "manifest_sha256": manifest_digest,
        "file_count": len(declared),
        "total_size": total_size,
        **({"dense_size": dense_size} if kind == "dense" else {}),
    }
