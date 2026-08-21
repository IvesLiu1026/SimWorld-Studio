from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .naming import is_v1_candidate_slug, v1_daily_branch_name
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
_SYSTEM_EXECUTABLE_DIRS = (
    Path("/usr/local/sbin"),
    Path("/usr/local/bin"),
    Path("/usr/sbin"),
    Path("/usr/bin"),
    Path("/sbin"),
    Path("/bin"),
)
_SYSTEM_PATH = os.pathsep.join(str(path) for path in _SYSTEM_EXECUTABLE_DIRS)
_FIXED_GIT_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "extensions.worktreeConfig=false",
    "-c",
    "credential.helper=",
    "-c",
    "credential.interactive=never",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "http.sslVerify=true",
    "-c",
    "http.curloptResolve=",
    "-c",
    "http.extraHeader=",
    "-c",
    "http.followRedirects=initial",
    "-c",
    "http.proxy=",
    "-c",
    "http.https://github.com/.proxy=",
    "-c",
    "core.sshCommand=/bin/false",
    "-c",
    "ssh.variant=simple",
)

_V1_REQUIRED_LOCAL_CONFIG = frozenset(
    {
        "core.bare",
        "core.filemode",
        "core.logallrefupdates",
        "core.repositoryformatversion",
        "remote.origin.fetch",
        "remote.origin.url",
    }
)
_V1_BRANCH_CONFIG = re.compile(
    r"^branch\.([a-z0-9][a-z0-9._/-]{0,127})\.(merge|remote)$"
)
_MAX_LOCAL_CONFIG_BYTES = 64 * 1024


class WorktreeError(StateError):
    """Base class for isolated Git worktree lifecycle failures."""


class GitOperationError(WorktreeError):
    """A fixed Git operation failed without exposing raw subprocess output."""


class RepositoryRootError(WorktreeError):
    """The configured checkout is not the exact non-bare repository root."""


class RepositoryIdentityError(WorktreeError):
    """The configured Git remote is not the pinned repository transport."""


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


def _trusted_git_executable() -> Path:
    """Resolve Git without consulting the caller-controlled environment."""

    found = shutil.which("git", path=_SYSTEM_PATH)
    if found is None:
        raise GitOperationError("trusted Git executable is unavailable")
    try:
        executable = Path(found).resolve(strict=True)
        info = executable.stat()
    except OSError as exc:
        raise GitOperationError("trusted Git executable is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o022
        or not os.access(executable, os.X_OK)
    ):
        raise GitOperationError("trusted Git executable is invalid")
    for directory in (executable.parent, *executable.parents):
        directory_info = directory.stat()
        if directory_info.st_uid != 0 or directory_info.st_mode & 0o022:
            raise GitOperationError("trusted Git path has a writable ancestor")
    return executable


def _safe_git_environment() -> Mapping[str, str]:
    return {
        "PATH": _SYSTEM_PATH,
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GCM_INTERACTIVE": "Never",
    }


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate every non-detached descendant in the command's session."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_fixed_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    """Run a shell-free command and reap its process group on timeout."""

    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise GitOperationError(f"{operation} could not start") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            # A deliberately detached descendant can retain inherited pipe FDs.
            # The deployment service must additionally use KillMode=control-group;
            # close our readers so this function still fails boundedly.
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise GitOperationError(f"{operation} timed out") from exc
    return subprocess.CompletedProcess(
        argv,
        process.returncode,
        stdout,
        stderr,
    )


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
        expected_remote_url: str | None = None,
    ) -> None:
        self.repository_root = Path(repository_root).absolute()
        self.state_store = state_store
        self.repository = repository
        self.remote = remote
        self.remote_branch = remote_branch
        self._git_executable = _trusted_git_executable()
        RemotePin(repository, remote, remote_branch, "0" * 40)
        canonical_remote_url = f"https://github.com/{repository}.git"
        if self.remote != "origin":
            raise StateContractError("unattended remote must be named origin")
        if expected_remote_url is not None and (
            not isinstance(expected_remote_url, str)
            or expected_remote_url != canonical_remote_url
        ):
            raise StateContractError(
                "unattended remote URL must be the exact canonical GitHub HTTPS URL"
            )
        self.expected_remote_url = canonical_remote_url
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
        command_cwd = cwd or self.repository_root
        sensitive_operation = bool(args) and (
            args[0] in {"fetch", "status"}
            or (args[0] == "worktree" and len(args) > 1 and args[1] == "add")
        )
        if sensitive_operation:
            self._validate_effective_git_config(command_cwd)
        result = _run_fixed_command(
            (
                str(self._git_executable),
                *_FIXED_GIT_CONFIG,
                "-c",
                f"remote.{self.remote}.proxy=",
                *args,
            ),
            cwd=command_cwd,
            environment=_safe_git_environment(),
            timeout_seconds=60,
            operation=operation,
        )
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
        self._validate_remote_identity()

    def _transport_url(self) -> str:
        """Return the code-owned canonical transport target.

        Tests may override this method to exercise the lifecycle against an
        isolated local bare repository.  The production constructor itself
        accepts only the canonical GitHub HTTPS identity.
        """

        return self.expected_remote_url

    @staticmethod
    def _parse_local_config_records(payload: str) -> tuple[tuple[str, str], ...]:
        if len(payload.encode("utf-8", "strict")) > _MAX_LOCAL_CONFIG_BYTES:
            raise RepositoryIdentityError(
                "repository-local Git configuration is too large"
            )
        records: list[tuple[str, str]] = []
        for record in payload.split("\0"):
            if not record:
                continue
            name, separator, value = record.partition("\n")
            if not separator or not name or "\0" in value:
                raise RepositoryIdentityError(
                    "repository-local Git configuration is malformed"
                )
            records.append((name.casefold(), value))
        return tuple(records)

    def _validate_v1_config_record(self, name: str, value: str) -> None:
        exact_values: dict[str, frozenset[str]] = {
            "core.bare": frozenset({"false"}),
            "core.filemode": frozenset({"false", "true"}),
            "core.logallrefupdates": frozenset({"true"}),
            "core.repositoryformatversion": frozenset({"0", "1"}),
            "remote.origin.fetch": frozenset({"+refs/heads/*:refs/remotes/origin/*"}),
            "remote.origin.url": frozenset({self._transport_url()}),
        }
        if name == "extensions.worktreeconfig":
            raise RepositoryIdentityError(
                "extensions.worktreeConfig is forbidden for unattended runs"
            )
        if name in exact_values:
            if value not in exact_values[name]:
                if name == "remote.origin.url":
                    raise RepositoryIdentityError(
                        "repository remote URL does not match the pinned target"
                    )
                raise RepositoryIdentityError(
                    "repository-local Git configuration is outside the V1 allowlist"
                )
            return
        if name in {"user.email", "user.name"}:
            if not value or len(value.encode("utf-8", "strict")) > 256 or "\n" in value:
                raise RepositoryIdentityError(
                    "repository-local Git identity configuration is invalid"
                )
            return
        branch_match = _V1_BRANCH_CONFIG.fullmatch(name)
        if branch_match:
            branch, field = branch_match.groups()
            if any(part in {"", ".", ".."} for part in branch.split("/")):
                raise RepositoryIdentityError(
                    "repository-local branch configuration is invalid"
                )
            if field == "remote" and value == "origin":
                return
            if field == "merge" and value.startswith("refs/heads/"):
                merge_branch = value.removeprefix("refs/heads/")
                if _REMOTE_BRANCH.fullmatch(merge_branch) and not any(
                    part in {"", ".", ".."} for part in merge_branch.split("/")
                ):
                    return
            raise RepositoryIdentityError(
                "repository-local branch configuration is invalid"
            )
        raise RepositoryIdentityError(
            "repository-local Git configuration is outside the V1 allowlist"
        )

    def _reject_worktree_config(self, cwd: Path) -> None:
        result = self._git(
            "rev-parse",
            "--absolute-git-dir",
            cwd=cwd,
            check=False,
            operation="repository Git directory check",
        )
        git_dir = Path(result.stdout.strip()) if result.returncode == 0 else None
        if git_dir is None or not git_dir.is_absolute():
            raise RepositoryIdentityError("repository Git directory is invalid")
        worktree_config = git_dir / "config.worktree"
        try:
            worktree_config.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RepositoryIdentityError(
                "per-worktree Git configuration could not be audited"
            ) from exc
        raise RepositoryIdentityError(
            "per-worktree config.worktree is forbidden for unattended runs"
        )

    def _validate_effective_git_config(self, cwd: Path) -> None:
        self._reject_worktree_config(cwd)
        result = self._git(
            "config",
            "--local",
            "--null",
            "--list",
            cwd=cwd,
            check=False,
            operation="repository configuration allowlist check",
        )
        if result.returncode != 0:
            raise RepositoryIdentityError(
                "repository-local Git configuration could not be audited"
            )
        records = self._parse_local_config_records(result.stdout)
        names = [name for name, _ in records]
        if len(names) != len(set(names)):
            raise RepositoryIdentityError(
                "repository-local Git configuration contains duplicate keys"
            )
        for name, value in records:
            self._validate_v1_config_record(name, value)
        missing = sorted(_V1_REQUIRED_LOCAL_CONFIG - set(names))
        if missing:
            raise RepositoryIdentityError(
                "repository-local Git configuration is missing V1 keys"
            )

    def _validate_remote_identity(self) -> None:
        self._validate_effective_git_config(self.repository_root)
        result = self._git(
            "remote",
            "get-url",
            "--all",
            self.remote,
            check=False,
            operation="repository remote identity check",
        )
        urls = tuple(line for line in result.stdout.splitlines() if line)
        if result.returncode != 0 or urls != (self._transport_url(),):
            raise RepositoryIdentityError(
                "repository remote URL does not match the pinned target"
            )

    def _require_clean(self, path: Path, label: str) -> None:
        self._validate_effective_git_config(path)
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
        self._validate_remote_identity()
        result = self._git(
            "ls-remote",
            "--exit-code",
            "--refs",
            self._transport_url(),
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
        self._validate_remote_identity()
        self._git(
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            self._transport_url(),
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
        if not is_v1_candidate_slug(candidate_slug):
            raise StateContractError("candidate slug is invalid")
        return v1_daily_branch_name(
            key.run_date,
            candidate_slug,
            key.base_sha,
        )

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
        self._validate_effective_git_config(path)
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
        if not is_v1_candidate_slug(slug):
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
                if existing.publication.state not in {
                    PullRequestState.UNKNOWN,
                    PullRequestState.NONE,
                }:
                    publication = existing.publication
                elif publication != PublicationSnapshot():
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
                if publication.state not in {
                    PullRequestState.UNKNOWN,
                    PullRequestState.NONE,
                }:
                    raise ExistingPublicationError(existing)
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
            self._validate_effective_git_config(self.repository_root)
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
            if (
                state.key.repository != self.repository
                or state.remote != self.remote
                or state.remote_branch != self.remote_branch
            ):
                raise StateContractError("run state belongs to a different repository")
            self._validate_repository_root()
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
