from __future__ import annotations

import copy
import importlib
import json
import pathlib
import sys
import tempfile
import types
import unittest
from typing import Any


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import semantic_index_schema_runtime as runtime  # noqa: E402

try:  # The differential check is optional under /usr/bin/python3 -I -S.
    from jsonschema import Draft202012Validator as ThirdPartyValidator  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised by isolated-system validation
    ThirdPartyValidator = None


SCHEMA_FILES = tuple(sorted(TOOLS_DIR.glob("*semantic*schema.json")))


def load_schemas() -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(path.read_text(encoding="utf-8")) for path in SCHEMA_FILES)


def schema_registry(schemas: tuple[dict[str, Any], ...]) -> dict[str, dict[str, Any]]:
    return {schema["$id"]: schema for schema in schemas}


def schema_for_instance(
    schemas: tuple[dict[str, Any], ...], instance: dict[str, Any]
) -> dict[str, Any]:
    matches = [
        schema
        for schema in schemas
        if schema.get("properties", {}).get("schema", {}).get("const") == instance.get("schema")
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one schema for {instance.get('schema')!r}, got {len(matches)}")
    return matches[0]


def closed_schema(**keywords: Any) -> dict[str, Any]:
    value = {
        "$schema": runtime.DRAFT_2020_12,
        "$id": "urn:simworld:test:closed-schema",
    }
    value.update(keywords)
    return value


def _load_fixture_module(module_name: str):
    """Load fixture constructors without requiring jsonschema in -I -S mode."""

    if module_name in sys.modules:
        return sys.modules[module_name]
    installed_stubs: list[str] = []
    if ThirdPartyValidator is None:
        jsonschema_stub = types.ModuleType("jsonschema")
        jsonschema_stub.Draft202012Validator = type("UnavailableValidator", (), {})
        referencing_stub = types.ModuleType("referencing")
        referencing_stub.Registry = type("UnavailableRegistry", (), {})
        referencing_stub.Resource = type("UnavailableResource", (), {})
        sys.modules["jsonschema"] = jsonschema_stub
        sys.modules["referencing"] = referencing_stub
        installed_stubs.extend(("jsonschema", "referencing"))
    try:
        return importlib.import_module(module_name)
    finally:
        for name in installed_stubs:
            sys.modules.pop(name, None)


class SemanticIndexSchemaRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = load_schemas()
        cls.registry = schema_registry(cls.schemas)
        cls.fixtures = _load_fixture_module("test_semantic_index_production_schemas")
        cls.state_fixtures = _load_fixture_module(
            "test_semantic_index_state_terminal_artifact_schemas"
        )
        cls.launcher_verification_fixtures = _load_fixture_module(
            "test_semantic_index_launcher_verification"
        )

    def assert_runtime_invalid(
        self,
        schema: dict[str, Any],
        instance: Any,
        *,
        code: str | None = None,
        registry: dict[str, dict[str, Any]] | None = None,
    ) -> runtime.SchemaRuntimeError:
        with self.assertRaises(runtime.SchemaRuntimeError) as caught:
            runtime.validate_instance(schema, instance, registry=registry)
        if code is not None:
            self.assertEqual(code, caught.exception.code)
        return caught.exception

    def validate_corpus_instance(self, instance: dict[str, Any]) -> None:
        runtime.validate_instance(
            schema_for_instance(self.schemas, instance),
            instance,
            registry=self.registry,
        )

    def test_all_twenty_four_pinned_semantic_schemas_use_supported_closed_subset(self) -> None:
        self.assertEqual(24, len(self.schemas))
        for schema in self.schemas:
            with self.subTest(schema=schema["$id"]):
                runtime.validate_schema(schema, registry=self.registry)

    def test_valid_documents_cover_all_twenty_four_schemas(self) -> None:
        fixture = self.fixtures
        values: list[dict[str, Any]] = [fixture.valid_job(), fixture.valid_plan(), fixture.valid_basis()]
        values.extend(
            fixture.valid_approval(kind)
            for kind in (
                "data_owner_review",
                "cost_owner",
                "admin_state_change",
                "deployment",
                "runtime",
                "rollback_readiness",
            )
        )
        values.append(fixture.valid_existing_generation_rollback_approval())
        for phase in fixture.PHASE_FIXTURES:
            request, _capabilities, _policy = fixture.worker_protocol_fixtures.make_request(
                operation=fixture.PHASE_CONTRACTS[phase]["operation"]
            )
            values.extend(
                (
                    request,
                    fixture.worker_protocol_fixtures.make_success_response(request),
                    fixture.valid_worker_root(phase),
                    fixture.PHASE_FIXTURES[phase](),
                )
            )
        for operation in fixture.CONTROL_CONTRACTS:
            values.extend(
                (
                    fixture.valid_control_request(operation),
                    fixture.valid_control_result(operation),
                )
            )

        state_fixture = self.state_fixtures
        values.extend(state_fixture.artifact_fixture(phase) for phase in state_fixture.PHASES)
        values.extend(
            (
                state_fixture.state_fixture(),
                state_fixture.terminal_success_fixture(),
                state_fixture.terminal_recovery_fixture(),
                state_fixture.terminal_failure_fixture(),
                state_fixture.adapter_fixture(),
            )
        )
        values.append(self.launcher_verification_fixtures.fake_receipt()[0])

        import test_prepare_semantic_asset_index_job as legacy_preparation
        import test_execute_semantic_asset_index_job as legacy_execution

        with tempfile.TemporaryDirectory() as temporary:
            legacy_fixture = legacy_preparation.Fixture(pathlib.Path(temporary))
            values.extend((legacy_preparation.recipe(), legacy_fixture.prepare().job))

        legacy_case = legacy_execution.SemanticIndexExecutorTests(
            methodName="test_execution_plan_schema_matches_closed_plan"
        )
        legacy_case.setUp()
        try:
            values.append(legacy_case.make_plan())
        finally:
            legacy_case.tearDown()

        covered: set[str] = set()
        for value in values:
            with self.subTest(document_schema=value["schema"]):
                schema = schema_for_instance(self.schemas, value)
                runtime.validate_instance(schema, value, registry=self.registry)
                covered.add(schema["$id"])
        self.assertEqual(set(self.registry), covered)

    def test_closed_object_rejects_unknown_missing_and_wrong_type(self) -> None:
        value = self.fixtures.valid_job()
        schema = schema_for_instance(self.schemas, value)
        unknown = copy.deepcopy(value)
        unknown["credential"] = "not allowed"
        self.assert_runtime_invalid(
            schema,
            unknown,
            code="SCHEMA_RUNTIME_ADDITIONAL_PROPERTY",
            registry=self.registry,
        )
        missing = copy.deepcopy(value)
        del missing["source"]
        self.assert_runtime_invalid(
            schema,
            missing,
            code="SCHEMA_RUNTIME_REQUIRED_MISSING",
            registry=self.registry,
        )
        wrong_type = copy.deepcopy(value)
        wrong_type["resource_contract"]["limits"]["assets"] = True
        self.assert_runtime_invalid(
            schema,
            wrong_type,
            code="SCHEMA_RUNTIME_TYPE_MISMATCH",
            registry=self.registry,
        )

    def test_one_of_contains_prefix_and_unique_semantics(self) -> None:
        schema = closed_schema(
            type="array",
            minItems=2,
            maxItems=4,
            prefixItems=[{"const": "header"}],
            items={"type": "integer", "minimum": 1},
            contains={"const": 7},
            minContains=1,
            uniqueItems=True,
            oneOf=[{"maxItems": 3}, {"minItems": 4}],
        )
        runtime.validate_instance(schema, ["header", 7, 8])
        self.assert_runtime_invalid(schema, ["header", 2, 3], code="SCHEMA_RUNTIME_CONTAINS_MISMATCH")
        self.assert_runtime_invalid(schema, ["header", 7, 7], code="SCHEMA_RUNTIME_UNIQUE_MISMATCH")
        self.assert_runtime_invalid(schema, ["header", True], code="SCHEMA_RUNTIME_TYPE_MISMATCH")

        ambiguous = closed_schema(oneOf=[{"type": "integer"}, {"minimum": 0}])
        self.assert_runtime_invalid(ambiguous, 1, code="SCHEMA_RUNTIME_ONE_OF_MISMATCH")

    def test_pattern_uses_json_schema_search_semantics_and_rejects_mismatch(self) -> None:
        schema = closed_schema(type="string", minLength=3, maxLength=16, pattern="[a-z]{3}")
        runtime.validate_instance(schema, "00abc00")
        self.assert_runtime_invalid(schema, "00AB00", code="SCHEMA_RUNTIME_PATTERN_MISMATCH")
        invalid_pattern = closed_schema(type="string", pattern="[")
        with self.assertRaises(runtime.SchemaRuntimeError) as caught:
            runtime.validate_schema(invalid_pattern)
        self.assertEqual("SCHEMA_RUNTIME_PATTERN_INVALID", caught.exception.code)

    def test_pattern_audit_rejects_high_cost_and_non_closed_constructs(self) -> None:
        high_cost = (
            "(a+)+$",
            "(a|aa)+$",
            "^" + "a*" * 12 + "b$",
        )
        for pattern in high_cost:
            with self.subTest(pattern=pattern):
                with self.assertRaises(runtime.SchemaRuntimeError) as caught:
                    runtime.validate_schema(closed_schema(type="string", pattern=pattern))
                self.assertEqual("SCHEMA_RUNTIME_PATTERN_HIGH_COST", caught.exception.code)

        unsupported = (
            r"(a)\1",
            r"(a)?(?(1)b|c)",
            r"(?<=a)b",
            r"(?P<name>a)",
            r"(?=a)b",
        )
        for pattern in unsupported:
            with self.subTest(pattern=pattern):
                with self.assertRaises(runtime.SchemaRuntimeError) as caught:
                    runtime.validate_schema(closed_schema(type="string", pattern=pattern))
                self.assertEqual("SCHEMA_RUNTIME_PATTERN_UNSUPPORTED", caught.exception.code)

    def test_pattern_input_has_an_independent_hard_bound(self) -> None:
        schema = closed_schema(type="string", pattern="^a+$")
        self.assert_runtime_invalid(
            schema,
            "a" * (runtime.MAX_PATTERN_INPUT_CHARS + 1),
            code="SCHEMA_RUNTIME_LIMIT_EXCEEDED",
        )

        with self.assertRaises(runtime.SchemaRuntimeError) as caught:
            runtime.validate_schema(
                closed_schema(type="string", pattern="a{999999999999999999999}")
            )
        self.assertEqual("SCHEMA_RUNTIME_LIMIT_EXCEEDED", caught.exception.code)

    def test_instance_control_characters_cannot_exploit_end_anchor_semantics(self) -> None:
        digest_schema = closed_schema(type="string", pattern="^[a-f0-9]{64}$")
        for suffix in ("\n", "\r", "\x00"):
            with self.subTest(suffix=repr(suffix)):
                self.assert_runtime_invalid(
                    digest_schema,
                    "a" * 64 + suffix,
                    code="SCHEMA_RUNTIME_TEXT_INVALID",
                )

    def test_exact_id_registry_is_local_only_and_unknown_remote_ref_fails(self) -> None:
        worker_result = next(
            schema
            for schema in self.schemas
            if schema["properties"]["schema"]["const"]
            == "simworld-semantic-index-worker-result/v1"
        )
        with self.assertRaises(runtime.SchemaRuntimeError) as caught:
            runtime.validate_schema(worker_result)
        self.assertEqual("SCHEMA_RUNTIME_REF_UNRESOLVED", caught.exception.code)
        runtime.validate_schema(worker_result, registry=self.registry)

        unknown = closed_schema(**{"$ref": "https://invalid.example/schema.json"})
        with self.assertRaises(runtime.SchemaRuntimeError) as caught:
            runtime.validate_schema(unknown, registry=self.registry)
        self.assertEqual("SCHEMA_RUNTIME_REF_UNRESOLVED", caught.exception.code)

    def test_recursive_ref_and_python_container_cycles_fail_closed(self) -> None:
        recursive = closed_schema(
            **{
                "$defs": {"node": {"$ref": "#/$defs/node"}},
                "$ref": "#/$defs/node",
            }
        )
        with self.assertRaises(runtime.SchemaRuntimeError) as caught:
            runtime.validate_schema(recursive)
        self.assertEqual("SCHEMA_RUNTIME_REF_CYCLE", caught.exception.code)

        cyclic_instance: list[Any] = []
        cyclic_instance.append(cyclic_instance)
        self.assert_runtime_invalid(
            closed_schema(type="array", maxItems=1, items={"type": "array", "maxItems": 1}),
            cyclic_instance,
            code="SCHEMA_RUNTIME_CYCLE",
        )

    def test_unknown_keyword_and_excessive_depth_fail_schema_audit(self) -> None:
        unsupported = closed_schema(type="object", description="annotations are not in the pinned subset")
        with self.assertRaises(runtime.SchemaRuntimeError) as caught:
            runtime.validate_schema(unsupported)
        self.assertEqual("SCHEMA_RUNTIME_KEYWORD_UNSUPPORTED", caught.exception.code)

        child: dict[str, Any] = {"type": "integer"}
        for _ in range(runtime.MAX_SCHEMA_DEPTH + 2):
            child = {"allOf": [child]}
        too_deep = closed_schema(allOf=[child])
        with self.assertRaises(runtime.SchemaRuntimeError) as caught:
            runtime.validate_schema(too_deep)
        self.assertEqual("SCHEMA_RUNTIME_LIMIT_EXCEEDED", caught.exception.code)

    def test_large_unique_array_is_linear_and_hard_cap_precedes_iteration(self) -> None:
        count = 100_000
        schema = closed_schema(
            type="array",
            minItems=count,
            maxItems=count,
            items={"type": "integer"},
            uniqueItems=True,
        )
        runtime.validate_instance(schema, list(range(count)))
        huge = [0] * (runtime.MAX_INSTANCE_CONTAINER_ITEMS + 1)
        self.assert_runtime_invalid(schema, huge, code="SCHEMA_RUNTIME_LIMIT_EXCEEDED")

    @unittest.skipIf(ThirdPartyValidator is None, "jsonschema is absent under isolated system Python")
    def test_supported_subset_matches_jsonschema_on_differential_cases(self) -> None:
        cases = [
            (
                closed_schema(
                    type="object",
                    additionalProperties=False,
                    required=["kind", "values"],
                    properties={
                        "kind": {"enum": ["a", "b"]},
                        "values": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 3,
                            "items": {"type": "integer", "minimum": 0, "maximum": 9},
                            "uniqueItems": True,
                        },
                    },
                    allOf=[
                        {
                            "if": {"properties": {"kind": {"const": "a"}}},
                            "then": {"properties": {"values": {"contains": {"const": 1}}}},
                            "else": {"not": {"properties": {"values": {"contains": {"const": 1}}}}},
                        }
                    ],
                ),
                (
                    {"kind": "a", "values": [1, 2]},
                    {"kind": "a", "values": [2]},
                    {"kind": "b", "values": [2, 3]},
                    {"kind": "b", "values": [1]},
                    {"kind": "b", "values": [2, 2]},
                    {"kind": "c", "values": [2]},
                ),
            ),
            (
                closed_schema(
                    anyOf=[{"type": "null"}, {"type": "string", "pattern": "^ok"}],
                ),
                (None, "okay", "bad", 1, True),
            ),
        ]
        for schema, instances in cases:
            validator = ThirdPartyValidator(schema)
            for instance in instances:
                with self.subTest(instance=instance):
                    expected = validator.is_valid(instance)
                    try:
                        runtime.validate_instance(schema, instance)
                        observed = True
                    except runtime.SchemaRuntimeError:
                        observed = False
                    self.assertEqual(expected, observed)


if __name__ == "__main__":
    unittest.main()
