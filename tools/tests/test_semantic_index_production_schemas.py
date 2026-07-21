from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import sys
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(TOOLS_DIR / "tests"))

import semantic_index_worker_protocol as worker_protocol  # noqa: E402
import semantic_index_production_fixtures as production_fixtures  # noqa: E402
import test_semantic_index_worker_protocol as worker_protocol_fixtures  # noqa: E402

H = "a" * 64
H2 = "b" * 64
REV = f"sha256:{H}"
REV2 = f"sha256:{H2}"
GEN = f"semantic-generation:{H}"
PG_DATABASE = f"semantic_gen_{H[:50]}"
PG_SCHEMA = f"snapshot_{H[:54]}"
QDRANT_COLLECTION = f"semantic_gen_{H}"
NONCE = "1" * 32
SNAPSHOT = "asset-snapshot-20260721-reviewed"
IMAGE = f"registry.example/simworld/component@sha256:{H}"
NOW = "2026-07-21T12:00:00Z"
LATER = "2026-07-21T13:00:00Z"
CONTROL_LATER = "2026-07-21T12:05:00Z"

PUBLIC_ERROR_MESSAGES = {
    ("worker", "WORKER_BUSY"): "Worker is temporarily busy",
    ("worker", "WORKER_INTERNAL"): "Worker operation failed",
    ("worker", "WORKER_LEDGER_CONFLICT"): "Worker idempotency ledger conflict",
    ("worker", "WORKER_CANCELLED"): "Worker operation was cancelled",
    ("unreal", "UNREAL_UNAVAILABLE"): "Unreal runtime is unavailable",
    ("unreal", "UNREAL_OPERATION_FAILED"): "Unreal operation failed",
    ("caption", "CAPTION_UNAVAILABLE"): "Caption provider is unavailable",
    ("caption", "CAPTION_BUDGET_EXCEEDED"): "Caption budget was exceeded",
    ("caption", "CAPTION_RESULT_INVALID"): "Caption result was invalid",
    ("caption", "CAPTION_STATE_AMBIGUOUS"): "Caption request state is ambiguous",
    ("embedding", "EMBEDDING_UNAVAILABLE"): "Embedding service is unavailable",
    ("embedding", "EMBEDDING_RESULT_INVALID"): "Embedding result was invalid",
    ("postgres", "POSTGRES_UNAVAILABLE"): "PostgreSQL is unavailable",
    ("postgres", "POSTGRES_WRITE_FAILED"): "PostgreSQL generation write failed",
    ("qdrant", "QDRANT_UNAVAILABLE"): "Qdrant is unavailable",
    ("qdrant", "QDRANT_WRITE_FAILED"): "Qdrant generation write failed",
    ("evidence_store", "EVIDENCE_STORE_UNAVAILABLE"): "Evidence store is unavailable",
    ("evidence_store", "EVIDENCE_PUBLICATION_FAILED"): "Evidence publication failed",
}


SCHEMA_FILES = {
    "job": "semantic_asset_index_job_v2_schema.json",
    "plan": "semantic_asset_index_execution_plan_v2_schema.json",
    "basis": "semantic_index_production_approval_basis_schema.json",
    "approval": "semantic_index_approval_receipt_schema.json",
    "request": "semantic_index_phase_request_schema.json",
    "result": "semantic_index_worker_result_schema.json",
    "control_request": "semantic_index_control_request_schema.json",
    "control_result": "semantic_index_control_result_schema.json",
    "worker_root": "semantic_index_worker_artifact_root_schema.json",
    "evidence": "semantic_index_phase_evidence_schema.json",
}


def strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def serialization_contract() -> dict[str, Any]:
    return {
        "encoding": "utf-8",
        "canonicalization": "rfc8785-jcs-v1",
        "duplicate_keys": "reject",
        "unicode_normalization": "require_already_nfc",
        "numbers": "integers_only",
        "nonfinite_numbers": "reject",
        "trailing_newline": False,
    }


def asset(*, accepted: bool, static_mesh: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {
        "asset_id": "sm_table" if static_mesh else "bp_chair",
        "ue_name": "SM_Table" if static_mesh else "BP_Chair",
        "ue_path": "/Game/Furniture/SM_Table.SM_Table" if static_mesh else "/Game/Furniture/BP_Chair.BP_Chair",
        "asset_type": "StaticMesh" if static_mesh else "Blueprint",
        "source_pack": "VISTA",
        "content_revision": REV,
        "status": "reviewed_accepted" if accepted else "reviewed_rejected",
    }
    if not accepted:
        value["asset_id"] = "sm_rejected"
        value["ue_name"] = "SM_Rejected"
        value["ue_path"] = "/Game/Furniture/SM_Rejected.SM_Rejected"
        value["asset_type"] = "StaticMesh"
        value["reason_code"] = "data_owner_rejected"
        value["ledger_entry_sha256"] = H2
    return value


def valid_job() -> dict[str, Any]:
    resource = {
            "assets": 2,
            "rendered_views": 4,
            "render_pixels": 1048576,
            "caption_calls": 2,
            "caption_output_tokens": 512,
            "catalog_bytes": 8192,
            "embedding_batches": 1,
            "postgres_rows": 2,
            "qdrant_points": 2,
            "dense_vector_payload_bytes": 8192,
    }
    return {
        "schema": "simworld-semantic-asset-index-job/v2",
        "source": {
            "v1_job_schema": "simworld-semantic-asset-index-job/v1",
            "v1_job_sha256": H,
            "v1_job_revision": REV,
            "preparation_receipt_schema": "simworld-semantic-asset-index-job-preparation-receipt/v1",
            "preparation_receipt_sha256": H2,
            "candidate_set_count": 3,
            "candidate_set_sha256": H,
            "project": {
                "name": "VISTAWorld",
                "revision": "project:20260721",
                "engine_version": "5.5.4",
            },
            "content": {"mount_point": "/Game", "revision": REV},
            "recipe_schema": "simworld-semantic-asset-index-recipe/v1",
            "recipe_sha256": H,
            "recipe_revision": "recipe:20260721",
            "target_snapshot_revision": SNAPSHOT,
        },
        "reviewed_selection": {
            "selection": "explicit_reviewed_partition",
            "selection_approval_basis_sha256": H,
            "candidate_set": {"count": 3, "assets_sha256": H},
            "accepted_set": {
                "count": 2,
                "assets_sha256": H,
                "assets": [asset(accepted=True), asset(accepted=True, static_mesh=True)],
            },
            "rejected_set": {"count": 1, "assets_sha256": H2, "assets": [asset(accepted=False)]},
            "rejection_ledger": {
                "schema": "simworld-semantic-index-rejection-ledger/v1",
                "revision": REV2,
                "sha256": H2,
            },
            "partition_proof": {
                "algorithm": "sorted-asset-id-partition-v1",
                "candidate_count": 3,
                "candidate_set_sha256": H,
                "accepted_count": 2,
                "accepted_assets_sha256": H,
                "rejected_count": 1,
                "rejected_assets_sha256": H2,
                "disjoint": True,
                "complete": True,
                "proof_sha256": H,
            },
        },
        "recipe_contract": {
            "schema": "simworld-semantic-asset-index-recipe/v1",
            "sha256": H,
            "revision": "recipe:20260721",
            "unchanged_from_v1_job": True,
            "accepted_set_only": True,
        },
        "snapshot_target": {
            "asset_snapshot_revision": SNAPSHOT,
            "postgres_schema_revision": 3,
            "qdrant_dense_vector_name": "dense_v1",
            "qdrant_sparse_vector_name": "sparse_v1",
            "row_point_parity_required": True,
            "fresh_live_audit_required": True,
            "catalog_complete": False,
            "snapshot_complete": False,
        },
        "resource_contract": {
            "computed_for_accepted_assets_sha256": H,
            "limits": copy.deepcopy(resource),
            "estimates": copy.deepcopy(resource),
            "cost_authorized": False,
            "state_change_authorized": False,
        },
        "data_owner_approval": {
            "receipt_schema": "simworld-semantic-index-approval-receipt/v1",
            "receipt_id": "semantic-index-approval:data-review-001",
            "receipt_sha256": H,
            "approval_basis_sha256": H,
            "approval_kind": "data_owner_review",
            "accepted_assets_sha256": H,
            "rejection_ledger_sha256": H2,
            "content_revision": REV,
        },
        "execution_contract": {
            "reviewed_only": True,
            "production_authorized": False,
            "cost_authorized": False,
            "mutation_authorized": False,
            "activation_authorized": False,
            "legacy_runner_authorized": False,
            "ambient_discovery_allowed": False,
            "separate_v2_plan_required": True,
        },
        "serialization_contract": serialization_contract(),
        "job_revision": REV,
    }


def approval_scope(*, data: bool) -> dict[str, Any]:
    if data:
        return {
            "scope_kind": "reviewed_selection",
            "source_v1_job_sha256": H,
            "source_v1_job_revision": REV,
            "reviewed_selection_basis_sha256": H,
            "accepted_assets_sha256": H,
            "accepted_asset_count": 2,
            "rejection_ledger_sha256": H2,
            "content_revision": REV,
        }
    return {
        "scope_kind": "production_build",
        "reviewed_job_sha256": H,
        "reviewed_job_revision": REV,
        "plan_approval_basis_sha256": H,
        "generation_id": GEN,
        "generation_nonce_sha256": H,
        "target_snapshot_revision": SNAPSHOT,
        "worker_deployment_identity": "semantic-worker:20260721",
        "active_generation_pointer_identity": "semantic-active-pointer:prod",
        "expected_active_generation_epoch": 7,
    }


def valid_approval(kind: str) -> dict[str, Any]:
    roles = {
        "data_owner_review": "data_owner",
        "cost_owner": "cost_owner",
        "admin_state_change": "state_change_admin",
        "deployment": "deployment_attestor",
        "runtime": "runtime_owner",
        "rollback_readiness": "rollback_owner",
    }
    authorizations: dict[str, dict[str, Any]] = {
        "data_owner_review": {
            "kind": "data_owner_review",
            "reviewed_selection_approved": True,
            "production_execution_authorized": False,
        },
        "cost_owner": {
            "kind": "cost_owner",
            "max_caption_calls": 2,
            "max_output_tokens": 512,
            "currency": "USD",
            "minor_unit_exponent": 6,
            "max_cost_minor_units": 1000000,
            "rounding_mode": "ceiling_each_provider_charge_to_minor_unit",
            "retry_charge_policy": "all_provider_accepted_requests_counted",
        },
        "admin_state_change": {
            "kind": "admin_state_change",
            "allowed_mutating_phases": ["inspect", "render", "caption", "embed", "postgres", "qdrant"],
            "allowed_control_operations": [
                "query_phase_status",
                "recover_phase_receipt",
                "cancel_phase_work",
                "quarantine_generation",
            ],
            "activation_authorized": False,
        },
        "deployment": {
            "kind": "deployment",
            "deployment_preflight_sha256": H,
            "worker_image": IMAGE,
            "worker_protocol_sha256": H,
        },
        "runtime": {
            "kind": "runtime",
            "ue_lease_id": "ue-lease:001",
            "ue_lease_expires_at": LATER,
            "worker_deployment_identity": "semantic-worker:20260721",
        },
        "rollback_readiness": {
            "kind": "rollback_readiness",
            "bootstrap": True,
            "previous_generation_id": None,
            "previous_snapshot_revision": None,
            "rollback_readiness_receipt_sha256": H,
            "active_generation_pointer_identity": "semantic-active-pointer:prod",
            "expected_active_generation_epoch": 0,
        },
    }
    value = {
        "schema": "simworld-semantic-index-approval-receipt/v1",
        "receipt_id": f"semantic-index-approval:{kind}-001",
        "approval_kind": kind,
        "decision": "approved",
        "approval_basis_sha256": H,
        "scope": approval_scope(data=kind == "data_owner_review"),
        "authorization": authorizations[kind],
        "issuer": {
            "issuer_id": f"issuer:{kind}",
            "role": roles[kind],
            "key_id": "approval-key:001",
            "key_revision": REV,
        },
        "trust_bundle": {
            "schema": "simworld-approval-trust-bundle/v1",
            "revision": REV,
            "sha256": H,
            "issuer_membership_sha256": H2,
        },
        "issued_at": NOW,
        "expires_at": LATER,
        "signature": {
            "algorithm": "ed25519",
            "canonicalization": "rfc8785-jcs-v1",
            "signed_payload_sha256": H,
            "detached_signature_sha256": H2,
            "detached_signature_base64": "A" * 86 + "==",
        },
        "serialization_contract": serialization_contract(),
    }
    if kind == "rollback_readiness":
        value["scope"]["expected_active_generation_epoch"] = 0
    return value


def valid_existing_generation_rollback_approval() -> dict[str, Any]:
    value = valid_approval("rollback_readiness")
    value["scope"]["expected_active_generation_epoch"] = 7
    value["authorization"] = {
        "kind": "rollback_readiness",
        "bootstrap": False,
        "previous_generation_id": f"semantic-generation:{H2}",
        "previous_snapshot_revision": "asset-snapshot-20260720-active",
        "rollback_readiness_receipt_sha256": H,
        "active_generation_pointer_identity": "semantic-active-pointer:prod",
        "expected_active_generation_epoch": 7,
    }
    return value


def capability(scope: str, target: str) -> dict[str, Any]:
    return {
        "credential_generation": REV,
        "generation_id": GEN,
        "target_identity": target,
        "scope": scope,
    }


def credential_target(scope: str, target: str) -> dict[str, Any]:
    return {
        "credential_generation": REV,
        "target_identity": target,
        "scope": scope,
    }


def approval_ref(kind: str, *, data: bool = False) -> dict[str, Any]:
    return {
        "receipt_schema": "simworld-semantic-index-approval-receipt/v1",
        "receipt_id": f"semantic-index-approval:{kind}-001",
        "receipt_sha256": H,
        "approval_kind": kind,
        "approval_basis_sha256": H2 if data else H,
        "trust_bundle_sha256": H,
        "issuer_key_id": "approval-key:001",
        "issuer_key_revision": REV,
        "signature_algorithm": "ed25519",
        "expires_at": LATER,
    }


def valid_basis() -> dict[str, Any]:
    return {
        "schema": "simworld-semantic-index-production-approval-basis/v1",
        "revision": "semantic-index-production-approval-basis:v1",
        "reviewed_job_sha256": H,
        "reviewed_job_revision": REV,
        "commitments": {
            key: H
            for key in (
                "run_identity_sha256",
                "reviewed_job_reference_sha256",
                "contract_digests_sha256",
                "adapter_sha256",
                "deployment_preflight_sha256",
                "worker_sha256",
                "unreal_sha256",
                "caption_sha256",
                "embedding_sha256",
                "runtime_images_sha256",
                "target_attestations_sha256",
                "expected_sets_sha256",
                "acceptance_sha256",
                "phase_policy_sha256",
                "activation_contract_sha256",
            )
        },
        "generation_inputs": {
            "generation_nonce": NONCE,
            "generation_nonce_sha256": H2,
            "generation_nonce_sha256_derivation": "sha256(decoded-lowercase-hex-16-bytes)-v1",
            "derivation": "sha256-ascii-nul-separated(simworld-semantic-index-generation-id/v1,reviewed_job_revision,approval_basis_sha256)-v1",
            "target_generation_identities": {
                "artifact_store": {
                    "store_identity": "artifact-store:prod",
                    "workspace_root": "/var/lib/simworld",
                    "layout": "content-addressed-sha256-no-replace-v1",
                    "generation_name_policy": "semantic-generation-id-path-segment-v1",
                    "namespace_revision": REV,
                },
                "catalog": {"namespace_revision": REV},
                "postgres": {
                    "deployment_identity": "postgres-deployment:prod",
                    "schema_revision": 3,
                    "generation_name_policy": "semantic-generation-id-sql-identifiers-v1",
                },
                "qdrant": {
                    "cluster_identity": "qdrant-cluster:prod",
                    "dense_vector_name": "dense_v1",
                    "sparse_vector_name": "sparse_v1",
                    "generation_name_policy": "semantic-generation-id-qdrant-collection-v1",
                },
            },
            "generation_reservation_sha256": H,
            "previous_active_sha256": H,
            "active_generation_pointer_sha256": H,
            "cleanup_policy": "quarantine_failed_unactivated_generation",
        },
        "credential_targets": {
            "transport": "sealed_memfd_scm_rights_v1",
            "ordinary_phases": {
                "inspect": credential_target("ue_disposable_scene_inspect_spawn_cleanup", "ue-broker:prod"),
                "render": credential_target("ue_disposable_scene_render_staging_cleanup", "ue-broker:prod"),
                "caption": credential_target("caption_bounded_idempotent_call", "caption-project:vista"),
                "embed": credential_target("embedding_immutable_vector_staging", "embedding-project:vista"),
                "postgres": credential_target("postgres_generation_write", "postgres-deployment:prod"),
                "qdrant": credential_target("qdrant_generation_write", "qdrant-cluster:prod"),
            },
            "reconcile": {
                "postgres_read": credential_target("postgres_generation_read_only", "postgres-deployment:prod"),
                "qdrant_read": credential_target("qdrant_generation_read_only", "qdrant-cluster:prod"),
                "ue_runtime_read": credential_target("ue_runtime_read_only", "ue-broker:prod"),
            },
            "control_operations": {
                "query_phase_status": credential_target(
                    "semantic_index_phase_status_read", "semantic-worker:20260721"
                ),
                "recover_phase_receipt": credential_target(
                    "semantic_index_phase_receipt_recover", "semantic-worker:20260721"
                ),
                "cancel_phase_work": credential_target(
                    "semantic_index_phase_work_cancel", "semantic-worker:20260721"
                ),
                "quarantine_generation": credential_target(
                    "semantic_index_generation_quarantine", "semantic-worker:20260721"
                ),
            },
        },
        "serialization_contract": serialization_contract(),
    }


def phase_policy(operation: str, states: list[str]) -> dict[str, Any]:
    return {
        "operation": operation,
        "timeout_seconds": 300,
        "max_attempts": 2,
        "allowed_mutation_states": states,
    }


def plan_target_attestation(
    component: str,
    *,
    runtime_image: str | None = IMAGE,
) -> dict[str, Any]:
    return {
        "runtime_identity": f"{component}-runtime:20260721",
        "runtime_image": runtime_image,
        "whoami_sha256": H,
        "runtime_attestation_sha256": H,
    }


def valid_plan() -> dict[str, Any]:
    expected = {"count": 2, "sorted_ids_sha256": H, "canonicalization": "sorted-utf8-nfc-lf-v1"}
    return {
        "schema": "simworld-semantic-asset-index-execution-plan/v2",
        "profile": "production",
        "run_identity": {
            "run_id": "semantic-index-run:run-001",
            "correlation_id": "semantic-index-correlation:corr-001",
            "operator_id": "operator:codex",
            "service_owner_id": "service-owner:semantic-index",
            "retention_class": "production_evidence_1y",
        },
        "reviewed_job": {
            "schema": "simworld-semantic-asset-index-job/v2",
            "sha256": H,
            "revision": REV,
            "source_v1_job_sha256": H2,
            "preparation_receipt_sha256": H,
            "accepted_asset_count": 2,
            "accepted_assets_sha256": H,
            "rejected_asset_count": 1,
            "rejected_assets_sha256": H2,
            "rejection_ledger_sha256": H2,
            "content_revision": REV,
            "target_snapshot_revision": SNAPSHOT,
        },
        "approval_basis": {
            "schema": "simworld-semantic-index-production-approval-basis/v1",
            "revision": "semantic-index-production-approval-basis:v1",
            "sha256": H,
            "canonicalization": "rfc8785-jcs-v1",
        },
        "contract_digests": {
            key: H
            for key in (
                "reviewed_job_schema_sha256",
                "execution_plan_schema_sha256",
                "approval_basis_schema_sha256",
                "approval_receipt_schema_sha256",
                "launcher_verification_receipt_schema_sha256",
                "phase_request_schema_sha256",
                "worker_result_schema_sha256",
                "control_request_schema_sha256",
                "control_result_schema_sha256",
                "worker_artifact_root_schema_sha256",
                "phase_evidence_schema_sha256",
                "state_schema_sha256",
                "terminal_receipt_schema_sha256",
                "inspect_artifact_schema_sha256",
                "render_artifact_schema_sha256",
                "caption_artifact_schema_sha256",
                "embed_artifact_schema_sha256",
                "postgres_artifact_schema_sha256",
                "qdrant_artifact_schema_sha256",
                "reconcile_artifact_schema_sha256",
                "adapter_contract_sha256",
                "adapter_source_sha256",
                "coordinator_source_closure_sha256",
                "worker_protocol_sha256",
                "worker_source_closure_sha256",
                "dependency_lock_sha256",
                "sql_bundle_sha256",
            )
        },
        "adapter": {
            "adapter_id": "semantic-index-production-v2-uds",
            "contract_revision": "semantic-index-production-adapter/v2",
            "production_capable": True,
            "registration": "static_reviewed_registry_only",
        },
        "deployment_preflight": {
            "receipt_schema": "simworld-semantic-index-deployment-preflight-receipt/v1",
            "receipt_sha256": H,
            "signature_sha256": H2,
            "deployment_identity": "semantic-worker:20260721",
            "worker_image": IMAGE,
            "endpoint_map_sha256": H,
            "network_policy": "uds_and_loopback_mtls_only_v1",
            "whoami_contract_sha256": H,
            "worker_attestation_sha256": H,
            "issued_at": NOW,
            "expires_at": LATER,
        },
        "generation": {
            "generation_nonce": NONCE,
            "generation_nonce_sha256": H2,
            "generation_nonce_sha256_derivation": "sha256(decoded-lowercase-hex-16-bytes)-v1",
            "generation_id": GEN,
            "derivation": "sha256-ascii-nul-separated(simworld-semantic-index-generation-id/v1,reviewed_job_revision,approval_basis_sha256)-v1",
            "reviewed_job_sha256": H,
            "reviewed_job_revision": REV,
            "approval_basis_sha256": H,
            "target_generation_identities_sha256": H,
            "artifact_namespace": {
                "store_identity": "artifact-store:prod",
                "workspace_root": "/var/lib/simworld",
                "workspace": f"/var/lib/simworld/semantic_generation_{H}",
                "layout": "content-addressed-sha256-no-replace-v1",
                "generation_name_policy": "semantic-generation-id-path-segment-v1",
                "namespace_revision": REV,
            },
            "reservation": {
                "receipt_schema": "simworld-semantic-index-generation-reservation/v1",
                "receipt_sha256": H,
                "reservation_id": "generation-reservation:001",
                "mode": "exclusive_create_only",
                "owner_identity": "service-owner:semantic-index",
                "issued_at": NOW,
                "expires_at": LATER,
            },
            "previous_active": {"generation_id": None, "snapshot_revision": None},
            "active_generation_pointer": {
                "identity": "semantic-active-pointer:prod",
                "observed_epoch": 0,
                "activation_mode": "single_pointer_epoch_compare_and_swap",
                "activation_during_build": False,
            },
            "cleanup_policy": "quarantine_failed_unactivated_generation",
        },
        "worker": {
            "transport": "launcher_attested_preconnected_af_unix_framed_canonical_json_v1",
            "socket_path": "/run/simworld/semantic-index.sock",
            "socket_owner_uid": 1000,
            "socket_owner_gid": 1000,
            "protocol_revision": "simworld-semantic-index-worker/v1",
            "peer_deployment_identity": "semantic-worker:20260721",
            "whoami_sha256": H,
            "peer_attestation_sha256": H,
            "runtime_attestation_sha256": H,
            "host_boot_id_sha256": H,
            "idempotency_ledger_identity": "worker-ledger:prod",
            "idempotency_ledger_revision": REV,
            "idempotency_ledger_mode": "append_only_prepare_result_fsync_no_replace_v1",
            "idempotency_incomplete_policy": "recovery_required_no_blind_retry_v1",
            "peer_pid": 12345,
            "process_start_time_ticks": 987654321,
            "process_start_token_sha256": H,
            "socket_device": 42,
            "socket_inode": 4242,
            "socket_inode_binding_sha256": H,
            "max_frame_bytes": 2097152,
            "connect_timeout_ms": 1000,
            "read_chunk_timeout_ms": 1000,
            "write_timeout_ms": 1000,
            "deadline_field": "deadline_monotonic_ns",
            "deadline_clock": "host_shared_CLOCK_MONOTONIC",
            "phase_operations": [
                "inspect_exact_assets",
                "render_exact_views",
                "caption_exact_render_set",
                "embed_exact_text_set",
                "upsert_postgres_exact",
                "upsert_qdrant_exact",
                "reconcile_exact_snapshot",
            ],
            "control_operations": [
                "query_phase_status",
                "recover_phase_receipt",
                "cancel_phase_work",
                "quarantine_generation",
            ],
        },
        "unreal": {
            "project_name": "VISTAWorld",
            "project_revision": "project:20260721",
            "content_revision": REV,
            "runtime_image": IMAGE,
            "broker_identity": "ue-broker:prod",
            "broker_operations": [
                "inspect_asset_identity",
                "spawn_in_disposable_scene",
                "read_spawned_asset_properties",
                "render_fixed_views",
                "destroy_disposable_scene",
            ],
            "slot_id": "ue-slot:001",
            "lease_id": "ue-lease:001",
            "lease_issued_at": NOW,
            "lease_expires_at": LATER,
            "inspect_scope": "ue_disposable_scene_inspect_spawn_cleanup",
            "render_scope": "ue_disposable_scene_render_staging_cleanup",
        },
        "caption": {
            "provider_identity": "caption-provider:prod",
            "project_identity": "caption-project:vista",
            "model_id": "caption-model:v1",
            "model_snapshot": "provider-snapshot:20260721",
            "endpoint_identity": "caption-endpoint:prod",
            "prompt_bytes_sha256": H,
            "output_schema_bytes_sha256": H,
            "render_recipe_bytes_sha256": H,
            "max_calls": 2,
            "max_output_tokens": 512,
            "currency": "USD",
            "minor_unit_exponent": 6,
            "max_cost_minor_units": 1000000,
            "rounding_mode": "ceiling_each_provider_charge_to_minor_unit",
            "retry_charge_policy": "all_provider_accepted_requests_counted",
            "idempotency_policy": "provider_key_or_worker_ledger_no_blind_retry",
            "tools_allowed": False,
        },
        "embedding": {
            "endpoint_identity": "embedding-endpoint:prod",
            "project_identity": "embedding-project:vista",
            "dense_model_id": "dense-model:v1",
            "dense_model_revision": REV,
            "dense_artifact_manifest_sha256": H,
            "dense_dimensions": 1024,
            "sparse_model_id": "sparse-model:v1",
            "sparse_model_revision": REV,
            "sparse_artifact_manifest_sha256": H,
            "sparse_max_dimensions": 100000,
            "batch_size": 1,
        },
        "storage": {
            "catalog": {"generation_id": GEN, "namespace_revision": REV, "active_target": False},
            "postgres": {
                "deployment_identity": "postgres-deployment:prod",
                "database": PG_DATABASE,
                "schema_name": PG_SCHEMA,
                "schema_revision": 3,
                "generation_id": GEN,
                "generation_name_policy": "semantic-generation-id-sql-identifiers-v1",
                "active_target": False,
            },
            "qdrant": {
                "cluster_identity": "qdrant-cluster:prod",
                "collection": QDRANT_COLLECTION,
                "dense_vector_name": "dense_v1",
                "sparse_vector_name": "sparse_v1",
                "generation_id": GEN,
                "generation_name_policy": "semantic-generation-id-qdrant-collection-v1",
                "active_target": False,
                "point_id_policy": "sha256-generation-plus-asset-id-v1",
                "same_id_replay_policy": "allow_only_identical_payload_and_vector_digests",
            },
        },
        "runtime_images": {key: IMAGE for key in ("unreal", "worker", "postgres", "qdrant", "embedding")},
        "target_attestations": {
            "unreal": plan_target_attestation("unreal"),
            "caption": plan_target_attestation("caption", runtime_image=None),
            "embedding": plan_target_attestation("embedding"),
            "postgres": plan_target_attestation("postgres"),
            "qdrant": plan_target_attestation("qdrant"),
        },
        "expected_sets": {
            "assets": copy.deepcopy(expected),
            "renders": {"count": 4, "sorted_ids_sha256": H, "canonicalization": "sorted-utf8-nfc-lf-v1"},
            "catalog_records": copy.deepcopy(expected),
            "dense_vectors": copy.deepcopy(expected),
            "sparse_vectors": copy.deepcopy(expected),
            "postgres_rows": copy.deepcopy(expected),
            "qdrant_points": copy.deepcopy(expected),
        },
        "acceptance": {
            "shadow_queries": [
                {
                    "query_id": "query:zh-chair",
                    "language": "zh-TW",
                    "query_text": "一張木椅",
                    "query_text_sha256": H,
                    "normalization_revision": REV,
                    "expected_asset_ids": ["bp_chair"],
                    "minimum_rank": 5,
                },
                {
                    "query_id": "query:en-chair",
                    "language": "en",
                    "query_text": "a wooden chair",
                    "query_text_sha256": H2,
                    "normalization_revision": REV,
                    "expected_asset_ids": ["bp_chair"],
                    "minimum_rank": 5,
                },
            ],
            "smoke_targets": [
                {
                    "asset_id": "bp_chair",
                    "ue_path": "/Game/Furniture/BP_Chair.BP_Chair",
                    "asset_type": "Blueprint",
                },
                {
                    "asset_id": "sm_table",
                    "ue_path": "/Game/Furniture/SM_Table.SM_Table",
                    "asset_type": "StaticMesh",
                },
            ],
            "authoritative_snapshot_schema": "simworld-asset-snapshot-exact-set/v2",
        },
        "credentials": {
            "transport": "sealed_memfd_scm_rights_v1",
            "json_contains_credential_bytes": False,
            "ordinary_phases": {
                "inspect": capability("ue_disposable_scene_inspect_spawn_cleanup", "ue-broker:prod"),
                "render": capability("ue_disposable_scene_render_staging_cleanup", "ue-broker:prod"),
                "caption": capability("caption_bounded_idempotent_call", "caption-project:vista"),
                "embed": capability("embedding_immutable_vector_staging", "embedding-project:vista"),
                "postgres": capability("postgres_generation_write", "postgres-deployment:prod"),
                "qdrant": capability("qdrant_generation_write", "qdrant-cluster:prod"),
            },
            "reconcile": {
                "postgres_read": capability("postgres_generation_read_only", "postgres-deployment:prod"),
                "qdrant_read": capability("qdrant_generation_read_only", "qdrant-cluster:prod"),
                "ue_runtime_read": capability("ue_runtime_read_only", "ue-broker:prod"),
            },
            "control_operations": {
                "query_phase_status": capability(
                    "semantic_index_phase_status_read", "semantic-worker:20260721"
                ),
                "recover_phase_receipt": capability(
                    "semantic_index_phase_receipt_recover", "semantic-worker:20260721"
                ),
                "cancel_phase_work": capability(
                    "semantic_index_phase_work_cancel", "semantic-worker:20260721"
                ),
                "quarantine_generation": capability(
                    "semantic_index_generation_quarantine", "semantic-worker:20260721"
                ),
            },
        },
        "phase_policy": {
            "max_total_elapsed_seconds": 3600,
            "deadline_clock": "host_shared_CLOCK_MONOTONIC",
            "inspect": phase_policy("inspect_exact_assets", ["none", "ambiguous"]),
            "render": phase_policy("render_exact_views", ["none", "staged", "ambiguous"]),
            "caption": phase_policy("caption_exact_render_set", ["none", "committed", "ambiguous"]),
            "embed": phase_policy("embed_exact_text_set", ["none", "staged", "ambiguous"]),
            "postgres": phase_policy("upsert_postgres_exact", ["none", "committed", "ambiguous"]),
            "qdrant": phase_policy(
                "upsert_qdrant_exact", ["none", "staged", "committed", "ambiguous"]
            ),
            "reconcile": phase_policy("reconcile_exact_snapshot", ["none"]),
        },
        "approvals": {
            "data_owner_review": approval_ref("data_owner_review", data=True),
            "cost_owner": approval_ref("cost_owner"),
            "admin_state_change": approval_ref("admin_state_change"),
            "deployment": approval_ref("deployment"),
            "runtime": approval_ref("runtime"),
            "rollback_readiness": approval_ref("rollback_readiness"),
        },
        "activation_contract": {
            "successful_build_state": "snapshot_built_verified",
            "activation_authorized": False,
            "activation_is_separate_operation": True,
            "activation_primitive": "single_active_generation_pointer_epoch_cas",
            "multi_system_atomicity_claimed": False,
            "rollback_primitive": "same_pointer_epoch_cas_to_previous_generation",
        },
        "serialization_contract": serialization_contract(),
    }


def credential_observation(scope: str, target: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "credential_generation": REV,
        "generation_id": GEN,
        "target_identity": target,
    }


def target_observation(component: str, target: str, *, runtime_image: str | None = IMAGE) -> dict[str, Any]:
    return {
        "component": component,
        "target_identity": target,
        "runtime_identity": f"{component}-runtime:20260721",
        "runtime_image": runtime_image,
        "whoami_sha256": H,
        "runtime_attestation_sha256": H,
    }


def inspect_record(*, static_mesh: bool = False) -> dict[str, Any]:
    stem = "Table" if static_mesh else "Chair"
    return {
        "asset_id": "sm_table" if static_mesh else "bp_chair",
        "ue_path": f"/Game/Furniture/{'SM' if static_mesh else 'BP'}_{stem}.{'SM' if static_mesh else 'BP'}_{stem}",
        "asset_type": "StaticMesh" if static_mesh else "Blueprint",
        "loaded": True,
        "spawned": True,
        "bounds_micrometers": {"x": 1000000, "y": 1000000, "z": 1000000},
        "collision": "enabled",
        "material_slots": [
            {
                "slot_index": 0,
                "material_path": f"/Game/Materials/M_{stem}.M_{stem}",
                "pbr_complete": True,
                "textures": [
                    {
                        "role": "BaseColor",
                        "ue_path": f"/Game/Textures/T_{stem}.T_{stem}",
                        "content_sha256": H,
                    }
                ],
            }
        ],
        "cleanup_state": "disposable_spawn_destroyed",
    }


def valid_inspect_evidence() -> dict[str, Any]:
    return {
        "schema": "simworld-semantic-index-phase-evidence/v1",
        "evidence_id": "semantic-index-evidence:inspect-001",
        "run_identity": {
            "run_id": "semantic-index-run:run-001",
            "correlation_id": "semantic-index-correlation:corr-001",
            "operator_id": "operator:codex",
            "service_owner_id": "service-owner:semantic-index",
            "ue_slot_id": "ue-slot:001",
            "ue_lease_id": "ue-lease:001",
            "ue_lease_expires_at": LATER,
            "retention_class": "production_evidence_1y",
        },
        "job_binding": {
            "schema": "simworld-semantic-asset-index-job/v2",
            "sha256": H,
            "revision": REV,
            "accepted_asset_count": 2,
            "accepted_assets_sha256": H,
            "content_revision": REV,
            "snapshot_revision": SNAPSHOT,
        },
        "plan_binding": {
            "schema": "simworld-semantic-asset-index-execution-plan/v2",
            "sha256": H,
            "approval_basis_sha256": H,
            "generation_id": GEN,
            "generation_nonce_sha256": H2,
            "reservation_receipt_sha256": H,
            "worker_deployment_identity": "semantic-worker:20260721",
        },
        "phase": "inspect",
        "operation": "inspect_exact_assets",
        "operation_revision": REV,
        "idempotency": {
            "key": REV,
            "request_sha256": H,
            "ledger_identity": "worker-ledger:prod",
            "ledger_revision": REV,
            "result": "applied",
        },
        "request": {
            "schema": "simworld-semantic-index-phase-request/v1",
            "sha256": H,
            "input_artifact_sha256": H,
            "deadline_monotonic_ns": 999999999,
            "host_boot_id_sha256": H,
            "credential_transport": "sealed_memfd_scm_rights_v1",
            "json_contains_credential_bytes": False,
        },
        "credentials_observed": [
            credential_observation("ue_disposable_scene_inspect_spawn_cleanup", "ue-broker:prod")
        ],
        "target_attestation": {
            "worker_deployment_identity": "semantic-worker:20260721",
            "worker_image": IMAGE,
            "deployment_preflight_sha256": H,
            "worker_whoami_sha256": H,
            "worker_runtime_attestation_sha256": H,
            "targets": [target_observation("unreal", "ue-broker:prod")],
        },
        "prior_receipts": [],
        "asset_set": {"count": 2, "sorted_ids_sha256": H, "canonicalization": "sorted-utf8-nfc-lf-v1"},
        "outcome": {
            "status": "success",
            "mutation_state": "none",
            "retryable": False,
            "error": None,
            "remote_receipt_recovered": False,
        },
        "started_at": NOW,
        "completed_at": LATER,
        "payload": {
            "kind": "inspect_exact_assets/v1",
            "content_revision": REV,
            "disposable_scene_id": "ue-disposable-scene:001",
            "records_sha256": H,
            "records": [inspect_record(), inspect_record(static_mesh=True)],
            "scene_cleanup_state": "destroyed_and_verified",
        },
        "worker_signature": {
            "key_revision": REV,
            "signed_payload_sha256": H,
            "signature_sha256": H2,
        },
        "serialization_contract": serialization_contract(),
    }


def render_from_inspect() -> dict[str, Any]:
    value = valid_inspect_evidence()
    value["evidence_id"] = "semantic-index-evidence:render-001"
    value["phase"] = "render"
    value["operation"] = "render_exact_views"
    value["credentials_observed"] = [
        credential_observation("ue_disposable_scene_render_staging_cleanup", "ue-broker:prod")
    ]
    value["prior_receipts"] = [
        {
            "phase": "inspect",
            "evidence_id": "semantic-index-evidence:inspect-001",
            "evidence_sha256": H,
            "asset_set_sha256": H,
            "output_artifact_sha256": H,
        }
    ]
    value["outcome"]["mutation_state"] = "staged"
    value["payload"] = {
        "kind": "render_exact_views/v1",
        "render_recipe_sha256": H,
        "ue_runtime_revision": "unreal-runtime:20260721",
        "render_set_count": 4,
        "render_set_sha256": H,
        "render_manifest_sha256": H,
        "records": [
            {
                "asset_id": "bp_chair",
                "view_id": "view:front",
                "image_sha256": H,
                "width_px": 512,
                "height_px": 512,
                "nonblank": True,
                "source_inspect_record_sha256": H,
            },
            {
                "asset_id": "bp_chair",
                "view_id": "view:side",
                "image_sha256": H,
                "width_px": 512,
                "height_px": 512,
                "nonblank": True,
                "source_inspect_record_sha256": H,
            },
            {
                "asset_id": "sm_table",
                "view_id": "view:front",
                "image_sha256": H,
                "width_px": 512,
                "height_px": 512,
                "nonblank": True,
                "source_inspect_record_sha256": H,
            },
            {
                "asset_id": "sm_table",
                "view_id": "view:side",
                "image_sha256": H,
                "width_px": 512,
                "height_px": 512,
                "nonblank": True,
                "source_inspect_record_sha256": H,
            },
        ],
        "scene_cleanup_state": "destroyed_and_verified",
    }
    return value


def prior_receipt(phase: str) -> dict[str, Any]:
    return {
        "phase": phase,
        "evidence_id": f"semantic-index-evidence:{phase}-001",
        "evidence_sha256": H,
        "asset_set_sha256": H,
        "output_artifact_sha256": H,
    }


def caption_from_render() -> dict[str, Any]:
    value = render_from_inspect()
    value["evidence_id"] = "semantic-index-evidence:caption-001"
    value["phase"] = "caption"
    value["operation"] = "caption_exact_render_set"
    value["credentials_observed"] = [
        credential_observation("caption_bounded_idempotent_call", "caption-project:vista")
    ]
    value["target_attestation"]["targets"] = [
        target_observation("caption", "caption-project:vista", runtime_image=None)
    ]
    value["prior_receipts"] = [prior_receipt("inspect"), prior_receipt("render")]
    value["outcome"]["mutation_state"] = "committed"
    value["payload"] = {
        "kind": "caption_exact_render_set/v1",
        "render_manifest_sha256": H,
        "provider_identity": "caption-provider:prod",
        "project_identity": "caption-project:vista",
        "model_snapshot": "provider-snapshot:20260721",
        "prompt_sha256": H,
        "output_schema_sha256": H,
        "strict_parse": True,
        "catalog_sha256": H,
        "records": [
            {
                "asset_id": asset_id,
                "render_subset_sha256": H,
                "caption_record_sha256": H,
                "caption_text_sha256": H,
                "strict_schema_valid": True,
                "provider_request_id": f"provider-request:{index}",
                "provider_idempotency_id": f"provider-idempotency:{index}",
                "output_tokens": 256,
            }
            for index, asset_id in enumerate(("bp_chair", "sm_table"), start=1)
        ],
        "usage": {
            "calls": 2,
            "input_tokens": 1024,
            "output_tokens": 512,
            "currency": "USD",
            "minor_unit_exponent": 6,
            "cost_minor_units": 1000000,
            "rounding_mode": "ceiling_each_provider_charge_to_minor_unit",
            "retry_charge_policy": "all_provider_accepted_requests_counted",
            "charged_retry_count": 0,
        },
    }
    return value


def embed_from_caption() -> dict[str, Any]:
    value = caption_from_render()
    value["evidence_id"] = "semantic-index-evidence:embed-001"
    value["phase"] = "embed"
    value["operation"] = "embed_exact_text_set"
    value["credentials_observed"] = [
        credential_observation("embedding_immutable_vector_staging", "embedding-project:vista")
    ]
    value["target_attestation"]["targets"] = [
        target_observation("embedding", "embedding-project:vista")
    ]
    value["prior_receipts"] = [
        prior_receipt("inspect"),
        prior_receipt("render"),
        prior_receipt("caption"),
    ]
    value["outcome"]["mutation_state"] = "staged"
    value["payload"] = {
        "kind": "embed_exact_text_set/v1",
        "catalog_sha256": H,
        "dense_model_revision": REV,
        "dense_artifact_manifest_sha256": H,
        "sparse_model_revision": REV,
        "sparse_artifact_manifest_sha256": H,
        "vector_bundle_manifest_sha256": H,
        "vector_set_sha256": H,
        "records": [
            {
                "asset_id": asset_id,
                "source_caption_sha256": H,
                "dense_vector_id": f"dense-vector:{index}",
                "dense_vector_sha256": H,
                "dense_dimensions": 1024,
                "sparse_vector_id": f"sparse-vector:{index}",
                "sparse_vector_sha256": H,
                "sparse_nonzero_count": 32,
                "finite": True,
            }
            for index, asset_id in enumerate(("bp_chair", "sm_table"), start=1)
        ],
    }
    return value


def postgres_from_embed() -> dict[str, Any]:
    value = embed_from_caption()
    value["evidence_id"] = "semantic-index-evidence:postgres-001"
    value["phase"] = "postgres"
    value["operation"] = "upsert_postgres_exact"
    value["credentials_observed"] = [
        credential_observation("postgres_generation_write", "postgres-deployment:prod")
    ]
    value["target_attestation"]["targets"] = [
        target_observation("postgres", "postgres-deployment:prod")
    ]
    value["prior_receipts"] = [
        prior_receipt("inspect"),
        prior_receipt("render"),
        prior_receipt("caption"),
        prior_receipt("embed"),
    ]
    value["outcome"]["mutation_state"] = "committed"
    value["payload"] = {
        "kind": "upsert_postgres_exact/v1",
        "deployment_identity": "postgres-deployment:prod",
        "database": PG_DATABASE,
        "schema_name": PG_SCHEMA,
        "schema_revision": 3,
        "generation_id": GEN,
        "transaction_marker": "postgres-transaction:001",
        "catalog_sha256": H,
        "row_set_sha256": H,
        "rows": [
            {
                "asset_id": asset_id,
                "row_sha256": H,
                "catalog_record_sha256": H,
                "readback_sha256": H,
            }
            for asset_id in ("bp_chair", "sm_table")
        ],
        "transaction_committed": True,
        "readback_complete": True,
    }
    return value


def qdrant_from_postgres() -> dict[str, Any]:
    value = postgres_from_embed()
    value["evidence_id"] = "semantic-index-evidence:qdrant-001"
    value["phase"] = "qdrant"
    value["operation"] = "upsert_qdrant_exact"
    value["credentials_observed"] = [
        credential_observation("qdrant_generation_write", "qdrant-cluster:prod")
    ]
    value["target_attestation"]["targets"] = [
        target_observation("qdrant", "qdrant-cluster:prod")
    ]
    value["prior_receipts"] = [
        prior_receipt("inspect"),
        prior_receipt("render"),
        prior_receipt("caption"),
        prior_receipt("embed"),
        prior_receipt("postgres"),
    ]
    value["outcome"]["mutation_state"] = "committed"
    value["payload"] = {
        "kind": "upsert_qdrant_exact/v1",
        "cluster_identity": "qdrant-cluster:prod",
        "collection": QDRANT_COLLECTION,
        "dense_vector_name": "dense_v1",
        "sparse_vector_name": "sparse_v1",
        "generation_id": GEN,
        "point_id_policy": "sha256-generation-plus-asset-id-v1",
        "same_id_replay_policy": "allow_only_identical_payload_and_vector_digests",
        "point_set_sha256": H,
        "points": [
            {
                "asset_id": asset_id,
                "point_id": H if index == 1 else H2,
                "payload_sha256": H,
                "dense_vector_sha256": H,
                "sparse_vector_sha256": H,
                "readback_sha256": H,
            }
            for index, asset_id in enumerate(("bp_chair", "sm_table"), start=1)
        ],
        "readback_complete": True,
    }
    return value


def reconcile_from_qdrant() -> dict[str, Any]:
    value = qdrant_from_postgres()
    value["evidence_id"] = "semantic-index-evidence:reconcile-001"
    value["phase"] = "reconcile"
    value["operation"] = "reconcile_exact_snapshot"
    value["credentials_observed"] = [
        credential_observation("postgres_generation_read_only", "postgres-deployment:prod"),
        credential_observation("qdrant_generation_read_only", "qdrant-cluster:prod"),
        credential_observation("ue_runtime_read_only", "ue-broker:prod"),
    ]
    value["target_attestation"]["targets"] = [
        target_observation("postgres", "postgres-deployment:prod"),
        target_observation("qdrant", "qdrant-cluster:prod"),
        target_observation("unreal", "ue-broker:prod"),
    ]
    value["prior_receipts"] = [
        prior_receipt("inspect"),
        prior_receipt("render"),
        prior_receipt("caption"),
        prior_receipt("embed"),
        prior_receipt("postgres"),
        prior_receipt("qdrant"),
    ]
    value["outcome"]["mutation_state"] = "none"
    value["payload"] = {
        "kind": "reconcile_exact_snapshot/v1",
        "authoritative_snapshot": {
            "schema": "simworld-asset-snapshot-exact-set/v2",
            "sha256": H,
            "snapshot_revision": SNAPSHOT,
            "asset_set": {"count": 2, "sorted_ids_sha256": H, "canonicalization": "sorted-utf8-nfc-lf-v1"},
        },
        "compatibility_snapshot_v1_sha256": H,
        "live_audit": {
            "schema": "simworld-asset-live-audit/v1",
            "sha256": H,
            "manifest_sha256": H,
            "observations_sha256": H,
            "issued_at": NOW,
            "expires_at": LATER,
        },
        "catalog_sha256": H,
        "postgres_row_set_sha256": H,
        "qdrant_point_set_sha256": H,
        "exact_set_parity": True,
        "runtime_attestations": [
            {
                "component": component,
                "target_identity": target,
                "runtime_image": IMAGE,
                "attestation_sha256": H,
            }
            for component, target in (
                ("unreal", "ue-broker:prod"),
                ("worker", "semantic-worker:20260721"),
                ("embedding", "embedding-project:vista"),
                ("postgres", "postgres-deployment:prod"),
                ("qdrant", "qdrant-cluster:prod"),
            )
        ],
        "query_results": [
            {
                "query_id": "query:zh-chair",
                "language": "zh-TW",
                "query_text_sha256": H,
                "normalization_revision": REV,
                "expected_asset_ids": ["bp_chair"],
                "returned_asset_ids": ["bp_chair", "sm_table"],
                "minimum_rank": 5,
                "best_expected_rank": 1,
                "passed": True,
            },
            {
                "query_id": "query:en-table",
                "language": "en",
                "query_text_sha256": H2,
                "normalization_revision": REV,
                "expected_asset_ids": ["sm_table"],
                "returned_asset_ids": ["sm_table", "bp_chair"],
                "minimum_rank": 5,
                "best_expected_rank": 1,
                "passed": True,
            },
        ],
        "smoke_results": [
            {
                "asset_id": "bp_chair",
                "asset_type": "Blueprint",
                "inspect_receipt_sha256": H,
                "loaded": True,
                "spawned": True,
                "material_slots_verified": True,
                "pbr_textures_verified": True,
                "cleanup_state": "disposable_spawn_destroyed",
            },
            {
                "asset_id": "sm_table",
                "asset_type": "StaticMesh",
                "inspect_receipt_sha256": H,
                "loaded": True,
                "spawned": True,
                "material_slots_verified": True,
                "pbr_textures_verified": True,
                "cleanup_state": "disposable_spawn_destroyed",
            },
        ],
        "previous_active": {"generation_id": None, "snapshot_revision": None, "ready": False},
        "rollback_readiness_receipt_sha256": H,
        "active_generation_pointer": {
            "identity": "semantic-active-pointer:prod",
            "observed_epoch": 0,
            "unchanged": True,
        },
        "activation_performed": False,
    }
    return value


PHASE_FIXTURES = {
    "inspect": valid_inspect_evidence,
    "render": render_from_inspect,
    "caption": caption_from_render,
    "embed": embed_from_caption,
    "postgres": postgres_from_embed,
    "qdrant": qdrant_from_postgres,
    "reconcile": reconcile_from_qdrant,
}

PHASE_CONTRACTS = {
    "inspect": {
        "operation": "inspect_exact_assets",
        "artifact_schema": "simworld-semantic-index-inspect-artifact/v1",
        "targets": [("unreal", "ue_disposable_scene_inspect_spawn_cleanup", "ue-broker:prod", IMAGE)],
    },
    "render": {
        "operation": "render_exact_views",
        "artifact_schema": "simworld-semantic-index-render-artifact/v1",
        "targets": [("unreal", "ue_disposable_scene_render_staging_cleanup", "ue-broker:prod", IMAGE)],
    },
    "caption": {
        "operation": "caption_exact_render_set",
        "artifact_schema": "simworld-semantic-index-caption-artifact/v1",
        "targets": [("caption", "caption_bounded_idempotent_call", "caption-project:vista", None)],
    },
    "embed": {
        "operation": "embed_exact_text_set",
        "artifact_schema": "simworld-semantic-index-embed-artifact/v1",
        "targets": [("embedding", "embedding_immutable_vector_staging", "embedding-project:vista", IMAGE)],
    },
    "postgres": {
        "operation": "upsert_postgres_exact",
        "artifact_schema": "simworld-semantic-index-postgres-artifact/v1",
        "targets": [("postgres", "postgres_generation_write", "postgres-deployment:prod", IMAGE)],
    },
    "qdrant": {
        "operation": "upsert_qdrant_exact",
        "artifact_schema": "simworld-semantic-index-qdrant-artifact/v1",
        "targets": [("qdrant", "qdrant_generation_write", "qdrant-cluster:prod", IMAGE)],
    },
    "reconcile": {
        "operation": "reconcile_exact_snapshot",
        "artifact_schema": "simworld-semantic-index-reconcile-artifact/v1",
        "targets": [
            ("postgres", "postgres_generation_read_only", "postgres-deployment:prod", IMAGE),
            ("qdrant", "qdrant_generation_read_only", "qdrant-cluster:prod", IMAGE),
            ("unreal", "ue_runtime_read_only", "ue-broker:prod", IMAGE),
        ],
    },
}

CONTROL_CONTRACTS = {
    "query_phase_status": {
        "revision": "sha256:89ec2677006dbda7892dfd30b63d2ef4df173760ba7bae0f9f08510188573094",
        "scope": "semantic_index_phase_status_read",
        "success_mutation_state": "none",
    },
    "recover_phase_receipt": {
        "revision": "sha256:c2618c4ab23112c05ff2697675b02224841818f11c2fddd164b0c97d5e68b1b2",
        "scope": "semantic_index_phase_receipt_recover",
        "success_mutation_state": "none",
    },
    "cancel_phase_work": {
        "revision": "sha256:151239b83048b806e9c1940457b24d53880c3140df022bbbd6fa4eb714fc673f",
        "scope": "semantic_index_phase_work_cancel",
        "success_mutation_state": "committed",
    },
    "quarantine_generation": {
        "revision": "sha256:76c589ba7651cf9b5fc6d899082f5742ff0b72c41bac5a80a618f83117edc62d",
        "scope": "semantic_index_generation_quarantine",
        "success_mutation_state": "committed",
    },
}


def worker_target(
    component: str,
    scope: str,
    target_identity: str,
    runtime_image: str | None,
) -> dict[str, Any]:
    return {
        "component": component,
        "scope": scope,
        "credential_generation": REV,
        "generation_id": GEN,
        "target_identity": target_identity,
        "runtime_identity": f"{component}-runtime:20260721",
        "runtime_image": runtime_image,
        "whoami_sha256": H,
        "runtime_attestation_sha256": H,
    }


def valid_worker_root(phase: str) -> dict[str, Any]:
    contract = PHASE_CONTRACTS[phase]
    return {
        "schema": "simworld-semantic-index-worker-artifact-root/v1",
        "protocol": "simworld-semantic-index-worker/v1",
        "phase": phase,
        "operation": contract["operation"],
        "request_sha256": H,
        "job_revision": REV,
        "reviewed_job_sha256": H,
        "approval_basis_sha256": H,
        "execution_plan_sha256": H,
        "generation_id": GEN,
        "generation_binding_sha256": H,
        "idempotency_key": REV,
        "idempotency_ledger_identity": "worker-ledger:prod",
        "idempotency_ledger_revision": REV,
        "input_artifact_sha256": H,
        "artifact_root": {
            "schema": contract["artifact_schema"],
            "sha256": H,
            "byte_count": 4096,
            "chunk_count": 1,
            "chunks_sha256": H,
        },
        "observed_targets": [worker_target(*target) for target in contract["targets"]],
        "worker_attestation": {
            "deployment_identity": "semantic-worker:20260721",
            "runtime_image": IMAGE,
            "whoami_sha256": H,
            "peer_attestation_sha256": H,
            "runtime_attestation_sha256": H,
            "host_boot_id_sha256": H,
            "process_start_token_sha256": H,
            "socket_inode_binding_sha256": H,
        },
        "issued_at": NOW,
        "expires_at": LATER,
        "serialization_contract": serialization_contract(),
    }


def failed_evidence(phase: str, *, mutation_state: str = "none") -> dict[str, Any]:
    value = PHASE_FIXTURES[phase]()
    retryable = mutation_state == "none"
    dependency, public_code, redacted_message = {
        "inspect": ("unreal", "UNREAL_UNAVAILABLE", "Unreal runtime is unavailable"),
        "render": ("unreal", "UNREAL_UNAVAILABLE", "Unreal runtime is unavailable"),
        "caption": ("caption", "CAPTION_UNAVAILABLE", "Caption provider is unavailable"),
        "embed": ("embedding", "EMBEDDING_UNAVAILABLE", "Embedding service is unavailable"),
        "postgres": ("postgres", "POSTGRES_UNAVAILABLE", "PostgreSQL is unavailable"),
        "qdrant": ("qdrant", "QDRANT_UNAVAILABLE", "Qdrant is unavailable"),
        "reconcile": ("worker", "WORKER_INTERNAL", "Worker operation failed"),
    }[phase]
    value["outcome"] = {
        "status": "failed",
        "mutation_state": mutation_state,
        "retryable": retryable,
        "error": {
            "dependency": dependency,
            "public_code": public_code,
            "retryable": retryable,
            "mutation_state": mutation_state,
            "redacted_message": redacted_message,
        },
        "remote_receipt_recovered": False,
    }
    value["payload"] = {
        "kind": "typed_failure/v1",
        "last_safe_artifact_sha256": H,
        "operator_action": {
            "none": "retry_same_key",
            "staged": "recover_receipt",
            "committed": "recover_receipt",
            "ambiguous": "manual_reconcile",
        }[mutation_state],
    }
    return value


def recovered_success_evidence(phase: str) -> dict[str, Any]:
    value = PHASE_FIXTURES[phase]()
    value["idempotency"]["result"] = "recovered_immutable_receipt"
    value["outcome"]["remote_receipt_recovered"] = True
    return value


def valid_control_request(operation: str) -> dict[str, Any]:
    phase_request, _capabilities, _policy = worker_protocol_fixtures.make_request()
    contract = CONTROL_CONTRACTS[operation]
    return {
        "schema": "simworld-semantic-index-control-request/v1",
        "protocol": "simworld-semantic-index-worker/v1",
        "operation": operation,
        "operation_revision": contract["revision"],
        "request_id": f"control-request-{operation}",
        "job_revision": phase_request["job_revision"],
        "reviewed_job_sha256": phase_request["reviewed_job_sha256"],
        "approval_basis_sha256": phase_request["approval_basis_sha256"],
        "execution_plan_sha256": phase_request["execution_plan_sha256"],
        "launcher_handoff_receipt_sha256": phase_request[
            "launcher_handoff_receipt_sha256"
        ],
        "generation_id": phase_request["generation_id"],
        "generation_binding_sha256": phase_request["generation_binding_sha256"],
        "execution_binding": copy.deepcopy(phase_request["execution_binding"]),
        "worker_binding": copy.deepcopy(phase_request["worker_binding"]),
        "target_phase": phase_request["phase"],
        "phase_request_sha256": hashlib.sha256(
            worker_protocol.canonical_json_bytes(phase_request)
        ).hexdigest(),
        "phase_idempotency_key": phase_request["idempotency_key"],
        "control_idempotency_key": REV2,
        "idempotency_ledger_identity": phase_request[
            "idempotency_ledger_identity"
        ],
        "idempotency_ledger_revision": phase_request[
            "idempotency_ledger_revision"
        ],
        "credential_transport": {
            "kind": "sealed_memfd_scm_rights_v1",
            "json_contains_credential_bytes": False,
            "descriptors": [
                {
                    "fd_index": 0,
                    "scope": contract["scope"],
                    "credential_generation": REV,
                    "generation_id": phase_request["generation_id"],
                    "target_identity": phase_request["worker_binding"]["deployment_identity"],
                    "byte_count": 64,
                }
            ],
        },
        "deadline_monotonic_ns": phase_request["deadline_monotonic_ns"],
    }


def control_request_binding(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_sha256": hashlib.sha256(worker_protocol.canonical_json_bytes(request)).hexdigest(),
        "request_id": request["request_id"],
        "operation": request["operation"],
        "target_phase": request["target_phase"],
        "phase_request_sha256": request["phase_request_sha256"],
        "phase_idempotency_key": request["phase_idempotency_key"],
        "control_idempotency_key": request["control_idempotency_key"],
        "idempotency_ledger_identity": request["idempotency_ledger_identity"],
        "idempotency_ledger_revision": request["idempotency_ledger_revision"],
        "job_revision": request["job_revision"],
        "reviewed_job_sha256": request["reviewed_job_sha256"],
        "approval_basis_sha256": request["approval_basis_sha256"],
        "execution_plan_sha256": request["execution_plan_sha256"],
        "launcher_handoff_receipt_sha256": request[
            "launcher_handoff_receipt_sha256"
        ],
        "generation_id": request["generation_id"],
        "generation_binding_sha256": request["generation_binding_sha256"],
        "host_boot_id_sha256": request["worker_binding"]["host_boot_id_sha256"],
        "deadline_monotonic_ns": request["deadline_monotonic_ns"],
    }


def valid_control_result(
    operation: str,
    *,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if request is None:
        request = valid_control_request(operation)
    return {
        "schema": "simworld-semantic-index-control-result/v1",
        "protocol": "simworld-semantic-index-worker/v1",
        "request_binding": control_request_binding(request),
        "status": "succeeded",
        "mutation_state": CONTROL_CONTRACTS[operation]["success_mutation_state"],
        "receipt": {
            "schema": "simworld-semantic-index-control-receipt/v1",
            "operation": operation,
            "generation_id": request["generation_id"],
            "target_phase": request["target_phase"],
            "phase_request_sha256": request["phase_request_sha256"],
            "phase_idempotency_key": request["phase_idempotency_key"],
            "control_idempotency_key": request["control_idempotency_key"],
            "observed_phase_state": "none",
            "immutable_phase_receipt_sha256": H if operation == "recover_phase_receipt" else None,
            "cancelled": operation == "cancel_phase_work",
            "quarantined": operation == "quarantine_generation",
            "issued_at": NOW,
            "expires_at": CONTROL_LATER,
        },
        "error": None,
    }


def failed_control_result(
    operation: str,
    *,
    mutation_state: str = "none",
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if request is None:
        request = valid_control_request(operation)
    return {
        "schema": "simworld-semantic-index-control-result/v1",
        "protocol": "simworld-semantic-index-worker/v1",
        "request_binding": control_request_binding(request),
        "status": "failed",
        "mutation_state": mutation_state,
        "receipt": None,
        "error": {
            "dependency": "worker",
            "code": "WORKER_BUSY",
            "retryable": mutation_state == "none",
            "mutation_state": mutation_state,
        },
    }


class SemanticIndexProductionSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {
            key: json.loads(
                (TOOLS_DIR / filename).read_text(encoding="utf-8"),
                object_pairs_hook=strict_json_object,
            )
            for key, filename in SCHEMA_FILES.items()
        }
        cls.registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in cls.schemas.values()
        )
        cls.validators = {
            key: Draft202012Validator(schema, registry=cls.registry)
            for key, schema in cls.schemas.items()
        }

    def assert_valid(self, schema: str, value: dict[str, Any]) -> None:
        errors = sorted(self.validators[schema].iter_errors(value), key=lambda error: list(error.path))
        self.assertEqual([], errors, "\n".join(error.message for error in errors[:10]))

    def assert_invalid(self, schema: str, value: dict[str, Any]) -> None:
        self.assertTrue(list(self.validators[schema].iter_errors(value)))

    def test_schemas_are_valid_closed_and_bounded_draft_2020_12(self) -> None:
        for name, schema in self.schemas.items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)
                self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
                self.assertFalse(schema["additionalProperties"])

                def walk(node: Any) -> None:
                    if isinstance(node, dict):
                        if node.get("type") == "object":
                            self.assertIs(node.get("additionalProperties"), False)
                        if node.get("type") == "array":
                            self.assertTrue("maxItems" in node or node.get("items") is False)
                        for child in node.values():
                            walk(child)
                    elif isinstance(node, list):
                        for child in node:
                            walk(child)

                walk(schema)

    def test_valid_instances(self) -> None:
        self.assert_valid("job", valid_job())
        self.assert_valid("plan", valid_plan())
        self.assert_valid("basis", valid_basis())
        for kind in (
            "data_owner_review",
            "cost_owner",
            "admin_state_change",
            "deployment",
            "runtime",
            "rollback_readiness",
        ):
            with self.subTest(approval_kind=kind):
                self.assert_valid("approval", valid_approval(kind))
        self.assert_valid("approval", valid_existing_generation_rollback_approval())
        for phase in PHASE_FIXTURES:
            with self.subTest(worker_phase=phase):
                self.assert_valid("worker_root", valid_worker_root(phase))
            with self.subTest(evidence_phase=phase):
                self.assert_valid("evidence", PHASE_FIXTURES[phase]())
        for operation in CONTROL_CONTRACTS:
            with self.subTest(control_operation=operation):
                self.assert_valid("control_request", valid_control_request(operation))
                self.assert_valid("control_result", valid_control_result(operation))

    def test_shared_sealed_production_bundle_passes_formal_and_semantic_validation(
        self,
    ) -> None:
        bundle = production_fixtures.make_bundle()
        self.assert_valid("job", production_fixtures.unseal(bundle.reviewed_job))
        self.assert_valid("plan", production_fixtures.unseal(bundle.execution_plan))
        self.assert_valid("basis", production_fixtures.unseal(bundle.approval_basis))

        self.assertEqual(set(production_fixtures.APPROVAL_KINDS), set(bundle.approvals))
        for kind, document in bundle.approvals.items():
            with self.subTest(approval_kind=kind):
                self.assert_valid("approval", production_fixtures.unseal(document))

        self.assertEqual(len(production_fixtures.PHASES), len(bundle.phase_evidence))
        for document in bundle.phase_evidence:
            evidence = production_fixtures.unseal(document)
            with self.subTest(evidence_phase=evidence["phase"]):
                self.assert_valid("evidence", evidence)

        receipt = production_fixtures.validate_production_contracts(
            bundle,
            now_utc=production_fixtures.NOW,
        )
        self.assertEqual(
            len(production_fixtures.PHASES),
            len(receipt.evidence_sha256_by_phase),
        )

    def test_shared_bundle_unknown_top_level_field_fails_formal_gate(self) -> None:
        bundle = production_fixtures.make_bundle()
        plan = production_fixtures.unseal(bundle.execution_plan)
        plan["unknown_top_level"] = True
        tampered = dataclasses.replace(
            bundle,
            execution_plan=production_fixtures.seal(plan),
        )

        with self.assertRaises(production_fixtures.ContractValidationError) as caught:
            production_fixtures.validate_production_contracts(
                tampered,
                now_utc=production_fixtures.NOW,
            )
        self.assertEqual("CONTRACT_SCHEMA_INSTANCE_INVALID", caught.exception.code)

    def test_unknown_fields_are_rejected(self) -> None:
        fixtures = {
            "job": valid_job(),
            "plan": valid_plan(),
            "basis": valid_basis(),
            "approval": valid_approval("cost_owner"),
            "control_request": valid_control_request("query_phase_status"),
            "control_result": valid_control_result("query_phase_status"),
            "worker_root": valid_worker_root("inspect"),
            "evidence": valid_inspect_evidence(),
        }
        for name, fixture in fixtures.items():
            with self.subTest(schema=name):
                fixture["unknown"] = True
                self.assert_invalid(name, fixture)

    def test_plan_rejects_offline_or_unsealed_production_inputs(self) -> None:
        mutations = [
            ("profile", "offline_fixture"),
            ("approval_basis.sha256", "not-a-hash"),
            ("generation.reservation.mode", "shared"),
            ("generation.generation_id", REV),
            ("generation.generation_nonce", "1" * 64),
            ("worker.max_frame_bytes", 2097151),
            ("unreal.slot_id", 1),
            ("credentials.transport", "json_secret"),
            ("credentials.reconcile.ue_runtime_read.scope", "ue_smoke_read_only"),
        ]
        for pointer, replacement in mutations:
            with self.subTest(pointer=pointer):
                value = valid_plan()
                target: dict[str, Any] = value
                parts = pointer.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = replacement
                self.assert_invalid("plan", value)

        legacy_basis = valid_plan()
        legacy_basis["approval_basis"]["includes"] = ["approvals"]
        self.assert_invalid("plan", legacy_basis)

        half_null_previous = valid_plan()
        half_null_previous["generation"]["previous_active"] = {
            "generation_id": GEN,
            "snapshot_revision": None,
        }
        self.assert_invalid("plan", half_null_previous)

        existing_previous = valid_plan()
        existing_previous["generation"]["previous_active"] = {
            "generation_id": f"semantic-generation:{H2}",
            "snapshot_revision": "asset-snapshot-20260720-active",
        }
        self.assert_valid("plan", existing_previous)

        missing_key_binding = valid_plan()
        del missing_key_binding["approvals"]["cost_owner"]["issuer_key_id"]
        self.assert_invalid("plan", missing_key_binding)

        missing_algorithm_binding = valid_plan()
        del missing_algorithm_binding["approvals"]["cost_owner"]["signature_algorithm"]
        self.assert_invalid("plan", missing_algorithm_binding)

    def test_approval_basis_is_independent_and_generation_acyclic(self) -> None:
        self.assert_valid("basis", valid_basis())

        forbidden_insertions = (
            ((), "sha256", H),
            ((), "approval_receipts", []),
            (("generation_inputs",), "generation_id", GEN),
            (("generation_inputs", "target_generation_identities", "postgres"), "database", PG_DATABASE),
            (
                ("generation_inputs", "target_generation_identities", "qdrant"),
                "collection",
                QDRANT_COLLECTION,
            ),
        )
        for pointer, key, replacement in forbidden_insertions:
            with self.subTest(forbidden=key):
                value = valid_basis()
                target: dict[str, Any] = value
                for part in pointer:
                    target = target[part]
                target[key] = replacement
                self.assert_invalid("basis", value)

        bad_nonce = valid_basis()
        bad_nonce["generation_inputs"]["generation_nonce"] = "1" * 64
        self.assert_invalid("basis", bad_nonce)

        wrong_scope = valid_basis()
        wrong_scope["credential_targets"]["reconcile"]["ue_runtime_read"]["scope"] = (
            "ue_disposable_scene_inspect_spawn_cleanup"
        )
        self.assert_invalid("basis", wrong_scope)

    def test_plan_and_basis_reject_scope_substitution_for_every_capability(self) -> None:
        capability_paths = (
            ("ordinary_phases", "inspect"),
            ("ordinary_phases", "render"),
            ("ordinary_phases", "caption"),
            ("ordinary_phases", "embed"),
            ("ordinary_phases", "postgres"),
            ("ordinary_phases", "qdrant"),
            ("reconcile", "postgres_read"),
            ("reconcile", "qdrant_read"),
            ("reconcile", "ue_runtime_read"),
            ("control_operations", "query_phase_status"),
            ("control_operations", "recover_phase_receipt"),
            ("control_operations", "cancel_phase_work"),
            ("control_operations", "quarantine_generation"),
        )
        for section, capability_name in capability_paths:
            with self.subTest(plan_capability=f"{section}.{capability_name}"):
                plan = valid_plan()
                plan["credentials"][section][capability_name]["scope"] = "substituted_scope"
                self.assert_invalid("plan", plan)

            with self.subTest(basis_capability=f"{section}.{capability_name}"):
                basis = valid_basis()
                basis["credential_targets"][section][capability_name]["scope"] = "substituted_scope"
                self.assert_invalid("basis", basis)

    def test_worker_phase_and_control_operations_are_exact_and_separately_authorized(self) -> None:
        plan = valid_plan()
        self.assertEqual(
            [
                "inspect_exact_assets",
                "render_exact_views",
                "caption_exact_render_set",
                "embed_exact_text_set",
                "upsert_postgres_exact",
                "upsert_qdrant_exact",
                "reconcile_exact_snapshot",
            ],
            plan["worker"]["phase_operations"],
        )
        self.assertEqual(
            [
                "query_phase_status",
                "recover_phase_receipt",
                "cancel_phase_work",
                "quarantine_generation",
            ],
            plan["worker"]["control_operations"],
        )

        legacy = valid_plan()
        legacy["worker"]["operations"] = legacy["worker"].pop("phase_operations")
        self.assert_invalid("plan", legacy)

        wrong_phase_order = valid_plan()
        wrong_phase_order["worker"]["phase_operations"].reverse()
        self.assert_invalid("plan", wrong_phase_order)

        wrong_control_order = valid_plan()
        wrong_control_order["worker"]["control_operations"].reverse()
        self.assert_invalid("plan", wrong_control_order)

        missing_control_capability = valid_plan()
        del missing_control_capability["credentials"]["control_operations"]["quarantine_generation"]
        self.assert_invalid("plan", missing_control_capability)

        missing_basis_control = valid_basis()
        del missing_basis_control["credential_targets"]["control_operations"]["query_phase_status"]
        self.assert_invalid("basis", missing_basis_control)

    def test_plan_exposes_complete_worker_and_target_attestation_projection(self) -> None:
        plan = valid_plan()
        phase_targets = {
            "inspect": [("unreal", "ordinary_phases", "inspect")],
            "render": [("unreal", "ordinary_phases", "render")],
            "caption": [("caption", "ordinary_phases", "caption")],
            "embed": [("embedding", "ordinary_phases", "embed")],
            "postgres": [("postgres", "ordinary_phases", "postgres")],
            "qdrant": [("qdrant", "ordinary_phases", "qdrant")],
            "reconcile": [
                ("postgres", "reconcile", "postgres_read"),
                ("qdrant", "reconcile", "qdrant_read"),
                ("unreal", "reconcile", "ue_runtime_read"),
            ],
        }
        for phase, projections in phase_targets.items():
            projected = []
            for component, section, credential_name in projections:
                credential = plan["credentials"][section][credential_name]
                attestation = plan["target_attestations"][component]
                projected.append(
                    {
                        "component": component,
                        "scope": credential["scope"],
                        "credential_generation": credential["credential_generation"],
                        "generation_id": credential["generation_id"],
                        "target_identity": credential["target_identity"],
                        **copy.deepcopy(attestation),
                    }
                )
            with self.subTest(phase=phase):
                self.assertEqual(valid_worker_root(phase)["observed_targets"], projected)

        root_worker = valid_worker_root("inspect")["worker_attestation"]
        self.assertEqual(plan["worker"]["peer_deployment_identity"], root_worker["deployment_identity"])
        self.assertEqual(plan["runtime_images"]["worker"], root_worker["runtime_image"])
        for key in (
            "whoami_sha256",
            "peer_attestation_sha256",
            "runtime_attestation_sha256",
            "host_boot_id_sha256",
            "process_start_token_sha256",
            "socket_inode_binding_sha256",
        ):
            self.assertEqual(plan["worker"][key], root_worker[key])

        for missing in ("whoami_sha256", "peer_attestation_sha256", "runtime_attestation_sha256"):
            with self.subTest(missing_worker_attestation=missing):
                value = valid_plan()
                del value["worker"][missing]
                self.assert_invalid("plan", value)

        missing_target = valid_plan()
        del missing_target["target_attestations"]["postgres"]
        self.assert_invalid("plan", missing_target)

        caption_with_image = valid_plan()
        caption_with_image["target_attestations"]["caption"]["runtime_image"] = IMAGE
        self.assert_invalid("plan", caption_with_image)

        unreal_without_image = valid_plan()
        unreal_without_image["target_attestations"]["unreal"]["runtime_image"] = None
        self.assert_invalid("plan", unreal_without_image)

        missing_basis_commitment = valid_basis()
        del missing_basis_commitment["commitments"]["target_attestations_sha256"]
        self.assert_invalid("basis", missing_basis_commitment)

    def test_approval_rejects_bad_signature_or_expiry_shape(self) -> None:
        bad_signature = valid_approval("cost_owner")
        bad_signature["signature"]["detached_signature_base64"] = "short"
        self.assert_invalid("approval", bad_signature)

        bad_expiry = valid_approval("cost_owner")
        bad_expiry["expires_at"] = "never"
        self.assert_invalid("approval", bad_expiry)

    def test_all_production_approval_kinds_are_bound_to_their_branch(self) -> None:
        for kind in (
            "cost_owner",
            "admin_state_change",
            "deployment",
            "runtime",
            "rollback_readiness",
        ):
            with self.subTest(kind=kind):
                value = valid_approval(kind)
                value["authorization"] = copy.deepcopy(valid_approval("cost_owner")["authorization"])
                if kind == "cost_owner":
                    value["authorization"] = copy.deepcopy(
                        valid_approval("admin_state_change")["authorization"]
                    )
                self.assert_invalid("approval", value)

                wrong_role = valid_approval(kind)
                wrong_role["issuer"]["role"] = "data_owner"
                self.assert_invalid("approval", wrong_role)

        missing_control_authority = valid_approval("admin_state_change")
        del missing_control_authority["authorization"]["allowed_control_operations"]
        self.assert_invalid("approval", missing_control_authority)

        reordered_control_authority = valid_approval("admin_state_change")
        reordered_control_authority["authorization"]["allowed_control_operations"].reverse()
        self.assert_invalid("approval", reordered_control_authority)

    def test_rollback_approval_bootstrap_and_existing_generation_are_exclusive(self) -> None:
        self.assert_valid("approval", valid_approval("rollback_readiness"))
        self.assert_valid("approval", valid_existing_generation_rollback_approval())

        half_null = valid_approval("rollback_readiness")
        half_null["authorization"]["previous_generation_id"] = GEN
        self.assert_invalid("approval", half_null)

        false_bootstrap_without_previous = valid_approval("rollback_readiness")
        false_bootstrap_without_previous["authorization"]["bootstrap"] = False
        self.assert_invalid("approval", false_bootstrap_without_previous)

        existing_with_epoch_zero = valid_existing_generation_rollback_approval()
        existing_with_epoch_zero["authorization"]["expected_active_generation_epoch"] = 0
        self.assert_invalid("approval", existing_with_epoch_zero)

    def test_worker_artifact_roots_bind_each_phase_operation_artifact_and_targets(self) -> None:
        phases = list(PHASE_CONTRACTS)
        for index, phase in enumerate(phases):
            with self.subTest(phase=phase):
                self.assert_valid("worker_root", valid_worker_root(phase))

                wrong_operation = valid_worker_root(phase)
                wrong_operation["operation"] = PHASE_CONTRACTS[phases[(index + 1) % len(phases)]][
                    "operation"
                ]
                self.assert_invalid("worker_root", wrong_operation)

                wrong_artifact = valid_worker_root(phase)
                wrong_artifact["artifact_root"]["schema"] = PHASE_CONTRACTS[
                    phases[(index + 1) % len(phases)]
                ]["artifact_schema"]
                self.assert_invalid("worker_root", wrong_artifact)

                wrong_scope = valid_worker_root(phase)
                wrong_scope["observed_targets"][0]["scope"] = "unrelated_scope"
                self.assert_invalid("worker_root", wrong_scope)

                wrong_component = valid_worker_root(phase)
                wrong_component["observed_targets"][0]["component"] = "caption"
                if phase == "caption":
                    wrong_component["observed_targets"][0]["component"] = "unreal"
                self.assert_invalid("worker_root", wrong_component)

        missing_reconcile_target = valid_worker_root("reconcile")
        missing_reconcile_target["observed_targets"].pop()
        self.assert_invalid("worker_root", missing_reconcile_target)

        caption_with_image = valid_worker_root("caption")
        caption_with_image["observed_targets"][0]["runtime_image"] = IMAGE
        self.assert_invalid("worker_root", caption_with_image)

    def test_ledger_identity_revision_are_required_across_wire_and_evidence(self) -> None:
        request, _capabilities, _policy = worker_protocol_fixtures.make_request()
        response = worker_protocol_fixtures.make_success_response(request)
        control_request = valid_control_request("query_phase_status")
        control_result = valid_control_result(
            "query_phase_status",
            request=control_request,
        )
        evidence = valid_inspect_evidence()
        worker_root = valid_worker_root("inspect")

        self.assert_valid("request", request)
        self.assert_valid("result", response)
        self.assert_valid("control_request", control_request)
        self.assert_valid("control_result", control_result)
        self.assert_valid("evidence", evidence)
        self.assert_valid("worker_root", worker_root)

        for field in ("idempotency_ledger_identity", "idempotency_ledger_revision"):
            missing_request = copy.deepcopy(request)
            missing_request.pop(field)
            with self.subTest(kind="request", field=field):
                self.assert_invalid("request", missing_request)

            missing_result = copy.deepcopy(response)
            missing_result["request_binding"].pop(field)
            with self.subTest(kind="result_binding", field=field):
                self.assert_invalid("result", missing_result)

            missing_root = copy.deepcopy(worker_root)
            missing_root.pop(field)
            with self.subTest(kind="worker_root", field=field):
                self.assert_invalid("worker_root", missing_root)

            missing_control_request = copy.deepcopy(control_request)
            missing_control_request.pop(field)
            with self.subTest(kind="control_request", field=field):
                self.assert_invalid("control_request", missing_control_request)

            missing_control_result = copy.deepcopy(control_result)
            missing_control_result["request_binding"].pop(field)
            with self.subTest(kind="control_result_binding", field=field):
                self.assert_invalid("control_result", missing_control_result)

        for field in ("ledger_identity", "ledger_revision"):
            missing_evidence = copy.deepcopy(evidence)
            missing_evidence["idempotency"].pop(field)
            with self.subTest(kind="evidence", field=field):
                self.assert_invalid("evidence", missing_evidence)

    def test_ordinary_wire_schemas_interoperate_with_protocol_fixtures(self) -> None:
        request_schema = self.schemas["request"]
        result_schema = self.schemas["result"]
        self.assertEqual(set(request_schema["required"]), set(request_schema["properties"]))
        self.assertEqual(set(request_schema["required"]), worker_protocol._REQUEST_KEYS)
        self.assertEqual(set(result_schema["required"]), set(result_schema["properties"]))
        self.assertEqual(set(result_schema["required"]), worker_protocol._RESULT_KEYS)
        self.assertEqual(
            set(request_schema["$defs"]["executionBinding"]["required"]),
            worker_protocol._EXECUTION_BINDING_KEYS,
        )
        self.assertEqual(
            set(request_schema["$defs"]["workerBinding"]["required"]),
            worker_protocol._WORKER_BINDING_KEYS,
        )
        self.assertEqual(
            set(request_schema["$defs"]["inputBinding"]["required"]),
            worker_protocol._INPUT_BINDING_KEYS,
        )
        self.assertEqual(
            set(request_schema["$defs"]["credentialDescriptor"]["required"]),
            worker_protocol._CREDENTIAL_DESCRIPTOR_KEYS,
        )
        self.assertEqual(
            set(request_schema["$defs"]["targetObservation"]["required"]),
            worker_protocol._TARGET_KEYS,
        )
        self.assertEqual(
            set(result_schema["$defs"]["requestBinding"]["required"]),
            worker_protocol._REQUEST_BINDING_KEYS,
        )

        contract_names = {
            "inspect_exact_assets": "inspectContract",
            "render_exact_views": "renderContract",
            "caption_exact_render_set": "captionContract",
            "embed_exact_text_set": "embedContract",
            "upsert_postgres_exact": "postgresContract",
            "upsert_qdrant_exact": "qdrantContract",
            "reconcile_exact_snapshot": "reconcileContract",
        }
        for operation in worker_protocol_fixtures.OPERATIONS:
            with self.subTest(operation=operation):
                request, _capabilities, _policy = worker_protocol_fixtures.make_request(
                    operation=operation
                )
                self.assert_valid("request", request)
                worker_protocol.validate_request(request)

                schema_revision = request_schema["$defs"][contract_names[operation]]["then"][
                    "properties"
                ]["operation_revision"]["const"]
                self.assertEqual(worker_protocol.operation_contract(operation)["revision"], schema_revision)

                success = worker_protocol_fixtures.make_success_response(request)
                self.assert_valid("result", success)
                worker_protocol.validate_response(
                    success,
                    request,
                    now_utc=worker_protocol_fixtures.FIXED_NOW,
                )

                failure = worker_protocol_fixtures.make_failed_response(request)
                self.assert_valid("result", failure)
                worker_protocol.validate_response(failure, request)

                wrong_revision = copy.deepcopy(request)
                wrong_revision["operation_revision"] = REV
                self.assert_invalid("request", wrong_revision)

                wrong_metric = copy.deepcopy(request)
                wrong_metric["expected_metrics"] = {"qdrant_points": 2}
                if operation == "upsert_qdrant_exact":
                    wrong_metric["expected_metrics"] = {"assets_inspected": 2}
                self.assert_invalid("request", wrong_metric)

                peer_text = copy.deepcopy(success)
                peer_text["message"] = "peer controlled"
                self.assert_invalid("result", peer_text)

    def test_control_wire_schemas_are_closed_phase_bound_and_recovery_safe(self) -> None:
        request_schema = self.schemas["control_request"]
        result_schema = self.schemas["control_result"]
        self.assertEqual(set(request_schema["required"]), set(request_schema["properties"]))
        self.assertEqual(set(request_schema["required"]), worker_protocol._CONTROL_REQUEST_KEYS)
        self.assertEqual(set(result_schema["required"]), set(result_schema["properties"]))
        self.assertEqual(set(result_schema["required"]), worker_protocol._CONTROL_RESULT_KEYS)
        self.assertEqual(
            set(request_schema["$defs"]["executionBinding"]["required"]),
            worker_protocol._EXECUTION_BINDING_KEYS,
        )
        self.assertEqual(
            set(request_schema["$defs"]["workerBinding"]["required"]),
            worker_protocol._WORKER_BINDING_KEYS,
        )
        self.assertEqual(
            set(request_schema["$defs"]["credentialTransport"]["required"]),
            worker_protocol._CREDENTIAL_TRANSPORT_KEYS,
        )
        self.assertEqual(
            set(request_schema["$defs"]["credentialDescriptor"]["required"]),
            worker_protocol._CREDENTIAL_DESCRIPTOR_KEYS,
        )
        self.assertEqual(
            set(result_schema["$defs"]["requestBinding"]["required"]),
            worker_protocol._CONTROL_REQUEST_BINDING_KEYS,
        )
        self.assertEqual(
            set(result_schema["$defs"]["controlReceipt"]["required"]),
            worker_protocol._CONTROL_RECEIPT_KEYS,
        )

        request_contracts = {
            "query_phase_status": "queryContract",
            "recover_phase_receipt": "recoverContract",
            "cancel_phase_work": "cancelContract",
            "quarantine_generation": "quarantineContract",
        }
        for operation, contract in CONTROL_CONTRACTS.items():
            with self.subTest(operation=operation):
                request = valid_control_request(operation)
                self.assert_valid("control_request", request)
                validated_request = worker_protocol.validate_control_request(
                    request,
                    now_monotonic_ns=request["deadline_monotonic_ns"] - 1,
                )
                self.assertEqual(request, validated_request)
                self.assertEqual(
                    control_request_binding(request),
                    worker_protocol.control_request_binding_for(request),
                )
                schema_revision = request_schema["$defs"][request_contracts[operation]]["then"][
                    "properties"
                ]["operation_revision"]["const"]
                self.assertEqual(contract["revision"], schema_revision)
                protocol_contract = worker_protocol._CONTROL_OPERATION_CONTRACTS[operation]
                self.assertEqual(contract["revision"], protocol_contract["revision"])
                self.assertEqual(contract["scope"], protocol_contract["scope"])
                self.assertEqual(
                    contract["success_mutation_state"],
                    protocol_contract["success_mutation_state"],
                )

                success = valid_control_result(operation, request=request)
                self.assert_valid("control_result", success)
                worker_protocol.validate_control_response(
                    success,
                    request,
                    now_utc=worker_protocol_fixtures.FIXED_NOW,
                )
                self.assertEqual(
                    success["request_binding"]["phase_request_sha256"],
                    success["receipt"]["phase_request_sha256"],
                )

                failure = failed_control_result(operation, request=request)
                self.assert_valid("control_result", failure)
                worker_protocol.validate_control_response(failure, request)

                wrong_revision = valid_control_request(operation)
                wrong_revision["operation_revision"] = REV
                self.assert_invalid("control_request", wrong_revision)

                wrong_scope = valid_control_request(operation)
                wrong_scope["credential_transport"]["descriptors"][0]["scope"] = (
                    "substituted_scope"
                )
                self.assert_invalid("control_request", wrong_scope)

                extra_capability = valid_control_request(operation)
                extra_capability["credential_transport"]["descriptors"].append(
                    copy.deepcopy(extra_capability["credential_transport"]["descriptors"][0])
                )
                self.assert_invalid("control_request", extra_capability)

        recover_without_receipt = valid_control_result("recover_phase_receipt")
        recover_without_receipt["receipt"]["immutable_phase_receipt_sha256"] = None
        self.assert_invalid("control_result", recover_without_receipt)

        cancel_without_flag = valid_control_result("cancel_phase_work")
        cancel_without_flag["receipt"]["cancelled"] = False
        self.assert_invalid("control_result", cancel_without_flag)

        quarantine_without_flag = valid_control_result("quarantine_generation")
        quarantine_without_flag["receipt"]["quarantined"] = False
        self.assert_invalid("control_result", quarantine_without_flag)

        query_committed_failure = failed_control_result(
            "query_phase_status", mutation_state="committed"
        )
        self.assert_invalid("control_result", query_committed_failure)

        for operation in ("cancel_phase_work", "quarantine_generation"):
            for mutation_state in ("none", "committed", "ambiguous"):
                with self.subTest(operation=operation, failure_state=mutation_state):
                    request = valid_control_request(operation)
                    failure = failed_control_result(
                        operation,
                        mutation_state=mutation_state,
                        request=request,
                    )
                    self.assert_valid("control_result", failure)
                    worker_protocol.validate_control_response(failure, request)

        unsafe_retry = failed_control_result("cancel_phase_work", mutation_state="committed")
        unsafe_retry["error"]["retryable"] = True
        self.assert_invalid("control_result", unsafe_retry)

    def test_evidence_rejects_wrong_payload_or_prior_chain(self) -> None:
        for phase, fixture_factory in PHASE_FIXTURES.items():
            with self.subTest(wrong_payload_phase=phase):
                wrong_payload = fixture_factory()
                wrong_payload["payload"] = {
                    "kind": "typed_failure/v1",
                    "last_safe_artifact_sha256": H,
                    "operator_action": "manual_reconcile",
                }
                self.assert_invalid("evidence", wrong_payload)

            with self.subTest(wrong_chain_phase=phase):
                wrong_chain = fixture_factory()
                if phase == "inspect":
                    wrong_chain["prior_receipts"] = [prior_receipt("inspect")]
                else:
                    wrong_chain["prior_receipts"].pop()
                self.assert_invalid("evidence", wrong_chain)

        wrong_scope = valid_inspect_evidence()
        wrong_scope["credentials_observed"][0]["scope"] = "ue_read_only"
        self.assert_invalid("evidence", wrong_scope)

    def test_evidence_accepts_typed_failures_and_immutable_receipt_recovery_for_all_phases(self) -> None:
        terminal_mutation = {
            "inspect": "ambiguous",
            "render": "staged",
            "caption": "committed",
            "embed": "staged",
            "postgres": "committed",
            "qdrant": "committed",
        }
        for phase in PHASE_FIXTURES:
            with self.subTest(retryable_failure_phase=phase):
                self.assert_valid("evidence", failed_evidence(phase))
            if phase in terminal_mutation:
                with self.subTest(nonempty_mutation_failure_phase=phase):
                    self.assert_valid(
                        "evidence",
                        failed_evidence(phase, mutation_state=terminal_mutation[phase]),
                    )
            with self.subTest(recovered_success_phase=phase):
                self.assert_valid("evidence", recovered_success_evidence(phase))

    def test_phase_mutation_state_matrix_is_exact(self) -> None:
        expected_plan_states = {
            "inspect": ["none", "ambiguous"],
            "render": ["none", "staged", "ambiguous"],
            "caption": ["none", "committed", "ambiguous"],
            "embed": ["none", "staged", "ambiguous"],
            "postgres": ["none", "committed", "ambiguous"],
            "qdrant": ["none", "staged", "committed", "ambiguous"],
            "reconcile": ["none"],
        }
        plan = valid_plan()
        for phase, states in expected_plan_states.items():
            self.assertEqual(states, plan["phase_policy"][phase]["allowed_mutation_states"])

            with self.subTest(plan_phase=phase):
                changed = valid_plan()
                changed["phase_policy"][phase]["allowed_mutation_states"] = list(reversed(states))
                if len(states) == 1:
                    changed["phase_policy"][phase]["allowed_mutation_states"] = ["none", "staged"]
                self.assert_invalid("plan", changed)

        allowed_failures = {
            "inspect": {"none", "ambiguous"},
            "render": {"none", "staged", "ambiguous"},
            "caption": {"none", "committed", "ambiguous"},
            "embed": {"none", "staged", "ambiguous"},
            "postgres": {"none", "committed", "ambiguous"},
            "qdrant": {"none", "staged", "committed", "ambiguous"},
            "reconcile": {"none"},
        }
        all_states = {"none", "staged", "committed", "ambiguous"}
        for phase, allowed in allowed_failures.items():
            for mutation_state in all_states:
                with self.subTest(evidence_phase=phase, mutation_state=mutation_state):
                    value = failed_evidence(phase, mutation_state=mutation_state)
                    if mutation_state in allowed:
                        self.assert_valid("evidence", value)
                    else:
                        self.assert_invalid("evidence", value)

    def test_failure_outcome_consistency_and_redaction_are_enforced(self) -> None:
        retry_mismatch = failed_evidence("caption")
        retry_mismatch["outcome"]["error"]["retryable"] = False
        self.assert_invalid("evidence", retry_mismatch)

        mutation_mismatch = failed_evidence("postgres", mutation_state="committed")
        mutation_mismatch["outcome"]["error"]["mutation_state"] = "none"
        self.assert_invalid("evidence", mutation_mismatch)

        unsafe_retry = failed_evidence("caption", mutation_state="committed")
        unsafe_retry["outcome"]["retryable"] = True
        unsafe_retry["outcome"]["error"]["retryable"] = True
        self.assert_invalid("evidence", unsafe_retry)

        staged_same_key_recovery = failed_evidence("qdrant", mutation_state="staged")
        self.assert_valid("evidence", staged_same_key_recovery)

        staged_marked_retryable = failed_evidence("qdrant", mutation_state="staged")
        staged_marked_retryable["outcome"]["retryable"] = True
        staged_marked_retryable["outcome"]["error"]["retryable"] = True
        self.assert_invalid("evidence", staged_marked_retryable)

        impossible_reconcile_state = failed_evidence("reconcile")
        impossible_reconcile_state["outcome"]["mutation_state"] = "ambiguous"
        impossible_reconcile_state["outcome"]["retryable"] = False
        impossible_reconcile_state["outcome"]["error"]["mutation_state"] = "ambiguous"
        impossible_reconcile_state["outcome"]["error"]["retryable"] = False
        self.assert_invalid("evidence", impossible_reconcile_state)

        for message in (
            "TOKEN leaked",
            "read path /home/operator/file",
            "line one\nline two",
            "api_key exposed",
        ):
            with self.subTest(redacted_message=message):
                value = failed_evidence("embed")
                value["outcome"]["error"]["redacted_message"] = message
                self.assert_invalid("evidence", value)

    def test_public_errors_are_exactly_the_protocol_fixed_message_allowlist(self) -> None:
        branches = self.schemas["evidence"]["$defs"]["publicErrorIdentity"]["oneOf"]
        actual = {
            (
                branch["properties"]["dependency"]["const"],
                branch["properties"]["public_code"]["const"],
            ): branch["properties"]["redacted_message"]["const"]
            for branch in branches
        }
        self.assertEqual(PUBLIC_ERROR_MESSAGES, actual)

        mismatched_fixed_message = failed_evidence("inspect")
        mismatched_fixed_message["outcome"]["error"]["public_code"] = "UNREAL_OPERATION_FAILED"
        self.assert_invalid("evidence", mismatched_fixed_message)

        unknown_safe_code = failed_evidence("inspect")
        unknown_safe_code["outcome"]["error"]["public_code"] = "UNREAL_UNKNOWN"
        self.assert_invalid("evidence", unknown_safe_code)

    def test_phase_target_shapes_and_reconcile_read_only_capabilities_are_enforced(self) -> None:
        for phase, fixture_factory in PHASE_FIXTURES.items():
            with self.subTest(phase=phase):
                value = fixture_factory()
                value["target_attestation"]["targets"][0]["component"] = "caption"
                if phase == "caption":
                    value["target_attestation"]["targets"][0]["component"] = "unreal"
                self.assert_invalid("evidence", value)

        caption_image = caption_from_render()
        caption_image["target_attestation"]["targets"][0]["runtime_image"] = IMAGE
        self.assert_invalid("evidence", caption_image)

        missing_reconcile_target = reconcile_from_qdrant()
        missing_reconcile_target["target_attestation"]["targets"].pop()
        self.assert_invalid("evidence", missing_reconcile_target)

        legacy_reconcile_scope = reconcile_from_qdrant()
        legacy_reconcile_scope["credentials_observed"][2]["scope"] = "ue_smoke_read_only"
        self.assert_invalid("evidence", legacy_reconcile_scope)

    def test_reconcile_requires_compatibility_snapshot_and_coherent_previous_active_shape(self) -> None:
        missing_compatibility = reconcile_from_qdrant()
        missing_compatibility["payload"]["compatibility_snapshot_v1_sha256"] = None
        self.assert_invalid("evidence", missing_compatibility)

        half_null_previous = reconcile_from_qdrant()
        half_null_previous["payload"]["previous_active"] = {
            "generation_id": GEN,
            "snapshot_revision": None,
            "ready": True,
        }
        self.assert_invalid("evidence", half_null_previous)

        existing_previous = reconcile_from_qdrant()
        existing_previous["payload"]["previous_active"] = {
            "generation_id": f"semantic-generation:{H2}",
            "snapshot_revision": "asset-snapshot-20260720-active",
            "ready": True,
        }
        self.assert_valid("evidence", existing_previous)


if __name__ == "__main__":
    unittest.main()
