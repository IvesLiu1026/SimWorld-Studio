from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol, TypeVar

from .candidate import has_v1_forbidden_authority
from .guard import _SECRET_PATTERNS as _GUARD_SECRET_PATTERNS
from .naming import (
    is_v1_candidate_slug,
    is_v1_daily_branch_name,
    v1_daily_branch_name,
)


CANONICAL_REPOSITORY = "IvesLiu1026/VISTA-World"
DEFAULT_BRANCH = "main"
AUTOMATION_TRAILER = "Automated-by: Codex Daily Maintainer"
FINALIZED_ENVELOPE_SCHEMA = "vista.world.daily-maintainer.finalized-verification.v1"
PROTECTED_POLICY_SCHEMA = "vista.world.daily-maintainer.publisher-policy.v1"
PUBLISHER_ENVIRONMENT_ALLOWLIST = (
    "GIT_CONFIG_NOSYSTEM",
    "GIT_TERMINAL_PROMPT",
    "HOME",
    "LANG",
    "LC_ALL",
    "NO_COLOR",
    "PATH",
    "TZ",
)

_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_ID = re.compile(r"^VW-DM-[0-9]{4,}$")
_COMMAND_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_PERMISSION = re.compile(r"^[a-z][a-z0-9_]*:(?:read|write)$")
_ACTOR = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})|"
    r"[A-Za-z0-9][A-Za-z0-9-]{0,93}\[bot\])$"
)
_EMAIL = re.compile(r"^[^\s<>@]+@[^\s<>@]+$")
_OPAQUE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BODY_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
_PUBLISHER_SECRET_PATTERNS = (
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(
        r"(?i)(?:^|[^A-Za-z0-9])(?:"
        r"(?:[A-Za-z0-9]+[_-])*(?:password|passwd|secret|"
        r"secret[_-]?access[_-]?key|api[_-]?key|access[_-]?token|"
        r"auth[_-]?token|token)|_?auth[_-]?token"
        r")\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:@-]{16,}"
    ),
    re.compile(
        r"(?i)(?:OPENAI|ANTHROPIC|CLAUDE|CODEX|GEMINI|OPENROUTER|"
        r"REPLICATE|COHERE|MISTRAL|GROQ|XAI)_(?:API_)?(?:KEY|TOKEN|AUTH)"
    ),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
)
_SECRET_PATTERNS = (*_GUARD_SECRET_PATTERNS, *_PUBLISHER_SECRET_PATTERNS)
_MODEL_ENVIRONMENT_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "CHATGPT_ACCESS_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_API_KEY",
        "CODEX_AUTH_TOKEN",
        "CODEX_HOME",
        "COHERE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "MISTRAL_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_ACCESS_TOKEN",
        "OPENROUTER_API_KEY",
        "REPLICATE_API_TOKEN",
        "XAI_API_KEY",
    }
)
_EXACT_APP_PERMISSIONS = (
    "contents:write",
    "metadata:read",
    "pull_requests:write",
)
_V1_PROFILE_IDS = frozenset(
    {
        "daily-maintainer-core-tests",
        "tools-python-offline",
        "web-frontend-build",
        "web-server-contracts",
        "web-server-unit",
    }
)
_V1_TIER1_PREFIXES = (
    "contracts/",
    "docs/",
    "packages/",
    "simworld_studio_workspace/web/server/",
    "simworld_studio_workspace/web/src/",
    "src/",
    "tests/",
    "tools/",
)
_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/*?-]+$")


class PublicationError(RuntimeError):
    """Base class for fail-closed publisher failures."""


class PublicationContractError(PublicationError, ValueError):
    """A reference, policy, or port value violates the publisher contract."""


class PublicationPreflightError(PublicationError):
    """A trust, identity, digest, runtime, or read-back check failed."""


class PublicationConflictError(PublicationError):
    """Existing publication state cannot be reconciled exactly."""


class PublicationOperationError(PublicationError):
    """A dependency operation failed without exposing its sensitive error."""


class PrincipalMode(str, Enum):
    CLI_BOOTSTRAP = "cli_bootstrap"
    GITHUB_APP = "github_app"


@dataclass(frozen=True)
class GitIdentity:
    name: str
    email: str

    def __post_init__(self) -> None:
        _safe_one_line(self.name, "git identity name", maximum=100)
        if not isinstance(self.email, str) or not _EMAIL.fullmatch(self.email):
            raise PublicationContractError("git identity email is invalid")
        _reject_secret_material((self.name, self.email), "git identity")


@dataclass(frozen=True)
class FinalizedEnvelopeReference:
    """Opaque lookup key plus digest pinned by the run manager's spool."""

    spool_id: str
    envelope_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.spool_id, str) or not _OPAQUE_ID.fullmatch(
            self.spool_id
        ):
            raise PublicationContractError("finalized spool ID is invalid")
        _require_sha256(self.envelope_sha256, "finalized envelope digest")


@dataclass(frozen=True)
class ProtectedPolicyReference:
    """Opaque lookup key plus digest of operator-owned publisher policy."""

    policy_id: str
    policy_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not _OPAQUE_ID.fullmatch(
            self.policy_id
        ):
            raise PublicationContractError("protected policy ID is invalid")
        _require_sha256(self.policy_sha256, "protected policy digest")


class VerificationCheckView(Protocol):
    command_id: str
    output_sha256: str
    exit_code: int
    timed_out: bool


class FinalizedVerifierEnvelope(Protocol):
    """Adapter projection over the finalized core ``VerificationReport``.

    The adapter is responsible for producing canonical bytes from the core's
    initial/final ``GuardReport``, ``IsolationAttestation`` and validation
    results. The publisher never accepts a patcher-created evidence dataclass.
    """

    canonical_bytes: bytes
    schema_version: str
    finalized: bool
    run_id: str
    run_date: str
    repository: str
    base_sha: str
    candidate_id: str
    candidate_slug: str
    candidate_title: str
    risk_tier: int
    acceptance: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    validation_profile_ids: tuple[str, ...]
    expected_external_side_effects: str
    source_kind: str
    source_manifest_revision: int
    source_approved_by: str
    guard_ok: bool
    guard_patch_sha256: str
    final_guard_ok: bool
    final_guard_patch_sha256: str
    final_guard_sha256: str
    head_sha256: str
    mutation_detected: bool
    isolation_network_isolated: bool
    isolation_credentials_absent: bool
    isolation_verified_by: str
    isolation_evidence_sha256: str
    checks: tuple[VerificationCheckView, ...]


class ProtectedPublisherPolicy(Protocol):
    canonical_bytes: bytes
    schema_version: str
    policy_id: str
    repository: str
    principal_mode: PrincipalMode
    expected_actor: str
    app_id: int | None
    installation_id: int | None
    permissions: tuple[str, ...]
    unattended: bool
    committer_name: str
    committer_email: str
    publisher_uid: int
    publisher_home: str
    environment_keys: tuple[str, ...]
    runtime_attestor: str


class TrustMaterialPort(Protocol):
    """Trusted one-way spool and protected-policy reader."""

    def read_finalized_envelope(
        self, reference: FinalizedEnvelopeReference
    ) -> FinalizedVerifierEnvelope: ...

    def read_protected_policy(
        self, reference: ProtectedPolicyReference
    ) -> ProtectedPublisherPolicy: ...


@dataclass(frozen=True)
class RuntimeAttestation:
    uid: int
    home: str
    environment_keys: tuple[str, ...]
    dedicated_uid: bool
    home_model_credentials_absent: bool
    environment_model_credentials_absent: bool
    model_credential_keys: tuple[str, ...]
    attested_by: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.uid, bool) or not isinstance(self.uid, int) or self.uid < 1:
            raise PublicationContractError("runtime UID is invalid")
        _validate_home(self.home)
        _validate_environment_keys(self.environment_keys)
        for name in (
            "dedicated_uid",
            "home_model_credentials_absent",
            "environment_model_credentials_absent",
        ):
            if not isinstance(getattr(self, name), bool):
                raise PublicationContractError(f"runtime {name} is invalid")
        if not isinstance(self.model_credential_keys, tuple) or any(
            not isinstance(item, str) or not item for item in self.model_credential_keys
        ):
            raise PublicationContractError("runtime model credential keys are invalid")
        _safe_one_line(self.attested_by, "runtime attestor", maximum=120)
        _require_sha256(self.evidence_sha256, "runtime attestation digest")
        if self.evidence_sha256 != _runtime_attestation_digest(
            uid=self.uid,
            home=self.home,
            environment_keys=self.environment_keys,
            dedicated_uid=self.dedicated_uid,
            home_model_credentials_absent=self.home_model_credentials_absent,
            environment_model_credentials_absent=(
                self.environment_model_credentials_absent
            ),
            model_credential_keys=self.model_credential_keys,
            attested_by=self.attested_by,
        ):
            raise PublicationContractError("runtime attestation digest does not match")

    @classmethod
    def attest(
        cls,
        *,
        uid: int,
        home: str,
        environment_keys: tuple[str, ...],
        dedicated_uid: bool,
        home_model_credentials_absent: bool,
        environment_model_credentials_absent: bool,
        model_credential_keys: tuple[str, ...],
        attested_by: str,
    ) -> RuntimeAttestation:
        digest = _runtime_attestation_digest(
            uid=uid,
            home=home,
            environment_keys=environment_keys,
            dedicated_uid=dedicated_uid,
            home_model_credentials_absent=home_model_credentials_absent,
            environment_model_credentials_absent=environment_model_credentials_absent,
            model_credential_keys=model_credential_keys,
            attested_by=attested_by,
        )
        return cls(
            uid=uid,
            home=home,
            environment_keys=environment_keys,
            dedicated_uid=dedicated_uid,
            home_model_credentials_absent=home_model_credentials_absent,
            environment_model_credentials_absent=(environment_model_credentials_absent),
            model_credential_keys=model_credential_keys,
            attested_by=attested_by,
            evidence_sha256=digest,
        )


class RuntimeAttestationPort(Protocol):
    def inspect_runtime(self) -> RuntimeAttestation: ...


@dataclass(frozen=True)
class PrincipalSnapshot:
    mode: PrincipalMode
    actor: str
    app_id: int | None
    installation_id: int | None
    repository: str
    permissions: tuple[str, ...]
    repository_scoped: bool
    is_admin: bool
    can_bypass_branch_protection: bool
    committer: GitIdentity


@dataclass(frozen=True)
class RepositorySnapshot:
    repository: str
    default_branch: str
    main_sha: str
    public: bool
    is_fork: bool


@dataclass(frozen=True)
class PatchSnapshot:
    base_sha: str
    patch_sha256: str
    head_sha256: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class LocalBranchSnapshot:
    branch: str
    head_sha: str
    checked_out: bool


@dataclass(frozen=True)
class CommitSpec:
    paths: tuple[str, ...]
    message: str
    author: GitIdentity
    committer: GitIdentity
    amend: bool = False

    def __post_init__(self) -> None:
        if self.amend:
            raise PublicationContractError("publisher never amends commits")
        if not isinstance(self.paths, tuple) or not self.paths:
            raise PublicationContractError("commit paths must be non-empty")
        if not isinstance(self.message, str) or not self.message.endswith(
            f"\n{AUTOMATION_TRAILER}\n"
        ):
            raise PublicationContractError("commit message lacks automation trailer")
        if self.message.count(AUTOMATION_TRAILER) != 1:
            raise PublicationContractError(
                "automation trailer must appear exactly once"
            )
        if self.author != IVES_AUTHOR:
            raise PublicationContractError(
                "commit author must be the approved Ives identity"
            )


@dataclass(frozen=True)
class CommitRecord:
    head_sha: str
    parent_sha: str
    patch_sha256: str
    head_sha256: str
    message: str
    author: GitIdentity
    committer: GitIdentity


@dataclass(frozen=True)
class PushSpec:
    repository: str
    branch: str
    head_sha: str
    expected_main_sha: str
    force: bool = False

    def __post_init__(self) -> None:
        if self.repository != CANONICAL_REPOSITORY:
            raise PublicationContractError("push repository is not canonical")
        _validate_branch(self.branch)
        _require_git_oid(self.head_sha, "push head SHA")
        _require_git_oid(self.expected_main_sha, "push expected main SHA")
        if self.force:
            raise PublicationContractError("publisher never force-pushes")


@dataclass(frozen=True)
class DraftPullRequestSpec:
    repository: str
    base_branch: str
    head_branch: str
    title: str
    body: str
    draft: bool = True

    def __post_init__(self) -> None:
        if self.repository != CANONICAL_REPOSITORY:
            raise PublicationContractError("PR repository is not canonical")
        if self.base_branch != DEFAULT_BRANCH:
            raise PublicationContractError("PR base must be main")
        _validate_branch(self.head_branch)
        _safe_one_line(self.title, "PR title", maximum=240)
        if not isinstance(self.body, str) or not self.body or len(self.body) > 20_000:
            raise PublicationContractError("PR body is invalid")
        if _BODY_CONTROL.search(self.body):
            raise PublicationContractError("PR body contains unsafe control characters")
        _reject_secret_material((self.title, self.body), "pull request")
        if not self.draft:
            raise PublicationContractError("publisher may only open draft PRs")

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PullRequestSnapshot:
    number: int
    url: str
    repository: str
    base_branch: str
    head_branch: str
    head_sha: str
    actor: str
    title: str
    body_sha256: str
    state: str
    draft: bool


@dataclass(frozen=True)
class PublicationResult:
    repository: str
    base_sha: str
    head_sha: str
    branch: str
    patch_sha256: str
    verification_sha256: str
    commit_author: GitIdentity
    commit_committer: GitIdentity
    pr_actor: str
    pr_number: int
    pr_url: str
    draft: bool
    reconciled: bool


class GitPort(Protocol):
    def inspect_patch(self, worktree: Path, base_sha: str) -> PatchSnapshot: ...

    def read_local_branch(
        self, worktree: Path, branch: str
    ) -> LocalBranchSnapshot | None: ...

    def create_branch(self, worktree: Path, branch: str, start_sha: str) -> None: ...

    def commit(self, worktree: Path, spec: CommitSpec) -> None: ...

    def inspect_commit(self, worktree: Path, head_sha: str) -> CommitRecord: ...

    def push_new_branch(self, worktree: Path, spec: PushSpec) -> None: ...


class GitHubPort(Protocol):
    def inspect_principal(
        self, repository: str, mode: PrincipalMode
    ) -> PrincipalSnapshot: ...

    def inspect_repository(self, repository: str) -> RepositorySnapshot: ...

    def read_branch_sha(self, repository: str, branch: str) -> str | None: ...

    def read_commit(self, repository: str, head_sha: str) -> CommitRecord: ...

    def list_pull_requests(
        self, repository: str, head_branch: str
    ) -> tuple[PullRequestSnapshot, ...]: ...

    def open_draft_pull_request(self, spec: DraftPullRequestSpec) -> None: ...


@dataclass(frozen=True)
class _CheckState:
    command_id: str
    output_sha256: str
    exit_code: int
    timed_out: bool


@dataclass(frozen=True)
class _VerifierState:
    canonical_sha256: str
    run_id: str
    run_date: str
    repository: str
    base_sha: str
    candidate_id: str
    candidate_slug: str
    candidate_title: str
    risk_tier: int
    acceptance: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    validation_profile_ids: tuple[str, ...]
    expected_external_side_effects: str
    source_kind: str
    source_manifest_revision: int
    source_approved_by: str
    guard_patch_sha256: str
    final_guard_patch_sha256: str
    final_guard_sha256: str
    head_sha256: str
    isolation_verified_by: str
    isolation_evidence_sha256: str
    checks: tuple[_CheckState, ...]

    @property
    def branch(self) -> str:
        return v1_daily_branch_name(
            self.run_date,
            self.candidate_slug,
            self.base_sha,
        )


@dataclass(frozen=True)
class _PolicyState:
    canonical_sha256: str
    policy_id: str
    repository: str
    principal_mode: PrincipalMode
    expected_actor: str
    app_id: int | None
    installation_id: int | None
    permissions: tuple[str, ...]
    unattended: bool
    committer: GitIdentity
    publisher_uid: int
    publisher_home: str
    environment_keys: tuple[str, ...]
    runtime_attestor: str


@dataclass(frozen=True)
class _PrincipalState:
    mode: PrincipalMode
    actor: str
    app_id: int | None
    installation_id: int | None
    repository: str
    permissions: tuple[str, ...]
    repository_scoped: bool
    is_admin: bool
    can_bypass_branch_protection: bool
    committer: GitIdentity


_T = TypeVar("_T")


class Publisher:
    """Publish one finalized verifier envelope through read-back-only ports.

    Port implementations and the spool/policy adapters are inside the
    publisher trusted computing base. This class distrusts returned mutation
    acknowledgements and requires independent read-back, but a malicious port
    implementation can still lie about both the mutation and its read-back.
    """

    def __init__(
        self,
        *,
        git: GitPort,
        github: GitHubPort,
        trust: TrustMaterialPort,
        runtime: RuntimeAttestationPort,
    ) -> None:
        self._git = git
        self._github = github
        self._trust = trust
        self._runtime = runtime

    def publish(
        self,
        worktree: Path,
        envelope_reference: FinalizedEnvelopeReference,
        policy_reference: ProtectedPolicyReference,
    ) -> PublicationResult:
        if not isinstance(envelope_reference, FinalizedEnvelopeReference):
            raise PublicationContractError("finalized envelope reference is required")
        if not isinstance(policy_reference, ProtectedPolicyReference):
            raise PublicationContractError("protected policy reference is required")
        repo = self._validated_worktree(worktree)

        evidence = self._read_envelope(envelope_reference)
        policy = self._read_policy(policy_reference)
        runtime = self._read_runtime(policy)
        principal = self._read_principal(policy)
        repository = self._read_repository(evidence)
        self._validate_repository(repository, evidence)

        commit_spec = CommitSpec(
            paths=evidence.changed_paths,
            message=_commit_message(evidence),
            author=IVES_AUTHOR,
            committer=policy.committer,
            amend=False,
        )
        pr_spec = DraftPullRequestSpec(
            repository=CANONICAL_REPOSITORY,
            base_branch=DEFAULT_BRANCH,
            head_branch=evidence.branch,
            title=_pr_title(evidence),
            body=_pr_body(evidence),
            draft=True,
        )

        local = self._read_local_branch(repo, evidence.branch)
        remote_head = self._read_remote_branch(evidence.branch)
        pull_requests = self._read_pull_requests(evidence.branch)

        if pull_requests:
            if remote_head is None or len(pull_requests) != 1:
                raise PublicationConflictError(
                    "existing pull request cannot be reconciled without one branch"
                )
            remote_commit = self._read_remote_commit(remote_head)
            self._validate_commit(remote_commit, commit_spec, evidence)
            self._validate_pull_request(
                pull_requests[0], pr_spec, principal, remote_head
            )
            if local is not None and local.head_sha not in {
                evidence.base_sha,
                remote_head,
            }:
                raise PublicationConflictError(
                    "local branch disagrees with reconciled remote branch"
                )
            self._final_read_barrier(
                envelope_reference,
                evidence,
                policy_reference,
                policy,
                runtime,
                principal,
            )
            self._require_remote_branch(evidence.branch, remote_head)
            final_remote_commit = self._read_remote_commit(remote_head)
            self._validate_commit(final_remote_commit, commit_spec, evidence)
            if final_remote_commit != remote_commit:
                raise PublicationPreflightError(
                    "reconciled remote commit read-back changed"
                )
            reread = self._read_pull_requests(evidence.branch)
            if len(reread) != 1:
                raise PublicationPreflightError("reconciled PR read-back changed")
            self._validate_pull_request(reread[0], pr_spec, principal, remote_head)
            if reread[0] != pull_requests[0]:
                raise PublicationPreflightError("reconciled PR read-back changed")
            return self._result(
                evidence,
                final_remote_commit,
                reread[0],
                reconciled=True,
            )

        commit: CommitRecord | None = None
        reconciled = remote_head is not None
        if remote_head is not None:
            commit = self._read_remote_commit(remote_head)
            self._validate_commit(commit, commit_spec, evidence)
            if local is not None and local.head_sha not in {
                evidence.base_sha,
                remote_head,
            }:
                raise PublicationConflictError(
                    "local branch disagrees with existing remote branch"
                )
        elif local is not None and local.head_sha != evidence.base_sha:
            commit = self._read_local_commit(repo, local.head_sha)
            self._validate_commit(commit, commit_spec, evidence)

        if local is None and commit is None:
            self._before_local_patch_write(
                repo,
                envelope_reference,
                evidence,
                policy_reference,
                policy,
                runtime,
                principal,
            )
            self._port_call(
                "local branch creation",
                lambda: self._git.create_branch(
                    repo, evidence.branch, evidence.base_sha
                ),
            )
            local = self._read_local_branch(repo, evidence.branch)
            if (
                local is None
                or not local.checked_out
                or local.head_sha != evidence.base_sha
            ):
                raise PublicationPreflightError(
                    "local branch creation read-back failed"
                )
        elif local is not None and not local.checked_out and remote_head is None:
            raise PublicationConflictError(
                "existing local publication branch is not checked out"
            )

        if commit is None:
            self._before_local_patch_write(
                repo,
                envelope_reference,
                evidence,
                policy_reference,
                policy,
                runtime,
                principal,
            )
            self._port_call(
                "commit creation", lambda: self._git.commit(repo, commit_spec)
            )
            local = self._read_local_branch(repo, evidence.branch)
            if (
                local is None
                or not local.checked_out
                or local.head_sha == evidence.base_sha
            ):
                raise PublicationPreflightError("commit creation read-back failed")
            commit = self._read_local_commit(repo, local.head_sha)
            self._validate_commit(commit, commit_spec, evidence)

        if remote_head is None:
            self._before_push_write(
                repo,
                commit,
                commit_spec,
                envelope_reference,
                evidence,
                policy_reference,
                policy,
                runtime,
                principal,
            )
            push_spec = PushSpec(
                repository=CANONICAL_REPOSITORY,
                branch=evidence.branch,
                head_sha=commit.head_sha,
                expected_main_sha=evidence.base_sha,
                force=False,
            )
            self._port_call(
                "new branch push",
                lambda: self._git.push_new_branch(repo, push_spec),
            )
            self._require_remote_branch(evidence.branch, commit.head_sha)
            remote_commit = self._read_remote_commit(commit.head_sha)
            self._validate_commit(remote_commit, commit_spec, evidence)
            remote_head = commit.head_sha
        elif remote_head != commit.head_sha:
            raise PublicationConflictError(
                "existing remote branch does not match verified commit"
            )

        self._before_pr_write(
            remote_head,
            commit_spec,
            envelope_reference,
            evidence,
            policy_reference,
            policy,
            runtime,
            principal,
        )
        self._port_call(
            "draft pull request creation",
            lambda: self._github.open_draft_pull_request(pr_spec),
        )
        self._require_remote_branch(evidence.branch, remote_head)
        remote_commit = self._read_remote_commit(remote_head)
        self._validate_commit(remote_commit, commit_spec, evidence)
        pull_requests = self._read_pull_requests(evidence.branch)
        if len(pull_requests) != 1:
            raise PublicationPreflightError("draft PR creation read-back is not unique")
        created_pull_request = pull_requests[0]
        self._validate_pull_request(
            created_pull_request, pr_spec, principal, remote_head
        )
        self._final_read_barrier(
            envelope_reference,
            evidence,
            policy_reference,
            policy,
            runtime,
            principal,
        )
        self._require_remote_branch(evidence.branch, remote_head)
        final_remote_commit = self._read_remote_commit(remote_head)
        self._validate_commit(final_remote_commit, commit_spec, evidence)
        if final_remote_commit != remote_commit:
            raise PublicationPreflightError("remote commit changed after PR creation")
        final_pull_requests = self._read_pull_requests(evidence.branch)
        if len(final_pull_requests) != 1:
            raise PublicationPreflightError("final draft PR read-back is not unique")
        final_pull_request = final_pull_requests[0]
        self._validate_pull_request(final_pull_request, pr_spec, principal, remote_head)
        if final_pull_request != created_pull_request:
            raise PublicationPreflightError("draft PR changed after final barrier")
        return self._result(
            evidence,
            final_remote_commit,
            final_pull_request,
            reconciled=reconciled,
        )

    @staticmethod
    def _validated_worktree(worktree: Path) -> Path:
        if not isinstance(worktree, Path) or not worktree.is_absolute():
            raise PublicationContractError(
                "publisher worktree must be an absolute Path"
            )
        try:
            resolved = worktree.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PublicationContractError("publisher worktree does not exist") from exc
        if not resolved.is_dir():
            raise PublicationContractError("publisher worktree must be a directory")
        return resolved

    def _before_local_patch_write(
        self,
        repo: Path,
        envelope_reference: FinalizedEnvelopeReference,
        evidence: _VerifierState,
        policy_reference: ProtectedPolicyReference,
        policy: _PolicyState,
        runtime: RuntimeAttestation,
        principal: _PrincipalState,
    ) -> None:
        self._revalidate_context(policy_reference, policy, runtime, principal, evidence)
        patch = self._read_patch(repo, evidence.base_sha)
        self._validate_patch(patch, evidence)
        self._revalidate_envelope(envelope_reference, evidence)

    def _before_push_write(
        self,
        repo: Path,
        commit: CommitRecord,
        commit_spec: CommitSpec,
        envelope_reference: FinalizedEnvelopeReference,
        evidence: _VerifierState,
        policy_reference: ProtectedPolicyReference,
        policy: _PolicyState,
        runtime: RuntimeAttestation,
        principal: _PrincipalState,
    ) -> None:
        self._revalidate_context(policy_reference, policy, runtime, principal, evidence)
        reread = self._read_local_commit(repo, commit.head_sha)
        self._validate_commit(reread, commit_spec, evidence)
        if reread != commit:
            raise PublicationPreflightError("local commit changed before push")
        self._revalidate_envelope(envelope_reference, evidence)

    def _before_pr_write(
        self,
        remote_head: str,
        commit_spec: CommitSpec,
        envelope_reference: FinalizedEnvelopeReference,
        evidence: _VerifierState,
        policy_reference: ProtectedPolicyReference,
        policy: _PolicyState,
        runtime: RuntimeAttestation,
        principal: _PrincipalState,
    ) -> None:
        self._revalidate_context(policy_reference, policy, runtime, principal, evidence)
        self._require_remote_branch(evidence.branch, remote_head)
        remote_commit = self._read_remote_commit(remote_head)
        self._validate_commit(remote_commit, commit_spec, evidence)
        self._revalidate_envelope(envelope_reference, evidence)

    def _final_read_barrier(
        self,
        envelope_reference: FinalizedEnvelopeReference,
        evidence: _VerifierState,
        policy_reference: ProtectedPolicyReference,
        policy: _PolicyState,
        runtime: RuntimeAttestation,
        principal: _PrincipalState,
    ) -> None:
        self._revalidate_context(policy_reference, policy, runtime, principal, evidence)
        self._revalidate_envelope(envelope_reference, evidence)

    def _revalidate_context(
        self,
        policy_reference: ProtectedPolicyReference,
        policy: _PolicyState,
        runtime: RuntimeAttestation,
        principal: _PrincipalState,
        evidence: _VerifierState,
    ) -> None:
        if self._read_policy(policy_reference) != policy:
            raise PublicationPreflightError("protected publisher policy changed")
        if self._read_runtime(policy) != runtime:
            raise PublicationPreflightError("publisher runtime attestation changed")
        if self._read_principal(policy) != principal:
            raise PublicationPreflightError("publisher principal changed")
        self._require_remote_main(evidence.base_sha)

    def _revalidate_envelope(
        self,
        reference: FinalizedEnvelopeReference,
        expected: _VerifierState,
    ) -> None:
        if self._read_envelope(reference) != expected:
            raise PublicationPreflightError("finalized verifier envelope changed")

    def _read_envelope(self, reference: FinalizedEnvelopeReference) -> _VerifierState:
        view = self._port_call(
            "finalized verifier envelope read",
            lambda: self._trust.read_finalized_envelope(reference),
        )
        return _freeze_verifier_envelope(view, reference)

    def _read_policy(self, reference: ProtectedPolicyReference) -> _PolicyState:
        view = self._port_call(
            "protected publisher policy read",
            lambda: self._trust.read_protected_policy(reference),
        )
        return _freeze_policy(view, reference)

    def _read_runtime(self, policy: _PolicyState) -> RuntimeAttestation:
        value = self._port_call(
            "publisher runtime attestation", self._runtime.inspect_runtime
        )
        snapshot = _copy_runtime_attestation(value)
        _validate_runtime(snapshot, policy)
        return snapshot

    def _read_principal(self, policy: _PolicyState) -> _PrincipalState:
        value = self._port_call(
            "publisher principal read",
            lambda: self._github.inspect_principal(
                CANONICAL_REPOSITORY, policy.principal_mode
            ),
        )
        snapshot = _freeze_principal(value)
        _validate_principal(snapshot, policy)
        return snapshot

    def _read_repository(self, evidence: _VerifierState) -> RepositorySnapshot:
        value = self._port_call(
            "repository preflight",
            lambda: self._github.inspect_repository(CANONICAL_REPOSITORY),
        )
        snapshot = _copy_repository(value)
        self._validate_repository(snapshot, evidence)
        return snapshot

    @staticmethod
    def _validate_repository(
        repository: RepositorySnapshot, evidence: _VerifierState
    ) -> None:
        if repository.repository != CANONICAL_REPOSITORY:
            raise PublicationPreflightError("remote repository identity does not match")
        if repository.default_branch != DEFAULT_BRANCH:
            raise PublicationPreflightError("remote default branch is not main")
        if not repository.public or repository.is_fork:
            raise PublicationPreflightError(
                "canonical repository must remain public and non-fork"
            )
        if repository.main_sha != evidence.base_sha:
            raise PublicationPreflightError("verified base is not current remote main")

    def _require_remote_main(self, expected: str) -> None:
        value = self._port_call(
            "remote main read",
            lambda: self._github.read_branch_sha(CANONICAL_REPOSITORY, DEFAULT_BRANCH),
        )
        if value != expected:
            raise PublicationPreflightError("remote main moved")

    def _read_patch(self, repo: Path, base_sha: str) -> PatchSnapshot:
        value = self._port_call(
            "local patch read", lambda: self._git.inspect_patch(repo, base_sha)
        )
        return _copy_patch(value)

    @staticmethod
    def _validate_patch(patch: PatchSnapshot, evidence: _VerifierState) -> None:
        if patch.base_sha != evidence.base_sha:
            raise PublicationPreflightError("local patch base changed")
        if patch.patch_sha256 != evidence.final_guard_patch_sha256:
            raise PublicationPreflightError("local patch digest changed")
        if patch.head_sha256 != evidence.head_sha256:
            raise PublicationPreflightError("local head digest changed")
        if patch.changed_paths != evidence.changed_paths:
            raise PublicationPreflightError("local changed paths changed")

    def _read_local_branch(self, repo: Path, branch: str) -> LocalBranchSnapshot | None:
        value = self._port_call(
            "local branch read", lambda: self._git.read_local_branch(repo, branch)
        )
        return None if value is None else _copy_local_branch(value, branch)

    def _read_remote_branch(self, branch: str) -> str | None:
        value = self._port_call(
            "remote branch read",
            lambda: self._github.read_branch_sha(CANONICAL_REPOSITORY, branch),
        )
        if value is not None:
            _require_git_oid(value, "remote branch SHA")
        return value

    def _require_remote_branch(self, branch: str, expected: str) -> None:
        if self._read_remote_branch(branch) != expected:
            raise PublicationPreflightError("remote branch read-back does not match")

    def _read_local_commit(self, repo: Path, head_sha: str) -> CommitRecord:
        value = self._port_call(
            "local commit read",
            lambda: self._git.inspect_commit(repo, head_sha),
        )
        commit = _copy_commit(value)
        if commit.head_sha != head_sha:
            raise PublicationPreflightError("local commit read returned another commit")
        return commit

    def _read_remote_commit(self, head_sha: str) -> CommitRecord:
        value = self._port_call(
            "remote commit read",
            lambda: self._github.read_commit(CANONICAL_REPOSITORY, head_sha),
        )
        commit = _copy_commit(value)
        if commit.head_sha != head_sha:
            raise PublicationPreflightError(
                "remote commit read returned another commit"
            )
        return commit

    @staticmethod
    def _validate_commit(
        commit: CommitRecord, spec: CommitSpec, evidence: _VerifierState
    ) -> None:
        if commit.parent_sha != evidence.base_sha:
            raise PublicationPreflightError("commit parent is not verified base")
        if commit.patch_sha256 != evidence.final_guard_patch_sha256:
            raise PublicationPreflightError("commit patch digest does not match")
        if commit.head_sha256 != evidence.head_sha256:
            raise PublicationPreflightError("commit head digest does not match")
        if commit.message != spec.message:
            raise PublicationPreflightError("commit message does not match")
        if commit.author != IVES_AUTHOR or commit.author != spec.author:
            raise PublicationPreflightError("commit author does not match")
        if commit.committer != spec.committer:
            raise PublicationPreflightError("commit committer does not match")

    def _read_pull_requests(self, branch: str) -> tuple[PullRequestSnapshot, ...]:
        values = self._port_call(
            "pull request read",
            lambda: self._github.list_pull_requests(CANONICAL_REPOSITORY, branch),
        )
        if not isinstance(values, tuple):
            raise PublicationPreflightError("pull request read returned invalid data")
        return tuple(_copy_pull_request(value) for value in values)

    @staticmethod
    def _validate_pull_request(
        pull_request: PullRequestSnapshot,
        spec: DraftPullRequestSpec,
        principal: _PrincipalState,
        head_sha: str,
    ) -> None:
        if pull_request.repository != spec.repository:
            raise PublicationPreflightError("PR repository does not match")
        if (
            pull_request.base_branch != spec.base_branch
            or pull_request.head_branch != spec.head_branch
            or pull_request.head_sha != head_sha
        ):
            raise PublicationPreflightError("PR branch or head does not match")
        if pull_request.actor != principal.actor:
            raise PublicationPreflightError("PR actor does not match")
        if pull_request.title != spec.title:
            raise PublicationPreflightError("PR title does not match")
        if pull_request.body_sha256 != spec.body_sha256:
            raise PublicationPreflightError("PR body digest does not match")
        if pull_request.state != "open" or not pull_request.draft:
            raise PublicationPreflightError("PR must remain open and draft")
        if (
            isinstance(pull_request.number, bool)
            or not isinstance(pull_request.number, int)
            or pull_request.number < 1
        ):
            raise PublicationPreflightError("PR number is invalid")
        expected_url = (
            f"https://github.com/{CANONICAL_REPOSITORY}/pull/{pull_request.number}"
        )
        if pull_request.url != expected_url:
            raise PublicationPreflightError("PR URL does not match")

    @staticmethod
    def _result(
        evidence: _VerifierState,
        commit: CommitRecord,
        pull_request: PullRequestSnapshot,
        *,
        reconciled: bool,
    ) -> PublicationResult:
        return PublicationResult(
            repository=CANONICAL_REPOSITORY,
            base_sha=evidence.base_sha,
            head_sha=commit.head_sha,
            branch=evidence.branch,
            patch_sha256=evidence.final_guard_patch_sha256,
            verification_sha256=evidence.canonical_sha256,
            commit_author=commit.author,
            commit_committer=commit.committer,
            pr_actor=pull_request.actor,
            pr_number=pull_request.number,
            pr_url=pull_request.url,
            draft=pull_request.draft,
            reconciled=reconciled,
        )

    @staticmethod
    def _port_call(stage: str, call: Callable[[], _T]) -> _T:
        try:
            return call()
        except PublicationError:
            raise
        except Exception:
            raise PublicationOperationError(f"publisher port failed: {stage}") from None


def _freeze_verifier_envelope(
    value: FinalizedVerifierEnvelope,
    reference: FinalizedEnvelopeReference,
) -> _VerifierState:
    canonical = _canonical_bytes(value, "finalized verifier envelope")
    digest = hashlib.sha256(canonical).hexdigest()
    if digest != reference.envelope_sha256:
        raise PublicationPreflightError(
            "finalized verifier digest does not match spool"
        )
    if value.schema_version != FINALIZED_ENVELOPE_SCHEMA or value.finalized is not True:
        raise PublicationPreflightError("verifier envelope is not finalized v1")

    run_date = _canonical_date(value.run_date)
    repository = _copy_string(value.repository, "verifier repository")
    if repository != CANONICAL_REPOSITORY:
        raise PublicationPreflightError("verifier repository is not canonical")
    base_sha = _copy_git_oid(value.base_sha, "verifier base SHA")
    expected_run_id = f"{run_date}/{repository}@{base_sha}"
    run_id = _copy_string(value.run_id, "verifier run ID")
    if run_id != expected_run_id:
        raise PublicationPreflightError("verifier run ID is not date/repo@base")

    candidate_id = _copy_string(value.candidate_id, "candidate ID")
    if not _CANDIDATE_ID.fullmatch(candidate_id):
        raise PublicationPreflightError("candidate ID is invalid")
    slug = _copy_string(value.candidate_slug, "candidate slug")
    if not is_v1_candidate_slug(slug):
        raise PublicationPreflightError("candidate slug is invalid")
    title = _copy_safe_line(value.candidate_title, "candidate title", maximum=160)
    risk_tier = _copy_int(value.risk_tier, "candidate risk tier", minimum=0)
    if risk_tier not in {0, 1}:
        raise PublicationPreflightError("publisher accepts only V1 Tier 0 or Tier 1")
    acceptance = _copy_lines(value.acceptance, "acceptance", maximum=500)
    allowed_paths = _copy_paths(value.allowed_paths, "allowed paths", patterns=True)
    changed_paths = _copy_paths(value.changed_paths, "changed paths", patterns=False)
    profiles = _copy_identifiers(
        value.validation_profile_ids, _PROFILE_ID, "validation profiles"
    )
    if value.expected_external_side_effects != "none":
        raise PublicationPreflightError("candidate external side effects are not none")
    source_kind = _copy_safe_line(value.source_kind, "source kind", maximum=64)
    if source_kind != "curated_backlog":
        raise PublicationPreflightError("candidate source is not curated backlog")
    source_revision = _copy_int(
        value.source_manifest_revision,
        "source manifest revision",
        minimum=1,
    )
    source_approver = _copy_safe_line(
        value.source_approved_by, "source approver", maximum=100
    )
    _validate_v1_candidate_authority(
        risk_tier=risk_tier,
        allowed_paths=allowed_paths,
        changed_paths=changed_paths,
        profiles=profiles,
        source_approver=source_approver,
    )

    for name in (
        "guard_ok",
        "final_guard_ok",
        "mutation_detected",
        "isolation_network_isolated",
        "isolation_credentials_absent",
    ):
        if not isinstance(getattr(value, name), bool):
            raise PublicationPreflightError(f"verifier {name} is not boolean")
    if not value.guard_ok or not value.final_guard_ok or value.mutation_detected:
        raise PublicationPreflightError("verifier guard finalization failed")
    if not value.isolation_network_isolated or not value.isolation_credentials_absent:
        raise PublicationPreflightError("verifier isolation evidence failed")

    guard_patch = _copy_sha256(value.guard_patch_sha256, "guard patch digest")
    final_guard_patch = _copy_sha256(
        value.final_guard_patch_sha256, "final guard patch digest"
    )
    final_guard = _copy_sha256(value.final_guard_sha256, "final guard digest")
    if guard_patch != final_guard_patch:
        raise PublicationPreflightError(
            "initial and final guard patch identities differ"
        )
    head_sha256 = _copy_sha256(value.head_sha256, "verified head digest")
    isolation_verified_by = _copy_safe_line(
        value.isolation_verified_by, "isolation verifier", maximum=120
    )
    isolation_evidence = _copy_sha256(
        value.isolation_evidence_sha256, "isolation evidence digest"
    )

    if not isinstance(value.checks, tuple) or not value.checks:
        raise PublicationPreflightError("verifier checks must be a non-empty tuple")
    checks: list[_CheckState] = []
    for item in value.checks:
        command_id = _copy_string(item.command_id, "verification command ID")
        if not _COMMAND_ID.fullmatch(command_id):
            raise PublicationPreflightError("verification command ID is invalid")
        output_sha256 = _copy_sha256(item.output_sha256, "verification output digest")
        exit_code = _copy_int(
            item.exit_code, "verification exit code", minimum=-(2**31)
        )
        if not isinstance(item.timed_out, bool):
            raise PublicationPreflightError("verification timeout flag is invalid")
        if exit_code != 0 or item.timed_out:
            raise PublicationPreflightError("verification did not pass")
        checks.append(_CheckState(command_id, output_sha256, exit_code, item.timed_out))
    if tuple(item.command_id for item in checks) != (
        "git-diff-check",
        *profiles,
    ):
        raise PublicationPreflightError(
            "verification checks do not match exact candidate profiles"
        )
    _reject_secret_material(
        (
            run_id,
            candidate_id,
            slug,
            title,
            *acceptance,
            *allowed_paths,
            *changed_paths,
            *profiles,
            source_kind,
            source_approver,
            isolation_verified_by,
        ),
        "finalized verifier envelope",
    )
    state = _VerifierState(
        canonical_sha256=digest,
        run_id=run_id,
        run_date=run_date,
        repository=repository,
        base_sha=base_sha,
        candidate_id=candidate_id,
        candidate_slug=slug,
        candidate_title=title,
        risk_tier=risk_tier,
        acceptance=acceptance,
        allowed_paths=allowed_paths,
        changed_paths=changed_paths,
        validation_profile_ids=profiles,
        expected_external_side_effects="none",
        source_kind=source_kind,
        source_manifest_revision=source_revision,
        source_approved_by=source_approver,
        guard_patch_sha256=guard_patch,
        final_guard_patch_sha256=final_guard_patch,
        final_guard_sha256=final_guard,
        head_sha256=head_sha256,
        isolation_verified_by=isolation_verified_by,
        isolation_evidence_sha256=isolation_evidence,
        checks=tuple(checks),
    )
    _require_verifier_canonical_bytes(canonical, state)
    return state


def _freeze_policy(
    value: ProtectedPublisherPolicy,
    reference: ProtectedPolicyReference,
) -> _PolicyState:
    canonical = _canonical_bytes(value, "protected publisher policy")
    digest = hashlib.sha256(canonical).hexdigest()
    if digest != reference.policy_sha256:
        raise PublicationPreflightError("protected policy digest does not match")
    if value.schema_version != PROTECTED_POLICY_SCHEMA:
        raise PublicationPreflightError("protected publisher policy is not v1")
    policy_id = _copy_string(value.policy_id, "protected policy ID")
    if policy_id != reference.policy_id:
        raise PublicationPreflightError("protected policy ID does not match")
    if value.repository != CANONICAL_REPOSITORY:
        raise PublicationPreflightError("protected policy repository is not canonical")
    try:
        mode = PrincipalMode(value.principal_mode)
    except (TypeError, ValueError) as exc:
        raise PublicationPreflightError("protected principal mode is invalid") from exc
    actor = _copy_string(value.expected_actor, "protected expected actor")
    if not _ACTOR.fullmatch(actor):
        raise PublicationPreflightError("protected expected actor is invalid")
    permissions = _copy_identifiers(value.permissions, _PERMISSION, "permissions")
    if permissions != _EXACT_APP_PERMISSIONS:
        raise PublicationPreflightError("publisher permissions are not exact allowlist")
    if not isinstance(value.unattended, bool):
        raise PublicationPreflightError("protected unattended flag is invalid")

    app_id = _copy_optional_positive_int(value.app_id, "GitHub App ID")
    installation_id = _copy_optional_positive_int(
        value.installation_id, "GitHub App installation ID"
    )
    if mode is PrincipalMode.GITHUB_APP:
        if app_id is None or installation_id is None:
            raise PublicationPreflightError("GitHub App identity is not fully pinned")
    else:
        if actor != "IvesLiu1026" or app_id is not None or installation_id is not None:
            raise PublicationPreflightError("CLI bootstrap identity is not exact")
        if value.unattended:
            raise PublicationPreflightError("CLI bootstrap cannot be unattended")
    if value.unattended and mode is not PrincipalMode.GITHUB_APP:
        raise PublicationPreflightError("unattended principal must be GitHub App")

    committer = GitIdentity(
        _copy_safe_line(value.committer_name, "committer name", maximum=100),
        _copy_string(value.committer_email, "committer email"),
    )
    uid = _copy_int(value.publisher_uid, "publisher UID", minimum=1)
    home = _copy_string(value.publisher_home, "publisher home")
    _validate_home(home)
    if home == "/home/yhliu":
        raise PublicationPreflightError("publisher home is not dedicated")
    environment_keys = _copy_environment_keys(value.environment_keys)
    if environment_keys != PUBLISHER_ENVIRONMENT_ALLOWLIST:
        raise PublicationPreflightError(
            "publisher environment does not match exact allowlist"
        )
    runtime_attestor = _copy_safe_line(
        value.runtime_attestor, "runtime attestor", maximum=120
    )
    state = _PolicyState(
        canonical_sha256=digest,
        policy_id=policy_id,
        repository=CANONICAL_REPOSITORY,
        principal_mode=mode,
        expected_actor=actor,
        app_id=app_id,
        installation_id=installation_id,
        permissions=permissions,
        unattended=value.unattended,
        committer=committer,
        publisher_uid=uid,
        publisher_home=home,
        environment_keys=environment_keys,
        runtime_attestor=runtime_attestor,
    )
    _require_policy_canonical_bytes(canonical, state, unattended=value.unattended)
    return state


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PublicationPreflightError(
            "trusted material is not canonical JSON"
        ) from exc


def _require_verifier_canonical_bytes(canonical: bytes, state: _VerifierState) -> None:
    payload = {
        "schema_version": FINALIZED_ENVELOPE_SCHEMA,
        "finalized": True,
        "run_id": state.run_id,
        "run_date": state.run_date,
        "repository": state.repository,
        "base_sha": state.base_sha,
        "candidate_id": state.candidate_id,
        "candidate_slug": state.candidate_slug,
        "candidate_title": state.candidate_title,
        "risk_tier": state.risk_tier,
        "acceptance": list(state.acceptance),
        "allowed_paths": list(state.allowed_paths),
        "changed_paths": list(state.changed_paths),
        "validation_profile_ids": list(state.validation_profile_ids),
        "expected_external_side_effects": state.expected_external_side_effects,
        "source_kind": state.source_kind,
        "source_manifest_revision": state.source_manifest_revision,
        "source_approved_by": state.source_approved_by,
        "guard_ok": True,
        "guard_patch_sha256": state.guard_patch_sha256,
        "final_guard_ok": True,
        "final_guard_patch_sha256": state.final_guard_patch_sha256,
        "final_guard_sha256": state.final_guard_sha256,
        "head_sha256": state.head_sha256,
        "mutation_detected": False,
        "isolation_network_isolated": True,
        "isolation_credentials_absent": True,
        "isolation_verified_by": state.isolation_verified_by,
        "isolation_evidence_sha256": state.isolation_evidence_sha256,
        "checks": [
            {
                "command_id": item.command_id,
                "output_sha256": item.output_sha256,
                "exit_code": item.exit_code,
                "timed_out": item.timed_out,
            }
            for item in state.checks
        ],
    }
    if canonical != _canonical_json_bytes(payload):
        raise PublicationPreflightError(
            "finalized verifier fields do not match canonical bytes"
        )


def _require_policy_canonical_bytes(
    canonical: bytes,
    state: _PolicyState,
    *,
    unattended: bool,
) -> None:
    payload = {
        "schema_version": PROTECTED_POLICY_SCHEMA,
        "policy_id": state.policy_id,
        "repository": state.repository,
        "principal_mode": state.principal_mode.value,
        "expected_actor": state.expected_actor,
        "app_id": state.app_id,
        "installation_id": state.installation_id,
        "permissions": list(state.permissions),
        "unattended": unattended,
        "committer_name": state.committer.name,
        "committer_email": state.committer.email,
        "publisher_uid": state.publisher_uid,
        "publisher_home": state.publisher_home,
        "environment_keys": list(state.environment_keys),
        "runtime_attestor": state.runtime_attestor,
    }
    if canonical != _canonical_json_bytes(payload):
        raise PublicationPreflightError(
            "publisher policy fields do not match canonical bytes"
        )


def _validate_v1_candidate_authority(
    *,
    risk_tier: int,
    allowed_paths: tuple[str, ...],
    changed_paths: tuple[str, ...],
    profiles: tuple[str, ...],
    source_approver: str,
) -> None:
    if source_approver != "IvesLiu1026":
        raise PublicationPreflightError("candidate approver is not policy pinned")
    unknown_profiles = sorted(set(profiles) - _V1_PROFILE_IDS)
    if unknown_profiles:
        raise PublicationPreflightError("candidate profile is outside V1 authority")
    for pattern in allowed_paths:
        _validate_allowed_pattern(pattern)
        lowered = pattern.lower()
        parts = tuple(part for part in lowered.split("/") if part)
        if pattern == "**" or has_v1_forbidden_authority(pattern, pattern=True):
            raise PublicationPreflightError("candidate path is protected in V1")
        is_test_scope = (
            any(part in {"test", "tests", "fixtures"} for part in parts)
            or parts[-1].startswith(("test_", "test-"))
            or parts[-1].endswith(
                (
                    "_test.py",
                    ".test.js",
                    ".test.jsx",
                    ".test.mjs",
                    ".test.ts",
                    ".test.tsx",
                    ".spec.js",
                    ".spec.jsx",
                    ".spec.mjs",
                    ".spec.ts",
                    ".spec.tsx",
                )
            )
        )
        if risk_tier == 0:
            if not (lowered.startswith("docs/") or is_test_scope):
                raise PublicationPreflightError("Tier 0 path is outside V1 authority")
        elif not (
            any(lowered.startswith(prefix) for prefix in _V1_TIER1_PREFIXES)
            or is_test_scope
        ):
            raise PublicationPreflightError("Tier 1 path is outside V1 authority")
    for path in changed_paths:
        if has_v1_forbidden_authority(path):
            raise PublicationPreflightError("verified changed path is protected in V1")
        if not any(_path_matches_pattern(path, pattern) for pattern in allowed_paths):
            raise PublicationPreflightError(
                "verified changed path is outside candidate authority"
            )


def _validate_allowed_pattern(pattern: str) -> None:
    if (
        not pattern
        or pattern.startswith(("/", "\\"))
        or "\\" in pattern
        or "//" in pattern
        or not _PATH_PATTERN.fullmatch(pattern)
        or any(part in {"", ".", ".."} for part in pattern.split("/"))
        or any("**" in part and part != "**" for part in pattern.split("/"))
    ):
        raise PublicationPreflightError("candidate allowed path pattern is invalid")


def _path_matches_pattern(path: str, pattern: str) -> bool:
    if pattern == "**":
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        if not any(character in prefix for character in "*?"):
            return path == prefix or path.startswith(prefix + "/")
    index = 0
    expression: list[str] = ["^"]
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    expression.append("(?:.*/)?")
                else:
                    expression.append(".*")
                    continue
            else:
                expression.append("[^/]*")
        elif character == "?":
            expression.append("[^/]")
        else:
            expression.append(re.escape(character))
        index += 1
    expression.append("$")
    return re.fullmatch("".join(expression), path) is not None


def _copy_runtime_attestation(value: RuntimeAttestation) -> RuntimeAttestation:
    if not isinstance(value, RuntimeAttestation):
        raise PublicationPreflightError("runtime port returned invalid attestation")
    return RuntimeAttestation(
        uid=value.uid,
        home=_copy_string(value.home, "runtime home"),
        environment_keys=_copy_environment_keys(value.environment_keys),
        dedicated_uid=value.dedicated_uid,
        home_model_credentials_absent=value.home_model_credentials_absent,
        environment_model_credentials_absent=(
            value.environment_model_credentials_absent
        ),
        model_credential_keys=tuple(value.model_credential_keys),
        attested_by=_copy_string(value.attested_by, "runtime attestor"),
        evidence_sha256=_copy_sha256(
            value.evidence_sha256, "runtime attestation digest"
        ),
    )


def _validate_runtime(value: RuntimeAttestation, policy: _PolicyState) -> None:
    if value.uid != policy.publisher_uid or value.home != policy.publisher_home:
        raise PublicationPreflightError("publisher UID or home is not policy pinned")
    if value.environment_keys != policy.environment_keys:
        raise PublicationPreflightError("publisher environment keys changed")
    if not value.dedicated_uid:
        raise PublicationPreflightError("publisher UID is not dedicated")
    if not value.home_model_credentials_absent:
        raise PublicationPreflightError(
            "publisher home contains model credential state"
        )
    if not value.environment_model_credentials_absent:
        raise PublicationPreflightError(
            "publisher environment has model credential state"
        )
    if value.model_credential_keys:
        raise PublicationPreflightError("publisher observed model credential keys")
    if value.attested_by != policy.runtime_attestor:
        raise PublicationPreflightError("runtime attestor is not policy pinned")
    if set(value.environment_keys) & _MODEL_ENVIRONMENT_KEYS:
        raise PublicationPreflightError(
            "model credential key is environment allowlisted"
        )


def _freeze_principal(value: PrincipalSnapshot) -> _PrincipalState:
    if not isinstance(value, PrincipalSnapshot):
        raise PublicationPreflightError("principal port returned invalid data")
    try:
        mode = PrincipalMode(value.mode)
    except (TypeError, ValueError) as exc:
        raise PublicationPreflightError("principal mode is invalid") from exc
    actor = _copy_string(value.actor, "principal actor")
    if not _ACTOR.fullmatch(actor):
        raise PublicationPreflightError("principal actor is invalid")
    app_id = _copy_optional_positive_int(value.app_id, "principal App ID")
    installation_id = _copy_optional_positive_int(
        value.installation_id, "principal installation ID"
    )
    repository = _copy_string(value.repository, "principal repository")
    permissions = _copy_identifiers(value.permissions, _PERMISSION, "permissions")
    for name in (
        "repository_scoped",
        "is_admin",
        "can_bypass_branch_protection",
    ):
        if not isinstance(getattr(value, name), bool):
            raise PublicationPreflightError(f"principal {name} is invalid")
    if not isinstance(value.committer, GitIdentity):
        raise PublicationPreflightError("principal committer is invalid")
    committer = GitIdentity(value.committer.name, value.committer.email)
    return _PrincipalState(
        mode=mode,
        actor=actor,
        app_id=app_id,
        installation_id=installation_id,
        repository=repository,
        permissions=permissions,
        repository_scoped=value.repository_scoped,
        is_admin=value.is_admin,
        can_bypass_branch_protection=value.can_bypass_branch_protection,
        committer=committer,
    )


def _validate_principal(value: _PrincipalState, policy: _PolicyState) -> None:
    if (
        value.mode != policy.principal_mode
        or value.actor != policy.expected_actor
        or value.app_id != policy.app_id
        or value.installation_id != policy.installation_id
        or value.repository != policy.repository
        or value.permissions != policy.permissions
        or value.committer != policy.committer
    ):
        raise PublicationPreflightError("publisher principal is not policy pinned")
    if value.mode is PrincipalMode.GITHUB_APP:
        if not value.repository_scoped:
            raise PublicationPreflightError("publisher App is not repository scoped")
        if value.is_admin or value.can_bypass_branch_protection:
            raise PublicationPreflightError(
                "publisher App is admin or can bypass protection"
            )


def _copy_repository(value: RepositorySnapshot) -> RepositorySnapshot:
    if not isinstance(value, RepositorySnapshot):
        raise PublicationPreflightError("repository port returned invalid data")
    if not isinstance(value.public, bool) or not isinstance(value.is_fork, bool):
        raise PublicationPreflightError("repository visibility flags are invalid")
    return RepositorySnapshot(
        repository=_copy_string(value.repository, "repository name"),
        default_branch=_copy_string(value.default_branch, "default branch"),
        main_sha=_copy_git_oid(value.main_sha, "remote main SHA"),
        public=value.public,
        is_fork=value.is_fork,
    )


def _copy_patch(value: PatchSnapshot) -> PatchSnapshot:
    if not isinstance(value, PatchSnapshot):
        raise PublicationPreflightError("Git port returned invalid patch data")
    return PatchSnapshot(
        base_sha=_copy_git_oid(value.base_sha, "local patch base SHA"),
        patch_sha256=_copy_sha256(value.patch_sha256, "local patch digest"),
        head_sha256=_copy_sha256(value.head_sha256, "local head digest"),
        changed_paths=_copy_paths(
            value.changed_paths, "local changed paths", patterns=False
        ),
    )


def _copy_local_branch(
    value: LocalBranchSnapshot, expected_branch: str
) -> LocalBranchSnapshot:
    if not isinstance(value, LocalBranchSnapshot):
        raise PublicationPreflightError("Git port returned invalid local branch")
    branch = _copy_string(value.branch, "local branch")
    if branch != expected_branch:
        raise PublicationPreflightError("local branch read returned another branch")
    if not isinstance(value.checked_out, bool):
        raise PublicationPreflightError("local branch checkout flag is invalid")
    return LocalBranchSnapshot(
        branch=branch,
        head_sha=_copy_git_oid(value.head_sha, "local branch head SHA"),
        checked_out=value.checked_out,
    )


def _copy_commit(value: CommitRecord) -> CommitRecord:
    if not isinstance(value, CommitRecord):
        raise PublicationPreflightError("commit read returned invalid data")
    if not isinstance(value.author, GitIdentity) or not isinstance(
        value.committer, GitIdentity
    ):
        raise PublicationPreflightError("commit identities are invalid")
    return CommitRecord(
        head_sha=_copy_git_oid(value.head_sha, "commit head SHA"),
        parent_sha=_copy_git_oid(value.parent_sha, "commit parent SHA"),
        patch_sha256=_copy_sha256(value.patch_sha256, "commit patch digest"),
        head_sha256=_copy_sha256(value.head_sha256, "commit head digest"),
        message=_copy_string(value.message, "commit message"),
        author=GitIdentity(value.author.name, value.author.email),
        committer=GitIdentity(value.committer.name, value.committer.email),
    )


def _copy_pull_request(value: PullRequestSnapshot) -> PullRequestSnapshot:
    if not isinstance(value, PullRequestSnapshot):
        raise PublicationPreflightError("PR read returned invalid data")
    if not isinstance(value.draft, bool):
        raise PublicationPreflightError("PR draft flag is invalid")
    return PullRequestSnapshot(
        number=_copy_int(value.number, "PR number", minimum=1),
        url=_copy_string(value.url, "PR URL"),
        repository=_copy_string(value.repository, "PR repository"),
        base_branch=_copy_string(value.base_branch, "PR base branch"),
        head_branch=_copy_string(value.head_branch, "PR head branch"),
        head_sha=_copy_git_oid(value.head_sha, "PR head SHA"),
        actor=_copy_string(value.actor, "PR actor"),
        title=_copy_safe_line(value.title, "PR title", maximum=240),
        body_sha256=_copy_sha256(value.body_sha256, "PR body digest"),
        state=_copy_string(value.state, "PR state"),
        draft=value.draft,
    )


def _commit_message(evidence: _VerifierState) -> str:
    return (
        f"chore: address {evidence.candidate_id}\n\n"
        f"Candidate: {evidence.candidate_id}\n"
        f"Final-Guard-SHA256: {evidence.final_guard_sha256}\n"
        f"Patch-SHA256: {evidence.final_guard_patch_sha256}\n"
        f"Verification-SHA256: {evidence.canonical_sha256}\n\n"
        f"{AUTOMATION_TRAILER}\n"
    )


def _pr_title(evidence: _VerifierState) -> str:
    return f"[Daily Maintainer] {evidence.candidate_id} verified patch"


def _pr_body(evidence: _VerifierState) -> str:
    tick = chr(96)
    lines = [
        "## Verified daily maintenance",
        "",
        f"- Run: {tick}{evidence.run_id}{tick}",
        f"- Candidate: {tick}{evidence.candidate_id}{tick}",
        f"- Risk tier: {tick}{evidence.risk_tier}{tick}",
        f"- Base SHA: {tick}{evidence.base_sha}{tick}",
        f"- Patch SHA-256: {tick}{evidence.final_guard_patch_sha256}{tick}",
        f"- Final guard SHA-256: {tick}{evidence.final_guard_sha256}{tick}",
        f"- Head SHA-256: {tick}{evidence.head_sha256}{tick}",
        f"- Verification SHA-256: {tick}{evidence.canonical_sha256}{tick}",
        f"- Isolation evidence: {tick}{evidence.isolation_evidence_sha256}{tick}",
        "",
        "### Acceptance",
        "",
    ]
    lines.extend(f"- {_escape_markdown(item)}" for item in evidence.acceptance)
    lines.extend(("", "### Changed paths", ""))
    lines.extend(
        f"- {tick}{_escape_code(item)}{tick}" for item in evidence.changed_paths
    )
    lines.extend(("", "### Independent checks", ""))
    lines.extend(
        f"- {tick}{item.command_id}{tick}: {tick}{item.output_sha256}{tick}"
        for item in evidence.checks
    )
    lines.extend(("", AUTOMATION_TRAILER, ""))
    body = "\n".join(lines)
    _reject_secret_material((body,), "pull request")
    return body


def _escape_markdown(value: str) -> str:
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for character in "\\`*_{}[]()#+-.!|":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped.replace("@", "&#64;")


def _escape_code(value: str) -> str:
    return value.replace("`", "&#96;").replace("@", "&#64;")


def _runtime_attestation_digest(
    *,
    uid: int,
    home: str,
    environment_keys: tuple[str, ...],
    dedicated_uid: bool,
    home_model_credentials_absent: bool,
    environment_model_credentials_absent: bool,
    model_credential_keys: tuple[str, ...],
    attested_by: str,
) -> str:
    payload = {
        "schema_version": "vista.world.daily-maintainer.publisher-runtime.v1",
        "uid": uid,
        "home": home,
        "environment_keys": list(environment_keys),
        "dedicated_uid": dedicated_uid,
        "home_model_credentials_absent": home_model_credentials_absent,
        "environment_model_credentials_absent": (environment_model_credentials_absent),
        "model_credential_keys": list(model_credential_keys),
        "attested_by": attested_by,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_bytes(value: object, label: str) -> bytes:
    canonical = getattr(value, "canonical_bytes", None)
    if type(canonical) is not bytes or not canonical or len(canonical) > 1024 * 1024:
        raise PublicationPreflightError(f"{label} canonical bytes are invalid")
    return bytes(canonical)


def _canonical_date(value: object) -> str:
    text = _copy_string(value, "run date")
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise PublicationPreflightError("run date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise PublicationPreflightError("run date is not canonical")
    return text


def _copy_string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise PublicationPreflightError(f"{label} is invalid")
    return str(value)


def _copy_safe_line(value: object, label: str, *, maximum: int) -> str:
    text = _copy_string(value, label)
    if len(text) > maximum or _CONTROL.search(text):
        raise PublicationPreflightError(f"{label} contains control or newline")
    return text


def _copy_int(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or type(value) is not int or value < minimum:
        raise PublicationPreflightError(f"{label} is invalid")
    return int(value)


def _copy_optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _copy_int(value, label, minimum=1)


def _copy_git_oid(value: object, label: str) -> str:
    text = _copy_string(value, label)
    _require_git_oid(text, label)
    return text


def _copy_sha256(value: object, label: str) -> str:
    text = _copy_string(value, label)
    _require_sha256(text, label)
    return text


def _copy_lines(value: object, label: str, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise PublicationPreflightError(f"{label} must be a non-empty tuple")
    result = tuple(
        _copy_safe_line(item, f"{label} item", maximum=maximum) for item in value
    )
    if len(set(result)) != len(result):
        raise PublicationPreflightError(f"{label} must be unique")
    return result


def _copy_paths(value: object, label: str, *, patterns: bool) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise PublicationPreflightError(f"{label} must be a non-empty tuple")
    result = tuple(_copy_safe_line(item, label, maximum=300) for item in value)
    if tuple(sorted(result)) != result or len(set(result)) != len(result):
        raise PublicationPreflightError(f"{label} must be sorted and unique")
    for item in result:
        if "\\" in item:
            raise PublicationPreflightError(f"{label} contains backslash")
        path = PurePosixPath(item)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise PublicationPreflightError(f"{label} is not normalized relative path")
        if not patterns and item != path.as_posix():
            raise PublicationPreflightError(f"{label} is not canonical path")
    return result


def _copy_identifiers(
    value: object, pattern: re.Pattern[str], label: str
) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise PublicationPreflightError(f"{label} must be a non-empty tuple")
    result = tuple(_copy_string(item, label) for item in value)
    if tuple(sorted(result)) != result or len(set(result)) != len(result):
        raise PublicationPreflightError(f"{label} must be sorted and unique")
    if any(not pattern.fullmatch(item) for item in result):
        raise PublicationPreflightError(f"{label} contains invalid value")
    return result


def _copy_environment_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise PublicationPreflightError("environment keys must be a tuple")
    result = tuple(_copy_string(item, "environment key") for item in value)
    _validate_environment_keys(result)
    return result


def _validate_environment_keys(value: tuple[str, ...]) -> None:
    if tuple(sorted(value)) != value or len(set(value)) != len(value):
        raise PublicationContractError("environment keys must be sorted and unique")
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item) for item in value):
        raise PublicationContractError("environment key is invalid")


def _validate_home(value: object) -> None:
    if type(value) is not str or _CONTROL.search(value):
        raise PublicationContractError("publisher home is invalid")
    path = PurePosixPath(value)
    if not path.is_absolute() or value != path.as_posix() or value == "/":
        raise PublicationContractError("publisher home must be dedicated absolute path")


def _validate_branch(value: object) -> None:
    if not is_v1_daily_branch_name(value):
        raise PublicationContractError("publication branch is invalid")


def _safe_one_line(value: object, label: str, *, maximum: int) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or _CONTROL.search(value)
    ):
        raise PublicationContractError(f"{label} contains control or newline")


def _require_git_oid(value: object, label: str) -> None:
    if type(value) is not str or not _GIT_OBJECT_ID.fullmatch(value):
        raise PublicationContractError(f"{label} must be lowercase Git object ID")


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise PublicationContractError(f"{label} must be lowercase SHA-256")


def _reject_secret_material(values: tuple[str, ...], label: str) -> None:
    if any(pattern.search(value) for value in values for pattern in _SECRET_PATTERNS):
        raise PublicationContractError(f"{label} contains credential-like material")


IVES_AUTHOR = GitIdentity("Ives Liu", "zhiy0517xiang@gmail.com")
