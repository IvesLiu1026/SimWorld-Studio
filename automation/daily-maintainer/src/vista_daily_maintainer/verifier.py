from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .candidate import Candidate
from .guard import DiffGuard, GuardLimits, GuardReport
from .profiles import (
    BUILTIN_VALIDATION_PROFILES,
    ValidationProfile,
    ValidationProfileRegistry,
)


@dataclass(frozen=True)
class ValidationResult:
    command_id: str
    exit_code: int
    output_sha256: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class VerificationReport:
    guard: GuardReport
    validation: tuple[ValidationResult, ...]

    @property
    def ok(self) -> bool:
        return (
            self.guard.ok
            and bool(self.validation)
            and all(item.ok for item in self.validation)
        )


class Verifier:
    """Credential-free verifier with candidate-independent process arguments."""

    def __init__(
        self,
        *,
        registry: ValidationProfileRegistry = BUILTIN_VALIDATION_PROFILES,
        guard: DiffGuard | None = None,
    ) -> None:
        self._registry = registry
        self._guard = guard or DiffGuard()

    def verify(
        self,
        repo_root: Path,
        base_sha: str,
        candidate: Candidate,
        *,
        inherited_env: Mapping[str, str] | None = None,
        limits: GuardLimits | None = None,
    ) -> VerificationReport:
        # Resolve every ID before inspecting or executing the patch. A candidate
        # cannot smuggle cwd/argv into this lookup.
        profiles = tuple(
            self._registry.resolve(item) for item in candidate.validation_profiles
        )
        guard_report = self._guard.inspect(
            repo_root, base_sha, candidate, limits=limits
        )
        if not guard_report.ok:
            return VerificationReport(guard=guard_report, validation=())

        repo = Path(repo_root).resolve(strict=True)
        environment = self._sanitized_environment(inherited_env)
        validation: list[ValidationResult] = []

        # This mandatory command structure is verifier-owned. base_sha already
        # passed the guard's exact object-ID validation.
        validation.append(
            self._run(
                command_id="git-diff-check",
                argv=("git", "diff", "--check", base_sha, "--"),
                cwd=repo,
                timeout_seconds=60,
                environment=environment,
            )
        )
        if not validation[-1].ok:
            return VerificationReport(guard=guard_report, validation=tuple(validation))

        for profile in profiles:
            result = self._run_profile(repo, profile, environment)
            validation.append(result)
            if not result.ok:
                break
        return VerificationReport(guard=guard_report, validation=tuple(validation))

    @staticmethod
    def _sanitized_environment(inherited: Mapping[str, str] | None) -> dict[str, str]:
        source = inherited if inherited is not None else os.environ
        return {
            "PATH": source.get("PATH", os.defpath),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "NO_COLOR": "1",
            "PYTHONNOUSERSITE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def _run_profile(
        self,
        repo: Path,
        profile: ValidationProfile,
        environment: Mapping[str, str],
    ) -> ValidationResult:
        cwd = (repo / profile.cwd).resolve(strict=False)
        if cwd != repo and repo not in cwd.parents:
            raise ValueError(
                f"validation profile cwd escapes repository: {profile.profile_id}"
            )
        if not cwd.is_dir():
            return self._synthetic_failure(
                profile.profile_id,
                f"profile cwd does not exist: {profile.cwd}",
            )
        return self._run(
            command_id=profile.profile_id,
            argv=profile.argv,
            cwd=cwd,
            timeout_seconds=profile.timeout_seconds,
            environment=environment,
        )

    @staticmethod
    def _run(
        *,
        command_id: str,
        argv: tuple[str, ...],
        cwd: Path,
        timeout_seconds: int,
        environment: Mapping[str, str],
    ) -> ValidationResult:
        started = time.monotonic_ns()
        timed_out = False
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
                timeout=timeout_seconds,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
        except OSError as exc:
            exit_code = 127
            stdout = b""
            stderr = f"{exc.__class__.__name__}:{exc.errno}".encode("ascii", "replace")
        duration_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
        digest = hashlib.sha256()
        digest.update(b"stdout\0")
        digest.update(stdout)
        digest.update(b"\0stderr\0")
        digest.update(stderr)
        return ValidationResult(
            command_id=command_id,
            exit_code=exit_code,
            output_sha256=digest.hexdigest(),
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    @staticmethod
    def _synthetic_failure(command_id: str, reason: str) -> ValidationResult:
        return ValidationResult(
            command_id=command_id,
            exit_code=127,
            output_sha256=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            duration_ms=0,
        )
