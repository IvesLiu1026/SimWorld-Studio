from __future__ import annotations

import datetime as dt
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from vista_daily_maintainer.state import (
    Lifecycle,
    PublicationSnapshot,
    PullRequestState,
    RunStateStore,
)
from vista_daily_maintainer.worktree import (
    DirtyRepositoryError,
    ExistingDailyBranchError,
    ExistingPublicationError,
    GitOperationError,
    RemoteMainMovedError,
    RepositoryIdentityError,
    RepositoryRootError,
    WorktreeManager,
    _run_fixed_command,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class LocalRemoteFixture:
    def __init__(self, root: Path) -> None:
        self.checkout = root / "checkout"
        self.remote = root / "remote.git"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q", "-b", "main")
        git(self.checkout, "config", "user.name", "Daily Maintainer Test")
        git(
            self.checkout,
            "config",
            "user.email",
            "daily-maintainer-test@example.invalid",
        )
        (self.checkout / "src").mkdir()
        (self.checkout / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        git(self.checkout, "add", "src/app.py")
        git(self.checkout, "commit", "-qm", "test: initial fixture")
        git(root, "init", "-q", "--bare", "--initial-branch=main", str(self.remote))
        git(self.checkout, "remote", "add", "origin", str(self.remote))
        git(self.checkout, "push", "-qu", "origin", "main")

    @property
    def head(self) -> str:
        return git(self.checkout, "rev-parse", "HEAD")

    def advance_remote(self) -> str:
        target = self.checkout / "src" / "app.py"
        target.write_text("VALUE = 2\n", encoding="utf-8")
        git(self.checkout, "add", "src/app.py")
        git(self.checkout, "commit", "-qm", "test: advance remote")
        git(self.checkout, "push", "-q", "origin", "main")
        return self.head


def manager_for(root: Path, fixture: LocalRemoteFixture) -> WorktreeManager:
    return WorktreeManager(
        repository_root=fixture.checkout,
        state_store=RunStateStore(root / "state"),
        worktrees_root=root / "worktrees",
        repository="IvesLiu1026/VISTA-World",
        expected_remote_url=str(fixture.remote),
    )


class WorktreeLifecycleTests(unittest.TestCase):
    def test_remote_url_must_match_pinned_repository_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            manager = WorktreeManager(
                repository_root=fixture.checkout,
                state_store=RunStateStore(root / "state"),
                worktrees_root=root / "worktrees",
                repository="IvesLiu1026/VISTA-World",
                expected_remote_url=str(root / "different.git"),
            )
            with self.assertRaisesRegex(RepositoryIdentityError, "pinned target"):
                manager.preflight()
            self.assertEqual(
                git(fixture.checkout, "branch", "--list", "codex/daily/*"), ""
            )

    def test_manager_ignores_caller_path_and_fake_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            marker = root / "fake-git-ran"
            fake_git = fake_bin / "git"
            fake_git.write_text(
                f"#!/bin/sh\nprintf pwned > {marker}\nexit 99\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            old_path = os.environ.get("PATH")
            os.environ["PATH"] = str(fake_bin)
            try:
                pin = manager_for(root, fixture).pin_remote_main()
            finally:
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path
            self.assertEqual(pin.sha, fixture.head)
            self.assertFalse(marker.exists())

    def test_repository_fsmonitor_cannot_execute_during_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            marker = root / "fsmonitor-ran"
            monitor = root / "malicious-fsmonitor.sh"
            monitor.write_text(
                f"#!/bin/sh\nprintf pwned > {marker}\nexit 1\n",
                encoding="utf-8",
            )
            monitor.chmod(0o755)
            git(fixture.checkout, "config", "core.fsmonitor", str(monitor))

            pin = manager_for(root, fixture).pin_remote_main()

            self.assertEqual(pin.sha, fixture.head)
            self.assertFalse(marker.exists())

    def test_timeout_terminates_command_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file = root / "child.pid"
            script = root / "hang.sh"
            script.write_text(
                f"#!/bin/sh\nsleep 30 &\nprintf '%s' \"$!\" > {pid_file}\nwait\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            with self.assertRaisesRegex(GitOperationError, "timed out"):
                _run_fixed_command(
                    (str(script),),
                    cwd=root,
                    environment={"PATH": "/usr/bin:/bin"},
                    timeout_seconds=0.2,
                    operation="timeout fixture",
                )
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and Path(f"/proc/{child_pid}").exists():
                time.sleep(0.02)
            self.assertFalse(Path(f"/proc/{child_pid}").exists())

    def test_duplicate_date_repository_base_is_an_idempotent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            manager = manager_for(root, fixture)
            first = manager.prepare(run_date="2026-08-21", candidate_slug="doc-link")
            second = manager.prepare(run_date="2026-08-21", candidate_slug="doc-link")

            self.assertFalse(first.idempotent_replay)
            self.assertTrue(second.idempotent_replay)
            self.assertEqual(first.state, second.state)
            self.assertEqual(first.state.lifecycle, Lifecycle.WORKTREE_READY)
            worktree = Path(first.state.worktree_path or "")
            self.assertTrue(worktree.is_dir())
            self.assertEqual(stat.S_IMODE(worktree.stat().st_mode), 0o700)
            branch_lines = git(fixture.checkout, "branch", "--list", "codex/daily/*")
            self.assertEqual(branch_lines.count("codex/daily/"), 1)

    def test_dirty_source_checkout_is_rejected_before_worktree_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            manager = manager_for(root, fixture)
            (fixture.checkout / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(DirtyRepositoryError, "source checkout"):
                manager.prepare(run_date="2026-08-21", candidate_slug="doc-link")
            self.assertEqual(list((root / "worktrees").iterdir()), [])

    def test_non_root_checkout_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            manager = WorktreeManager(
                repository_root=fixture.checkout / "src",
                state_store=RunStateStore(root / "state"),
                worktrees_root=root / "worktrees",
                repository="IvesLiu1026/VISTA-World",
                expected_remote_url=str(fixture.remote),
            )
            with self.assertRaisesRegex(RepositoryRootError, "exact"):
                manager.preflight()

    def test_remote_movement_is_persisted_and_creates_no_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            manager = manager_for(root, fixture)
            old_pin = manager.pin_remote_main()
            new_head = fixture.advance_remote()

            with self.assertRaises(RemoteMainMovedError) as caught:
                manager.prepare(
                    run_date="2026-08-21",
                    candidate_slug="doc-link",
                    expected_pin=old_pin,
                )
            self.assertEqual(caught.exception.previous_sha, old_pin.sha)
            self.assertEqual(caught.exception.current_sha, new_head)
            states = manager.state_store.states_for_date(
                "IvesLiu1026/VISTA-World", "2026-08-21"
            )
            self.assertEqual(len(states), 1)
            self.assertEqual(states[0].lifecycle, Lifecycle.REMOTE_MOVED)
            self.assertEqual(
                git(fixture.checkout, "branch", "--list", "codex/daily/*"), ""
            )

    def test_remote_movement_after_prepare_preserves_branch_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            manager = manager_for(root, fixture)
            prepared = manager.prepare(run_date="2026-08-21", candidate_slug="doc-link")
            worktree = Path(prepared.state.worktree_path or "")
            new_head = fixture.advance_remote()

            with self.assertRaises(RemoteMainMovedError) as caught:
                manager.assert_remote_unchanged(prepared.state)
            self.assertEqual(caught.exception.current_sha, new_head)
            self.assertTrue(worktree.is_dir())
            self.assertEqual(
                git(fixture.checkout, "rev-parse", prepared.state.branch_name),
                prepared.state.key.base_sha,
            )

    def test_stale_lock_and_reboot_catch_up_are_safe_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            manager = manager_for(root, fixture)
            lock_path = manager.state_store.lock_path("IvesLiu1026/VISTA-World")
            lock_path.write_text("stale-after-reboot\n", encoding="utf-8")
            lock_path.chmod(0o600)
            reboot_time = dt.datetime.fromisoformat("2026-08-21T08:00:00+08:00")
            due = manager.state_store.due_period("IvesLiu1026/VISTA-World", reboot_time)
            self.assertEqual(due.run_date, "2026-08-20")
            prepared = manager.prepare(
                run_date=due.run_date or "",
                candidate_slug="doc-link",
            )
            self.assertTrue(prepared.recovered_stale_lock)

            rebooted_store = RunStateStore(root / "state")
            after_restart = rebooted_store.due_period(
                "IvesLiu1026/VISTA-World", reboot_time
            )
            self.assertIsNone(after_restart.run_date)
            self.assertEqual(
                git(fixture.checkout, "branch", "--list", "codex/daily/*").count(
                    "codex/daily/"
                ),
                1,
            )

    def test_known_existing_pr_is_recorded_without_github_or_git_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            manager = manager_for(root, fixture)
            publication = PublicationSnapshot(
                state=PullRequestState.DRAFT,
                number=7,
                url="https://github.com/IvesLiu1026/VISTA-World/pull/7",
                head_sha=fixture.head,
            )
            with self.assertRaises(ExistingPublicationError) as caught:
                manager.prepare(
                    run_date="2026-08-21",
                    candidate_slug="doc-link",
                    publication=publication,
                )
            self.assertEqual(
                caught.exception.state.lifecycle, Lifecycle.EXISTING_PUBLICATION
            )
            self.assertEqual(
                git(fixture.checkout, "branch", "--list", "codex/daily/*"), ""
            )

    def test_existing_daily_branch_is_observed_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            manager = manager_for(root, fixture)
            pin = manager.pin_remote_main()
            branch = manager.branch_name("2026-08-21", "doc-link", pin.sha)
            git(fixture.checkout, "branch", branch, pin.sha)

            with self.assertRaises(ExistingDailyBranchError) as caught:
                manager.prepare(
                    run_date="2026-08-21",
                    candidate_slug="doc-link",
                    expected_pin=pin,
                )
            self.assertEqual(
                caught.exception.state.lifecycle, Lifecycle.EXISTING_BRANCH
            )
            self.assertEqual(git(fixture.checkout, "rev-parse", branch), pin.sha)


if __name__ == "__main__":
    unittest.main()
