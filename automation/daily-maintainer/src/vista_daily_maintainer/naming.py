from __future__ import annotations

import datetime as dt
import re


MAX_CANDIDATE_SLUG_LENGTH = 48

_CANDIDATE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_DAILY_BRANCH = re.compile(
    r"^codex/daily/"
    r"(?P<run_date>[0-9]{4}-[0-9]{2}-[0-9]{2})-"
    r"(?P<candidate_slug>[a-z0-9]+(?:-[a-z0-9]+)*)-"
    r"(?P<base_prefix>[0-9a-f]{8})$"
)


class NamingContractError(ValueError):
    """A Daily Maintainer slug or derived branch violates the V1 contract."""


def is_v1_candidate_slug(value: object) -> bool:
    """Return whether *value* is the canonical, bounded V1 candidate slug."""

    return (
        type(value) is str
        and len(value) <= MAX_CANDIDATE_SLUG_LENGTH
        and _CANDIDATE_SLUG.fullmatch(value) is not None
    )


def v1_daily_branch_name(
    run_date: str,
    candidate_slug: str,
    base_sha: str,
) -> str:
    """Construct the one canonical V1 daily branch name."""

    if not _is_canonical_date(run_date):
        raise NamingContractError("run date is invalid")
    if not is_v1_candidate_slug(candidate_slug):
        raise NamingContractError("candidate slug is invalid")
    if type(base_sha) is not str or _GIT_OBJECT_ID.fullmatch(base_sha) is None:
        raise NamingContractError("base SHA is invalid")
    return f"codex/daily/{run_date}-{candidate_slug}-{base_sha[:8]}"


def is_v1_daily_branch_name(value: object) -> bool:
    """Return whether *value* could have been emitted by the V1 constructor."""

    if type(value) is not str or len(value) > 128:
        return False
    match = _DAILY_BRANCH.fullmatch(value)
    if match is None:
        return False
    return _is_canonical_date(match.group("run_date")) and is_v1_candidate_slug(
        match.group("candidate_slug")
    )


def _is_canonical_date(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value
