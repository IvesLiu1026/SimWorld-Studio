from __future__ import annotations

import json
import unittest

from vista_daily_maintainer.receipt import (
    MAX_RECEIPT_BYTES,
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

    def test_every_status_has_an_explicit_valid_shape(self) -> None:
        common = {
            "run_id": f"2026-08-21/IvesLiu1026/VISTA-World@{HEX_A}",
            "run_date": "2026-08-21",
            "repository": "IvesLiu1026/VISTA-World",
            "base_sha": HEX_A,
            "duration_ms": 10,
        }
        empty = {
            **common,
            "head_sha": None,
            "candidate_id": None,
            "validation": (),
            "diff_summary": None,
            "protected_paths_touched": (),
            "pr_url": None,
            "merge_sha": None,
            "actors": ReceiptActors(),
        }
        for status in (RunStatus.SKIPPED, RunStatus.NO_CHANGE):
            with self.subTest(status=status):
                self.assertEqual(
                    RunReceipt(
                        **empty,
                        status=status,
                        failure_category=None,
                    ).status,
                    status,
                )

        diff = DiffSummary(
            files_changed=1,
            production_lines=2,
            test_lines=3,
            patch_sha256=HEX_C,
        )
        failed = ValidationReceipt(
            command_id="focused-test",
            exit_code=1,
            output_sha256=HEX_C,
            duration_ms=2,
        )
        patch_rejected = RunReceipt(
            **common,
            status=RunStatus.PATCH_REJECTED,
            head_sha=None,
            candidate_id="VW-DM-0001",
            validation=(),
            diff_summary=diff,
            protected_paths_touched=("docs/.npmrc",),
            pr_url=None,
            merge_sha=None,
            failure_category="policy_rejected",
            actors=ReceiptActors(),
        )
        validation_failed = RunReceipt(
            **common,
            status=RunStatus.VALIDATION_FAILED,
            head_sha=None,
            candidate_id="VW-DM-0001",
            validation=(failed,),
            diff_summary=diff,
            protected_paths_touched=(),
            pr_url=None,
            merge_sha=None,
            failure_category="test_failed",
            actors=ReceiptActors(),
        )
        infrastructure_failed = RunReceipt(
            **empty,
            status=RunStatus.INFRASTRUCTURE_FAILED,
            failure_category="preflight_failed",
        )
        halted_values = dict(make_pr_receipt().__dict__)
        halted_values.update(
            status=RunStatus.HALTED,
            failure_category="failure_threshold",
        )
        halted = RunReceipt(**halted_values)
        self.assertEqual(patch_rejected.status, RunStatus.PATCH_REJECTED)
        self.assertEqual(validation_failed.status, RunStatus.VALIDATION_FAILED)
        self.assertEqual(infrastructure_failed.status, RunStatus.INFRASTRUCTURE_FAILED)
        self.assertEqual(make_pr_receipt().status, RunStatus.PR_OPEN)
        self.assertEqual(make_pr_receipt(RunStatus.MERGED).status, RunStatus.MERGED)
        self.assertEqual(halted.status, RunStatus.HALTED)

    def test_halted_cannot_retain_merge_sha(self) -> None:
        values = dict(make_pr_receipt(RunStatus.MERGED).__dict__)
        values.update(
            status=RunStatus.HALTED,
            failure_category="failure_threshold",
        )
        with self.assertRaisesRegex(ReceiptContractError, "cannot claim merge SHA"):
            RunReceipt(**values)

    def test_failure_post_pr_shape_must_be_coherent(self) -> None:
        values = dict(make_pr_receipt().__dict__)
        values.update(
            status=RunStatus.INFRASTRUCTURE_FAILED,
            failure_category="journal_failed",
            head_sha=None,
        )
        with self.assertRaisesRegex(ReceiptContractError, "incoherent run stage"):
            RunReceipt(**values)

    def test_protected_paths_are_unique_safe_relative_paths(self) -> None:
        base = {
            **dict(make_pr_receipt().__dict__),
            "status": RunStatus.PATCH_REJECTED,
            "head_sha": None,
            "validation": (),
            "pr_url": None,
            "merge_sha": None,
            "failure_category": "policy_rejected",
            "actors": ReceiptActors(),
        }
        for paths in (("docs/.npmrc", "docs/.npmrc"), ("../secret",), ("a//b",)):
            with self.subTest(paths=paths), self.assertRaises(ReceiptContractError):
                RunReceipt(**{**base, "protected_paths_touched": paths})

    def test_parse_receipt_rejects_oversized_input_before_json(self) -> None:
        payload = b"{" + b" " * MAX_RECEIPT_BYTES
        with self.assertRaisesRegex(ReceiptContractError, "size limit"):
            parse_receipt(payload)
        with self.assertRaisesRegex(ReceiptContractError, "size limit"):
            parse_receipt("{" + " " * MAX_RECEIPT_BYTES)


if __name__ == "__main__":
    unittest.main()
