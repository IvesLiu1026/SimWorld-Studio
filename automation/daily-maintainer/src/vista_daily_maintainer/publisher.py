from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Protocol, TypeVar


CANONICAL_REPOSITORY = "IvesLiu1026/VISTA-World"
DEFAULT_BRANCH = "main"
AUTOMATION_TRAILER = "Automated-by: Codex Daily Maintainer"

_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_ID = re.compile(r"^VW-DM-[0-9]{4,}$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_COMMAND_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_PERMISSION = re.compile(r"^[a-z][a-z0-9_]*:(?:read|write)$")
_ACTOR = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})|"
    r"[A-Za-z0-9][A-Za-z0-9-]{0,93}\[bot\])$"
)
_EMAIL = re.compile(r"^[^\s<>@]+@[^\s<>@]+$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_MATERIAL = re.compile(
    r"(?i)(?:"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"gh[opusr]_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"(?:OPENAI|ANTHROPIC|CLAUDE|CODEX|GEMINI|OPENROUTER|REPLICATE|"
    r"COHERE|MISTRAL|GROQ|XAI)_(?:API_)?(?:KEY|TOKEN|AUTH)|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}"
    r")"
)

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
_UNATTENDED_FORBIDDEN_ENVIRONMENT_KEYS = frozenset(
    {"GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"}
)
_REQUIRED_APP_PERMISSIONS = frozenset(
    {"metadata:read", "contents:write", "pull_requests:write"}
)
_FORBIDDEN_UNATTENDED_PERMISSIONS = frozenset(
    {
        "actions:write",
        "administration:write",
        "deployments:write",
        "environments:write",
        "members:write",
        "organization_administration:write",
        "secrets:write",
        "workflows:write",
    }
)


class PublicationError(RuntimeError):
    """Base class for fail-closed publisher failures."""


class PublicationContractError(PublicationError, ValueError):
    """Publication input does not satisfy the approved stable contract."""


class PublicationPreflightError(PublicationError):
    """Identity, repository, credential boundary, or digest preflight failed."""


class PublicationConflictError(PublicationError):
    """A branch or pull request already occupies the deterministic run name."""


class PublicationOperationError(PublicationError):
    """A dependency port failed without exposing its possibly sensitive error."""


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
class VerifiedCheck:
    command_id: str
    output_sha256: str
    exit_code: int = 0
    timed_out: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not _COMMAND_ID.fullmatch(
            self.command_id
        ):
            raise PublicationContractError("verified command ID is invalid")
        _require_sha256(self.output_sha256, "verified command output digest")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise PublicationContractError("verified command exit code is invalid")
        if not isinstance(self.timed_out, bool):
            raise PublicationContractError("verified command timeout flag is invalid")
        if self.exit_code != 0 or self.timed_out:
            raise PublicationContractError("publisher requires successful verification")


@dataclass(frozen=True)
class VerifiedPatch:
    """Immutable, self-bound evidence emitted by the credential-free verifier.

    ``head_sha256`` is a digest of the complete candidate head content before a
    commit is made. It complements the Git base object ID and the canonical
    patch digest, so a publisher can reject any worktree mutation after verify.
    """

    repository: str
    run_date: str
    candidate_id: str
    candidate_slug: str
    candidate_title: str
    risk_tier: int
    acceptance: tuple[str, ...]
    changed_paths: tuple[str, ...]
    base_sha: str
    patch_sha256: str
    head_sha256: str
    checks: tuple[VerifiedCheck, ...]
    verification_sha256: str

    def __post_init__(self) -> None:
        if self.repository != CANONICAL_REPOSITORY:
            raise PublicationContractError(
                f"publisher repository must be {CANONICAL_REPOSITORY}"
            )
        try:
            parsed_date = dt.date.fromisoformat(self.run_date)
        except (TypeError, ValueError) as exc:
            raise PublicationContractError("run date must be YYYY-MM-DD") from exc
        if parsed_date.isoformat() != self.run_date:
            raise PublicationContractError("run date must be canonical YYYY-MM-DD")
        if not isinstance(self.candidate_id, str) or not _CANDIDATE_ID.fullmatch(
            self.candidate_id
        ):
            raise PublicationContractError("candidate ID is invalid")
        if (
            not isinstance(self.candidate_slug, str)
            or len(self.candidate_slug) > 48
            or not _SLUG.fullmatch(self.candidate_slug)
        ):
            raise PublicationContractError("candidate slug is invalid")
        _safe_one_line(self.candidate_title, "candidate title", maximum=160)
        if isinstance(self.risk_tier, bool) or self.risk_tier not in {0, 1, 2, 3}:
            raise PublicationContractError("risk tier must be 0, 1, 2, or 3")
        if not isinstance(self.acceptance, tuple) or not self.acceptance:
            raise PublicationContractError("acceptance must be a non-empty tuple")
        for criterion in self.acceptance:
            _safe_one_line(criterion, "acceptance criterion", maximum=500)
        if (
            not isinstance(self.changed_paths, tuple)
            or not self.changed_paths
            or tuple(sorted(self.changed_paths)) != self.changed_paths
            or len(set(self.changed_paths)) != len(self.changed_paths)
        ):
            raise PublicationContractError(
                "changed paths must be a non-empty sorted unique tuple"
            )
        for path in self.changed_paths:
            _validate_relative_path(path)
        _require_git_oid(self.base_sha, "verified base SHA")
        _require_sha256(self.patch_sha256, "verified patch digest")
        _require_sha256(self.head_sha256, "verified head digest")
        if not isinstance(self.checks, tuple) or not self.checks:
            raise PublicationContractError("verified checks must be non-empty")
        if any(not isinstance(item, VerifiedCheck) for item in self.checks):
            raise PublicationContractError("verified checks have an invalid member")
        if len({item.command_id for item in self.checks}) != len(self.checks):
            raise PublicationContractError("verified check IDs must be unique")
        _require_sha256(self.verification_sha256, "verification binding digest")
        _reject_secret_material(
            (
                self.candidate_id,
                self.candidate_slug,
                self.candidate_title,
                *self.acceptance,
                *self.changed_paths,
            ),
            "verified patch",
        )
        if self.verification_sha256 != self.expected_verification_sha256():
            raise PublicationContractError("verification binding digest does not match")

    @classmethod
    def bind(
        cls,
        *,
        repository: str,
        run_date: str,
        candidate_id: str,
        candidate_slug: str,
        candidate_title: str,
        risk_tier: int,
        acceptance: tuple[str, ...],
        changed_paths: tuple[str, ...],
        base_sha: str,
        patch_sha256: str,
        head_sha256: str,
        checks: tuple[VerifiedCheck, ...],
    ) -> VerifiedPatch:
        fields: dict[str, object] = {
            "repository": repository,
            "run_date": run_date,
            "candidate_id": candidate_id,
            "candidate_slug": candidate_slug,
            "candidate_title": candidate_title,
            "risk_tier": risk_tier,
            "acceptance": acceptance,
            "changed_paths": changed_paths,
            "base_sha": base_sha,
            "patch_sha256": patch_sha256,
            "head_sha256": head_sha256,
            "checks": checks,
        }
        digest = _verification_digest(fields)
        return cls(**fields, verification_sha256=digest)  # type: ignore[arg-type]

    def expected_verification_sha256(self) -> str:
        return _verification_digest(
            {
                "repository": self.repository,
                "run_date": self.run_date,
                "candidate_id": self.candidate_id,
                "candidate_slug": self.candidate_slug,
                "candidate_title": self.candidate_title,
                "risk_tier": self.risk_tier,
                "acceptance": self.acceptance,
                "changed_paths": self.changed_paths,
                "base_sha": self.base_sha,
                "patch_sha256": self.patch_sha256,
                "head_sha256": self.head_sha256,
                "checks": self.checks,
            }
        )

    @property
    def branch(self) -> str:
        return f"codex/daily/{self.run_date}-{self.candidate_slug}"


@dataclass(frozen=True)
class PublicationPolicy:
    principal_mode: PrincipalMode
    expected_actor: str
    unattended: bool

    def __post_init__(self) -> None:
        try:
            mode = PrincipalMode(self.principal_mode)
        except ValueError as exc:
            raise PublicationContractError(
                "publisher principal mode is invalid"
            ) from exc
        object.__setattr__(self, "principal_mode", mode)
        if not isinstance(self.expected_actor, str) or not _ACTOR.fullmatch(
            self.expected_actor
        ):
            raise PublicationContractError("expected publisher actor is invalid")
        if not isinstance(self.unattended, bool):
            raise PublicationContractError("unattended flag must be boolean")
        if self.unattended and mode is not PrincipalMode.GITHUB_APP:
            raise PublicationContractError(
                "unattended publication requires the GitHub App principal"
            )


@dataclass(frozen=True)
class PrincipalSnapshot:
    mode: PrincipalMode
    actor: str
    repository: str
    permissions: frozenset[str]
    repository_scoped: bool
    is_admin: bool
    can_bypass_branch_protection: bool
    committer: GitIdentity

    def __post_init__(self) -> None:
        try:
            mode = PrincipalMode(self.mode)
        except ValueError as exc:
            raise PublicationContractError(
                "principal snapshot mode is invalid"
            ) from exc
        object.__setattr__(self, "mode", mode)
        if not isinstance(self.actor, str) or not _ACTOR.fullmatch(self.actor):
            raise PublicationContractError("principal snapshot actor is invalid")
        if not isinstance(self.repository, str):
            raise PublicationContractError("principal repository is invalid")
        if not isinstance(self.permissions, frozenset) or any(
            not isinstance(item, str) or not _PERMISSION.fullmatch(item)
            for item in self.permissions
        ):
            raise PublicationContractError("principal permissions are invalid")
        for name in (
            "repository_scoped",
            "is_admin",
            "can_bypass_branch_protection",
        ):
            if not isinstance(getattr(self, name), bool):
                raise PublicationContractError(f"principal {name} flag is invalid")
        if not isinstance(self.committer, GitIdentity):
            raise PublicationContractError("principal committer is invalid")


@dataclass(frozen=True)
class RepositorySnapshot:
    repository: str
    default_branch: str
    main_sha: str
    public: bool
    is_fork: bool

    def __post_init__(self) -> None:
        _require_git_oid(self.main_sha, "remote main SHA")
        if not isinstance(self.public, bool) or not isinstance(self.is_fork, bool):
            raise PublicationContractError("repository visibility flags are invalid")


@dataclass(frozen=True)
class PatchSnapshot:
    base_sha: str
    patch_sha256: str
    head_sha256: str
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_git_oid(self.base_sha, "local patch base SHA")
        _require_sha256(self.patch_sha256, "local patch digest")
        _require_sha256(self.head_sha256, "local head digest")
        if not isinstance(self.changed_paths, tuple):
            raise PublicationContractError("local changed paths must be a tuple")


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

    def __post_init__(self) -> None:
        _require_git_oid(self.head_sha, "commit head SHA")
        _require_git_oid(self.parent_sha, "commit parent SHA")
        _require_sha256(self.patch_sha256, "committed patch digest")
        _require_sha256(self.head_sha256, "committed head digest")


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
class ExistingPullRequest:
    number: int
    state: str
    head_branch: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.number, bool)
            or not isinstance(self.number, int)
            or self.number < 1
        ):
            raise PublicationContractError("existing PR number is invalid")
        if self.state not in {"open", "closed", "merged"}:
            raise PublicationContractError("existing PR state is invalid")
        _validate_branch(self.head_branch)


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
        if not isinstance(self.body, str) or len(self.body) > 20_000:
            raise PublicationContractError("PR body is invalid")
        if _CONTROL.search(self.body):
            raise PublicationContractError("PR body contains control characters")
        _reject_secret_material((self.title, self.body), "pull request")
        if not self.draft:
            raise PublicationContractError("publisher may only open draft PRs")


@dataclass(frozen=True)
class PullRequestRecord:
    number: int
    url: str
    repository: str
    base_branch: str
    head_branch: str
    actor: str
    draft: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.number, bool)
            or not isinstance(self.number, int)
            or self.number < 1
        ):
            raise PublicationContractError("PR result number is invalid")
        expected_url = f"https://github.com/{self.repository}/pull/{self.number}"
        if self.url != expected_url:
            raise PublicationContractError("PR result URL is invalid")
        if not isinstance(self.actor, str) or not _ACTOR.fullmatch(self.actor):
            raise PublicationContractError("PR result actor is invalid")
        if not isinstance(self.draft, bool):
            raise PublicationContractError("PR result draft flag is invalid")


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
    draft: bool = True


class GitPort(Protocol):
    def inspect_patch(self, worktree: Path, base_sha: str) -> PatchSnapshot: ...

    def local_branch_exists(self, worktree: Path, branch: str) -> bool: ...

    def create_branch(self, worktree: Path, branch: str, start_sha: str) -> None: ...

    def commit(self, worktree: Path, spec: CommitSpec) -> CommitRecord: ...

    def push_new_branch(self, worktree: Path, spec: PushSpec) -> None: ...


class GitHubPort(Protocol):
    def inspect_principal(
        self, repository: str, mode: PrincipalMode
    ) -> PrincipalSnapshot: ...

    def inspect_repository(self, repository: str) -> RepositorySnapshot: ...

    def read_branch_sha(self, repository: str, branch: str) -> str | None: ...

    def list_pull_requests(
        self, repository: str, head_branch: str
    ) -> tuple[ExistingPullRequest, ...]: ...

    def open_draft_pull_request(
        self, spec: DraftPullRequestSpec
    ) -> PullRequestRecord: ...


_T = TypeVar("_T")


class Publisher:
    """Publish one verified patch without owning model or promotion credentials."""

    def __init__(
        self,
        *,
        git: GitPort,
        github: GitHubPort,
        environment_keys: Iterable[str] | None = None,
    ) -> None:
        self._git = git
        self._github = github
        source = os.environ.keys() if environment_keys is None else environment_keys
        self._environment_keys = frozenset(str(item).upper() for item in source)

    def publish(
        self,
        worktree: Path,
        verified: VerifiedPatch,
        policy: PublicationPolicy,
    ) -> PublicationResult:
        if not isinstance(verified, VerifiedPatch):
            raise PublicationContractError("publisher requires VerifiedPatch evidence")
        if not isinstance(policy, PublicationPolicy):
            raise PublicationContractError("publisher requires PublicationPolicy")
        repo = self._validated_worktree(worktree)
        self._validate_environment(policy)

        principal = self._port_call(
            "principal preflight",
            lambda: self._github.inspect_principal(
                CANONICAL_REPOSITORY, policy.principal_mode
            ),
        )
        self._validate_principal(principal, policy)

        repository = self._port_call(
            "repository preflight",
            lambda: self._github.inspect_repository(CANONICAL_REPOSITORY),
        )
        self._validate_repository(repository, verified)

        patch = self._port_call(
            "verified patch inspection",
            lambda: self._git.inspect_patch(repo, verified.base_sha),
        )
        self._validate_patch_snapshot(patch, verified)

        branch = verified.branch
        local_branch_exists = self._port_call(
            "local branch lookup",
            lambda: self._git.local_branch_exists(repo, branch),
        )
        if not isinstance(local_branch_exists, bool):
            raise PublicationPreflightError("local branch lookup returned invalid data")
        if local_branch_exists:
            raise PublicationConflictError(
                f"local publication branch already exists: {branch}"
            )

        remote_branch = self._port_call(
            "remote branch lookup",
            lambda: self._github.read_branch_sha(CANONICAL_REPOSITORY, branch),
        )
        if remote_branch is not None:
            _require_git_oid(remote_branch, "remote publication branch SHA")
            raise PublicationConflictError(
                f"remote publication branch already exists: {branch}"
            )

        pull_requests = self._port_call(
            "pull request lookup",
            lambda: self._github.list_pull_requests(CANONICAL_REPOSITORY, branch),
        )
        if not isinstance(pull_requests, tuple) or any(
            not isinstance(item, ExistingPullRequest) for item in pull_requests
        ):
            raise PublicationPreflightError("pull request lookup returned invalid data")
        if pull_requests:
            raise PublicationConflictError(
                f"publication branch already has a pull request: {branch}"
            )

        self._assert_remote_main(verified.base_sha, "before local mutation")
        message = _commit_message(verified)
        pr_spec = DraftPullRequestSpec(
            repository=CANONICAL_REPOSITORY,
            base_branch=DEFAULT_BRANCH,
            head_branch=branch,
            title=_pr_title(verified),
            body=_pr_body(verified),
        )

        self._port_call(
            "local branch creation",
            lambda: self._git.create_branch(repo, branch, verified.base_sha),
        )
        second_patch = self._port_call(
            "pre-commit patch inspection",
            lambda: self._git.inspect_patch(repo, verified.base_sha),
        )
        self._validate_patch_snapshot(second_patch, verified)

        commit_spec = CommitSpec(
            paths=verified.changed_paths,
            message=message,
            author=IVES_AUTHOR,
            committer=principal.committer,
            amend=False,
        )
        commit = self._port_call(
            "commit creation", lambda: self._git.commit(repo, commit_spec)
        )
        self._validate_commit(commit, commit_spec, verified)

        self._assert_remote_main(verified.base_sha, "before branch push")
        push_spec = PushSpec(
            repository=CANONICAL_REPOSITORY,
            branch=branch,
            head_sha=commit.head_sha,
            expected_main_sha=verified.base_sha,
            force=False,
        )
        self._port_call(
            "new branch push", lambda: self._git.push_new_branch(repo, push_spec)
        )
        self._assert_remote_main(verified.base_sha, "after branch push")

        pr = self._port_call(
            "draft pull request creation",
            lambda: self._github.open_draft_pull_request(pr_spec),
        )
        self._validate_pull_request(pr, pr_spec, principal)
        return PublicationResult(
            repository=CANONICAL_REPOSITORY,
            base_sha=verified.base_sha,
            head_sha=commit.head_sha,
            branch=branch,
            patch_sha256=verified.patch_sha256,
            verification_sha256=verified.verification_sha256,
            commit_author=commit.author,
            commit_committer=commit.committer,
            pr_actor=pr.actor,
            pr_number=pr.number,
            pr_url=pr.url,
            draft=True,
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

    def _validate_environment(self, policy: PublicationPolicy) -> None:
        model_keys = sorted(self._environment_keys & _MODEL_ENVIRONMENT_KEYS)
        if model_keys:
            raise PublicationPreflightError(
                "publisher environment contains a model credential boundary key"
            )
        if policy.unattended and (
            self._environment_keys & _UNATTENDED_FORBIDDEN_ENVIRONMENT_KEYS
        ):
            raise PublicationPreflightError(
                "unattended App publisher inherited CLI or SSH credential state"
            )

    @staticmethod
    def _validate_principal(
        principal: PrincipalSnapshot, policy: PublicationPolicy
    ) -> None:
        if not isinstance(principal, PrincipalSnapshot):
            raise PublicationPreflightError("principal preflight returned invalid data")
        if principal.mode is not policy.principal_mode:
            raise PublicationPreflightError("selected publisher mode does not match")
        if principal.actor != policy.expected_actor:
            raise PublicationPreflightError("selected publisher actor does not match")
        if principal.repository != CANONICAL_REPOSITORY:
            raise PublicationPreflightError(
                "publisher principal is scoped to another repo"
            )
        if policy.principal_mode is PrincipalMode.CLI_BOOTSTRAP:
            if policy.expected_actor != "IvesLiu1026":
                raise PublicationPreflightError(
                    "CLI bootstrap must authenticate as IvesLiu1026"
                )
        if policy.unattended:
            if not principal.repository_scoped:
                raise PublicationPreflightError(
                    "unattended publisher must be repository scoped"
                )
            if principal.is_admin or principal.can_bypass_branch_protection:
                raise PublicationPreflightError(
                    "unattended publisher cannot be admin or bypass protection"
                )
            if not _REQUIRED_APP_PERMISSIONS <= principal.permissions:
                raise PublicationPreflightError(
                    "unattended publisher lacks required repository permissions"
                )
            if principal.permissions & _FORBIDDEN_UNATTENDED_PERMISSIONS:
                raise PublicationPreflightError(
                    "unattended publisher has a forbidden write permission"
                )

    @staticmethod
    def _validate_repository(
        repository: RepositorySnapshot, verified: VerifiedPatch
    ) -> None:
        if not isinstance(repository, RepositorySnapshot):
            raise PublicationPreflightError(
                "repository preflight returned invalid data"
            )
        if repository.repository != CANONICAL_REPOSITORY:
            raise PublicationPreflightError("remote repository identity does not match")
        if repository.default_branch != DEFAULT_BRANCH:
            raise PublicationPreflightError("remote default branch is not main")
        if not repository.public or repository.is_fork:
            raise PublicationPreflightError(
                "canonical repository must remain public and non-fork"
            )
        if repository.main_sha != verified.base_sha:
            raise PublicationPreflightError("verified base is not current remote main")

    @staticmethod
    def _validate_patch_snapshot(patch: PatchSnapshot, verified: VerifiedPatch) -> None:
        if not isinstance(patch, PatchSnapshot):
            raise PublicationPreflightError("patch inspection returned invalid data")
        if patch.base_sha != verified.base_sha:
            raise PublicationPreflightError(
                "local patch base changed after verification"
            )
        if patch.patch_sha256 != verified.patch_sha256:
            raise PublicationPreflightError(
                "local patch digest changed after verification"
            )
        if patch.head_sha256 != verified.head_sha256:
            raise PublicationPreflightError(
                "local head digest changed after verification"
            )
        if patch.changed_paths != verified.changed_paths:
            raise PublicationPreflightError(
                "local changed path set changed after verification"
            )

    @staticmethod
    def _validate_commit(
        commit: CommitRecord, spec: CommitSpec, verified: VerifiedPatch
    ) -> None:
        if not isinstance(commit, CommitRecord):
            raise PublicationPreflightError("commit operation returned invalid data")
        if commit.parent_sha != verified.base_sha:
            raise PublicationPreflightError(
                "created commit does not descend from verified base"
            )
        if commit.patch_sha256 != verified.patch_sha256:
            raise PublicationPreflightError(
                "committed patch digest does not match verified patch"
            )
        if commit.head_sha256 != verified.head_sha256:
            raise PublicationPreflightError(
                "committed head digest does not match verified head"
            )
        if commit.message != spec.message:
            raise PublicationPreflightError("created commit message does not match")
        if commit.author != IVES_AUTHOR or commit.author != spec.author:
            raise PublicationPreflightError("created commit author does not match")
        if commit.committer != spec.committer:
            raise PublicationPreflightError("created commit committer does not match")

    def _assert_remote_main(self, expected: str, stage: str) -> None:
        remote_main = self._port_call(
            f"remote main check {stage}",
            lambda: self._github.read_branch_sha(CANONICAL_REPOSITORY, DEFAULT_BRANCH),
        )
        if remote_main != expected:
            raise PublicationPreflightError(f"remote main moved {stage}")

    @staticmethod
    def _validate_pull_request(
        pr: PullRequestRecord,
        spec: DraftPullRequestSpec,
        principal: PrincipalSnapshot,
    ) -> None:
        if not isinstance(pr, PullRequestRecord):
            raise PublicationPreflightError("PR operation returned invalid data")
        if pr.repository != spec.repository:
            raise PublicationPreflightError("created PR repository does not match")
        if pr.base_branch != spec.base_branch or pr.head_branch != spec.head_branch:
            raise PublicationPreflightError("created PR branches do not match")
        if pr.actor != principal.actor:
            raise PublicationPreflightError("created PR actor does not match principal")
        if not pr.draft:
            raise PublicationPreflightError("created PR is not draft")

    @staticmethod
    def _port_call(stage: str, call: Callable[[], _T]) -> _T:
        try:
            return call()
        except PublicationError:
            raise
        except Exception:
            # Port exceptions may contain command lines, URLs or credentials.
            raise PublicationOperationError(f"publisher port failed: {stage}") from None


def _verification_digest(fields: dict[str, object]) -> str:
    checks = fields["checks"]
    if not isinstance(checks, tuple):
        raise PublicationContractError("checks must be a tuple")
    payload = {
        "schema_version": "vista.world.daily-maintainer.verified-patch.v1",
        "repository": fields["repository"],
        "run_date": fields["run_date"],
        "candidate_id": fields["candidate_id"],
        "candidate_slug": fields["candidate_slug"],
        "candidate_title": fields["candidate_title"],
        "risk_tier": fields["risk_tier"],
        "acceptance": list(fields["acceptance"]),  # type: ignore[arg-type]
        "changed_paths": list(fields["changed_paths"]),  # type: ignore[arg-type]
        "base_sha": fields["base_sha"],
        "patch_sha256": fields["patch_sha256"],
        "head_sha256": fields["head_sha256"],
        "checks": [
            {
                "command_id": item.command_id,
                "output_sha256": item.output_sha256,
                "exit_code": item.exit_code,
                "timed_out": item.timed_out,
            }
            for item in checks
            if isinstance(item, VerifiedCheck)
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _commit_message(verified: VerifiedPatch) -> str:
    return (
        f"chore: address {verified.candidate_id}\n\n"
        f"Candidate: {verified.candidate_id}\n"
        f"Patch-SHA256: {verified.patch_sha256}\n"
        f"Verification-SHA256: {verified.verification_sha256}\n\n"
        f"{AUTOMATION_TRAILER}\n"
    )


def _pr_title(verified: VerifiedPatch) -> str:
    return f"[Daily Maintainer] {verified.candidate_id}: {verified.candidate_title}"


def _pr_body(verified: VerifiedPatch) -> str:
    tick = chr(96)
    lines = [
        "## Verified daily maintenance",
        "",
        f"- Candidate: {tick}{verified.candidate_id}{tick}",
        f"- Risk tier: {tick}{verified.risk_tier}{tick}",
        f"- Base SHA: {tick}{verified.base_sha}{tick}",
        f"- Patch SHA-256: {tick}{verified.patch_sha256}{tick}",
        f"- Head SHA-256: {tick}{verified.head_sha256}{tick}",
        f"- Verification SHA-256: {tick}{verified.verification_sha256}{tick}",
        "",
        "### Acceptance",
        "",
    ]
    lines.extend(f"- {_escape_markdown(item)}" for item in verified.acceptance)
    lines.extend(("", "### Changed paths", ""))
    lines.extend(
        f"- {tick}{_escape_code(item)}{tick}" for item in verified.changed_paths
    )
    lines.extend(("", "### Independent checks", ""))
    lines.extend(
        f"- {tick}{item.command_id}{tick}: {tick}{item.output_sha256}{tick}"
        for item in verified.checks
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


def _safe_one_line(value: object, label: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\n" in value
        or "\r" in value
        or _CONTROL.search(value)
    ):
        raise PublicationContractError(f"{label} must be safe one-line text")


def _validate_relative_path(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or _CONTROL.search(value)
    ):
        raise PublicationContractError("changed path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PublicationContractError("changed path must be normalized and relative")


def _validate_branch(value: object) -> None:
    if (
        not isinstance(value, str)
        or not re.fullmatch(
            r"codex/daily/[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*",
            value,
        )
        or len(value) > 128
    ):
        raise PublicationContractError("publication branch is invalid")


def _require_git_oid(value: object, label: str) -> None:
    if not isinstance(value, str) or not _GIT_OBJECT_ID.fullmatch(value):
        raise PublicationContractError(f"{label} must be a lowercase Git object ID")


def _require_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PublicationContractError(f"{label} must be lowercase SHA-256")


def _reject_secret_material(values: Iterable[str], label: str) -> None:
    if any(_SECRET_MATERIAL.search(value) for value in values):
        raise PublicationContractError(f"{label} contains credential-like material")


IVES_AUTHOR = GitIdentity("Ives Liu", "zhiy0517xiang@gmail.com")
