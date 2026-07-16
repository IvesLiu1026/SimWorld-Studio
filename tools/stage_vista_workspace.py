#!/usr/bin/env python3
"""Create one private, immutable-by-convention Studio release workspace."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path


COPY_PATHS = (
    ".studio_version",
    "web/src",
    "web/public",
    "web/server",
    "web/index.html",
    "web/vite.config.js",
    "web/package.json",
    "web/package-lock.json",
    "arena/skills",
    "arena/config",
)
RUNTIME_DIRS = (
    "scenes",
    "skills",
    "tmp/screens",
    "tmp/thumbnails",
    "logs",
    "arena_data",
    "checkpoints",
    "tasksets",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    """Hash every regular file under a tree by relative path and file digest."""

    root = Path(root).resolve()
    if not root.is_dir():
        raise RuntimeError(f"Required hash tree is missing: {root}")
    files = []
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise RuntimeError(f"Hash tree cannot contain symlinks: {candidate}")
        if candidate.is_file():
            files.append(candidate)
    digest = hashlib.sha256()
    for candidate in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = candidate.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(candidate)))
    return digest.hexdigest()


def sha256_server_source_tree(server_root: Path) -> str:
    """Hash the complete staged server source tree, excluding dependencies."""

    server_root = Path(server_root).resolve()
    if not server_root.is_dir():
        raise RuntimeError(f"Required server source tree is missing: {server_root}")
    files = []
    for candidate in server_root.rglob("*"):
        relative = candidate.relative_to(server_root)
        if relative.parts and relative.parts[0] == "node_modules":
            continue
        if candidate.is_symlink():
            raise RuntimeError(f"Server source tree cannot contain symlinks: {candidate}")
        if candidate.is_file():
            files.append(candidate)
    digest = hashlib.sha256()
    for candidate in sorted(files, key=lambda item: item.relative_to(server_root).as_posix()):
        relative = candidate.relative_to(server_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(candidate)))
    return digest.hexdigest()


def sha256_dependency_tree(root: Path) -> str:
    """Hash every production dependency file and safe in-tree symlink."""

    root = Path(root).resolve()
    if not root.is_dir():
        raise RuntimeError(f"Required dependency tree is missing: {root}")
    entries = []
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            target = os.readlink(candidate)
            if os.path.isabs(target):
                raise RuntimeError(f"Dependency tree has an absolute symlink: {candidate}")
            try:
                (candidate.parent / target).resolve(strict=True).relative_to(root)
            except (FileNotFoundError, RuntimeError, ValueError) as error:
                raise RuntimeError(f"Dependency symlink escapes or is broken: {candidate}") from error
            entries.append((relative, "L", target))
        elif candidate.is_file():
            entries.append((relative, "F", sha256_file(candidate)))
    digest = hashlib.sha256()
    for relative, entry_type, payload in sorted(entries):
        digest.update(entry_type.encode("ascii"))
        digest.update(b"\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def git_output(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _is_allowlisted_workspace_path(relative: Path, copy_paths: tuple[str, ...]) -> bool:
    value = relative.as_posix()
    return any(value == allowed or value.startswith(f"{allowed}/") for allowed in copy_paths)


def copy_tracked_workspace(
    source_repository: Path,
    destination: Path,
    copy_paths: tuple[str, ...] = COPY_PATHS,
) -> int:
    """Materialize only allowlisted files tracked by the clean source commit."""

    source_repository = Path(source_repository).resolve()
    destination = Path(destination).resolve()
    pathspecs = [f"simworld_studio_workspace/{relative}" for relative in copy_paths]
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD", "--", *pathspecs],
        cwd=source_repository,
        check=True,
        capture_output=True,
    )
    copied = []
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        for member in bundle.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise RuntimeError(f"Tracked workspace archive has a non-file entry: {member.name}")
            source_relative = Path(member.name)
            try:
                relative = source_relative.relative_to("simworld_studio_workspace")
            except ValueError as error:
                raise RuntimeError(f"Tracked workspace entry escapes source root: {member.name}") from error
            if not _is_allowlisted_workspace_path(relative, copy_paths):
                raise RuntimeError(f"Tracked workspace entry is not allowlisted: {member.name}")
            target = destination / relative
            try:
                target.parent.resolve().relative_to(destination)
            except ValueError as error:
                raise RuntimeError(f"Tracked workspace entry escapes destination: {member.name}") from error
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError(f"Tracked workspace entry could not be read: {member.name}")
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            target.chmod(member.mode & 0o777)
            copied.append(relative)
    for allowed in copy_paths:
        if not any(
            relative.as_posix() == allowed
            or relative.as_posix().startswith(f"{allowed}/")
            for relative in copied
        ):
            raise RuntimeError(f"Allowlisted tracked source path is missing from HEAD: {allowed}")
    return len(copied)


def run_launcher_validation(source_repository: Path, workspace: Path) -> None:
    """Validate the completed release with the launcher from the same commit."""

    packaging_root = source_repository / "packaging"
    script = (
        "import sys; sys.path.insert(0, sys.argv[3]); from pathlib import Path; "
        "from simworld_arena.launcher import validate_prepared_workspace; "
        "validate_prepared_workspace(Path(sys.argv[1]), Path(sys.argv[2]))"
    )
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(workspace),
            str(source_repository / "packaging" / "simworld_arena" / "security-manifest.json"),
            str(packaging_root),
        ],
        cwd=source_repository,
        check=True,
    )


def stage(source_repository: Path, release: Path) -> Path:
    source_repository = source_repository.resolve()
    release = release.resolve()
    commit = git_output(source_repository, "rev-parse", "HEAD")
    tree = git_output(source_repository, "rev-parse", "HEAD^{tree}")
    status = git_output(source_repository, "status", "--porcelain")
    if status:
        raise RuntimeError("source repository must be clean before staging a release")
    release.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    release.mkdir(mode=0o700, exist_ok=False)
    workspace = release / "workspace"
    workspace.mkdir(mode=0o700)

    try:
        tracked_file_count = copy_tracked_workspace(source_repository, workspace)
        for relative in RUNTIME_DIRS:
            (workspace / relative).mkdir(mode=0o700, parents=True, exist_ok=True)

        mcp_config = {"mcpServers": {}}
        (workspace / "web" / "mcp.json").write_text(
            json.dumps(mcp_config, indent=2) + "\n",
            encoding="utf-8",
        )

        runtime_root = release.parent.parent
        npm_cache = runtime_root / "cache" / "npm"
        npm_cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["npm_config_cache"] = str(npm_cache)
        install = ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
        subprocess.run(install, cwd=workspace / "web", env=environment, check=True)
        subprocess.run(["npm", "run", "build"], cwd=workspace / "web", env=environment, check=True)
        # The frontend is served from dist. Do not retain its build-time dependency
        # tree where Node's parent-directory resolution could load it at runtime.
        shutil.rmtree(workspace / "web" / "node_modules")
        subprocess.run(
            [*install, "--omit=dev"],
            cwd=workspace / "web" / "server",
            env=environment,
            check=True,
        )
        dist_index = workspace / "web" / "dist" / "index.html"
        server_root = workspace / "web" / "server"
        node_modules = server_root / "node_modules"
        express_package = node_modules / "express" / "package.json"
        if not dist_index.is_file() or not express_package.is_file():
            raise RuntimeError("staged frontend build or server dependencies are incomplete")

        receipt = {
            "schema": "vista-simworld-staged-workspace/v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_repository": str(source_repository),
            "source_commit": commit,
            "source_tree": tree,
            "source_copy": {
                "policy": "git-archive-head-allowlist/v1",
                "tracked_file_count": tracked_file_count,
            },
            "workspace": str(workspace),
            "model_mode": "off",
            "lockfiles": {
                "web/package-lock.json": sha256_file(workspace / "web" / "package-lock.json"),
                "web/server/package-lock.json": sha256_file(
                    workspace / "web" / "server" / "package-lock.json"
                ),
            },
            "security_files": {
                "web/mcp.json": sha256_file(workspace / "web" / "mcp.json"),
                "runtime-security.js": sha256_file(
                    workspace / "web" / "server" / "runtime-security.js"
                ),
                "vista-runtime-broker.js": sha256_file(
                    workspace / "web" / "server" / "vista-runtime-broker.js"
                ),
                "agent-sandbox.js": sha256_file(
                    workspace / "web" / "server" / "agent-sandbox.js"
                ),
                "web/public/ue-player.html": sha256_file(
                    workspace / "web" / "public" / "ue-player.html"
                ),
                "index.js": sha256_file(workspace / "web" / "server" / "index.js"),
            },
            "artifacts": {
                "web/dist/index.html": sha256_file(dist_index),
                "web/dist/tree_sha256": sha256_tree(dist_index.parent),
                "web/server/node_modules/express/package.json": sha256_file(express_package),
                "web/server/source_tree_sha256": sha256_server_source_tree(server_root),
                "web/server/node_modules/tree_sha256": sha256_dependency_tree(node_modules),
            },
            "launcher_validation": {
                "validator": "simworld_arena.launcher.validate_prepared_workspace",
                "launcher_sha256": sha256_file(
                    source_repository / "packaging" / "simworld_arena" / "launcher.py"
                ),
                "status": "passed",
            },
        }
        receipt_path = release / "source-receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        run_launcher_validation(source_repository, workspace)
        os.chmod(release, 0o700)
        os.chmod(workspace, 0o700)
        return receipt_path
    except Exception:
        shutil.rmtree(release, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--release", type=Path, required=True)
    args = parser.parse_args()
    print(stage(args.source_repository, args.release))


if __name__ == "__main__":
    main()
