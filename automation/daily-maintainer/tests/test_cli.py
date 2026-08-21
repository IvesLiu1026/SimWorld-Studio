from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

from vista_daily_maintainer.cli import main

try:
    from .test_worktree import LocalRemoteFixture, LocalRemoteWorktreeManager
except ImportError:  # Root-level unittest discovery imports test modules directly.
    from test_worktree import LocalRemoteFixture, LocalRemoteWorktreeManager


class CliTests(unittest.TestCase):
    def _manager_args(self, root: Path, fixture: LocalRemoteFixture) -> list[str]:
        return [
            "--state-root",
            str(root / "state"),
            "--repo-root",
            str(fixture.checkout),
            "--worktrees-root",
            str(root / "worktrees"),
            "--repository",
            "IvesLiu1026/VISTA-World",
        ]

    def test_importable_cli_preflight_and_idempotent_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRemoteFixture(root)
            output = io.StringIO()
            local_manager = partial(
                LocalRemoteWorktreeManager,
                local_remote_url=str(fixture.remote),
            )
            with (
                patch(
                    "vista_daily_maintainer.cli.WorktreeManager",
                    new=local_manager,
                ),
                contextlib.redirect_stdout(output),
            ):
                code = main(["preflight", *self._manager_args(root, fixture)])
            self.assertEqual(code, 0)
            preflight = json.loads(output.getvalue())
            self.assertEqual(preflight["base_sha"], fixture.head)

            command = [
                "prepare",
                *self._manager_args(root, fixture),
                "--date",
                "2026-08-21",
                "--candidate-slug",
                "doc-link",
                "--expected-base",
                fixture.head,
            ]
            first_output = io.StringIO()
            with (
                patch(
                    "vista_daily_maintainer.cli.WorktreeManager",
                    new=local_manager,
                ),
                contextlib.redirect_stdout(first_output),
            ):
                self.assertEqual(main(command), 0)
            self.assertFalse(json.loads(first_output.getvalue())["idempotent_replay"])

            second_output = io.StringIO()
            with (
                patch(
                    "vista_daily_maintainer.cli.WorktreeManager",
                    new=local_manager,
                ),
                contextlib.redirect_stdout(second_output),
            ):
                self.assertEqual(main(command), 0)
            self.assertTrue(json.loads(second_output.getvalue())["idempotent_replay"])

    def test_due_command_reports_one_catch_up_period(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "due",
                        "--state-root",
                        str(root / "state"),
                        "--repository",
                        "IvesLiu1026/VISTA-World",
                        "--now",
                        "2026-08-21T08:00:00+08:00",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["run_date"], "2026-08-20")


if __name__ == "__main__":
    unittest.main()
