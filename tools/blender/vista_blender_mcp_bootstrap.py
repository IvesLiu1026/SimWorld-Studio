"""Integrity helpers and the isolated VISTA Blender MCP bootstrap.

The shell launchers use the manifest subcommands before entering bubblewrap.
Blender then verifies the same manifests again, before either vendor path is
added to ``sys.path``.  This keeps a previously prepared run from silently
trusting a non-empty (but changed) dependency directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
from collections.abc import Iterator
from typing import Any


MANIFEST_SCHEMA = "vista-directory-manifest/v1"
EXPECTED_SOURCE_MANIFEST_SHA256 = (
    "eeaa3a5dcd4d695ca030960f7632b14e481a829af2b1ceaa299d89d3f333935b"
)


class IntegrityError(RuntimeError):
    """Raised when a prepared vendor tree no longer matches its manifest."""


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_entries(root: pathlib.Path) -> Iterator[dict[str, str]]:
    """Yield a stable inventory without following directory symlinks."""

    def visit(
        directory: pathlib.Path,
        prefix: pathlib.PurePosixPath,
    ) -> Iterator[dict[str, str]]:
        for child in sorted(
            directory.iterdir(), key=lambda item: os.fsencode(item.name)
        ):
            relative = prefix / child.name
            metadata = child.lstat()
            mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
            if stat.S_ISLNK(metadata.st_mode):
                yield {
                    "path": relative.as_posix(),
                    "type": "symlink",
                    "mode": mode,
                    "target": os.readlink(child),
                }
            elif stat.S_ISDIR(metadata.st_mode):
                yield from visit(child, relative)
            elif stat.S_ISREG(metadata.st_mode):
                yield {
                    "path": relative.as_posix(),
                    "type": "file",
                    "mode": mode,
                    "sha256": _sha256_file(child),
                }
            else:
                raise IntegrityError(
                    f"special file is not allowed in locked tree: {child}"
                )

    yield from visit(root, pathlib.PurePosixPath())


def build_manifest(root: pathlib.Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise IntegrityError(f"manifest root is not a directory: {root}")
    return {"schema": MANIFEST_SCHEMA, "entries": list(_walk_entries(root))}


def write_manifest(root: pathlib.Path, output: pathlib.Path) -> None:
    payload = build_manifest(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output.parent,
        prefix=f".{output.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True)
        handle.write("\n")
        temporary = pathlib.Path(handle.name)
    os.replace(temporary, output)


def load_manifest(path: pathlib.Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != MANIFEST_SCHEMA:
        raise IntegrityError(f"unsupported manifest schema: {path}")
    if not isinstance(payload.get("entries"), list):
        raise IntegrityError(f"manifest entries must be a list: {path}")
    return payload


def verify_manifest(root: pathlib.Path, manifest_path: pathlib.Path) -> None:
    expected = load_manifest(manifest_path)
    actual = build_manifest(root)
    if actual != expected:
        raise IntegrityError(
            f"locked directory does not match manifest: root={root} manifest={manifest_path}"
        )


def _required_directory(name: str) -> pathlib.Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    path = pathlib.Path(value).resolve(strict=True)
    if not path.is_dir():
        raise RuntimeError(f"{name} is not a directory: {path}")
    return path


def _required_file(name: str) -> pathlib.Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    path = pathlib.Path(value).resolve(strict=True)
    if not path.is_file():
        raise RuntimeError(f"{name} is not a file: {path}")
    return path


def _verify_manifest_file_hash(path: pathlib.Path, expected: str, label: str) -> None:
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise IntegrityError(f"invalid {label} manifest SHA-256")
    actual = _sha256_file(path)
    if actual != expected:
        raise IntegrityError(
            f"{label} manifest SHA-256 mismatch: actual={actual} expected={expected}"
        )


def start_server() -> None:
    """Verify locked inputs, import the add-on, and start localhost MCP."""

    site_packages = _required_directory("VISTA_BLENDER_MCP_SITE")
    source_dir = _required_directory("VISTA_BLENDER_MCP_SOURCE")
    source_manifest = _required_file("VISTA_BLENDER_MCP_SOURCE_MANIFEST")
    site_manifest = _required_file("VISTA_BLENDER_MCP_SITE_MANIFEST")

    _verify_manifest_file_hash(
        source_manifest,
        EXPECTED_SOURCE_MANIFEST_SHA256,
        "source",
    )
    _verify_manifest_file_hash(
        site_manifest,
        os.environ.get("VISTA_BLENDER_MCP_SITE_MANIFEST_SHA256", ""),
        "site-packages",
    )
    verify_manifest(source_dir, source_manifest)
    verify_manifest(site_packages, site_manifest)

    port = int(os.environ.get("VISTA_BLENDER_MCP_PORT", "8400"))
    if not 1024 <= port <= 65535:
        raise RuntimeError(f"invalid VISTA_BLENDER_MCP_PORT: {port}")

    sys.path[:0] = [str(site_packages), str(source_dir)]

    from blender_addon import bridge as bridge_module
    from blender_addon import server as server_module

    if os.environ.get("VISTA_BLENDER_MCP_VERIFY_ONLY") == "1":
        print("VISTA_BLENDER_MCP_IMPORT_OK", flush=True)
        return

    bridge_module.bridge = bridge_module.MainThreadBridge()
    bridge_module.bridge.start()
    server_module.setup(
        port=port,
        allow_execute_python=False,
        unrestricted=False,
    )
    server_module.start()

    print(
        f"VISTA_BLENDER_MCP_READY port={port} execute_python=false",
        flush=True,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    write_parser = subparsers.add_parser("write-manifest")
    write_parser.add_argument("root", type=pathlib.Path)
    write_parser.add_argument("output", type=pathlib.Path)

    verify_parser = subparsers.add_parser("verify-manifest")
    verify_parser.add_argument("root", type=pathlib.Path)
    verify_parser.add_argument("manifest", type=pathlib.Path)

    hash_parser = subparsers.add_parser("sha256")
    hash_parser.add_argument("path", type=pathlib.Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "write-manifest":
            write_manifest(args.root, args.output)
        elif args.command == "verify-manifest":
            verify_manifest(args.root, args.manifest)
        elif args.command == "sha256":
            print(_sha256_file(args.path))
        else:  # pragma: no cover - argparse prevents this branch
            raise AssertionError(args.command)
    except (IntegrityError, OSError) as exc:
        print(f"integrity error: {exc}", file=sys.stderr)
        return 1
    return 0


if os.environ.get("VISTA_BLENDER_MCP_BOOTSTRAP") == "1":
    start_server()
elif __name__ == "__main__":
    raise SystemExit(main())
