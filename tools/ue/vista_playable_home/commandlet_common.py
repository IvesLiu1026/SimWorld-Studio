"""Shared fail-closed helpers for fixed UE Editor commandlets."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from typing import Any


EXECUTION_SCHEMA = "simworld.vista.playable-home-ue-execution/v1"
IMPORT_RECEIPT_SCHEMA = "simworld.vista.playable-home-ue-import-receipt/v1"
SCENE_RECEIPT_SCHEMA = "simworld.vista.playable-home-ue-scene-receipt/v1"
EXECUTION_ENV = "VISTA_PLAYABLE_HOME_EXECUTION"
EXECUTION_SHA_ENV = "VISTA_PLAYABLE_HOME_EXECUTION_SHA256"
IMPORT_RECEIPT_SHA_ENV = "VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256"
IMPORT_MARKER = "VISTA_PLAYABLE_HOME_IMPORT_RESULT:"
SCENE_MARKER = "VISTA_PLAYABLE_HOME_SCENE_RESULT:"
IMPORT_RESULT_FILE = "import-result.json"
SCENE_RESULT_FILE = "scene-result.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_PATH_PARTS = {
    "archive", "archives", "canonical", "production", "release", "releases",
    "r8", "disposable-project-r8",
}
BUILTIN_URI_ALLOWLIST = {
    "builtin://vista/playable-home/pawn": {
        "object_path": "/Script/VistaPlayableHome.VistaPlayableHomeCharacter",
        "kind": "class",
    },
    "builtin://vista/playable-home/game-mode": {
        "object_path": "/Script/VistaPlayableHome.VistaPlayableHomeGameMode",
        "kind": "class",
    },
    "builtin://vista/playable-home/npc": {
        "object_path": "/Script/VistaPlayableHome.VistaHomeNpcCharacter",
        "kind": "class",
    },
    "builtin://engine/basic-shapes/cube": {
        "object_path": "/Engine/BasicShapes/Cube.Cube",
        "kind": "asset",
    },
}


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def require_sha(value: Any, label: str) -> str:
    require(isinstance(value, str) and SHA256.fullmatch(value) is not None,
            label + " digest invalid")
    return value


def canonical_path(value: Any) -> str:
    return os.path.realpath(os.path.abspath(str(value))).replace("\\", "/")


def safe_attempt_child(path: Any, attempt_root: Any, label: str) -> str:
    resolved = canonical_path(path)
    root = canonical_path(attempt_root)
    require(resolved == root or resolved.startswith(root + "/"), label + " escapes attempt root")
    require(not any(part.casefold() in FORBIDDEN_PATH_PARTS
                    for part in pathlib.PurePosixPath(resolved).parts),
            label + " uses a forbidden destination")
    return resolved


def asset_name(asset_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", asset_id)
    require(value and len(value) <= 128, "asset ID cannot form a UE name")
    return value


def derived_asset_path(namespace: str, asset: dict[str, Any]) -> str:
    if asset["source_kind"] == "builtin":
        entry = BUILTIN_URI_ALLOWLIST.get(asset["uri"])
        require(entry is not None, "builtin URI is not allowlisted: " + asset["uri"])
        return entry["object_path"]
    name = asset_name(asset["asset_id"])
    return namespace + "/Assets/" + name + "/" + name + "." + name


def load_execution(script_kind: str, script_file: str) -> tuple[dict[str, Any], str, str]:
    manifest_path = canonical_path(os.environ.get(EXECUTION_ENV, ""))
    expected_sha = require_sha(os.environ.get(EXECUTION_SHA_ENV, ""), "execution manifest")
    require(os.path.isfile(manifest_path), "execution manifest missing")
    require(sha256_file(manifest_path) == expected_sha, "execution manifest digest mismatch")
    with open(manifest_path, "r", encoding="utf-8") as source:
        execution = json.load(source)
    require(execution.get("schema_version") == EXECUTION_SCHEMA, "execution schema mismatch")
    policy = execution.get("policy", {})
    require(policy == {
        "append_only_namespace": True,
        "quarantine_on_failure": True,
        "replace_existing": False,
        "save_reload_required": True,
        "studio_socket_fallback_allowed": False,
    }, "execution safety policy mismatch")
    attempt_root = canonical_path(execution["attempt_root"])
    safe_attempt_child(manifest_path, attempt_root, "execution manifest")
    project = safe_attempt_child(execution["project_file"], attempt_root, "project")
    require(canonical_path(os.environ.get("VISTA_PLAYABLE_HOME_PROJECT", project)) == project,
            "project environment mismatch")
    require(os.path.isfile(project) and sha256_file(project) == execution["project_sha256"],
            "project pin mismatch")
    plan_path = safe_attempt_child(execution["build_plan_path"], attempt_root, "build plan")
    require(os.path.isfile(plan_path) and sha256_file(plan_path) == execution["build_plan_sha256"],
            "build plan pin mismatch")
    composition = execution["composition_spec"]
    require(hashlib.sha256(canonical_json(composition)).hexdigest() == execution["composition_spec_sha256"],
            "composition spec digest mismatch")
    scripts = execution.get("scripts")
    require(isinstance(scripts, dict) and set(scripts) == {"import", "compose", "common"},
            "execution script pins differ")
    common_contract = scripts["common"]
    require(canonical_path(__file__) == canonical_path(common_contract["path"]),
            "commandlet common helper identity mismatch")
    require(sha256_file(__file__) == common_contract["sha256"],
            "commandlet common helper digest mismatch")
    script_contract = scripts[script_kind]
    require(canonical_path(script_file) == canonical_path(script_contract["path"]),
            "commandlet script identity mismatch")
    require(sha256_file(script_file) == script_contract["sha256"],
            "commandlet script digest mismatch")
    return execution, manifest_path, expected_sha


def load_build_plan(execution: dict[str, Any]) -> dict[str, Any]:
    with open(execution["build_plan_path"], "r", encoding="utf-8") as source:
        plan = json.load(source)
    require(plan.get("content_digest") == execution["build_plan_content_digest"],
            "build plan content digest mismatch")
    return plan


def write_exclusive_receipt(path: str, attempt_root: str, receipt: dict[str, Any]) -> str:
    output = safe_attempt_child(path, attempt_root, "receipt")
    require(os.path.dirname(output) == canonical_path(attempt_root),
            "receipt must be a direct attempt-root child")
    raw = canonical_json(receipt)
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL |
        getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return hashlib.sha256(raw).hexdigest()
