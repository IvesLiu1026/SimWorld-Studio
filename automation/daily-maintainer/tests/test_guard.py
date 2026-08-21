from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from vista_daily_maintainer.candidate import CandidateContractError
from vista_daily_maintainer.guard import DiffGuard, GuardLimits

try:
    from .helpers import init_repo, make_candidate
except ImportError:  # Support unittest discovery without an explicit top-level.
    from helpers import init_repo, make_candidate


class DiffGuardIntegrationTests(unittest.TestCase):
    def test_safe_bounded_text_change_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")

            report = DiffGuard().inspect(repo, base, make_candidate())

        self.assertTrue(report.ok, report.violations)
        self.assertEqual(report.production_files, 1)
        self.assertRegex(report.patch_sha256, r"^[0-9a-f]{64}$")

    def test_public_guard_rejects_direct_candidate_policy_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            workflow = repo / ".github/workflows/pwn.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: pwn\n", encoding="utf-8")

            with self.assertRaisesRegex(CandidateContractError, "V1 candidate policy"):
                DiffGuard().inspect(
                    repo,
                    base,
                    make_candidate(allowed_paths=("**",)),
                )

            with self.assertRaisesRegex(CandidateContractError, "Tier 0 path"):
                DiffGuard().inspect(
                    repo,
                    base,
                    make_candidate(risk_tier=0, allowed_paths=("src/**",)),
                )

    def test_self_modification_and_dependency_files_are_protected(self) -> None:
        paths = (
            "packages/widget/pyproject.toml",
            "src/.env.production",
            "src/.gitignore",
            "src/.coveragerc",
            "src/vitest.config.ts",
            "src/conftest.py",
            "src/runtime/launch.py",
            "src/unreal/launch.py",
            "src/vista.timer",
        )
        for relative in paths:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                base = init_repo(repo)
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("safe = true\n", encoding="utf-8")
                report = DiffGuard().inspect(
                    repo,
                    base,
                    make_candidate(
                        allowed_paths=(
                            "packages/**"
                            if relative.startswith("packages/")
                            else "src/**",
                        )
                    ),
                )
                self.assertIn(
                    "protected_path", {item.code for item in report.violations}
                )

    def test_allowlist_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            target = repo / "docs/outside.md"
            target.parent.mkdir(parents=True)
            target.write_text("outside\n", encoding="utf-8")
            report = DiffGuard().inspect(repo, base, make_candidate())
        self.assertIn("path_not_allowlisted", {item.code for item in report.violations})

    def test_broad_allowlist_cannot_cross_forbidden_authority(self) -> None:
        paths = (
            "packages/auth/login.py",
            "packages/network/client.py",
            "packages/credentials/loader.py",
            "packages/deploy/release.py",
        )
        for relative in paths:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                base = init_repo(repo)
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("VALUE = 1\n", encoding="utf-8")
                report = DiffGuard().inspect(
                    repo,
                    base,
                    make_candidate(allowed_paths=("packages/**",)),
                )
                self.assertIn(
                    "protected_path", {item.code for item in report.violations}
                )

    def test_symlink_and_symlink_parent_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/link.py").symlink_to("app.py")
            report = DiffGuard().inspect(repo, base, make_candidate())
            self.assertIn("symlink", {item.code for item in report.violations})

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            outside = Path(tmp) / "outside"
            outside.mkdir()
            base = init_repo(repo)
            (repo / "src/linked").symlink_to(outside, target_is_directory=True)
            (outside / "escape.py").write_text("VALUE = 2\n", encoding="utf-8")
            report = DiffGuard().inspect(
                repo,
                base,
                make_candidate(allowed_paths=("src/linked/**",)),
            )
            self.assertFalse(report.ok)

    def test_binary_and_secret_content_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/blob.dat").write_bytes(b"text\x00binary")
            report = DiffGuard().inspect(repo, base, make_candidate())
            self.assertIn("binary_file", {item.code for item in report.violations})

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            token = "github_pat_" + "A" * 82
            (repo / "src/app.py").write_text(f'TOKEN = "{token}"\n', encoding="utf-8")
            report = DiffGuard().inspect(repo, base, make_candidate())
            self.assertIn("secret_detected", {item.code for item in report.violations})

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text(
                "//registry.npmjs.org/:_authToken=npm_" + "A" * 40 + "\n",
                encoding="utf-8",
            )
            report = DiffGuard().inspect(repo, base, make_candidate())
            self.assertIn("secret_detected", {item.code for item in report.violations})

    def test_credential_basenames_and_auth_configs_are_protected(self) -> None:
        paths = (
            "docs/.npmrc",
            "docs/.yarnrc.yml",
            "docs/.pypirc",
            "docs/.netrc",
            "docs/.git-credentials",
            "docs/.docker/config.json",
            "docs/.config/gh/hosts.yml",
            "docs/.config/gcloud/application_default_credentials.json",
        )
        for relative in paths:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                base = init_repo(repo)
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("placeholder\n", encoding="utf-8")
                report = DiffGuard().inspect(
                    repo,
                    base,
                    make_candidate(allowed_paths=("docs/**",)),
                )
                self.assertIn(
                    "protected_path", {item.code for item in report.violations}
                )

    def test_untracked_whitespace_and_conflict_markers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/new.py").write_text(
                "VALUE = 1  \n<<<<<<< ours\n", encoding="utf-8"
            )
            report = DiffGuard().inspect(repo, base, make_candidate())
        codes = {item.code for item in report.violations}
        self.assertIn("whitespace_error", codes)
        self.assertIn("conflict_marker", codes)

    def test_line_and_file_limits_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            for index in range(4):
                (repo / f"src/new_{index}.py").write_text(
                    "VALUE = 1\n", encoding="utf-8"
                )
            report = DiffGuard().inspect(repo, base, make_candidate())
            self.assertIn(
                "production_file_limit", {item.code for item in report.violations}
            )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            (repo / "src/app.py").write_text(
                "".join(f"VALUE_{index} = {index}\n" for index in range(151)),
                encoding="utf-8",
            )
            report = DiffGuard().inspect(
                repo,
                base,
                make_candidate(),
                limits=GuardLimits(max_production_lines=150),
            )
            self.assertIn(
                "production_line_limit", {item.code for item in report.violations}
            )

    def test_deleted_assertion_and_added_skip_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(
                repo, {"tests/test_app.py": "def test_value():\n    assert 1 == 1\n"}
            )
            (repo / "tests/test_app.py").write_text(
                "def test_value():\n    pass\n", encoding="utf-8"
            )
            report = DiffGuard().inspect(
                repo,
                base,
                make_candidate(allowed_paths=("tests/**",)),
            )
            self.assertIn(
                "test_assertion_removed", {item.code for item in report.violations}
            )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(
                repo, {"tests/test_app.py": "def test_value():\n    assert 1 == 1\n"}
            )
            (repo / "tests/test_app.py").write_text(
                "import pytest\n\n@pytest.mark.skip(reason='later')\ndef test_value():\n    assert 1 == 1\n",
                encoding="utf-8",
            )
            report = DiffGuard().inspect(
                repo,
                base,
                make_candidate(allowed_paths=("tests/**",)),
            )
            self.assertIn("test_skip_added", {item.code for item in report.violations})

    def test_patch_digest_covers_untracked_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            target = repo / "src/new.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            first = DiffGuard().inspect(repo, base, make_candidate()).patch_sha256
            target.write_text("VALUE = 2\n", encoding="utf-8")
            second = DiffGuard().inspect(repo, base, make_candidate()).patch_sha256
        self.assertNotEqual(first, second)

    def test_patch_digest_covers_untracked_executable_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(repo)
            target = repo / "src/new.sh"
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o644)
            first = DiffGuard().inspect(repo, base, make_candidate()).patch_sha256
            target.chmod(0o755)
            second = DiffGuard().inspect(repo, base, make_candidate()).patch_sha256
        self.assertNotEqual(first, second)

    def test_nested_evidence_ledger_and_receipt_paths_are_protected(self) -> None:
        paths = (
            "docs/specs/home/evidence.md",
            "docs/review/ledger/items.md",
            "docs/maintenance/receipt-journal.md",
        )
        for relative in paths:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                base = init_repo(repo)
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("protected\n", encoding="utf-8")
                report = DiffGuard().inspect(
                    repo,
                    base,
                    make_candidate(allowed_paths=("docs/**",)),
                )
                self.assertIn(
                    "protected_path", {item.code for item in report.violations}
                )

    def test_json_schema_relaxation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(
                repo,
                {
                    "contracts/scene.schema.json": (
                        '{"type":"object","additionalProperties":false,'
                        '"required":["name"],"properties":{"name":{"type":"string"}}}\n'
                    )
                },
            )
            (repo / "contracts/scene.schema.json").write_text(
                '{"type":"object","additionalProperties":true,'
                '"required":[],"properties":{"name":{}}}\n',
                encoding="utf-8",
            )
            report = DiffGuard().inspect(
                repo,
                base,
                make_candidate(allowed_paths=("contracts/**",)),
            )
        self.assertIn("schema_weakening", {item.code for item in report.violations})

    def test_typescript_only_and_root_python_test_weakening_are_rejected(self) -> None:
        fixtures = (
            (
                "unit.test.ts",
                "test('value', () => expect(1).toBe(1));\n",
                "test.only('value', () => expect(1).toBe(1));\n",
                "test_focus_added",
            ),
            (
                "widget_test.py",
                "def test_value():\n    assert 1 == 1\n",
                "def test_value():\n    pass\n",
                "test_assertion_removed",
            ),
        )
        for relative, before, after, expected in fixtures:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                base = init_repo(repo, {relative: before})
                (repo / relative).write_text(after, encoding="utf-8")
                report = DiffGuard().inspect(
                    repo,
                    base,
                    make_candidate(allowed_paths=(relative,)),
                )
                self.assertIn(expected, {item.code for item in report.violations})

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are required")
    def test_git_base_must_be_an_exact_object_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_repo(repo)
            with self.assertRaisesRegex(ValueError, "base SHA"):
                DiffGuard().inspect(repo, "HEAD; touch pwn", make_candidate())


if __name__ == "__main__":
    unittest.main()
