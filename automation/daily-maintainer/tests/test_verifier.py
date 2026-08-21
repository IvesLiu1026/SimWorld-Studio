from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vista_daily_maintainer.candidate import CandidateContractError
from vista_daily_maintainer.profiles import (
    TrustedExecutables,
    ValidationProfile,
    ValidationProfileRegistry,
)
from vista_daily_maintainer.verifier import IsolationEvidence, Verifier

try:
    from .helpers import init_repo, make_candidate
except ImportError:  # Support unittest discovery without an explicit top-level.
    from helpers import init_repo, make_candidate


class VerifierTests(unittest.TestCase):
    @staticmethod
    def _trusted_tools() -> TrustedExecutables:
        git = shutil.which("git")
        if not git:
            raise unittest.SkipTest("git is required")
        return TrustedExecutables(
            {
                "git": Path(git),
                "python": Path(sys.executable),
            }
        )

    @staticmethod
    def _isolation() -> IsolationEvidence:
        return IsolationEvidence(
            network_isolated=True,
            credentials_absent=True,
            observed_by="unit-test-harness",
            evidence_sha256="d" * 64,
        )

    def test_runs_fixed_argv_with_scrubbed_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            probe = (
                "import os,sys; "
                "blocked=('GITHUB_TOKEN','OPENAI_API_KEY','SSH_AUTH_SOCK'); "
                "bad_paths=('/tmp/malicious-home','/tmp/malicious-xdg',"
                "'/tmp/malicious-npmrc','/tmp/malicious-uv.toml',"
                "'/tmp/malicious-gitconfig'); "
                "sys.exit(1 if any(os.getenv(key) for key in blocked) "
                "or any(value in bad_paths for value in os.environ.values()) else 0)"
            )
            registry = ValidationProfileRegistry(
                (
                    ValidationProfile(
                        profile_id="daily-maintainer-core-tests",
                        cwd=".",
                        argv=(sys.executable, "-c", probe),
                        timeout_seconds=10,
                    ),
                )
            )
            inherited = dict(os.environ)
            inherited.update(
                {
                    "GITHUB_TOKEN": "must-not-leak",
                    "OPENAI_API_KEY": "must-not-leak",
                    "SSH_AUTH_SOCK": "/tmp/must-not-leak",
                    "HOME": "/tmp/malicious-home",
                    "XDG_CONFIG_HOME": "/tmp/malicious-xdg",
                    "NPM_CONFIG_USERCONFIG": "/tmp/malicious-npmrc",
                    "UV_CONFIG_FILE": "/tmp/malicious-uv.toml",
                    "GIT_CONFIG_GLOBAL": "/tmp/malicious-gitconfig",
                }
            )

            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(),
                ).verify(
                    repo,
                    base,
                    make_candidate(),
                    inherited_env=inherited,
                )

        self.assertTrue(report.checks_passed, report)
        self.assertFalse(report.publication_authorized)
        self.assertFalse(hasattr(report, "ok"))
        self.assertEqual(
            [item.command_id for item in report.validation],
            ["git-diff-check", "daily-maintainer-core-tests"],
        )
        self.assertTrue(all(item.exit_code == 0 for item in report.validation))
        self.assertFalse(report.mutation_detected)
        self.assertEqual(report.guard.patch_sha256, report.final_guard.patch_sha256)
        self.assertEqual(
            report.isolation_evidence.observed_by,
            "unit-test-harness",
        )

    def test_validation_mutation_is_detected_and_stops_later_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            marker = Path(tmp) / "later-profile-ran"
            registry = ValidationProfileRegistry(
                (
                    ValidationProfile(
                        profile_id="daily-maintainer-core-tests",
                        cwd=".",
                        argv=(
                            sys.executable,
                            "-c",
                            "from pathlib import Path; "
                            "Path('src/app.py').write_text('VALUE = 999\\n')",
                        ),
                    ),
                    ValidationProfile(
                        profile_id="tools-python-offline",
                        cwd=".",
                        argv=(
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).touch()",
                        ),
                    ),
                )
            )
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(),
                ).verify(
                    repo,
                    base,
                    make_candidate(
                        profiles=(
                            "daily-maintainer-core-tests",
                            "tools-python-offline",
                        )
                    ),
                )

        self.assertFalse(report.checks_passed)
        self.assertTrue(report.mutation_detected)
        self.assertNotEqual(report.guard.patch_sha256, report.final_guard.patch_sha256)
        self.assertEqual(
            [item.command_id for item in report.validation],
            ["git-diff-check", "daily-maintainer-core-tests"],
        )
        self.assertFalse(marker.exists())

    def test_validation_untracked_mode_mutation_changes_patch_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            target = repo / "src/new.sh"
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o644)
            registry = ValidationProfileRegistry(
                (
                    ValidationProfile(
                        profile_id="daily-maintainer-core-tests",
                        cwd=".",
                        argv=(
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('src/new.sh').chmod(0o755)",
                        ),
                    ),
                )
            )
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(),
                ).verify(repo, base, make_candidate())

        self.assertFalse(report.checks_passed)
        self.assertTrue(report.mutation_detected)
        self.assertNotEqual(report.guard.patch_sha256, report.final_guard.patch_sha256)

    def test_inherited_path_cannot_replace_git_uv_or_npm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            markers: dict[str, Path] = {}
            for tool in ("git", "uv", "npm"):
                marker = root / f"{tool}-invoked"
                markers[tool] = marker
                executable = fake_bin / tool
                executable.write_text(
                    f"#!/bin/sh\n: > {marker}\nexit 0\n",
                    encoding="utf-8",
                )
                executable.chmod(0o755)

            safe_registry = ValidationProfileRegistry(
                (
                    ValidationProfile(
                        profile_id="daily-maintainer-core-tests",
                        cwd=".",
                        argv=(sys.executable, "-c", "raise SystemExit(0)"),
                    ),
                )
            )
            inherited = {"PATH": str(fake_bin), "HOME": str(root / "attacker-home")}
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                safe_registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(),
                ).verify(
                    repo,
                    base,
                    make_candidate(),
                    inherited_env=inherited,
                )
            self.assertTrue(report.checks_passed)
            self.assertFalse(markers["git"].exists())

            for tool, profile_id in (
                ("uv", "tools-python-offline"),
                ("npm", "web-server-unit"),
            ):
                registry = ValidationProfileRegistry(
                    (
                        ValidationProfile(
                            profile_id=profile_id,
                            cwd=".",
                            argv=(tool, "--version"),
                        ),
                    )
                )
                with (
                    self.subTest(tool=tool),
                    self.assertRaisesRegex(ValueError, "trusted executable"),
                ):
                    with patch(
                        "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                        registry,
                    ):
                        Verifier(
                            executables=self._trusted_tools(),
                            isolation_evidence=self._isolation(),
                        ).verify(
                            repo,
                            base,
                            make_candidate(profiles=(profile_id,)),
                            inherited_env=inherited,
                        )
                self.assertFalse(markers[tool].exists())

    def test_timeout_kills_validation_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            marker = root / "leaked-child"
            child = (
                "import time; from pathlib import Path; "
                f"time.sleep(1.2); Path({str(marker)!r}).touch()"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}], "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "time.sleep(10)"
            )
            registry = ValidationProfileRegistry(
                (
                    ValidationProfile(
                        profile_id="daily-maintainer-core-tests",
                        cwd=".",
                        argv=(sys.executable, "-c", parent),
                        timeout_seconds=1,
                    ),
                )
            )
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(),
                ).verify(repo, base, make_candidate())
            self.assertFalse(report.checks_passed)
            self.assertTrue(report.validation[-1].timed_out)
            time.sleep(1.5)
            self.assertFalse(marker.exists())

    def test_timeout_kills_group_after_validation_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            marker = root / "detached-from-leader"
            child = (
                "import time; from pathlib import Path; "
                f"time.sleep(1.2); Path({str(marker)!r}).touch()"
            )
            parent = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
                "sys.exit(0)"
            )
            registry = ValidationProfileRegistry(
                (
                    ValidationProfile(
                        profile_id="daily-maintainer-core-tests",
                        cwd=".",
                        argv=(sys.executable, "-c", parent),
                        timeout_seconds=1,
                    ),
                )
            )
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(),
                ).verify(repo, base, make_candidate())
            self.assertFalse(report.checks_passed)
            self.assertTrue(report.validation[-1].timed_out)
            time.sleep(1.5)
            self.assertFalse(marker.exists())

    def test_guard_failure_prevents_candidate_profile_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            marker = Path(tmp) / "must-not-exist"
            workflow = repo / ".github/workflows/pwn.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: pwn\n", encoding="utf-8")
            with self.assertRaisesRegex(CandidateContractError, "V1 candidate policy"):
                Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(),
                ).verify(
                    repo,
                    base,
                    make_candidate(allowed_paths=("**",)),
                )
        self.assertFalse(marker.exists())

    def test_direct_custom_profile_fails_before_process_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(CandidateContractError, "V1 candidate policy"):
                Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(),
                ).verify(
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

    def test_trusted_executable_identity_is_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "probe"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            trusted = TrustedExecutables({"probe": executable})
            self.assertEqual(trusted.resolve("probe"), executable)
            executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "identity changed"):
                trusted.resolve("probe")

    def test_verifier_refuses_missing_isolation_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "isolation evidence"):
            Verifier(
                executables=self._trusted_tools(),
            )

        with self.assertRaisesRegex(ValueError, "network isolation"):
            IsolationEvidence(
                network_isolated=False,
                credentials_absent=True,
                observed_by="unit-test-harness",
                evidence_sha256="d" * 64,
            )


if __name__ == "__main__":
    unittest.main()
