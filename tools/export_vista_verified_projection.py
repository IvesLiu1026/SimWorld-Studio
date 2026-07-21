#!/usr/bin/env python3
"""Project one explicit private VISTA selection into a verified-source bundle.

The exporter is deliberately offline, path-explicit, and fail-closed. It
understands only the reviewed raw schemas documented for the VISTA
``mmg_040`` handoff. It never scans the dataset tree and never treats private
review metadata as dataset-owner approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

import yaml
from yaml.resolver import BaseResolver
from yaml.tokens import AliasToken, AnchorToken, DirectiveToken, TagToken

import stage_vista_import_bundle as staging


RESULT_SCHEMA = "vista-verified-projection-result/v1"
REPORT_SCHEMA = "vista-verified-projection-report/v1"
TREE_DIGEST_ALGORITHM = "vista-verified-projection-tree-sha256/v1"

MAX_NO_ORACLE_BYTES = 16 * 1024 * 1024
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_RENDER_BYTES = staging.MAX_RENDER_SCRIPT_BYTES
MAX_SUMMARY_BYTES = 4 * 1024 * 1024
MAX_MEDIA_BYTES = staging.MAX_MEDIA_BYTES
MAX_JSONL_ROWS = 100_000
MAX_TEXT = 32_000

OUTPUT_RENDER = "render_script.yaml"
OUTPUT_DIALOGUE = "dialogue.no-oracle.json"
OUTPUT_MANIFEST = "verified-manifest.json"
OUTPUT_REPORT = "provenance-report.json"
OUTPUT_MEDIA = "media/reference.mp4"

NO_ORACLE_FIELDS = frozenset(
    {
        "row_id",
        "dataset_source",
        "handoff_split",
        "visual_id",
        "pairing_id",
        "case_scope",
        "scenario_type",
        "review_group",
        "video_model_key",
        "video_attempt_index",
        "video_path",
        "video_url",
        "video_r2_url",
        "video_r2_object_key",
        "video_r2_url_expires_in_seconds",
        "video_r2_url_for_model_api",
        "video_r2_object_key_for_model_api",
        "video_r2_url_for_model_api_expires_in_seconds",
        "video_r2_url_for_model_api_is_provider_normalized",
        "reference_image_path",
        "reference_image_url",
        "duration_seconds",
        "dialogue_en",
        "dialogue_time_sec",
    }
)
NO_ORACLE_OMITTED_FIELDS = sorted(
    NO_ORACLE_FIELDS
    - {
        "row_id",
        "dataset_source",
        "visual_id",
        "case_scope",
        "scenario_type",
        "video_model_key",
        "video_attempt_index",
        "dialogue_en",
    }
)

LEDGER_ROOT_FIELDS = frozenset({"schema_version", "artifact_root", "attempts"})
LEDGER_ATTEMPT_FIELDS = frozenset(
    {
        "attempt_index",
        "script_revision",
        "script_origin",
        "trigger",
        "status",
        "started_at",
        "completed_at",
        "reference_image_path",
        "video_path",
        "media_summary_path",
        "triggered_by",
        "review_decision",
        "reviewed_by",
        "reviewed_at",
        "review_note",
        "selected_for_export",
        "usable_from_seconds",
        "usable_until_seconds",
    }
)
LEDGER_ATTEMPT_REQUIRED = LEDGER_ATTEMPT_FIELDS - {"usable_from_seconds"}

SUMMARY_FIELDS = frozenset(
    {
        "status",
        "artifact_root",
        "case_id",
        "model_key",
        "attempt_index",
        "trigger",
        "render_script_path",
        "reference_image_path",
        "video_path",
        "video_prompt",
        "video_backend",
        "target_size",
        "seconds",
    }
)

RENDER_ROOT_FIELDS = frozenset(
    {"Global_Metadata", "Camera_Continuity", "Scene", "Exit_State", "Intervention_Cues"}
)
RENDER_METADATA_FIELDS = frozenset(
    {
        "Title",
        "Perspective",
        "Environment",
        "Lighting",
        "Emotional_Tone",
        "Duration_sec",
        "Dialogue_Delivery",
        "Speech_Policy",
        "Audio_Policy",
        "Viewpoint_Contract",
    }
)
RENDER_VIEWPOINT_FIELDS = frozenset(
    {"POV_Mode", "Camera_Rig", "Allowed_Body_Visibility", "Face_Visibility", "Forbidden_Views"}
)
RENDER_CAMERA_FIELDS = frozenset(
    {
        "Position",
        "Height",
        "Angle",
        "Motion",
        "Framing",
        "Head_Motion_Profile",
        "Forbidden_Camera_Behaviors",
    }
)
RENDER_SCENE_FIELDS = frozenset(
    {"Key_Visual_Elements", "Description", "Actions", "Dialogue", "Emotion"}
)
RENDER_EXIT_FIELDS = frozenset({"Action", "Audio"})
RENDER_INTERVENTION_FIELDS = frozenset(
    {"Signal_State", "Signal_Location", "User_Awareness", "Reasoning", "Trigger_Condition"}
)

APPROVAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{5,255}$")
TARGET_SIZE_RE = re.compile(r"^(\d{1,5})x(\d{1,5})$")
URI_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE)
SIGNED_VALUE_RE = re.compile(
    r"(?:x-amz-(?:credential|signature|security-token)|(?:^|[?&])signature=|"
    r"(?:^|[?&])expires=|-----BEGIN [A-Z ]+PRIVATE KEY-----)",
    re.IGNORECASE,
)
ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|[\s\"'`(])(?:/(?:home|mnt|srv|data|nas|tmp|var|Users)/|[A-Za-z]:[\\/])"
)


class VistaProjectionError(RuntimeError):
    """A typed projection error whose public representation never echoes data."""

    def __init__(self, code: str, message: str, *, pointer: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.pointer = pointer

    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.pointer:
            result["pointer"] = self.pointer
        return result


def fail(code: str, message: str, *, pointer: str | None = None) -> None:
    raise VistaProjectionError(code, message, pointer=pointer)


def exact_object(
    value: Any,
    fields: frozenset[str],
    pointer: str,
    *,
    required: frozenset[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("VISTA_PROJECTION_SCHEMA_INVALID", "Expected an object", pointer=pointer)
    keys = set(value)
    if keys - fields:
        fail(
            "VISTA_PROJECTION_SCHEMA_DRIFT",
            "Raw source contains fields outside the reviewed allowlist",
            pointer=pointer,
        )
    required_fields = fields if required is None else required
    if required_fields - keys:
        fail(
            "VISTA_PROJECTION_SCHEMA_INVALID",
            "Raw source is missing reviewed fields",
            pointer=pointer,
        )
    return value


def text_value(value: Any, pointer: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail("VISTA_PROJECTION_SCHEMA_INVALID", "Expected a bounded non-empty string", pointer=pointer)
    return value.strip()


def integer_value(value: Any, pointer: str, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        fail("VISTA_PROJECTION_SCHEMA_INVALID", "Expected a bounded integer", pointer=pointer)
    return value


def number_value(value: Any, pointer: str, *, minimum: float = 0, maximum: float = 3600) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("VISTA_PROJECTION_SCHEMA_INVALID", "Expected a finite number", pointer=pointer)
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        fail("VISTA_PROJECTION_SCHEMA_INVALID", "Number is outside the reviewed range", pointer=pointer)
    return result


def nullable_number(value: Any, pointer: str) -> float | None:
    return None if value is None else number_value(value, pointer)


def nullable_text(value: Any, pointer: str) -> str | None:
    return None if value is None else text_value(value, pointer)


def string_list(value: Any, pointer: str, *, empty_allowed: bool = False, maximum: int = 200) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum or (not empty_allowed and not value):
        fail("VISTA_PROJECTION_SCHEMA_INVALID", "Expected a bounded string array", pointer=pointer)
    return [text_value(item, f"{pointer}/{index}") for index, item in enumerate(value)]


class _DuplicateJsonKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def parse_json(value: bytes, pointer: str) -> Any:
    try:
        parsed = json.loads(value.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, RecursionError):
        fail("VISTA_PROJECTION_JSON_INVALID", "Source is not unique-key UTF-8 JSON", pointer=pointer)
    staging.guard_tree(parsed, pointer)
    return parsed


class UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: UniqueSafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            fail("VISTA_PROJECTION_YAML_INVALID", "YAML keys must be unique strings", pointer="raw_render_script")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueSafeLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def parse_yaml(value: bytes) -> dict[str, Any]:
    try:
        source = value.decode("utf-8")
    except UnicodeDecodeError:
        fail("VISTA_PROJECTION_YAML_INVALID", "Render script must be UTF-8", pointer="raw_render_script")
    if "\x00" in source:
        fail("VISTA_PROJECTION_YAML_INVALID", "Render script contains a NUL byte", pointer="raw_render_script")
    try:
        for token in yaml.scan(source):
            if isinstance(token, (AliasToken, AnchorToken, DirectiveToken, TagToken)):
                fail(
                    "VISTA_PROJECTION_YAML_UNSAFE",
                    "YAML aliases, anchors, directives, and tags are forbidden",
                    pointer="raw_render_script",
                )
        parsed = yaml.load(source, Loader=UniqueSafeLoader)
    except VistaProjectionError:
        raise
    except (yaml.YAMLError, RecursionError):
        fail("VISTA_PROJECTION_YAML_INVALID", "Render script is not safe valid YAML", pointer="raw_render_script")
    staging.guard_tree(parsed, "raw_render_script")
    return exact_object(parsed, RENDER_ROOT_FIELDS, "raw_render_script")


def _assert_emitted_text_safe(value: Any, pointer: str = "emitted") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_emitted_text_safe(item, f"{pointer}/{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_emitted_text_safe(item, f"{pointer}/{index}")
        return
    if not isinstance(value, str):
        return
    if URI_RE.search(value) or SIGNED_VALUE_RE.search(value) or ABSOLUTE_PATH_RE.search(value):
        fail(
            "VISTA_PROJECTION_LEAKAGE_REJECTED",
            "An emitted text field resembles a URL, credential, or absolute storage path",
            pointer=pointer,
        )


def _declared_path_matches(
    value: Any,
    dataset: staging.DatasetRoot,
    expected_relative: str,
    pointer: str,
) -> None:
    declaration = text_value(value, pointer, maximum=4096)
    expected_absolute = str(dataset.path(expected_relative, pointer))
    if os.path.isabs(declaration):
        matches = declaration == os.path.normpath(declaration) == expected_absolute
    else:
        try:
            matches = staging.safe_relative_path(declaration, pointer) == expected_relative
        except staging.VistaStagingError:
            matches = False
    if not matches:
        fail(
            "VISTA_PROJECTION_PATH_MISMATCH",
            "Raw path declaration does not match the explicit selected path",
            pointer=pointer,
        )


def _declared_directory_matches(
    value: Any,
    dataset: staging.DatasetRoot,
    expected_relative: str,
    pointer: str,
) -> None:
    declaration = text_value(value, pointer, maximum=4096)
    expected_absolute = str(dataset.root.joinpath(*PurePosixPath(expected_relative).parts))
    if os.path.isabs(declaration):
        matches = declaration == os.path.normpath(declaration) == expected_absolute
    else:
        try:
            matches = staging.safe_relative_path(declaration, pointer) == expected_relative
        except staging.VistaStagingError:
            matches = False
    if not matches:
        fail(
            "VISTA_PROJECTION_PATH_MISMATCH",
            "Raw directory declaration does not match the explicit source scope",
            pointer=pointer,
        )


def load_selected_no_oracle_row(value: bytes, row_id: str) -> dict[str, Any]:
    try:
        source = value.decode("utf-8")
    except UnicodeDecodeError:
        fail("VISTA_PROJECTION_JSON_INVALID", "No-oracle JSONL must be UTF-8", pointer="no_oracle_jsonl")
    matches: list[dict[str, Any]] = []
    row_count = 0
    for line_number, line in enumerate(source.splitlines(), start=1):
        if not line.strip():
            continue
        row_count += 1
        if row_count > MAX_JSONL_ROWS or len(line.encode("utf-8")) > 1024 * 1024:
            fail("VISTA_PROJECTION_SOURCE_TOO_COMPLEX", "No-oracle JSONL exceeds safety limits", pointer="no_oracle_jsonl")
        parsed = parse_json(line.encode("utf-8"), f"no_oracle_jsonl/line/{line_number}")
        if isinstance(parsed, dict) and parsed.get("row_id") == row_id:
            matches.append(parsed)
    if len(matches) != 1:
        fail(
            "VISTA_PROJECTION_SELECTION_INVALID",
            "No-oracle JSONL must contain exactly one requested row",
            pointer="no_oracle_jsonl",
        )
    return matches[0]


def validate_no_oracle_row(
    raw: Any,
    *,
    dataset: staging.DatasetRoot,
    dataset_revision: str,
    row_id: str,
    visual_id: str,
    case_scope: str,
    scenario_type: str,
    provider: str,
    attempt: int,
    media_relative: str,
) -> tuple[list[dict[str, Any]], float | None]:
    row = exact_object(raw, NO_ORACLE_FIELDS, "no_oracle_row")
    expected_pairing = f"{visual_id}__{scenario_type}"
    expected_row = f"{expected_pairing}::{case_scope}::{provider}::attempt_{attempt:03d}"
    expected_identity = {
        "row_id": row_id,
        "dataset_source": dataset_revision,
        "visual_id": visual_id,
        "pairing_id": expected_pairing,
        "case_scope": case_scope,
        "scenario_type": scenario_type,
        "video_model_key": provider,
        "video_attempt_index": attempt,
    }
    if row_id != expected_row:
        fail("VISTA_PROJECTION_IDENTITY_MISMATCH", "Requested row id is not canonical", pointer="--row-id")
    for field, expected in expected_identity.items():
        if row[field] != expected:
            fail("VISTA_PROJECTION_IDENTITY_MISMATCH", "No-oracle identity does not match the request", pointer=f"no_oracle_row/{field}")

    text_value(row["handoff_split"], "no_oracle_row/handoff_split", maximum=128)
    nullable_text(row["review_group"], "no_oracle_row/review_group")
    _declared_path_matches(row["video_path"], dataset, media_relative, "no_oracle_row/video_path")
    for field in (
        "video_url",
        "video_r2_url",
        "video_r2_object_key",
        "video_r2_url_for_model_api",
        "video_r2_object_key_for_model_api",
        "reference_image_path",
    ):
        text_value(row[field], f"no_oracle_row/{field}", maximum=16_384)
    nullable_text(row["reference_image_url"], "no_oracle_row/reference_image_url")
    for field in (
        "video_r2_url_expires_in_seconds",
        "video_r2_url_for_model_api_expires_in_seconds",
    ):
        number_value(row[field], f"no_oracle_row/{field}", maximum=10_000_000)
    if not isinstance(row["video_r2_url_for_model_api_is_provider_normalized"], bool):
        fail("VISTA_PROJECTION_SCHEMA_INVALID", "Expected a boolean", pointer="no_oracle_row/video_r2_url_for_model_api_is_provider_normalized")
    declared_duration = nullable_number(row["duration_seconds"], "no_oracle_row/duration_seconds")
    number_value(row["dialogue_time_sec"], "no_oracle_row/dialogue_time_sec")

    raw_turns = row["dialogue_en"]
    if not isinstance(raw_turns, list) or len(raw_turns) > 200:
        fail("VISTA_PROJECTION_SCHEMA_INVALID", "Dialogue must be a bounded array", pointer="no_oracle_row/dialogue_en")
    turns: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, raw_turn in enumerate(raw_turns):
        pointer = f"no_oracle_row/dialogue_en/{index}"
        turn = exact_object(raw_turn, frozenset({"turn_id", "role", "speaker", "text"}), pointer)
        turn_id = integer_value(turn["turn_id"], f"{pointer}/turn_id", minimum=1)
        if turn_id in seen or turn["role"] != "context" or turn["speaker"] not in {"other_person", "user"}:
            fail("VISTA_PROJECTION_DIALOGUE_INVALID", "Dialogue identity or privilege is not allowlisted", pointer=pointer)
        seen.add(turn_id)
        turns.append(
            {
                "turn_id": turn_id,
                "role": "context",
                "speaker": turn["speaker"],
                "text": text_value(turn["text"], f"{pointer}/text", maximum=8000),
            }
        )
    _assert_emitted_text_safe(turns, "dialogue_no_oracle/turns")
    return turns, declared_duration


def validate_attempt_ledger(
    raw: Any,
    *,
    dataset: staging.DatasetRoot,
    ledger_relative: str,
    summary_relative: str,
    media_relative: str,
    attempt: int,
) -> None:
    ledger = exact_object(raw, LEDGER_ROOT_FIELDS, "attempt_ledger")
    if ledger["schema_version"] != "1.0":
        fail("VISTA_PROJECTION_SCHEMA_INVALID", "Unsupported attempt-ledger schema", pointer="attempt_ledger/schema_version")
    media_root = PurePosixPath(ledger_relative).parent.as_posix()
    _declared_directory_matches(ledger["artifact_root"], dataset, media_root, "attempt_ledger/artifact_root")
    attempts = ledger["attempts"]
    if not isinstance(attempts, list) or not attempts or len(attempts) > 10_000:
        fail("VISTA_PROJECTION_SCHEMA_INVALID", "Attempt ledger must be a bounded array", pointer="attempt_ledger/attempts")
    seen: set[int] = set()
    selected: list[dict[str, Any]] = []
    for position, raw_attempt in enumerate(attempts):
        pointer = f"attempt_ledger/attempts/{position}"
        item = exact_object(raw_attempt, LEDGER_ATTEMPT_FIELDS, pointer, required=LEDGER_ATTEMPT_REQUIRED)
        index = integer_value(item["attempt_index"], f"{pointer}/attempt_index", minimum=1)
        if index in seen:
            fail("VISTA_PROJECTION_SELECTION_INVALID", "Attempt ledger contains duplicate indexes", pointer=pointer)
        seen.add(index)
        for field in (
            "script_revision",
            "script_origin",
            "trigger",
            "status",
            "started_at",
            "completed_at",
            "reference_image_path",
            "video_path",
            "media_summary_path",
            "triggered_by",
            "review_decision",
            "reviewed_by",
            "reviewed_at",
            "review_note",
        ):
            text_value(item[field], f"{pointer}/{field}", maximum=16_384)
        if not isinstance(item["selected_for_export"], bool):
            fail("VISTA_PROJECTION_SCHEMA_INVALID", "Selection flag must be boolean", pointer=f"{pointer}/selected_for_export")
        nullable_number(item.get("usable_from_seconds"), f"{pointer}/usable_from_seconds")
        nullable_number(item["usable_until_seconds"], f"{pointer}/usable_until_seconds")
        if item["selected_for_export"]:
            selected.append(item)
    if len(selected) != 1 or selected[0]["attempt_index"] != attempt or selected[0]["status"] != "completed":
        fail(
            "VISTA_PROJECTION_SELECTION_INVALID",
            "Requested attempt must be the sole completed selected-for-export entry",
            pointer="attempt_ledger/attempts",
        )
    _declared_path_matches(selected[0]["media_summary_path"], dataset, summary_relative, "attempt_ledger/selected/media_summary_path")
    _declared_path_matches(selected[0]["video_path"], dataset, media_relative, "attempt_ledger/selected/video_path")


def validate_media_summary(
    raw: Any,
    *,
    dataset: staging.DatasetRoot,
    ledger_relative: str,
    render_relative: str,
    media_relative: str,
    case_scope: str,
    provider: str,
    attempt: int,
) -> tuple[float, int, int]:
    summary = exact_object(raw, SUMMARY_FIELDS, "media_summary")
    media_root = PurePosixPath(ledger_relative).parent.as_posix()
    _declared_directory_matches(summary["artifact_root"], dataset, media_root, "media_summary/artifact_root")
    if (
        summary["status"] != "ok"
        or summary["case_id"] != case_scope
        or summary["model_key"] != provider
        or summary["video_backend"] != provider
        or summary["attempt_index"] != attempt
    ):
        fail("VISTA_PROJECTION_IDENTITY_MISMATCH", "Media summary identity is not the selected attempt", pointer="media_summary")
    for field in ("trigger", "reference_image_path", "video_prompt"):
        text_value(summary[field], f"media_summary/{field}", maximum=32_000)
    _declared_path_matches(summary["render_script_path"], dataset, render_relative, "media_summary/render_script_path")
    _declared_path_matches(summary["video_path"], dataset, media_relative, "media_summary/video_path")
    duration = number_value(summary["seconds"], "media_summary/seconds", minimum=0.001)
    target = text_value(summary["target_size"], "media_summary/target_size", maximum=32)
    match = TARGET_SIZE_RE.fullmatch(target)
    if not match:
        fail("VISTA_PROJECTION_MEDIA_INVALID", "Media target size must be WIDTHxHEIGHT", pointer="media_summary/target_size")
    width, height = int(match.group(1)), int(match.group(2))
    if not 1 <= width <= 16_384 or not 1 <= height <= 16_384:
        fail("VISTA_PROJECTION_MEDIA_INVALID", "Media target size is out of range", pointer="media_summary/target_size")
    return duration, width, height


def sanitize_render_script(raw: Any, duration_sec: float) -> bytes:
    root = exact_object(raw, RENDER_ROOT_FIELDS, "raw_render_script")
    metadata = exact_object(root["Global_Metadata"], RENDER_METADATA_FIELDS, "raw_render_script/Global_Metadata")
    viewpoint = exact_object(
        metadata["Viewpoint_Contract"], RENDER_VIEWPOINT_FIELDS, "raw_render_script/Global_Metadata/Viewpoint_Contract"
    )
    metadata_text = {
        field: text_value(metadata[field], f"raw_render_script/Global_Metadata/{field}")
        for field in (
            "Title",
            "Perspective",
            "Environment",
            "Lighting",
            "Emotional_Tone",
            "Dialogue_Delivery",
            "Speech_Policy",
            "Audio_Policy",
        )
    }
    for field in ("POV_Mode", "Camera_Rig", "Face_Visibility"):
        text_value(viewpoint[field], f"raw_render_script/Global_Metadata/Viewpoint_Contract/{field}")
    string_list(viewpoint["Allowed_Body_Visibility"], "raw_render_script/Global_Metadata/Viewpoint_Contract/Allowed_Body_Visibility", empty_allowed=True)
    string_list(viewpoint["Forbidden_Views"], "raw_render_script/Global_Metadata/Viewpoint_Contract/Forbidden_Views", empty_allowed=True)
    raw_duration = number_value(metadata["Duration_sec"], "raw_render_script/Global_Metadata/Duration_sec", minimum=0.001)
    if abs(raw_duration - duration_sec) > 0.001:
        fail("VISTA_PROJECTION_DURATION_MISMATCH", "Render and media durations differ", pointer="raw_render_script/Global_Metadata/Duration_sec")

    camera = exact_object(root["Camera_Continuity"], RENDER_CAMERA_FIELDS, "raw_render_script/Camera_Continuity")
    camera_text = {
        field: text_value(camera[field], f"raw_render_script/Camera_Continuity/{field}")
        for field in ("Position", "Height", "Angle", "Motion", "Framing", "Head_Motion_Profile")
    }
    string_list(camera["Forbidden_Camera_Behaviors"], "raw_render_script/Camera_Continuity/Forbidden_Camera_Behaviors", empty_allowed=True)

    scene = exact_object(root["Scene"], RENDER_SCENE_FIELDS, "raw_render_script/Scene")
    elements = string_list(scene["Key_Visual_Elements"], "raw_render_script/Scene/Key_Visual_Elements")
    actions = string_list(scene["Actions"], "raw_render_script/Scene/Actions", maximum=500)
    description = text_value(scene["Description"], "raw_render_script/Scene/Description")
    text_value(scene["Emotion"], "raw_render_script/Scene/Emotion")
    if scene["Dialogue"] != []:
        fail("VISTA_PROJECTION_DIALOGUE_INVALID", "Raw render dialogue must be empty", pointer="raw_render_script/Scene/Dialogue")

    exit_state = exact_object(root["Exit_State"], RENDER_EXIT_FIELDS, "raw_render_script/Exit_State")
    exit_action = text_value(exit_state["Action"], "raw_render_script/Exit_State/Action")
    text_value(exit_state["Audio"], "raw_render_script/Exit_State/Audio")
    intervention = exact_object(
        root["Intervention_Cues"], RENDER_INTERVENTION_FIELDS, "raw_render_script/Intervention_Cues"
    )
    for field in RENDER_INTERVENTION_FIELDS:
        text_value(intervention[field], f"raw_render_script/Intervention_Cues/{field}")

    sanitized = {
        "Global_Metadata": {
            "Title": metadata_text["Title"],
            "Perspective": metadata_text["Perspective"],
            "Environment": metadata_text["Environment"],
            "Lighting": metadata_text["Lighting"],
            "Duration_sec": duration_sec,
        },
        "Camera_Continuity": {
            field: camera_text[field]
            for field in ("Position", "Height", "Angle", "Motion", "Framing")
        },
        "Scene": {
            "Key_Visual_Elements": elements,
            "Description": description,
            "Actions": actions,
            "Dialogue": [],
        },
        "Exit_State": {"Action": exit_action},
    }
    _assert_emitted_text_safe(sanitized, "render_script")
    encoded = yaml.safe_dump(
        sanitized,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=10_000,
    ).encode("utf-8")
    if len(encoded) > MAX_RENDER_BYTES:
        fail("VISTA_PROJECTION_SOURCE_TOO_COMPLEX", "Sanitized render exceeds its limit", pointer="raw_render_script")
    return encoded


def file_entry(path: str, value: bytes) -> dict[str, Any]:
    return {"path": path, "sha256": staging.sha256_bytes(value), "bytes": len(value)}


def tree_digest(entries: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(TREE_DIGEST_ALGORITHM.encode("ascii") + b"\0")
    for entry in sorted(entries, key=lambda item: item["path"]):
        digest.update(entry["path"].encode("utf-8") + b"\0")
        digest.update(str(entry["bytes"]).encode("ascii") + b"\0")
        digest.update(bytes.fromhex(entry["sha256"]) + b"\0")
    return digest.hexdigest()


def _approval_hash(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            fail(
                "VISTA_PROJECTION_OWNER_APPROVAL_REQUIRED",
                "--apply requires an explicit immutable dataset-owner approval reference",
                pointer="--owner-approval-ref",
            )
        return None
    normalized = value.strip()
    if not APPROVAL_RE.fullmatch(normalized) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", normalized):
        fail(
            "VISTA_PROJECTION_OWNER_APPROVAL_INVALID",
            "Owner approval reference must be a bounded non-secret ticket or decision id",
            pointer="--owner-approval-ref",
        )
    return staging.sha256_bytes(normalized.encode("utf-8"))


@dataclass
class ProjectionPlan:
    dataset: staging.DatasetRoot
    output_dir: Path
    small_files: dict[str, bytes]
    media_source: staging.FileEvidence
    source_evidence: dict[str, staging.FileEvidence]
    source_limits: dict[str, int]
    projection_entries: list[dict[str, Any]]
    projection_digest_sha256: str
    report: dict[str, Any]
    approval_reference_sha256: str | None


def build_projection_plan(args: argparse.Namespace) -> ProjectionPlan:
    dataset = staging.DatasetRoot(args.dataset_root)
    dataset_revision = staging.require_string(args.dataset_revision, "--dataset-revision", pattern=staging.SAFE_ID_RE, maximum=128)
    row_id = staging.require_string(args.row_id, "--row-id", pattern=staging.SAFE_SOURCE_ID_RE, maximum=512)
    visual_id = staging.require_string(args.visual_id, "--visual-id", pattern=staging.SAFE_VISUAL_ID_RE, maximum=80)
    case_scope = staging.require_string(args.case_scope, "--case-scope", pattern=staging.SAFE_ID_RE, maximum=128)
    scenario_type = staging.require_string(args.scenario_type, "--scenario-type", pattern=staging.SAFE_ID_RE, maximum=128)
    if scenario_type not in staging.SCENARIO_TYPES:
        fail("VISTA_PROJECTION_IDENTITY_MISMATCH", "Scenario type is not allowlisted", pointer="--scenario-type")
    provider = staging.require_string(args.provider, "--provider", pattern=staging.SAFE_PROVIDER_RE, maximum=64)
    attempt = integer_value(args.attempt, "--attempt", minimum=1)
    approval_hash = _approval_hash(args.owner_approval_ref, required=bool(args.apply))

    selected_paths = {
        "no_oracle_jsonl": staging.safe_relative_path(args.no_oracle_jsonl, "--no-oracle-jsonl"),
        "attempt_ledger": staging.safe_relative_path(args.attempt_ledger, "--attempt-ledger"),
        "raw_render_script": staging.safe_relative_path(args.raw_render_script, "--raw-render-script"),
        "media_summary": staging.safe_relative_path(args.media_summary, "--media-summary"),
        "media": staging.safe_relative_path(args.media, "--media"),
    }
    if len(set(selected_paths.values())) != len(selected_paths):
        fail("VISTA_PROJECTION_PATH_INVALID", "Every selected source path must be distinct")
    if not selected_paths["media"].lower().endswith(".mp4"):
        fail("VISTA_PROJECTION_MEDIA_INVALID", "Selected media path must end in .mp4", pointer="--media")
    output_dir = staging.normalized_output_dir(dataset, args.output_dir)
    staging._validate_private_parent(output_dir.parent)

    limits = {
        "no_oracle_jsonl": MAX_NO_ORACLE_BYTES,
        "attempt_ledger": MAX_LEDGER_BYTES,
        "raw_render_script": MAX_RENDER_BYTES,
        "media_summary": MAX_SUMMARY_BYTES,
        "media": MAX_MEDIA_BYTES,
    }
    source_bytes: dict[str, bytes] = {}
    evidence: dict[str, staging.FileEvidence] = {}
    for role in ("no_oracle_jsonl", "attempt_ledger", "raw_render_script", "media_summary"):
        source_bytes[role], evidence[role] = dataset.read(selected_paths[role], f"--{role.replace('_', '-')}", limits[role])
    evidence["media"] = dataset.hash(selected_paths["media"], "--media", MAX_MEDIA_BYTES)

    raw_row = load_selected_no_oracle_row(source_bytes["no_oracle_jsonl"], row_id)
    turns, declared_row_duration = validate_no_oracle_row(
        raw_row,
        dataset=dataset,
        dataset_revision=dataset_revision,
        row_id=row_id,
        visual_id=visual_id,
        case_scope=case_scope,
        scenario_type=scenario_type,
        provider=provider,
        attempt=attempt,
        media_relative=selected_paths["media"],
    )
    validate_attempt_ledger(
        parse_json(source_bytes["attempt_ledger"], "attempt_ledger"),
        dataset=dataset,
        ledger_relative=selected_paths["attempt_ledger"],
        summary_relative=selected_paths["media_summary"],
        media_relative=selected_paths["media"],
        attempt=attempt,
    )
    duration, width, height = validate_media_summary(
        parse_json(source_bytes["media_summary"], "media_summary"),
        dataset=dataset,
        ledger_relative=selected_paths["attempt_ledger"],
        render_relative=selected_paths["raw_render_script"],
        media_relative=selected_paths["media"],
        case_scope=case_scope,
        provider=provider,
        attempt=attempt,
    )
    metadata = staging.inspect_mp4(dataset, evidence["media"])
    if (
        abs(metadata.duration_sec - duration) > 0.001
        or metadata.width != width
        or metadata.height != height
        or (declared_row_duration is not None and abs(declared_row_duration - duration) > 0.001)
    ):
        fail("VISTA_PROJECTION_MEDIA_MISMATCH", "Raw row, summary, and MP4 metadata do not agree", pointer="--media")

    render_bytes = sanitize_render_script(parse_yaml(source_bytes["raw_render_script"]), duration)
    identity = {
        "dataset_revision": dataset_revision,
        "source_row_id": row_id,
        "visual_id": visual_id,
        "case_scope": case_scope,
        "scenario_type": scenario_type,
        "attempt": {"provider": provider, "index": attempt, "selected": True},
    }
    dialogue = {
        "schema": staging.DIALOGUE_SCHEMA,
        "profile": "evaluation_safe",
        "privilege": {"classification": "evaluation_safe", "evaluation_input_allowed": True},
        "source": identity,
        "turns": turns,
    }
    dialogue_bytes = staging.canonical_json_bytes(dialogue)
    declarations = {
        "render_script": file_entry(OUTPUT_RENDER, render_bytes),
        "dialogue_no_oracle": file_entry(OUTPUT_DIALOGUE, dialogue_bytes),
        "media": {
            "path": OUTPUT_MEDIA,
            "media_id": f"{visual_id}:{provider}:attempt_{attempt:03d}:video",
            "media_type": "video/mp4",
            "sha256": evidence["media"].sha256,
            "bytes": evidence["media"].bytes,
            "duration_sec": duration,
            "width": width,
            "height": height,
        },
    }
    record = {
        "schema": staging.VERIFIED_RECORD_SCHEMA,
        "dataset_revision": dataset_revision,
        "source_row_id": row_id,
        "visual_id": visual_id,
        "case_scope": case_scope,
        "scenario_type": scenario_type,
        "duration_sec": duration,
        "selected_attempt": {
            "provider": provider,
            "index": attempt,
            "selected": True,
            **declarations,
        },
    }
    manifest = {
        "schema": staging.VERIFIED_MANIFEST_SCHEMA,
        "dataset_revision": dataset_revision,
        "records": [record],
    }
    manifest_bytes = staging.canonical_json_bytes(manifest)
    # Reuse the downstream adapter's exact validators before producing a plan.
    staging.load_verified_records(manifest_bytes, "manifest")
    staging.validate_render_script(render_bytes, record)
    staging.validate_dialogue(dialogue_bytes, record)

    entries = [
        file_entry(OUTPUT_MANIFEST, manifest_bytes),
        file_entry(OUTPUT_RENDER, render_bytes),
        file_entry(OUTPUT_DIALOGUE, dialogue_bytes),
        {"path": OUTPUT_MEDIA, "sha256": evidence["media"].sha256, "bytes": evidence["media"].bytes},
    ]
    projection_digest = tree_digest(entries)
    source_report = []
    field_policy = {
        "no_oracle_jsonl": {
            "validated_fields": sorted(NO_ORACLE_FIELDS),
            "emitted_fields": sorted(NO_ORACLE_FIELDS - set(NO_ORACLE_OMITTED_FIELDS)),
            "gating_fields": [
                "row_id",
                "dataset_source",
                "visual_id",
                "pairing_id",
                "case_scope",
                "scenario_type",
                "video_model_key",
                "video_attempt_index",
                "video_path",
            ],
            "omitted_fields": NO_ORACLE_OMITTED_FIELDS,
        },
        "attempt_ledger": {
            "validated_fields": sorted(
                ["schema_version", "artifact_root", "attempts"]
                + [f"attempts.{field}" for field in LEDGER_ATTEMPT_FIELDS]
            ),
            "emitted_fields": ["attempts.attempt_index", "attempts.selected_for_export"],
            "gating_fields": [
                "artifact_root",
                "attempts.attempt_index",
                "attempts.media_summary_path",
                "attempts.selected_for_export",
                "attempts.status",
                "attempts.video_path",
            ],
            "omitted_fields": sorted(
                f"attempts.{field}"
                for field in LEDGER_ATTEMPT_FIELDS
                if field not in {"attempt_index", "selected_for_export"}
            )
            + ["artifact_root"],
        },
        "raw_render_script": {
            "validated_fields": sorted(
                [f"Global_Metadata.{field}" for field in RENDER_METADATA_FIELDS]
                + [f"Global_Metadata.Viewpoint_Contract.{field}" for field in RENDER_VIEWPOINT_FIELDS]
                + [f"Camera_Continuity.{field}" for field in RENDER_CAMERA_FIELDS]
                + [f"Scene.{field}" for field in RENDER_SCENE_FIELDS]
                + [f"Exit_State.{field}" for field in RENDER_EXIT_FIELDS]
                + [f"Intervention_Cues.{field}" for field in RENDER_INTERVENTION_FIELDS]
            ),
            "emitted_fields": [
                "Global_Metadata.Title",
                "Global_Metadata.Perspective",
                "Global_Metadata.Environment",
                "Global_Metadata.Lighting",
                "Global_Metadata.Duration_sec",
                "Camera_Continuity.Position",
                "Camera_Continuity.Height",
                "Camera_Continuity.Angle",
                "Camera_Continuity.Motion",
                "Camera_Continuity.Framing",
                "Scene.Key_Visual_Elements",
                "Scene.Description",
                "Scene.Actions",
                "Exit_State.Action",
            ],
            "gating_fields": [
                "Global_Metadata.Duration_sec",
                "Global_Metadata.Perspective",
                "Scene.Actions",
                "Scene.Dialogue",
            ],
            "omitted_fields": [
                "Global_Metadata.Emotional_Tone",
                "Global_Metadata.Dialogue_Delivery",
                "Global_Metadata.Speech_Policy",
                "Global_Metadata.Audio_Policy",
                "Global_Metadata.Viewpoint_Contract",
                "Camera_Continuity.Head_Motion_Profile",
                "Camera_Continuity.Forbidden_Camera_Behaviors",
                "Scene.Emotion",
                "Scene.Dialogue",
                "Exit_State.Audio",
                "Intervention_Cues",
            ],
        },
        "media_summary": {
            "validated_fields": sorted(SUMMARY_FIELDS),
            "emitted_fields": ["target_size", "seconds"],
            "gating_fields": [
                "artifact_root",
                "attempt_index",
                "case_id",
                "model_key",
                "render_script_path",
                "seconds",
                "status",
                "target_size",
                "video_backend",
                "video_path",
            ],
            "omitted_fields": sorted(SUMMARY_FIELDS - {"target_size", "seconds"}),
        },
        "media": {
            "validated_fields": ["container.ftyp", "container.moov.mvhd.duration_sec", "container.moov.trak.tkhd.width", "container.moov.trak.tkhd.height"],
            "emitted_fields": ["container.moov.mvhd.duration_sec", "container.moov.trak.tkhd.width", "container.moov.trak.tkhd.height"],
            "gating_fields": ["container.ftyp", "container.moov.mvhd.duration_sec", "container.moov.trak.tkhd.width", "container.moov.trak.tkhd.height"],
            "omitted_fields": [],
        },
    }
    for role in selected_paths:
        source_report.append(
            {
                "role": role,
                "path": selected_paths[role],
                "sha256": evidence[role].sha256,
                "bytes": evidence[role].bytes,
                **field_policy[role],
            }
        )
    report = {
        "schema": REPORT_SCHEMA,
        "valid": True,
        "policy": {
            "network": "forbidden",
            "dataset_directory_scan": "forbidden",
            "symlink_follow": "forbidden",
            "raw_values_in_report": "forbidden",
            "default_mode": "dry_run",
            "owner_approval_required_for_apply": True,
        },
        "owner_approval_reference_sha256": approval_hash,
        "source_evidence": source_report,
        "validation": [
            {"name": name, "status": "passed"}
            for name in (
                "raw_schema_allowlist",
                "exact_identity_join",
                "sole_selected_attempt",
                "declared_path_join",
                "sanitized_render_allowlist",
                "evaluation_safe_dialogue",
                "mp4_binary_metadata",
                "downstream_staging_contract",
            )
        ],
        "projection": {
            "digest_algorithm": TREE_DIGEST_ALGORITHM,
            "digest_sha256": projection_digest,
            "entries": sorted(entries, key=lambda item: item["path"]),
        },
    }
    report_bytes = staging.canonical_json_bytes(report)
    small_files = {
        OUTPUT_MANIFEST: manifest_bytes,
        OUTPUT_RENDER: render_bytes,
        OUTPUT_DIALOGUE: dialogue_bytes,
        OUTPUT_REPORT: report_bytes,
    }
    return ProjectionPlan(
        dataset=dataset,
        output_dir=output_dir,
        small_files=small_files,
        media_source=evidence["media"],
        source_evidence=evidence,
        source_limits=limits,
        projection_entries=entries,
        projection_digest_sha256=projection_digest,
        report=report,
        approval_reference_sha256=approval_hash,
    )


EXPECTED_FILES = frozenset({OUTPUT_MANIFEST, OUTPUT_RENDER, OUTPUT_DIALOGUE, OUTPUT_REPORT, OUTPUT_MEDIA})
EXPECTED_DIRS = frozenset({"media"})


def _expected_output(plan: ProjectionPlan) -> dict[str, tuple[str, int]]:
    result = {path: (staging.sha256_bytes(value), len(value)) for path, value in plan.small_files.items()}
    result[OUTPUT_MEDIA] = (plan.media_source.sha256, plan.media_source.bytes)
    return result


def _verify_existing_output(plan: ProjectionPlan) -> bool:
    try:
        root_stat = os.lstat(plan.output_dir)
    except FileNotFoundError:
        return False
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_IMODE(root_stat.st_mode) != 0o700
        or root_stat.st_uid != os.geteuid()
    ):
        fail("VISTA_PROJECTION_OUTPUT_CONFLICT", "Existing output is not a private owned directory", pointer="--output-dir")
    expected = _expected_output(plan)
    observed_files: set[str] = set()
    observed_dirs: set[str] = set()
    for current, directories, files in os.walk(plan.output_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_root = current_path.relative_to(plan.output_dir)
        for directory in directories:
            path = current_path / directory
            relative = (relative_root / directory).as_posix()
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
                fail("VISTA_PROJECTION_OUTPUT_CONFLICT", "Existing output contains an unsafe directory", pointer=relative)
            observed_dirs.add(relative)
        for filename in files:
            path = current_path / filename
            relative = (relative_root / filename).as_posix()
            metadata = os.lstat(path)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
            ):
                fail("VISTA_PROJECTION_OUTPUT_CONFLICT", "Existing output contains an unsafe file", pointer=relative)
            observed_files.add(relative)
    if observed_dirs != EXPECTED_DIRS or observed_files != EXPECTED_FILES:
        fail("VISTA_PROJECTION_OUTPUT_CONFLICT", "Existing output file set differs from the plan", pointer="--output-dir")
    for relative, (expected_digest, expected_bytes) in expected.items():
        path = plan.output_dir.joinpath(*PurePosixPath(relative).parts)
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                total += len(chunk)
        if total != expected_bytes or digest.hexdigest() != expected_digest:
            fail("VISTA_PROJECTION_OUTPUT_CONFLICT", "Existing output bytes differ from the plan", pointer=relative)
    return True


def _verify_sources_unchanged(plan: ProjectionPlan) -> None:
    for role, original in plan.source_evidence.items():
        current = plan.dataset.hash(original.relative_path, role, plan.source_limits[role])
        if current.sha256 != original.sha256 or current.bytes != original.bytes:
            fail("VISTA_PROJECTION_SOURCE_CHANGED", "A selected source changed before apply", pointer=role)


def _copy_media(plan: ProjectionPlan, destination: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    output_fd = os.open(destination, flags, 0o600)
    digest = hashlib.sha256()
    total = 0
    try:
        with plan.dataset.open_regular(plan.media_source.relative_path, "--media", MAX_MEDIA_BYTES) as (source, metadata):
            if (
                metadata.st_dev != plan.media_source.device
                or metadata.st_ino != plan.media_source.inode
                or metadata.st_size != plan.media_source.bytes
                or metadata.st_mtime_ns != plan.media_source.mtime_ns
            ):
                fail("VISTA_PROJECTION_SOURCE_CHANGED", "Selected media changed before apply", pointer="--media")
            with os.fdopen(output_fd, "wb", closefd=False) as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                target.flush()
                os.fsync(target.fileno())
    finally:
        os.close(output_fd)
    os.chmod(destination, 0o600, follow_symlinks=False)
    if total != plan.media_source.bytes or digest.hexdigest() != plan.media_source.sha256:
        fail("VISTA_PROJECTION_SOURCE_CHANGED", "Selected media changed during apply", pointer="--media")


def apply_projection(plan: ProjectionPlan) -> str:
    if plan.approval_reference_sha256 is None:
        fail(
            "VISTA_PROJECTION_OWNER_APPROVAL_REQUIRED",
            "Atomic apply requires a dataset-owner approval reference",
            pointer="--owner-approval-ref",
        )
    parent = plan.output_dir.parent
    staging._validate_private_parent(parent)
    _verify_sources_unchanged(plan)
    if _verify_existing_output(plan):
        return "idempotent"
    lock = parent / f".{plan.output_dir.name}.projection.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock, flags, 0o600)
    except FileExistsError:
        fail("VISTA_PROJECTION_OUTPUT_BUSY", "Another exporter owns the output lock", pointer="--output-dir")
    os.close(lock_fd)
    temp_root: Path | None = None
    try:
        if _verify_existing_output(plan):
            return "idempotent"
        temp_root = Path(tempfile.mkdtemp(prefix=f".{plan.output_dir.name}.tmp-", dir=parent))
        os.chmod(temp_root, 0o700)
        media_dir = temp_root / "media"
        media_dir.mkdir(mode=0o700)
        for relative, value in plan.small_files.items():
            staging._write_private(temp_root.joinpath(*PurePosixPath(relative).parts), value)
        _copy_media(plan, media_dir / "reference.mp4")
        for directory in (media_dir, temp_root):
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if plan.output_dir.exists() or plan.output_dir.is_symlink():
            fail("VISTA_PROJECTION_OUTPUT_CONFLICT", "Output appeared during atomic apply", pointer="--output-dir")
        os.rename(temp_root, plan.output_dir)
        temp_root = None
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return "created"
    finally:
        if temp_root is not None and temp_root.exists():
            shutil.rmtree(temp_root)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


class MachineReadableArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        fail("VISTA_PROJECTION_ARGUMENT_INVALID", "CLI arguments are missing or invalid; consult --help")


def build_parser() -> argparse.ArgumentParser:
    parser = MachineReadableArgumentParser(
        description="Offline export of one exact raw VISTA selection into a safe verified projection."
    )
    parser.add_argument("--dataset-root", required=True, help="Explicit authoritative source root; symlinks forbidden")
    parser.add_argument("--no-oracle-jsonl", required=True, help="Exact relative no-oracle JSONL path")
    parser.add_argument("--attempt-ledger", required=True, help="Exact relative selected-attempt ledger path")
    parser.add_argument("--raw-render-script", required=True, help="Exact relative raw render YAML path")
    parser.add_argument("--media-summary", required=True, help="Exact relative attempt-specific media summary path")
    parser.add_argument("--media", required=True, help="Exact relative selected MP4 path")
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--row-id", required=True)
    parser.add_argument("--visual-id", required=True)
    parser.add_argument("--case-scope", required=True)
    parser.add_argument("--scenario-type", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--output-dir", required=True, help="Absolute private output outside the source root")
    parser.add_argument("--owner-approval-ref", help="Immutable owner ticket/decision id; hashed in output, required for --apply")
    parser.add_argument("--apply", action="store_true", help="Atomically create the private projection")
    return parser


def result_for_plan(plan: ProjectionPlan, status: str) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "valid": True,
        "status": status,
        "projection_digest_sha256": plan.projection_digest_sha256,
        "owner_approval_reference_sha256": plan.approval_reference_sha256,
        "provenance_report": plan.report,
    }


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        args = build_parser().parse_args(argv)
        plan = build_projection_plan(args)
        status = apply_projection(plan) if args.apply else "dry_run"
        print(staging.compact_json(result_for_plan(plan, status)))
        return 0
    except (VistaProjectionError, staging.VistaStagingError) as error:
        print(
            staging.compact_json(
                {"schema": RESULT_SCHEMA, "valid": False, "status": "failed", "error": error.public_dict()}
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            staging.compact_json(
                {
                    "schema": RESULT_SCHEMA,
                    "valid": False,
                    "status": "failed",
                    "error": {
                        "code": "VISTA_PROJECTION_INTERNAL_ERROR",
                        "message": "Projection failed before a safe result could be produced",
                    },
                }
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
