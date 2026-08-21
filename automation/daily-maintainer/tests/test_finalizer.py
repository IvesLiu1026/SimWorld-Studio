from __future__ import annotations

import datetime as dt
import json
import unittest
from dataclasses import replace

from vista_daily_maintainer.candidate import (
    Candidate,
    CandidateSource,
    candidate_authorization_digest,
)
from vista_daily_maintainer.finalizer import (
    FINALIZED_ENVELOPE_SCHEMA,
    FinalizationError,
    finalize_verification,
    guard_report_digest,
    verified_head_digest,
)
from vista_daily_maintainer.guard import ChangedFile, GuardReport
from vista_daily_maintainer.patcher import PatcherRequest
from vista_daily_maintainer.publisher import (
    FinalizedEnvelopeReference,
    _freeze_verifier_envelope,
)
from vista_daily_maintainer.state import (
    BranchDisposition,
    Lifecycle,
    RunKey,
    RunState,
    StateContractError,
    state_digest,
)
from vista_daily_maintainer.verifier import (
    IsolationEvidence,
    ValidationResult,
    VerificationReport,
)


BASE_SHA = "a" * 40
BACKLOG_SHA256 = "b" * 64
PATCH_SHA256 = "c" * 64
RUN_DATE = dt.date(2026, 8, 21)
REPOSITORY = "IvesLiu1026/VISTA-World"
SLUG = "focused-contract-test"


def candidate(*, title: str = "Add one focused contract regression") -> Candidate:
    return Candidate(
        candidate_id="VW-DM-0001",
        title=title,
        risk_tier=0,
        allowed_paths=("tests/**",),
        acceptance=("One focused regression test passes offline.",),
        validation_profiles=("tools-python-offline",),
        expected_external_side_effects="none",
        source=CandidateSource(
            kind="curated_backlog",
            manifest_revision=7,
            approved_by="IvesLiu1026",
            issue_url=("https://github.com/IvesLiu1026/VISTA-World/issues/2"),
        ),
    )


def request(selected: Candidate | None = None) -> PatcherRequest:
    selected = selected or candidate()
    return PatcherRequest(
        run_date=RUN_DATE,
        repository=REPOSITORY,
        base_sha=BASE_SHA,
        backlog_sha256=BACKLOG_SHA256,
        manifest_revision=selected.source.manifest_revision,
        approved_by=selected.source.approved_by,
        candidate=selected,
        candidate_slug=SLUG,
        candidate_sha256=candidate_authorization_digest(selected),
    )


def run_state(value: PatcherRequest | None = None) -> RunState:
    value = value or request()
    return RunState(
        key=RunKey(value.run_date.isoformat(), value.repository, value.base_sha),
        candidate_id=value.candidate.candidate_id,
        candidate_slug=value.candidate_slug,
        backlog_sha256=value.backlog_sha256,
        candidate_sha256=value.candidate_sha256,
        remote="origin",
        remote_branch="main",
        branch_name=(
            f"codex/daily/{value.run_date.isoformat()}-{value.candidate_slug}-"
            f"{value.base_sha[:8]}"
        ),
        lifecycle=Lifecycle.WORKTREE_READY,
        branch_disposition=BranchDisposition.CREATED,
        branch_head_sha=value.base_sha,
        worktree_path=(
            f"/var/lib/vista-world/worktrees/{value.run_date.isoformat()}-"
            f"{value.candidate_slug}-{value.base_sha[:12]}"
        ),
        observed_remote_sha=value.base_sha,
    )


def guard(*, patch_sha256: str = PATCH_SHA256) -> GuardReport:
    return GuardReport(
        base_sha=BASE_SHA,
        patch_sha256=patch_sha256,
        changed_files=(
            ChangedFile(
                path="tests/test_contract.py",
                status="M",
                additions=5,
                deletions=0,
                is_test=True,
            ),
        ),
        violations=(),
        production_files=0,
        production_lines=0,
        test_files=1,
        test_lines=5,
    )


def report(value: PatcherRequest | None = None) -> VerificationReport:
    value = value or request()
    snapshot = guard()
    return VerificationReport(
        candidate_sha256=value.candidate_sha256,
        guard=snapshot,
        final_guard=snapshot,
        validation=(
            ValidationResult("git-diff-check", 0, "d" * 64, 10),
            ValidationResult("tools-python-offline", 0, "e" * 64, 20),
        ),
        isolation_evidence=IsolationEvidence(
            network_isolated=True,
            credentials_absent=True,
            observed_by="outer-sandbox-controller",
            evidence_sha256="f" * 64,
        ),
    )


class FinalizerTests(unittest.TestCase):
    def test_exact_artifacts_emit_stable_canonical_envelope(self) -> None:
        patcher_request = request()
        state = run_state(patcher_request)
        verification = report(patcher_request)

        envelope = finalize_verification(patcher_request, state, verification)
        payload = json.loads(envelope.canonical_bytes)

        self.assertEqual(envelope.schema_version, FINALIZED_ENVELOPE_SCHEMA)
        self.assertTrue(envelope.finalized)
        self.assertEqual(payload["backlog_sha256"], BACKLOG_SHA256)
        self.assertEqual(payload["candidate_sha256"], patcher_request.candidate_sha256)
        self.assertEqual(payload["run_state_sha256"], state_digest(state))
        self.assertEqual(payload["branch_name"], state.branch_name)
        self.assertEqual(
            payload["final_guard_sha256"], guard_report_digest(verification.final_guard)
        )
        self.assertEqual(
            payload["head_sha256"],
            verified_head_digest(
                BASE_SHA,
                PATCH_SHA256,
                ("tests/test_contract.py",),
            ),
        )
        self.assertEqual(
            envelope.canonical_bytes,
            finalize_verification(patcher_request, state, verification).canonical_bytes,
        )
        frozen = _freeze_verifier_envelope(
            envelope,
            FinalizedEnvelopeReference("typed-finalizer-test", envelope.sha256),
        )
        self.assertEqual(frozen.run_state_sha256, state_digest(state))
        self.assertEqual(frozen.branch, state.branch_name)

    def test_swapped_backlog_candidate_run_slug_and_report_fail_closed(self) -> None:
        original = request()
        state = run_state(original)
        verification = report(original)
        changed_candidate = candidate(title="A different reviewed candidate title")
        changed_request = request(changed_candidate)
        cases = (
            (
                "backlog digest",
                replace(original, backlog_sha256="9" * 64),
                state,
                verification,
            ),
            ("candidate", changed_request, state, verification),
            (
                "run state",
                original,
                replace(state, candidate_sha256="8" * 64),
                verification,
            ),
            (
                "candidate slug",
                replace(original, candidate_slug="other-contract-test"),
                state,
                verification,
            ),
            (
                "verification report",
                original,
                state,
                replace(verification, candidate_sha256="7" * 64),
            ),
        )
        for label, patcher_request, candidate_state, candidate_report in cases:
            with self.subTest(label=label), self.assertRaises(FinalizationError):
                finalize_verification(
                    patcher_request,
                    candidate_state,
                    candidate_report,
                )

    def test_branch_swap_is_rejected_by_state_contract(self) -> None:
        with self.assertRaisesRegex(StateContractError, "daily branch"):
            replace(
                run_state(),
                branch_name=("codex/daily/2026-08-21-other-contract-test-aaaaaaaa"),
            )

    def test_patch_or_changed_path_swap_inside_report_fails_closed(self) -> None:
        patcher_request = request()
        state = run_state(patcher_request)
        verification = report(patcher_request)
        changed_file = replace(
            verification.final_guard.changed_files[0],
            path="tests/test_other_contract.py",
        )
        cases = (
            replace(
                verification,
                final_guard=guard(patch_sha256="9" * 64),
            ),
            replace(
                verification,
                final_guard=replace(
                    verification.final_guard,
                    changed_files=(changed_file,),
                ),
            ),
        )
        for changed_report in cases:
            with self.assertRaises(FinalizationError):
                finalize_verification(patcher_request, state, changed_report)

    def test_check_profile_swap_fails_closed(self) -> None:
        patcher_request = request()
        state = run_state(patcher_request)
        verification = report(patcher_request)
        changed = replace(
            verification,
            validation=(
                verification.validation[0],
                replace(
                    verification.validation[1],
                    command_id="daily-maintainer-core-tests",
                ),
            ),
        )
        with self.assertRaisesRegex(FinalizationError, "candidate profiles"):
            finalize_verification(patcher_request, state, changed)


if __name__ == "__main__":
    unittest.main()
