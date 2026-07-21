#!/usr/bin/env python3
"""Capture or verify a local, content-addressed FastEmbed model artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys

from asset_stack_config import (
    AssetStackConfigError,
    _hash_regular_file,
    require_text,
    verify_embedding_model_artifact,
)


SCHEMA = "simworld-embedding-model-artifact/v1"


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def capture(root: pathlib.Path, *, kind: str, model_id: str, dense_size: int | None):
    model_id = require_text(model_id, "model_id", limit=240)
    if kind not in {"dense", "sparse"}:
        raise AssetStackConfigError(
            "ASSET_MODEL_ARTIFACT_INVALID", "kind must be dense or sparse"
        )
    if kind == "dense" and (dense_size is None or dense_size <= 0):
        raise AssetStackConfigError(
            "ASSET_MODEL_ARTIFACT_INVALID", "--dense-size is required for dense"
        )
    if kind == "sparse" and dense_size is not None:
        raise AssetStackConfigError(
            "ASSET_MODEL_ARTIFACT_INVALID", "--dense-size is invalid for sparse"
        )
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise AssetStackConfigError(
            "ASSET_MODEL_ARTIFACT_UNAVAILABLE", "model directory is invalid"
        )
    output = root / "artifact-manifest.json"
    if output.exists() or output.is_symlink():
        raise AssetStackConfigError(
            "ASSET_MODEL_MANIFEST_EXISTS",
            "artifact-manifest.json is immutable and may not be replaced",
        )
    entries = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise AssetStackConfigError(
                "ASSET_MODEL_SYMLINK_REJECTED", "model artifact tree has a symlink"
            )
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        digest, size, _payload = _hash_regular_file(path, f"model artifact {relative}")
        entries.append({"path": relative, "sha256": digest, "size": size})
    if not entries:
        raise AssetStackConfigError(
            "ASSET_MODEL_ARTIFACT_INVALID", "model artifact has no files"
        )
    manifest = {
        "schema": SCHEMA,
        "kind": kind,
        "model_id": model_id,
        "files": entries,
        **({"dense_size": dense_size} if kind == "dense" else {}),
    }
    payload = canonical_json(manifest) + b"\n"
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o644,
    )
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            os.link(temporary, output)
        except FileExistsError as error:
            raise AssetStackConfigError(
                "ASSET_MODEL_MANIFEST_EXISTS",
                "artifact-manifest.json is immutable and may not be replaced",
            ) from error
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    revision = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    return manifest, revision


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("capture", "verify"):
        item = sub.add_parser(command)
        item.add_argument("--model-dir", type=pathlib.Path, required=True)
        item.add_argument("--kind", choices=["dense", "sparse"], required=True)
        item.add_argument("--model-id", required=True)
        item.add_argument("--dense-size", type=int)
        if command == "verify":
            item.add_argument("--revision", required=True)
    return parser


def main():
    args = make_parser().parse_args()
    try:
        if args.command == "capture":
            manifest, revision = capture(
                args.model_dir,
                kind=args.kind,
                model_id=args.model_id,
                dense_size=args.dense_size,
            )
            result = {
                "schema": "simworld-embedding-model-artifact-capture/v1",
                "status": "captured",
                "model_id": manifest["model_id"],
                "kind": manifest["kind"],
                "revision": revision,
            }
        else:
            result = {
                "schema": "simworld-embedding-model-artifact-verification/v1",
                "status": "verified",
                **verify_embedding_model_artifact(
                    args.model_dir,
                    model_id=args.model_id,
                    revision=args.revision,
                    kind=args.kind,
                    dense_size=args.dense_size,
                ),
            }
    except AssetStackConfigError as error:
        print(
            canonical_json(
                {
                    "schema": "simworld-embedding-model-artifact-verification/v1",
                    "status": "not_ready",
                    "code": error.code,
                    "message": str(error),
                }
            ).decode(),
            file=sys.stderr,
        )
        return 2
    print(canonical_json(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
