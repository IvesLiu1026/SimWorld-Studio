from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from .candidate import (
    Candidate,
    candidate_authorization_digest,
    enforce_v1_candidate_policy,
    path_matches_pattern,
)
from .guard import GuardReport
from .naming import v1_daily_branch_name
from .patcher import PATCHER_REPOSITORY, PatcherRequest
from .state import (
    BranchDisposition,
    Lifecycle,
    PullRequestState,
    RunState,
    state_digest,
)
from .verifier import IsolationEvidence, VerificationReport, ValidationResult


FINALIZED_ENVELOPE_SCHEMA = "vista.world.daily-maintainer.finalized-verification.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMAND_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")


class FinalizationError(ValueError):
    """The three trusted stage artifacts do not describe one exact run."""


@dataclass(frozen=True)
class FinalizedVerificationCheck:
    command_id: str
    output_sha256: str
    exit_code: int
    timed_out: bool

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not _COMMAND_ID.fullmatch(
            self.command_id
        ):
            raise FinalizationError("finalized command ID is invalid")
        _require_sha256(self.output_sha256, "verification output digest")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise FinalizationError("verification exit code is invalid")
        if not isinstance(self.timed_out, bool):
            raise FinalizationError("verification timeout flag is invalid")


@dataclass(frozen=True)
class FinalizedVerifierEnvelope:
    """Immutable, canonical bridge from local verification to publication."""

    run_id: str
    run_date: str
    repository: str
    base_sha: str
    branch_name: str
    backlog_sha256: str
    candidate_sha256: str
    run_state_sha256: str
    run_remote: str
    run_remote_branch: str
    run_lifecycle: str
    run_branch_disposition: str
    run_branch_head_sha: str
    run_worktree_path: str
    run_observed_remote_sha: str
    run_publication_state: str
    candidate_id: str
    candidate_slug: str
    candidate_title: str
    risk_tier: int
    acceptance: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    validation_profile_ids: tuple[str, ...]
    expected_external_side_effects: str
    candidate_state: str
    candidate_not_before: str | None
    candidate_expires_on: str | None
    source_kind: str
    source_manifest_revision: int
    source_approved_by: str
    source_issue_url: str | None
    guard_ok: bool
    guard_patch_sha256: str
    final_guard_ok: bool
    final_guard_patch_sha256: str
    final_guard_sha256: str
    head_sha256: str
    mutation_detected: bool
    isolation_network_isolated: bool
    isolation_credentials_absent: bool
    isolation_verified_by: str
    isolation_evidence_sha256: str
    checks: tuple[FinalizedVerificationCheck, ...]
    schema_version: str = FINALIZED_ENVELOPE_SCHEMA
    finalized: bool = True

    def __post_init__(self) -> None:
        if (
            self.schema_version != FINALIZED_ENVELOPE_SCHEMA
            or self.finalized is not True
        ):
            raise FinalizationError("finalized envelope schema is invalid")
        for value, label in (
            (self.backlog_sha256, "backlog digest"),
            (self.candidate_sha256, "candidate digest"),
            (self.run_state_sha256, "run state digest"),
            (self.guard_patch_sha256, "guard patch digest"),
            (self.final_guard_patch_sha256, "final guard patch digest"),
            (self.final_guard_sha256, "final guard digest"),
            (self.head_sha256, "head digest"),
            (self.isolation_evidence_sha256, "isolation evidence digest"),
        ):
            _require_sha256(value, label)
        if not isinstance(self.checks, tuple) or not self.checks:
            raise FinalizationError("finalized checks must be non-empty")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(_envelope_payload(self))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


class VerificationFinalizer:
    """Fail-closed authority join for one patcher, run, and verifier result."""

    @staticmethod
    def finalize(
        request: PatcherRequest,
        run_state: RunState,
        report: VerificationReport,
    ) -> FinalizedVerifierEnvelope:
        return finalize_verification(request, run_state, report)


def finalize_verification(
    request: PatcherRequest,
    run_state: RunState,
    report: VerificationReport,
) -> FinalizedVerifierEnvelope:
    if not isinstance(request, PatcherRequest):
        raise FinalizationError("exact patcher request is required")
    if not isinstance(run_state, RunState):
        raise FinalizationError("exact run state is required")
    if not isinstance(report, VerificationReport):
        raise FinalizationError("exact verification report is required")

    candidate = request.candidate
    enforce_v1_candidate_policy(candidate)
    candidate_sha256 = candidate_authorization_digest(candidate)
    if request.candidate_sha256 != candidate_sha256:
        raise FinalizationError("patcher request candidate digest changed")

    _validate_run_binding(request, run_state, candidate_sha256)
    _validate_report_binding(request, run_state, report, candidate)

    changed_paths = tuple(item.path for item in report.final_guard.changed_files)
    checks = tuple(_copy_check(item) for item in report.validation)
    final_guard_sha256 = guard_report_digest(report.final_guard)
    head_sha256 = verified_head_digest(
        report.final_guard.base_sha,
        report.final_guard.patch_sha256,
        changed_paths,
    )
    source = candidate.source
    envelope = FinalizedVerifierEnvelope(
        run_id=run_state.key.run_id,
        run_date=run_state.key.run_date,
        repository=run_state.key.repository,
        base_sha=run_state.key.base_sha,
        branch_name=run_state.branch_name,
        backlog_sha256=request.backlog_sha256,
        candidate_sha256=candidate_sha256,
        run_state_sha256=state_digest(run_state),
        run_remote=run_state.remote,
        run_remote_branch=run_state.remote_branch,
        run_lifecycle=run_state.lifecycle.value,
        run_branch_disposition=run_state.branch_disposition.value,
        run_branch_head_sha=run_state.branch_head_sha or "",
        run_worktree_path=run_state.worktree_path or "",
        run_observed_remote_sha=run_state.observed_remote_sha or "",
        run_publication_state=run_state.publication.state.value,
        candidate_id=candidate.candidate_id,
        candidate_slug=request.candidate_slug,
        candidate_title=candidate.title,
        risk_tier=candidate.risk_tier,
        acceptance=candidate.acceptance,
        allowed_paths=candidate.allowed_paths,
        changed_paths=changed_paths,
        validation_profile_ids=candidate.validation_profiles,
        expected_external_side_effects=candidate.expected_external_side_effects,
        candidate_state=candidate.state,
        candidate_not_before=(
            candidate.not_before.isoformat() if candidate.not_before else None
        ),
        candidate_expires_on=(
            candidate.expires_on.isoformat() if candidate.expires_on else None
        ),
        source_kind=source.kind,
        source_manifest_revision=source.manifest_revision,
        source_approved_by=source.approved_by,
        source_issue_url=source.issue_url,
        guard_ok=report.guard.ok,
        guard_patch_sha256=report.guard.patch_sha256,
        final_guard_ok=report.final_guard.ok,
        final_guard_patch_sha256=report.final_guard.patch_sha256,
        final_guard_sha256=final_guard_sha256,
        head_sha256=head_sha256,
        mutation_detected=report.mutation_detected,
        isolation_network_isolated=report.isolation_evidence.network_isolated,
        isolation_credentials_absent=report.isolation_evidence.credentials_absent,
        isolation_verified_by=report.isolation_evidence.observed_by,
        isolation_evidence_sha256=report.isolation_evidence.evidence_sha256,
        checks=checks,
    )
    # Exercise canonical encoding before returning the artifact.  This turns any
    # unexpected Unicode/JSON regression into a finalization failure, not a
    # publisher-time surprise.
    _ = envelope.canonical_bytes
    return envelope


def _validate_run_binding(
    request: PatcherRequest,
    run_state: RunState,
    candidate_sha256: str,
) -> None:
    expected_branch = v1_daily_branch_name(
        request.run_date.isoformat(),
        request.candidate_slug,
        request.base_sha,
    )
    mismatches = (
        (run_state.key.run_date, request.run_date.isoformat(), "run date"),
        (run_state.key.repository, request.repository, "repository"),
        (run_state.key.base_sha, request.base_sha, "base SHA"),
        (run_state.candidate_id, request.candidate.candidate_id, "candidate ID"),
        (run_state.candidate_slug, request.candidate_slug, "candidate slug"),
        (run_state.backlog_sha256, request.backlog_sha256, "backlog digest"),
        (run_state.candidate_sha256, candidate_sha256, "candidate digest"),
        (run_state.branch_name, expected_branch, "daily branch"),
    )
    for observed, expected, label in mismatches:
        if observed != expected:
            raise FinalizationError(f"run state {label} does not match request")
    if request.repository != PATCHER_REPOSITORY:
        raise FinalizationError("run repository is not canonical")
    if run_state.remote != "origin" or run_state.remote_branch != "main":
        raise FinalizationError("run state remote identity is not canonical")
    if (
        run_state.lifecycle is not Lifecycle.WORKTREE_READY
        or run_state.branch_disposition is not BranchDisposition.CREATED
        or not run_state.worktree_path
    ):
        raise FinalizationError("run state is not a prepared isolated worktree")
    if run_state.branch_head_sha != request.base_sha:
        raise FinalizationError("run state branch head is not the pinned base")
    if run_state.observed_remote_sha != request.base_sha:
        raise FinalizationError("run state remote observation is not the pinned base")
    if run_state.publication.state not in {
        PullRequestState.UNKNOWN,
        PullRequestState.NONE,
    }:
        raise FinalizationError("run state already contains publication evidence")


def _validate_report_binding(
    request: PatcherRequest,
    run_state: RunState,
    report: VerificationReport,
    candidate: Candidate,
) -> None:
    if not isinstance(report.guard, GuardReport) or not isinstance(
        report.final_guard, GuardReport
    ):
        raise FinalizationError("verification guard evidence has an invalid type")
    if not isinstance(report.validation, tuple) or any(
        not isinstance(item, ValidationResult) for item in report.validation
    ):
        raise FinalizationError("verification checks have an invalid type")
    if not isinstance(report.isolation_evidence, IsolationEvidence):
        raise FinalizationError("verification isolation evidence has an invalid type")
    if report.candidate_sha256 != request.candidate_sha256:
        raise FinalizationError("verification report candidate digest does not match")
    if report.publication_authorized is not False:
        raise FinalizationError("verification report may not authorize publication")
    if not report.checks_passed:
        raise FinalizationError("verification report did not pass all checks")
    if report.mutation_detected:
        raise FinalizationError("verification report detected patch mutation")
    if report.guard.base_sha != request.base_sha or report.final_guard.base_sha != (
        request.base_sha
    ):
        raise FinalizationError("verification report base SHA does not match run")
    if report.guard.patch_sha256 != report.final_guard.patch_sha256:
        raise FinalizationError("initial and final patch digests differ")
    if report.guard.changed_files != report.final_guard.changed_files:
        raise FinalizationError("initial and final changed-file evidence differs")

    changed_paths = tuple(item.path for item in report.final_guard.changed_files)
    if not changed_paths or changed_paths != tuple(sorted(set(changed_paths))):
        raise FinalizationError("verified changed paths are not sorted and unique")
    for path in changed_paths:
        if not any(
            path_matches_pattern(path, pattern) for pattern in candidate.allowed_paths
        ):
            raise FinalizationError("verified changed path exceeds candidate authority")

    command_ids = tuple(item.command_id for item in report.validation)
    expected_commands = ("git-diff-check", *candidate.validation_profiles)
    if command_ids != expected_commands:
        raise FinalizationError("verification checks do not match candidate profiles")
    if any(not item.ok for item in report.validation):
        raise FinalizationError("verification report contains a failed check")
    if (
        report.isolation_evidence.network_isolated is not True
        or report.isolation_evidence.credentials_absent is not True
    ):
        raise FinalizationError("verification isolation evidence did not pass")
    if run_state.candidate_sha256 != report.candidate_sha256:
        raise FinalizationError("run state and verification candidate digests differ")


def _copy_check(value: ValidationResult) -> FinalizedVerificationCheck:
    if not isinstance(value, ValidationResult):
        raise FinalizationError("verification check has an invalid type")
    return FinalizedVerificationCheck(
        command_id=value.command_id,
        output_sha256=value.output_sha256,
        exit_code=value.exit_code,
        timed_out=value.timed_out,
    )


def guard_report_digest(report: GuardReport) -> str:
    if not isinstance(report, GuardReport):
        raise FinalizationError("final guard report has an invalid type")
    payload = {
        "schema_version": "vista.world.daily-maintainer.guard-report.v1",
        "base_sha": report.base_sha,
        "patch_sha256": report.patch_sha256,
        "changed_files": [asdict(item) for item in report.changed_files],
        "violations": [asdict(item) for item in report.violations],
        "production_files": report.production_files,
        "production_lines": report.production_lines,
        "test_files": report.test_files,
        "test_lines": report.test_lines,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def verified_head_digest(
    base_sha: str,
    patch_sha256: str,
    changed_paths: tuple[str, ...],
) -> str:
    """Derive the normalized pre-commit head identity from verified patch facts."""

    _require_sha256(patch_sha256, "verified patch digest")
    payload = {
        "schema_version": "vista.world.daily-maintainer.verified-head.v1",
        "base_sha": base_sha,
        "patch_sha256": patch_sha256,
        "changed_paths": list(changed_paths),
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _envelope_payload(envelope: FinalizedVerifierEnvelope) -> dict[str, object]:
    return {
        "schema_version": envelope.schema_version,
        "finalized": envelope.finalized,
        "run_id": envelope.run_id,
        "run_date": envelope.run_date,
        "repository": envelope.repository,
        "base_sha": envelope.base_sha,
        "branch_name": envelope.branch_name,
        "backlog_sha256": envelope.backlog_sha256,
        "candidate_sha256": envelope.candidate_sha256,
        "run_state_sha256": envelope.run_state_sha256,
        "run_remote": envelope.run_remote,
        "run_remote_branch": envelope.run_remote_branch,
        "run_lifecycle": envelope.run_lifecycle,
        "run_branch_disposition": envelope.run_branch_disposition,
        "run_branch_head_sha": envelope.run_branch_head_sha,
        "run_worktree_path": envelope.run_worktree_path,
        "run_observed_remote_sha": envelope.run_observed_remote_sha,
        "run_publication_state": envelope.run_publication_state,
        "candidate_id": envelope.candidate_id,
        "candidate_slug": envelope.candidate_slug,
        "candidate_title": envelope.candidate_title,
        "risk_tier": envelope.risk_tier,
        "acceptance": list(envelope.acceptance),
        "allowed_paths": list(envelope.allowed_paths),
        "changed_paths": list(envelope.changed_paths),
        "validation_profile_ids": list(envelope.validation_profile_ids),
        "expected_external_side_effects": envelope.expected_external_side_effects,
        "candidate_state": envelope.candidate_state,
        "candidate_not_before": envelope.candidate_not_before,
        "candidate_expires_on": envelope.candidate_expires_on,
        "source_kind": envelope.source_kind,
        "source_manifest_revision": envelope.source_manifest_revision,
        "source_approved_by": envelope.source_approved_by,
        "source_issue_url": envelope.source_issue_url,
        "guard_ok": envelope.guard_ok,
        "guard_patch_sha256": envelope.guard_patch_sha256,
        "final_guard_ok": envelope.final_guard_ok,
        "final_guard_patch_sha256": envelope.final_guard_patch_sha256,
        "final_guard_sha256": envelope.final_guard_sha256,
        "head_sha256": envelope.head_sha256,
        "mutation_detected": envelope.mutation_detected,
        "isolation_network_isolated": envelope.isolation_network_isolated,
        "isolation_credentials_absent": envelope.isolation_credentials_absent,
        "isolation_verified_by": envelope.isolation_verified_by,
        "isolation_evidence_sha256": envelope.isolation_evidence_sha256,
        "checks": [asdict(item) for item in envelope.checks],
    }


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FinalizationError("finalized evidence is not canonical JSON") from exc


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise FinalizationError(f"{label} must be lowercase SHA-256")
    return value
