"""Shared formally-valid and semantically-linked Production fixtures."""

from __future__ import annotations

import base64
import copy
import dataclasses
import datetime as dt
import hashlib
import sys
from pathlib import Path
from typing import Any, Callable


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from semantic_index_production_contracts import (  # noqa: E402
    ContractValidationError as ContractValidationError,
    ProductionContractBundle,
    SealedDocument,
    canonical_jcs_bytes,
    canonical_sha256,
    decode_canonical_jcs,
    derive_expected_approval_basis,
    derive_generation_id,
    derive_target_generation_identities,
    sorted_id_set_sha256,
    validate_production_contracts as validate_production_contracts,
    worker_operation_revision,
)


NOW = dt.datetime(2026, 7, 21, 12, 0, tzinfo=dt.timezone.utc)
ISSUED = "2026-07-21T09:00:00Z"
EXPIRES = "2026-07-21T13:00:00Z"
SNAPSHOT = "asset-snapshot-20260721-reviewed"
SERIALIZATION = {
    "encoding": "utf-8",
    "canonicalization": "rfc8785-jcs-v1",
    "duplicate_keys": "reject",
    "unicode_normalization": "require_already_nfc",
    "numbers": "integers_only",
    "nonfinite_numbers": "reject",
    "trailing_newline": False,
}
PHASES = ("inspect", "render", "caption", "embed", "postgres", "qdrant", "reconcile")
APPROVAL_KINDS = (
    "data_owner_review",
    "cost_owner",
    "admin_state_change",
    "deployment",
    "runtime",
    "rollback_readiness",
)
CONTROL_SCOPES = {
    "query_phase_status": "semantic_index_phase_status_read",
    "recover_phase_receipt": "semantic_index_phase_receipt_recover",
    "cancel_phase_work": "semantic_index_phase_work_cancel",
    "quarantine_generation": "semantic_index_generation_quarantine",
}
OPERATIONS = {
    "inspect": "inspect_exact_assets",
    "render": "render_exact_views",
    "caption": "caption_exact_render_set",
    "embed": "embed_exact_text_set",
    "postgres": "upsert_postgres_exact",
    "qdrant": "upsert_qdrant_exact",
    "reconcile": "reconcile_exact_snapshot",
}
SUCCESS_STATES = {
    "inspect": "none",
    "render": "staged",
    "caption": "committed",
    "embed": "staged",
    "postgres": "committed",
    "qdrant": "committed",
    "reconcile": "none",
}
FORMAL_SCHEMA_FILES = {
    "reviewed_job_schema_sha256": "semantic_asset_index_job_v2_schema.json",
    "execution_plan_schema_sha256": "semantic_asset_index_execution_plan_v2_schema.json",
    "approval_basis_schema_sha256": "semantic_index_production_approval_basis_schema.json",
    "approval_receipt_schema_sha256": "semantic_index_approval_receipt_schema.json",
    "launcher_verification_receipt_schema_sha256": "semantic_index_launcher_verification_receipt_schema.json",
    "phase_evidence_schema_sha256": "semantic_index_phase_evidence_schema.json",
    "phase_request_schema_sha256": "semantic_index_phase_request_schema.json",
    "worker_result_schema_sha256": "semantic_index_worker_result_schema.json",
    "control_request_schema_sha256": "semantic_index_control_request_schema.json",
    "control_result_schema_sha256": "semantic_index_control_result_schema.json",
    "worker_artifact_root_schema_sha256": "semantic_index_worker_artifact_root_schema.json",
    "state_schema_sha256": "semantic_asset_index_execution_state_v2_schema.json",
    "terminal_receipt_schema_sha256": "semantic_asset_index_terminal_receipt_v2_schema.json",
    "adapter_contract_sha256": "semantic_index_production_adapter_v2_schema.json",
    "inspect_artifact_schema_sha256": "semantic_index_inspect_artifact_v1_schema.json",
    "render_artifact_schema_sha256": "semantic_index_render_artifact_v1_schema.json",
    "caption_artifact_schema_sha256": "semantic_index_caption_artifact_v1_schema.json",
    "embed_artifact_schema_sha256": "semantic_index_embed_artifact_v1_schema.json",
    "postgres_artifact_schema_sha256": "semantic_index_postgres_artifact_v1_schema.json",
    "qdrant_artifact_schema_sha256": "semantic_index_qdrant_artifact_v1_schema.json",
    "reconcile_artifact_schema_sha256": "semantic_index_reconcile_artifact_v1_schema.json",
}


def sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def revision(label: str) -> str:
    return "sha256:" + sha(label)


def image(label: str) -> str:
    return f"registry.example/simworld/{label}@sha256:{sha('image-' + label)}"


def seal(value: Any) -> SealedDocument:
    raw = canonical_jcs_bytes(value)
    return SealedDocument(raw=raw, expected_sha256=hashlib.sha256(raw).hexdigest())


def sealed_formal_schemas() -> dict[str, SealedDocument]:
    result: dict[str, SealedDocument] = {}
    for digest_key, filename in FORMAL_SCHEMA_FILES.items():
        raw = (TOOLS_DIR / filename).read_bytes()
        result[digest_key] = SealedDocument(
            raw=raw, expected_sha256=hashlib.sha256(raw).hexdigest()
        )
    return result


def unseal(document: SealedDocument) -> Any:
    return decode_canonical_jcs(document.raw)


def signed_approval(value: dict[str, Any], label: str) -> dict[str, Any]:
    signature_bytes = (label.encode("ascii") + b"-signature").ljust(64, b"!")[:64]
    value["signature"] = {
        "algorithm": "ed25519",
        "canonicalization": "rfc8785-jcs-v1",
        "signed_payload_sha256": canonical_sha256(value),
        "detached_signature_base64": base64.b64encode(signature_bytes).decode("ascii"),
        "detached_signature_sha256": hashlib.sha256(signature_bytes).hexdigest(),
    }
    return value


def approval(
    kind: str,
    basis_sha256: str,
    scope: dict[str, Any],
    authorization: dict[str, Any],
) -> SealedDocument:
    roles = {
        "data_owner_review": "data_owner",
        "cost_owner": "cost_owner",
        "admin_state_change": "state_change_admin",
        "deployment": "deployment_attestor",
        "runtime": "runtime_owner",
        "rollback_readiness": "rollback_owner",
    }
    value = {
        "schema": "simworld-semantic-index-approval-receipt/v1",
        "receipt_id": f"semantic-index-approval:{kind}-001",
        "approval_kind": kind,
        "decision": "approved",
        "approval_basis_sha256": basis_sha256,
        "scope": scope,
        "authorization": authorization,
        "issuer": {
            "issuer_id": f"issuer:{kind}",
            "role": roles[kind],
            "key_id": f"approval-key:{kind}",
            "key_revision": revision("approval-key"),
        },
        "trust_bundle": {
            "schema": "simworld-approval-trust-bundle/v1",
            "revision": revision("trust-bundle"),
            "sha256": sha("trust-bundle"),
            "issuer_membership_sha256": sha("issuer-membership"),
        },
        "issued_at": ISSUED,
        "expires_at": EXPIRES,
        "serialization_contract": copy.deepcopy(SERIALIZATION),
    }
    return seal(signed_approval(value, kind))


def approval_ref(document: SealedDocument) -> dict[str, Any]:
    value = unseal(document)
    return {
        "receipt_schema": value["schema"],
        "receipt_id": value["receipt_id"],
        "receipt_sha256": document.expected_sha256,
        "approval_kind": value["approval_kind"],
        "approval_basis_sha256": value["approval_basis_sha256"],
        "trust_bundle_sha256": value["trust_bundle"]["sha256"],
        "issuer_key_id": value["issuer"]["key_id"],
        "issuer_key_revision": value["issuer"]["key_revision"],
        "signature_algorithm": value["signature"]["algorithm"],
        "expires_at": value["expires_at"],
    }


def asset(asset_id: str, asset_type: str, accepted: bool) -> dict[str, Any]:
    stem = "BP_Chair" if asset_type == "Blueprint" else ("SM_Table" if accepted else "SM_Rejected")
    value = {
        "asset_id": asset_id,
        "ue_name": stem,
        "ue_path": f"/Game/Furniture/{stem}.{stem}",
        "asset_type": asset_type,
        "source_pack": "VISTA",
        "content_revision": revision("content"),
        "status": "reviewed_accepted" if accepted else "reviewed_rejected",
    }
    if not accepted:
        value.update(reason_code="data_owner_rejected", ledger_entry_sha256=sha("reject"))
    return value


def capability(scope: str, target: str, generation_id: str) -> dict[str, Any]:
    return {
        "credential_generation": revision("credentials"),
        "generation_id": generation_id,
        "target_identity": target,
        "scope": scope,
    }


def make_bundle(
    *,
    basis_mutator: Callable[[dict[str, Any]], None] | None = None,
    plan_mutator: Callable[[dict[str, Any]], None] | None = None,
    accepted_asset_mutator: Callable[[list[dict[str, Any]]], None] | None = None,
) -> ProductionContractBundle:
    formal_schemas = sealed_formal_schemas()
    source_accepted_assets = [
        asset("bp_chair", "Blueprint", True),
        asset("sm_table", "StaticMesh", True),
    ]
    source_rejected_assets = [asset("sm_rejected", "StaticMesh", False)]
    source_fields = (
        "asset_id",
        "ue_name",
        "ue_path",
        "asset_type",
        "source_pack",
        "content_revision",
    )
    candidates = [
        {key: record[key] for key in source_fields}
        for record in (
            source_accepted_assets[0],
            source_rejected_assets[0],
            source_accepted_assets[1],
        )
    ]
    candidate_document = seal(candidates)
    accepted_assets = copy.deepcopy(source_accepted_assets)
    rejected_assets = copy.deepcopy(source_rejected_assets)
    if accepted_asset_mutator is not None:
        accepted_asset_mutator(accepted_assets)
    accepted_sha = canonical_sha256(accepted_assets)
    rejected_sha = canonical_sha256(rejected_assets)
    selection_basis = sha("selection-basis")
    rejection_ledger = sha("rejection-ledger")
    data_scope = {
        "scope_kind": "reviewed_selection",
        "source_v1_job_sha256": sha("v1-job"),
        "source_v1_job_revision": revision("v1-job"),
        "reviewed_selection_basis_sha256": selection_basis,
        "accepted_assets_sha256": accepted_sha,
        "accepted_asset_count": 2,
        "rejection_ledger_sha256": rejection_ledger,
        "content_revision": revision("content"),
    }
    data_receipt = approval(
        "data_owner_review",
        selection_basis,
        data_scope,
        {
            "kind": "data_owner_review",
            "reviewed_selection_approved": True,
            "production_execution_authorized": False,
        },
    )
    proof_basis = {
        "algorithm": "sorted-asset-id-partition-v1",
        "candidate_count": 3,
        "candidate_set_sha256": candidate_document.expected_sha256,
        "accepted_count": 2,
        "accepted_assets_sha256": accepted_sha,
        "rejected_count": 1,
        "rejected_assets_sha256": rejected_sha,
        "disjoint": True,
        "complete": True,
    }
    resources = {
        "assets": 2,
        "rendered_views": 4,
        "render_pixels": 1_048_576,
        "caption_calls": 2,
        "caption_output_tokens": 512,
        "catalog_bytes": 8_192,
        "embedding_batches": 1,
        "postgres_rows": 2,
        "qdrant_points": 2,
        "dense_vector_payload_bytes": 8_192,
    }
    job = {
        "schema": "simworld-semantic-asset-index-job/v2",
        "source": {
            "v1_job_schema": "simworld-semantic-asset-index-job/v1",
            "v1_job_sha256": sha("v1-job"),
            "v1_job_revision": revision("v1-job"),
            "preparation_receipt_schema": "simworld-semantic-asset-index-job-preparation-receipt/v1",
            "preparation_receipt_sha256": sha("preparation"),
            "candidate_set_count": 3,
            "candidate_set_sha256": candidate_document.expected_sha256,
            "project": {
                "name": "VISTAWorld",
                "revision": "project:20260721",
                "engine_version": "5.5.4",
            },
            "content": {"mount_point": "/Game", "revision": revision("content")},
            "recipe_schema": "simworld-semantic-asset-index-recipe/v1",
            "recipe_sha256": sha("recipe"),
            "recipe_revision": "recipe:20260721",
            "target_snapshot_revision": SNAPSHOT,
        },
        "reviewed_selection": {
            "selection": "explicit_reviewed_partition",
            "selection_approval_basis_sha256": selection_basis,
            "candidate_set": {"count": 3, "assets_sha256": candidate_document.expected_sha256},
            "accepted_set": {"count": 2, "assets_sha256": accepted_sha, "assets": accepted_assets},
            "rejected_set": {"count": 1, "assets_sha256": rejected_sha, "assets": rejected_assets},
            "rejection_ledger": {
                "schema": "simworld-semantic-index-rejection-ledger/v1",
                "revision": revision("rejection-ledger"),
                "sha256": rejection_ledger,
            },
            "partition_proof": {**proof_basis, "proof_sha256": canonical_sha256(proof_basis)},
        },
        "recipe_contract": {
            "schema": "simworld-semantic-asset-index-recipe/v1",
            "sha256": sha("recipe"),
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
            "computed_for_accepted_assets_sha256": accepted_sha,
            "limits": copy.deepcopy(resources),
            "estimates": copy.deepcopy(resources),
            "cost_authorized": False,
            "state_change_authorized": False,
        },
        "data_owner_approval": {
            "receipt_schema": "simworld-semantic-index-approval-receipt/v1",
            "receipt_id": unseal(data_receipt)["receipt_id"],
            "receipt_sha256": data_receipt.expected_sha256,
            "approval_basis_sha256": selection_basis,
            "approval_kind": "data_owner_review",
            "accepted_assets_sha256": accepted_sha,
            "rejection_ledger_sha256": rejection_ledger,
            "content_revision": revision("content"),
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
        "serialization_contract": copy.deepcopy(SERIALIZATION),
    }
    job["job_revision"] = "sha256:" + canonical_sha256(job)
    job_document = seal(job)
    accepted_id_sha = sorted_id_set_sha256(["bp_chair", "sm_table"])
    zero_generation = "semantic-generation:" + "0" * 64
    credentials = {
        "transport": "sealed_memfd_scm_rights_v1",
        "json_contains_credential_bytes": False,
        "ordinary_phases": {
            "inspect": capability("ue_disposable_scene_inspect_spawn_cleanup", "ue-broker:prod", zero_generation),
            "render": capability("ue_disposable_scene_render_staging_cleanup", "ue-broker:prod", zero_generation),
            "caption": capability("caption_bounded_idempotent_call", "caption-project:vista", zero_generation),
            "embed": capability("embedding_immutable_vector_staging", "embedding-project:vista", zero_generation),
            "postgres": capability("postgres_generation_write", "postgres-deployment:prod", zero_generation),
            "qdrant": capability("qdrant_generation_write", "qdrant-cluster:prod", zero_generation),
        },
        "reconcile": {
            "postgres_read": capability("postgres_generation_read_only", "postgres-deployment:prod", zero_generation),
            "qdrant_read": capability("qdrant_generation_read_only", "qdrant-cluster:prod", zero_generation),
            "ue_runtime_read": capability("ue_runtime_read_only", "ue-broker:prod", zero_generation),
        },
        "control_operations": {
            operation: capability(scope, "semantic-worker:prod", zero_generation)
            for operation, scope in CONTROL_SCOPES.items()
        },
    }
    expected = {"count": 2, "sorted_ids_sha256": accepted_id_sha, "canonicalization": "sorted-utf8-nfc-lf-v1"}
    plan = {
        "schema": "simworld-semantic-asset-index-execution-plan/v2",
        "profile": "production",
        "run_identity": {
            "run_id": "semantic-index-run:fixture-001",
            "correlation_id": "semantic-index-correlation:001",
            "operator_id": "operator:codex",
            "service_owner_id": "service-owner:semantic-index",
            "retention_class": "production_evidence_1y",
        },
        "reviewed_job": {
            "schema": job["schema"],
            "sha256": job_document.expected_sha256,
            "revision": job["job_revision"],
            "source_v1_job_sha256": job["source"]["v1_job_sha256"],
            "preparation_receipt_sha256": job["source"]["preparation_receipt_sha256"],
            "accepted_asset_count": 2,
            "accepted_assets_sha256": accepted_sha,
            "rejected_asset_count": 1,
            "rejected_assets_sha256": rejected_sha,
            "rejection_ledger_sha256": rejection_ledger,
            "content_revision": revision("content"),
            "target_snapshot_revision": SNAPSHOT,
        },
        "approval_basis": {},
        "contract_digests": {
            key: (
                formal_schemas[key].expected_sha256
                if key in formal_schemas
                else sha("worker-protocol")
                if key == "worker_protocol_sha256"
                else sha(key)
            )
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
            "receipt_sha256": sha("preflight"),
            "signature_sha256": sha("preflight-signature"),
            "deployment_identity": "semantic-worker:prod",
            "worker_image": image("worker"),
            "endpoint_map_sha256": sha("endpoint-map"),
            "network_policy": "uds_and_loopback_mtls_only_v1",
            "whoami_contract_sha256": sha("whoami-contract"),
            "worker_attestation_sha256": sha("worker-attestation"),
            "issued_at": ISSUED,
            "expires_at": EXPIRES,
        },
        "generation": {
            "generation_nonce": "0123456789abcdeffedcba9876543210",
            "generation_nonce_sha256": sha("placeholder"),
            "generation_nonce_sha256_derivation": "sha256(decoded-lowercase-hex-16-bytes)-v1",
            "generation_id": zero_generation,
            "derivation": "sha256-ascii-nul-separated(simworld-semantic-index-generation-id/v1,reviewed_job_revision,approval_basis_sha256)-v1",
            "reviewed_job_sha256": job_document.expected_sha256,
            "reviewed_job_revision": job["job_revision"],
            "approval_basis_sha256": sha("placeholder"),
            "target_generation_identities_sha256": sha("placeholder"),
            "artifact_namespace": {
                "store_identity": "artifact-store:prod",
                "workspace_root": "/var/lib/simworld",
                "workspace": "/var/lib/simworld/placeholder",
                "layout": "content-addressed-sha256-no-replace-v1",
                "generation_name_policy": "semantic-generation-id-path-segment-v1",
                "namespace_revision": revision("namespace"),
            },
            "reservation": {
                "receipt_schema": "simworld-semantic-index-generation-reservation/v1",
                "receipt_sha256": sha("reservation"),
                "reservation_id": "generation-reservation:fixture-001",
                "mode": "exclusive_create_only",
                "owner_identity": "service-owner:semantic-index",
                "issued_at": ISSUED,
                "expires_at": EXPIRES,
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
            "peer_deployment_identity": "semantic-worker:prod",
            "whoami_sha256": sha("worker-whoami"),
            "peer_attestation_sha256": sha("worker-peer-attestation"),
            "runtime_attestation_sha256": sha("worker-attestation"),
            "host_boot_id_sha256": sha("boot"),
            "idempotency_ledger_identity": "worker-ledger:prod",
            "idempotency_ledger_revision": revision("worker-ledger"),
            "idempotency_ledger_mode": "append_only_prepare_result_fsync_no_replace_v1",
            "idempotency_incomplete_policy": "recovery_required_no_blind_retry_v1",
            "peer_pid": 12345,
            "process_start_time_ticks": 987654321,
            "process_start_token_sha256": sha("process-start"),
            "socket_device": 42,
            "socket_inode": 4242,
            "socket_inode_binding_sha256": sha("socket-binding"),
            "max_frame_bytes": 2097152,
            "connect_timeout_ms": 1000,
            "read_chunk_timeout_ms": 1000,
            "write_timeout_ms": 1000,
            "deadline_field": "deadline_monotonic_ns",
            "deadline_clock": "host_shared_CLOCK_MONOTONIC",
            "phase_operations": [OPERATIONS[phase] for phase in PHASES],
            "control_operations": list(CONTROL_SCOPES),
        },
        "unreal": {
            "project_name": "VISTAWorld",
            "project_revision": "project:20260721",
            "content_revision": revision("content"),
            "runtime_image": image("unreal"),
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
            "lease_issued_at": ISSUED,
            "lease_expires_at": EXPIRES,
            "inspect_scope": "ue_disposable_scene_inspect_spawn_cleanup",
            "render_scope": "ue_disposable_scene_render_staging_cleanup",
        },
        "caption": {
            "provider_identity": "caption-provider:prod",
            "project_identity": "caption-project:vista",
            "model_id": "caption-model:v1",
            "model_snapshot": "provider-snapshot:20260721",
            "endpoint_identity": "caption-endpoint:prod",
            "prompt_bytes_sha256": sha("caption-prompt"),
            "output_schema_bytes_sha256": sha("caption-output-schema"),
            "render_recipe_bytes_sha256": sha("render-recipe"),
            "max_calls": 2,
            "max_output_tokens": 512,
            "currency": "USD",
            "minor_unit_exponent": 6,
            "max_cost_minor_units": 1000,
            "rounding_mode": "ceiling_each_provider_charge_to_minor_unit",
            "retry_charge_policy": "all_provider_accepted_requests_counted",
            "idempotency_policy": "provider_key_or_worker_ledger_no_blind_retry",
            "tools_allowed": False,
        },
        "embedding": {
            "project_identity": "embedding-project:vista",
            "endpoint_identity": "embedding-endpoint:prod",
            "dense_model_id": "dense-model:v1",
            "dense_model_revision": revision("dense"),
            "dense_artifact_manifest_sha256": sha("dense-artifact"),
            "sparse_model_id": "sparse-model:v1",
            "sparse_model_revision": revision("sparse"),
            "sparse_artifact_manifest_sha256": sha("sparse-artifact"),
            "dense_dimensions": 8,
            "sparse_max_dimensions": 100000,
            "batch_size": 1,
        },
        "storage": {
            "catalog": {
                "generation_id": zero_generation,
                "namespace_revision": revision("namespace"),
                "active_target": False,
            },
            "postgres": {
                "deployment_identity": "postgres-deployment:prod",
                "database": "semantic_gen_placeholder",
                "schema_name": "snapshot_placeholder",
                "schema_revision": 3,
                "generation_id": zero_generation,
                "generation_name_policy": "semantic-generation-id-sql-identifiers-v1",
                "active_target": False,
            },
            "qdrant": {
                "cluster_identity": "qdrant-cluster:prod",
                "collection": "semantic_gen_placeholder",
                "dense_vector_name": "dense_v1",
                "sparse_vector_name": "sparse_v1",
                "generation_id": zero_generation,
                "generation_name_policy": "semantic-generation-id-qdrant-collection-v1",
                "active_target": False,
                "point_id_policy": "sha256-generation-plus-asset-id-v1",
                "same_id_replay_policy": "allow_only_identical_payload_and_vector_digests",
            },
        },
        "runtime_images": {name: image(name) for name in ("unreal", "worker", "postgres", "qdrant", "embedding")},
        "target_attestations": {
            component: {
                "runtime_identity": f"{component}-runtime:20260721",
                "runtime_image": None if component == "caption" else image(component),
                "whoami_sha256": sha(f"{component}-whoami"),
                "runtime_attestation_sha256": sha(f"{component}-attestation"),
            }
            for component in ("unreal", "caption", "embedding", "postgres", "qdrant")
        },
        "expected_sets": {
            "assets": copy.deepcopy(expected),
            "renders": {"count": 4, "sorted_ids_sha256": sha("render-set"), "canonicalization": "sorted-utf8-nfc-lf-v1"},
            **{name: copy.deepcopy(expected) for name in ("catalog_records", "dense_vectors", "sparse_vectors", "postgres_rows", "qdrant_points")},
        },
        "acceptance": {
            "shadow_queries": [
                {"query_id": "query:zh", "language": "zh-TW", "query_text": "木椅", "query_text_sha256": hashlib.sha256("木椅".encode()).hexdigest(), "normalization_revision": revision("normalize"), "expected_asset_ids": ["bp_chair"], "minimum_rank": 2},
                {"query_id": "query:en", "language": "en", "query_text": "wooden table", "query_text_sha256": hashlib.sha256(b"wooden table").hexdigest(), "normalization_revision": revision("normalize"), "expected_asset_ids": ["sm_table"], "minimum_rank": 2},
            ],
            "smoke_targets": [
                {"asset_id": "bp_chair", "ue_path": accepted_assets[0]["ue_path"], "asset_type": "Blueprint"},
                {"asset_id": "sm_table", "ue_path": accepted_assets[1]["ue_path"], "asset_type": "StaticMesh"},
            ],
            "authoritative_snapshot_schema": "simworld-asset-snapshot-exact-set/v2",
        },
        "credentials": credentials,
        "phase_policy": {
            "max_total_elapsed_seconds": 3600,
            "deadline_clock": "host_shared_CLOCK_MONOTONIC",
            "inspect": {"operation": OPERATIONS["inspect"], "allowed_mutation_states": ["none", "ambiguous"]},
            "render": {"operation": OPERATIONS["render"], "allowed_mutation_states": ["none", "staged", "ambiguous"]},
            "caption": {"operation": OPERATIONS["caption"], "allowed_mutation_states": ["none", "committed", "ambiguous"]},
            "embed": {"operation": OPERATIONS["embed"], "allowed_mutation_states": ["none", "staged", "ambiguous"]},
            "postgres": {"operation": OPERATIONS["postgres"], "allowed_mutation_states": ["none", "committed", "ambiguous"]},
            "qdrant": {"operation": OPERATIONS["qdrant"], "allowed_mutation_states": ["none", "staged", "committed", "ambiguous"]},
            "reconcile": {"operation": OPERATIONS["reconcile"], "allowed_mutation_states": ["none"]},
        },
        "approvals": {},
        "activation_contract": {
            "successful_build_state": "snapshot_built_verified",
            "activation_authorized": False,
            "activation_is_separate_operation": True,
            "activation_primitive": "single_active_generation_pointer_epoch_cas",
            "multi_system_atomicity_claimed": False,
            "rollback_primitive": "same_pointer_epoch_cas_to_previous_generation",
        },
        "serialization_contract": copy.deepcopy(SERIALIZATION),
    }
    for phase in PHASES:
        plan["phase_policy"][phase].update(timeout_seconds=300, max_attempts=2)
    if plan_mutator is not None:
        plan_mutator(plan)
    nonce = bytes.fromhex(plan["generation"]["generation_nonce"])
    plan["generation"]["generation_nonce_sha256"] = hashlib.sha256(nonce).hexdigest()
    basis = derive_expected_approval_basis(plan, job)
    if basis_mutator is not None:
        basis_mutator(basis)
    basis_document = seal(basis)
    generation_id = derive_generation_id(job["job_revision"], basis_document.expected_sha256)
    generation_hex = generation_id.split(":", 1)[1]
    plan["approval_basis"] = {
        "schema": basis["schema"],
        "revision": basis["revision"],
        "sha256": basis_document.expected_sha256,
        "canonicalization": "rfc8785-jcs-v1",
    }
    generation = plan["generation"]
    generation["generation_id"] = generation_id
    generation["approval_basis_sha256"] = basis_document.expected_sha256
    generation["target_generation_identities_sha256"] = canonical_sha256(derive_target_generation_identities(plan))
    generation["artifact_namespace"]["workspace"] = f"/var/lib/simworld/semantic_generation_{generation_hex}"
    for component in plan["storage"].values():
        component["generation_id"] = generation_id
    plan["storage"]["postgres"]["database"] = f"semantic_gen_{generation_hex[:50]}"
    plan["storage"]["postgres"]["schema_name"] = f"snapshot_{generation_hex[:54]}"
    plan["storage"]["qdrant"]["collection"] = f"semantic_gen_{generation_hex}"
    for group in (
        credentials["ordinary_phases"],
        credentials["reconcile"],
        credentials["control_operations"],
    ):
        for item in group.values():
            item["generation_id"] = generation_id
    pointer = generation["active_generation_pointer"]
    production_scope = {
        "scope_kind": "production_build",
        "reviewed_job_sha256": job_document.expected_sha256,
        "reviewed_job_revision": job["job_revision"],
        "plan_approval_basis_sha256": basis_document.expected_sha256,
        "generation_id": generation_id,
        "generation_nonce_sha256": generation["generation_nonce_sha256"],
        "target_snapshot_revision": SNAPSHOT,
        "worker_deployment_identity": "semantic-worker:prod",
        "active_generation_pointer_identity": pointer["identity"],
        "expected_active_generation_epoch": pointer["observed_epoch"],
    }
    authorizations = {
        "cost_owner": {"kind": "cost_owner", "max_caption_calls": 2, "max_output_tokens": 512, "currency": "USD", "minor_unit_exponent": 6, "max_cost_minor_units": 1000, "rounding_mode": "ceiling_each_provider_charge_to_minor_unit", "retry_charge_policy": "all_provider_accepted_requests_counted"},
        "admin_state_change": {"kind": "admin_state_change", "allowed_mutating_phases": ["inspect", "render", "caption", "embed", "postgres", "qdrant"], "allowed_control_operations": list(CONTROL_SCOPES), "activation_authorized": False},
        "deployment": {"kind": "deployment", "deployment_preflight_sha256": sha("preflight"), "worker_image": image("worker"), "worker_protocol_sha256": sha("worker-protocol")},
        "runtime": {"kind": "runtime", "ue_lease_id": "ue-lease:001", "ue_lease_expires_at": EXPIRES, "worker_deployment_identity": "semantic-worker:prod"},
        "rollback_readiness": {"kind": "rollback_readiness", "bootstrap": True, "previous_generation_id": None, "previous_snapshot_revision": None, "rollback_readiness_receipt_sha256": sha("rollback-readiness"), "active_generation_pointer_identity": pointer["identity"], "expected_active_generation_epoch": pointer["observed_epoch"]},
    }
    approvals = {"data_owner_review": data_receipt}
    for kind, authorization in authorizations.items():
        approvals[kind] = approval(kind, basis_document.expected_sha256, production_scope, authorization)
    plan["approvals"] = {kind: approval_ref(document) for kind, document in approvals.items()}
    plan_document = seal(plan)
    phase_requests = make_phase_requests(
        plan, plan_document, job, job_document, basis_document
    )
    phase_evidence = make_evidence(
        plan,
        plan_document,
        job,
        job_document,
        basis_document,
        approvals,
        accepted_assets,
        phase_requests,
    )
    return ProductionContractBundle(
        source_candidates=candidate_document,
        reviewed_job=job_document,
        approval_basis=basis_document,
        execution_plan=plan_document,
        approvals=approvals,
        phase_requests=tuple(phase_requests),
        phase_evidence=tuple(phase_evidence),
        contract_schemas=formal_schemas,
    )


def evidence_signature(value: dict[str, Any], phase: str) -> None:
    value["worker_signature"] = {
        "key_revision": revision("worker-key"),
        "signed_payload_sha256": canonical_sha256(value),
        "signature_sha256": sha("worker-signature-" + phase),
    }


def make_phase_requests(
    plan: dict[str, Any],
    plan_document: SealedDocument,
    job: dict[str, Any],
    job_document: SealedDocument,
    basis_document: SealedDocument,
) -> list[SealedDocument]:
    component_map = {
        "inspect": ("unreal",),
        "render": ("unreal",),
        "caption": ("caption",),
        "embed": ("embedding",),
        "postgres": ("postgres",),
        "qdrant": ("qdrant",),
        "reconcile": ("postgres", "qdrant", "unreal"),
    }
    expected = plan["expected_sets"]
    metrics = {
        "inspect": {"assets_inspected": expected["assets"]["count"]},
        "render": {
            "assets_rendered": expected["assets"]["count"],
            "rendered_views": expected["renders"]["count"],
        },
        "caption": {"catalog_records": expected["catalog_records"]["count"]},
        "embed": {
            "dense_vectors": expected["dense_vectors"]["count"],
            "sparse_vectors": expected["sparse_vectors"]["count"],
        },
        "postgres": {"postgres_rows": expected["postgres_rows"]["count"]},
        "qdrant": {"qdrant_points": expected["qdrant_points"]["count"]},
        "reconcile": {
            "catalog_records": expected["catalog_records"]["count"],
            "postgres_rows": expected["postgres_rows"]["count"],
            "qdrant_points": expected["qdrant_points"]["count"],
        },
    }
    worker = plan["worker"]
    deployment = plan["deployment_preflight"]
    worker_binding = {
        "deployment_identity": deployment["deployment_identity"],
        "runtime_image": deployment["worker_image"],
        "whoami_sha256": worker["whoami_sha256"],
        "peer_attestation_sha256": worker["peer_attestation_sha256"],
        "runtime_attestation_sha256": worker["runtime_attestation_sha256"],
        "host_boot_id_sha256": worker["host_boot_id_sha256"],
        "peer_uid": worker["socket_owner_uid"],
        "peer_gid": worker["socket_owner_gid"],
        "peer_pid": worker["peer_pid"],
        "process_start_time_ticks": worker["process_start_time_ticks"],
        "process_start_token_sha256": worker["process_start_token_sha256"],
        "socket_device": worker["socket_device"],
        "socket_inode": worker["socket_inode"],
        "socket_inode_binding_sha256": worker["socket_inode_binding_sha256"],
    }
    result: list[SealedDocument] = []
    for phase in PHASES:
        if phase == "reconcile":
            capabilities = [
                plan["credentials"]["reconcile"][name]
                for name in ("postgres_read", "qdrant_read", "ue_runtime_read")
            ]
        else:
            capabilities = [plan["credentials"]["ordinary_phases"][phase]]
        descriptors = [
            {
                "fd_index": index,
                "scope": capability["scope"],
                "credential_generation": capability["credential_generation"],
                "generation_id": plan["generation"]["generation_id"],
                "target_identity": capability["target_identity"],
                "byte_count": 64,
            }
            for index, capability in enumerate(capabilities)
        ]
        targets = []
        for capability, component in zip(capabilities, component_map[phase]):
            attestation = plan["target_attestations"][component]
            targets.append(
                {
                    "component": component,
                    "scope": capability["scope"],
                    "credential_generation": capability["credential_generation"],
                    "generation_id": plan["generation"]["generation_id"],
                    "target_identity": capability["target_identity"],
                    "runtime_identity": attestation["runtime_identity"],
                    "runtime_image": attestation["runtime_image"],
                    "whoami_sha256": attestation["whoami_sha256"],
                    "runtime_attestation_sha256": attestation[
                        "runtime_attestation_sha256"
                    ],
                }
            )
        request = {
            "schema": "simworld-semantic-index-phase-request/v1",
            "protocol": "simworld-semantic-index-worker/v1",
            "operation": OPERATIONS[phase],
            "operation_revision": worker_operation_revision(OPERATIONS[phase]),
            "phase": phase,
            "request_id": f"semantic-index-request:{phase}-001",
            "job_revision": job["job_revision"],
            "reviewed_job_sha256": job_document.expected_sha256,
            "approval_basis_sha256": basis_document.expected_sha256,
            "execution_plan_sha256": plan_document.expected_sha256,
            "launcher_handoff_receipt_sha256": sha("launcher-handoff-" + phase),
            "generation_id": plan["generation"]["generation_id"],
            "generation_binding_sha256": plan["generation"][
                "target_generation_identities_sha256"
            ],
            "execution_binding": {
                "run_id": plan["run_identity"]["run_id"],
                "correlation_id": plan["run_identity"]["correlation_id"],
                "owner_identity": plan["run_identity"]["service_owner_id"],
                "lease_id": plan["unreal"]["lease_id"],
                "slot_id": plan["unreal"]["slot_id"],
            },
            "worker_binding": copy.deepcopy(worker_binding),
            "input_binding": {
                "schema": f"semantic-index-input:{phase}/v1",
                "sha256": sha("input-" + phase),
                "item_count": expected["assets"]["count"],
                "byte_count": 4096,
            },
            "expected_targets": targets,
            "expected_metrics": metrics[phase],
            "credential_transport": {
                "kind": "sealed_memfd_scm_rights_v1",
                "json_contains_credential_bytes": False,
                "descriptors": descriptors,
            },
            "idempotency_key": revision("idempotency-" + phase),
            "idempotency_ledger_identity": plan["worker"][
                "idempotency_ledger_identity"
            ],
            "idempotency_ledger_revision": plan["worker"][
                "idempotency_ledger_revision"
            ],
            "deadline_monotonic_ns": 999999999,
        }
        result.append(seal(request))
    return result


def make_evidence(
    plan: dict[str, Any],
    plan_document: SealedDocument,
    job: dict[str, Any],
    job_document: SealedDocument,
    basis_document: SealedDocument,
    approvals: dict[str, SealedDocument],
    accepted: list[dict[str, Any]],
    phase_requests: list[SealedDocument],
) -> list[SealedDocument]:
    target_map = {
        "unreal": ("ue-broker:prod", image("unreal")),
        "caption": ("caption-project:vista", None),
        "embedding": ("embedding-project:vista", image("embedding")),
        "postgres": ("postgres-deployment:prod", image("postgres")),
        "qdrant": ("qdrant-cluster:prod", image("qdrant")),
        "worker": ("semantic-worker:prod", image("worker")),
    }
    component_map = {
        "inspect": ["unreal"], "render": ["unreal"], "caption": ["caption"],
        "embed": ["embedding"], "postgres": ["postgres"], "qdrant": ["qdrant"],
        "reconcile": ["postgres", "qdrant", "unreal"],
    }
    evidence_documents: list[SealedDocument] = []
    outputs: dict[str, str] = {}
    ids: dict[str, str] = {}
    asset_set = plan["expected_sets"]["assets"]
    for index, phase in enumerate(PHASES):
        phase_request = unseal(phase_requests[index])
        capability_values = ([plan["credentials"]["ordinary_phases"][phase]] if phase != "reconcile" else list(plan["credentials"]["reconcile"].values()))
        observed_credentials = [
            {key: item[key] for key in ("scope", "credential_generation", "generation_id", "target_identity")}
            for item in capability_values
        ]
        targets = [
            {
                "component": component,
                "target_identity": target_map[component][0],
                **copy.deepcopy(plan["target_attestations"][component]),
            }
            for component in component_map[phase]
        ]
        value = {
            "schema": "simworld-semantic-index-phase-evidence/v1",
            "evidence_id": f"semantic-index-evidence:{phase}-001",
            "run_identity": {
                **plan["run_identity"],
                "ue_slot_id": plan["unreal"]["slot_id"],
                "ue_lease_id": plan["unreal"]["lease_id"],
                "ue_lease_expires_at": plan["unreal"]["lease_expires_at"],
            },
            "job_binding": {
                "schema": job["schema"], "sha256": job_document.expected_sha256,
                "revision": job["job_revision"], "accepted_asset_count": 2,
                "accepted_assets_sha256": job["reviewed_selection"]["accepted_set"]["assets_sha256"],
                "content_revision": revision("content"), "snapshot_revision": SNAPSHOT,
            },
            "plan_binding": {
                "schema": plan["schema"], "sha256": plan_document.expected_sha256,
                "approval_basis_sha256": basis_document.expected_sha256,
                "generation_id": plan["generation"]["generation_id"],
                "generation_nonce_sha256": plan["generation"]["generation_nonce_sha256"],
                "reservation_receipt_sha256": plan["generation"]["reservation"]["receipt_sha256"],
                "worker_deployment_identity": "semantic-worker:prod",
            },
            "phase": phase,
            "operation": OPERATIONS[phase],
            "operation_revision": phase_request["operation_revision"],
            "idempotency": {
                "key": phase_request["idempotency_key"],
                "request_sha256": phase_requests[index].expected_sha256,
                "ledger_identity": plan["worker"]["idempotency_ledger_identity"],
                "ledger_revision": plan["worker"]["idempotency_ledger_revision"],
                "result": "applied",
            },
            "request": {
                "schema": phase_request["schema"],
                "sha256": phase_requests[index].expected_sha256,
                "input_artifact_sha256": phase_request["input_binding"]["sha256"],
                "deadline_monotonic_ns": phase_request["deadline_monotonic_ns"],
                "host_boot_id_sha256": phase_request["worker_binding"][
                    "host_boot_id_sha256"
                ],
                "credential_transport": phase_request["credential_transport"]["kind"],
                "json_contains_credential_bytes": phase_request["credential_transport"][
                    "json_contains_credential_bytes"
                ],
            },
            "credentials_observed": observed_credentials,
            "target_attestation": {
                "worker_deployment_identity": "semantic-worker:prod",
                "worker_image": image("worker"),
                "deployment_preflight_sha256": sha("preflight"),
                "worker_whoami_sha256": sha("worker-whoami"),
                "worker_runtime_attestation_sha256": sha("worker-attestation"),
                "targets": targets,
            },
            "prior_receipts": [
                {"phase": prior, "evidence_id": ids[prior], "evidence_sha256": evidence_documents[i].expected_sha256, "asset_set_sha256": asset_set["sorted_ids_sha256"], "output_artifact_sha256": outputs[prior]}
                for i, prior in enumerate(PHASES[:index])
            ],
            "asset_set": copy.deepcopy(asset_set),
            "outcome": {
                "status": "success",
                "mutation_state": SUCCESS_STATES[phase],
                "retryable": False,
                "error": None,
                "remote_receipt_recovered": False,
            },
            "started_at": f"2026-07-21T11:{40 + index * 2:02d}:00Z",
            "completed_at": f"2026-07-21T11:{40 + index * 2 + 1:02d}:00Z",
            "payload": phase_payload(phase, plan, accepted, evidence_documents, outputs, approvals, target_map),
            "serialization_contract": copy.deepcopy(SERIALIZATION),
        }
        evidence_signature(value, phase)
        document = seal(value)
        evidence_documents.append(document)
        ids[phase] = value["evidence_id"]
        outputs[phase] = evidence_output(value, phase)
    return evidence_documents


def phase_payload(
    phase: str,
    plan: dict[str, Any],
    accepted: list[dict[str, Any]],
    prior_documents: list[SealedDocument],
    outputs: dict[str, str],
    approvals: dict[str, SealedDocument],
    target_map: dict[str, tuple[str, str | None]],
) -> dict[str, Any]:
    if phase == "inspect":
        records = []
        for item in accepted:
            stem = "Chair" if item["asset_type"] == "Blueprint" else "Table"
            records.append(
                {
                    "asset_id": item["asset_id"],
                    "ue_path": item["ue_path"],
                    "asset_type": item["asset_type"],
                    "loaded": True,
                    "spawned": True,
                    "bounds_micrometers": {"x": 1_000_000, "y": 1_000_000, "z": 1_000_000},
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
                                    "content_sha256": sha("texture-" + stem),
                                }
                            ],
                        }
                    ],
                    "cleanup_state": "disposable_spawn_destroyed",
                }
            )
        return {
            "kind": "inspect_exact_assets/v1",
            "content_revision": revision("content"),
            "disposable_scene_id": "ue-disposable-scene:fixture-001",
            "records": records,
            "records_sha256": canonical_sha256(records),
            "scene_cleanup_state": "destroyed_and_verified",
        }
    if phase == "render":
        records = [
            {
                "asset_id": item["asset_id"],
                "view_id": f"view:{view}",
                "image_sha256": sha(f"render-{item['asset_id']}-{view}"),
                "width_px": 512,
                "height_px": 512,
                "nonblank": True,
                "source_inspect_record_sha256": sha("inspect-record-" + item["asset_id"]),
            }
            for item in accepted
            for view in ("front", "side")
        ]
        return {
            "kind": "render_exact_views/v1",
            "render_recipe_sha256": sha("render-recipe"),
            "ue_runtime_revision": "unreal-runtime:20260721",
            "records": records,
            "render_set_count": 4,
            "render_set_sha256": plan["expected_sets"]["renders"]["sorted_ids_sha256"],
            "render_manifest_sha256": sha("render-manifest"),
            "scene_cleanup_state": "destroyed_and_verified",
        }
    if phase == "caption":
        records = [
            {
                "asset_id": item["asset_id"],
                "render_subset_sha256": sha("render-subset-" + item["asset_id"]),
                "caption_record_sha256": sha("caption-record-" + item["asset_id"]),
                "caption_text_sha256": sha("caption-text-" + item["asset_id"]),
                "strict_schema_valid": True,
                "provider_request_id": f"provider-request:{index}",
                "provider_idempotency_id": f"provider-idempotency:{index}",
                "output_tokens": 100,
            }
            for index, item in enumerate(accepted, start=1)
        ]
        return {
            "kind": "caption_exact_render_set/v1",
            "render_manifest_sha256": sha("render-manifest"),
            "provider_identity": "caption-provider:prod",
            "project_identity": "caption-project:vista",
            "model_snapshot": "provider-snapshot:20260721",
            "prompt_sha256": sha("caption-prompt"),
            "output_schema_sha256": sha("caption-output-schema"),
            "strict_parse": True,
            "records": records,
            "catalog_sha256": sha("catalog"),
            "usage": {
                "calls": 2,
                "input_tokens": 400,
                "output_tokens": 200,
                "cost_minor_units": 200,
                "currency": "USD",
                "minor_unit_exponent": 6,
                "rounding_mode": "ceiling_each_provider_charge_to_minor_unit",
                "retry_charge_policy": "all_provider_accepted_requests_counted",
                "charged_retry_count": 0,
            },
        }
    if phase == "embed":
        return {
            "kind": "embed_exact_text_set/v1",
            "catalog_sha256": sha("catalog"),
            "dense_model_revision": revision("dense"),
            "dense_artifact_manifest_sha256": sha("dense-artifact"),
            "sparse_model_revision": revision("sparse"),
            "sparse_artifact_manifest_sha256": sha("sparse-artifact"),
            "vector_bundle_manifest_sha256": sha("vectors"),
            "vector_set_sha256": sha("vector-set"),
            "records": [
                {
                    "asset_id": item["asset_id"],
                    "source_caption_sha256": sha("caption-record-" + item["asset_id"]),
                    "dense_vector_id": f"dense-vector:{index}",
                    "dense_vector_sha256": sha("dense-vector-" + item["asset_id"]),
                    "dense_dimensions": 8,
                    "sparse_vector_id": f"sparse-vector:{index}",
                    "sparse_vector_sha256": sha("sparse-vector-" + item["asset_id"]),
                    "sparse_nonzero_count": 4,
                    "finite": True,
                }
                for index, item in enumerate(accepted, start=1)
            ],
        }
    if phase == "postgres":
        storage = plan["storage"]["postgres"]
        return {
            "kind": "upsert_postgres_exact/v1",
            **{key: storage[key] for key in ("deployment_identity", "database", "schema_name", "schema_revision", "generation_id")},
            "transaction_marker": "postgres-transaction:fixture-001",
            "catalog_sha256": sha("catalog"),
            "rows": [
                {
                    "asset_id": item["asset_id"],
                    "row_sha256": sha("row-" + item["asset_id"]),
                    "catalog_record_sha256": sha("caption-record-" + item["asset_id"]),
                    "readback_sha256": sha("row-readback-" + item["asset_id"]),
                }
                for item in accepted
            ],
            "row_set_sha256": sha("rows"),
            "transaction_committed": True,
            "readback_complete": True,
        }
    if phase == "qdrant":
        storage = plan["storage"]["qdrant"]
        return {
            "kind": "upsert_qdrant_exact/v1",
            **{key: storage[key] for key in ("cluster_identity", "collection", "dense_vector_name", "sparse_vector_name", "generation_id", "point_id_policy", "same_id_replay_policy")},
            "points": [
                {
                    "asset_id": item["asset_id"],
                    "point_id": sha("point-id-" + item["asset_id"]),
                    "payload_sha256": sha("point-payload-" + item["asset_id"]),
                    "dense_vector_sha256": sha("dense-vector-" + item["asset_id"]),
                    "sparse_vector_sha256": sha("sparse-vector-" + item["asset_id"]),
                    "readback_sha256": sha("point-readback-" + item["asset_id"]),
                }
                for item in accepted
            ],
            "point_set_sha256": sha("points"),
            "readback_complete": True,
        }
    inspect_sha = prior_documents[0].expected_sha256
    queries = []
    for query in plan["acceptance"]["shadow_queries"]:
        expected = query["expected_asset_ids"][0]
        returned = [expected] + [item["asset_id"] for item in accepted if item["asset_id"] != expected]
        queries.append({**{key: query[key] for key in ("query_id", "language", "query_text_sha256", "normalization_revision", "expected_asset_ids", "minimum_rank")}, "returned_asset_ids": returned, "best_expected_rank": 1, "passed": True})
    return {
        "kind": "reconcile_exact_snapshot/v1",
        "authoritative_snapshot": {"schema": "simworld-asset-snapshot-exact-set/v2", "sha256": sha("authoritative"), "snapshot_revision": SNAPSHOT, "asset_set": copy.deepcopy(plan["expected_sets"]["assets"])},
        "compatibility_snapshot_v1_sha256": sha("compatibility"),
        "live_audit": {"schema": "simworld-asset-live-audit/v1", "sha256": sha("live-audit"), "manifest_sha256": sha("compatibility"), "observations_sha256": sha("live-observations"), "issued_at": "2026-07-21T11:52:30Z", "expires_at": "2026-07-21T12:07:30Z"},
        "catalog_sha256": outputs["caption"], "postgres_row_set_sha256": outputs["postgres"], "qdrant_point_set_sha256": outputs["qdrant"], "exact_set_parity": True,
        "runtime_attestations": [
            {
                "component": component,
                "target_identity": target_map[component][0],
                "runtime_image": target_map[component][1],
                "attestation_sha256": (
                    sha("worker-attestation")
                    if component == "worker"
                    else plan["target_attestations"][component]["runtime_attestation_sha256"]
                ),
            }
            for component in ("unreal", "worker", "embedding", "postgres", "qdrant")
        ],
        "query_results": queries,
        "smoke_results": [
            {
                "asset_id": item["asset_id"],
                "asset_type": item["asset_type"],
                "inspect_receipt_sha256": inspect_sha,
                "loaded": True,
                "spawned": True,
                "material_slots_verified": True,
                "pbr_textures_verified": True,
                "cleanup_state": "disposable_spawn_destroyed",
            }
            for item in accepted
        ],
        "previous_active": {"generation_id": None, "snapshot_revision": None, "ready": False},
        "rollback_readiness_receipt_sha256": approvals["rollback_readiness"].expected_sha256,
        "active_generation_pointer": {"identity": "semantic-active-pointer:prod", "observed_epoch": 0, "unchanged": True},
        "activation_performed": False,
    }


def evidence_output(value: dict[str, Any], phase: str) -> str:
    fields = {"inspect": "records_sha256", "render": "render_manifest_sha256", "caption": "catalog_sha256", "embed": "vector_bundle_manifest_sha256", "postgres": "row_set_sha256", "qdrant": "point_set_sha256"}
    return value["payload"][fields[phase]] if phase != "reconcile" else value["payload"]["authoritative_snapshot"]["sha256"]


def mutate_document(document: SealedDocument, mutate: Callable[[dict[str, Any]], None]) -> SealedDocument:
    value = copy.deepcopy(unseal(document))
    mutate(value)
    return seal(value)


def mutate_evidence(bundle: ProductionContractBundle, phase: str, mutate: Callable[[dict[str, Any]], None]) -> ProductionContractBundle:
    values = list(bundle.phase_evidence)
    index = PHASES.index(phase)
    value = copy.deepcopy(unseal(values[index]))
    value.pop("worker_signature", None)
    mutate(value)
    evidence_signature(value, phase)
    values[index] = seal(value)
    return dataclasses.replace(bundle, phase_evidence=tuple(values))


def mutate_approval(
    bundle: ProductionContractBundle,
    kind: str,
    mutate: Callable[[dict[str, Any]], None],
) -> ProductionContractBundle:
    receipt = copy.deepcopy(unseal(bundle.approvals[kind]))
    receipt.pop("signature", None)
    mutate(receipt)
    document = seal(signed_approval(receipt, kind))
    approvals = dict(bundle.approvals)
    approvals[kind] = document
    plan = copy.deepcopy(unseal(bundle.execution_plan))
    plan["approvals"][kind] = approval_ref(document)
    return dataclasses.replace(
        bundle, approvals=approvals, execution_plan=seal(plan)
    )
