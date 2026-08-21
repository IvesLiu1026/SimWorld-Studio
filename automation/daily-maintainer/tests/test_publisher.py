from __future__ import annotations

import dataclasses
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from vista_daily_maintainer.publisher import (
    AUTOMATION_TRAILER,
    CANONICAL_REPOSITORY,
    DEFAULT_BRANCH,
    FINALIZED_ENVELOPE_SCHEMA,
    IVES_AUTHOR,
    PROTECTED_POLICY_SCHEMA,
    PUBLISHER_ENVIRONMENT_ALLOWLIST,
    CommitRecord,
    DraftPullRequestSpec,
    FinalizedEnvelopeReference,
    GitIdentity,
    LocalBranchSnapshot,
    PatchSnapshot,
    PrincipalMode,
    PrincipalSnapshot,
    ProtectedPolicyReference,
    PublicationConflictError,
    PublicationContractError,
    PublicationOperationError,
    PublicationPreflightError,
    Publisher,
    PullRequestSnapshot,
    RepositorySnapshot,
    RuntimeAttestation,
)


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
MOVED_SHA = "f" * 40
PATCH_SHA256 = "c" * 64
HEAD_SHA256 = "d" * 64
FINAL_GUARD_SHA256 = "e" * 64
ISOLATION_SHA256 = "1" * 64
APP_ACTOR = "vista-world-maintainer[bot]"
APP_COMMITTER = GitIdentity(
    "VISTA World Maintainer",
    "vista-world-maintainer@users.noreply.github.com",
)
PERMISSIONS = ("contents:write", "metadata:read", "pull_requests:write")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class Check:
    command_id: str
    output_sha256: str
    exit_code: int = 0
    timed_out: bool = False


class Envelope:
    def __init__(self, **changes: object) -> None:
        values: dict[str, object] = {
            "schema_version": FINALIZED_ENVELOPE_SCHEMA,
            "finalized": True,
            "run_date": "2026-08-21",
            "repository": CANONICAL_REPOSITORY,
            "base_sha": BASE_SHA,
            "candidate_id": "VW-DM-0001",
            "candidate_slug": "docs-contract",
            "candidate_title": "Correct one documented contract drift",
            "risk_tier": 0,
            "acceptance": ("The documented command matches the tested CLI.",),
            "allowed_paths": ("docs/**",),
            "changed_paths": ("docs/guide.md",),
            "validation_profile_ids": ("daily-maintainer-core-tests",),
            "expected_external_side_effects": "none",
            "source_kind": "curated_backlog",
            "source_manifest_revision": 7,
            "source_approved_by": "IvesLiu1026",
            "guard_ok": True,
            "guard_patch_sha256": PATCH_SHA256,
            "final_guard_ok": True,
            "final_guard_patch_sha256": PATCH_SHA256,
            "final_guard_sha256": FINAL_GUARD_SHA256,
            "head_sha256": HEAD_SHA256,
            "mutation_detected": False,
            "isolation_network_isolated": True,
            "isolation_credentials_absent": True,
            "isolation_verified_by": "outer-sandbox-controller",
            "isolation_evidence_sha256": ISOLATION_SHA256,
            "checks": (
                Check("git-diff-check", "2" * 64),
                Check("daily-maintainer-core-tests", "3" * 64),
            ),
        }
        values.update(changes)
        values.setdefault(
            "run_id",
            f"{values['run_date']}/{values['repository']}@{values['base_sha']}",
        )
        for name, value in values.items():
            setattr(self, name, value)
        self.canonical_bytes = self._canonical_bytes()

    def _canonical_bytes(self) -> bytes:
        return canonical_json(
            {
                "schema_version": self.schema_version,
                "finalized": self.finalized,
                "run_id": self.run_id,
                "run_date": self.run_date,
                "repository": self.repository,
                "base_sha": self.base_sha,
                "candidate_id": self.candidate_id,
                "candidate_slug": self.candidate_slug,
                "candidate_title": self.candidate_title,
                "risk_tier": self.risk_tier,
                "acceptance": list(self.acceptance),
                "allowed_paths": list(self.allowed_paths),
                "changed_paths": list(self.changed_paths),
                "validation_profile_ids": list(self.validation_profile_ids),
                "expected_external_side_effects": self.expected_external_side_effects,
                "source_kind": self.source_kind,
                "source_manifest_revision": self.source_manifest_revision,
                "source_approved_by": self.source_approved_by,
                "guard_ok": self.guard_ok,
                "guard_patch_sha256": self.guard_patch_sha256,
                "final_guard_ok": self.final_guard_ok,
                "final_guard_patch_sha256": self.final_guard_patch_sha256,
                "final_guard_sha256": self.final_guard_sha256,
                "head_sha256": self.head_sha256,
                "mutation_detected": self.mutation_detected,
                "isolation_network_isolated": self.isolation_network_isolated,
                "isolation_credentials_absent": self.isolation_credentials_absent,
                "isolation_verified_by": self.isolation_verified_by,
                "isolation_evidence_sha256": self.isolation_evidence_sha256,
                "checks": [dataclasses.asdict(item) for item in self.checks],
            }
        )


class Policy:
    def __init__(self, *, app: bool = False, **changes: object) -> None:
        values: dict[str, object] = {
            "schema_version": PROTECTED_POLICY_SCHEMA,
            "policy_id": "publisher-v1",
            "repository": CANONICAL_REPOSITORY,
            "principal_mode": (
                PrincipalMode.GITHUB_APP if app else PrincipalMode.CLI_BOOTSTRAP
            ),
            "expected_actor": APP_ACTOR if app else "IvesLiu1026",
            "app_id": 1234 if app else None,
            "installation_id": 5678 if app else None,
            "permissions": PERMISSIONS,
            "unattended": app,
            "committer_name": APP_COMMITTER.name if app else IVES_AUTHOR.name,
            "committer_email": APP_COMMITTER.email if app else IVES_AUTHOR.email,
            "publisher_uid": 23456,
            "publisher_home": "/var/lib/vista-world-publisher",
            "environment_keys": PUBLISHER_ENVIRONMENT_ALLOWLIST,
            "runtime_attestor": "systemd-publisher-boundary",
        }
        values.update(changes)
        for name, value in values.items():
            setattr(self, name, value)
        self.canonical_bytes = canonical_json(
            {
                "schema_version": self.schema_version,
                "policy_id": self.policy_id,
                "repository": self.repository,
                "principal_mode": PrincipalMode(self.principal_mode).value,
                "expected_actor": self.expected_actor,
                "app_id": self.app_id,
                "installation_id": self.installation_id,
                "permissions": list(self.permissions),
                "unattended": self.unattended,
                "committer_name": self.committer_name,
                "committer_email": self.committer_email,
                "publisher_uid": self.publisher_uid,
                "publisher_home": self.publisher_home,
                "environment_keys": list(self.environment_keys),
                "runtime_attestor": self.runtime_attestor,
            }
        )


class FakeTrust:
    def __init__(self, envelope: Envelope, policy: Policy) -> None:
        self.envelope = envelope
        self.policy = policy

    def read_finalized_envelope(self, reference: FinalizedEnvelopeReference):
        return self.envelope

    def read_protected_policy(self, reference: ProtectedPolicyReference):
        return self.policy


class FakeRuntime:
    def __init__(self, policy: Policy) -> None:
        self.value = RuntimeAttestation.attest(
            uid=policy.publisher_uid,
            home=policy.publisher_home,
            environment_keys=policy.environment_keys,
            dedicated_uid=True,
            home_model_credentials_absent=True,
            environment_model_credentials_absent=True,
            model_credential_keys=(),
            attested_by=policy.runtime_attestor,
        )

    def inspect_runtime(self) -> RuntimeAttestation:
        return self.value


def make_principal(policy: Policy) -> PrincipalSnapshot:
    return PrincipalSnapshot(
        mode=policy.principal_mode,
        actor=policy.expected_actor,
        app_id=policy.app_id,
        installation_id=policy.installation_id,
        repository=CANONICAL_REPOSITORY,
        permissions=policy.permissions,
        repository_scoped=policy.principal_mode is PrincipalMode.GITHUB_APP,
        is_admin=policy.principal_mode is PrincipalMode.CLI_BOOTSTRAP,
        can_bypass_branch_protection=(
            policy.principal_mode is PrincipalMode.CLI_BOOTSTRAP
        ),
        committer=GitIdentity(policy.committer_name, policy.committer_email),
    )


class FakeGitHub:
    def __init__(self, envelope: Envelope, policy: Policy) -> None:
        self.envelope = envelope
        self.principal = make_principal(policy)
        self.repository = RepositorySnapshot(
            repository=CANONICAL_REPOSITORY,
            default_branch=DEFAULT_BRANCH,
            main_sha=envelope.base_sha,
            public=True,
            is_fork=False,
        )
        self.branches: dict[str, str] = {DEFAULT_BRANCH: envelope.base_sha}
        self.commits: dict[str, CommitRecord] = {}
        self.pull_requests: list[PullRequestSnapshot] = []
        self.open_calls = 0
        self.fail_stage: str | None = None
        self.open_actor = policy.expected_actor
        self.open_draft = True

    def inspect_principal(self, repository: str, mode: PrincipalMode):
        self._fail("principal")
        return self.principal

    def inspect_repository(self, repository: str):
        self._fail("repository")
        return self.repository

    def read_branch_sha(self, repository: str, branch: str):
        self._fail("branch")
        return self.branches.get(branch)

    def read_commit(self, repository: str, head_sha: str):
        self._fail("commit")
        return self.commits[head_sha]

    def list_pull_requests(self, repository: str, head_branch: str):
        self._fail("list-pr")
        return tuple(
            item for item in self.pull_requests if item.head_branch == head_branch
        )

    def open_draft_pull_request(self, spec: DraftPullRequestSpec) -> None:
        self._fail("open-pr")
        self.open_calls += 1
        head_sha = self.branches[spec.head_branch]
        number = 7
        self.pull_requests.append(
            PullRequestSnapshot(
                number=number,
                url=f"https://github.com/{CANONICAL_REPOSITORY}/pull/{number}",
                repository=CANONICAL_REPOSITORY,
                base_branch=DEFAULT_BRANCH,
                head_branch=spec.head_branch,
                head_sha=head_sha,
                actor=self.open_actor,
                title=spec.title,
                body_sha256=spec.body_sha256,
                state="open",
                draft=self.open_draft,
            )
        )

    def _fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError("github_pat_" + "S" * 82)


class FakeGit:
    def __init__(self, envelope: Envelope, github: FakeGitHub) -> None:
        self.envelope = envelope
        self.github = github
        self.patch = PatchSnapshot(
            base_sha=envelope.base_sha,
            patch_sha256=envelope.final_guard_patch_sha256,
            head_sha256=envelope.head_sha256,
            changed_paths=envelope.changed_paths,
        )
        self.branches: dict[str, LocalBranchSnapshot] = {}
        self.commits: dict[str, CommitRecord] = {}
        self.commit_calls = 0
        self.push_calls = 0
        self.create_calls = 0
        self.bad_commit: dict[str, object] = {}
        self.skip_remote_branch_write = False

    def inspect_patch(self, worktree: Path, base_sha: str):
        return self.patch

    def read_local_branch(self, worktree: Path, branch: str):
        return self.branches.get(branch)

    def create_branch(self, worktree: Path, branch: str, start_sha: str) -> None:
        self.create_calls += 1
        self.branches[branch] = LocalBranchSnapshot(branch, start_sha, True)

    def commit(self, worktree: Path, spec) -> None:
        self.commit_calls += 1
        branch = next(name for name, item in self.branches.items() if item.checked_out)
        record = CommitRecord(
            head_sha=HEAD_SHA,
            parent_sha=self.envelope.base_sha,
            patch_sha256=self.envelope.final_guard_patch_sha256,
            head_sha256=self.envelope.head_sha256,
            message=spec.message,
            author=spec.author,
            committer=spec.committer,
        )
        record = replace(record, **self.bad_commit)
        self.commits[record.head_sha] = record
        self.branches[branch] = LocalBranchSnapshot(branch, record.head_sha, True)

    def inspect_commit(self, worktree: Path, head_sha: str):
        return self.commits[head_sha]

    def push_new_branch(self, worktree: Path, spec) -> None:
        self.push_calls += 1
        if not self.skip_remote_branch_write:
            self.github.branches[spec.branch] = spec.head_sha
            self.github.commits[spec.head_sha] = self.commits[spec.head_sha]


class Fixture:
    def __init__(
        self,
        root: Path,
        *,
        envelope: Envelope | None = None,
        policy: Policy | None = None,
    ) -> None:
        self.root = root
        self.envelope = envelope or Envelope()
        self.policy = policy or Policy()
        self.trust = FakeTrust(self.envelope, self.policy)
        self.runtime = FakeRuntime(self.policy)
        self.github = FakeGitHub(self.envelope, self.policy)
        self.git = FakeGit(self.envelope, self.github)
        self.publisher = Publisher(
            git=self.git,
            github=self.github,
            trust=self.trust,
            runtime=self.runtime,
        )

    @property
    def envelope_reference(self) -> FinalizedEnvelopeReference:
        return FinalizedEnvelopeReference(
            "run-envelope",
            hashlib.sha256(self.envelope.canonical_bytes).hexdigest(),
        )

    @property
    def policy_reference(self) -> ProtectedPolicyReference:
        return ProtectedPolicyReference(
            self.policy.policy_id,
            hashlib.sha256(self.policy.canonical_bytes).hexdigest(),
        )

    def publish(self):
        return self.publisher.publish(
            self.root,
            self.envelope_reference,
            self.policy_reference,
        )


class PublisherContractTests(unittest.TestCase):
    def test_cli_happy_path_is_draft_and_attributed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            result = fixture.publish()

        self.assertEqual(
            result.branch,
            "codex/daily/2026-08-21-docs-contract-aaaaaaaa",
        )
        self.assertEqual(result.commit_author, IVES_AUTHOR)
        self.assertEqual(result.commit_committer, IVES_AUTHOR)
        self.assertEqual(result.pr_actor, "IvesLiu1026")
        self.assertTrue(result.draft)
        self.assertEqual(fixture.git.commit_calls, 1)
        self.assertEqual(fixture.git.push_calls, 1)
        self.assertEqual(fixture.github.open_calls, 1)
        commit = fixture.github.commits[result.head_sha]
        self.assertEqual(commit.message.count(AUTOMATION_TRAILER), 1)

    def test_unattended_app_is_exact_non_admin_repo_scoped_principal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary), policy=Policy(app=True))
            result = fixture.publish()
        self.assertEqual(result.commit_author, IVES_AUTHOR)
        self.assertEqual(result.commit_committer, APP_COMMITTER)
        self.assertEqual(result.pr_actor, APP_ACTOR)

    def test_completed_publication_reconciles_without_duplicate_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            first = fixture.publish()
            second = fixture.publish()
        self.assertFalse(first.reconciled)
        self.assertTrue(second.reconciled)
        self.assertEqual(fixture.git.commit_calls, 1)
        self.assertEqual(fixture.git.push_calls, 1)
        self.assertEqual(fixture.github.open_calls, 1)

    def test_duplicate_pull_request_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.publish()
            fixture.github.pull_requests.append(fixture.github.pull_requests[0])
            with self.assertRaisesRegex(PublicationConflictError, "one branch"):
                fixture.publish()
        self.assertEqual(fixture.github.open_calls, 1)

    def test_remote_commit_without_pr_resumes_at_pr_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            initial = fixture.publish()
            fixture.github.pull_requests.clear()
            fixture.github.open_calls = 0
            fixture.git.branches.clear()
            result = fixture.publish()
        self.assertEqual(result.head_sha, initial.head_sha)
        self.assertTrue(result.reconciled)
        self.assertEqual(fixture.git.commit_calls, 1)
        self.assertEqual(fixture.git.push_calls, 1)
        self.assertEqual(fixture.github.open_calls, 1)

    def test_canonical_envelope_bytes_bind_every_field(self) -> None:
        envelope = Envelope()
        envelope.candidate_title = "tampered after finalization"
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary), envelope=envelope)
            with self.assertRaisesRegex(PublicationPreflightError, "canonical bytes"):
                fixture.publish()
        self.assertEqual(fixture.git.create_calls, 0)

    def test_canonical_policy_bytes_bind_every_field(self) -> None:
        policy = Policy()
        policy.runtime_attestor = "tampered-attestor"
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary), policy=policy)
            with self.assertRaisesRegex(PublicationPreflightError, "canonical bytes"):
                fixture.publish()
        self.assertEqual(fixture.git.create_calls, 0)

    def test_v1_authority_rejects_scope_profile_and_path_bypasses(self) -> None:
        cases = (
            Envelope(allowed_paths=("src/**",), changed_paths=("src/app.py",)),
            Envelope(
                validation_profile_ids=("unreal-content-contract",),
                checks=(
                    Check("git-diff-check", "2" * 64),
                    Check("unreal-content-contract", "3" * 64),
                ),
            ),
            Envelope(allowed_paths=("docs/foo**",), changed_paths=("docs/foo.md",)),
            Envelope(
                allowed_paths=("docs/guide.md",), changed_paths=("docs/other.md",)
            ),
            Envelope(source_approved_by="attacker"),
            Envelope(risk_tier=2),
        )
        for envelope in cases:
            with self.subTest(envelope=envelope), tempfile.TemporaryDirectory() as tmp:
                fixture = Fixture(Path(tmp), envelope=envelope)
                with self.assertRaises(PublicationPreflightError):
                    fixture.publish()
                self.assertEqual(fixture.git.create_calls, 0)

    def test_app_identity_installation_permissions_and_bypass_are_exact(self) -> None:
        policy = Policy(app=True)
        cases = (
            replace(make_principal(policy), actor="other[bot]"),
            replace(make_principal(policy), app_id=9999),
            replace(make_principal(policy), installation_id=9999),
            replace(make_principal(policy), permissions=("contents:write",)),
            replace(make_principal(policy), repository_scoped=False),
            replace(make_principal(policy), is_admin=True),
            replace(make_principal(policy), can_bypass_branch_protection=True),
        )
        for principal in cases:
            with (
                self.subTest(principal=principal),
                tempfile.TemporaryDirectory() as tmp,
            ):
                fixture = Fixture(Path(tmp), policy=policy)
                fixture.github.principal = principal
                with self.assertRaises(PublicationPreflightError):
                    fixture.publish()
                self.assertEqual(fixture.git.create_calls, 0)

    def test_cli_policy_cannot_claim_unattended_mode(self) -> None:
        policy = Policy(unattended=True)
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary), policy=policy)
            with self.assertRaisesRegex(PublicationPreflightError, "CLI bootstrap"):
                fixture.publish()

    def test_runtime_must_be_dedicated_and_model_credential_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.runtime.value = RuntimeAttestation.attest(
                uid=fixture.policy.publisher_uid,
                home=fixture.policy.publisher_home,
                environment_keys=fixture.policy.environment_keys,
                dedicated_uid=False,
                home_model_credentials_absent=True,
                environment_model_credentials_absent=True,
                model_credential_keys=(),
                attested_by=fixture.policy.runtime_attestor,
            )
            with self.assertRaisesRegex(PublicationPreflightError, "not dedicated"):
                fixture.publish()
        self.assertEqual(fixture.git.create_calls, 0)

    def test_remote_main_movement_stops_before_local_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.github.repository = replace(
                fixture.github.repository, main_sha=MOVED_SHA
            )
            with self.assertRaisesRegex(
                PublicationPreflightError, "current remote main"
            ):
                fixture.publish()
        self.assertEqual(fixture.git.create_calls, 0)

    def test_patch_commit_and_remote_readbacks_are_independently_bound(self) -> None:
        cases = (
            ("patch", {"patch_sha256": "9" * 64}),
            ("commit", {"author": GitIdentity("Wrong", "wrong@example.invalid")}),
        )
        for stage, changes in cases:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                fixture = Fixture(Path(tmp))
                if stage == "patch":
                    fixture.git.patch = replace(fixture.git.patch, **changes)
                else:
                    fixture.git.bad_commit = changes
                with self.assertRaises(PublicationPreflightError):
                    fixture.publish()
                self.assertEqual(fixture.git.push_calls, 0)
                self.assertEqual(fixture.github.open_calls, 0)

    def test_missing_remote_branch_readback_stops_before_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.git.skip_remote_branch_write = True
            with self.assertRaisesRegex(PublicationPreflightError, "branch read-back"):
                fixture.publish()
        self.assertEqual(fixture.github.open_calls, 0)

    def test_wrong_pr_actor_or_non_draft_readback_is_rejected(self) -> None:
        for actor, draft in (("someone-else", True), ("IvesLiu1026", False)):
            with (
                self.subTest(actor=actor, draft=draft),
                tempfile.TemporaryDirectory() as tmp,
            ):
                fixture = Fixture(Path(tmp))
                fixture.github.open_actor = actor
                fixture.github.open_draft = draft
                with self.assertRaises(PublicationPreflightError):
                    fixture.publish()
                self.assertEqual(fixture.github.open_calls, 1)

    def test_control_secret_and_markdown_injection_fail_or_escape(self) -> None:
        invalid = (
            Envelope(acceptance=("line one\nline two",)),
            Envelope(acceptance=("Use OPENAI_API_KEY with sk-" + "A" * 30,)),
            Envelope(changed_paths=("docs/bad\npath.md",)),
        )
        for envelope in invalid:
            with self.subTest(envelope=envelope), tempfile.TemporaryDirectory() as tmp:
                fixture = Fixture(Path(tmp), envelope=envelope)
                with self.assertRaises(
                    (PublicationPreflightError, PublicationContractError)
                ):
                    fixture.publish()

        safe = Envelope(
            acceptance=("Fix [link](https://example.invalid) @team <script>.",)
        )
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary), envelope=safe)
            fixture.publish()
            body_hash = fixture.github.pull_requests[0].body_sha256
        self.assertEqual(len(body_hash), 64)

    def test_port_errors_are_redacted_and_no_write_follows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.github.fail_stage = "repository"
            with self.assertRaises(PublicationOperationError) as captured:
                fixture.publish()
        self.assertNotIn("github_pat_", str(captured.exception))
        self.assertEqual(fixture.git.create_calls, 0)

    def test_publisher_has_no_promotion_or_merge_surface(self) -> None:
        pr_fields = {item.name for item in dataclasses.fields(DraftPullRequestSpec)}
        for forbidden in ("auto_merge", "merge", "promote", "ready_for_review"):
            self.assertNotIn(forbidden, pr_fields)
        self.assertTrue(DraftPullRequestSpec.__dataclass_fields__["draft"].default)


if __name__ == "__main__":
    unittest.main()
