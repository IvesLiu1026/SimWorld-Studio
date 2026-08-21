from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vista_daily_maintainer.candidate import (
    CandidateContractError,
    candidate_authorization_digest,
)
from vista_daily_maintainer.guard import DiffGuard
from vista_daily_maintainer.profiles import (
    TrustedExecutables,
    ValidationProfile,
    ValidationProfileRegistry,
)
from vista_daily_maintainer.verifier import (
    IsolationEvidence,
    VerificationSubject,
    Verifier,
)

try:
    from .helpers import init_repo, make_candidate
except ImportError:  # Support unittest discovery without an explicit top-level.
    from helpers import init_repo, make_candidate


class VerifierTests(unittest.TestCase):
    _subject_patches: dict[str, str] = {}

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

    @classmethod
    def _subject(cls, repo: Path, base: str, selected) -> VerificationSubject:
        run_date = "2026-08-21"
        repository = "IvesLiu1026/VISTA-World"
        slug = "focused-contract-test"
        subject = VerificationSubject(
            run_id=f"{run_date}/{repository}@{base}",
            run_date=run_date,
            repository=repository,
            base_sha=base,
            branch_name=f"codex/daily/{run_date}-{slug}-{base[:8]}",
            worktree_path=str(repo.resolve()),
            candidate_id=selected.candidate_id,
            candidate_slug=slug,
            backlog_sha256="b" * 64,
            backlog_authorization_sha256="c" * 64,
            candidate_sha256=candidate_authorization_digest(selected),
            run_state_sha256="d" * 64,
        )
        try:
            patch_sha256 = (
                DiffGuard()
                .inspect(
                    repo,
                    base,
                    selected,
                )
                .patch_sha256
            )
        except CandidateContractError:
            # Entry-policy rejection occurs before the evidence patch is read.
            patch_sha256 = "e" * 64
        cls._subject_patches[subject.sha256] = patch_sha256
        return subject

    @classmethod
    def _isolation(cls, subject: VerificationSubject) -> IsolationEvidence:
        return IsolationEvidence.attest(
            subject.sha256,
            cls._subject_patches[subject.sha256],
            observed_by="unit-test-harness",
        )

    @staticmethod
    def _external_validation_python(root: Path) -> Path:
        environment = root / "validation-python"
        system_python = shutil.which(
            "python3",
            path="/usr/local/bin:/usr/bin:/bin",
        )
        if not system_python:
            raise unittest.SkipTest("system python3 is required")
        subprocess.run(
            (
                system_python,
                "-m",
                "venv",
                "--copies",
                "--without-pip",
                str(environment),
            ),
            check=True,
            capture_output=True,
        )
        interpreter = environment / "bin/python3"
        discovery = subprocess.run(
            (
                str(interpreter),
                "-c",
                "import sysconfig; print(sysconfig.get_paths()['purelib'])",
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        site_packages = Path(discovery.stdout.strip())
        site_packages.mkdir(parents=True, exist_ok=True)
        (site_packages / "pinned_validation_dependency.py").write_text(
            "FINGERPRINT = 'locked-offline-dependency'\n",
            encoding="utf-8",
        )
        return interpreter

    @staticmethod
    def _init_tools_profile_repo(repo: Path) -> str:
        test_source = (
            "import unittest\n"
            "import pinned_validation_dependency as dependency\n\n"
            "class OfflineDependencyTests(unittest.TestCase):\n"
            "    def test_dependency_is_available(self):\n"
            "        self.assertEqual(\n"
            "            dependency.FINGERPRINT,\n"
            "            'locked-offline-dependency',\n"
            "        )\n"
        )
        return init_repo(
            repo,
            {
                ".gitignore": "__pycache__/\n*.pyc\n",
                "src/app.py": "VALUE = 1\n",
                "tools/tests/test_vista_playable_home_compiler.py": test_source,
                "tools/tests/test_vista_playable_home_contracts.py": test_source,
            },
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
            selected = make_candidate()
            subject = self._subject(repo, base, selected)
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(subject),
                ).verify(
                    repo,
                    base,
                    selected,
                    subject=subject,
                    inherited_env=inherited,
                )

        self.assertTrue(report.checks_passed, report)
        self.assertFalse(report.publication_authorized)
        self.assertEqual(
            report.candidate_sha256,
            candidate_authorization_digest(selected),
        )
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
            selected = make_candidate(
                profiles=(
                    "daily-maintainer-core-tests",
                    "tools-python-offline",
                )
            )
            subject = self._subject(repo, base, selected)
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(subject),
                ).verify(
                    repo,
                    base,
                    selected,
                    subject=subject,
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
            selected = make_candidate()
            subject = self._subject(repo, base, selected)
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(subject),
                ).verify(repo, base, selected, subject=subject)

        self.assertFalse(report.checks_passed)
        self.assertTrue(report.mutation_detected)
        self.assertNotEqual(report.guard.patch_sha256, report.final_guard.patch_sha256)

    def test_inherited_path_cannot_replace_git_python3_or_npm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            markers: dict[str, Path] = {}
            for tool in ("git", "python3", "npm"):
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
            selected = make_candidate()
            subject = self._subject(repo, base, selected)
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                safe_registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(subject),
                ).verify(
                    repo,
                    base,
                    selected,
                    subject=subject,
                    inherited_env=inherited,
                )
            self.assertTrue(report.checks_passed)
            self.assertFalse(markers["git"].exists())

            for tool, profile_id in (
                ("python3", "tools-python-offline"),
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
                    selected = make_candidate(profiles=(profile_id,))
                    subject = self._subject(repo, base, selected)
                    with patch(
                        "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                        registry,
                    ):
                        Verifier(
                            executables=self._trusted_tools(),
                            isolation_evidence=self._isolation(subject),
                        ).verify(
                            repo,
                            base,
                            selected,
                            subject=subject,
                            inherited_env=inherited,
                        )
                self.assertFalse(markers[tool].exists())

    def test_tools_profile_runs_with_pinned_python_outside_repository(self) -> None:
        git = shutil.which("git")
        if not git:
            raise unittest.SkipTest("git is required")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base = self._init_tools_profile_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            interpreter = self._external_validation_python(root)
            selected = make_candidate(profiles=("tools-python-offline",))
            subject = self._subject(repo, base, selected)
            report = Verifier(
                executables=TrustedExecutables(
                    {
                        "git": Path(git),
                        "python3": interpreter,
                    }
                ),
                isolation_evidence=self._isolation(subject),
            ).verify(
                repo,
                base,
                selected,
                subject=subject,
            )

            self.assertTrue(report.checks_passed, report)
            self.assertEqual(
                [item.command_id for item in report.validation],
                ["git-diff-check", "tools-python-offline"],
            )
            self.assertFalse(list(repo.rglob("__pycache__")))

    def test_tools_profile_fails_before_spawn_without_pinned_python(self) -> None:
        git = shutil.which("git")
        if not git:
            raise unittest.SkipTest("git is required")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = self._init_tools_profile_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            selected = make_candidate(profiles=("tools-python-offline",))
            subject = self._subject(repo, base, selected)
            with self.assertRaisesRegex(ValueError, "trusted executable"):
                Verifier(
                    executables=TrustedExecutables({"git": Path(git)}),
                    isolation_evidence=self._isolation(subject),
                ).verify(
                    repo,
                    base,
                    selected,
                    subject=subject,
                )

    def test_tools_profile_rejects_repository_local_python_before_spawn(self) -> None:
        git = shutil.which("git")
        if not git:
            raise unittest.SkipTest("git is required")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = self._init_tools_profile_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            local_python = repo / "tools/pinned-python3"
            shutil.copy2(Path(sys.executable).resolve(strict=True), local_python)
            local_python.chmod(0o755)
            selected = make_candidate(profiles=("tools-python-offline",))
            subject = self._subject(repo, base, selected)
            with self.assertRaisesRegex(ValueError, "outside the repository"):
                Verifier(
                    executables=TrustedExecutables(
                        {
                            "git": Path(git),
                            "python3": local_python,
                        }
                    ),
                    isolation_evidence=self._isolation(subject),
                ).verify(
                    repo,
                    base,
                    selected,
                    subject=subject,
                )

    def test_system_defaults_discovers_a_separately_pinned_python3(self) -> None:
        if not shutil.which("python3", path="/usr/local/bin:/usr/bin:/bin"):
            raise unittest.SkipTest("system python3 is required")
        trusted = TrustedExecutables.system_defaults()
        python3 = trusted.resolve("python3")
        self.assertTrue(python3.is_absolute())
        self.assertNotIn(".venv", python3.parts)

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
            selected = make_candidate()
            subject = self._subject(repo, base, selected)
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(subject),
                ).verify(repo, base, selected, subject=subject)
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
            selected = make_candidate()
            subject = self._subject(repo, base, selected)
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(subject),
                ).verify(repo, base, selected, subject=subject)
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
            selected = make_candidate(allowed_paths=("**",))
            subject = self._subject(repo, base, selected)
            with self.assertRaisesRegex(CandidateContractError, "V1 candidate policy"):
                Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(subject),
                ).verify(
                    repo,
                    base,
                    selected,
                    subject=subject,
                )
        self.assertFalse(marker.exists())

    def test_ignored_venv_sitecustomize_blocks_all_validation_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            base = init_repo(
                repo,
                {
                    ".gitignore": ".venv/\n",
                    "src/app.py": "VALUE = 1\n",
                },
            )
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            sitecustomize_marker = root / "sitecustomize-ran"
            profile_marker = root / "profile-ran"
            sitecustomize = repo / ".venv/lib/python/sitecustomize.py"
            sitecustomize.parent.mkdir(parents=True)
            sitecustomize.write_text(
                "from pathlib import Path\n"
                f"Path({str(sitecustomize_marker)!r}).touch()\n",
                encoding="utf-8",
            )
            selected = make_candidate()
            subject = self._subject(repo, base, selected)
            registry = ValidationProfileRegistry(
                (
                    ValidationProfile(
                        profile_id="daily-maintainer-core-tests",
                        cwd=".",
                        argv=(
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; Path({str(profile_marker)!r}).touch()",
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
                    isolation_evidence=self._isolation(subject),
                ).verify(repo, base, selected, subject=subject)

        self.assertFalse(report.checks_passed)
        self.assertEqual(report.validation, ())
        self.assertIn(
            "ignored_content",
            {item.code for item in report.guard.violations},
        )
        self.assertFalse(sitecustomize_marker.exists())
        self.assertFalse(profile_marker.exists())

    def test_validation_created_ignored_content_is_detected_after_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(
                repo,
                {
                    ".gitignore": "*.cache\n",
                    "src/app.py": "VALUE = 1\n",
                },
            )
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            registry = ValidationProfileRegistry(
                (
                    ValidationProfile(
                        profile_id="daily-maintainer-core-tests",
                        cwd=".",
                        argv=(
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('src/probe.cache').touch()",
                        ),
                    ),
                )
            )
            selected = make_candidate()
            subject = self._subject(repo, base, selected)
            with patch(
                "vista_daily_maintainer.verifier.BUILTIN_VALIDATION_PROFILES",
                registry,
            ):
                report = Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(subject),
                ).verify(repo, base, selected, subject=subject)

        self.assertFalse(report.checks_passed)
        self.assertTrue(report.mutation_detected)
        self.assertEqual(
            [item.command_id for item in report.validation],
            ["git-diff-check", "daily-maintainer-core-tests"],
        )
        self.assertTrue(all(item.ok for item in report.validation))
        self.assertIn(
            "ignored_content",
            {item.code for item in report.final_guard.violations},
        )
        self.assertNotEqual(report.guard.patch_sha256, report.final_guard.patch_sha256)

    def test_direct_custom_profile_fails_before_process_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            selected = make_candidate(profiles=("unknown",))
            subject = self._subject(repo, base, selected)
            with self.assertRaisesRegex(CandidateContractError, "V1 candidate policy"):
                Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(subject),
                ).verify(
                    repo,
                    base,
                    selected,
                    subject=subject,
                )

    def test_direct_tier_zero_source_authority_fails_at_verifier_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            selected = make_candidate(risk_tier=0, allowed_paths=("src/**",))
            subject = self._subject(repo, base, selected)
            with self.assertRaisesRegex(CandidateContractError, "Tier 0 path"):
                Verifier(
                    executables=self._trusted_tools(),
                    isolation_evidence=self._isolation(subject),
                ).verify(
                    repo,
                    base,
                    selected,
                    subject=subject,
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

    def test_unsafe_optional_system_tool_does_not_poison_safe_git(self) -> None:
        real_git = shutil.which("git")
        if not real_git:
            raise unittest.SkipTest("git is required")
        with tempfile.TemporaryDirectory() as tmp:
            unsafe_node = Path(tmp) / "node"
            unsafe_node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            unsafe_node.chmod(0o777)

            def fake_which(name: str, *, path: str | None = None) -> str | None:
                del path
                if name == "git":
                    return real_git
                if name == "node":
                    return str(unsafe_node)
                return None

            with patch(
                "vista_daily_maintainer.profiles.shutil.which",
                side_effect=fake_which,
            ):
                trusted = TrustedExecutables.system_defaults()

        self.assertEqual(trusted.resolve("git"), Path(real_git).resolve())
        with self.assertRaisesRegex(ValueError, "allowlist"):
            trusted.resolve("node")

    def test_verifier_refuses_missing_isolation_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "isolation evidence"):
            Verifier(
                executables=self._trusted_tools(),
            )

        with self.assertRaisesRegex(ValueError, "network isolation"):
            IsolationEvidence(
                subject_sha256="a" * 64,
                patch_sha256="b" * 64,
                network_isolated=False,
                credentials_absent=True,
                observed_by="unit-test-harness",
                evidence_sha256="d" * 64,
            )


if __name__ == "__main__":
    unittest.main()
