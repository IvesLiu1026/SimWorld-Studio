from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Callable


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TOOLS_DIR), str(TOOLS_DIR / "tests")]

from semantic_index_production_contracts import (  # noqa: E402
    ContractValidationError,
    ProductionContractBundle,
    canonical_jcs_bytes,
    decode_canonical_jcs,
    validate_production_contracts,
)
from semantic_index_production_fixtures import (  # noqa: E402
    NOW,
    image,
    make_bundle,
    mutate_approval,
    mutate_document,
    mutate_evidence,
    seal,
    sha,
    unseal,
)


class CanonicalContractTests(unittest.TestCase):
    def assert_code(self, expected: str, callback: Callable[[], Any]) -> None:
        with self.assertRaises(ContractValidationError) as caught:
            callback()
        self.assertEqual(caught.exception.code, expected)

    def test_canonical_jcs_and_utf16_key_order(self) -> None:
        self.assertEqual(
            canonical_jcs_bytes({"\ue000": 2, "😀": 1}),
            '{"😀":1,"\ue000":2}'.encode(),
        )
        self.assertEqual(decode_canonical_jcs(b'{"a":1}'), {"a": 1})

    def test_duplicate_non_nfc_float_integer_and_noncanonical_rejected(self) -> None:
        cases = (
            (b'{"a":1,"a":2}', "CONTRACT_DUPLICATE_KEY"),
            ('{"x":"e\u0301"}'.encode(), "CONTRACT_TEXT_NONCANONICAL"),
            (b'{"x":1.0}', "CONTRACT_FLOAT_FORBIDDEN"),
            (b'{"x":9223372036854775808}', "CONTRACT_INTEGER_INVALID"),
            (b'{ "a":1}', "CONTRACT_JSON_NONCANONICAL"),
        )
        for raw, code in cases:
            with self.subTest(code=code):
                self.assert_code(code, lambda raw=raw: decode_canonical_jcs(raw))
        self.assertEqual(
            decode_canonical_jcs(b'{"x":9007199254740992}'),
            {"x": 9007199254740992},
        )


class ProductionBundleTests(unittest.TestCase):
    def assert_invalid(self, bundle: ProductionContractBundle, code: str) -> None:
        with self.assertRaises(ContractValidationError) as caught:
            validate_production_contracts(bundle, now_utc=NOW)
        self.assertEqual(caught.exception.code, code)

    def test_valid_complete_formally_valid_bundle(self) -> None:
        bundle = make_bundle()
        self.assertEqual(21, len(bundle.contract_schemas))
        self.assertIn(
            "launcher_verification_receipt_schema_sha256",
            bundle.contract_schemas,
        )
        result = validate_production_contracts(bundle, now_utc=NOW)
        self.assertEqual(result.accepted_asset_ids, ("bp_chair", "sm_table"))
        self.assertEqual(len(result.evidence_sha256_by_phase), 7)

    def test_formal_schema_source_is_release_pinned_not_plan_self_authorized(self) -> None:
        bundle = make_bundle()
        key = "execution_plan_schema_sha256"
        replacement = json.loads(bundle.contract_schemas[key].raw)
        replacement.pop("additionalProperties")
        schemas = dict(bundle.contract_schemas)
        schemas[key] = seal(replacement)
        self.assert_invalid(
            dataclasses.replace(bundle, contract_schemas=schemas),
            "CONTRACT_SCHEMA_TRUST_MISMATCH",
        )

    def test_launcher_verification_receipt_schema_is_mandatory_and_release_pinned(
        self,
    ) -> None:
        key = "launcher_verification_receipt_schema_sha256"
        missing_bundle = make_bundle()
        missing_schemas = dict(missing_bundle.contract_schemas)
        missing_schemas.pop(key)
        self.assert_invalid(
            dataclasses.replace(missing_bundle, contract_schemas=missing_schemas),
            "CONTRACT_SCHEMA_SET_INVALID",
        )

        changed_bundle = make_bundle()
        changed_schema = json.loads(changed_bundle.contract_schemas[key].raw)
        changed_schema["title"] += " changed"
        changed_schemas = dict(changed_bundle.contract_schemas)
        changed_schemas[key] = seal(changed_schema)
        self.assert_invalid(
            dataclasses.replace(changed_bundle, contract_schemas=changed_schemas),
            "CONTRACT_SCHEMA_TRUST_MISMATCH",
        )

    def test_independently_recomputed_basis_rejects_rebound_tamper(self) -> None:
        bundle = make_bundle(
            basis_mutator=lambda value: value["commitments"].__setitem__(
                "worker_sha256", sha("attacker")
            )
        )
        self.assert_invalid(bundle, "CONTRACT_APPROVAL_BASIS_MISMATCH")

    def test_partition_overlap_count_hash_and_source_attacks(self) -> None:
        attacks = (
            (
                lambda job: job["reviewed_selection"]["rejected_set"]["assets"][0].__setitem__(
                    "asset_id", "bp_chair"
                ),
                "CONTRACT_PARTITION_INVALID",
            ),
            (
                lambda job: job["reviewed_selection"]["accepted_set"].__setitem__(
                    "count", 99
                ),
                "CONTRACT_BINDING_MISMATCH",
            ),
            (
                lambda job: job["reviewed_selection"]["accepted_set"].__setitem__(
                    "assets_sha256", sha("wrong")
                ),
                "CONTRACT_BINDING_MISMATCH",
            ),
        )
        for mutate, code in attacks:
            with self.subTest(code=code):
                bundle = make_bundle()
                self.assert_invalid(
                    dataclasses.replace(
                        bundle,
                        reviewed_job=mutate_document(bundle.reviewed_job, mutate),
                    ),
                    code,
                )
        bundle = make_bundle()
        source = copy.deepcopy(unseal(bundle.source_candidates))
        source[-1]["object_path"] = "/Game/SimWorld/Assets/Unexpected.Unexpected"
        self.assert_invalid(
            dataclasses.replace(bundle, source_candidates=seal(source)),
            "CONTRACT_BINDING_MISMATCH",
        )

    def test_generation_derivation_and_storage_ownership(self) -> None:
        bundle = make_bundle()
        foreign_workspace = mutate_document(
            bundle.execution_plan,
            lambda plan: plan["generation"]["artifact_namespace"].__setitem__(
                "workspace", "/var/lib/simworld/foreign"
            ),
        )
        self.assert_invalid(
            dataclasses.replace(bundle, execution_plan=foreign_workspace),
            "CONTRACT_GENERATION_OWNERSHIP_INVALID",
        )
        foreign_generation = mutate_document(
            bundle.execution_plan,
            lambda plan: plan["generation"].__setitem__(
                "generation_id", "semantic-generation:" + "f" * 64
            ),
        )
        self.assert_invalid(
            dataclasses.replace(bundle, execution_plan=foreign_generation),
            "CONTRACT_BINDING_MISMATCH",
        )

    def test_job_plan_approval_and_evidence_bindings(self) -> None:
        bundle = mutate_evidence(
            make_bundle(),
            "inspect",
            lambda value: value["job_binding"].__setitem__("sha256", sha("wrong")),
        )
        self.assert_invalid(bundle, "CONTRACT_BINDING_MISMATCH")

        wrong_revision = mutate_evidence(
            make_bundle(),
            "inspect",
            lambda value: value.__setitem__(
                "operation_revision", "sha256:" + sha("wrong-operation")
            ),
        )
        self.assert_invalid(wrong_revision, "CONTRACT_BINDING_MISMATCH")

        rebound_request = mutate_evidence(
            make_bundle(),
            "inspect",
            lambda value: (
                value["request"].__setitem__("sha256", sha("rebound-request")),
                value["idempotency"].__setitem__(
                    "request_sha256", sha("rebound-request")
                ),
            ),
        )
        self.assert_invalid(rebound_request, "CONTRACT_BINDING_MISMATCH")

        rebound_ledger = mutate_evidence(
            make_bundle(),
            "inspect",
            lambda value: value["idempotency"].__setitem__(
                "ledger_identity", "worker-ledger:foreign"
            ),
        )
        self.assert_invalid(rebound_ledger, "CONTRACT_BINDING_MISMATCH")

        rebound_ledger_revision = mutate_evidence(
            make_bundle(),
            "inspect",
            lambda value: value["idempotency"].__setitem__(
                "ledger_revision", "sha256:" + sha("foreign-ledger-revision")
            ),
        )
        self.assert_invalid(rebound_ledger_revision, "CONTRACT_BINDING_MISMATCH")

        rebound_bundle = make_bundle()
        rebound_requests = list(rebound_bundle.phase_requests)
        rebound_requests[0] = mutate_document(
            rebound_requests[0],
            lambda value: (
                value.__setitem__(
                    "idempotency_ledger_identity",
                    "worker-ledger:foreign",
                ),
                value.__setitem__(
                    "idempotency_ledger_revision",
                    "sha256:" + sha("foreign-ledger-revision"),
                ),
            ),
        )
        rebound_bundle = dataclasses.replace(
            rebound_bundle,
            phase_requests=tuple(rebound_requests),
        )
        rebound_request_sha256 = rebound_requests[0].expected_sha256
        rebound_bundle = mutate_evidence(
            rebound_bundle,
            "inspect",
            lambda value: (
                value["idempotency"].__setitem__(
                    "ledger_identity",
                    "worker-ledger:foreign",
                ),
                value["idempotency"].__setitem__(
                    "ledger_revision",
                    "sha256:" + sha("foreign-ledger-revision"),
                ),
                value["idempotency"].__setitem__(
                    "request_sha256",
                    rebound_request_sha256,
                ),
                value["request"].__setitem__(
                    "sha256",
                    rebound_request_sha256,
                ),
            ),
        )
        self.assert_invalid(rebound_bundle, "CONTRACT_BINDING_MISMATCH")

        raw_request_bundle = make_bundle()
        raw_requests = list(raw_request_bundle.phase_requests)
        raw_requests[0] = mutate_document(
            raw_requests[0],
            lambda value: value.__setitem__(
                "deadline_monotonic_ns", value["deadline_monotonic_ns"] + 1
            ),
        )
        self.assert_invalid(
            dataclasses.replace(raw_request_bundle, phase_requests=tuple(raw_requests)),
            "CONTRACT_BINDING_MISMATCH",
        )

    def test_capability_target_runtime_and_formal_scope_gate(self) -> None:
        target = mutate_evidence(
            make_bundle(),
            "inspect",
            lambda value: value["credentials_observed"][0].__setitem__(
                "target_identity", "foreign:target"
            ),
        )
        self.assert_invalid(target, "CONTRACT_CAPABILITY_MISMATCH")
        runtime = mutate_evidence(
            make_bundle(),
            "inspect",
            lambda value: value["target_attestation"]["targets"][0].__setitem__(
                "runtime_image", image("foreign")
            ),
        )
        self.assert_invalid(runtime, "CONTRACT_BINDING_MISMATCH")
        scope = mutate_evidence(
            make_bundle(),
            "inspect",
            lambda value: value["credentials_observed"][0].__setitem__(
                "scope", "postgres_generation_write"
            ),
        )
        self.assert_invalid(scope, "CONTRACT_SCHEMA_INSTANCE_INVALID")

    def test_expiry_phase_order_live_audit_and_time_policy(self) -> None:
        expired = make_bundle(
            plan_mutator=lambda plan: plan["deployment_preflight"].__setitem__(
                "expires_at", "2026-07-21T11:00:00Z"
            )
        )
        self.assert_invalid(expired, "CONTRACT_EXPIRED")
        overlap = mutate_evidence(
            make_bundle(),
            "render",
            lambda value: value.__setitem__("started_at", "2026-07-21T11:40:30Z"),
        )
        self.assert_invalid(overlap, "CONTRACT_TIME_INVALID")
        stale = mutate_evidence(
            make_bundle(),
            "reconcile",
            lambda value: value["payload"]["live_audit"].__setitem__(
                "expires_at", "2026-07-21T11:59:00Z"
            ),
        )
        self.assert_invalid(stale, "CONTRACT_LIVE_AUDIT_STALE")
        with self.assertRaises(ContractValidationError) as caught:
            validate_production_contracts(
                make_bundle(), now_utc=NOW, max_clock_skew_seconds=3601
            )
        self.assertEqual(caught.exception.code, "CONTRACT_TIME_POLICY_INVALID")

    def test_cost_ceiling_and_observed_cost(self) -> None:
        observed = mutate_evidence(
            make_bundle(),
            "caption",
            lambda value: value["payload"]["usage"].__setitem__(
                "cost_minor_units", 1001
            ),
        )
        self.assert_invalid(observed, "CONTRACT_COST_UNAUTHORIZED")
        lowered = mutate_approval(
            make_bundle(),
            "cost_owner",
            lambda receipt: receipt["authorization"].__setitem__(
                "max_cost_minor_units", 999
            ),
        )
        self.assert_invalid(lowered, "CONTRACT_COST_UNAUTHORIZED")

    def test_inspect_smoke_and_query_require_accepted_ids_and_rank(self) -> None:
        inspect = mutate_evidence(
            make_bundle(),
            "inspect",
            lambda value: value["payload"]["records"][0].__setitem__(
                "asset_id", "sm_rejected"
            ),
        )
        self.assert_invalid(inspect, "CONTRACT_ASSET_SET_MISMATCH")
        smoke = mutate_evidence(
            make_bundle(),
            "reconcile",
            lambda value: value["payload"]["smoke_results"][0].__setitem__(
                "asset_id", "sm_rejected"
            ),
        )
        self.assert_invalid(smoke, "CONTRACT_SMOKE_INVALID")
        missing = mutate_evidence(
            make_bundle(),
            "reconcile",
            lambda value: value["payload"]["query_results"][0].__setitem__(
                "returned_asset_ids", ["sm_table"]
            ),
        )
        self.assert_invalid(missing, "CONTRACT_QUERY_INVALID")
        rank_basis = make_bundle(
            plan_mutator=lambda plan: plan["acceptance"]["shadow_queries"][0].__setitem__(
                "minimum_rank", 1
            )
        )
        rank = mutate_evidence(
            rank_basis,
            "reconcile",
            lambda value: value["payload"]["query_results"][0].update(
                returned_asset_ids=["sm_table", "bp_chair"], best_expected_rank=2
            ),
        )
        self.assert_invalid(rank, "CONTRACT_QUERY_INVALID")

    def test_typed_error_mutation_state_and_retryability(self) -> None:
        def invalid_failure(value: dict[str, Any]) -> None:
            value["outcome"] = {
                "status": "failed",
                "mutation_state": "committed",
                "retryable": True,
                "error": {
                    "dependency": "qdrant",
                    "public_code": "QDRANT_WRITE_FAILED",
                    "retryable": True,
                    "mutation_state": "committed",
                    "redacted_message": "Qdrant generation write failed",
                },
                "remote_receipt_recovered": False,
            }
            value["payload"] = {
                "kind": "typed_failure/v1",
                "operator_action": "manual_reconcile",
                "last_safe_artifact_sha256": None,
            }

        self.assert_invalid(
            mutate_evidence(make_bundle(), "qdrant", invalid_failure),
            "CONTRACT_SCHEMA_INSTANCE_INVALID",
        )

    def test_successful_reconcile_requires_compatibility_receipt(self) -> None:
        bundle = mutate_evidence(
            make_bundle(),
            "reconcile",
            lambda value: value["payload"].__setitem__(
                "compatibility_snapshot_v1_sha256", None
            ),
        )
        self.assert_invalid(bundle, "CONTRACT_SCHEMA_INSTANCE_INVALID")

    def test_secret_transforms_and_percent_decoding_are_rejected(self) -> None:
        raw_secret = b"raw-secret-20260721"
        binary_secret = b"\xfb\xffsecret\xfa"
        standard = base64.b64encode(binary_secret)
        urlsafe = base64.urlsafe_b64encode(binary_secret)
        hash_secret = hashlib.sha256(binary_secret).hexdigest().encode()
        percent_secret = b"percent-secret"
        transforms = (
            (raw_secret, raw_secret),
            (binary_secret, standard),
            (binary_secret, standard.rstrip(b"=")),
            (binary_secret, urlsafe),
            (binary_secret, urlsafe.rstrip(b"=")),
            (binary_secret, hash_secret),
            (binary_secret, hash_secret.upper()),
            (percent_secret, b"".join(f"%{byte:02X}".encode() for byte in percent_secret)),
            (percent_secret, b"pe%72cent-secret"),
        )
        for secret, transformed in transforms:
            with self.subTest(transformed=transformed):
                text = transformed.decode("ascii")
                bundle = mutate_evidence(
                    make_bundle(),
                    "reconcile",
                    lambda value, text=text: value.__setitem__(
                        "evidence_id", "semantic-index-evidence:" + text
                    ),
                )
                self.assert_invalid(
                    dataclasses.replace(bundle, secret_values=(secret,)),
                    "CONTRACT_SECRET_LEAK",
                )


if __name__ == "__main__":
    unittest.main()
