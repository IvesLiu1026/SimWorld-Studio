from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .candidate import (
    Candidate,
    candidate_authorization_digest,
    enforce_v1_candidate_policy,
)
from .guard import DiffGuard, GuardLimits, GuardReport
from .profiles import (
    BUILTIN_VALIDATION_PROFILES,
    TrustedExecutables,
    ValidationProfile,
)


@dataclass(frozen=True)
class IsolationEvidence:
    """Digest-bound caller evidence, not a self-authenticating sandbox proof.

    The core cannot establish network, credential, filesystem, UID, cgroup, or
    read-only-mount isolation from inside this Python process.  T13 must issue
    and bind this evidence from an outer trusted boundary before unattended
    publication can exist.
    """

    network_isolated: bool
    credentials_absent: bool
    observed_by: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if self.network_isolated is not True or self.credentials_absent is not True:
            raise ValueError(
                "verifier requires network isolation and absent credentials"
            )
        if (
            not isinstance(self.observed_by, str)
            or not self.observed_by
            or any(character in self.observed_by for character in "\r\n\0")
        ):
            raise ValueError("isolation evidence observer is invalid")
        if (
            not isinstance(self.evidence_sha256, str)
            or len(self.evidence_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.evidence_sha256
            )
        ):
            raise ValueError("isolation evidence must be lowercase SHA-256")


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
    candidate_sha256: str
    guard: GuardReport
    final_guard: GuardReport
    validation: tuple[ValidationResult, ...]
    isolation_evidence: IsolationEvidence
    mutation_detected: bool = False
    publication_authorized: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidate_sha256, str)
            or len(self.candidate_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.candidate_sha256
            )
        ):
            raise ValueError("verification candidate digest must be lowercase SHA-256")
        if self.publication_authorized is not False:
            raise ValueError("core verification reports cannot authorize publication")

    @property
    def checks_passed(self) -> bool:
        """Return local deterministic check status, never publication authority."""

        return (
            self.guard.ok
            and self.final_guard.ok
            and not self.mutation_detected
            and bool(self.validation)
            and all(item.ok for item in self.validation)
        )


class Verifier:
    """Credential-free local checker with fixed tools and patch identity.

    A returned report is deliberately not a publishable authorization.  The
    outer T13 isolation service must bind this result into an immutable artifact
    that a separate publisher can independently authenticate.
    """

    def __init__(
        self,
        *,
        executables: TrustedExecutables | None = None,
        guard: DiffGuard | None = None,
        isolation_evidence: IsolationEvidence | None = None,
    ) -> None:
        if isolation_evidence is None:
            raise ValueError("outer isolation evidence is required before validation")
        self._executables = executables or TrustedExecutables.system_defaults()
        self._isolation_evidence = isolation_evidence
        self._guard = guard or DiffGuard(
            git_executable=self._executables.resolve("git")
        )

    def verify(
        self,
        repo_root: Path,
        base_sha: str,
        candidate: Candidate,
        *,
        inherited_env: Mapping[str, str] | None = None,
        limits: GuardLimits | None = None,
    ) -> VerificationReport:
        # inherited_env remains in the API for explicit proof that none of it is
        # copied into a validation process.
        del inherited_env
        enforce_v1_candidate_policy(candidate)
        candidate_sha256 = candidate_authorization_digest(candidate)
        profiles = self._trusted_profiles(candidate)
        repo = Path(repo_root).resolve(strict=True)
        self._preflight_profile_executables(repo, profiles)
        initial_guard = self._guard.inspect(
            repo,
            base_sha,
            candidate,
            limits=limits,
        )
        if not initial_guard.ok:
            return VerificationReport(
                candidate_sha256=candidate_sha256,
                guard=initial_guard,
                final_guard=initial_guard,
                validation=(),
                isolation_evidence=self._isolation_evidence,
            )

        validation: list[ValidationResult] = []
        current_guard = initial_guard
        with tempfile.TemporaryDirectory(prefix="vista-dm-verifier-") as temp:
            environment = self._sanitized_environment(
                Path(temp),
                self._executables,
            )
            commands: list[tuple[str, tuple[str, ...], Path, int]] = [
                (
                    "git-diff-check",
                    (
                        str(self._executables.resolve("git")),
                        "diff",
                        "--check",
                        "--no-ext-diff",
                        "--no-textconv",
                        base_sha,
                        "--",
                    ),
                    repo,
                    60,
                )
            ]
            for profile in profiles:
                cwd = self._profile_cwd(repo, profile)
                if cwd is None:
                    result = self._synthetic_failure(
                        profile.profile_id,
                        f"profile cwd does not exist: {profile.cwd}",
                    )
                    validation.append(result)
                    current_guard = self._guard.inspect(
                        repo,
                        base_sha,
                        candidate,
                        limits=limits,
                    )
                    return self._report(
                        candidate_sha256,
                        initial_guard,
                        current_guard,
                        validation,
                        self._isolation_evidence,
                    )
                argv = profile.argv
                commands.append(
                    (
                        profile.profile_id,
                        argv,
                        cwd,
                        profile.timeout_seconds,
                    )
                )

            for command_id, argv, cwd, timeout_seconds in commands:
                # Revalidate the executable identity immediately before every
                # spawn.  T13 still owns the final check-to-exec race.
                argv = self._executables.resolve_argv(argv)
                validation.append(
                    self._run(
                        command_id=command_id,
                        argv=argv,
                        cwd=cwd,
                        timeout_seconds=timeout_seconds,
                        environment=environment,
                    )
                )
                current_guard = self._guard.inspect(
                    repo,
                    base_sha,
                    candidate,
                    limits=limits,
                )
                report = self._report(
                    candidate_sha256,
                    initial_guard,
                    current_guard,
                    validation,
                    self._isolation_evidence,
                )
                if report.mutation_detected or not validation[-1].ok:
                    return report
        return self._report(
            candidate_sha256,
            initial_guard,
            current_guard,
            validation,
            self._isolation_evidence,
        )

    def _trusted_profiles(self, candidate: Candidate) -> tuple[ValidationProfile, ...]:
        profiles: list[ValidationProfile] = []
        for profile_id in candidate.validation_profiles:
            profiles.append(BUILTIN_VALIDATION_PROFILES.resolve(profile_id))
        return tuple(profiles)

    def _preflight_profile_executables(
        self,
        repo: Path,
        profiles: tuple[ValidationProfile, ...],
    ) -> None:
        for profile in profiles:
            executable = self._executables.resolve(profile.argv[0])
            if executable == repo or repo in executable.parents:
                raise ValueError(
                    "validation executable must be pinned outside the repository: "
                    f"{profile.profile_id}"
                )

    @staticmethod
    def _report(
        candidate_sha256: str,
        initial_guard: GuardReport,
        final_guard: GuardReport,
        validation: list[ValidationResult],
        isolation_evidence: IsolationEvidence,
    ) -> VerificationReport:
        mutation = (
            initial_guard.patch_sha256 != final_guard.patch_sha256
            or initial_guard.changed_files != final_guard.changed_files
        )
        return VerificationReport(
            candidate_sha256=candidate_sha256,
            guard=initial_guard,
            final_guard=final_guard,
            validation=tuple(validation),
            isolation_evidence=isolation_evidence,
            mutation_detected=mutation,
        )

    @staticmethod
    def _sanitized_environment(
        temp_root: Path,
        executables: TrustedExecutables,
    ) -> dict[str, str]:
        home = temp_root / "home"
        config = temp_root / "xdg-config"
        cache = temp_root / "xdg-cache"
        data = temp_root / "xdg-data"
        temporary = temp_root / "tmp"
        for path in (home, config, cache, data, temporary):
            path.mkdir(mode=0o700)
        npm_config = temp_root / "empty-npmrc"
        uv_config = temp_root / "empty-uv.toml"
        uv_environment = temp_root / "uv-environment"
        trusted_bin = executables.materialize_bin(temp_root / "trusted-bin")
        npm_config.write_text("", encoding="utf-8")
        uv_config.write_text("", encoding="utf-8")
        return {
            "PATH": str(trusted_bin),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
            "XDG_DATA_HOME": str(data),
            "TMPDIR": str(temporary),
            "TMP": str(temporary),
            "TEMP": str(temporary),
            "NPM_CONFIG_USERCONFIG": str(npm_config),
            "NPM_CONFIG_CACHE": str(cache / "npm"),
            "UV_CONFIG_FILE": str(uv_config),
            "UV_NO_CONFIG": "1",
            "UV_PROJECT_ENVIRONMENT": str(uv_environment),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "NO_COLOR": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }

    @staticmethod
    def _profile_cwd(repo: Path, profile: ValidationProfile) -> Path | None:
        cwd = (repo / profile.cwd).resolve(strict=False)
        if cwd != repo and repo not in cwd.parents:
            raise ValueError(
                f"validation profile cwd escapes repository: {profile.profile_id}"
            )
        return cwd if cwd.is_dir() else None

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
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            return Verifier._os_error_result(command_id, exc, started)
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            Verifier._terminate_process_group(process)
            stdout, stderr = process.communicate()
            exit_code = 124
        duration_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
        digest = hashlib.sha256()
        digest.update(b"stdout\0")
        digest.update(stdout or b"")
        digest.update(b"\0stderr\0")
        digest.update(stderr or b"")
        return ValidationResult(
            command_id=command_id,
            exit_code=exit_code,
            output_sha256=digest.hexdigest(),
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        # ``communicate`` can time out after the direct child has already
        # exited when one of its descendants still owns the captured pipes.
        # The process-group ID remains valid in that case, so always target
        # the group instead of treating a reaped leader as proof that the
        # validation tree is gone.
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

    @staticmethod
    def _os_error_result(
        command_id: str,
        error: OSError,
        started: int,
    ) -> ValidationResult:
        reason = f"{error.__class__.__name__}:{error.errno}".encode(
            "ascii",
            "replace",
        )
        return ValidationResult(
            command_id=command_id,
            exit_code=127,
            output_sha256=hashlib.sha256(reason).hexdigest(),
            duration_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )

    @staticmethod
    def _synthetic_failure(command_id: str, reason: str) -> ValidationResult:
        return ValidationResult(
            command_id=command_id,
            exit_code=127,
            output_sha256=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            duration_ms=0,
        )
