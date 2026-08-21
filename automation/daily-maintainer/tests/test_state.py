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
    def test_state_rejects_noncanonical_or_oversized_candidate_slug(self) -> None:
        for invalid_slug in ("a--b", "a" * 49):
            with self.subTest(invalid_slug=invalid_slug):
                with self.assertRaisesRegex(StateContractError, "daily branch"):
                    replace(
                        make_state(),
                        branch_name=(f"codex/daily/2026-08-21-{invalid_slug}-aaaaaaaa"),
                    )

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

    def test_state_parser_rejects_every_malformed_leaf_as_contract_error(self) -> None:
        base = json.loads(serialize_state(make_state()))
        mutations = {
            "schema_version": ("schema_version", []),
            "run_id": ("run_id", []),
            "run_date": ("run_date", []),
            "repository": ("repository", []),
            "base_sha": ("base_sha", []),
            "remote": ("remote", []),
            "remote_branch": ("remote_branch", []),
            "branch_name": ("branch_name", []),
            "lifecycle": ("lifecycle", []),
            "branch_disposition": ("branch_disposition", []),
            "branch_head_sha": ("branch_head_sha", []),
            "worktree_path": ("worktree_path", []),
            "observed_remote_sha": ("observed_remote_sha", []),
            "publication.state": ("publication.state", []),
            "publication.number": ("publication.number", True),
            "publication.url": ("publication.url", []),
            "publication.head_sha": ("publication.head_sha", []),
        }
        for label, (path, invalid) in mutations.items():
            with self.subTest(label=label):
                value = json.loads(json.dumps(base))
                if path.startswith("publication."):
                    value["publication"][path.removeprefix("publication.")] = invalid
                else:
                    value[path] = invalid
                with self.assertRaises(StateContractError):
                    parse_state(json.dumps(value))

    def test_worktree_path_array_regression_never_leaks_type_error(self) -> None:
        value = json.loads(serialize_state(make_state()))
        value["worktree_path"] = []
        with self.assertRaisesRegex(StateContractError, "worktree path"):
            parse_state(json.dumps(value))

    def test_state_parser_enforces_leaf_ranges_and_strict_numbers(self) -> None:
        value = json.loads(serialize_state(make_state()))
        value["worktree_path"] = "/" + ("a" * 4097)
        with self.assertRaisesRegex(StateContractError, "worktree path"):
            parse_state(json.dumps(value))

        value = json.loads(serialize_state(make_state()))
        value["publication"] = {
            "state": "draft",
            "number": 2**63,
            "url": "https://github.com/IvesLiu1026/VISTA-World/pull/1",
            "head_sha": SHA_A,
        }
        with self.assertRaisesRegex(StateContractError, "number"):
            parse_state(json.dumps(value))

        non_finite = (
            serialize_state(make_state())
            .decode("utf-8")
            .replace('"number":null', '"number":NaN')
        )
        with self.assertRaisesRegex(StateContractError, "non-finite"):
            parse_state(non_finite)

        with self.assertRaisesRegex(StateContractError, "Unicode"):
            parse_state("\ud800")

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
