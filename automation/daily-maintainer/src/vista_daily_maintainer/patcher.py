from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .candidate import Candidate


PATCHER_PROMPT_SHA256 = (
    "a6b55328e59a017156df3ae8644ec4635181c758466d7c937d118ae9a7076bfd"
)
PATCHER_OUTPUT_SCHEMA_SHA256 = (
    "d65ef05b457c38c2192c4b6eb0db2acce7c9d095578239f963934e579e96fc78"
)
PATCHER_MODEL = "gpt-5.6-sol"
PATCHER_REASONING_EFFORT = "ultra"
PATCHER_REPOSITORY = "IvesLiu1026/VISTA-World"
MAX_PATCHER_PROMPT_BYTES = 64 * 1024

_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_OPENAI_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "CODEX_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "SSH_AUTH_SOCK",
    }
)
_PATCHER_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TZ",
        "NO_COLOR",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT",
        "PYTHONNOUSERSITE",
        "NPM_CONFIG_USERCONFIG",
        "UV_OFFLINE",
        "UV_NO_CONFIG",
    }
)


class PatcherContractError(ValueError):
    """The patcher boundary is incomplete or unsafe."""


@dataclass(frozen=True)
class PatcherRequest:
    run_date: dt.date
    repository: str
    base_sha: str
    backlog_sha256: str
    candidate: Candidate

    def __post_init__(self) -> None:
        if not isinstance(self.run_date, dt.date) or isinstance(
            self.run_date, dt.datetime
        ):
            raise PatcherContractError("run date must be a date")
        if self.repository != PATCHER_REPOSITORY:
            raise PatcherContractError("patcher repository is not the canonical target")
        if not isinstance(self.base_sha, str) or not _OBJECT_ID.fullmatch(
            self.base_sha
        ):
            raise PatcherContractError("base SHA must be an exact object ID")
        if not isinstance(self.backlog_sha256, str) or not _SHA256.fullmatch(
            self.backlog_sha256
        ):
            raise PatcherContractError("backlog digest must be SHA-256")
        if not isinstance(self.candidate, Candidate):
            raise PatcherContractError("patcher candidate must use the strict contract")
        if not self.candidate.eligible_on(self.run_date):
            raise PatcherContractError("candidate is not eligible on the run date")
        if self.candidate.risk_tier not in {0, 1}:
            raise PatcherContractError(
                "patcher accepts only Tier 0 or Tier 1 candidates"
            )

    def normalized_payload(self) -> dict[str, object]:
        return {
            "schema_version": "vista.world.daily-maintainer.patcher-input.v1",
            "run_date": self.run_date.isoformat(),
            "repository": self.repository,
            "base_sha": self.base_sha,
            "backlog_sha256": self.backlog_sha256,
            "candidate": self.candidate.normalized_payload(),
        }


@dataclass(frozen=True)
class IsolationAttestation:
    """Facts the outer service/container must establish before invoking Codex.

    This structure is not a sandbox by itself. The privileged launcher must derive
    these values from its namespace, mounts, UIDs, and policy installation.
    """

    patcher_uid: int
    operator_uid: int
    worktree_owner_uid: int
    credential_owner_uid: int
    worktree_root: Path
    runtime_home: Path
    codex_home: Path
    state_root: Path
    policy_root: Path
    codex_binary: Path
    trusted_path: tuple[Path, ...]
    command_network_isolated: bool
    model_egress_restricted: bool
    policy_read_only_mount: bool
    operator_home_mounted: bool
    publisher_material_mounted: bool
    publisher_socket_exposed: bool

    def validate(self) -> None:
        integer_fields = (
            self.patcher_uid,
            self.operator_uid,
            self.worktree_owner_uid,
            self.credential_owner_uid,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in integer_fields
        ):
            raise PatcherContractError("isolation UIDs must be integers")
        if self.patcher_uid <= 0 or self.patcher_uid == self.operator_uid:
            raise PatcherContractError(
                "patcher must be a distinct non-root Unix identity"
            )
        if self.worktree_owner_uid != self.patcher_uid:
            raise PatcherContractError("patcher must own its isolated worktree")
        if self.credential_owner_uid != self.patcher_uid:
            raise PatcherContractError(
                "patcher credential must be owned by the patcher UID"
            )
        boolean_fields = (
            self.command_network_isolated,
            self.model_egress_restricted,
            self.policy_read_only_mount,
            self.operator_home_mounted,
            self.publisher_material_mounted,
            self.publisher_socket_exposed,
        )
        if any(not isinstance(value, bool) for value in boolean_fields):
            raise PatcherContractError("isolation flags must be booleans")
        if not self.command_network_isolated:
            raise PatcherContractError("patcher commands must have no network access")
        if not self.model_egress_restricted:
            raise PatcherContractError(
                "Codex model transport must use restricted provider egress"
            )
        if not self.policy_read_only_mount:
            raise PatcherContractError("patcher policy must be mounted read-only")
        if (
            self.operator_home_mounted
            or self.publisher_material_mounted
            or self.publisher_socket_exposed
        ):
            raise PatcherContractError(
                "forbidden operator or publisher material is exposed"
            )

        roots = {
            "worktree": _absolute_directory(self.worktree_root),
            "runtime home": _absolute_directory(self.runtime_home),
            "Codex home": _absolute_directory(self.codex_home),
            "state root": _absolute_directory(self.state_root),
            "policy root": _absolute_directory(self.policy_root),
        }
        worktree = roots["worktree"]
        if not (worktree / ".git").exists():
            raise PatcherContractError("patcher worktree is not a Git worktree")
        if worktree.stat().st_uid != self.patcher_uid:
            raise PatcherContractError(
                "patcher UID does not own the worktree directory"
            )
        for label in ("runtime home", "Codex home", "state root", "policy root"):
            path = roots[label]
            if path == worktree or worktree in path.parents or path in worktree.parents:
                raise PatcherContractError(f"{label} must be outside the worktree tree")
        if len(set(roots.values())) != len(roots):
            raise PatcherContractError("isolation roots must be distinct")

        _require_private_directory(roots["runtime home"], self.patcher_uid)
        _require_private_directory(roots["Codex home"], self.patcher_uid)
        _require_private_directory(roots["state root"], self.patcher_uid)
        policy_info = roots["policy root"].stat()
        if policy_info.st_mode & 0o022:
            raise PatcherContractError("installed policy directory is mutable")
        _require_trusted_executable(self.codex_binary, forbidden_owner=self.patcher_uid)
        if not self.trusted_path:
            raise PatcherContractError(
                "trusted PATH must contain at least one directory"
            )
        for directory in self.trusted_path:
            trusted = _absolute_directory(directory)
            info = trusted.stat()
            if info.st_uid == self.patcher_uid or info.st_mode & 0o022:
                raise PatcherContractError(
                    "trusted PATH is writable by the patcher boundary"
                )


@dataclass(frozen=True)
class PatcherInvocation:
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    stdin: bytes
    final_output_path: Path
    policy_prompt_sha256: str
    policy_schema_sha256: str

    def __post_init__(self) -> None:
        if not self.argv or not Path(self.argv[0]).is_absolute():
            raise PatcherContractError("patcher executable must be an absolute path")
        if any(name in self.environment for name in _SECRET_ENV_NAMES):
            raise PatcherContractError("patcher environment contains a credential")
        if set(self.environment) != _PATCHER_ENV_NAMES:
            raise PatcherContractError(
                "patcher environment does not match the fixed allowlist"
            )
        if any(
            not isinstance(value, str) or "\x00" in value or "\n" in value
            for value in self.environment.values()
        ):
            raise PatcherContractError("patcher environment contains an invalid value")
        object.__setattr__(
            self, "environment", MappingProxyType(dict(self.environment))
        )


def _absolute_directory(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise PatcherContractError("isolation path must be absolute")
    try:
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise PatcherContractError("isolation directory is unavailable") from exc
    if not resolved.is_dir():
        raise PatcherContractError("isolation path must be a directory")
    return resolved


def _require_private_directory(path: Path, owner_uid: int) -> None:
    info = path.stat()
    if info.st_uid != owner_uid or info.st_mode & 0o077:
        raise PatcherContractError(
            "private isolation directory has unsafe ownership or mode"
        )


def _require_trusted_executable(path: Path, *, forbidden_owner: int) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise PatcherContractError("Codex executable must be an absolute path")
    try:
        info = path.lstat()
    except OSError as exc:
        raise PatcherContractError("Codex executable is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PatcherContractError(
            "Codex executable must be a regular non-symlink file"
        )
    if (
        info.st_uid == forbidden_owner
        or info.st_mode & 0o022
        or not os.access(path, os.X_OK)
    ):
        raise PatcherContractError("Codex executable is writable or not executable")
    return path.resolve(strict=True)


def _load_policy_file(path: Path, expected_sha256: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise PatcherContractError("installed patcher policy is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o022
    ):
        raise PatcherContractError(
            "installed patcher policy is mutable or not a regular file"
        )
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise PatcherContractError(
            "installed patcher policy digest does not match code"
        )
    return payload


def build_patcher_invocation(
    request: PatcherRequest,
    isolation: IsolationAttestation,
) -> PatcherInvocation:
    """Build a shell-free Codex command after validating the outer boundary."""

    if not isinstance(request, PatcherRequest):
        raise PatcherContractError("patcher request must use the strict contract")
    if not isinstance(isolation, IsolationAttestation):
        raise PatcherContractError("patcher isolation attestation is required")
    isolation.validate()

    worktree = isolation.worktree_root.resolve(strict=True)
    policy_root = isolation.policy_root.resolve(strict=True)
    prompt_bytes = _load_policy_file(
        policy_root / "prompts" / "patcher.md", PATCHER_PROMPT_SHA256
    )
    schema_path = policy_root / "patcher-output.schema.json"
    _load_policy_file(schema_path, PATCHER_OUTPUT_SCHEMA_SHA256)

    normalized = json.dumps(
        request.normalized_payload(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")
    stdin = prompt_bytes + normalized + b"\nEND_NORMALIZED_CANDIDATE\n"
    if len(stdin) > MAX_PATCHER_PROMPT_BYTES:
        raise PatcherContractError("normalized patcher prompt exceeds the byte limit")

    state_root = isolation.state_root.resolve(strict=True)
    final_output = state_root / "patcher-final.json"
    if final_output.exists() or final_output.is_symlink():
        raise PatcherContractError("patcher final output path must not already exist")
    command_environment = {
        "PATH": os.pathsep.join(
            str(path.resolve(strict=True)) for path in isolation.trusted_path
        ),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "NO_COLOR": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONNOUSERSITE": "1",
        "NPM_CONFIG_USERCONFIG": os.devnull,
        "UV_OFFLINE": "1",
        "UV_NO_CONFIG": "1",
    }
    command_environment_toml = (
        "shell_environment_policy.set={"
        + ",".join(
            f"{key}={json.dumps(value)}"
            for key, value in sorted(command_environment.items())
        )
        + "}"
    )
    argv = (
        str(
            _require_trusted_executable(
                isolation.codex_binary, forbidden_owner=isolation.patcher_uid
            )
        ),
        "exec",
        "-",
        "--strict-config",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "workspace-write",
        "--model",
        PATCHER_MODEL,
        "--cd",
        str(worktree),
        "--json",
        "--color",
        "never",
        "--output-schema",
        str(schema_path.resolve(strict=True)),
        "--output-last-message",
        str(final_output),
        "--config",
        f'model_reasoning_effort="{PATCHER_REASONING_EFFORT}"',
        "--config",
        'approval_policy="never"',
        "--config",
        "allow_login_shell=false",
        "--config",
        "sandbox_workspace_write.network_access=false",
        "--config",
        'shell_environment_policy.inherit="none"',
        "--config",
        command_environment_toml,
        "--config",
        'history.persistence="none"',
    )
    environment = {
        **command_environment,
        "HOME": str(isolation.runtime_home.resolve(strict=True)),
        "CODEX_HOME": str(isolation.codex_home.resolve(strict=True)),
        "XDG_CONFIG_HOME": str(isolation.runtime_home.resolve(strict=True) / ".config"),
        "TMPDIR": str(state_root),
    }
    return PatcherInvocation(
        argv=argv,
        cwd=worktree,
        environment=environment,
        stdin=stdin,
        final_output_path=final_output,
        policy_prompt_sha256=PATCHER_PROMPT_SHA256,
        policy_schema_sha256=PATCHER_OUTPUT_SCHEMA_SHA256,
    )
