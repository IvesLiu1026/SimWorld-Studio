from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Mapping

import yaml
from yaml.tokens import AliasToken, AnchorToken, TagToken

from .profiles import BUILTIN_VALIDATION_PROFILES, ValidationProfileRegistry


BACKLOG_SCHEMA_VERSION = "vista.world.daily-maintainer.backlog.v1"
MAX_BACKLOG_BYTES = 1024 * 1024
_CANDIDATE_ID = re.compile(r"^VW-DM-[0-9]{4,}$")
_APPROVER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/*?-]+$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_V1_PROFILE_IDS = frozenset(
    {
        "daily-maintainer-core-tests",
        "tools-python-offline",
        "web-frontend-build",
        "web-server-contracts",
        "web-server-unit",
    }
)
_V1_FORBIDDEN_AUTHORITY = frozenset(
    {
        ".agent",
        ".claude",
        ".codex",
        ".github",
        "assets",
        "auth",
        "credentials",
        "datasets",
        "deploy",
        "evidence",
        "network",
        "ops",
        "runtime",
        "secrets",
        "systemd",
        "ue",
        "unreal",
        "unreal_plugins",
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


class CandidateContractError(ValueError):
    """The reviewed candidate contract failed closed."""


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _StrictSafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise CandidateContractError("mapping keys must be scalar") from exc
        if duplicate:
            raise CandidateContractError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class BacklogTrust:
    """Out-of-band identity of one human-reviewed backlog revision."""

    path: Path
    sha256: str
    manifest_revision: int
    approved_by: str

    def __post_init__(self) -> None:
        try:
            normalized_path = Path(self.path).absolute()
        except TypeError as exc:
            raise CandidateContractError("trusted backlog path is invalid") from exc
        object.__setattr__(self, "path", normalized_path)
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise CandidateContractError(
                "trusted backlog digest must be lowercase SHA-256"
            )
        if (
            isinstance(self.manifest_revision, bool)
            or not isinstance(self.manifest_revision, int)
            or self.manifest_revision < 1
        ):
            raise CandidateContractError(
                "trusted manifest revision must be a positive integer"
            )
        if not isinstance(self.approved_by, str) or not _APPROVER.fullmatch(
            self.approved_by
        ):
            raise CandidateContractError("trusted approver is invalid")


@dataclass(frozen=True)
class CandidateSource:
    kind: str
    manifest_revision: int
    approved_by: str
    issue_url: str | None = None

    def __post_init__(self) -> None:
        if self.kind != "curated_backlog":
            raise CandidateContractError(
                "candidate source must be curated_backlog in v1"
            )
        if (
            isinstance(self.manifest_revision, bool)
            or not isinstance(self.manifest_revision, int)
            or self.manifest_revision < 1
        ):
            raise CandidateContractError("candidate source revision must be positive")
        if not isinstance(self.approved_by, str) or not _APPROVER.fullmatch(
            self.approved_by
        ):
            raise CandidateContractError("candidate source approver is invalid")
        if self.issue_url is not None:
            if not isinstance(self.issue_url, str) or not re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*",
                self.issue_url,
            ):
                raise CandidateContractError(
                    "candidate issue URL must be an exact GitHub issue URL"
                )


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    title: str
    risk_tier: int
    allowed_paths: tuple[str, ...]
    acceptance: tuple[str, ...]
    validation_profiles: tuple[str, ...]
    expected_external_side_effects: str
    source: CandidateSource
    state: str = "open"
    not_before: dt.date | None = None
    expires_on: dt.date | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not _CANDIDATE_ID.fullmatch(
            self.candidate_id
        ):
            raise CandidateContractError(f"invalid candidate ID: {self.candidate_id!r}")
        _validate_text(self.title, "candidate title", minimum=1, maximum=160)
        if "\n" in self.title or "\r" in self.title:
            raise CandidateContractError("candidate title must be one line")
        if isinstance(self.risk_tier, bool) or self.risk_tier not in {0, 1, 2, 3}:
            raise CandidateContractError("candidate risk tier must be 0, 1, 2, or 3")
        if not isinstance(self.allowed_paths, tuple) or not self.allowed_paths:
            raise CandidateContractError("candidate must own at least one allowed path")
        if len(set(self.allowed_paths)) != len(self.allowed_paths):
            raise CandidateContractError("candidate allowed paths must be unique")
        for pattern in self.allowed_paths:
            validate_allowed_path(pattern)
        if not isinstance(self.acceptance, tuple) or not self.acceptance:
            raise CandidateContractError("candidate must have acceptance criteria")
        for criterion in self.acceptance:
            _validate_text(criterion, "acceptance criterion", minimum=1, maximum=500)
        if (
            not isinstance(self.validation_profiles, tuple)
            or not self.validation_profiles
        ):
            raise CandidateContractError(
                "candidate must reference a validation profile"
            )
        if len(set(self.validation_profiles)) != len(self.validation_profiles):
            raise CandidateContractError("candidate validation profiles must be unique")
        for profile_id in self.validation_profiles:
            if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
                raise CandidateContractError(
                    f"invalid validation profile ID: {profile_id!r}"
                )
        if self.expected_external_side_effects != "none":
            raise CandidateContractError(
                "unattended candidates must declare no external side effects"
            )
        if self.state not in {"open", "closed"}:
            raise CandidateContractError("candidate state must be open or closed")
        for value, label in (
            (self.not_before, "candidate not_before"),
            (self.expires_on, "candidate expires_on"),
        ):
            if value is not None and (
                not isinstance(value, dt.date) or isinstance(value, dt.datetime)
            ):
                raise CandidateContractError(f"{label} must be a date")
        if self.not_before and self.expires_on and self.not_before > self.expires_on:
            raise CandidateContractError("candidate active date range is inverted")

    @property
    def id(self) -> str:
        return self.candidate_id

    def eligible_on(self, on_date: dt.date) -> bool:
        return (
            self.state == "open"
            and (self.not_before is None or self.not_before <= on_date)
            and (self.expires_on is None or on_date <= self.expires_on)
        )

    def normalized_payload(self) -> dict[str, object]:
        """Return data for an unprivileged patcher, without commands or authority."""

        return {
            "id": self.candidate_id,
            "title": self.title,
            "risk_tier": self.risk_tier,
            "allowed_paths": list(self.allowed_paths),
            "acceptance": list(self.acceptance),
            "validation_profile_ids": list(self.validation_profiles),
            "expected_external_side_effects": self.expected_external_side_effects,
        }


@dataclass(frozen=True)
class Backlog:
    schema_version: str
    manifest_revision: int
    approved_by: str
    sha256: str
    candidates: tuple[Candidate, ...]


def _validate_text(value: object, label: str, *, minimum: int, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or _CONTROL.search(value)
    ):
        raise CandidateContractError(
            f"{label} must be safe text between {minimum} and {maximum} characters"
        )


def validate_allowed_path(pattern: object) -> None:
    if (
        not isinstance(pattern, str)
        or not pattern
        or pattern.startswith(("/", "\\"))
        or "\\" in pattern
        or "//" in pattern
        or not _PATH_PATTERN.fullmatch(pattern)
        or any(part in {"", ".", ".."} for part in pattern.split("/"))
        or any("**" in part and part != "**" for part in pattern.split("/"))
    ):
        raise CandidateContractError(f"invalid allowed path pattern: {pattern!r}")


def path_matches_pattern(path: str, pattern: str) -> bool:
    """Match the deliberately small V1 allowlist glob language."""

    if pattern == "**":
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        if not any(char in prefix for char in "*?"):
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


def enforce_v1_candidate_policy(candidate: Candidate) -> None:
    """Apply the approved unattended Tier 0/1 authority envelope."""

    if candidate.risk_tier not in {0, 1}:
        raise CandidateContractError(
            "V1 candidate policy permits only Tier 0 and Tier 1 candidates"
        )
    unknown_profiles = sorted(set(candidate.validation_profiles) - _V1_PROFILE_IDS)
    if unknown_profiles:
        raise CandidateContractError(
            "V1 candidate policy forbids validation profiles: "
            + ", ".join(unknown_profiles)
        )
    for pattern in candidate.allowed_paths:
        lowered = pattern.lower()
        parts = tuple(part for part in lowered.split("/") if part)
        literal_parts = tuple(
            part for part in parts if not any(character in part for character in "*?")
        )
        if pattern == "**" or any(
            part in _V1_FORBIDDEN_AUTHORITY for part in literal_parts
        ):
            raise CandidateContractError(
                f"V1 candidate policy forbids path authority: {pattern}"
            )
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
        if candidate.risk_tier == 0:
            if not (lowered.startswith("docs/") or is_test_scope):
                raise CandidateContractError(
                    f"V1 candidate policy restricts Tier 0 path: {pattern}"
                )
        elif not (
            any(lowered.startswith(prefix) for prefix in _V1_TIER1_PREFIXES)
            or is_test_scope
        ):
            raise CandidateContractError(
                f"V1 candidate policy restricts Tier 1 path: {pattern}"
            )


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise CandidateContractError(f"{label} must be a string-keyed mapping")
    return value


def _strict_fields(
    value: Mapping[str, object],
    *,
    required: Collection[str],
    optional: Collection[str] = (),
    label: str,
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise CandidateContractError(f"{label} missing fields: {', '.join(missing)}")
    if unknown:
        raise CandidateContractError(f"{label} unknown fields: {', '.join(unknown)}")


def _date(value: object, label: str) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        raise CandidateContractError(f"{label} must be a date, not a timestamp")
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise CandidateContractError(f"{label} must be YYYY-MM-DD") from exc
    raise CandidateContractError(f"{label} must be YYYY-MM-DD")


def _tuple_of_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CandidateContractError(f"{label} must be a list of strings")
    return tuple(value)


def _parse_source(value: object) -> CandidateSource:
    mapping = _require_mapping(value, "candidate source")
    _strict_fields(
        mapping,
        required={"kind", "manifest_revision", "approved_by"},
        optional={"issue_url"},
        label="candidate source",
    )
    return CandidateSource(
        kind=mapping["kind"],  # type: ignore[arg-type]
        manifest_revision=mapping["manifest_revision"],  # type: ignore[arg-type]
        approved_by=mapping["approved_by"],  # type: ignore[arg-type]
        issue_url=mapping.get("issue_url"),  # type: ignore[arg-type]
    )


def _parse_candidate(
    value: object,
    *,
    revision: int,
    approved_by: str,
    profiles: ValidationProfileRegistry,
) -> Candidate:
    mapping = _require_mapping(value, "candidate")
    _strict_fields(
        mapping,
        required={
            "id",
            "title",
            "risk_tier",
            "allowed_paths",
            "acceptance",
            "validation_profiles",
            "expected_external_side_effects",
            "source",
        },
        optional={"state", "not_before", "expires_on"},
        label="candidate",
    )
    candidate = Candidate(
        candidate_id=mapping["id"],  # type: ignore[arg-type]
        title=mapping["title"],  # type: ignore[arg-type]
        risk_tier=mapping["risk_tier"],  # type: ignore[arg-type]
        allowed_paths=_tuple_of_strings(
            mapping["allowed_paths"], "candidate allowed paths"
        ),
        acceptance=_tuple_of_strings(mapping["acceptance"], "candidate acceptance"),
        validation_profiles=_tuple_of_strings(
            mapping["validation_profiles"],
            "candidate validation profiles",
        ),
        expected_external_side_effects=mapping["expected_external_side_effects"],  # type: ignore[arg-type]
        source=_parse_source(mapping["source"]),
        state=mapping.get("state", "open"),  # type: ignore[arg-type]
        not_before=_date(mapping.get("not_before"), "candidate not_before"),
        expires_on=_date(mapping.get("expires_on"), "candidate expires_on"),
    )
    if candidate.source.manifest_revision != revision:
        raise CandidateContractError(
            "candidate source revision does not match backlog revision"
        )
    if candidate.source.approved_by != approved_by:
        raise CandidateContractError(
            "candidate source approver does not match backlog approver"
        )
    unknown_profiles = sorted(set(candidate.validation_profiles) - profiles.ids)
    if unknown_profiles:
        raise CandidateContractError(
            "candidate references unknown validation profile: "
            + ", ".join(unknown_profiles)
        )
    enforce_v1_candidate_policy(candidate)
    return candidate


def _read_reviewed_file(trust: BacklogTrust) -> bytes:
    path = trust.path
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise CandidateContractError(
                "trusted backlog path cannot contain a symlink"
            )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CandidateContractError(
            f"cannot open trusted backlog: {exc.strerror}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CandidateContractError("trusted backlog must be a regular file")
        if metadata.st_size > MAX_BACKLOG_BYTES:
            raise CandidateContractError("trusted backlog exceeds the size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(MAX_BACKLOG_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_BACKLOG_BYTES:
        raise CandidateContractError("trusted backlog exceeds the size limit")
    if hashlib.sha256(payload).hexdigest() != trust.sha256:
        raise CandidateContractError("trusted backlog digest mismatch")
    return payload


def _load_yaml(payload: bytes) -> object:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateContractError("trusted backlog must be UTF-8") from exc
    try:
        for token in yaml.scan(text):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise CandidateContractError(
                    "YAML aliases, anchors, and custom tags are forbidden"
                )
        return yaml.load(text, Loader=_StrictSafeLoader)
    except CandidateContractError:
        raise
    except yaml.YAMLError as exc:
        raise CandidateContractError(
            "trusted backlog is not valid strict YAML"
        ) from exc


def load_trusted_backlog(
    trust: BacklogTrust,
    *,
    profiles: ValidationProfileRegistry = BUILTIN_VALIDATION_PROFILES,
) -> Backlog:
    """Load a backlog only when its out-of-band review identity still matches."""

    payload = _read_reviewed_file(trust)
    mapping = _require_mapping(_load_yaml(payload), "backlog")
    _strict_fields(
        mapping,
        required={"schema_version", "manifest_revision", "approved_by", "candidates"},
        label="backlog",
    )
    if mapping["schema_version"] != BACKLOG_SCHEMA_VERSION:
        raise CandidateContractError("unsupported backlog schema version")
    revision = mapping["manifest_revision"]
    approver = mapping["approved_by"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise CandidateContractError(
            "backlog manifest revision must be a positive integer"
        )
    if not isinstance(approver, str) or not _APPROVER.fullmatch(approver):
        raise CandidateContractError("backlog approver is invalid")
    if revision != trust.manifest_revision or approver != trust.approved_by:
        raise CandidateContractError(
            "backlog review identity does not match trusted metadata"
        )
    raw_candidates = mapping["candidates"]
    if not isinstance(raw_candidates, list):
        raise CandidateContractError("backlog candidates must be a list")
    candidates = tuple(
        _parse_candidate(
            item, revision=revision, approved_by=approver, profiles=profiles
        )
        for item in raw_candidates
    )
    ids = [candidate.candidate_id for candidate in candidates]
    if len(set(ids)) != len(ids):
        raise CandidateContractError("backlog candidate IDs must be unique")
    return Backlog(
        schema_version=BACKLOG_SCHEMA_VERSION,
        manifest_revision=revision,
        approved_by=approver,
        sha256=trust.sha256,
        candidates=candidates,
    )


def select_candidate(
    backlog: Backlog,
    *,
    on_date: dt.date,
    completed_ids: Collection[str],
    allowed_risk_tiers: Collection[int] = (0, 1),
) -> Candidate | None:
    """Select the same candidate for the same immutable inputs."""

    if not isinstance(on_date, dt.date) or isinstance(on_date, dt.datetime):
        raise ValueError("selector date must be a date")
    completed = frozenset(completed_ids)
    tiers = frozenset(allowed_risk_tiers)
    for candidate in backlog.candidates:
        enforce_v1_candidate_policy(candidate)
    eligible = (
        candidate
        for candidate in backlog.candidates
        if candidate.candidate_id not in completed
        and candidate.risk_tier in tiers
        and candidate.eligible_on(on_date)
    )
    return min(
        eligible,
        key=lambda candidate: (candidate.risk_tier, candidate.candidate_id),
        default=None,
    )
