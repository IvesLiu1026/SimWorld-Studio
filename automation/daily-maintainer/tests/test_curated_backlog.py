from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path

from vista_daily_maintainer.candidate import (
    BacklogTrust,
    has_v1_forbidden_authority,
    load_trusted_backlog,
    select_candidate,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
BACKLOG_PATH: Path | None = (
    PACKAGE_ROOT.parents[1] / "docs" / "maintenance" / "backlog.yaml"
    if PACKAGE_ROOT.parent.name == "automation"
    else None
)
PINNED_DRAFT_BACKLOG_SHA256 = (
    "5e08d1f2f784aa5940e0606a58637e5006f0892b3c7971b2eb6fba669e2d4fa5"
)


class CuratedBacklogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if BACKLOG_PATH is None or not BACKLOG_PATH.is_file():
            raise unittest.SkipTest(
                "repository draft backlog is intentionally not part of the sdist"
            )
        cls.backlog = load_trusted_backlog(
            BacklogTrust(
                path=BACKLOG_PATH,
                sha256=PINNED_DRAFT_BACKLOG_SHA256,
                manifest_revision=1,
                approved_by="CodexDraft",
            )
        )

    def test_manifest_remains_an_explicit_unapproved_draft(self) -> None:
        self.assertEqual(self.backlog.sha256, PINNED_DRAFT_BACKLOG_SHA256)
        self.assertEqual(self.backlog.approved_by, "CodexDraft")
        self.assertTrue(
            all(
                candidate.source.approved_by == "CodexDraft"
                for candidate in self.backlog.candidates
            )
        )

    def test_initial_inventory_has_a_fourteen_day_tier_zero_buffer(self) -> None:
        tier_zero = [item for item in self.backlog.candidates if item.risk_tier == 0]
        self.assertGreaterEqual(len(tier_zero), 14)
        self.assertGreaterEqual(len(self.backlog.candidates), 28)

    def test_first_fourteen_selections_are_stable_and_distinct(self) -> None:
        completed: set[str] = set()
        selected: list[str] = []
        for _ in range(14):
            candidate = select_candidate(
                self.backlog,
                on_date=dt.date(2026, 8, 21),
                completed_ids=completed,
                allowed_risk_tiers=(0,),
            )
            self.assertIsNotNone(candidate)
            assert candidate is not None
            selected.append(candidate.candidate_id)
            completed.add(candidate.candidate_id)
        self.assertEqual(len(selected), len(set(selected)))
        self.assertEqual(selected, sorted(selected))

    def test_inventory_uses_canonical_projection_order(self) -> None:
        for candidate in self.backlog.candidates:
            with self.subTest(candidate=candidate.candidate_id):
                self.assertEqual(
                    candidate.allowed_paths,
                    tuple(sorted(set(candidate.allowed_paths))),
                )
                self.assertEqual(
                    candidate.validation_profiles,
                    tuple(sorted(set(candidate.validation_profiles))),
                )

    def test_inventory_has_no_external_side_effect_or_forbidden_authority(self) -> None:
        for candidate in self.backlog.candidates:
            with self.subTest(candidate=candidate.candidate_id):
                self.assertEqual(candidate.expected_external_side_effects, "none")
                self.assertEqual(
                    candidate.validation_profiles, ("tools-python-offline",)
                )
                self.assertTrue(
                    all(
                        not has_v1_forbidden_authority(pattern, pattern=True)
                        for pattern in candidate.allowed_paths
                    )
                )


if __name__ == "__main__":
    unittest.main()
