from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


TOOLS_DIR = Path(__file__).resolve().parents[1]

PHASES = ("inspect", "render", "caption", "embed", "postgres", "qdrant", "reconcile")
ARTIFACT_FILES = {
    phase: f"semantic_index_{phase}_artifact_v1_schema.json" for phase in PHASES
}
SCHEMA_FILES = {
    "state": "semantic_asset_index_execution_state_v2_schema.json",
    "terminal": "semantic_asset_index_terminal_receipt_v2_schema.json",
    "adapter": "semantic_index_production_adapter_v2_schema.json",
    **ARTIFACT_FILES,
}

H = "a" * 64
REV = f"sha256:{H}"
GEN = f"semantic-generation:{H}"
IMAGE = f"registry.example/simworld/component@sha256:{H}"
NOW = "2026-07-21T12:00:00Z"


def _load(name: str) -> dict[str, Any]:
    return json.loads((TOOLS_DIR / SCHEMA_FILES[name]).read_text(encoding="utf-8"))


SCHEMAS = {name: _load(name) for name in SCHEMA_FILES}


def _merge(left: Any, right: Any) -> Any:
    if isinstance(left, dict) and isinstance(right, dict):
        merged = copy.deepcopy(left)
        for key, value in right.items():
            merged[key] = _merge(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    return copy.deepcopy(right)


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not ref.startswith("#/$defs/"):
        raise AssertionError(f"test fixture generator only accepts local refs: {ref}")
    return root["$defs"][ref.removeprefix("#/$defs/")]


def _string_value(key: str | None, schema: dict[str, Any]) -> str:
    pattern = schema.get("pattern", "")
    if "semantic-index-run:" in pattern or key == "run_id":
        return "semantic-index-run:test"
    if "semantic-index-correlation:" in pattern or key == "correlation_id":
        return "semantic-index-correlation:test"
    if "semantic-index-state:" in pattern or key == "state_id":
        return "semantic-index-state:test"
    if "semantic-index-terminal:" in pattern or key == "receipt_id":
        return "semantic-index-terminal:test"
    if "semantic-generation:" in pattern or key in {
        "generation_id",
        "previous_snapshot_generation_id",
        "observed_generation_id",
    }:
        return GEN
    if "asset-snapshot-" in pattern or key in {"snapshot_revision", "target_snapshot_revision"}:
        return "asset-snapshot-20260721-test"
    if "@sha256:" in pattern or key in {"runtime_image", "worker_image"}:
        return IMAGE
    if pattern.startswith("^/Game") or key in {
        "ue_path",
        "class_path",
        "material_path",
        "base_color_texture",
        "normal_texture",
        "roughness_texture",
        "metallic_texture",
        "ambient_occlusion_texture",
    }:
        return "/Game/Test/SM_Test.SM_Test"
    if pattern.startswith("^sha256:") or key in {
        "job_revision",
        "implementation_revision",
        "operation_revision",
        "model_revision",
        "content_revision",
        "engine_revision",
        "level_revision",
        "idempotency_key",
    }:
        return REV
    if pattern == "^[a-f0-9]{64}$" or (key and key.endswith("_sha256")):
        return H
    if pattern.startswith("^sha256:[a-f0-9]") or key == "content_address":
        return REV
    if pattern == "^[A-Z]{3}$" or key == "currency":
        return "USD"
    if "[0-9]{4}-" in pattern or key in {
        "issued_at",
        "expires_at",
        "validated_at",
        "observed_at",
        "reconcile_completed_at",
        "committed_at",
    }:
        return NOW
    if key == "point_id":
        return H
    if key == "asset_id":
        return "sm_test"
    if key == "ue_name":
        return "SM_Test"
    if key in {"database", "schema_name", "collection"}:
        return "semantic_gen_test"
    if key in {"label_en", "label_zh_tw", "description_en", "description_zh_tw"}:
        return "test"
    minimum = max(1, int(schema.get("minLength", 1)))
    return "x" * minimum


def _sample(schema: dict[str, Any], root: dict[str, Any], key: str | None = None) -> Any:
    if "$ref" in schema:
        return _sample(_resolve(schema, root), root, key)
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    if "enum" in schema:
        return copy.deepcopy(schema["enum"][0])
    if "allOf" in schema and "type" not in schema and "required" not in schema:
        value: Any = {}
        for part in schema["allOf"]:
            if "properties" in part and "required" not in part and "type" not in part:
                item = {
                    name: _sample(child, root, name)
                    for name, child in part["properties"].items()
                }
            else:
                item = _sample(part, root, key)
            value = _merge(value, item)
        return value
    if "oneOf" in schema and not schema.get("type"):
        return _sample(schema["oneOf"][0], root, key)
    schema_type = schema.get("type")
    if schema_type == "object" or "required" in schema:
        required = schema.get("required", [])
        return {
            name: _sample(schema["properties"][name], root, name)
            for name in required
        }
    if schema_type == "array":
        minimum = int(schema.get("minItems", 0))
        prefix = schema.get("prefixItems", [])
        values = [_sample(item, root, key) for item in prefix]
        while len(values) < minimum:
            values.append(_sample(schema.get("items", {}), root, key))
        return values[: max(minimum, len(prefix))]
    if schema_type == "string":
        return _string_value(key, schema)
    if schema_type == "integer":
        return int(schema.get("minimum", 0))
    if schema_type == "boolean":
        return False
    if schema_type == "null":
        return None
    raise AssertionError(f"cannot synthesize {key}: {schema}")


def artifact_fixture(phase: str) -> dict[str, Any]:
    schema = SCHEMAS[phase]
    value = _sample(schema, schema)
    if phase == "reconcile":
        value["evidence"]["query_results"][0]["query_id"] = "query:zh"
        value["evidence"]["query_results"][0]["language"] = "zh-TW"
        value["evidence"]["query_results"][1]["query_id"] = "query:en"
        value["evidence"]["query_results"][1]["language"] = "en"
    return value


def state_fixture() -> dict[str, Any]:
    schema = SCHEMAS["state"]
    value = _sample(schema, schema)
    value["state_sequence"] = 0
    value["prior_state_sha256"] = None
    value["phase_transitions"] = []
    value["progress"] = {
        "completed_phase_count": 0,
        "last_completed_phase": None,
        "next_phase": "inspect",
    }
    value["prior_receipt_chain"] = []
    value["recovery_control_receipts"] = []
    value["mutation_tracking"].update(
        {
            "current_state": "none",
            "last_phase": None,
            "last_request_sha256": None,
            "ordinary_retry_permitted": True,
            "ambiguous_requires_operator": False,
        }
    )
    value["quarantine"].update(
        {"state": "not_requested", "control_request_sha256": None, "control_receipt_sha256": None}
    )
    value["terminal"] = {
        "lifecycle": "running",
        "verified": False,
        "terminal_receipt_sha256": None,
    }
    return value


def terminal_success_fixture() -> dict[str, Any]:
    schema = SCHEMAS["terminal"]
    value = _sample(schema, schema)
    evidence_defs = [f"{phase}Evidence" for phase in PHASES]
    artifact_defs = [f"{phase}Artifact" if phase != "qdrant" else "qdrantArtifact" for phase in PHASES]
    value["phase_evidence_chain"] = [
        _sample({"$ref": f"#/$defs/{name}"}, schema) for name in evidence_defs
    ]
    value["artifact_chain"] = [
        _sample({"$ref": f"#/$defs/{name}"}, schema) for name in artifact_defs
    ]
    value["status"] = "snapshot_built_verified"
    value["verified"] = True
    value["success"] = _sample({"$ref": "#/$defs/success"}, schema)
    value["failure"] = None
    value["recovery"] = None
    return value


def terminal_recovery_fixture() -> dict[str, Any]:
    schema = SCHEMAS["terminal"]
    value = terminal_success_fixture()
    value["status"] = "recovery_required"
    value["verified"] = False
    value["phase_evidence_chain"] = value["phase_evidence_chain"][:4]
    value["artifact_chain"] = value["artifact_chain"][:4]
    value["success"] = None
    value["failure"] = None
    value["recovery"] = _sample({"$ref": "#/$defs/recovery"}, schema)
    return value


def terminal_failure_fixture() -> dict[str, Any]:
    schema = SCHEMAS["terminal"]
    value = terminal_success_fixture()
    value["status"] = "failed_quarantined"
    value["verified"] = False
    value["phase_evidence_chain"] = value["phase_evidence_chain"][:2]
    value["artifact_chain"] = value["artifact_chain"][:2]
    value["success"] = None
    value["failure"] = _sample({"$ref": "#/$defs/failure"}, schema)
    value["recovery"] = None
    return value


def adapter_fixture() -> dict[str, Any]:
    schema = SCHEMAS["adapter"]
    return _sample(schema, schema)


class SemanticStateTerminalArtifactSchemaTests(unittest.TestCase):
    def assert_valid(self, name: str, instance: dict[str, Any]) -> None:
        errors = sorted(Draft202012Validator(SCHEMAS[name]).iter_errors(instance), key=lambda e: list(e.path))
        self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def assert_invalid(self, name: str, instance: dict[str, Any]) -> None:
        self.assertTrue(list(Draft202012Validator(SCHEMAS[name]).iter_errors(instance)))

    def test_all_ten_schemas_meta_validate(self) -> None:
        self.assertEqual(10, len(SCHEMAS))
        for schema in SCHEMAS.values():
            Draft202012Validator.check_schema(schema)

    def test_all_positive_artifact_instances_validate(self) -> None:
        for phase in PHASES:
            with self.subTest(phase=phase):
                self.assert_valid(phase, artifact_fixture(phase))

    def test_state_terminal_and_adapter_positive_instances_validate(self) -> None:
        self.assert_valid("state", state_fixture())
        self.assert_valid("terminal", terminal_success_fixture())
        self.assert_valid("terminal", terminal_recovery_fixture())
        self.assert_valid("terminal", terminal_failure_fixture())
        self.assert_valid("adapter", adapter_fixture())

    def test_worker_root_artifact_schema_enum_matches_exactly(self) -> None:
        worker_root = json.loads(
            (TOOLS_DIR / "semantic_index_worker_artifact_root_schema.json").read_text(encoding="utf-8")
        )
        worker_values = set(worker_root["properties"]["artifact_root"]["properties"]["schema"]["enum"])
        local_values = {SCHEMAS[phase]["properties"]["schema"]["const"] for phase in PHASES}
        self.assertEqual(worker_values, local_values)

    def test_schema_ids_are_unique_and_follow_root_const(self) -> None:
        ids = [schema["$id"] for schema in SCHEMAS.values()]
        self.assertEqual(len(ids), len(set(ids)))
        for phase in PHASES:
            self.assertEqual(
                f"simworld-semantic-index-{phase}-artifact/v1",
                SCHEMAS[phase]["properties"]["schema"]["const"],
            )

    def test_artifacts_reject_wrong_phase_operation_root_and_semantic_claim(self) -> None:
        for phase in PHASES:
            with self.subTest(phase=phase):
                fixture = artifact_fixture(phase)
                fixture["schema"] = "simworld-semantic-index-inspect-artifact/v1" if phase != "inspect" else "bad"
                self.assert_invalid(phase, fixture)

                fixture = artifact_fixture(phase)
                fixture["phase"] = "render" if phase != "render" else "inspect"
                self.assert_invalid(phase, fixture)

                fixture = artifact_fixture(phase)
                fixture["semantic_validation"]["counts_and_digests_recomputed"] = False
                self.assert_invalid(phase, fixture)

    def test_artifact_chunk_descriptor_is_closed_and_bounded(self) -> None:
        fixture = artifact_fixture("embed")
        fixture["chunks"][0]["free_form_path"] = "/tmp/secret"
        self.assert_invalid("embed", fixture)

        fixture = artifact_fixture("embed")
        fixture["chunks"][0]["canonical_byte_count"] = 16777217
        self.assert_invalid("embed", fixture)

    def test_state_enforces_exact_phase_prefix_and_verified_terminal_requires_all_seven(self) -> None:
        schema = SCHEMAS["state"]
        fixture = state_fixture()
        fixture["phase_transitions"] = [_sample({"$ref": "#/$defs/inspectTransition"}, schema)]
        fixture["prior_receipt_chain"] = [
            _sample({"$ref": "#/$defs/inspectReceiptChainEntry"}, schema)
        ]
        fixture["progress"] = {
            "completed_phase_count": 1,
            "last_completed_phase": "inspect",
            "next_phase": "render",
        }
        self.assert_valid("state", fixture)

        wrong = copy.deepcopy(fixture)
        wrong["phase_transitions"][0]["phase"] = "render"
        self.assert_invalid("state", wrong)

        premature = state_fixture()
        premature["terminal"] = {
            "lifecycle": "snapshot_built_verified",
            "verified": True,
            "terminal_receipt_sha256": H,
        }
        self.assert_invalid("state", premature)

    def test_state_control_receipts_enforce_closed_mutation_matrix(self) -> None:
        schema = SCHEMAS["state"]
        control = _sample({"$ref": "#/$defs/controlReceipt"}, schema)
        control["prior_control_receipt_sha256"] = None

        fixture = state_fixture()
        fixture["recovery_control_receipts"] = [copy.deepcopy(control)]
        self.assert_valid("state", fixture)

        wrong_read_only = copy.deepcopy(fixture)
        wrong_read_only["recovery_control_receipts"][0]["mutation_state"] = "committed"
        self.assert_invalid("state", wrong_read_only)

        wrong_quarantine = copy.deepcopy(fixture)
        wrong_quarantine["recovery_control_receipts"][0]["operation"] = "quarantine_generation"
        wrong_quarantine["recovery_control_receipts"][0]["mutation_state"] = "none"
        self.assert_invalid("state", wrong_quarantine)

    def test_terminal_success_is_fail_closed(self) -> None:
        fixture = terminal_success_fixture()
        fixture["phase_evidence_chain"].pop()
        self.assert_invalid("terminal", fixture)

        fixture = terminal_success_fixture()
        fixture["launcher_verification"]["verified_before_executor_start"] = False
        self.assert_invalid("terminal", fixture)

        fixture = terminal_success_fixture()
        fixture["activation"]["performed"] = True
        self.assert_invalid("terminal", fixture)

        fixture = terminal_recovery_fixture()
        fixture["verified"] = True
        self.assert_invalid("terminal", fixture)

        fixture = terminal_failure_fixture()
        fixture["failure"]["verified_snapshot_produced"] = True
        self.assert_invalid("terminal", fixture)

        for field in ("ledger_identity", "ledger_revision"):
            fixture = terminal_success_fixture()
            fixture["phase_evidence_chain"][0].pop(field)
            with self.subTest(missing_phase_evidence_projection=field):
                self.assert_invalid("terminal", fixture)

    def test_adapter_remains_unregistered_and_has_no_ambient_or_lifecycle_escape(self) -> None:
        fixture = adapter_fixture()
        fixture["production_capable"] = True
        self.assert_invalid("adapter", fixture)

        fixture = adapter_fixture()
        fixture["ambient_discovery"]["environment"] = True
        self.assert_invalid("adapter", fixture)

        fixture = adapter_fixture()
        fixture["commands"]["subprocess"] = True
        self.assert_invalid("adapter", fixture)

        fixture = adapter_fixture()
        fixture["lifecycle"]["opens_public_listener"] = True
        self.assert_invalid("adapter", fixture)

        fixture = adapter_fixture()
        fixture["credential_transport"]["undeclared_transform_coverage_claimed"] = True
        self.assert_invalid("adapter", fixture)

        fixture = adapter_fixture()
        fixture["credential_transport"]["declared_secret_scan_policy"] = (
            "all-secret-encodings"
        )
        self.assert_invalid("adapter", fixture)

    def test_every_top_level_and_selected_nested_object_is_closed(self) -> None:
        fixtures = {
            **{phase: artifact_fixture(phase) for phase in PHASES},
            "state": state_fixture(),
            "terminal": terminal_success_fixture(),
            "adapter": adapter_fixture(),
        }
        for name, fixture in fixtures.items():
            with self.subTest(name=name, level="top"):
                fixture["unexpected"] = True
                self.assert_invalid(name, fixture)

        fixture = artifact_fixture("inspect")
        fixture["evidence"]["records"][0]["unexpected"] = True
        self.assert_invalid("inspect", fixture)

        fixture = terminal_success_fixture()
        fixture["success"]["unexpected"] = True
        self.assert_invalid("terminal", fixture)

        fixture = adapter_fixture()
        fixture["launcher_handoff"]["socket_path"] = "/tmp/worker.sock"
        self.assert_invalid("adapter", fixture)

    def test_large_arrays_do_not_use_quadratic_unique_items(self) -> None:
        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if value.get("maxItems", 0) > 64:
                    self.assertNotIn("uniqueItems", value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for schema in SCHEMAS.values():
            walk(schema)


if __name__ == "__main__":
    unittest.main(verbosity=2)
