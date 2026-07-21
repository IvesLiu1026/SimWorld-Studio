#!/usr/bin/env python3
"""Typed phase adapter contract for the sealed semantic-index executor.

This module intentionally contains no production adapter.  The only registered
implementation is an offline fixture that performs deterministic arithmetic on
an already validated job.  It never opens a socket, starts Unreal, calls a
provider, reads credentials, or writes PostgreSQL/Qdrant.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


PHASES = (
    "inspect",
    "render",
    "caption",
    "embed",
    "postgres",
    "qdrant",
    "reconcile",
)

PHASE_METRIC_KEYS: Mapping[str, frozenset[str]] = {
    "inspect": frozenset({"assets_inspected"}),
    "render": frozenset({"assets_rendered", "rendered_views"}),
    "caption": frozenset({"catalog_records"}),
    "embed": frozenset({"dense_vectors", "sparse_vectors"}),
    "postgres": frozenset({"postgres_rows"}),
    "qdrant": frozenset({"qdrant_points"}),
    "reconcile": frozenset(
        {"catalog_records", "postgres_rows", "qdrant_points"}
    ),
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SecretSnapshot:
    """Immutable in-memory credential bytes captured once by the executor."""

    caption: bytes | None = field(repr=False)
    postgres: bytes | None = field(repr=False)
    qdrant: bytes | None = field(repr=False)
    embedding: bytes | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class PhaseRequest:
    phase: str
    job: Mapping[str, Any]
    execution_plan: Mapping[str, Any]
    idempotency_key: str
    deadline_monotonic_ns: int
    secret_snapshot: SecretSnapshot


@dataclass(frozen=True, slots=True)
class PhaseResult:
    phase: str
    idempotency_key: str
    metrics: Mapping[str, int]
    evidence_sha256: str
    observed_live: bool
    snapshot_revision: str | None = None
    live_audit_receipt_sha256: str | None = None
    observed_runtime_images: Mapping[str, str] | None = None
    credential_target_identities: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class SemanticIndexPhaseAdapter(Protocol):
    """Minimum reviewed boundary for a future concrete production adapter."""

    adapter_id: str
    adapter_contract_revision: str
    production_capable: bool

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        """Run one allowlisted idempotent phase before the supplied deadline."""


class OfflineFixtureAdapter:
    """Deterministic non-production adapter used only to test orchestration."""

    adapter_id = "offline-fixture-v1"
    adapter_contract_revision = "semantic-index-offline-fixture-adapter/v1"
    production_capable = False
    __slots__ = ()

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        if request.phase not in PHASES:
            raise ValueError("phase is not allowlisted")
        if time.monotonic_ns() >= request.deadline_monotonic_ns:
            raise TimeoutError("phase deadline expired")
        if any(
            value is not None
            for value in (
                request.secret_snapshot.caption,
                request.secret_snapshot.postgres,
                request.secret_snapshot.qdrant,
                request.secret_snapshot.embedding,
            )
        ):
            raise ValueError("offline fixture does not accept credential files")

        job = request.job
        count = int(job["pending_objects"]["count"])
        views = int(job["recipe_contract"]["caption"]["views_per_asset"])
        metrics: dict[str, int]
        if request.phase == "inspect":
            metrics = {"assets_inspected": count}
        elif request.phase == "render":
            metrics = {
                "assets_rendered": count,
                "rendered_views": count * views,
            }
        elif request.phase == "caption":
            metrics = {"catalog_records": count}
        elif request.phase == "embed":
            metrics = {"dense_vectors": count, "sparse_vectors": count}
        elif request.phase == "postgres":
            metrics = {"postgres_rows": count}
        elif request.phase == "qdrant":
            metrics = {"qdrant_points": count}
        else:
            metrics = {
                "catalog_records": count,
                "postgres_rows": count,
                "qdrant_points": count,
            }

        evidence_basis = {
            "schema": "simworld-semantic-index-offline-fixture-evidence/v1",
            "job_revision": job["job_revision"],
            "adapter_contract_revision": self.adapter_contract_revision,
            "phase": request.phase,
            "idempotency_key": request.idempotency_key,
            "metrics": metrics,
            "observed_live": False,
        }
        return PhaseResult(
            phase=request.phase,
            idempotency_key=request.idempotency_key,
            metrics=metrics,
            evidence_sha256=hashlib.sha256(_canonical_json(evidence_basis)).hexdigest(),
            observed_live=False,
            snapshot_revision=(
                job["snapshot_target"]["asset_snapshot_revision"]
                if request.phase == "reconcile"
                else None
            ),
            live_audit_receipt_sha256=None,
            observed_runtime_images=None,
            credential_target_identities={},
        )


def registered_adapters() -> Mapping[str, SemanticIndexPhaseAdapter]:
    """Return the exact reviewed adapter registry.

    Adding a production-capable entry is deliberately a code-review event.
    Dynamic imports, entry points, filesystem discovery, and legacy-runner
    wrappers are not supported.
    """

    fixture = OfflineFixtureAdapter()
    return {fixture.adapter_id: fixture}
