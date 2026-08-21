from __future__ import annotations

import datetime as dt
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from vista_daily_maintainer.state import (
    BranchDisposition,
    ConcurrentRunError,
    DueReason,
    Lifecycle,
    PublicationSnapshot,
    PullRequestState,
    RunKey,
    RunState,
    RunStateStore,
    StateContractError,
    choose_due_period,
    parse_state,
    serialize_state,
)


SHA_A = "a" * 40


def make_state() -> RunState:
    return RunState(
        key=RunKey("2026-08-21", "IvesLiu1026/VISTA-World", SHA_A),
        remote="origin",
        remote_branch="main",
        branch_name="codex/daily/2026-08-21-doc-link-aaaaaaaa",
        lifecycle=Lifecycle.WORKTREE_READY,
        branch_disposition=BranchDisposition.CREATED,
        branch_head_sha=SHA_A,
        worktree_path="/tmp/vista-world-worktrees/2026-08-21-doc-link-aaaaaaaaaaaa",
        observed_remote_sha=SHA_A,
    )


class RunStateTests(unittest.TestCase):
    def test_state_round_trip_is_canonical_and_digest_bound(self) -> None:
        state = make_state()
        payload = serialize_state(state)
        self.assertEqual(parse_state(payload), state)
        self.assertEqual(serialize_state(parse_state(payload)), payload)
        mapping = json.loads(payload)
        mapping["run_id"] = "2026-08-21/IvesLiu1026/VISTA-World@" + "b" * 40
        with self.assertRaisesRegex(StateContractError, "run_id"):
            parse_state(json.dumps(mapping))

    def test_direct_state_parser_rejects_oversized_input_before_json(self) -> None:
        oversized = b'"' + (b"a" * (64 * 1024)) + b'"'
        with self.assertRaisesRegex(StateContractError, "oversized"):
            parse_state(oversized)

    def test_publication_snapshot_is_repository_bound(self) -> None:
        snapshot = PublicationSnapshot(
            state=PullRequestState.DRAFT,
            number=12,
            url="https://github.com/IvesLiu1026/VISTA-World/pull/12",
            head_sha=SHA_A,
        )
        snapshot.validate_repository("IvesLiu1026/VISTA-World")
        with self.assertRaisesRegex(StateContractError, "repository"):
            snapshot.validate_repository("someone/else")

    def test_state_store_uses_owner_only_atomic_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            store = RunStateStore(root)
            state = make_state()
            path = store.save(state)
            self.assertEqual(store.load(state.key), state)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                store.states_for_date(state.key.repository, state.key.run_date),
                (state,),
            )

    def test_persisted_lifecycle_cannot_move_backward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStateStore(Path(temporary) / "state")
            state = make_state()
            store.save(state)
            moved = replace(
                state,
                lifecycle=Lifecycle.REMOTE_MOVED,
                observed_remote_sha="b" * 40,
            )
            store.save(moved)
            with self.assertRaisesRegex(StateContractError, "backward"):
                store.save(state)

    def test_unlocked_stale_metadata_is_recovered_but_active_flock_wins(self) -> None:
        repository = "IvesLiu1026/VISTA-World"
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStateStore(Path(temporary) / "state")
            lock_path = store.lock_path(repository)
            lock_path.write_text(
                '{"acquired_at":"2020-01-01T00:00:00+00:00","pid":1}\n',
                encoding="utf-8",
            )
            os.chmod(lock_path, 0o600)
            with store.lock(repository) as recovered:
                self.assertTrue(recovered.recovered_stale)
                with self.assertRaisesRegex(ConcurrentRunError, "kernel lock"):
                    with store.lock(repository):
                        self.fail("an active flock must never be bypassed by age")
            self.assertEqual(lock_path.read_bytes(), b"")
            with store.lock(repository) as clean:
                self.assertFalse(clean.recovered_stale)

    def test_schedule_returns_at_most_the_latest_missed_period(self) -> None:
        zone = dt.timezone(dt.timedelta(hours=8))
        after_reboot = dt.datetime(2026, 8, 21, 8, 0, tzinfo=zone)
        decision = choose_due_period(
            after_reboot,
            attempted_dates={"2026-08-10"},
        )
        self.assertEqual(decision.run_date, "2026-08-20")
        self.assertEqual(decision.reason, DueReason.CATCH_UP)
        self.assertNotEqual(decision.run_date, "2026-08-11")

        duplicate = choose_due_period(
            after_reboot,
            attempted_dates={"2026-08-20"},
        )
        self.assertIsNone(duplicate.run_date)
        self.assertEqual(duplicate.reason, DueReason.ALREADY_ATTEMPTED)


if __name__ == "__main__":
    unittest.main()
