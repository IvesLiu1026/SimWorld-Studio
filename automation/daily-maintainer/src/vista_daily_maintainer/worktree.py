from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .state import (
    BranchDisposition,
    Lifecycle,
    PublicationSnapshot,
    PullRequestState,
    RunKey,
    RunState,
    RunStateStore,
    StateContractError,
    StateError,
    UnsafeStatePathError,
    ensure_private_directory,
)


_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REMOTE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")


class WorktreeError(StateError):
    """Base class for isolated Git worktree lifecycle failures."""


class GitOperationError(WorktreeError):
    """A fixed Git operation failed without exposing raw subprocess output."""


class RepositoryRootError(WorktreeError):
    """The configured checkout is not the exact non-bare repository root."""


class DirtyRepositoryError(WorktreeError):
    """A source checkout or managed worktree contains uncommitted content."""


class RemoteUnavailableError(WorktreeError):
    """The configured remote branch cannot be pinned unambiguously."""


class RemoteMainMovedError(WorktreeError):
    """The remote branch no longer matches the immutable run base."""

    def __init__(self, state: RunState, current_sha: str) -> None:
        super().__init__(
            f"remote {state.remote}/{state.remote_branch} moved away from pinned base"
        )
        self.state = state
        self.previous_sha = state.key.base_sha
        self.current_sha = current_sha


class ExistingDailyBranchError(WorktreeError):
    """A daily branch exists but was not created by this idempotent run state."""

    def __init__(self, state: RunState) -> None:
        super().__init__("daily branch already exists; refusing to modify or delete it")
        self.state = state


class ExistingPublicationError(WorktreeError):
    """An existing PR snapshot blocks a new local patch attempt."""

    def __init__(self, state: RunState) -> None:
        super().__init__("existing pull request state blocks a new patch attempt")
        self.state = state


@dataclass(frozen=True)
class RemotePin:
    repository: str
    remote: str
    branch: str
    sha: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise StateContractError("pin repository must be owner/name")
        if not _REMOTE.fullmatch(self.remote):
            raise StateContractError("pin remote name is invalid")
        if not _REMOTE_BRANCH.fullmatch(self.branch) or any(
            part in {"", ".", ".."} for part in self.branch.split("/")
        ):
            raise StateContractError("pin remote branch is invalid")
        if not _SHA.fullmatch(self.sha):
            raise StateContractError("pin SHA must be an exact lowercase object ID")


@dataclass(frozen=True)
class PreflightResult:
    repository_root: str
    repository: str
    pin: RemotePin


@dataclass(frozen=True)
class PrepareResult:
    state: RunState
    idempotent_replay: bool
    recovered_stale_lock: bool


def _safe_git_environment() -> Mapping[str, str]:
    path = os.environ.get("PATH", "/usr/bin:/bin")
    return {
        "PATH": path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


class WorktreeManager:
    """Pin remote main and create one bounded, idempotent daily worktree."""

    def __init__(
        self,
        *,
        repository_root: Path | str,
        state_store: RunStateStore,
        worktrees_root: Path | str,
        repository: str,
        remote: str = "origin",
        remote_branch: str = "main",
    ) -> None:
        self.repository_root = Path(repository_root).absolute()
        self.state_store = state_store
        self.repository = repository
        self.remote = remote
        self.remote_branch = remote_branch
        RemotePin(repository, remote, remote_branch, "0" * 40)
        worktrees_path = Path(worktrees_root).absolute()
        repo_resolved = self.repository_root.resolve(strict=False)
        state_resolved = state_store.root.resolve(strict=False)
        worktrees_resolved = worktrees_path.resolve(strict=False)
        if state_resolved == repo_resolved or state_resolved.is_relative_to(
            repo_resolved
        ):
            raise UnsafeStatePathError(
                "state directory must live outside the repository"
            )
        if worktrees_resolved == repo_resolved or worktrees_resolved.is_relative_to(
            repo_resolved
        ):
            raise UnsafeStatePathError(
                "managed worktrees must live outside the repository"
            )
        if worktrees_resolved == state_resolved or worktrees_resolved.is_relative_to(
            state_resolved
        ):
            raise UnsafeStatePathError("worktrees must not overlap mutable state")
        if state_resolved.is_relative_to(worktrees_resolved):
            raise UnsafeStatePathError(
                "mutable state must not overlap managed worktrees"
            )
        self.worktrees_root = ensure_private_directory(worktrees_path)

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        operation: str = "git operation",
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ("git", *args),
                cwd=cwd or self.repository_root,
                env=_safe_git_environment(),
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitOperationError(f"{operation} could not complete") from exc
        if check and result.returncode != 0:
            raise GitOperationError(f"{operation} failed")
        return result

    def _validate_repository_root(self) -> None:
        try:
            info = self.repository_root.lstat()
        except FileNotFoundError as exc:
            raise RepositoryRootError("repository root does not exist") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RepositoryRootError("repository root must be a real directory")
        bare = self._git(
            "rev-parse",
            "--is-bare-repository",
            operation="repository type check",
        ).stdout.strip()
        top = self._git(
            "rev-parse",
            "--show-toplevel",
            operation="repository root check",
        ).stdout.strip()
        if bare != "false" or Path(top).absolute() != self.repository_root:
            raise RepositoryRootError(
                "repository_root must be the exact non-bare checkout root"
            )

    def _require_clean(self, path: Path, label: str) -> None:
        result = self._git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            cwd=path,
            operation=f"{label} cleanliness check",
        )
        if result.stdout:
            raise DirtyRepositoryError(f"{label} is dirty; refusing to reuse it")

    def _observe_remote(self) -> str:
        result = self._git(
            "ls-remote",
            "--exit-code",
            "--refs",
            self.remote,
            f"refs/heads/{self.remote_branch}",
            check=False,
            operation="remote branch observation",
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        expected_ref = f"refs/heads/{self.remote_branch}"
        if result.returncode != 0 or len(lines) != 1:
            raise RemoteUnavailableError("remote branch is missing or ambiguous")
        fields = lines[0].split("\t")
        if (
            len(fields) != 2
            or fields[1] != expected_ref
            or not _SHA.fullmatch(fields[0])
        ):
            raise RemoteUnavailableError("remote branch response is not canonical")
        return fields[0]

    def _fetch_remote_branch(self) -> None:
        self._git(
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            self.remote,
            f"refs/heads/{self.remote_branch}",
            operation="remote branch fetch",
        )

    def _commit_exists(self, sha: str) -> bool:
        return (
            self._git(
                "cat-file",
                "-e",
                f"{sha}^{{commit}}",
                check=False,
                operation="commit existence check",
            ).returncode
            == 0
        )

    def pin_remote_main(self) -> RemotePin:
        self._validate_repository_root()
        self._require_clean(self.repository_root, "source checkout")
        first = self._observe_remote()
        self._fetch_remote_branch()
        second = self._observe_remote()
        if first != second or not self._commit_exists(first):
            raise RemoteUnavailableError(
                "remote branch moved while it was being pinned"
            )
        return RemotePin(
            repository=self.repository,
            remote=self.remote,
            branch=self.remote_branch,
            sha=first,
        )

    def preflight(self) -> PreflightResult:
        with self.state_store.lock(self.repository):
            pin = self.pin_remote_main()
        return PreflightResult(
            repository_root=str(self.repository_root),
            repository=self.repository,
            pin=pin,
        )

    @staticmethod
    def branch_name(run_date: str, candidate_slug: str, base_sha: str) -> str:
        key = RunKey(run_date, "owner/repository", base_sha)
        if not _SLUG.fullmatch(candidate_slug):
            raise StateContractError("candidate slug is invalid")
        return f"codex/daily/{key.run_date}-{candidate_slug}-{key.base_sha[:8]}"

    def _worktree_path(self, key: RunKey, candidate_slug: str) -> Path:
        name = f"{key.run_date}-{candidate_slug}-{key.base_sha[:12]}"
        path = (self.worktrees_root / name).absolute()
        if path.parent != self.worktrees_root:
            raise UnsafeStatePathError("derived worktree path escaped its root")
        return path

    def _branch_snapshot(
        self, branch_name: str, base_sha: str
    ) -> tuple[BranchDisposition, str | None]:
        result = self._git(
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch_name}^{{commit}}",
            check=False,
            operation="daily branch inspection",
        )
        if result.returncode == 1:
            return BranchDisposition.ABSENT, None
        if result.returncode != 0:
            raise GitOperationError("daily branch inspection failed")
        head = result.stdout.strip()
        if not _SHA.fullmatch(head):
            raise GitOperationError("daily branch inspection returned an invalid SHA")
        if head == base_sha:
            return BranchDisposition.EXISTING_AT_BASE, head
        ancestor = self._git(
            "merge-base",
            "--is-ancestor",
            base_sha,
            head,
            check=False,
            operation="daily branch ancestry check",
        )
        if ancestor.returncode == 0:
            return BranchDisposition.EXISTING_ADVANCED, head
        if ancestor.returncode == 1:
            return BranchDisposition.EXISTING_DIVERGED, head
        raise GitOperationError("daily branch ancestry check failed")

    def _registered_worktrees(self) -> frozenset[Path]:
        result = self._git(
            "worktree",
            "list",
            "--porcelain",
            operation="worktree registry inspection",
        )
        paths: set[Path] = set()
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                paths.add(Path(line.removeprefix("worktree ")).absolute())
        return frozenset(paths)

    def _validate_ready_worktree(self, state: RunState) -> None:
        if state.worktree_path is None:
            raise StateContractError("ready state omitted its worktree path")
        path = Path(state.worktree_path)
        if path != self._worktree_path(state.key, self._slug_from_state(state)):
            raise UnsafeStatePathError(
                "state worktree path is not deterministically derived"
            )
        if path not in self._registered_worktrees():
            raise WorktreeError("state worktree is not registered with Git")
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise WorktreeError("state worktree is missing") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise UnsafeStatePathError("managed worktree must be owner-only directory")
        branch = self._git(
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            cwd=path,
            operation="worktree branch check",
        ).stdout.strip()
        head = self._git(
            "rev-parse", "HEAD", cwd=path, operation="worktree HEAD check"
        ).stdout.strip()
        if branch != state.branch_name or head != state.branch_head_sha:
            raise WorktreeError(
                "managed worktree branch or HEAD no longer matches state"
            )
        self._require_clean(path, "managed worktree")

    @staticmethod
    def _slug_from_state(state: RunState) -> str:
        prefix = f"codex/daily/{state.key.run_date}-"
        suffix = f"-{state.key.base_sha[:8]}"
        if not state.branch_name.startswith(prefix) or not state.branch_name.endswith(
            suffix
        ):
            raise StateContractError("state branch is not bound to date/base SHA")
        slug = state.branch_name[len(prefix) : -len(suffix)]
        if not _SLUG.fullmatch(slug):
            raise StateContractError("state branch candidate slug is invalid")
        return slug

    def _movement_state(
        self,
        *,
        key: RunKey,
        branch_name: str,
        worktree_path: Path,
        current_sha: str,
        publication: PublicationSnapshot,
        existing: RunState | None = None,
    ) -> RunState:
        if existing is not None:
            state = replace(
                existing,
                lifecycle=Lifecycle.REMOTE_MOVED,
                observed_remote_sha=current_sha,
            )
        else:
            disposition, head = self._branch_snapshot(branch_name, key.base_sha)
            state = RunState(
                key=key,
                remote=self.remote,
                remote_branch=self.remote_branch,
                branch_name=branch_name,
                lifecycle=Lifecycle.REMOTE_MOVED,
                branch_disposition=disposition,
                branch_head_sha=head,
                worktree_path=(
                    str(worktree_path)
                    if worktree_path in self._registered_worktrees()
                    else None
                ),
                observed_remote_sha=current_sha,
                publication=publication,
            )
        self.state_store.save(state)
        return state

    def _assert_pin_identity(self, pin: RemotePin) -> None:
        if (
            pin.repository != self.repository
            or pin.remote != self.remote
            or pin.branch != self.remote_branch
        ):
            raise StateContractError("expected pin does not belong to this manager")

    def _check_current_pin(
        self,
        pin: RemotePin,
        *,
        key: RunKey,
        branch_name: str,
        worktree_path: Path,
        publication: PublicationSnapshot,
        existing: RunState | None = None,
    ) -> None:
        current = self._observe_remote()
        if current != pin.sha:
            moved = self._movement_state(
                key=key,
                branch_name=branch_name,
                worktree_path=worktree_path,
                current_sha=current,
                publication=publication,
                existing=existing,
            )
            raise RemoteMainMovedError(moved, current)

    def _state_for_existing_branch(
        self,
        *,
        key: RunKey,
        branch_name: str,
        worktree_path: Path,
        publication: PublicationSnapshot,
    ) -> RunState:
        disposition, head = self._branch_snapshot(branch_name, key.base_sha)
        if disposition is BranchDisposition.ABSENT:
            raise StateContractError("existing branch state requires a local branch")
        state = RunState(
            key=key,
            remote=self.remote,
            remote_branch=self.remote_branch,
            branch_name=branch_name,
            lifecycle=Lifecycle.EXISTING_BRANCH,
            branch_disposition=disposition,
            branch_head_sha=head,
            worktree_path=(
                str(worktree_path)
                if worktree_path in self._registered_worktrees()
                else None
            ),
            observed_remote_sha=key.base_sha,
            publication=publication,
        )
        self.state_store.save(state)
        return state

    def _resume_pinned(
        self,
        state: RunState,
        *,
        publication: PublicationSnapshot,
    ) -> RunState | None:
        path = Path(state.worktree_path) if state.worktree_path else None
        registered = self._registered_worktrees()
        disposition, head = self._branch_snapshot(state.branch_name, state.key.base_sha)
        if path is not None and path in registered:
            if disposition is not BranchDisposition.EXISTING_AT_BASE:
                raise ExistingDailyBranchError(
                    self._state_for_existing_branch(
                        key=state.key,
                        branch_name=state.branch_name,
                        worktree_path=path,
                        publication=publication,
                    )
                )
            recovered = replace(
                state,
                lifecycle=Lifecycle.WORKTREE_READY,
                branch_disposition=BranchDisposition.CREATED,
                branch_head_sha=head,
                publication=publication,
            )
            self._validate_ready_worktree(recovered)
            self.state_store.save(recovered)
            return recovered
        if path is not None and path.exists():
            raise UnsafeStatePathError(
                "unregistered path occupies managed worktree target"
            )
        if disposition is not BranchDisposition.ABSENT:
            blocked = self._state_for_existing_branch(
                key=state.key,
                branch_name=state.branch_name,
                worktree_path=path or self.worktrees_root,
                publication=publication,
            )
            raise ExistingDailyBranchError(blocked)
        return None

    def prepare(
        self,
        *,
        run_date: str,
        candidate_slug: str,
        expected_pin: RemotePin | None = None,
        publication: PublicationSnapshot | None = None,
    ) -> PrepareResult:
        publication = publication or PublicationSnapshot()
        publication.validate_repository(self.repository)
        with self.state_store.lock(self.repository) as lease:
            self._validate_repository_root()
            self._require_clean(self.repository_root, "source checkout")
            if expected_pin is None:
                pin = self.pin_remote_main()
            else:
                self._assert_pin_identity(expected_pin)
                pin = expected_pin
                current = self._observe_remote()
                key = RunKey(run_date, self.repository, pin.sha)
                branch_name = self.branch_name(run_date, candidate_slug, pin.sha)
                worktree_path = self._worktree_path(key, candidate_slug)
                if current != pin.sha:
                    moved = self._movement_state(
                        key=key,
                        branch_name=branch_name,
                        worktree_path=worktree_path,
                        current_sha=current,
                        publication=publication,
                    )
                    raise RemoteMainMovedError(moved, current)
                self._fetch_remote_branch()
                if not self._commit_exists(pin.sha):
                    raise RemoteUnavailableError(
                        "pinned remote commit is unavailable locally"
                    )

            key = RunKey(run_date, self.repository, pin.sha)
            branch_name = self.branch_name(run_date, candidate_slug, pin.sha)
            worktree_path = self._worktree_path(key, candidate_slug)
            existing = self.state_store.load(key)
            if existing is not None:
                if existing.branch_name != branch_name:
                    raise StateContractError(
                        "idempotency key is already bound to a different candidate slug"
                    )
                if publication != PublicationSnapshot():
                    if existing.publication != PublicationSnapshot() and (
                        existing.publication != publication
                    ):
                        raise StateContractError(
                            "publication snapshot changed incompatibly"
                        )
                    existing = replace(existing, publication=publication)
                    self.state_store.save(existing)
                else:
                    publication = existing.publication
                self._check_current_pin(
                    pin,
                    key=key,
                    branch_name=branch_name,
                    worktree_path=worktree_path,
                    publication=publication,
                    existing=existing,
                )
                if existing.lifecycle is Lifecycle.WORKTREE_READY:
                    self._validate_ready_worktree(existing)
                    return PrepareResult(existing, True, lease.recovered_stale)
                if existing.lifecycle is Lifecycle.PINNED:
                    resumed = self._resume_pinned(existing, publication=publication)
                    if resumed is not None:
                        return PrepareResult(resumed, True, lease.recovered_stale)
                elif existing.lifecycle is Lifecycle.REMOTE_MOVED:
                    raise RemoteMainMovedError(
                        existing, existing.observed_remote_sha or pin.sha
                    )
                elif existing.lifecycle is Lifecycle.EXISTING_PUBLICATION:
                    raise ExistingPublicationError(existing)
                elif existing.lifecycle is Lifecycle.EXISTING_BRANCH:
                    raise ExistingDailyBranchError(existing)

            if publication.state not in {
                PullRequestState.UNKNOWN,
                PullRequestState.NONE,
            }:
                blocked = RunState(
                    key=key,
                    remote=self.remote,
                    remote_branch=self.remote_branch,
                    branch_name=branch_name,
                    lifecycle=Lifecycle.EXISTING_PUBLICATION,
                    branch_disposition=BranchDisposition.ABSENT,
                    branch_head_sha=None,
                    worktree_path=None,
                    observed_remote_sha=key.base_sha,
                    publication=publication,
                )
                self.state_store.save(blocked)
                raise ExistingPublicationError(blocked)

            disposition, _ = self._branch_snapshot(branch_name, key.base_sha)
            if disposition is not BranchDisposition.ABSENT:
                blocked = self._state_for_existing_branch(
                    key=key,
                    branch_name=branch_name,
                    worktree_path=worktree_path,
                    publication=publication,
                )
                raise ExistingDailyBranchError(blocked)
            if worktree_path.exists() or worktree_path in self._registered_worktrees():
                raise UnsafeStatePathError(
                    "managed worktree target is already occupied"
                )

            pinned = RunState(
                key=key,
                remote=self.remote,
                remote_branch=self.remote_branch,
                branch_name=branch_name,
                lifecycle=Lifecycle.PINNED,
                branch_disposition=BranchDisposition.ABSENT,
                branch_head_sha=None,
                worktree_path=str(worktree_path),
                observed_remote_sha=key.base_sha,
                publication=publication,
            )
            self.state_store.save(pinned)
            self._check_current_pin(
                pin,
                key=key,
                branch_name=branch_name,
                worktree_path=worktree_path,
                publication=publication,
                existing=pinned,
            )
            self._git(
                "worktree",
                "add",
                "--no-track",
                "-b",
                branch_name,
                str(worktree_path),
                key.base_sha,
                operation="isolated worktree creation",
            )
            os.chmod(worktree_path, 0o700, follow_symlinks=False)
            ready = replace(
                pinned,
                lifecycle=Lifecycle.WORKTREE_READY,
                branch_disposition=BranchDisposition.CREATED,
                branch_head_sha=key.base_sha,
            )
            self._validate_ready_worktree(ready)
            self.state_store.save(ready)
            self._check_current_pin(
                pin,
                key=key,
                branch_name=branch_name,
                worktree_path=worktree_path,
                publication=publication,
                existing=ready,
            )
            return PrepareResult(ready, False, lease.recovered_stale)

    def assert_remote_unchanged(self, state: RunState) -> None:
        """Recheck the pinned base without mutating or removing an existing run."""

        with self.state_store.lock(self.repository):
            if state.key.repository != self.repository:
                raise StateContractError("run state belongs to a different repository")
            pin = RemotePin(
                repository=self.repository,
                remote=self.remote,
                branch=self.remote_branch,
                sha=state.key.base_sha,
            )
            self._check_current_pin(
                pin,
                key=state.key,
                branch_name=state.branch_name,
                worktree_path=(
                    Path(state.worktree_path)
                    if state.worktree_path
                    else self._worktree_path(state.key, self._slug_from_state(state))
                ),
                publication=state.publication,
                existing=state,
            )
