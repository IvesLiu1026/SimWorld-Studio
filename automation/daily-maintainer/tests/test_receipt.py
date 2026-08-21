from __future__ import annotations

import json
import unittest

from vista_daily_maintainer.receipt import (
    DiffSummary,
    GitIdentity,
    ReceiptActors,
    ReceiptContractError,
    RunReceipt,
    RunStatus,
    ValidationReceipt,
    journal_entry,
    journal_marker,
    parse_receipt,
    receipt_digest,
    serialize_receipt,
    validate_status_transition,
)


HEX_A = "a" * 40
HEX_B = "b" * 40
HEX_C = "c" * 64


def make_actors(*, promoted: bool = False) -> ReceiptActors:
    return ReceiptActors(
        commit_author=GitIdentity(
            name="Ives Liu",
            email="zhiy0517xiang@gmail.com",
        ),
        git_committer=GitIdentity(
            name="VISTA World Publisher",
            email="publisher@users.noreply.github.com",
        ),
        pr_actor="vista-world-publisher[bot]",
        promotion_actor="github-actions[bot]" if promoted else None,
    )


def make_pr_receipt(status: RunStatus = RunStatus.PR_OPEN) -> RunReceipt:
    return RunReceipt(
        run_id=f"2026-08-21/IvesLiu1026/VISTA-World@{HEX_A}",
        run_date="2026-08-21",
        repository="IvesLiu1026/VISTA-World",
        status=status,
        base_sha=HEX_A,
        head_sha=HEX_B,
        candidate_id="VW-DM-0001",
        validation=(
            ValidationReceipt(
                command_id="daily-maintainer-core-tests",
                exit_code=0,
                output_sha256=HEX_C,
                duration_ms=42,
            ),
        ),
        diff_summary=DiffSummary(
            files_changed=2,
            production_lines=8,
            test_lines=12,
            patch_sha256=HEX_C,
        ),
        protected_paths_touched=(),
        pr_url="https://github.com/IvesLiu1026/VISTA-World/pull/1",
        merge_sha=HEX_B if status is RunStatus.MERGED else None,
        duration_ms=1250,
        failure_category=None,
        actors=make_actors(promoted=status is RunStatus.MERGED),
    )


class ReceiptContractTests(unittest.TestCase):
    def test_canonical_round_trip_and_digest(self) -> None:
        receipt = make_pr_receipt()
        payload = serialize_receipt(receipt)
        parsed = parse_receipt(payload)
        self.assertEqual(parsed, receipt)
        self.assertEqual(receipt_digest(receipt), receipt_digest(parsed))
        self.assertEqual(payload, serialize_receipt(parsed))
        self.assertTrue(payload.endswith(b"\n"))

    def test_unknown_or_raw_output_fields_are_rejected(self) -> None:
        payload = json.loads(serialize_receipt(make_pr_receipt()))
        payload["validation"][0]["stdout"] = "github_pat_secret"
        with self.assertRaisesRegex(ReceiptContractError, "unknown fields"):
            parse_receipt(json.dumps(payload).encode())

    def test_no_change_cannot_claim_patch_or_pr(self) -> None:
        with self.assertRaisesRegex(ReceiptContractError, "no_change"):
            RunReceipt(
                run_id=f"2026-08-21/IvesLiu1026/VISTA-World@{HEX_A}",
                run_date="2026-08-21",
                repository="IvesLiu1026/VISTA-World",
                status=RunStatus.NO_CHANGE,
                base_sha=HEX_A,
                head_sha=HEX_B,
                candidate_id="VW-DM-0001",
                validation=(),
                diff_summary=None,
                protected_paths_touched=(),
                pr_url=None,
                merge_sha=None,
                duration_ms=10,
                failure_category=None,
                actors=ReceiptActors(),
            )

    def test_failed_receipt_requires_failure_category(self) -> None:
        with self.assertRaisesRegex(ReceiptContractError, "failure_category"):
            RunReceipt(
                run_id=f"2026-08-21/IvesLiu1026/VISTA-World@{HEX_A}",
                run_date="2026-08-21",
                repository="IvesLiu1026/VISTA-World",
                status=RunStatus.VALIDATION_FAILED,
                base_sha=HEX_A,
                head_sha=None,
                candidate_id="VW-DM-0001",
                validation=(),
                diff_summary=None,
                protected_paths_touched=(),
                pr_url=None,
                merge_sha=None,
                duration_ms=10,
                failure_category=None,
                actors=ReceiptActors(),
            )

    def test_validation_failed_requires_a_failed_result(self) -> None:
        with self.assertRaisesRegex(ReceiptContractError, "failed result"):
            RunReceipt(
                run_id=f"2026-08-21/IvesLiu1026/VISTA-World@{HEX_A}",
                run_date="2026-08-21",
                repository="IvesLiu1026/VISTA-World",
                status=RunStatus.VALIDATION_FAILED,
                base_sha=HEX_A,
                head_sha=None,
                candidate_id="VW-DM-0001",
                validation=(
                    ValidationReceipt(
                        command_id="focused-tests",
                        exit_code=0,
                        output_sha256=HEX_C,
                        duration_ms=1,
                    ),
                ),
                diff_summary=DiffSummary(
                    files_changed=1,
                    production_lines=1,
                    test_lines=0,
                    patch_sha256=HEX_C,
                ),
                protected_paths_touched=(),
                pr_url=None,
                merge_sha=None,
                duration_ms=10,
                failure_category="test_failed",
                actors=ReceiptActors(),
            )

    def test_run_identity_must_bind_base_sha(self) -> None:
        with self.assertRaisesRegex(ReceiptContractError, "base SHA"):
            RunReceipt(
                run_id="2026-08-21/IvesLiu1026/VISTA-World",
                run_date="2026-08-21",
                repository="IvesLiu1026/VISTA-World",
                status=RunStatus.NO_CHANGE,
                base_sha=HEX_A,
                head_sha=None,
                candidate_id=None,
                validation=(),
                diff_summary=None,
                protected_paths_touched=(),
                pr_url=None,
                merge_sha=None,
                duration_ms=1,
                failure_category=None,
                actors=ReceiptActors(),
            )

    def test_pr_receipt_requires_recorded_author_committer_and_actor(self) -> None:
        receipt = make_pr_receipt()
        values = dict(receipt.__dict__)
        values["actors"] = ReceiptActors()
        with self.assertRaisesRegex(ReceiptContractError, "actors"):
            RunReceipt(**values)

    def test_status_transition_contract_rejects_regression(self) -> None:
        validate_status_transition(RunStatus.PR_OPEN, RunStatus.MERGED)
        with self.assertRaisesRegex(ReceiptContractError, "transition"):
            validate_status_transition(RunStatus.MERGED, RunStatus.PR_OPEN)

    def test_journal_marker_is_exact_and_entry_binds_digest(self) -> None:
        receipt = make_pr_receipt()
        marker = journal_marker(receipt.run_date, receipt.repository)
        entry = journal_entry(receipt)
        self.assertEqual(
            marker,
            "<!-- vista-daily-receipt:2026-08-21:IvesLiu1026/VISTA-World -->",
        )
        tick = chr(96)
        self.assertIn(marker, entry)
        self.assertIn(
            f"receipt_sha256: {tick}{receipt_digest(receipt)}{tick}",
            entry,
        )
        self.assertIn(
            f"pr_actor: {tick}vista-world-publisher[bot]{tick}",
            entry,
        )
        self.assertNotIn("stdout", entry)

    def test_invalid_repository_cannot_break_comment_marker(self) -> None:
        with self.assertRaisesRegex(ReceiptContractError, "repository"):
            journal_marker("2026-08-21", "owner/repo --> injected")


if __name__ == "__main__":
    unittest.main()
