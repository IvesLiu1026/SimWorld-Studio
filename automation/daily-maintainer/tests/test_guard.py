from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vista_daily_maintainer.candidate import CandidateContractError
from vista_daily_maintainer.guard import DiffGuard, GuardLimits

try:
    from .helpers import init_repo, make_candidate, run_git
except ImportError:  # Support unittest discovery without an explicit top-level.
    from helpers import init_repo, make_candidate, run_git


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
            "packages/widget/.node-version",
            "packages/widget/.nvmrc",
            "packages/widget/.pre-commit-config.yaml",
            "packages/widget/.pre-commit-config.yml",
            "packages/widget/.python-version",
            "packages/widget/.ruby-version",
            "packages/widget/.tool-versions",
            "packages/widget/.flake8",
            "packages/widget/.ruff.toml",
            "packages/widget/MANIFEST.in",
            "packages/widget/Pipfile",
            "packages/widget/Pipfile.lock",
            "packages/widget/conda-lock.yml",
            "packages/widget/environment.yml",
            "packages/widget/hatch.toml",
            "packages/widget/npm-shrinkwrap.json",
            "packages/widget/pdm.toml",
            "packages/widget/pixi.lock",
            "packages/widget/pixi.toml",
            "packages/widget/poetry.toml",
            "packages/widget/pyproject.toml",
            "packages/widget/ruff.toml",
            "packages/widget/setup.cfg",
            "packages/widget/setup.py",
            "packages/widget/sitecustomize.py",
            "packages/widget/uv.toml",
            "packages/widget/usercustomize.py",
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

    def test_candidate_and_guard_share_protected_basename_policy(self) -> None:
        basenames = (
            ".flake8",
            ".node-version",
            ".nvmrc",
            ".pre-commit-config.yaml",
            ".pre-commit-config.yml",
            ".pre-commit-hooks.yaml",
            ".python-version",
            ".ruff.toml",
            ".ruby-version",
            ".tool-versions",
            "MANIFEST.in",
            "Pipfile",
            "Pipfile.lock",
            "conda-lock.yml",
            "constraints.txt",
            "environment.yml",
            "hatch.toml",
            "mise.toml",
            "npm-shrinkwrap.json",
            "pdm.toml",
            "pixi.lock",
            "pixi.toml",
            "poetry.toml",
            "ruff.toml",
            "setup.cfg",
            "setup.py",
            "sitecustomize.py",
            "uv.toml",
            "usercustomize.py",
        )
        for basename in basenames:
            with self.subTest(basename=basename), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                base = init_repo(repo)
                relative = f"packages/widget/{basename}"
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("placeholder\n", encoding="utf-8")

                report = DiffGuard().inspect(
                    repo,
                    base,
                    make_candidate(allowed_paths=("packages/**",)),
                )
                self.assertIn(
                    "protected_path",
                    {item.code for item in report.violations},
                )
                with self.assertRaisesRegex(
                    CandidateContractError,
                    "V1 candidate policy",
                ):
                    DiffGuard().inspect(
                        repo,
                        base,
                        make_candidate(allowed_paths=(relative,)),
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
            "packages/auth.py",
            "packages/auth-client.py",
            "packages/auth_config.py",
            "packages/network/client.py",
            "packages/network.py",
            "packages/network-client.ts",
            "packages/credentials/loader.py",
            "packages/credentials.py",
            "packages/secret-store.py",
            "packages/deploy/release.py",
            "packages/infra/config.py",
            "packages/infrastructure/config.py",
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

    def test_authority_token_policy_preserves_safe_names_and_test_files(self) -> None:
        cases = (
            ("packages/authorization.py", "packages/**", "VALUE = 1\n"),
            ("packages/manifest_input.py", "packages/**", "VALUE = 1\n"),
            ("packages/setup_helper.py", "packages/**", "VALUE = 1\n"),
            ("packages/uv_helpers.py", "packages/**", "VALUE = 1\n"),
            ("docs/environment-notes.md", "docs/**", "# Environment notes\n"),
            ("docs/networking.md", "docs/**", "# Networking concepts\n"),
            (
                "tests/test_auth.py",
                "tests/**",
                "def test_auth_label_is_data():\n    assert 'auth' == 'auth'\n",
            ),
            (
                "tests/test_setup.py",
                "tests/**",
                "def test_setup_label_is_data():\n    assert 'setup' == 'setup'\n",
            ),
            (
                "tests/test_sitecustomize.py",
                "tests/**",
                "def test_sitecustomize_label_is_data():\n    assert True\n",
            ),
            (
                "tests/test_pixi.py",
                "tests/**",
                "def test_pixi_label_is_data():\n    assert True\n",
            ),
        )
        for relative, allowed, contents in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                base = init_repo(repo)
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")
                report = DiffGuard().inspect(
                    repo,
                    base,
                    make_candidate(allowed_paths=(allowed,)),
                )
                self.assertNotIn(
                    "protected_path", {item.code for item in report.violations}
                )
                self.assertTrue(report.ok, report.violations)

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

    def test_replacement_refs_cannot_rewrite_the_guard_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(
                repo,
                {"tests/test_target.py": "def test_target():\n    assert True\n"},
            )
            target = repo / "tests/test_target.py"
            target.write_text("def test_target():\n    pass\n", encoding="utf-8")
            run_git(repo, "add", "tests/test_target.py")
            run_git(repo, "commit", "-qm", "test: replacement fixture")
            replacement = run_git(repo, "rev-parse", "HEAD")
            target.write_text(
                "def test_target():\n    pass\n    # bounded addition\n",
                encoding="utf-8",
            )
            run_git(repo, "replace", base, replacement)

            with self.assertRaisesRegex(ValueError, "replacement refs are forbidden"):
                DiffGuard().inspect(
                    repo,
                    base,
                    make_candidate(allowed_paths=("tests/**",)),
                )

    def test_git_environment_disables_replacement_objects(self) -> None:
        self.assertEqual(
            DiffGuard._git_environment()["GIT_NO_REPLACE_OBJECTS"],
            "1",
        )

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

    def test_ignored_tree_is_collapsed_rejected_and_digest_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = init_repo(
                repo,
                {
                    ".gitignore": ".venv/\n",
                    "src/app.py": "VALUE = 1\n",
                },
            )
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            clean_digest = (
                DiffGuard().inspect(repo, base, make_candidate()).patch_sha256
            )
            ignored = repo / ".venv/lib/python/sitecustomize.py"
            ignored.parent.mkdir(parents=True)
            ignored.write_text("raise SystemExit('pwned')\n", encoding="utf-8")

            report = DiffGuard().inspect(repo, base, make_candidate())

        ignored_violations = [
            item for item in report.violations if item.code == "ignored_content"
        ]
        self.assertEqual([item.path for item in ignored_violations], [".venv/"])
        self.assertNotEqual(clean_digest, report.patch_sha256)
        self.assertFalse(report.ok)

    def test_ignored_enumeration_reporting_is_bounded(self) -> None:
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
            for index in range(140):
                (repo / "src" / f"{index:03}.cache").write_text(
                    "ignored\n",
                    encoding="utf-8",
                )

            report = DiffGuard().inspect(repo, base, make_candidate())

        ignored_violations = [
            item for item in report.violations if item.code == "ignored_content"
        ]
        self.assertLessEqual(len(ignored_violations), 128)
        self.assertIn(
            "ignored_content_overflow",
            {item.code for item in report.violations},
        )

    def test_ignored_git_output_cap_fails_during_capture(self) -> None:
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
            for index in range(20):
                (repo / f"ignored-{index:03}.cache").write_text(
                    "ignored\n",
                    encoding="utf-8",
                )

            with (
                patch(
                    "vista_daily_maintainer.guard._MAX_IGNORED_STATE_BYTES",
                    64,
                ),
                self.assertRaisesRegex(ValueError, "stdout exceeded output limit"),
            ):
                DiffGuard().inspect(repo, base, make_candidate())

    def test_git_subprocess_timeout_kills_the_bounded_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_git = root / "git"
            fake_git.write_text(
                "#!/usr/bin/python3\nimport time\ntime.sleep(5)\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            guard = DiffGuard(git_executable=fake_git)
            started = time.monotonic()
            with (
                patch("vista_daily_maintainer.guard._GIT_TIMEOUT_SECONDS", 0.05),
                self.assertRaisesRegex(ValueError, "git inspection timed out"),
            ):
                guard._git(root, "probe")
            self.assertLess(time.monotonic() - started, 1.0)

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
