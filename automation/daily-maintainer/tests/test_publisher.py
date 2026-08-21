from __future__ import annotations

import dataclasses
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from vista_daily_maintainer.publisher import (
    AUTOMATION_TRAILER,
    CANONICAL_REPOSITORY,
    DEFAULT_BRANCH,
    IVES_AUTHOR,
    CommitRecord,
    CommitSpec,
    DraftPullRequestSpec,
    ExistingPullRequest,
    GitIdentity,
    PatchSnapshot,
    PrincipalMode,
    PrincipalSnapshot,
    PublicationConflictError,
    PublicationContractError,
    PublicationOperationError,
    PublicationPolicy,
    PublicationPreflightError,
    Publisher,
    PullRequestRecord,
    PushSpec,
    RepositorySnapshot,
    VerifiedCheck,
    VerifiedPatch,
)


BASE_SHA = "a" * 40
COMMIT_SHA = "b" * 40
MOVED_SHA = "f" * 40
PATCH_SHA256 = "c" * 64
HEAD_SHA256 = "d" * 64
CHECK_SHA256 = "e" * 64
APP_ACTOR = "vista-world-maintainer[bot]"
APP_COMMITTER = GitIdentity(
    "VISTA World Maintainer",
    "vista-world-maintainer@users.noreply.github.com",
)
REQUIRED_PERMISSIONS = frozenset(
    {"metadata:read", "contents:write", "pull_requests:write"}
)


def make_verified(
    *,
    repository: str = CANONICAL_REPOSITORY,
    title: str = "Correct one documented contract drift",
    acceptance: tuple[str, ...] = ("The documented command matches the tested CLI.",),
    slug: str = "documented-contract-drift",
) -> VerifiedPatch:
    return VerifiedPatch.bind(
        repository=repository,
        run_date="2026-08-21",
        candidate_id="VW-DM-0001",
        candidate_slug=slug,
        candidate_title=title,
        risk_tier=0,
        acceptance=acceptance,
        changed_paths=("docs/guide.md", "tests/test_docs.py"),
        base_sha=BASE_SHA,
        patch_sha256=PATCH_SHA256,
        head_sha256=HEAD_SHA256,
        checks=(
            VerifiedCheck("git-diff-check", CHECK_SHA256),
            VerifiedCheck("daily-maintainer-core-tests", "1" * 64),
        ),
    )


def cli_principal(**changes: object) -> PrincipalSnapshot:
    values: dict[str, object] = {
        "mode": PrincipalMode.CLI_BOOTSTRAP,
        "actor": "IvesLiu1026",
        "repository": CANONICAL_REPOSITORY,
        "permissions": REQUIRED_PERMISSIONS,
        "repository_scoped": False,
        "is_admin": True,
        "can_bypass_branch_protection": True,
        "committer": IVES_AUTHOR,
    }
    values.update(changes)
    return PrincipalSnapshot(**values)  # type: ignore[arg-type]


def app_principal(**changes: object) -> PrincipalSnapshot:
    values: dict[str, object] = {
        "mode": PrincipalMode.GITHUB_APP,
        "actor": APP_ACTOR,
        "repository": CANONICAL_REPOSITORY,
        "permissions": REQUIRED_PERMISSIONS,
        "repository_scoped": True,
        "is_admin": False,
        "can_bypass_branch_protection": False,
        "committer": APP_COMMITTER,
    }
    values.update(changes)
    return PrincipalSnapshot(**values)  # type: ignore[arg-type]


class FakeGit:
    def __init__(
        self, events: list[tuple[object, ...]], verified: VerifiedPatch
    ) -> None:
        self.events = events
        self.snapshot = PatchSnapshot(
            base_sha=verified.base_sha,
            patch_sha256=verified.patch_sha256,
            head_sha256=verified.head_sha256,
            changed_paths=verified.changed_paths,
        )
        self.local_exists = False
        self.created_branch: tuple[str, str] | None = None
        self.commit_spec: CommitSpec | None = None
        self.push_spec: PushSpec | None = None
        self.commit_record = CommitRecord(
            head_sha=COMMIT_SHA,
            parent_sha=verified.base_sha,
            patch_sha256=verified.patch_sha256,
            head_sha256=verified.head_sha256,
            message="placeholder",
            author=IVES_AUTHOR,
            committer=IVES_AUTHOR,
        )
        self.fail_stage: str | None = None

    def inspect_patch(self, worktree: Path, base_sha: str) -> PatchSnapshot:
        self.events.append(("git.inspect_patch", base_sha))
        self._maybe_fail("inspect_patch")
        return self.snapshot

    def local_branch_exists(self, worktree: Path, branch: str) -> bool:
        self.events.append(("git.local_branch_exists", branch))
        self._maybe_fail("local_branch_exists")
        return self.local_exists

    def create_branch(self, worktree: Path, branch: str, start_sha: str) -> None:
        self.events.append(("git.create_branch", branch, start_sha))
        self._maybe_fail("create_branch")
        self.created_branch = (branch, start_sha)

    def commit(self, worktree: Path, spec: CommitSpec) -> CommitRecord:
        self.events.append(("git.commit",))
        self._maybe_fail("commit")
        self.commit_spec = spec
        if self.commit_record.message == "placeholder":
            self.commit_record = replace(
                self.commit_record,
                message=spec.message,
                author=spec.author,
                committer=spec.committer,
            )
        return self.commit_record

    def push_new_branch(self, worktree: Path, spec: PushSpec) -> None:
        self.events.append(("git.push_new_branch", spec.branch, spec.force))
        self._maybe_fail("push_new_branch")
        self.push_spec = spec

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError("github_pat_" + "S" * 82)


class FakeGitHub:
    def __init__(
        self,
        events: list[tuple[object, ...]],
        verified: VerifiedPatch,
        principal: PrincipalSnapshot,
    ) -> None:
        self.events = events
        self.principal = principal
        self.repository = RepositorySnapshot(
            repository=CANONICAL_REPOSITORY,
            default_branch=DEFAULT_BRANCH,
            main_sha=verified.base_sha,
            public=True,
            is_fork=False,
        )
        self.remote_branch_sha: str | None = None
        self.existing_prs: tuple[ExistingPullRequest, ...] = ()
        self.main_reads: list[str | None] = [verified.base_sha] * 8
        self.opened_spec: DraftPullRequestSpec | None = None
        self.result_actor = principal.actor
        self.result_draft = True
        self.fail_stage: str | None = None

    def inspect_principal(
        self, repository: str, mode: PrincipalMode
    ) -> PrincipalSnapshot:
        self.events.append(("gh.inspect_principal", repository, mode.value))
        self._maybe_fail("inspect_principal")
        return self.principal

    def inspect_repository(self, repository: str) -> RepositorySnapshot:
        self.events.append(("gh.inspect_repository", repository))
        self._maybe_fail("inspect_repository")
        return self.repository

    def read_branch_sha(self, repository: str, branch: str) -> str | None:
        self.events.append(("gh.read_branch_sha", branch))
        self._maybe_fail("read_branch_sha")
        if branch == DEFAULT_BRANCH:
            return self.main_reads.pop(0)
        return self.remote_branch_sha

    def list_pull_requests(
        self, repository: str, head_branch: str
    ) -> tuple[ExistingPullRequest, ...]:
        self.events.append(("gh.list_pull_requests", head_branch))
        self._maybe_fail("list_pull_requests")
        return self.existing_prs

    def open_draft_pull_request(self, spec: DraftPullRequestSpec) -> PullRequestRecord:
        self.events.append(("gh.open_draft_pull_request", spec.head_branch, spec.draft))
        self._maybe_fail("open_draft_pull_request")
        self.opened_spec = spec
        return PullRequestRecord(
            number=7,
            url=f"https://github.com/{CANONICAL_REPOSITORY}/pull/7",
            repository=CANONICAL_REPOSITORY,
            base_branch=DEFAULT_BRANCH,
            head_branch=spec.head_branch,
            actor=self.result_actor,
            draft=self.result_draft,
        )

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError("github_pat_" + "S" * 82)


class PublisherFixture:
    def __init__(
        self,
        root: Path,
        *,
        verified: VerifiedPatch | None = None,
        principal: PrincipalSnapshot | None = None,
        environment_keys: tuple[str, ...] = ("PATH",),
    ) -> None:
        self.verified = verified or make_verified()
        self.events: list[tuple[object, ...]] = []
        self.git = FakeGit(self.events, self.verified)
        self.github = FakeGitHub(
            self.events,
            self.verified,
            principal or cli_principal(),
        )
        self.publisher = Publisher(
            git=self.git,
            github=self.github,
            environment_keys=environment_keys,
        )
        self.root = root

    def publish(self, policy: PublicationPolicy | None = None):
        return self.publisher.publish(
            self.root,
            self.verified,
            policy
            or PublicationPolicy(
                PrincipalMode.CLI_BOOTSTRAP,
                expected_actor="IvesLiu1026",
                unattended=False,
            ),
        )


class PublisherContractTests(unittest.TestCase):
    def test_cli_bootstrap_happy_path_orders_calls_and_preserves_attribution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = PublisherFixture(Path(tmp))
            result = fixture.publish()

        branch = "codex/daily/2026-08-21-documented-contract-drift"
        self.assertEqual(
            fixture.events,
            [
                ("gh.inspect_principal", CANONICAL_REPOSITORY, "cli_bootstrap"),
                ("gh.inspect_repository", CANONICAL_REPOSITORY),
                ("git.inspect_patch", BASE_SHA),
                ("git.local_branch_exists", branch),
                ("gh.read_branch_sha", branch),
                ("gh.list_pull_requests", branch),
                ("gh.read_branch_sha", DEFAULT_BRANCH),
                ("git.create_branch", branch, BASE_SHA),
                ("git.inspect_patch", BASE_SHA),
                ("git.commit",),
                ("gh.read_branch_sha", DEFAULT_BRANCH),
                ("git.push_new_branch", branch, False),
                ("gh.read_branch_sha", DEFAULT_BRANCH),
                ("gh.open_draft_pull_request", branch, True),
            ],
        )
        self.assertEqual(result.branch, branch)
        self.assertEqual(result.commit_author, IVES_AUTHOR)
        self.assertEqual(result.commit_committer, IVES_AUTHOR)
        self.assertEqual(result.pr_actor, "IvesLiu1026")
        self.assertTrue(result.draft)
        self.assertEqual(result.patch_sha256, PATCH_SHA256)

        commit = fixture.git.commit_spec
        self.assertIsNotNone(commit)
        assert commit is not None
        self.assertEqual(commit.paths, fixture.verified.changed_paths)
        self.assertEqual(commit.author, IVES_AUTHOR)
        self.assertEqual(commit.committer, IVES_AUTHOR)
        self.assertFalse(commit.amend)
        self.assertEqual(commit.message.count(AUTOMATION_TRAILER), 1)
        self.assertTrue(commit.message.endswith(f"\n{AUTOMATION_TRAILER}\n"))

        push = fixture.git.push_spec
        self.assertIsNotNone(push)
        assert push is not None
        self.assertFalse(push.force)
        self.assertEqual(push.expected_main_sha, BASE_SHA)

        opened = fixture.github.opened_spec
        self.assertIsNotNone(opened)
        assert opened is not None
        self.assertTrue(opened.draft)
        self.assertEqual(opened.repository, CANONICAL_REPOSITORY)
        self.assertEqual(opened.base_branch, DEFAULT_BRANCH)
        self.assertEqual(opened.body.count(AUTOMATION_TRAILER), 1)

    def test_unattended_app_uses_separate_non_admin_principal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = PublisherFixture(Path(tmp), principal=app_principal())
            result = fixture.publish(
                PublicationPolicy(
                    PrincipalMode.GITHUB_APP,
                    expected_actor=APP_ACTOR,
                    unattended=True,
                )
            )

        self.assertEqual(result.commit_author, IVES_AUTHOR)
        self.assertEqual(result.commit_committer, APP_COMMITTER)
        self.assertEqual(result.pr_actor, APP_ACTOR)
        self.assertTrue(result.draft)
        assert fixture.git.commit_spec is not None
        self.assertEqual(fixture.git.commit_spec.author, IVES_AUTHOR)
        self.assertEqual(fixture.git.commit_spec.committer, APP_COMMITTER)

    def test_wrong_repository_is_rejected_while_binding_evidence(self) -> None:
        with self.assertRaisesRegex(PublicationContractError, "repository"):
            make_verified(repository="IvesLiu1026/SimWorld-Studio")

    def test_wrong_actor_or_mode_fails_before_git_inspection(self) -> None:
        cases = (
            (
                app_principal(actor="another-app[bot]"),
                PublicationPolicy(
                    PrincipalMode.GITHUB_APP,
                    expected_actor=APP_ACTOR,
                    unattended=True,
                ),
            ),
            (
                app_principal(mode=PrincipalMode.CLI_BOOTSTRAP),
                PublicationPolicy(
                    PrincipalMode.GITHUB_APP,
                    expected_actor=APP_ACTOR,
                    unattended=True,
                ),
            ),
            (
                cli_principal(actor="not-ives"),
                PublicationPolicy(
                    PrincipalMode.CLI_BOOTSTRAP,
                    expected_actor="not-ives",
                    unattended=False,
                ),
            ),
        )
        for principal, policy in cases:
            with (
                self.subTest(principal=principal),
                tempfile.TemporaryDirectory() as tmp,
            ):
                fixture = PublisherFixture(Path(tmp), principal=principal)
                with self.assertRaises(PublicationPreflightError):
                    fixture.publish(policy)
                self.assertEqual(len(fixture.events), 1)
                self.assertEqual(fixture.events[0][0], "gh.inspect_principal")

    def test_unattended_policy_rejects_cli_admin_bypass_scope_and_permissions(
        self,
    ) -> None:
        with self.assertRaisesRegex(PublicationContractError, "GitHub App"):
            PublicationPolicy(
                PrincipalMode.CLI_BOOTSTRAP,
                expected_actor="IvesLiu1026",
                unattended=True,
            )

        cases = (
            app_principal(repository_scoped=False),
            app_principal(is_admin=True),
            app_principal(can_bypass_branch_protection=True),
            app_principal(permissions=frozenset({"metadata:read"})),
            app_principal(
                permissions=REQUIRED_PERMISSIONS | frozenset({"actions:write"})
            ),
        )
        policy = PublicationPolicy(
            PrincipalMode.GITHUB_APP,
            expected_actor=APP_ACTOR,
            unattended=True,
        )
        for principal in cases:
            with (
                self.subTest(principal=principal),
                tempfile.TemporaryDirectory() as tmp,
            ):
                fixture = PublisherFixture(Path(tmp), principal=principal)
                with self.assertRaises(PublicationPreflightError):
                    fixture.publish(policy)
                self.assertEqual(
                    [item[0] for item in fixture.events], ["gh.inspect_principal"]
                )

    def test_model_or_cli_credentials_in_environment_fail_before_port_calls(
        self,
    ) -> None:
        cases = (
            (("PATH", "OPENAI_API_KEY"), False),
            (("PATH", "openai_api_key"), False),
            (("PATH", "CODEX_HOME"), False),
            (("PATH", "GITHUB_TOKEN"), True),
            (("PATH", "SSH_AUTH_SOCK"), True),
        )
        for environment_keys, unattended in cases:
            with (
                self.subTest(keys=environment_keys),
                tempfile.TemporaryDirectory() as tmp,
            ):
                principal = app_principal() if unattended else cli_principal()
                fixture = PublisherFixture(
                    Path(tmp),
                    principal=principal,
                    environment_keys=environment_keys,
                )
                policy = PublicationPolicy(
                    PrincipalMode.GITHUB_APP
                    if unattended
                    else PrincipalMode.CLI_BOOTSTRAP,
                    expected_actor=APP_ACTOR if unattended else "IvesLiu1026",
                    unattended=unattended,
                )
                with self.assertRaises(PublicationPreflightError):
                    fixture.publish(policy)
                self.assertEqual(fixture.events, [])

    def test_remote_main_mismatch_fails_before_patch_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = PublisherFixture(Path(tmp))
            fixture.github.repository = replace(
                fixture.github.repository, main_sha=MOVED_SHA
            )
            with self.assertRaisesRegex(PublicationPreflightError, "remote main"):
                fixture.publish()

        self.assertEqual(
            [event[0] for event in fixture.events],
            ["gh.inspect_principal", "gh.inspect_repository"],
        )
        self.assertIsNone(fixture.git.created_branch)

    def test_any_verified_patch_digest_or_path_change_fails_before_mutation(
        self,
    ) -> None:
        changes = (
            {"base_sha": MOVED_SHA},
            {"patch_sha256": "2" * 64},
            {"head_sha256": "3" * 64},
            {"changed_paths": ("docs/other.md",)},
        )
        for changed in changes:
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                fixture = PublisherFixture(Path(tmp))
                fixture.git.snapshot = replace(fixture.git.snapshot, **changed)
                with self.assertRaises(PublicationPreflightError):
                    fixture.publish()
                self.assertIsNone(fixture.git.created_branch)
                self.assertNotIn("git.commit", [item[0] for item in fixture.events])

    def test_verification_binding_is_immutable_and_detects_tampering(self) -> None:
        verified = make_verified()
        with self.assertRaises(FrozenInstanceError):
            verified.patch_sha256 = "9" * 64  # type: ignore[misc]
        with self.assertRaisesRegex(PublicationContractError, "binding digest"):
            replace(verified, patch_sha256="9" * 64)

    def test_duplicate_local_remote_branch_or_any_prior_pr_is_rejected(self) -> None:
        branch = make_verified().branch
        configurations = ("local", "remote", "pr")
        for configuration in configurations:
            with (
                self.subTest(configuration=configuration),
                tempfile.TemporaryDirectory() as tmp,
            ):
                fixture = PublisherFixture(Path(tmp))
                if configuration == "local":
                    fixture.git.local_exists = True
                elif configuration == "remote":
                    fixture.github.remote_branch_sha = COMMIT_SHA
                else:
                    fixture.github.existing_prs = (
                        ExistingPullRequest(7, "closed", branch),
                    )
                with self.assertRaises(PublicationConflictError):
                    fixture.publish()
                self.assertIsNone(fixture.git.created_branch)
                self.assertIsNone(fixture.git.push_spec)
                self.assertIsNone(fixture.github.opened_spec)

    def test_remote_main_movement_stops_before_each_remaining_write(self) -> None:
        scenarios = (
            ([MOVED_SHA], False, False, False),
            ([BASE_SHA, MOVED_SHA], True, False, False),
            ([BASE_SHA, BASE_SHA, MOVED_SHA], True, True, False),
        )
        for reads, committed, pushed, opened in scenarios:
            with self.subTest(reads=reads), tempfile.TemporaryDirectory() as tmp:
                fixture = PublisherFixture(Path(tmp))
                fixture.github.main_reads = list(reads)
                with self.assertRaisesRegex(
                    PublicationPreflightError, "remote main moved"
                ):
                    fixture.publish()
                self.assertEqual(fixture.git.commit_spec is not None, committed)
                self.assertEqual(fixture.git.push_spec is not None, pushed)
                self.assertEqual(fixture.github.opened_spec is not None, opened)

    def test_commit_result_must_echo_verified_digest_author_and_committer(self) -> None:
        bad_identity = GitIdentity("Wrong Author", "wrong@example.invalid")
        cases = (
            {"parent_sha": MOVED_SHA},
            {"patch_sha256": "2" * 64},
            {"head_sha256": "3" * 64},
            {"author": bad_identity},
            {"committer": bad_identity},
            {"message": "chore: forged\n"},
        )
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as tmp:
                fixture = PublisherFixture(Path(tmp))
                overrides = {"message": "will-not-be-placeholder"}
                overrides.update(changes)
                fixture.git.commit_record = replace(
                    fixture.git.commit_record,
                    **overrides,
                )
                with self.assertRaises(PublicationPreflightError):
                    fixture.publish()
                self.assertIsNone(fixture.git.push_spec)
                self.assertIsNone(fixture.github.opened_spec)

    def test_pr_body_is_sanitized_deterministic_and_contains_no_raw_output(
        self,
    ) -> None:
        acceptance = (
            "Fix [link](https://example.invalid) @team <script>*boom*</script>.",
        )
        verified = make_verified(acceptance=acceptance)
        bodies: list[str] = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                fixture = PublisherFixture(Path(tmp), verified=verified)
                fixture.publish()
                assert fixture.github.opened_spec is not None
                bodies.append(fixture.github.opened_spec.body)

        self.assertEqual(bodies[0], bodies[1])
        self.assertNotIn("<script>", bodies[0])
        self.assertNotIn("@team", bodies[0])
        self.assertIn("&lt;script&gt;", bodies[0])
        self.assertIn("&#64;team", bodies[0])
        self.assertNotIn("stdout", bodies[0])
        self.assertNotIn("stderr", bodies[0])
        self.assertEqual(bodies[0].count(AUTOMATION_TRAILER), 1)

    def test_credential_like_material_is_rejected_from_verified_input(self) -> None:
        with self.assertRaisesRegex(PublicationContractError, "credential-like"):
            make_verified(acceptance=("Use OPENAI_API_KEY=" + "sk-" + "A" * 30,))

    def test_invalid_slug_cannot_escape_deterministic_branch_namespace(self) -> None:
        for slug in ("../main", "Uppercase", "ends-", "daily/other"):
            with (
                self.subTest(slug=slug),
                self.assertRaisesRegex(PublicationContractError, "slug"),
            ):
                make_verified(slug=slug)

    def test_publisher_surface_has_no_promotion_or_auto_merge_control(self) -> None:
        pr_fields = {field.name for field in dataclasses.fields(DraftPullRequestSpec)}
        result_fields = {
            field.name
            for field in dataclasses.fields(
                __import__(
                    "vista_daily_maintainer.publisher",
                    fromlist=["PublicationResult"],
                ).PublicationResult
            )
        }
        for forbidden in ("auto_merge", "merge", "promote", "ready_for_review"):
            self.assertNotIn(forbidden, pr_fields)
            self.assertNotIn(forbidden, result_fields)
        self.assertEqual(
            DraftPullRequestSpec.__dataclass_fields__["draft"].default, True
        )

    def test_non_draft_or_wrong_actor_pr_result_is_rejected(self) -> None:
        cases = ((False, "IvesLiu1026"), (True, "someone-else"))
        for draft, actor in cases:
            with (
                self.subTest(draft=draft, actor=actor),
                tempfile.TemporaryDirectory() as tmp,
            ):
                fixture = PublisherFixture(Path(tmp))
                fixture.github.result_draft = draft
                fixture.github.result_actor = actor
                with self.assertRaises(PublicationPreflightError):
                    fixture.publish()
                self.assertIsNotNone(fixture.git.push_spec)
                self.assertIsNotNone(fixture.github.opened_spec)

    def test_dependency_error_is_redacted_and_no_later_write_occurs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = PublisherFixture(Path(tmp))
            fixture.github.fail_stage = "inspect_repository"
            with self.assertRaises(PublicationOperationError) as captured:
                fixture.publish()

        self.assertNotIn("github_pat_", str(captured.exception))
        self.assertIsNone(fixture.git.created_branch)
        self.assertIsNone(fixture.github.opened_spec)


if __name__ == "__main__":
    unittest.main()
