from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import semantic_index_production_adapter as adapter  # noqa: E402
import semantic_index_schema_runtime as schema_runtime  # noqa: E402


ADAPTER_SCHEMA_PATH = TOOLS_DIR / "semantic_index_production_adapter_v2_schema.json"


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def schema_digests() -> adapter.SchemaDigests:
    return adapter.SchemaDigests(
        **{name: digest(f"schema:{name}") for name in adapter.SCHEMA_DIGEST_FIELDS}
    )


def source_closure() -> adapter.SourceClosureDigests:
    return adapter.SourceClosureDigests(
        **{
            name: digest(f"source:{name}")
            for name in adapter.SOURCE_CLOSURE_DIGEST_FIELDS
        }
    )


def protocol_pins() -> adapter.ProtocolPins:
    return adapter.ProtocolPins(
        revision=adapter.WORKER_PROTOCOL_REVISION,
        protocol_source_sha256=digest("worker-protocol-source"),
        wire_schema_bundle_sha256=digest("wire-schema-bundle"),
        transport=adapter.WORKER_TRANSPORT,
    )


def launcher_pins() -> adapter.LauncherPins:
    return adapter.LauncherPins(
        mode=adapter.LAUNCHER_HANDOFF_MODE,
        fd_transport=adapter.LAUNCHER_FD_TRANSPORT,
        peer_credential_verification=(adapter.LAUNCHER_PEER_CREDENTIAL_VERIFICATION),
        peer_process_start_verification=(adapter.LAUNCHER_PROCESS_START_VERIFICATION),
        socket_inode_verification=adapter.LAUNCHER_SOCKET_INODE_VERIFICATION,
    )


def contract_inputs() -> adapter.AdapterContractInputs:
    return adapter.AdapterContractInputs(
        implementation_revision="sha256:" + digest("adapter-implementation"),
        schema_digests=schema_digests(),
        source_closure=source_closure(),
        protocol=protocol_pins(),
        launcher=launcher_pins(),
    )


def sealed_json(value: dict) -> adapter.ExplicitSealedJson:
    raw = adapter.canonical_json_bytes(value)
    return adapter.ExplicitSealedJson(
        canonical_bytes=raw,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )


class SemanticIndexProductionAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(ADAPTER_SCHEMA_PATH.read_text(encoding="utf-8"))
        schema_runtime.validate_schema(cls.schema)

    def assert_contract_error(
        self,
        code: str,
        callback,
    ) -> adapter.AdapterContractError:
        with self.assertRaises(adapter.AdapterContractError) as captured:
            callback()
        self.assertEqual(captured.exception.code, code)
        return captured.exception

    def test_builder_is_canonical_deterministic_and_formally_valid(self) -> None:
        inputs = contract_inputs()
        first = adapter.build_offline_adapter_contract(inputs)
        second = adapter.build_offline_adapter_contract(inputs)
        self.assertEqual(first, second)
        self.assertEqual(
            first.canonical_bytes, adapter.canonical_json_bytes(first.instance)
        )
        self.assertEqual(
            first.sha256, hashlib.sha256(first.canonical_bytes).hexdigest()
        )
        schema_runtime.validate_instance(self.schema, first.instance)

        instance = first.instance
        self.assertFalse(instance["production_capable"])
        self.assertFalse(instance["registration"]["registered"])
        self.assertEqual(
            instance["registration"]["mode"],
            "not_registered_offline_contract_only",
        )
        self.assertEqual(instance["schema_digests"], inputs.schema_digests.as_dict())
        self.assertEqual(
            instance["source_closure"],
            inputs.source_closure.as_dict(),
        )
        self.assertEqual(
            instance["operations"]["phase"], list(adapter.PHASE_OPERATIONS)
        )
        self.assertEqual(
            instance["operations"]["control"],
            list(adapter.CONTROL_OPERATIONS),
        )
        self.assertFalse(any(instance["ambient_discovery"].values()))
        self.assertFalse(any(instance["commands"].values()))
        self.assertFalse(any(instance["lifecycle"].values()))
        self.assertFalse(
            instance["credential_transport"]["undeclared_transform_coverage_claimed"]
        )

    def test_all_digest_and_pin_inputs_are_explicit_closed_dataclasses(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(adapter.SchemaDigests)),
            adapter.SCHEMA_DIGEST_FIELDS,
        )
        self.assertEqual(len(adapter.SCHEMA_DIGEST_FIELDS), 20)
        self.assertEqual(
            tuple(
                field.name for field in dataclasses.fields(adapter.SourceClosureDigests)
            ),
            adapter.SOURCE_CLOSURE_DIGEST_FIELDS,
        )
        self.assertEqual(len(adapter.SOURCE_CLOSURE_DIGEST_FIELDS), 7)
        for closed_type in (
            adapter.SchemaDigests,
            adapter.SourceClosureDigests,
            adapter.ProtocolPins,
            adapter.LauncherPins,
            adapter.AdapterContractInputs,
        ):
            parameters = getattr(closed_type, "__dataclass_params__")
            self.assertTrue(parameters.frozen)
            self.assertIn("__slots__", closed_type.__dict__)

        with self.assertRaises(TypeError):
            adapter.SchemaDigests(
                **schema_digests().as_dict(),
                unexpected=digest("unexpected"),
            )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            schema_digests().reviewed_job_v2 = digest("replacement")

    def test_bad_digest_revision_and_exact_pins_fail_without_echoing_values(
        self,
    ) -> None:
        values = schema_digests().as_dict()
        values["phase_request_v1"] = "SECRET-invalid-digest"
        error = self.assert_contract_error(
            "ADAPTER_CONTRACT_DIGEST_INVALID",
            lambda: adapter.SchemaDigests(**values),
        )
        self.assertNotIn("SECRET-invalid-digest", str(error))

        self.assert_contract_error(
            "ADAPTER_CONTRACT_REVISION_INVALID",
            lambda: dataclasses.replace(
                contract_inputs(),
                implementation_revision=digest("missing-prefix"),
            ),
        )
        self.assert_contract_error(
            "ADAPTER_CONTRACT_PIN_INVALID",
            lambda: dataclasses.replace(protocol_pins(), revision="worker/latest"),
        )
        self.assert_contract_error(
            "ADAPTER_CONTRACT_PIN_INVALID",
            lambda: dataclasses.replace(
                launcher_pins(),
                fd_transport="caller_socket_path",
            ),
        )
        self.assert_contract_error(
            "ADAPTER_CANONICAL_JSON_INVALID",
            lambda: adapter.canonical_json_bytes({"outside_i_json": 2**53}),
        )

    def test_schema_and_offline_control_reject_unknown_or_registered_contracts(
        self,
    ) -> None:
        sealed = adapter.build_offline_adapter_contract(contract_inputs())
        unknown = sealed.instance
        unknown["runtime_endpoint"] = "127.0.0.1:9999"
        with self.assertRaises(schema_runtime.SchemaRuntimeError) as captured:
            schema_runtime.validate_instance(self.schema, unknown)
        self.assertEqual(
            captured.exception.code,
            "SCHEMA_RUNTIME_ADDITIONAL_PROPERTY",
        )

        for field_path in ("production_capable", "registered"):
            changed = sealed.instance
            if field_path == "production_capable":
                changed[field_path] = True
            else:
                changed["registration"][field_path] = True
            with self.subTest(field=field_path):
                with self.assertRaises(schema_runtime.SchemaRuntimeError):
                    schema_runtime.validate_instance(self.schema, changed)
                raw = adapter.canonical_json_bytes(changed)
                changed_sealed = adapter.SealedAdapterContract(
                    canonical_bytes=raw,
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
                self.assert_contract_error(
                    "ADAPTER_CONTRACT_REGISTRATION_INVALID",
                    lambda changed_sealed=changed_sealed: (
                        adapter.UnregisteredOfflineAdapterControl(changed_sealed)
                    ),
                )

    def test_static_registry_is_empty_and_every_execution_gate_is_fixed(self) -> None:
        sealed = adapter.build_offline_adapter_contract(contract_inputs())
        control = adapter.UnregisteredOfflineAdapterControl(sealed)
        self.assertEqual(adapter.registered_adapters(), ())
        self.assertEqual(control.registry_snapshot(), ())
        self.assertIsNone(adapter.lookup_registered_adapter(adapter.ADAPTER_ID))
        self.assertIsNone(control.lookup())
        self.assertFalse(hasattr(adapter, "register_adapter"))
        self.assertFalse(hasattr(control, "register"))

        calls = [
            (lambda operation=operation: control.execute_phase(operation, object()))
            for operation in adapter.PHASE_OPERATIONS
        ]
        calls.extend(
            (lambda operation=operation: control.execute_control(operation, object()))
            for operation in adapter.CONTROL_OPERATIONS
        )
        calls.extend(
            (
                lambda: control.execute_phase("free_form", object()),
                lambda: control.execute_control("free_form", object()),
                control.execute_live,
            )
        )
        expected_public = {
            "code": adapter.ADAPTER_NOT_REGISTERED,
            "message": "Semantic-index Production adapter is not registered",
            "retryable": False,
            "production_capable": False,
            "registered": False,
        }
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(adapter.AdapterControlError) as captured:
                    call()
                self.assertEqual(
                    captured.exception.code, adapter.ADAPTER_NOT_REGISTERED
                )
                self.assertEqual(captured.exception.public_dict(), expected_public)

    def test_explicit_pair_verifier_is_digest_binding_only_and_never_production(
        self,
    ) -> None:
        request = sealed_json(
            {
                "schema": "simworld-semantic-index-phase-request/v1",
                "request_id": "request-001",
            }
        )
        response = sealed_json(
            {
                "schema": "simworld-semantic-index-worker-result/v1",
                "request_binding": {"request_sha256": request.expected_sha256},
            }
        )
        control = adapter.UnregisteredOfflineAdapterControl(
            adapter.build_offline_adapter_contract(contract_inputs())
        )
        verification = control.verify_pair(request, response)
        self.assertTrue(verification.digest_binding_verified)
        self.assertTrue(verification.schema_pair_verified)
        self.assertFalse(verification.schema_validation_performed)
        self.assertFalse(verification.execution_performed)
        self.assertFalse(verification.network_used)
        self.assertFalse(verification.production_verified)
        with self.assertRaises(TypeError):
            adapter.OfflinePairVerification(
                request_sha256=request.expected_sha256,
                response_sha256=response.expected_sha256,
                production_verified=True,
            )

        wrong_binding = sealed_json(
            {
                "schema": "simworld-semantic-index-worker-result/v1",
                "request_binding": {"request_sha256": digest("wrong-request")},
            }
        )
        self.assert_contract_error(
            "ADAPTER_OFFLINE_PAIR_BINDING_MISMATCH",
            lambda: control.verify_pair(request, wrong_binding),
        )
        wrong_schema = sealed_json(
            {
                "schema": "simworld-semantic-index-control-result/v1",
                "request_binding": {"request_sha256": request.expected_sha256},
            }
        )
        self.assert_contract_error(
            "ADAPTER_OFFLINE_PAIR_INVALID",
            lambda: control.verify_pair(request, wrong_schema),
        )
        self.assert_contract_error(
            "ADAPTER_OFFLINE_PAIR_DIGEST_MISMATCH",
            lambda: adapter.ExplicitSealedJson(
                canonical_bytes=request.canonical_bytes,
                expected_sha256=digest("wrong-pin"),
            ),
        )

    def test_builder_registry_verifier_and_gates_perform_no_ambient_io(self) -> None:
        request_value = {
            "schema": "simworld-semantic-index-control-request/v1",
            "request_id": "control-001",
        }
        request_raw = adapter.canonical_json_bytes(request_value)
        request_sha = hashlib.sha256(request_raw).hexdigest()
        response_value = {
            "schema": "simworld-semantic-index-control-result/v1",
            "request_binding": {"request_sha256": request_sha},
        }
        response_raw = adapter.canonical_json_bytes(response_value)

        denied = AssertionError("ambient I/O is forbidden")
        with (
            mock.patch.object(os, "getenv", side_effect=denied) as getenv,
            mock.patch("builtins.open", side_effect=denied) as open_file,
            mock.patch.object(socket, "socket", side_effect=denied) as open_socket,
            mock.patch.object(subprocess, "Popen", side_effect=denied) as popen,
            mock.patch.object(subprocess, "run", side_effect=denied) as run,
        ):
            sealed = adapter.build_offline_adapter_contract(contract_inputs())
            control = adapter.UnregisteredOfflineAdapterControl(sealed)
            self.assertEqual(control.registry_snapshot(), ())
            self.assertIsNone(control.lookup())
            request = adapter.ExplicitSealedJson(request_raw, request_sha)
            response = adapter.ExplicitSealedJson(
                response_raw,
                hashlib.sha256(response_raw).hexdigest(),
            )
            control.verify_pair(request, response)
            with self.assertRaises(adapter.AdapterControlError):
                control.execute_live()

        for patched in (getenv, open_file, open_socket, popen, run):
            patched.assert_not_called()

    def test_source_closure_has_no_discovery_process_network_or_legacy_surface(
        self,
    ) -> None:
        source_path = TOOLS_DIR / "semantic_index_production_adapter.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_roots.add(node.module.split(".", 1)[0])
        self.assertTrue(
            {
                "os",
                "pathlib",
                "socket",
                "subprocess",
            }.isdisjoint(imported_roots)
        )
        for forbidden in (
            "execute_semantic_asset_index_job",
            "semantic_index_job_adapters",
            "legacy_semantic_index",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
