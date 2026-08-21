from __future__ import annotations

import datetime as dt
import hashlib
import unittest
from pathlib import Path

from vista_daily_maintainer.candidate import (
    BacklogTrust,
    load_trusted_backlog,
    select_candidate,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKLOG_PATH = REPOSITORY_ROOT / "docs" / "maintenance" / "backlog.yaml"


class CuratedBacklogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        payload = BACKLOG_PATH.read_bytes()
        cls.backlog = load_trusted_backlog(
            BacklogTrust(
                path=BACKLOG_PATH,
                sha256=hashlib.sha256(payload).hexdigest(),
                manifest_revision=1,
                approved_by="IvesLiu1026",
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

    def test_inventory_has_no_runtime_or_external_side_effect_authority(self) -> None:
        forbidden = {
            "assets",
            "auth",
            "datasets",
            "deploy",
            "network",
            "ops",
            "runtime",
            "secrets",
            "ue",
            "unreal",
            "world_packs",
        }
        for candidate in self.backlog.candidates:
            with self.subTest(candidate=candidate.candidate_id):
                self.assertEqual(candidate.expected_external_side_effects, "none")
                self.assertEqual(
                    candidate.validation_profiles, ("tools-python-offline",)
                )
                tokens = {
                    token.lower()
                    for pattern in candidate.allowed_paths
                    for token in pattern.replace("-", "/").replace("_", "/").split("/")
                }
                self.assertTrue(tokens.isdisjoint(forbidden))


if __name__ == "__main__":
    unittest.main()
