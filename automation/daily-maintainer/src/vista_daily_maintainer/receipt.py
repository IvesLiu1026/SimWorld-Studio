from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


RECEIPT_SCHEMA_VERSION = "vista.world.daily-maintainer.receipt.v1"
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_ID = re.compile(r"^VW-DM-[0-9]{4,}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMAND_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_FAILURE_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_ACTOR_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")
_IDENTITY_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")


class ReceiptContractError(ValueError):
    """A run receipt failed the stable v1 contract."""


class RunStatus(str, Enum):
    SKIPPED = "skipped"
    NO_CHANGE = "no_change"
    PATCH_REJECTED = "patch_rejected"
    VALIDATION_FAILED = "validation_failed"
    PR_OPEN = "pr_open"
    MERGED = "merged"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"
    HALTED = "halted"


_FAILURE_STATUSES = frozenset(
    {
        RunStatus.PATCH_REJECTED,
        RunStatus.VALIDATION_FAILED,
        RunStatus.INFRASTRUCTURE_FAILED,
        RunStatus.HALTED,
    }
)
_ALLOWED_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.SKIPPED: frozenset(),
    RunStatus.NO_CHANGE: frozenset(),
    RunStatus.PATCH_REJECTED: frozenset(),
    RunStatus.VALIDATION_FAILED: frozenset(),
    RunStatus.PR_OPEN: frozenset(
        {RunStatus.MERGED, RunStatus.INFRASTRUCTURE_FAILED, RunStatus.HALTED}
    ),
    RunStatus.MERGED: frozenset(),
    RunStatus.INFRASTRUCTURE_FAILED: frozenset({RunStatus.PR_OPEN, RunStatus.HALTED}),
    RunStatus.HALTED: frozenset(),
}


@dataclass(frozen=True)
class ValidationReceipt:
    command_id: str
    exit_code: int
    output_sha256: str
    duration_ms: int
    timed_out: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not _COMMAND_ID.fullmatch(
            self.command_id
        ):
            raise ReceiptContractError("validation command_id is invalid")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise ReceiptContractError("validation exit_code must be an integer")
        if not isinstance(self.output_sha256, str) or not _SHA256.fullmatch(
            self.output_sha256
        ):
            raise ReceiptContractError("validation output digest must be SHA-256")
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ReceiptContractError(
                "validation duration_ms must be a non-negative integer"
            )
        if not isinstance(self.timed_out, bool):
            raise ReceiptContractError("validation timed_out must be a boolean")

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class DiffSummary:
    files_changed: int
    production_lines: int
    test_lines: int
    patch_sha256: str

    def __post_init__(self) -> None:
        for name in ("files_changed", "production_lines", "test_lines"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReceiptContractError(f"diff summary {name} must be non-negative")
        if self.files_changed < 1:
            raise ReceiptContractError(
                "diff summary must contain at least one changed file"
            )
        if not isinstance(self.patch_sha256, str) or not _SHA256.fullmatch(
            self.patch_sha256
        ):
            raise ReceiptContractError("diff patch digest must be SHA-256")


@dataclass(frozen=True)
class GitIdentity:
    name: str
    email: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not 1 <= len(self.name) <= 128
            or _IDENTITY_CONTROL.search(self.name)
        ):
            raise ReceiptContractError("Git identity name is invalid")
        if (
            not isinstance(self.email, str)
            or not 3 <= len(self.email) <= 254
            or not _EMAIL.fullmatch(self.email)
            or _IDENTITY_CONTROL.search(self.email)
        ):
            raise ReceiptContractError("Git identity email is invalid")


@dataclass(frozen=True)
class ReceiptActors:
    commit_author: GitIdentity | None = None
    git_committer: GitIdentity | None = None
    pr_actor: str | None = None
    promotion_actor: str | None = None

    def __post_init__(self) -> None:
        for identity in (self.commit_author, self.git_committer):
            if identity is not None and not isinstance(identity, GitIdentity):
                raise ReceiptContractError("receipt Git actors must be Git identities")
        for actor in (self.pr_actor, self.promotion_actor):
            if actor is not None and (
                not isinstance(actor, str) or not _ACTOR_LOGIN.fullmatch(actor)
            ):
                raise ReceiptContractError("receipt GitHub actor login is invalid")

    @property
    def empty(self) -> bool:
        return all(
            value is None
            for value in (
                self.commit_author,
                self.git_committer,
                self.pr_actor,
                self.promotion_actor,
            )
        )

    @property
    def complete_for_pr(self) -> bool:
        return bool(self.commit_author and self.git_committer and self.pr_actor)


@dataclass(frozen=True)
class RunReceipt:
    run_id: str
    run_date: str
    repository: str
    status: RunStatus
    base_sha: str
    head_sha: str | None
    candidate_id: str | None
    validation: tuple[ValidationReceipt, ...]
    diff_summary: DiffSummary | None
    protected_paths_touched: tuple[str, ...]
    pr_url: str | None
    merge_sha: str | None
    duration_ms: int
    failure_category: str | None
    actors: ReceiptActors
    schema_version: str = RECEIPT_SCHEMA_VERSION
    automated: bool = True

    def __post_init__(self) -> None:
        try:
            status = RunStatus(self.status)
        except ValueError as exc:
            raise ReceiptContractError("receipt status is invalid") from exc
        object.__setattr__(self, "status", status)
        if self.schema_version != RECEIPT_SCHEMA_VERSION:
            raise ReceiptContractError("unsupported receipt schema version")
        if not isinstance(self.automated, bool) or not self.automated:
            raise ReceiptContractError(
                "daily maintainer receipts must set automated=true"
            )
        run_date = _parse_date(self.run_date)
        if not isinstance(self.repository, str) or not _REPOSITORY.fullmatch(
            self.repository
        ):
            raise ReceiptContractError("receipt repository must be owner/name")
        _require_sha(self.base_sha, "base SHA")
        expected_run_id = f"{run_date.isoformat()}/{self.repository}@{self.base_sha}"
        if self.run_id != expected_run_id:
            raise ReceiptContractError(
                "receipt run_id must bind date, repository, and base SHA"
            )
        if self.head_sha is not None:
            _require_sha(self.head_sha, "head SHA")
        if self.candidate_id is not None:
            if not isinstance(self.candidate_id, str) or not _CANDIDATE_ID.fullmatch(
                self.candidate_id
            ):
                raise ReceiptContractError("receipt candidate ID is invalid")
        if not isinstance(self.validation, tuple) or any(
            not isinstance(item, ValidationReceipt) for item in self.validation
        ):
            raise ReceiptContractError("receipt validation must be a tuple of results")
        if len({item.command_id for item in self.validation}) != len(self.validation):
            raise ReceiptContractError("receipt validation command IDs must be unique")
        if not isinstance(self.protected_paths_touched, tuple) or any(
            not isinstance(path, str) or not path
            for path in self.protected_paths_touched
        ):
            raise ReceiptContractError(
                "protected_paths_touched must be a tuple of paths"
            )
        if self.pr_url is not None:
            if not isinstance(self.pr_url, str) or not re.fullmatch(
                rf"https://github\.com/{re.escape(self.repository)}/pull/[1-9][0-9]*",
                self.pr_url,
            ):
                raise ReceiptContractError("receipt PR URL does not match repository")
        if self.merge_sha is not None:
            _require_sha(self.merge_sha, "merge SHA")
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ReceiptContractError("receipt duration_ms must be non-negative")
        if self.failure_category is not None:
            if not isinstance(
                self.failure_category, str
            ) or not _FAILURE_CATEGORY.fullmatch(self.failure_category):
                raise ReceiptContractError("receipt failure_category is invalid")
        if not isinstance(self.actors, ReceiptActors):
            raise ReceiptContractError("receipt actors contract is invalid")
        self._validate_status_shape()

    def _validate_status_shape(self) -> None:
        if self.status in _FAILURE_STATUSES and self.failure_category is None:
            raise ReceiptContractError("failed receipt requires failure_category")
        if self.status not in _FAILURE_STATUSES and self.failure_category is not None:
            raise ReceiptContractError(
                "successful receipt cannot include failure_category"
            )

        if self.status in {RunStatus.SKIPPED, RunStatus.NO_CHANGE}:
            patch_claims = (
                self.head_sha,
                self.candidate_id,
                self.diff_summary,
                self.pr_url,
                self.merge_sha,
            )
            if any(value is not None for value in patch_claims) or self.validation:
                raise ReceiptContractError(
                    f"{self.status.value} receipt cannot claim a patch or PR"
                )
            if self.protected_paths_touched:
                raise ReceiptContractError(
                    f"{self.status.value} receipt cannot claim a patch or PR"
                )
            if not self.actors.empty:
                raise ReceiptContractError(
                    f"{self.status.value} receipt cannot claim commit or PR actors"
                )
            return

        if self.status in {RunStatus.PR_OPEN, RunStatus.MERGED}:
            if (
                not self.candidate_id
                or not self.head_sha
                or not self.diff_summary
                or not self.pr_url
            ):
                raise ReceiptContractError(
                    f"{self.status.value} receipt requires candidate, patch, and PR"
                )
            if not self.validation or not all(item.ok for item in self.validation):
                raise ReceiptContractError(
                    f"{self.status.value} receipt requires successful validation"
                )
            if self.protected_paths_touched:
                raise ReceiptContractError(
                    f"{self.status.value} receipt cannot touch protected paths"
                )
            if not self.actors.complete_for_pr:
                raise ReceiptContractError(
                    f"{self.status.value} receipt requires commit and PR actors"
                )
            if self.status is RunStatus.MERGED and self.merge_sha is None:
                raise ReceiptContractError("merged receipt requires merge SHA")
            if self.status is RunStatus.MERGED and self.actors.promotion_actor is None:
                raise ReceiptContractError("merged receipt requires promotion actor")
            if self.status is RunStatus.PR_OPEN and self.merge_sha is not None:
                raise ReceiptContractError("pr_open receipt cannot claim merge SHA")
            return

        if self.status is RunStatus.PATCH_REJECTED:
            if not self.candidate_id or not self.diff_summary:
                raise ReceiptContractError(
                    "patch_rejected receipt requires candidate and diff evidence"
                )
            if self.head_sha or self.validation or self.pr_url or self.merge_sha:
                raise ReceiptContractError(
                    "patch_rejected receipt cannot claim validation, commit, or PR"
                )
        if self.status is RunStatus.VALIDATION_FAILED:
            if not self.candidate_id or not self.diff_summary or not self.validation:
                raise ReceiptContractError(
                    "validation_failed receipt requires candidate, diff, and results"
                )
            if all(item.ok for item in self.validation):
                raise ReceiptContractError(
                    "validation_failed receipt must contain a failed result"
                )
            if self.head_sha or self.pr_url or self.merge_sha:
                raise ReceiptContractError(
                    "validation_failed receipt cannot claim a commit or PR"
                )
        if (
            self.status is RunStatus.INFRASTRUCTURE_FAILED
            and self.merge_sha is not None
        ):
            raise ReceiptContractError(
                "infrastructure_failed receipt cannot claim merge SHA"
            )


def _parse_date(value: object) -> dt.date:
    if not isinstance(value, str):
        raise ReceiptContractError("receipt date must be YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ReceiptContractError("receipt date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ReceiptContractError("receipt date must use canonical YYYY-MM-DD")
    return parsed


def _require_sha(value: object, label: str) -> None:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ReceiptContractError(f"receipt {label} must be an exact object ID")


def _expect_fields(
    mapping: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    label: str,
) -> None:
    missing = sorted(required - set(mapping))
    unknown = sorted(set(mapping) - required - set(optional))
    if missing:
        raise ReceiptContractError(f"{label} missing fields: {', '.join(missing)}")
    if unknown:
        raise ReceiptContractError(f"{label} unknown fields: {', '.join(unknown)}")


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validation_from_mapping(value: object) -> ValidationReceipt:
    if not isinstance(value, Mapping):
        raise ReceiptContractError("validation result must be an object")
    _expect_fields(
        value,
        required={
            "command_id",
            "exit_code",
            "output_sha256",
            "duration_ms",
            "timed_out",
        },
        label="validation result",
    )
    return ValidationReceipt(
        command_id=value["command_id"],  # type: ignore[arg-type]
        exit_code=value["exit_code"],  # type: ignore[arg-type]
        output_sha256=value["output_sha256"],  # type: ignore[arg-type]
        duration_ms=value["duration_ms"],  # type: ignore[arg-type]
        timed_out=value["timed_out"],  # type: ignore[arg-type]
    )


def _diff_from_mapping(value: object) -> DiffSummary | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReceiptContractError("diff_summary must be an object or null")
    _expect_fields(
        value,
        required={"files_changed", "production_lines", "test_lines", "patch_sha256"},
        label="diff summary",
    )
    return DiffSummary(
        files_changed=value["files_changed"],  # type: ignore[arg-type]
        production_lines=value["production_lines"],  # type: ignore[arg-type]
        test_lines=value["test_lines"],  # type: ignore[arg-type]
        patch_sha256=value["patch_sha256"],  # type: ignore[arg-type]
    )


def _identity_to_dict(identity: GitIdentity | None) -> dict[str, str] | None:
    if identity is None:
        return None
    return {
        "name": identity.name,
        "email": identity.email,
    }


def _identity_from_mapping(value: object, label: str) -> GitIdentity | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReceiptContractError(f"{label} must be an object or null")
    _expect_fields(value, required={"name", "email"}, label=label)
    return GitIdentity(
        name=value["name"],  # type: ignore[arg-type]
        email=value["email"],  # type: ignore[arg-type]
    )


def _actors_from_mapping(value: object) -> ReceiptActors:
    if not isinstance(value, Mapping):
        raise ReceiptContractError("receipt actors must be an object")
    _expect_fields(
        value,
        required={
            "commit_author",
            "git_committer",
            "pr_actor",
            "promotion_actor",
        },
        label="receipt actors",
    )
    return ReceiptActors(
        commit_author=_identity_from_mapping(
            value["commit_author"],
            "commit_author",
        ),
        git_committer=_identity_from_mapping(
            value["git_committer"],
            "git_committer",
        ),
        pr_actor=value["pr_actor"],  # type: ignore[arg-type]
        promotion_actor=value["promotion_actor"],  # type: ignore[arg-type]
    )


def receipt_to_dict(receipt: RunReceipt) -> dict[str, object]:
    return {
        "schema_version": receipt.schema_version,
        "run_id": receipt.run_id,
        "run_date": receipt.run_date,
        "repository": receipt.repository,
        "status": receipt.status.value,
        "base_sha": receipt.base_sha,
        "head_sha": receipt.head_sha,
        "candidate_id": receipt.candidate_id,
        "validation": [
            {
                "command_id": item.command_id,
                "exit_code": item.exit_code,
                "output_sha256": item.output_sha256,
                "duration_ms": item.duration_ms,
                "timed_out": item.timed_out,
            }
            for item in receipt.validation
        ],
        "diff_summary": (
            {
                "files_changed": receipt.diff_summary.files_changed,
                "production_lines": receipt.diff_summary.production_lines,
                "test_lines": receipt.diff_summary.test_lines,
                "patch_sha256": receipt.diff_summary.patch_sha256,
            }
            if receipt.diff_summary
            else None
        ),
        "protected_paths_touched": list(receipt.protected_paths_touched),
        "pr_url": receipt.pr_url,
        "merge_sha": receipt.merge_sha,
        "duration_ms": receipt.duration_ms,
        "failure_category": receipt.failure_category,
        "actors": {
            "commit_author": _identity_to_dict(receipt.actors.commit_author),
            "git_committer": _identity_to_dict(receipt.actors.git_committer),
            "pr_actor": receipt.actors.pr_actor,
            "promotion_actor": receipt.actors.promotion_actor,
        },
        "automated": receipt.automated,
    }


def serialize_receipt(receipt: RunReceipt) -> bytes:
    return (
        json.dumps(
            receipt_to_dict(receipt),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def parse_receipt(payload: bytes | str) -> RunReceipt:
    try:
        value = json.loads(payload, object_pairs_hook=_strict_json_pairs)
    except ReceiptContractError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise ReceiptContractError("receipt is not valid strict JSON") from exc
    if not isinstance(value, Mapping):
        raise ReceiptContractError("receipt must be a JSON object")
    _expect_fields(
        value,
        required={
            "schema_version",
            "run_id",
            "run_date",
            "repository",
            "status",
            "base_sha",
            "head_sha",
            "candidate_id",
            "validation",
            "diff_summary",
            "protected_paths_touched",
            "pr_url",
            "merge_sha",
            "duration_ms",
            "failure_category",
            "actors",
            "automated",
        },
        label="receipt",
    )
    raw_validation = value["validation"]
    raw_protected = value["protected_paths_touched"]
    if not isinstance(raw_validation, list):
        raise ReceiptContractError("receipt validation must be a list")
    if not isinstance(raw_protected, list) or any(
        not isinstance(item, str) for item in raw_protected
    ):
        raise ReceiptContractError("protected_paths_touched must be a list of strings")
    return RunReceipt(
        schema_version=value["schema_version"],  # type: ignore[arg-type]
        run_id=value["run_id"],  # type: ignore[arg-type]
        run_date=value["run_date"],  # type: ignore[arg-type]
        repository=value["repository"],  # type: ignore[arg-type]
        status=value["status"],  # type: ignore[arg-type]
        base_sha=value["base_sha"],  # type: ignore[arg-type]
        head_sha=value["head_sha"],  # type: ignore[arg-type]
        candidate_id=value["candidate_id"],  # type: ignore[arg-type]
        validation=tuple(_validation_from_mapping(item) for item in raw_validation),
        diff_summary=_diff_from_mapping(value["diff_summary"]),
        protected_paths_touched=tuple(raw_protected),
        pr_url=value["pr_url"],  # type: ignore[arg-type]
        merge_sha=value["merge_sha"],  # type: ignore[arg-type]
        duration_ms=value["duration_ms"],  # type: ignore[arg-type]
        failure_category=value["failure_category"],  # type: ignore[arg-type]
        actors=_actors_from_mapping(value["actors"]),
        automated=value["automated"],  # type: ignore[arg-type]
    )


def receipt_digest(receipt: RunReceipt) -> str:
    return hashlib.sha256(serialize_receipt(receipt)).hexdigest()


def journal_marker(run_date: str, repository: str) -> str:
    parsed = _parse_date(run_date)
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
        raise ReceiptContractError("journal repository must be owner/name")
    return f"<!-- vista-daily-receipt:{parsed.isoformat()}:{repository} -->"


def journal_entry(receipt: RunReceipt) -> str:
    tick = chr(96)
    lines = [
        journal_marker(receipt.run_date, receipt.repository),
        f"status: {tick}{receipt.status.value}{tick}",
        f"run_id: {tick}{receipt.run_id}{tick}",
        f"candidate_id: {tick}{receipt.candidate_id or 'none'}{tick}",
        f"base_sha: {tick}{receipt.base_sha}{tick}",
        f"head_sha: {tick}{receipt.head_sha or 'none'}{tick}",
        f"commit_author: {tick}{receipt.actors.commit_author.name if receipt.actors.commit_author else 'none'}{tick}",
        f"git_committer: {tick}{receipt.actors.git_committer.name if receipt.actors.git_committer else 'none'}{tick}",
        f"pr_actor: {tick}{receipt.actors.pr_actor or 'none'}{tick}",
        f"promotion_actor: {tick}{receipt.actors.promotion_actor or 'none'}{tick}",
        f"receipt_sha256: {tick}{receipt_digest(receipt)}{tick}",
    ]
    return "\n".join(lines) + "\n"


def validate_status_transition(previous: RunStatus, current: RunStatus) -> None:
    try:
        before = RunStatus(previous)
        after = RunStatus(current)
    except ValueError as exc:
        raise ReceiptContractError(
            "status transition contains an invalid status"
        ) from exc
    if after not in _ALLOWED_TRANSITIONS[before]:
        raise ReceiptContractError(
            f"invalid receipt status transition: {before.value} -> {after.value}"
        )
