from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Collection, Mapping
from zoneinfo import ZoneInfo

from .naming import is_v1_candidate_slug, v1_daily_branch_name


STATE_SCHEMA_VERSION = "vista.world.daily-maintainer.state.v1"
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_ID = re.compile(r"^VW-DM-[0-9]{4,}$")
_PR_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)$"
)
_MAX_STATE_BYTES = 64 * 1024
_MAX_WORKTREE_PATH_BYTES = 4096
_MAX_PR_URL_BYTES = 512
_MAX_PR_NUMBER = 2**63 - 1


class StateError(RuntimeError):
    """Base class for fail-closed local state errors."""


class StateContractError(StateError, ValueError):
    """Persisted run state does not satisfy the strict local contract."""


class UnsafeStatePathError(StateError):
    """A local state path is unsafe, shared, or unexpectedly permissive."""


class ConcurrentRunError(StateError):
    """The kernel reports another run holding the repository singleton lock."""


class Lifecycle(str, Enum):
    PINNED = "pinned"
    WORKTREE_READY = "worktree_ready"
    REMOTE_MOVED = "remote_moved"
    EXISTING_BRANCH = "existing_branch"
    EXISTING_PUBLICATION = "existing_publication"


class BranchDisposition(str, Enum):
    ABSENT = "absent"
    CREATED = "created"
    EXISTING_AT_BASE = "existing_at_base"
    EXISTING_ADVANCED = "existing_advanced"
    EXISTING_DIVERGED = "existing_diverged"


class PullRequestState(str, Enum):
    UNKNOWN = "unknown"
    NONE = "none"
    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class DueReason(str, Enum):
    SCHEDULED = "scheduled"
    CATCH_UP = "catch_up"
    ALREADY_ATTEMPTED = "already_attempted"


def _canonical_date(value: dt.date | str) -> str:
    if isinstance(value, dt.datetime):
        raise StateContractError("run date must be a date, not a timestamp")
    if isinstance(value, dt.date):
        return value.isoformat()
    if not isinstance(value, str):
        raise StateContractError("run date must be YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise StateContractError("run date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise StateContractError("run date must use canonical YYYY-MM-DD")
    return value


def _require_repository(value: object) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise StateContractError("repository must be an exact owner/name")
    return value


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise StateContractError(f"{label} must be an exact lowercase object ID")
    return value


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise StateContractError(f"{label} must be lowercase SHA-256")
    return value


def validate_run_authority(
    candidate_id: object,
    candidate_slug: object,
    backlog_sha256: object,
    candidate_sha256: object,
) -> None:
    """Validate the immutable candidate authority stored for one run."""

    if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id):
        raise StateContractError("candidate ID is invalid")
    if not is_v1_candidate_slug(candidate_slug):
        raise StateContractError("candidate slug is invalid")
    _require_sha256(backlog_sha256, "backlog digest")
    _require_sha256(candidate_sha256, "candidate digest")


def _require_text(
    value: object,
    label: str,
    *,
    maximum_bytes: int,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise StateContractError(f"{label} must be text")
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeEncodeError as exc:
        raise StateContractError(f"{label} must be valid Unicode text") from exc
    if not value or size > maximum_bytes or "\0" in value:
        raise StateContractError(f"{label} is outside its allowed range")
    return value


def _require_optional_sha(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_sha(value, label)


def _require_optional_pr_number(value: object) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_PR_NUMBER
    ):
        raise StateContractError("pull request number is outside its allowed range")
    return value


@dataclass(frozen=True)
class RunKey:
    run_date: str
    repository: str
    base_sha: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_date", _canonical_date(self.run_date))
        object.__setattr__(self, "repository", _require_repository(self.repository))
        object.__setattr__(self, "base_sha", _require_sha(self.base_sha, "base SHA"))

    @property
    def run_id(self) -> str:
        return f"{self.run_date}/{self.repository}@{self.base_sha}"


@dataclass(frozen=True)
class PublicationSnapshot:
    state: PullRequestState = PullRequestState.UNKNOWN
    number: int | None = None
    url: str | None = None
    head_sha: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, (str, PullRequestState)):
            raise StateContractError("pull request state is invalid")
        try:
            state_value = PullRequestState(self.state)
        except (TypeError, ValueError) as exc:
            raise StateContractError("pull request state is invalid") from exc
        object.__setattr__(self, "state", state_value)
        _require_optional_pr_number(self.number)
        if self.head_sha is not None:
            _require_sha(self.head_sha, "pull request head SHA")
        if state_value in {PullRequestState.UNKNOWN, PullRequestState.NONE}:
            if (
                self.number is not None
                or self.url is not None
                or self.head_sha is not None
            ):
                raise StateContractError(
                    "unknown/none pull request state cannot claim publication data"
                )
            return
        if self.number is None or self.url is None:
            raise StateContractError(
                "known pull request state requires number and canonical URL"
            )
        url = _require_text(
            self.url,
            "pull request URL",
            maximum_bytes=_MAX_PR_URL_BYTES,
        )
        match = _PR_URL.fullmatch(url)
        if not match or int(match.group(2)) != self.number:
            raise StateContractError("pull request URL does not match its number")

    def validate_repository(self, repository: str) -> None:
        if self.url is None:
            return
        match = _PR_URL.fullmatch(self.url) if isinstance(self.url, str) else None
        if not match or match.group(1) != repository:
            raise StateContractError("pull request URL does not match repository")


@dataclass(frozen=True)
class RunState:
    key: RunKey
    candidate_id: str
    candidate_slug: str
    backlog_sha256: str
    candidate_sha256: str
    remote: str
    remote_branch: str
    branch_name: str
    lifecycle: Lifecycle
    branch_disposition: BranchDisposition
    branch_head_sha: str | None
    worktree_path: str | None
    observed_remote_sha: str | None
    publication: PublicationSnapshot = PublicationSnapshot()
    schema_version: str = STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.key, RunKey):
            raise StateContractError("run key is invalid")
        validate_run_authority(
            self.candidate_id,
            self.candidate_slug,
            self.backlog_sha256,
            self.candidate_sha256,
        )
        if (
            not isinstance(self.schema_version, str)
            or self.schema_version != STATE_SCHEMA_VERSION
        ):
            raise StateContractError("unsupported run state schema version")
        if not isinstance(self.remote, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.remote
        ):
            raise StateContractError("remote name is invalid")
        if not isinstance(self.remote_branch, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", self.remote_branch
        ):
            raise StateContractError("remote branch is invalid")
        if any(
            part in {"", ".", ".."} for part in self.remote_branch.split("/")
        ) or not isinstance(self.branch_name, str):
            raise StateContractError("daily branch name is invalid")
        expected_branch = v1_daily_branch_name(
            self.key.run_date,
            self.candidate_slug,
            self.key.base_sha,
        )
        if self.branch_name != expected_branch:
            raise StateContractError(
                "daily branch is not bound to run date/candidate slug/base SHA"
            )
        if not isinstance(self.lifecycle, (str, Lifecycle)) or not isinstance(
            self.branch_disposition, (str, BranchDisposition)
        ):
            raise StateContractError("run lifecycle or branch disposition is invalid")
        try:
            lifecycle = Lifecycle(self.lifecycle)
            disposition = BranchDisposition(self.branch_disposition)
        except (TypeError, ValueError) as exc:
            raise StateContractError(
                "run lifecycle or branch disposition is invalid"
            ) from exc
        object.__setattr__(self, "lifecycle", lifecycle)
        object.__setattr__(self, "branch_disposition", disposition)
        if self.branch_head_sha is not None:
            _require_sha(self.branch_head_sha, "branch head SHA")
        if self.observed_remote_sha is not None:
            _require_sha(self.observed_remote_sha, "observed remote SHA")
        if self.worktree_path is not None:
            worktree_path = _require_text(
                self.worktree_path,
                "worktree path",
                maximum_bytes=_MAX_WORKTREE_PATH_BYTES,
            )
            assert worktree_path is not None
            if worktree_path.startswith("//"):
                raise StateContractError(
                    "worktree path must be absolute and normalized"
                )
            path = Path(self.worktree_path)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or str(path) != worktree_path
            ):
                raise StateContractError(
                    "worktree path must be absolute and normalized"
                )
        if not isinstance(self.publication, PublicationSnapshot):
            raise StateContractError("publication snapshot is invalid")
        self.publication.validate_repository(self.key.repository)
        if disposition is BranchDisposition.ABSENT and self.branch_head_sha is not None:
            raise StateContractError("absent branch cannot claim a head SHA")
        if disposition is not BranchDisposition.ABSENT and self.branch_head_sha is None:
            raise StateContractError("present branch requires a head SHA")
        if lifecycle is Lifecycle.WORKTREE_READY:
            if disposition is not BranchDisposition.CREATED or not self.worktree_path:
                raise StateContractError(
                    "ready lifecycle requires a created branch and worktree"
                )
        if (
            lifecycle is Lifecycle.PINNED
            and disposition is not BranchDisposition.ABSENT
        ):
            raise StateContractError("pinned lifecycle cannot claim an existing branch")
        if lifecycle is Lifecycle.EXISTING_BRANCH and disposition in {
            BranchDisposition.ABSENT,
            BranchDisposition.CREATED,
        }:
            raise StateContractError(
                "existing_branch lifecycle requires an observed pre-existing branch"
            )
        if lifecycle is Lifecycle.REMOTE_MOVED:
            if (
                not self.observed_remote_sha
                or self.observed_remote_sha == self.key.base_sha
            ):
                raise StateContractError(
                    "remote_moved lifecycle requires a different observed SHA"
                )
        elif self.observed_remote_sha not in {None, self.key.base_sha}:
            raise StateContractError(
                "non-movement lifecycle cannot claim a different remote SHA"
            )


@dataclass(frozen=True)
class LockLease:
    repository: str
    path: Path
    recovered_stale: bool


@dataclass(frozen=True)
class DuePeriod:
    run_date: str | None
    reason: DueReason
    scheduled_for: str


def choose_due_period(
    now: dt.datetime,
    *,
    attempted_dates: Collection[str | dt.date] = (),
    timezone: str = "Asia/Taipei",
    hour: int = 9,
    minute: int = 17,
) -> DuePeriod:
    """Return only the most recent due period, never a multi-day backfill list."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise StateContractError("schedule time must be timezone-aware")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise StateContractError("schedule hour/minute are invalid")
    try:
        zone = ZoneInfo(timezone)
    except Exception as exc:  # pragma: no cover - platform tzdata failure
        raise StateContractError("schedule timezone is unavailable") from exc
    local_now = now.astimezone(zone)
    today_due = dt.datetime.combine(
        local_now.date(), dt.time(hour, minute), tzinfo=zone
    )
    scheduled = (
        today_due if local_now >= today_due else today_due - dt.timedelta(days=1)
    )
    run_date = scheduled.date().isoformat()
    attempted = {_canonical_date(value) for value in attempted_dates}
    if run_date in attempted:
        return DuePeriod(
            run_date=None,
            reason=DueReason.ALREADY_ATTEMPTED,
            scheduled_for=scheduled.isoformat(),
        )
    reason = DueReason.SCHEDULED if local_now == scheduled else DueReason.CATCH_UP
    return DuePeriod(
        run_date=run_date,
        reason=reason,
        scheduled_for=scheduled.isoformat(),
    )


def publication_to_dict(snapshot: PublicationSnapshot) -> dict[str, object]:
    return {
        "state": snapshot.state.value,
        "number": snapshot.number,
        "url": snapshot.url,
        "head_sha": snapshot.head_sha,
    }


def state_to_dict(state: RunState) -> dict[str, object]:
    return {
        "schema_version": state.schema_version,
        "run_id": state.key.run_id,
        "run_date": state.key.run_date,
        "repository": state.key.repository,
        "base_sha": state.key.base_sha,
        "candidate_id": state.candidate_id,
        "candidate_slug": state.candidate_slug,
        "backlog_sha256": state.backlog_sha256,
        "candidate_sha256": state.candidate_sha256,
        "remote": state.remote,
        "remote_branch": state.remote_branch,
        "branch_name": state.branch_name,
        "lifecycle": state.lifecycle.value,
        "branch_disposition": state.branch_disposition.value,
        "branch_head_sha": state.branch_head_sha,
        "worktree_path": state.worktree_path,
        "observed_remote_sha": state.observed_remote_sha,
        "publication": publication_to_dict(state.publication),
    }


def serialize_state(state: RunState) -> bytes:
    return (
        json.dumps(state_to_dict(state), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def state_digest(state: RunState) -> str:
    """Digest the exact canonical state accepted by the run-state parser."""

    return hashlib.sha256(serialize_state(state)).hexdigest()


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise StateContractError(f"non-finite JSON number is forbidden: {value}")


def _expect_fields(
    mapping: Mapping[str, object], *, required: set[str], label: str
) -> None:
    missing = sorted(required - set(mapping))
    unknown = sorted(set(mapping) - required)
    if missing:
        raise StateContractError(f"{label} missing fields: {', '.join(missing)}")
    if unknown:
        raise StateContractError(f"{label} unknown fields: {', '.join(unknown)}")


def parse_state(payload: bytes | str) -> RunState:
    try:
        if isinstance(payload, bytes):
            payload_size = len(payload)
        elif isinstance(payload, str):
            payload_size = len(payload.encode("utf-8", "strict"))
        else:
            raise StateContractError("state must be bytes or text")
    except UnicodeEncodeError as exc:
        raise StateContractError("state must be valid Unicode text") from exc
    if payload_size > _MAX_STATE_BYTES:
        raise StateContractError("state is oversized")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_strict_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except StateContractError:
        raise
    except (
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise StateContractError("state is not valid strict JSON") from exc
    if not isinstance(value, Mapping):
        raise StateContractError("state must be a JSON object")
    _expect_fields(
        value,
        required={
            "schema_version",
            "run_id",
            "run_date",
            "repository",
            "base_sha",
            "candidate_id",
            "candidate_slug",
            "backlog_sha256",
            "candidate_sha256",
            "remote",
            "remote_branch",
            "branch_name",
            "lifecycle",
            "branch_disposition",
            "branch_head_sha",
            "worktree_path",
            "observed_remote_sha",
            "publication",
        },
        label="state",
    )
    publication = value["publication"]
    if not isinstance(publication, Mapping):
        raise StateContractError("publication must be an object")
    _expect_fields(
        publication,
        required={"state", "number", "url", "head_sha"},
        label="publication",
    )
    try:
        schema_version = _require_text(
            value["schema_version"], "schema version", maximum_bytes=128
        )
        run_id = _require_text(value["run_id"], "run_id", maximum_bytes=512)
        run_date = _require_text(value["run_date"], "run date", maximum_bytes=10)
        repository = _require_text(value["repository"], "repository", maximum_bytes=201)
        base_sha = _require_sha(value["base_sha"], "base SHA")
        candidate_id = _require_text(
            value["candidate_id"], "candidate ID", maximum_bytes=64
        )
        candidate_slug = _require_text(
            value["candidate_slug"], "candidate slug", maximum_bytes=48
        )
        backlog_sha256 = _require_sha256(value["backlog_sha256"], "backlog digest")
        candidate_sha256 = _require_sha256(
            value["candidate_sha256"], "candidate digest"
        )
        remote = _require_text(value["remote"], "remote", maximum_bytes=64)
        remote_branch = _require_text(
            value["remote_branch"], "remote branch", maximum_bytes=128
        )
        branch_name = _require_text(
            value["branch_name"], "daily branch name", maximum_bytes=128
        )
        lifecycle = _require_text(value["lifecycle"], "run lifecycle", maximum_bytes=32)
        branch_disposition = _require_text(
            value["branch_disposition"],
            "branch disposition",
            maximum_bytes=32,
        )
        branch_head_sha = _require_optional_sha(
            value["branch_head_sha"], "branch head SHA"
        )
        worktree_path = _require_text(
            value["worktree_path"],
            "worktree path",
            maximum_bytes=_MAX_WORKTREE_PATH_BYTES,
            nullable=True,
        )
        observed_remote_sha = _require_optional_sha(
            value["observed_remote_sha"], "observed remote SHA"
        )
        publication_state = _require_text(
            publication["state"], "pull request state", maximum_bytes=16
        )
        publication_number = _require_optional_pr_number(publication["number"])
        publication_url = _require_text(
            publication["url"],
            "pull request URL",
            maximum_bytes=_MAX_PR_URL_BYTES,
            nullable=True,
        )
        publication_head_sha = _require_optional_sha(
            publication["head_sha"], "pull request head SHA"
        )
        key = RunKey(
            run_date=run_date,
            repository=repository,
            base_sha=base_sha,
        )
        if run_id != key.run_id:
            raise StateContractError("run_id is not bound to date/repository/base SHA")
        return RunState(
            key=key,
            candidate_id=candidate_id,
            candidate_slug=candidate_slug,
            backlog_sha256=backlog_sha256,
            candidate_sha256=candidate_sha256,
            remote=remote,
            remote_branch=remote_branch,
            branch_name=branch_name,
            lifecycle=lifecycle,
            branch_disposition=branch_disposition,
            branch_head_sha=branch_head_sha,
            worktree_path=worktree_path,
            observed_remote_sha=observed_remote_sha,
            publication=PublicationSnapshot(
                state=publication_state,
                number=publication_number,
                url=publication_url,
                head_sha=publication_head_sha,
            ),
            schema_version=schema_version,
        )
    except StateContractError:
        raise
    except (OverflowError, TypeError, ValueError) as exc:
        raise StateContractError("state violates the strict contract") from exc


def _assert_no_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeStatePathError(f"managed path contains symlink: {current}")


def ensure_private_directory(path: Path) -> Path:
    path = path.absolute()
    _assert_no_symlink_components(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise UnsafeStatePathError(f"managed path is not a real directory: {path}")
    if info.st_uid != os.getuid():
        raise UnsafeStatePathError(f"managed directory has a different owner: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise UnsafeStatePathError(f"managed directory must have mode 0700: {path}")
    return path


class _RepositoryLock:
    def __init__(
        self,
        path: Path,
        repository: str,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        self.path = path
        self.repository = repository
        self.now = now
        self._fd: int | None = None
        self._lease: LockLease | None = None

    def __enter__(self) -> LockLease:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise UnsafeStatePathError("unable to open repository lock safely") from exc
        self._fd = fd
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            os.close(fd)
            self._fd = None
            raise UnsafeStatePathError(
                "repository lock must be owner-only regular file"
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            self._fd = None
            raise ConcurrentRunError(
                "another run holds the kernel lock; metadata age cannot bypass it"
            ) from exc
        old_payload = os.pread(fd, _MAX_STATE_BYTES + 1, 0)
        if len(old_payload) > _MAX_STATE_BYTES:
            self._unlock_and_close(clear=False)
            raise UnsafeStatePathError("repository lock metadata is oversized")
        recovered_stale = bool(old_payload.strip())
        acquired = self.now or dt.datetime.now(dt.timezone.utc)
        if acquired.tzinfo is None or acquired.utcoffset() is None:
            self._unlock_and_close(clear=False)
            raise StateContractError("lock timestamp must be timezone-aware")
        metadata = {
            "acquired_at": acquired.astimezone(dt.timezone.utc).isoformat(),
            "pid": os.getpid(),
            "repository": self.repository,
        }
        payload = (
            json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        os.ftruncate(fd, 0)
        os.pwrite(fd, payload, 0)
        os.fsync(fd)
        self._lease = LockLease(
            repository=self.repository,
            path=self.path,
            recovered_stale=recovered_stale,
        )
        return self._lease

    def _unlock_and_close(self, *, clear: bool) -> None:
        if self._fd is None:
            return
        fd = self._fd
        try:
            if clear:
                os.ftruncate(fd, 0)
                os.fsync(fd)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            self._fd = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._unlock_and_close(clear=True)


class RunStateStore:
    """Owner-only, atomic state storage outside the target repository."""

    def __init__(self, root: Path | str) -> None:
        self.root = ensure_private_directory(Path(root))
        self.runs_root = ensure_private_directory(self.root / "runs")
        self.locks_root = ensure_private_directory(self.root / "locks")

    @staticmethod
    def _repo_digest(repository: str) -> str:
        _require_repository(repository)
        return hashlib.sha256(repository.encode("utf-8")).hexdigest()

    def lock_path(self, repository: str) -> Path:
        return self.locks_root / f"{self._repo_digest(repository)}.lock"

    def lock(
        self, repository: str, *, now: dt.datetime | None = None
    ) -> _RepositoryLock:
        repository = _require_repository(repository)
        return _RepositoryLock(self.lock_path(repository), repository, now=now)

    def _state_directory(self, key: RunKey) -> Path:
        repo_dir = ensure_private_directory(
            self.runs_root / self._repo_digest(key.repository)
        )
        return ensure_private_directory(repo_dir / key.run_date)

    def path_for(self, key: RunKey) -> Path:
        return self._state_directory(key) / f"{key.base_sha}.json"

    @staticmethod
    def _read_private(path: Path) -> bytes:
        _assert_no_symlink_components(path)
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise UnsafeStatePathError("state file could not be opened safely") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise UnsafeStatePathError("state file must be owner-only regular file")
            if info.st_size > _MAX_STATE_BYTES:
                raise StateContractError("state file is oversized")
            payload = os.pread(fd, _MAX_STATE_BYTES + 1, 0)
            if len(payload) > _MAX_STATE_BYTES:
                raise StateContractError("state file is oversized")
            return payload
        finally:
            os.close(fd)

    def load(self, key: RunKey) -> RunState | None:
        path = self.path_for(key)
        try:
            payload = self._read_private(path)
        except FileNotFoundError:
            return None
        state = parse_state(payload)
        if state.key != key:
            raise StateContractError("state file key does not match its path")
        return state

    def save(self, state: RunState) -> Path:
        path = self.path_for(state.key)
        existing = self.load(state.key)
        if existing is not None:
            immutable_existing = (
                existing.key,
                existing.candidate_id,
                existing.candidate_slug,
                existing.backlog_sha256,
                existing.candidate_sha256,
                existing.remote,
                existing.remote_branch,
                existing.branch_name,
            )
            immutable_new = (
                state.key,
                state.candidate_id,
                state.candidate_slug,
                state.backlog_sha256,
                state.candidate_sha256,
                state.remote,
                state.remote_branch,
                state.branch_name,
            )
            if immutable_existing != immutable_new:
                raise StateContractError("state update changed immutable run identity")
            allowed_lifecycle_updates = {
                existing.lifecycle,
                Lifecycle.REMOTE_MOVED,
            }
            if existing.lifecycle is Lifecycle.PINNED:
                allowed_lifecycle_updates |= {
                    Lifecycle.WORKTREE_READY,
                    Lifecycle.EXISTING_BRANCH,
                    Lifecycle.EXISTING_PUBLICATION,
                }
            if state.lifecycle not in allowed_lifecycle_updates:
                raise StateContractError("run state lifecycle cannot move backward")
        payload = serialize_state(state)
        parent = ensure_private_directory(path.parent)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600, follow_symlinks=False)
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return path

    def states_for_date(
        self, repository: str, run_date: str | dt.date
    ) -> tuple[RunState, ...]:
        repository = _require_repository(repository)
        canonical_date = _canonical_date(run_date)
        directory = self.runs_root / self._repo_digest(repository) / canonical_date
        if not directory.exists():
            return ()
        ensure_private_directory(directory)
        states: list[RunState] = []
        for path in sorted(directory.glob("*.json")):
            payload = self._read_private(path)
            state = parse_state(payload)
            if (
                state.key.repository != repository
                or state.key.run_date != canonical_date
            ):
                raise StateContractError("state directory contains a mismatched run")
            if path.name != f"{state.key.base_sha}.json":
                raise StateContractError("state filename does not match base SHA")
            states.append(state)
        return tuple(states)

    def attempted_dates(self, repository: str) -> frozenset[str]:
        repository = _require_repository(repository)
        repo_dir = self.runs_root / self._repo_digest(repository)
        if not repo_dir.exists():
            return frozenset()
        ensure_private_directory(repo_dir)
        attempted: set[str] = set()
        for directory in sorted(repo_dir.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                raise UnsafeStatePathError("run state tree contains an unsafe entry")
            run_date = _canonical_date(directory.name)
            states = self.states_for_date(repository, run_date)
            if any(state.lifecycle is not Lifecycle.REMOTE_MOVED for state in states):
                attempted.add(run_date)
        return frozenset(attempted)

    def due_period(self, repository: str, now: dt.datetime) -> DuePeriod:
        return choose_due_period(
            now,
            attempted_dates=self.attempted_dates(repository),
        )
