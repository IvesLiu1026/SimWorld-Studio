#!/usr/bin/env python3
"""Create one private, immutable-by-convention Studio release workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
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


def git_output(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def copy_path(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"required source path is missing: {source}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns("node_modules", "__pycache__", "*.pyc"),
        )
    else:
        shutil.copy2(source, destination)


def stage(source_repository: Path, release: Path) -> Path:
    source_repository = source_repository.resolve()
    source_workspace = source_repository / "simworld_studio_workspace"
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
        for relative in COPY_PATHS:
            copy_path(source_workspace / relative, workspace / relative)
        for relative in RUNTIME_DIRS:
            (workspace / relative).mkdir(mode=0o700, parents=True, exist_ok=True)

        mcp_config = {
            "mcpServers": {
                "simworld": {
                    "command": "node",
                    "args": [str(workspace / "web" / "server" / "mcp-server.js")],
                    "env": {"UNREAL_HOST": "127.0.0.1", "UNREAL_PORT": "55560"},
                }
            }
        }
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
        subprocess.run(
            [*install, "--omit=dev"],
            cwd=workspace / "web" / "server",
            env=environment,
            check=True,
        )
        dist_index = workspace / "web" / "dist" / "index.html"
        express_package = workspace / "web" / "server" / "node_modules" / "express" / "package.json"
        if not dist_index.is_file() or not express_package.is_file():
            raise RuntimeError("staged frontend build or server dependencies are incomplete")

        receipt = {
            "schema": "vista-simworld-staged-workspace/v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_repository": str(source_repository),
            "source_commit": commit,
            "source_tree": tree,
            "workspace": str(workspace),
            "model_mode": "off",
            "lockfiles": {
                "web/package-lock.json": sha256_file(workspace / "web" / "package-lock.json"),
                "web/server/package-lock.json": sha256_file(
                    workspace / "web" / "server" / "package-lock.json"
                ),
            },
            "security_files": {
                "runtime-security.js": sha256_file(
                    workspace / "web" / "server" / "runtime-security.js"
                ),
                "index.js": sha256_file(workspace / "web" / "server" / "index.js"),
            },
            "artifacts": {
                "web/dist/index.html": sha256_file(dist_index),
                "web/server/node_modules/express/package.json": sha256_file(express_package),
            },
        }
        receipt_path = release / "source-receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
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
