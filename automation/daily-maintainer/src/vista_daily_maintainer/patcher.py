from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from . import candidate as candidate_contract
from .candidate import (
    Backlog,
    BacklogTrust,
    Candidate,
    CandidateContractError,
    load_trusted_backlog,
    path_matches_pattern,
)
from .profiles import BUILTIN_VALIDATION_PROFILES


# Prompt/schema hashes are updated with the files in the same reviewed commit.
PATCHER_PROMPT_SHA256 = (
    "88e2ecb639f9466fbbabeab71d74348c8b3705a03b84b1f67de8e4eff0a1598e"
)
PATCHER_OUTPUT_SCHEMA_SHA256 = (
    "b41c1cba4171999b7f9bd907811e5f153e812a48897117118432cbc511809a5e"
)
PATCHER_MODEL = "gpt-5.6-sol"
PATCHER_REASONING_EFFORT = "ultra"
PATCHER_REPOSITORY = "IvesLiu1026/VISTA-World"
PATCHER_PERMISSION_PROFILE = "vista-daily-patcher"
PINNED_CODEX_VERSION = "codex-cli 0.144.4"
PINNED_CODEX_SHA256 = "2b3edc9cdfd1717fba3dbc92817205a8a2c7511d459e456d4817eeff6f78ed7a"
PINNED_GIT_VERSION = "git version 2.34.1"
PINNED_GIT_SHA256 = "587ef21868c948b883993e23209b86a72a6ddc06aab1545c697ffc31075acd4a"
MAX_PATCHER_PROMPT_BYTES = 64 * 1024
MAX_CONTROL_MANIFEST_BYTES = 64 * 1024
MAX_PATCHER_OUTPUT_BYTES = 64 * 1024
MAX_CREDENTIAL_BYTES = 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 1024 * 1024
MAX_TRACKED_FILE_BYTES = 64 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 15.0
CONTROL_OWNER_UID = 0
CODEX_CREDENTIAL_NAME = "auth.json"

APPROVED_PUBLIC_REPOSITORY_AUTH_KINDS = frozenset(
    {"api_key", "codex_access_token", "workload_identity"}
)
DISABLED_CODEX_FEATURES = (
    "apps",
    "artifact",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode",
    "code_mode_host",
    "code_mode_only",
    "computer_use",
    "current_time_reminder",
    "default_mode_request_user_input",
    "enable_mcp_apps",
    "enable_fanout",
    "goals",
    "guardian_approval",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "multi_agent_v2",
    "network_proxy",
    "plugin_sharing",
    "plugins",
    "realtime_conversation",
    "remote_plugin",
    "request_permissions_tool",
    "skill_mcp_dependency_install",
    "standalone_web_search",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unavailable_dummy_tools",
    "workspace_dependencies",
)
ALLOWED_PATCHER_TOOL_SURFACES = ("apply_patch", "local_shell")

_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_NAMESPACE_ID = re.compile(r"^(?:mnt|net|pid|user):\[[1-9][0-9]*\]$")
_CGROUP = re.compile(r"^/[A-Za-z0-9_.@:/-]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")

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
_FIXED_GIT_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.trustctime=true",
    "-c",
    "core.checkStat=default",
    "-c",
    "core.ignoreStat=false",
    "-c",
    "core.fileMode=true",
    "-c",
    "core.sparseCheckout=false",
    "-c",
    "core.sparseCheckoutCone=false",
    "-c",
    "index.sparse=false",
    "-c",
    "submodule.recurse=false",
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
_UNSAFE_LOCAL_GIT_CONFIG_PREFIXES = (
    "credential.",
    "filter.",
    "http.",
    "include.",
    "includeif.",
    "protocol.",
    "submodule.",
    "url.",
)
_UNSAFE_LOCAL_GIT_CONFIG_KEYS = frozenset(
    {
        "core.fsmonitor",
        "core.gitproxy",
        "core.hookspath",
        "core.sshcommand",
        "diff.external",
        "interactive.difffilter",
        "ssh.variant",
    }
)
_PINNED_LOCAL_GIT_CONFIG = MappingProxyType(
    {
        "core.checkstat": "default",
        "core.filemode": "true",
        "core.ignorestat": "false",
        "core.sparsecheckout": "false",
        "core.sparsecheckoutcone": "false",
        "core.trustctime": "true",
        "index.sparse": "false",
        "submodule.recurse": "false",
    }
)
_OUTPUT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "summary",
        "paths_considered",
        "blocker_category",
    }
)
_BLOCKERS = frozenset(
    {
        "finding_not_reproduced",
        "protected_surface_required",
        "allowlist_insufficient",
        "validation_unavailable",
        "safety_uncertain",
    }
)
_BLOCKED_ONLY = _BLOCKERS - {"finding_not_reproduced"}


class PatcherContractError(ValueError):
    """The patcher boundary is incomplete or unsafe."""


@dataclass(frozen=True)
class FileEvidence:
    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    owner_uid: int
    mode: int
    sha256: str
    link_count: int = 1

    def stable_identity(self) -> tuple[int, int, int, int]:
        return (self.device, self.inode, self.owner_uid, self.link_count)


def candidate_authorization_payload(candidate: Candidate) -> dict[str, object]:
    """Canonical backlog authority, including fields omitted from model input."""

    if not isinstance(candidate, Candidate):
        raise PatcherContractError("candidate must use the strict contract")
    return {
        "schema_version": "vista.world.daily-maintainer.candidate-authority.v1",
        "candidate": candidate.normalized_payload(),
        "state": candidate.state,
        "not_before": (
            candidate.not_before.isoformat() if candidate.not_before else None
        ),
        "expires_on": (
            candidate.expires_on.isoformat() if candidate.expires_on else None
        ),
        "source": {
            "kind": candidate.source.kind,
            "manifest_revision": candidate.source.manifest_revision,
            "approved_by": candidate.source.approved_by,
            "issue_url": candidate.source.issue_url,
        },
    }


def candidate_authorization_digest(candidate: Candidate) -> str:
    payload = json.dumps(
        candidate_authorization_payload(candidate),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class PatcherRequest:
    run_date: dt.date
    repository: str
    base_sha: str
    backlog_sha256: str
    manifest_revision: int
    approved_by: str
    candidate: Candidate
    candidate_sha256: str

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
        if (
            isinstance(self.manifest_revision, bool)
            or not isinstance(self.manifest_revision, int)
            or self.manifest_revision < 1
        ):
            raise PatcherContractError("manifest revision must be positive")
        if not isinstance(self.candidate, Candidate):
            raise PatcherContractError("patcher candidate must use the strict contract")
        if not self.candidate.eligible_on(self.run_date):
            raise PatcherContractError("candidate is not eligible on the run date")
        if self.candidate.risk_tier not in {0, 1}:
            raise PatcherContractError(
                "patcher accepts only Tier 0 or Tier 1 candidates"
            )
        if self.candidate.source.manifest_revision != self.manifest_revision:
            raise PatcherContractError("candidate revision does not match the request")
        if self.candidate.source.approved_by != self.approved_by:
            raise PatcherContractError("candidate approver does not match the request")
        unknown_profiles = sorted(
            set(self.candidate.validation_profiles) - BUILTIN_VALIDATION_PROFILES.ids
        )
        if unknown_profiles:
            raise PatcherContractError(
                "candidate references an unknown validation profile: "
                + ", ".join(unknown_profiles)
            )
        if not isinstance(
            self.candidate_sha256, str
        ) or self.candidate_sha256 != candidate_authorization_digest(self.candidate):
            raise PatcherContractError("candidate authority digest mismatch")

    def normalized_payload(self) -> dict[str, object]:
        return {
            "schema_version": "vista.world.daily-maintainer.patcher-input.v1",
            "run_date": self.run_date.isoformat(),
            "repository": self.repository,
            "base_sha": self.base_sha,
            "backlog_sha256": self.backlog_sha256,
            "manifest_revision": self.manifest_revision,
            "approved_by": self.approved_by,
            "candidate_sha256": self.candidate_sha256,
            "candidate": self.candidate.normalized_payload(),
        }


@dataclass(frozen=True)
class TrustedBinary:
    path: Path
    sha256: str
    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise PatcherContractError("trusted binary path must be absolute")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise PatcherContractError("trusted binary digest must be SHA-256")
        if (
            not isinstance(self.version, str)
            or not self.version
            or "\n" in self.version
            or "\r" in self.version
        ):
            raise PatcherContractError("trusted binary version is invalid")


@dataclass(frozen=True)
class CredentialBinding:
    auth_kind: str
    binding_id: str
    credential_path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str

    def __post_init__(self) -> None:
        if self.auth_kind not in APPROVED_PUBLIC_REPOSITORY_AUTH_KINDS:
            raise PatcherContractError(
                "public-repository automation auth kind is not approved"
            )
        if not isinstance(self.binding_id, str) or not _IDENTIFIER.fullmatch(
            self.binding_id
        ):
            raise PatcherContractError("credential binding ID is invalid")
        if (
            not isinstance(self.credential_path, Path)
            or not self.credential_path.is_absolute()
        ):
            raise PatcherContractError("credential path must be absolute")
        for value in (self.device, self.inode):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PatcherContractError("credential inode binding is invalid")
        for value, label in (
            (self.size, "credential size"),
            (self.mtime_ns, "credential modification time"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PatcherContractError(f"{label} binding is invalid")
        if self.size > MAX_CREDENTIAL_BYTES:
            raise PatcherContractError("credential exceeds the size limit")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise PatcherContractError("credential digest must be SHA-256")


@dataclass(frozen=True)
class KernelBoundary:
    mount_namespace: str
    network_namespace: str
    pid_namespace: str
    user_namespace: str
    cgroup_path: str
    supplementary_gids: tuple[int, ...]

    def __post_init__(self) -> None:
        values = (
            (self.mount_namespace, "mnt"),
            (self.network_namespace, "net"),
            (self.pid_namespace, "pid"),
            (self.user_namespace, "user"),
        )
        for value, prefix in values:
            if (
                not isinstance(value, str)
                or not _NAMESPACE_ID.fullmatch(value)
                or not value.startswith(prefix + ":[")
            ):
                raise PatcherContractError("kernel namespace binding is invalid")
        if (
            not isinstance(self.cgroup_path, str)
            or self.cgroup_path == "/"
            or not _CGROUP.fullmatch(self.cgroup_path)
        ):
            raise PatcherContractError("dedicated cgroup path is invalid")
        if (
            not isinstance(self.supplementary_gids, tuple)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in self.supplementary_gids
            )
            or tuple(sorted(set(self.supplementary_gids))) != self.supplementary_gids
        ):
            raise PatcherContractError("supplementary group binding is invalid")


@dataclass(frozen=True)
class DeploymentManifest:
    control_path: Path
    control_evidence: FileEvidence
    repository: str
    expected_origin: str
    patcher_uid: int
    operator_uid: int
    patcher_gid: int
    operator_gid: int
    worktree_root: Path
    runtime_home: Path
    codex_home: Path
    state_root: Path
    scratch_root: Path
    policy_root: Path
    backlog_path: Path
    backlog_sha256: str
    manifest_revision: int
    approved_by: str
    requirements_path: Path
    requirements_sha256: str
    permission_profile: str
    credential: CredentialBinding
    codex_binary: TrustedBinary
    git_binary: TrustedBinary
    trusted_path: tuple[Path, ...]
    kernel: KernelBoundary


@dataclass(frozen=True)
class PatcherInvocation:
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    stdin: bytes
    final_output_path: Path
    output_evidence: FileEvidence
    control_evidence: tuple[FileEvidence, ...]
    boundary_sha256: str
    request: PatcherRequest
    deployment: DeploymentManifest

    def __post_init__(self) -> None:
        if not isinstance(self.request, PatcherRequest) or not isinstance(
            self.deployment, DeploymentManifest
        ):
            raise PatcherContractError("patcher invocation authority is invalid")
        if not self.argv or not Path(self.argv[0]).is_absolute():
            raise PatcherContractError("patcher executable must be an absolute path")
        if any(not isinstance(value, str) or "\x00" in value for value in self.argv):
            raise PatcherContractError("patcher argv contains an invalid value")
        if not isinstance(self.cwd, Path) or not self.cwd.is_absolute():
            raise PatcherContractError("patcher cwd must be absolute")
        if (
            not isinstance(self.final_output_path, Path)
            or not self.final_output_path.is_absolute()
            or self.output_evidence.path != self.final_output_path
        ):
            raise PatcherContractError("patcher output binding is invalid")
        if (
            not isinstance(self.stdin, bytes)
            or len(self.stdin) > MAX_PATCHER_PROMPT_BYTES
        ):
            raise PatcherContractError("patcher stdin binding is invalid")
        if not isinstance(self.boundary_sha256, str) or not _SHA256.fullmatch(
            self.boundary_sha256
        ):
            raise PatcherContractError("patcher boundary digest is invalid")
        if not isinstance(self.control_evidence, tuple) or any(
            not isinstance(item, FileEvidence) for item in self.control_evidence
        ):
            raise PatcherContractError("patcher control evidence is invalid")
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


def _strict_mapping(
    value: object, *, required: frozenset[str], label: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PatcherContractError(f"{label} must be a string-keyed object")
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required)
    if missing or unknown:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unknown:
            detail.append("unknown " + ", ".join(unknown))
        raise PatcherContractError(f"{label} fields are invalid: {'; '.join(detail)}")
    return value


def _absolute_path(value: object, label: str) -> Path:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or _CONTROL.search(value)
    ):
        raise PatcherContractError(f"{label} must be an absolute safe path")
    path = Path(value)
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise PatcherContractError(f"{label} cannot traverse")
    return path


def _positive_integer(value: object, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PatcherContractError(f"{label} must be an integer >= {minimum}")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PatcherContractError(f"{label} must be SHA-256")
    return value


def _assert_no_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise PatcherContractError(f"path is unavailable: {path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise PatcherContractError(f"trusted path contains a symlink: {path}")


def _assert_trusted_ancestors(path: Path, *, owner_uid: int) -> None:
    _assert_no_symlink_ancestors(path)
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        info = current.stat()
        if info.st_uid != owner_uid or info.st_mode & 0o022:
            raise PatcherContractError(
                f"trusted ancestor is not control-owned and read-only: {current}"
            )


def _read_stable_file(
    path: Path,
    *,
    owner_uid: int,
    expected_sha256: str | None = None,
    max_bytes: int = MAX_CONTROL_MANIFEST_BYTES,
    require_executable: bool = False,
    capture_payload: bool = True,
) -> tuple[bytes, FileEvidence]:
    if not isinstance(path, Path) or not path.is_absolute():
        raise PatcherContractError("trusted file path must be absolute")
    _assert_no_symlink_ancestors(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PatcherContractError(f"trusted file is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PatcherContractError("trusted file must be regular")
        if before.st_uid != owner_uid or before.st_mode & 0o022:
            raise PatcherContractError("trusted file is not control-owned/read-only")
        if require_executable and not before.st_mode & 0o111:
            raise PatcherContractError("trusted binary is not executable")
        if before.st_size > max_bytes:
            raise PatcherContractError("trusted file exceeds the size limit")
        chunks: list[bytes] = []
        hasher = hashlib.sha256()
        total = 0
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
            if capture_payload:
                chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks) if capture_payload else b""
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if total > max_bytes:
        raise PatcherContractError("trusted file exceeds the size limit")
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_mode,
        before.st_uid,
        before.st_nlink,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_mode,
        after.st_uid,
        after.st_nlink,
    )
    if before_identity != after_identity:
        raise PatcherContractError("trusted file changed while it was read")
    digest = hasher.hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise PatcherContractError("trusted file digest mismatch")
    return payload, FileEvidence(
        path=path,
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        owner_uid=after.st_uid,
        mode=stat.S_IMODE(after.st_mode),
        sha256=digest,
        link_count=after.st_nlink,
    )


def _parse_control_manifest(
    payload: bytes, *, path: Path, evidence: FileEvidence
) -> DeploymentManifest:
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatcherContractError(
            "control manifest must be strict UTF-8 JSON"
        ) from exc
    root = _strict_mapping(
        raw,
        required=frozenset(
            {
                "schema_version",
                "repository",
                "expected_origin",
                "patcher_uid",
                "operator_uid",
                "patcher_gid",
                "operator_gid",
                "paths",
                "backlog",
                "auth",
                "binaries",
                "kernel",
                "permission_profile",
            }
        ),
        label="control manifest",
    )
    if root["schema_version"] != "vista.world.daily-maintainer.deployment.v1":
        raise PatcherContractError("unsupported control manifest schema")
    if root["repository"] != PATCHER_REPOSITORY:
        raise PatcherContractError("control manifest repository is not canonical")
    expected_origin = root["expected_origin"]
    if expected_origin not in {
        "git@github.com:IvesLiu1026/VISTA-World.git",
        "https://github.com/IvesLiu1026/VISTA-World.git",
    }:
        raise PatcherContractError("control manifest origin is not canonical")
    paths = _strict_mapping(
        root["paths"],
        required=frozenset(
            {
                "worktree_root",
                "runtime_home",
                "codex_home",
                "state_root",
                "scratch_root",
                "policy_root",
                "requirements_path",
            }
        ),
        label="control paths",
    )
    backlog = _strict_mapping(
        root["backlog"],
        required=frozenset({"path", "sha256", "manifest_revision", "approved_by"}),
        label="backlog binding",
    )
    auth = _strict_mapping(
        root["auth"],
        required=frozenset(
            {
                "kind",
                "binding_id",
                "credential_path",
                "device",
                "inode",
                "size",
                "mtime_ns",
                "sha256",
            }
        ),
        label="auth binding",
    )
    binaries = _strict_mapping(
        root["binaries"],
        required=frozenset({"codex", "git", "trusted_path"}),
        label="binary bindings",
    )
    codex = _strict_mapping(
        binaries["codex"],
        required=frozenset({"path", "sha256", "version"}),
        label="Codex binding",
    )
    git = _strict_mapping(
        binaries["git"],
        required=frozenset({"path", "sha256", "version"}),
        label="Git binding",
    )
    trusted_path_raw = binaries["trusted_path"]
    if (
        not isinstance(trusted_path_raw, list)
        or not trusted_path_raw
        or len(trusted_path_raw) > 8
    ):
        raise PatcherContractError("trusted PATH must be a bounded non-empty list")
    trusted_path = tuple(
        _absolute_path(item, "trusted PATH entry") for item in trusted_path_raw
    )
    kernel = _strict_mapping(
        root["kernel"],
        required=frozenset(
            {
                "mount_namespace",
                "network_namespace",
                "pid_namespace",
                "user_namespace",
                "cgroup_path",
                "supplementary_gids",
            }
        ),
        label="kernel binding",
    )
    gids = kernel["supplementary_gids"]
    if not isinstance(gids, list):
        raise PatcherContractError("supplementary groups must be a list")
    permission = _strict_mapping(
        root["permission_profile"],
        required=frozenset({"name", "requirements_sha256"}),
        label="permission profile binding",
    )
    if permission["name"] != PATCHER_PERMISSION_PROFILE:
        raise PatcherContractError("unexpected patcher permission profile")

    codex_binary = TrustedBinary(
        path=_absolute_path(codex["path"], "Codex binary"),
        sha256=_sha256(codex["sha256"], "Codex binary digest"),
        version=codex["version"],  # type: ignore[arg-type]
    )
    if (
        codex_binary.sha256 != PINNED_CODEX_SHA256
        or codex_binary.version != PINNED_CODEX_VERSION
    ):
        raise PatcherContractError("Codex binary is not the code-pinned build")
    git_binary = TrustedBinary(
        path=_absolute_path(git["path"], "Git binary"),
        sha256=_sha256(git["sha256"], "Git binary digest"),
        version=git["version"],  # type: ignore[arg-type]
    )
    if (
        git_binary.sha256 != PINNED_GIT_SHA256
        or git_binary.version != PINNED_GIT_VERSION
    ):
        raise PatcherContractError("Git binary is not the code-pinned build")
    deployment = DeploymentManifest(
        control_path=path,
        control_evidence=evidence,
        repository=PATCHER_REPOSITORY,
        expected_origin=expected_origin,  # type: ignore[arg-type]
        patcher_uid=_positive_integer(root["patcher_uid"], "patcher UID"),
        operator_uid=_positive_integer(root["operator_uid"], "operator UID"),
        patcher_gid=_positive_integer(root["patcher_gid"], "patcher GID"),
        operator_gid=_positive_integer(root["operator_gid"], "operator GID"),
        worktree_root=_absolute_path(paths["worktree_root"], "worktree root"),
        runtime_home=_absolute_path(paths["runtime_home"], "runtime home"),
        codex_home=_absolute_path(paths["codex_home"], "Codex home"),
        state_root=_absolute_path(paths["state_root"], "state root"),
        scratch_root=_absolute_path(paths["scratch_root"], "scratch root"),
        policy_root=_absolute_path(paths["policy_root"], "policy root"),
        backlog_path=_absolute_path(backlog["path"], "backlog path"),
        backlog_sha256=_sha256(backlog["sha256"], "backlog digest"),
        manifest_revision=_positive_integer(
            backlog["manifest_revision"], "manifest revision"
        ),
        approved_by=backlog["approved_by"],  # type: ignore[arg-type]
        requirements_path=_absolute_path(
            paths["requirements_path"], "managed requirements path"
        ),
        requirements_sha256=_sha256(
            permission["requirements_sha256"], "managed requirements digest"
        ),
        permission_profile=PATCHER_PERMISSION_PROFILE,
        credential=CredentialBinding(
            auth_kind=auth["kind"],  # type: ignore[arg-type]
            binding_id=auth["binding_id"],  # type: ignore[arg-type]
            credential_path=_absolute_path(auth["credential_path"], "credential path"),
            device=_positive_integer(auth["device"], "credential device"),
            inode=_positive_integer(auth["inode"], "credential inode"),
            size=_positive_integer(auth["size"], "credential size"),
            mtime_ns=_positive_integer(
                auth["mtime_ns"], "credential modification time"
            ),
            sha256=_sha256(auth["sha256"], "credential digest"),
        ),
        codex_binary=codex_binary,
        git_binary=git_binary,
        trusted_path=trusted_path,
        kernel=KernelBoundary(
            mount_namespace=kernel["mount_namespace"],  # type: ignore[arg-type]
            network_namespace=kernel["network_namespace"],  # type: ignore[arg-type]
            pid_namespace=kernel["pid_namespace"],  # type: ignore[arg-type]
            user_namespace=kernel["user_namespace"],  # type: ignore[arg-type]
            cgroup_path=kernel["cgroup_path"],  # type: ignore[arg-type]
            supplementary_gids=tuple(gids),  # type: ignore[arg-type]
        ),
    )
    expected_credential_path = deployment.codex_home / CODEX_CREDENTIAL_NAME
    if deployment.credential.credential_path != expected_credential_path:
        raise PatcherContractError(
            "credential path is not the code-owned Codex credential location"
        )
    if deployment.requirements_path != Path("/etc/codex/requirements.toml"):
        raise PatcherContractError(
            "managed requirements path is not the code-owned Codex location"
        )
    return deployment


def load_control_manifest(path: Path) -> DeploymentManifest:
    """Load authority only from an immutable root-owned deployment manifest."""

    _assert_trusted_ancestors(path, owner_uid=CONTROL_OWNER_UID)
    payload, evidence = _read_stable_file(path, owner_uid=CONTROL_OWNER_UID)
    return _parse_control_manifest(payload, path=path, evidence=evidence)


def _absolute_directory(path: Path, *, owner_uid: int, private: bool) -> Path:
    _assert_no_symlink_ancestors(path)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise PatcherContractError(
            f"isolation directory is unavailable: {path}"
        ) from exc
    if resolved != path or not resolved.is_dir():
        raise PatcherContractError("isolation path must be a canonical directory")
    if info.st_uid != owner_uid or info.st_mode & 0o022:
        raise PatcherContractError("isolation directory has unsafe ownership or mode")
    if private and info.st_mode & 0o077:
        raise PatcherContractError("private isolation directory is not mode 0700")
    return resolved


def _verify_credential(
    binding: CredentialBinding, *, patcher_uid: int, codex_home: Path
) -> FileEvidence:
    path = binding.credential_path
    if path != codex_home / CODEX_CREDENTIAL_NAME:
        raise PatcherContractError(
            "credential path is not the code-owned Codex credential location"
        )
    _, evidence = _read_stable_file(
        path,
        owner_uid=patcher_uid,
        expected_sha256=binding.sha256,
        max_bytes=MAX_CREDENTIAL_BYTES,
        capture_payload=False,
    )
    if (
        evidence.mode != 0o600
        or evidence.link_count != 1
        or (evidence.device, evidence.inode) != (binding.device, binding.inode)
        or evidence.size != binding.size
        or evidence.mtime_ns != binding.mtime_ns
    ):
        raise PatcherContractError("credential content/metadata binding failed")
    return evidence


def _verify_binary(binary: TrustedBinary) -> FileEvidence:
    _assert_trusted_ancestors(binary.path, owner_uid=CONTROL_OWNER_UID)
    _, evidence = _read_stable_file(
        binary.path,
        owner_uid=CONTROL_OWNER_UID,
        expected_sha256=binary.sha256,
        max_bytes=512 * 1024 * 1024,
        require_executable=True,
        capture_payload=False,
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    try:
        result = subprocess.run(
            (str(binary.path), "--version"),
            cwd="/",
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PatcherContractError("trusted binary version probe failed") from exc
    output = result.stdout[:1024].decode("utf-8", "replace").strip()
    if result.returncode != 0 or output != binary.version:
        raise PatcherContractError("trusted binary version binding failed")
    return evidence


def _proc_status() -> Mapping[str, str]:
    values: dict[str, str] = {}
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = value.strip()
    return MappingProxyType(values)


def _namespace(name: str) -> str:
    try:
        value = os.readlink(f"/proc/self/ns/{name}")
    except OSError as exc:
        raise PatcherContractError("kernel namespace evidence is unavailable") from exc
    return f"{name}:{value[value.index('[') :]}" if "[" in value else value


def _current_cgroup() -> str:
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise PatcherContractError("cgroup evidence is unavailable") from exc
    unified = [line.split(":", 2)[2] for line in lines if line.startswith("0::")]
    if len(unified) != 1:
        raise PatcherContractError("a dedicated cgroup-v2 boundary is required")
    return unified[0]


def _verify_kernel_boundary(deployment: DeploymentManifest) -> None:
    if os.geteuid() != deployment.patcher_uid or deployment.patcher_uid <= 0:
        raise PatcherContractError("effective UID is not the dedicated patcher UID")
    if os.getegid() != deployment.patcher_gid:
        raise PatcherContractError("effective GID is not the dedicated patcher GID")
    if (
        deployment.patcher_uid == deployment.operator_uid
        or deployment.patcher_gid == deployment.operator_gid
    ):
        raise PatcherContractError("patcher and operator identities must be distinct")
    status = _proc_status()
    try:
        uids = tuple(int(value) for value in status["Uid"].split())
        gids = tuple(int(value) for value in status["Gid"].split())
        groups = tuple(sorted(int(value) for value in status["Groups"].split()))
    except (KeyError, ValueError) as exc:
        raise PatcherContractError("process credential evidence is malformed") from exc
    if uids != (deployment.patcher_uid,) * 4:
        raise PatcherContractError("real/saved/filesystem UIDs are not pinned")
    if gids != (deployment.patcher_gid,) * 4:
        raise PatcherContractError("real/saved/filesystem GIDs are not pinned")
    if groups != deployment.kernel.supplementary_gids:
        raise PatcherContractError("supplementary groups are not pinned")
    if status.get("CapEff") != "0000000000000000":
        raise PatcherContractError("patcher process retains effective capabilities")
    if status.get("NoNewPrivs") != "1":
        raise PatcherContractError("NoNewPrivileges is not active")
    if status.get("Seccomp") != "2":
        raise PatcherContractError("seccomp filter mode is not active")
    actual_namespaces = (
        _namespace("mnt"),
        _namespace("net"),
        _namespace("pid"),
        _namespace("user"),
    )
    expected_namespaces = (
        deployment.kernel.mount_namespace,
        deployment.kernel.network_namespace,
        deployment.kernel.pid_namespace,
        deployment.kernel.user_namespace,
    )
    if actual_namespaces != expected_namespaces:
        raise PatcherContractError("kernel namespace identity changed")
    if _current_cgroup() != deployment.kernel.cgroup_path:
        raise PatcherContractError("dedicated cgroup identity changed")
    cgroup_fs = Path("/sys/fs/cgroup") / deployment.kernel.cgroup_path.lstrip("/")
    if not cgroup_fs.is_dir() or cgroup_fs == Path("/sys/fs/cgroup"):
        raise PatcherContractError("dedicated cgroup filesystem entry is unavailable")


def _unescape_mount_path(value: str) -> Path:
    for encoded, decoded in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(encoded, decoded)
    return Path(value)


def _mount_evidence_for(path: Path) -> tuple[int, Path, frozenset[str]]:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PatcherContractError("mount namespace evidence is unavailable") from exc
    matches: list[tuple[int, int, Path, frozenset[str]]] = []
    for line in lines:
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        if len(fields) < 6:
            continue
        mountpoint = _unescape_mount_path(fields[4])
        try:
            path.relative_to(mountpoint)
        except ValueError:
            continue
        super_options = after.split()[2].split(",") if len(after.split()) >= 3 else []
        options = frozenset(fields[5].split(",")) | frozenset(super_options)
        matches.append((len(mountpoint.parts), int(fields[0]), mountpoint, options))
    if not matches:
        raise PatcherContractError(f"path has no mount evidence: {path}")
    _, mount_id, mountpoint, options = max(matches, key=lambda item: item[0])
    return mount_id, mountpoint, options


def _verify_mount_boundaries(deployment: DeploymentManifest) -> None:
    policy_mount_id, policy_mountpoint, policy_options = _mount_evidence_for(
        deployment.policy_root
    )
    if policy_mountpoint != deployment.policy_root or "ro" not in policy_options:
        raise PatcherContractError("policy root must be a dedicated read-only mount")
    for path in (deployment.control_path, deployment.backlog_path):
        mount_id, _, options = _mount_evidence_for(path)
        if mount_id != policy_mount_id or "ro" not in options:
            raise PatcherContractError(
                "control manifest/backlog escaped the policy mount"
            )
    _, _, requirements_options = _mount_evidence_for(deployment.requirements_path)
    if "ro" not in requirements_options:
        raise PatcherContractError("managed requirements are not read-only mounted")
    writable_paths = (
        deployment.worktree_root,
        deployment.runtime_home,
        deployment.codex_home,
        deployment.state_root,
        deployment.scratch_root,
    )
    writable_mount_ids: set[int] = set()
    for path in writable_paths:
        mount_id, mountpoint, options = _mount_evidence_for(path)
        if mountpoint != path or "rw" not in options:
            raise PatcherContractError(
                f"patcher path is not a dedicated writable mount: {path}"
            )
        if mount_id in writable_mount_ids:
            raise PatcherContractError("patcher writable mounts are not distinct")
        writable_mount_ids.add(mount_id)


def _verify_managed_requirements(payload: bytes) -> None:
    try:
        parsed = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PatcherContractError("managed requirements are not valid TOML") from exc
    if parsed.get("default_permissions") != PATCHER_PERMISSION_PROFILE:
        raise PatcherContractError("managed default permission profile is not pinned")
    if parsed.get("allowed_permission_profiles") != {PATCHER_PERMISSION_PROFILE: True}:
        raise PatcherContractError("managed permission profile allowlist is not exact")
    if parsed.get("allowed_approval_policies") != ["never"]:
        raise PatcherContractError("managed approval policy is not pinned")
    if parsed.get("allowed_web_search_modes") not in ([], ["disabled"]):
        raise PatcherContractError("managed web-search policy is not disabled")
    if parsed.get("allow_login_shell") is not False:
        raise PatcherContractError("managed login-shell policy is not disabled")
    if parsed.get("allow_managed_hooks_only") is not True:
        raise PatcherContractError("managed hook policy is not fail-closed")
    if parsed.get("mcp_servers") != {}:
        raise PatcherContractError("managed MCP allowlist must be empty")
    features = parsed.get("features")
    if not isinstance(features, Mapping):
        raise PatcherContractError("managed feature policy is missing")
    missing_disabled = [
        name for name in DISABLED_CODEX_FEATURES if features.get(name) is not False
    ]
    if missing_disabled:
        raise PatcherContractError(
            "managed feature policy does not disable: " + ", ".join(missing_disabled)
        )


def _git_environment(runtime_home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(runtime_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
    }


def _git(
    git_binary: Path,
    worktree: Path,
    runtime_home: Path,
    *args: str,
) -> str:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            (
                str(git_binary),
                *_FIXED_GIT_CONFIG,
                "-C",
                str(worktree),
                *args,
            ),
            cwd=worktree,
            env=_git_environment(runtime_home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise PatcherContractError("trusted Git command could not start") from exc
    if process.stdout is None:
        _terminate_process_group(process)
        raise PatcherContractError("trusted Git output pipe is unavailable")

    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                raise PatcherContractError("trusted Git command timed out")
            if not selector.select(remaining):
                _terminate_process_group(process)
                raise PatcherContractError("trusted Git command timed out")
            chunk = os.read(process.stdout.fileno(), 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_GIT_OUTPUT_BYTES:
                _terminate_process_group(process)
                raise PatcherContractError("trusted Git output exceeds the size limit")
            chunks.append(chunk)
        remaining = max(0.0, deadline - time.monotonic())
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise PatcherContractError("trusted Git command timed out") from exc
    finally:
        selector.close()
        process.stdout.close()
    if returncode != 0:
        raise PatcherContractError("trusted Git preflight failed")
    try:
        return b"".join(chunks).decode("utf-8", "strict").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise PatcherContractError("trusted Git output is not UTF-8") from exc


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def _reject_unsafe_local_git_config(
    *, git_binary: Path, worktree: Path, runtime_home: Path
) -> None:
    entries: list[tuple[str, str]] = []
    for scope in ("--local", "--worktree"):
        payload = _git(
            git_binary,
            worktree,
            runtime_home,
            "config",
            scope,
            "--no-includes",
            "--null",
            "--list",
        )
        for item in payload.split("\x00"):
            if not item:
                continue
            key, separator, value = item.partition("\n")
            if not separator:
                raise PatcherContractError("local Git configuration is malformed")
            entries.append((key.lower(), value))
    unsafe = sorted(
        key
        for key, value in entries
        if (
            key in _PINNED_LOCAL_GIT_CONFIG
            and value.lower() != _PINNED_LOCAL_GIT_CONFIG[key]
        )
        or (
            key not in _PINNED_LOCAL_GIT_CONFIG
            and (
                key in _UNSAFE_LOCAL_GIT_CONFIG_KEYS
                or any(
                    key.startswith(prefix)
                    for prefix in _UNSAFE_LOCAL_GIT_CONFIG_PREFIXES
                )
                or (key.startswith("diff.") and key.endswith((".command", ".textconv")))
                or key.startswith("merge.")
                and key.endswith(".driver")
            )
        )
    )
    if unsafe:
        raise PatcherContractError("worktree has unsafe local Git configuration")


def _safe_tracked_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith(("/", "\\"))
        or "\\" in value
        or _CONTROL.search(value)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PatcherContractError("Git index contains an unsafe path")
    return value


def _index_entries(payload: str) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for record in payload.split("\x00"):
        if not record:
            continue
        header, separator, raw_path = record.partition("\t")
        fields = header.split(" ")
        if not separator or len(fields) != 3 or fields[2] != "0":
            raise PatcherContractError("Git index contains an unresolved entry")
        mode, object_id, _ = fields
        path = _safe_tracked_path(raw_path)
        if path in entries or mode not in {"100644", "100755"}:
            raise PatcherContractError("Git index contains an unsupported entry")
        if not _OBJECT_ID.fullmatch(object_id):
            raise PatcherContractError("Git index object ID is invalid")
        entries[path] = (mode, object_id)
    return entries


def _tree_entries(payload: str) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for record in payload.split("\x00"):
        if not record:
            continue
        header, separator, raw_path = record.partition("\t")
        fields = header.split(" ")
        if not separator or len(fields) != 3:
            raise PatcherContractError("authorized Git tree output is malformed")
        mode, object_type, object_id = fields
        path = _safe_tracked_path(raw_path)
        if (
            path in entries
            or object_type != "blob"
            or mode not in {"100644", "100755"}
            or not _OBJECT_ID.fullmatch(object_id)
        ):
            raise PatcherContractError(
                "authorized Git tree contains an unsupported entry"
            )
        entries[path] = (mode, object_id)
    return entries


def _reject_index_flags(
    *, git_binary: Path, worktree: Path, runtime_home: Path
) -> None:
    payload = _git(
        git_binary,
        worktree,
        runtime_home,
        "ls-files",
        "-v",
        "-z",
        "--",
    )
    for record in payload.split("\x00"):
        if not record:
            continue
        if len(record) < 3 or record[1] != " ":
            raise PatcherContractError("Git index flag output is malformed")
        tag = record[0]
        if tag == "S" or tag.islower():
            raise PatcherContractError(
                "Git index contains assume-unchanged or skip-worktree state"
            )


def _working_tree_blob_oid(path: Path, *, object_format: str, mode: str) -> str:
    _assert_no_symlink_ancestors(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PatcherContractError("tracked worktree file is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PatcherContractError("tracked worktree path is not a regular file")
        if before.st_size > MAX_TRACKED_FILE_BYTES:
            raise PatcherContractError("tracked worktree file exceeds the size limit")
        executable = bool(before.st_mode & 0o111)
        if executable != (mode == "100755"):
            raise PatcherContractError(
                "tracked worktree file mode differs from the index"
            )
        hasher = hashlib.new(object_format)
        hasher.update(f"blob {before.st_size}\0".encode("ascii"))
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            hasher.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_mode,
        before.st_nlink,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_mode,
        after.st_nlink,
    )
    if total != before.st_size or before_identity != after_identity:
        raise PatcherContractError("tracked worktree file changed while it was read")
    return hasher.hexdigest()


def _verify_index_and_worktree(
    *,
    git_binary: Path,
    worktree: Path,
    runtime_home: Path,
    base_sha: str,
    require_clean: bool,
) -> None:
    _reject_index_flags(
        git_binary=git_binary, worktree=worktree, runtime_home=runtime_home
    )
    index = _index_entries(
        _git(
            git_binary,
            worktree,
            runtime_home,
            "ls-files",
            "--stage",
            "-z",
            "--",
        )
    )
    tree = _tree_entries(
        _git(
            git_binary,
            worktree,
            runtime_home,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            base_sha,
            "--",
        )
    )
    if index != tree:
        raise PatcherContractError("Git index does not match the authorized base tree")
    if not require_clean:
        return
    object_format = _git(
        git_binary,
        worktree,
        runtime_home,
        "rev-parse",
        "--show-object-format",
    )
    if object_format not in {"sha1", "sha256"}:
        raise PatcherContractError("Git object format is unsupported")
    for relative_path, (mode, expected_oid) in index.items():
        actual_oid = _working_tree_blob_oid(
            worktree / relative_path,
            object_format=object_format,
            mode=mode,
        )
        if actual_oid != expected_oid:
            raise PatcherContractError(
                "tracked worktree content differs from the index"
            )
    untracked = _git(
        git_binary,
        worktree,
        runtime_home,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
    )
    if untracked:
        raise PatcherContractError("patcher worktree is not clean")


def _verify_git_checkout(
    *,
    git_binary: Path,
    worktree: Path,
    runtime_home: Path,
    expected_origin: str,
    base_sha: str,
    require_clean: bool = True,
) -> None:
    _reject_unsafe_local_git_config(
        git_binary=git_binary,
        worktree=worktree,
        runtime_home=runtime_home,
    )
    top = _git(git_binary, worktree, runtime_home, "rev-parse", "--show-toplevel")
    if Path(top).resolve(strict=True) != worktree:
        raise PatcherContractError("Git top-level does not match the isolated worktree")
    head = _git(git_binary, worktree, runtime_home, "rev-parse", "--verify", "HEAD")
    if head != base_sha:
        raise PatcherContractError("worktree HEAD does not match the authorized base")
    origin = _git(git_binary, worktree, runtime_home, "remote", "get-url", "origin")
    if origin != expected_origin:
        raise PatcherContractError("worktree origin is not the authorized repository")
    _verify_index_and_worktree(
        git_binary=git_binary,
        worktree=worktree,
        runtime_home=runtime_home,
        base_sha=base_sha,
        require_clean=require_clean,
    )


def _enforce_v1_candidate_policy(candidate: Candidate) -> None:
    enforce = getattr(candidate_contract, "enforce_v1_candidate_policy", None)
    if not callable(enforce):
        raise PatcherContractError(
            "V1 candidate policy API is unavailable; integrate the hardened core first"
        )
    try:
        enforce(candidate)
    except CandidateContractError as exc:
        raise PatcherContractError(
            "candidate is outside the V1 authority envelope"
        ) from exc
    unknown = sorted(
        set(candidate.validation_profiles) - BUILTIN_VALIDATION_PROFILES.ids
    )
    if unknown:
        raise PatcherContractError(
            "candidate references unknown validation profiles: " + ", ".join(unknown)
        )


def _bind_candidate_to_backlog(request: PatcherRequest, backlog: Backlog) -> None:
    if (
        backlog.sha256 != request.backlog_sha256
        or backlog.manifest_revision != request.manifest_revision
        or backlog.approved_by != request.approved_by
    ):
        raise PatcherContractError(
            "request does not match the reviewed backlog identity"
        )
    matches = [
        candidate
        for candidate in backlog.candidates
        if candidate.candidate_id == request.candidate.candidate_id
    ]
    if len(matches) != 1 or matches[0] != request.candidate:
        raise PatcherContractError(
            "candidate is not an exact member of the reviewed backlog"
        )
    if candidate_authorization_digest(matches[0]) != request.candidate_sha256:
        raise PatcherContractError("reviewed candidate digest mismatch")
    _enforce_v1_candidate_policy(matches[0])


def _reserve_output(path: Path, *, owner_uid: int) -> FileEvidence:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise PatcherContractError("patcher output path must be fresh") from exc
    except OSError as exc:
        raise PatcherContractError("patcher output cannot be reserved safely") from exc
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != owner_uid
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        try:
            path.unlink()
        except OSError:
            pass
        raise PatcherContractError("reserved output has unsafe ownership or mode")
    return FileEvidence(
        path=path,
        device=info.st_dev,
        inode=info.st_ino,
        size=0,
        mtime_ns=info.st_mtime_ns,
        owner_uid=info.st_uid,
        mode=0o600,
        sha256=hashlib.sha256(b"").hexdigest(),
        link_count=1,
    )


def _command_environment(deployment: DeploymentManifest) -> dict[str, str]:
    return {
        "PATH": os.pathsep.join(str(path) for path in deployment.trusted_path),
        "HOME": str(deployment.runtime_home),
        "CODEX_HOME": str(deployment.codex_home),
        "XDG_CONFIG_HOME": str(deployment.runtime_home / ".config"),
        "TMPDIR": str(deployment.scratch_root),
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


def _fixed_codex_argv(
    *,
    codex_binary: Path,
    worktree: Path,
    scratch_root: Path,
    schema_path: Path,
    output_path: Path,
    command_environment: Mapping[str, str],
) -> tuple[str, ...]:
    environment_toml = (
        "shell_environment_policy.set={"
        + ",".join(
            f"{key}={json.dumps(value)}"
            for key, value in sorted(command_environment.items())
        )
        + "}"
    )
    profile = PATCHER_PERMISSION_PROFILE
    permission_toml = (
        "permissions={"
        + json.dumps(profile)
        + '={extends=":workspace",workspace_roots={'
        + json.dumps(str(scratch_root))
        + '=true},filesystem={":root"="deny",":minimal"="read",'
        + '":tmpdir"="deny",":slash_tmp"="deny",'
        + '":workspace_roots"={"."="write"}},network={enabled=false}}}'
    )
    configs = (
        permission_toml,
        f"default_permissions={json.dumps(profile)}",
        f'model_reasoning_effort="{PATCHER_REASONING_EFFORT}"',
        'approval_policy="never"',
        "allow_login_shell=false",
        'web_search="disabled"',
        "mcp_servers={}",
        "notify=[]",
        'shell_environment_policy.inherit="none"',
        environment_toml,
        'history.persistence="none"',
        'otel.exporter="none"',
    )
    argv: list[str] = [
        str(codex_binary),
        "exec",
        "-",
        "--strict-config",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        PATCHER_MODEL,
        "--cd",
        str(worktree),
        "--json",
        "--color",
        "never",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
    ]
    for feature in DISABLED_CODEX_FEATURES:
        argv.extend(("--disable", feature))
    for config in configs:
        argv.extend(("--config", config))
    return tuple(argv)


def _boundary_digest(
    request: PatcherRequest,
    deployment: DeploymentManifest,
    evidence: tuple[FileEvidence, ...],
    output_evidence: FileEvidence,
) -> str:
    payload = {
        "request": request.normalized_payload(),
        "deployment_manifest_sha256": deployment.control_evidence.sha256,
        "auth_kind": deployment.credential.auth_kind,
        "auth_binding_id": deployment.credential.binding_id,
        "permission_profile": deployment.permission_profile,
        "kernel": {
            "mount": deployment.kernel.mount_namespace,
            "network": deployment.kernel.network_namespace,
            "pid": deployment.kernel.pid_namespace,
            "user": deployment.kernel.user_namespace,
            "cgroup": deployment.kernel.cgroup_path,
        },
        "files": [
            {
                "path": str(item.path),
                "device": item.device,
                "inode": item.inode,
                "link_count": item.link_count,
                "sha256": item.sha256,
            }
            for item in evidence
        ],
        "reserved_output": {
            "path": str(output_evidence.path),
            "device": output_evidence.device,
            "inode": output_evidence.inode,
            "link_count": output_evidence.link_count,
            "owner_uid": output_evidence.owner_uid,
            "mode": output_evidence.mode,
            "size": output_evidence.size,
            "sha256": output_evidence.sha256,
        },
        "disabled_features": list(DISABLED_CODEX_FEATURES),
        "allowed_tool_surfaces": list(ALLOWED_PATCHER_TOOL_SURFACES),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _expected_output_path(
    request: PatcherRequest, deployment: DeploymentManifest
) -> Path:
    output_name = (
        f"patcher-{request.run_date.isoformat()}-"
        f"{request.candidate.candidate_id.lower()}-{request.base_sha[:8]}.json"
    )
    return deployment.state_root / output_name


def _normalized_patcher_stdin(request: PatcherRequest, prompt_bytes: bytes) -> bytes:
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
    return stdin


def _expected_control_paths(deployment: DeploymentManifest) -> tuple[Path, ...]:
    return (
        deployment.control_path,
        deployment.policy_root / "prompts" / "patcher.md",
        deployment.policy_root / "patcher-output.schema.json",
        deployment.backlog_path,
        deployment.requirements_path,
        deployment.credential.credential_path,
        deployment.codex_binary.path,
        deployment.git_binary.path,
    )


def _assert_static_invocation_binding(invocation: PatcherInvocation) -> None:
    """Rebuild every immutable launch field from reviewed authority."""

    request = invocation.request
    deployment = invocation.deployment
    if (
        request.repository != deployment.repository
        or request.backlog_sha256 != deployment.backlog_sha256
        or request.manifest_revision != deployment.manifest_revision
        or request.approved_by != deployment.approved_by
    ):
        raise PatcherContractError("invocation request is not deployment-authorized")
    expected_paths = _expected_control_paths(deployment)
    if tuple(item.path for item in invocation.control_evidence) != expected_paths:
        raise PatcherContractError("invocation control evidence set is not exact")
    prompt_evidence = invocation.control_evidence[1]
    schema_evidence = invocation.control_evidence[2]
    credential_evidence = invocation.control_evidence[5]
    if (
        prompt_evidence.sha256 != PATCHER_PROMPT_SHA256
        or schema_evidence.sha256 != PATCHER_OUTPUT_SCHEMA_SHA256
        or credential_evidence.sha256 != deployment.credential.sha256
        or (
            credential_evidence.device,
            credential_evidence.inode,
            credential_evidence.size,
            credential_evidence.mtime_ns,
            credential_evidence.owner_uid,
            credential_evidence.mode,
            credential_evidence.link_count,
        )
        != (
            deployment.credential.device,
            deployment.credential.inode,
            deployment.credential.size,
            deployment.credential.mtime_ns,
            deployment.patcher_uid,
            0o600,
            1,
        )
    ):
        raise PatcherContractError("invocation credential/policy evidence is not exact")
    prompt_size = prompt_evidence.size
    if prompt_size < 1 or len(invocation.stdin) < prompt_size:
        raise PatcherContractError("invocation prompt binding is invalid")
    prompt_bytes = invocation.stdin[:prompt_size]
    if hashlib.sha256(prompt_bytes).hexdigest() != PATCHER_PROMPT_SHA256:
        raise PatcherContractError("invocation prompt digest is invalid")
    if invocation.stdin != _normalized_patcher_stdin(request, prompt_bytes):
        raise PatcherContractError("invocation stdin is not the normalized request")
    expected_output = _expected_output_path(request, deployment)
    if (
        invocation.cwd != deployment.worktree_root
        or invocation.final_output_path != expected_output
        or invocation.output_evidence.path != expected_output
    ):
        raise PatcherContractError("invocation worktree/output binding is invalid")
    if (
        invocation.output_evidence.owner_uid != deployment.patcher_uid
        or invocation.output_evidence.mode != 0o600
        or invocation.output_evidence.link_count != 1
        or invocation.output_evidence.size != 0
        or invocation.output_evidence.sha256 != hashlib.sha256(b"").hexdigest()
    ):
        raise PatcherContractError("invocation reserved output evidence is not exact")
    expected_environment = _command_environment(deployment)
    if dict(invocation.environment) != expected_environment:
        raise PatcherContractError("invocation environment is not code-owned")
    expected_argv = _fixed_codex_argv(
        codex_binary=deployment.codex_binary.path,
        worktree=deployment.worktree_root,
        scratch_root=deployment.scratch_root,
        schema_path=deployment.policy_root / "patcher-output.schema.json",
        output_path=expected_output,
        command_environment=expected_environment,
    )
    if invocation.argv != expected_argv:
        raise PatcherContractError("invocation argv is not code-owned")
    expected_boundary = _boundary_digest(
        request,
        deployment,
        invocation.control_evidence,
        invocation.output_evidence,
    )
    if invocation.boundary_sha256 != expected_boundary:
        raise PatcherContractError("invocation boundary digest mismatch")


def build_patcher_invocation(
    request: PatcherRequest,
    control_manifest_path: Path,
) -> PatcherInvocation:
    """Fail closed unless the real kernel and root-owned deployment agree.

    This function reserves but does not execute the output file. The caller must
    revalidate immediately before launch and validate the returned output after
    the process exits. It cannot be activated from an ordinary developer login.
    """

    if not isinstance(request, PatcherRequest):
        raise PatcherContractError("patcher request must use the strict contract")
    deployment = load_control_manifest(control_manifest_path)
    if (
        request.repository != deployment.repository
        or request.backlog_sha256 != deployment.backlog_sha256
        or request.manifest_revision != deployment.manifest_revision
        or request.approved_by != deployment.approved_by
    ):
        raise PatcherContractError("request is not authorized by the control manifest")
    _verify_kernel_boundary(deployment)

    worktree = _absolute_directory(
        deployment.worktree_root, owner_uid=deployment.patcher_uid, private=False
    )
    runtime_home = _absolute_directory(
        deployment.runtime_home, owner_uid=deployment.patcher_uid, private=True
    )
    codex_home = _absolute_directory(
        deployment.codex_home, owner_uid=deployment.patcher_uid, private=True
    )
    state_root = _absolute_directory(
        deployment.state_root, owner_uid=deployment.patcher_uid, private=True
    )
    scratch_root = _absolute_directory(
        deployment.scratch_root, owner_uid=deployment.patcher_uid, private=True
    )
    policy_root = _absolute_directory(
        deployment.policy_root, owner_uid=CONTROL_OWNER_UID, private=False
    )
    roots = (worktree, runtime_home, codex_home, state_root, scratch_root, policy_root)
    if len(set(roots)) != len(roots) or any(
        left in right.parents or right in left.parents
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        raise PatcherContractError(
            "isolation roots must be distinct, non-nested mounts"
        )
    credential_evidence = _verify_credential(
        deployment.credential,
        patcher_uid=deployment.patcher_uid,
        codex_home=codex_home,
    )
    for directory in deployment.trusted_path:
        _assert_trusted_ancestors(directory, owner_uid=CONTROL_OWNER_UID)
        _absolute_directory(directory, owner_uid=CONTROL_OWNER_UID, private=False)
    _verify_mount_boundaries(deployment)

    prompt_path = policy_root / "prompts" / "patcher.md"
    schema_path = policy_root / "patcher-output.schema.json"
    _assert_trusted_ancestors(prompt_path, owner_uid=CONTROL_OWNER_UID)
    _assert_trusted_ancestors(schema_path, owner_uid=CONTROL_OWNER_UID)
    prompt_bytes, prompt_evidence = _read_stable_file(
        prompt_path,
        owner_uid=CONTROL_OWNER_UID,
        expected_sha256=PATCHER_PROMPT_SHA256,
    )
    _, schema_evidence = _read_stable_file(
        schema_path,
        owner_uid=CONTROL_OWNER_UID,
        expected_sha256=PATCHER_OUTPUT_SCHEMA_SHA256,
    )
    backlog_payload, backlog_evidence = _read_stable_file(
        deployment.backlog_path,
        owner_uid=CONTROL_OWNER_UID,
        expected_sha256=deployment.backlog_sha256,
        max_bytes=1024 * 1024,
    )
    _assert_trusted_ancestors(deployment.requirements_path, owner_uid=CONTROL_OWNER_UID)
    requirements_payload, requirements_evidence = _read_stable_file(
        deployment.requirements_path,
        owner_uid=CONTROL_OWNER_UID,
        expected_sha256=deployment.requirements_sha256,
    )
    _verify_managed_requirements(requirements_payload)
    codex_evidence = _verify_binary(deployment.codex_binary)
    git_evidence = _verify_binary(deployment.git_binary)

    trust = BacklogTrust(
        path=deployment.backlog_path,
        sha256=deployment.backlog_sha256,
        manifest_revision=deployment.manifest_revision,
        approved_by=deployment.approved_by,
    )
    try:
        backlog = load_trusted_backlog(trust)
    except CandidateContractError as exc:
        raise PatcherContractError("reviewed backlog failed strict validation") from exc
    # Detect a control-plane race around candidate parsing. The read-only mount is
    # required too; digest equality alone is not considered a mount boundary.
    after_backlog, after_backlog_evidence = _read_stable_file(
        deployment.backlog_path,
        owner_uid=CONTROL_OWNER_UID,
        expected_sha256=deployment.backlog_sha256,
        max_bytes=1024 * 1024,
    )
    if backlog_payload != after_backlog or backlog_evidence != after_backlog_evidence:
        raise PatcherContractError("reviewed backlog changed during authorization")
    _bind_candidate_to_backlog(request, backlog)
    _verify_git_checkout(
        git_binary=deployment.git_binary.path,
        worktree=worktree,
        runtime_home=runtime_home,
        expected_origin=deployment.expected_origin,
        base_sha=request.base_sha,
    )

    stdin = _normalized_patcher_stdin(request, prompt_bytes)

    final_output = _expected_output_path(request, deployment)
    output_evidence = _reserve_output(final_output, owner_uid=deployment.patcher_uid)
    environment = _command_environment(deployment)
    argv = _fixed_codex_argv(
        codex_binary=deployment.codex_binary.path,
        worktree=worktree,
        scratch_root=scratch_root,
        schema_path=schema_path,
        output_path=final_output,
        command_environment=environment,
    )
    control_evidence = (
        deployment.control_evidence,
        prompt_evidence,
        schema_evidence,
        backlog_evidence,
        requirements_evidence,
        credential_evidence,
        codex_evidence,
        git_evidence,
    )
    return PatcherInvocation(
        argv=argv,
        cwd=worktree,
        environment=environment,
        stdin=stdin,
        final_output_path=final_output,
        output_evidence=output_evidence,
        control_evidence=control_evidence,
        boundary_sha256=_boundary_digest(
            request, deployment, control_evidence, output_evidence
        ),
        request=request,
        deployment=deployment,
    )


def _revalidate_file_evidence(
    evidence: FileEvidence, *, allow_size_change: bool
) -> None:
    try:
        info = evidence.path.lstat()
    except OSError as exc:
        raise PatcherContractError("bound file disappeared") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PatcherContractError("bound file is no longer regular")
    if (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_nlink,
    ) != evidence.stable_identity():
        raise PatcherContractError("bound file identity changed")
    if stat.S_IMODE(info.st_mode) != evidence.mode:
        raise PatcherContractError("bound file mode changed")
    if not allow_size_change:
        _, current = _read_stable_file(
            evidence.path,
            owner_uid=evidence.owner_uid,
            expected_sha256=evidence.sha256,
            max_bytes=max(evidence.size, 1),
            require_executable=bool(evidence.mode & 0o111),
            capture_payload=False,
        )
        if current != evidence:
            raise PatcherContractError("bound file evidence changed")


def revalidate_patcher_invocation(
    invocation: PatcherInvocation, *, phase: str = "pre_launch"
) -> None:
    """Re-check mutable kernel/Git/file facts immediately around execution."""

    if not isinstance(invocation, PatcherInvocation):
        raise PatcherContractError("patcher invocation contract is required")
    if phase not in {"pre_launch", "post_run"}:
        raise PatcherContractError("unknown invocation validation phase")
    _assert_static_invocation_binding(invocation)
    fresh_deployment = load_control_manifest(invocation.deployment.control_path)
    if fresh_deployment != invocation.deployment:
        raise PatcherContractError("deployment manifest binding changed")
    _verify_kernel_boundary(invocation.deployment)
    _verify_mount_boundaries(invocation.deployment)
    for evidence in invocation.control_evidence:
        _revalidate_file_evidence(evidence, allow_size_change=False)
    credential_evidence = _verify_credential(
        invocation.deployment.credential,
        patcher_uid=invocation.deployment.patcher_uid,
        codex_home=invocation.deployment.codex_home,
    )
    if credential_evidence != invocation.control_evidence[5]:
        raise PatcherContractError("credential evidence changed")
    requirements_payload, requirements_evidence = _read_stable_file(
        invocation.deployment.requirements_path,
        owner_uid=CONTROL_OWNER_UID,
        expected_sha256=invocation.deployment.requirements_sha256,
    )
    if requirements_evidence != invocation.control_evidence[4]:
        raise PatcherContractError("managed requirements evidence changed")
    _verify_managed_requirements(requirements_payload)
    trust = BacklogTrust(
        path=invocation.deployment.backlog_path,
        sha256=invocation.deployment.backlog_sha256,
        manifest_revision=invocation.deployment.manifest_revision,
        approved_by=invocation.deployment.approved_by,
    )
    try:
        backlog = load_trusted_backlog(trust)
    except CandidateContractError as exc:
        raise PatcherContractError("reviewed backlog failed revalidation") from exc
    _bind_candidate_to_backlog(invocation.request, backlog)
    _verify_git_checkout(
        git_binary=invocation.deployment.git_binary.path,
        worktree=invocation.cwd,
        runtime_home=invocation.deployment.runtime_home,
        expected_origin=invocation.deployment.expected_origin,
        base_sha=invocation.request.base_sha,
        require_clean=phase == "pre_launch",
    )
    _revalidate_file_evidence(
        invocation.output_evidence, allow_size_change=phase == "post_run"
    )
    if phase == "pre_launch" and invocation.final_output_path.stat().st_size != 0:
        raise PatcherContractError("reserved output changed before launch")
    if (
        phase == "post_run"
        and invocation.final_output_path.stat().st_size > MAX_PATCHER_OUTPUT_BYTES
    ):
        raise PatcherContractError("patcher output exceeds the size limit")


def _safe_relative_output_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or value.startswith(("/", "\\"))
        or "\\" in value
        or "//" in value
        or _CONTROL.search(value)
        or not _SAFE_RELATIVE_PATH.fullmatch(value)
    ):
        raise PatcherContractError("patcher output contains an unsafe path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise PatcherContractError("patcher output path cannot traverse")
    return value


def _validate_output_payload(
    payload: object, candidate: Candidate
) -> Mapping[str, object]:
    mapping = _strict_mapping(payload, required=_OUTPUT_FIELDS, label="patcher output")
    if mapping["schema_version"] != "vista.world.daily-maintainer.patcher-output.v1":
        raise PatcherContractError("unsupported patcher output schema")
    status_value = mapping["status"]
    if status_value not in {"changed", "no_change", "blocked"}:
        raise PatcherContractError("patcher output status is invalid")
    summary = mapping["summary"]
    if (
        not isinstance(summary, str)
        or not 1 <= len(summary) <= 500
        or _CONTROL.search(summary)
    ):
        raise PatcherContractError("patcher output summary is invalid")
    raw_paths = mapping["paths_considered"]
    if (
        not isinstance(raw_paths, list)
        or len(raw_paths) > 12
        or any(not isinstance(item, str) for item in raw_paths)
    ):
        raise PatcherContractError("patcher output paths are invalid")
    paths = tuple(_safe_relative_output_path(item) for item in raw_paths)
    if len(set(paths)) != len(paths):
        raise PatcherContractError("patcher output paths must be unique")
    for path in paths:
        if not any(
            path_matches_pattern(path, pattern) for pattern in candidate.allowed_paths
        ):
            raise PatcherContractError(
                "patcher output path exceeds candidate authority"
            )
    blocker = mapping["blocker_category"]
    if blocker is not None and blocker not in _BLOCKERS:
        raise PatcherContractError("patcher output blocker category is invalid")
    if status_value == "changed" and (not paths or blocker is not None):
        raise PatcherContractError("changed output requires paths and no blocker")
    if status_value == "no_change" and blocker not in {
        None,
        "finding_not_reproduced",
    }:
        raise PatcherContractError("no_change output has an incompatible blocker")
    if status_value == "blocked" and blocker not in _BLOCKED_ONLY:
        raise PatcherContractError("blocked output requires an actionable blocker")
    return MappingProxyType(
        {
            "schema_version": mapping["schema_version"],
            "status": status_value,
            "summary": summary,
            "paths_considered": paths,
            "blocker_category": blocker,
        }
    )


def validate_patcher_output(invocation: PatcherInvocation) -> Mapping[str, object]:
    """Validate the reserved model result only after the subprocess exits."""

    revalidate_patcher_invocation(invocation, phase="post_run")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(invocation.final_output_path, flags)
    except OSError as exc:
        raise PatcherContractError("patcher output cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        payload = os.read(descriptor, MAX_PATCHER_OUTPUT_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_PATCHER_OUTPUT_BYTES:
        raise PatcherContractError("patcher output exceeds the size limit")
    if (
        (before.st_dev, before.st_ino, before.st_uid, before.st_nlink)
        != invocation.output_evidence.stable_identity()
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise PatcherContractError("patcher output changed while it was read")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatcherContractError("patcher output is not strict UTF-8 JSON") from exc
    return _validate_output_payload(decoded, invocation.request.candidate)
