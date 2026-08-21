from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from vista_daily_maintainer.profiles import ValidationProfile, ValidationProfileRegistry
from vista_daily_maintainer.verifier import Verifier

try:
    from .helpers import init_repo, make_candidate
except ImportError:  # Support unittest discovery without an explicit top-level.
    from helpers import init_repo, make_candidate


class VerifierTests(unittest.TestCase):
    def test_runs_only_registry_argv_with_shell_disabled_and_sanitized_env(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            probe = (
                "import os,sys; "
                "blocked=('GITHUB_TOKEN','OPENAI_API_KEY','SSH_AUTH_SOCK'); "
                "sys.exit(1 if any(os.getenv(key) for key in blocked) else 0)"
            )
            registry = ValidationProfileRegistry(
                (
                    ValidationProfile(
                        profile_id="safe-probe",
                        cwd=".",
                        argv=(sys.executable, "-c", probe),
                        timeout_seconds=10,
                    ),
                )
            )
            candidate = make_candidate(profiles=("safe-probe",))
            verifier = Verifier(registry=registry)
            inherited = dict(os.environ)
            inherited.update(
                {
                    "GITHUB_TOKEN": "must-not-leak",
                    "OPENAI_API_KEY": "must-not-leak",
                    "SSH_AUTH_SOCK": "/tmp/must-not-leak",
                }
            )

            report = verifier.verify(repo, base, candidate, inherited_env=inherited)

        self.assertTrue(report.ok, report)
        self.assertEqual(
            [item.command_id for item in report.validation],
            ["git-diff-check", "safe-probe"],
        )
        self.assertTrue(all(item.exit_code == 0 for item in report.validation))
        self.assertTrue(
            all(len(item.output_sha256) == 64 for item in report.validation)
        )

    def test_guard_failure_prevents_candidate_profile_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            marker = Path(tmp) / "must-not-exist"
            workflow = repo / ".github/workflows/pwn.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: pwn\n", encoding="utf-8")
            registry = ValidationProfileRegistry(
                (
                    ValidationProfile(
                        profile_id="write-marker",
                        cwd=".",
                        argv=(
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).touch()",
                        ),
                    ),
                )
            )
            report = Verifier(registry=registry).verify(
                repo,
                base,
                make_candidate(allowed_paths=("**",), profiles=("write-marker",)),
            )
        self.assertFalse(report.ok)
        self.assertEqual(report.validation, ())
        self.assertFalse(marker.exists())

    def test_unknown_profile_fails_before_process_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "validation profile"):
                Verifier(registry=ValidationProfileRegistry(())).verify(
                    repo,
                    base,
                    make_candidate(profiles=("unknown",)),
                )

    def test_shell_dash_c_profile_is_rejected_by_registry(self) -> None:
        with self.assertRaisesRegex(ValueError, "shell -c"):
            ValidationProfile(
                profile_id="unsafe-shell",
                cwd=".",
                argv=("sh", "-c", "touch /tmp/pwn"),
            )


if __name__ == "__main__":
    unittest.main()
