from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from vista_daily_maintainer.candidate import Candidate, CandidateSource


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def init_repo(repo: Path, files: dict[str, str] | None = None) -> str:
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.name", "Daily Maintainer Test")
    run_git(repo, "config", "user.email", "daily-maintainer-test@example.invalid")
    for relative, contents in (files or {"src/app.py": "VALUE = 1\n"}).items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    run_git(repo, "add", "--all")
    run_git(repo, "commit", "-qm", "test: initial fixture")
    return run_git(repo, "rev-parse", "HEAD")


def make_candidate(
    *,
    candidate_id: str = "VW-DM-0001",
    allowed_paths: tuple[str, ...] = ("src/**",),
    profiles: tuple[str, ...] = ("daily-maintainer-core-tests",),
    risk_tier: int = 1,
) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        title="Exercise one bounded maintenance fix",
        risk_tier=risk_tier,
        allowed_paths=allowed_paths,
        acceptance=("The regression remains covered by an offline test.",),
        validation_profiles=profiles,
        expected_external_side_effects="none",
        source=CandidateSource(
            kind="curated_backlog",
            manifest_revision=7,
            approved_by="IvesLiu1026",
        ),
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
