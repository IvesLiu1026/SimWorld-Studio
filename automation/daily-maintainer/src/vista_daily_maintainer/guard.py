from __future__ import annotations

import hashlib
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from .candidate import (
    Candidate,
    enforce_v1_candidate_policy,
    has_v1_forbidden_authority,
    is_v1_protected_authority_basename,
    is_v1_test_scope,
    path_matches_pattern,
)
from .profiles import TrustedExecutables


_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_PATH_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ASSERTION = re.compile(r"\bassert\b|\bself\.assert[A-Z]|\bexpect\s*\(")
_TEST_SKIP = re.compile(
    r"pytest\.mark\.(?:skip|skipif|xfail)|unittest\.(?:skip|skipIf|skipUnless)|"
    r"\b(?:it|test|describe)\.(?:skip|todo)\s*\(",
    re.IGNORECASE,
)
_TEST_FOCUS = re.compile(
    r"\b(?:context|describe|it|suite|test)\.only\s*\(|"
    r"\b(?:fcontext|fdescribe|fit)\s*\(",
    re.IGNORECASE,
)
_EVIDENCE_NAME = re.compile(
    r"(?:^|[-_.])(?:accepted|artifact|evidence|journal|ledger|receipt|report)"
    r"(?:[-_.]|$)",
)
_RUNTIME_NAME = re.compile(r"(?:^|[-_.])(?:runtime|unreal|ue)(?:[-_.]|$)")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{32,}\b"),
    re.compile(
        r"(?i)(?:^|[^A-Za-z0-9])(?:_authToken|npmAuthToken|_auth)\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9_./+=-]{12,}"
    ),
    re.compile(r"(?i)\bAuthorization\s*[:=]\s*['\"]?Bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9_./+=-]{16,}"
    ),
)
_CREDENTIAL_BASENAMES = frozenset(
    {
        ".authinfo",
        ".authinfo.gpg",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "_netrc",
        "application_default_credentials.json",
        "auth.json",
        "credentials.json",
        "service-account.json",
        "service_account.json",
    }
)
_CREDENTIAL_PATH_SUFFIXES = (
    (".aws", "config"),
    (".aws", "credentials"),
    (".azure", "accesstokens.json"),
    (".config", "gcloud", "application_default_credentials.json"),
    (".config", "gh", "hosts.yaml"),
    (".config", "gh", "hosts.yml"),
    (".config", "glab-cli", "config.yml"),
    (".config", "pypoetry", "auth.toml"),
    (".docker", "config.json"),
)
_BINARY_SUFFIXES = frozenset(
    {
        ".7z",
        ".a",
        ".bin",
        ".blend",
        ".bmp",
        ".dll",
        ".exe",
        ".fbx",
        ".gif",
        ".glb",
        ".gz",
        ".jpeg",
        ".jpg",
        ".mov",
        ".mp3",
        ".mp4",
        ".o",
        ".pak",
        ".pdf",
        ".png",
        ".pyc",
        ".so",
        ".tar",
        ".uasset",
        ".umap",
        ".wav",
        ".webp",
        ".zip",
    }
)
_PROTECTED_TOP_LEVEL = frozenset(
    {
        ".agent",
        ".agents",
        ".claude",
        ".codex",
        ".github",
        "artifacts",
        "assets",
        "datasets",
        "deploy",
        "evidence",
        "infra",
        "infrastructure",
        "ops",
        "outputs",
        "reports",
        "runs",
        "scenes",
        "world-packs",
        "world_packs",
    }
)
_MAX_IGNORED_STATE_BYTES = 256 * 1024
_MAX_IGNORED_STATE_PATHS = 128
_MAX_IGNORED_FILE_HASH_BYTES = 1024 * 1024
_MAX_GIT_STDOUT_BYTES = 16 * 1024 * 1024
_MAX_GIT_STDERR_BYTES = 64 * 1024
_GIT_READ_CHUNK_BYTES = 64 * 1024
_GIT_TIMEOUT_SECONDS = 30.0


class _Digest(Protocol):
    def update(self, value: bytes) -> None: ...


@dataclass(frozen=True)
class GuardLimits:
    max_production_files: int = 3
    max_production_lines: int = 150
    max_test_files: int = 8
    max_test_lines: int = 250
    max_total_files: int = 10
    max_file_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"guard limit {name} must be a positive integer")


@dataclass(frozen=True)
class GuardViolation:
    code: str
    path: str | None
    detail: str


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    additions: int
    deletions: int
    is_test: bool


@dataclass(frozen=True)
class GuardReport:
    base_sha: str
    patch_sha256: str
    changed_files: tuple[ChangedFile, ...]
    violations: tuple[GuardViolation, ...]
    production_files: int
    production_lines: int
    test_files: int
    test_lines: int

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class _IgnoredState:
    present: bool
    paths: tuple[str, ...]
    identity_sha256: bytes
    overflow: bool


@dataclass(frozen=True)
class _GitProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class DiffGuard:
    """Deterministically reject a patch before any candidate test executes."""

    def __init__(self, *, git_executable: Path | None = None) -> None:
        if git_executable is None:
            git_executable = TrustedExecutables.system_defaults().resolve("git")
        self._executables = TrustedExecutables({"git": Path(git_executable)})

    def inspect(
        self,
        repo_root: Path,
        base_sha: str,
        candidate: Candidate,
        *,
        limits: GuardLimits | None = None,
    ) -> GuardReport:
        # Candidate instances are public Python objects, so callers can bypass
        # the reviewed YAML loader.  Re-apply the complete unattended policy at
        # every public trust-boundary entry point.
        enforce_v1_candidate_policy(candidate)
        limits = limits or GuardLimits()
        repo = self._validated_repo(repo_root)
        self._validate_base(repo, base_sha)

        statuses = self._changed_statuses(repo, base_sha)
        untracked = self._untracked_paths(repo)
        for path in untracked:
            statuses.setdefault(path, "?")
        ignored = self._ignored_state(repo)
        numstat = self._numstat(repo, base_sha)
        changed: list[ChangedFile] = []
        violations: list[GuardViolation] = []

        for path in ignored.paths:
            violations.append(
                GuardViolation(
                    "ignored_content",
                    path,
                    "ignored worktree content is forbidden during validation",
                )
            )
        if ignored.present and not ignored.paths:
            violations.append(
                GuardViolation(
                    "ignored_content",
                    None,
                    "ignored worktree content is forbidden during validation",
                )
            )
        if ignored.overflow:
            violations.append(
                GuardViolation(
                    "ignored_content_overflow",
                    None,
                    "ignored worktree enumeration exceeded its reporting bound",
                )
            )

        for path in sorted(statuses):
            status_code = statuses[path]
            self._check_path_shape(path, violations)
            if self._is_protected(path):
                violations.append(
                    GuardViolation(
                        "protected_path",
                        path,
                        "path is protected from daily automation",
                    )
                )
            if not any(
                path_matches_pattern(path, pattern)
                for pattern in candidate.allowed_paths
            ):
                violations.append(
                    GuardViolation(
                        "path_not_allowlisted",
                        path,
                        "path is outside candidate ownership",
                    )
                )

            current = repo / path
            if status_code != "D":
                self._check_current_file(repo, current, path, limits, violations)
            if self._base_is_symlink(repo, base_sha, path):
                violations.append(
                    GuardViolation("symlink", path, "base path is a symlink")
                )

            if path in numstat:
                additions, deletions, binary = numstat[path]
            elif status_code == "?":
                additions, deletions, binary = self._untracked_numstat(current)
            else:
                additions, deletions, binary = (0, 0, False)
            if binary or PurePosixPath(path).suffix.lower() in _BINARY_SUFFIXES:
                violations.append(
                    GuardViolation(
                        "binary_file", path, "binary files are not permitted"
                    )
                )

            changed.append(
                ChangedFile(
                    path=path,
                    status=status_code,
                    additions=additions,
                    deletions=deletions,
                    is_test=self._is_test_path(path),
                )
            )

        if not changed:
            violations.append(
                GuardViolation("no_changes", None, "candidate produced no file changes")
            )

        self._check_diff_content(repo, base_sha, untracked, violations)
        self._check_schema_changes(repo, base_sha, statuses, violations)
        production = [item for item in changed if not item.is_test]
        tests = [item for item in changed if item.is_test]
        production_lines = sum(item.additions + item.deletions for item in production)
        test_lines = sum(item.additions + item.deletions for item in tests)

        if len(changed) > limits.max_total_files:
            violations.append(
                GuardViolation(
                    "total_file_limit",
                    None,
                    f"{len(changed)} files exceeds limit {limits.max_total_files}",
                )
            )
        if len(production) > limits.max_production_files:
            violations.append(
                GuardViolation(
                    "production_file_limit",
                    None,
                    f"{len(production)} files exceeds limit {limits.max_production_files}",
                )
            )
        if production_lines > limits.max_production_lines:
            violations.append(
                GuardViolation(
                    "production_line_limit",
                    None,
                    f"{production_lines} lines exceeds limit {limits.max_production_lines}",
                )
            )
        if len(tests) > limits.max_test_files:
            violations.append(
                GuardViolation(
                    "test_file_limit",
                    None,
                    f"{len(tests)} files exceeds limit {limits.max_test_files}",
                )
            )
        if test_lines > limits.max_test_lines:
            violations.append(
                GuardViolation(
                    "test_line_limit",
                    None,
                    f"{test_lines} lines exceeds limit {limits.max_test_lines}",
                )
            )

        return GuardReport(
            base_sha=base_sha,
            patch_sha256=self._patch_digest(repo, base_sha, untracked, ignored),
            changed_files=tuple(changed),
            violations=tuple(self._deduplicate_violations(violations)),
            production_files=len(production),
            production_lines=production_lines,
            test_files=len(tests),
            test_lines=test_lines,
        )

    @staticmethod
    def _git_environment() -> dict[str, str]:
        return {
            "PATH": "/nonexistent/vista-daily-maintainer",
            "HOME": "/nonexistent/vista-daily-maintainer",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PAGER": "",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def _run_git(
        self,
        repo: Path,
        *args: str,
        stdout_limit: int | None = None,
    ) -> _GitProcessResult:
        if stdout_limit is None:
            stdout_limit = _MAX_GIT_STDOUT_BYTES
        if stdout_limit < 1:
            raise ValueError("git stdout limit must be positive")
        git_executable = self._executables.resolve("git")
        try:
            process = subprocess.Popen(
                (str(git_executable), "-c", "core.quotepath=false", *args),
                cwd=repo,
                env=self._git_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ValueError(f"git inspection could not start: {exc.errno}") from exc
        if process.stdout is None or process.stderr is None:
            self._kill_git_process_group(process)
            raise ValueError("git inspection pipes are unavailable")

        stdout = bytearray()
        stderr = bytearray()
        stream_limits = {
            process.stdout.fileno(): ("stdout", stdout, stdout_limit),
            process.stderr.fileno(): ("stderr", stderr, _MAX_GIT_STDERR_BYTES),
        }
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
        failure: str | None = None
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "git inspection timed out"
                    break
                events = selector.select(remaining)
                if not events:
                    failure = "git inspection timed out"
                    break
                for key, _mask in events:
                    chunk = os.read(key.fd, _GIT_READ_CHUNK_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    label, buffer, limit = stream_limits[key.fd]
                    available = limit - len(buffer)
                    if len(chunk) > available:
                        if available > 0:
                            buffer.extend(chunk[:available])
                        failure = f"git inspection {label} exceeded output limit"
                        break
                    buffer.extend(chunk)
                if failure:
                    break
            if failure is None:
                remaining = max(0.001, deadline - time.monotonic())
                try:
                    returncode = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    failure = "git inspection timed out"
            if failure is not None:
                self._kill_git_process_group(process)
                raise ValueError(failure)
            return _GitProcessResult(
                returncode=returncode,
                stdout=bytes(stdout),
                stderr=bytes(stderr),
            )
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
            if process.poll() is None:
                self._kill_git_process_group(process)

    @staticmethod
    def _kill_git_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _git(
        self,
        repo: Path,
        *args: str,
        check: bool = True,
        stdout_limit: int | None = None,
    ) -> bytes:
        result = self._run_git(repo, *args, stdout_limit=stdout_limit)
        if check and result.returncode:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise ValueError(f"git inspection failed: {stderr or result.returncode}")
        return result.stdout

    def _validated_repo(self, repo_root: Path) -> Path:
        repo = Path(repo_root).resolve(strict=True)
        if not repo.is_dir():
            raise ValueError("repository root must be a directory")
        reported = self._git(repo, "rev-parse", "--show-toplevel").decode().strip()
        if Path(reported).resolve() != repo:
            raise ValueError("repository root must be the Git worktree root")
        return repo

    def _validate_base(self, repo: Path, base_sha: str) -> None:
        if not isinstance(base_sha, str) or not _OBJECT_ID.fullmatch(base_sha):
            raise ValueError("base SHA must be an exact 40- or 64-character object ID")
        result = self._run_git(repo, "cat-file", "-e", f"{base_sha}^{{commit}}")
        if result.returncode:
            raise ValueError("base SHA is not a reachable commit object")

    def _changed_statuses(self, repo: Path, base_sha: str) -> dict[str, str]:
        payload = self._git(
            repo,
            "diff",
            "--name-status",
            "-z",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            base_sha,
            "--",
        )
        tokens = payload.split(b"\0")
        if tokens and tokens[-1] == b"":
            tokens.pop()
        if len(tokens) % 2:
            raise ValueError("unexpected git name-status output")
        result: dict[str, str] = {}
        for index in range(0, len(tokens), 2):
            status_code = tokens[index].decode("ascii", "strict")
            path = tokens[index + 1].decode("utf-8", "strict")
            result[path] = status_code[:1]
        return result

    def _untracked_paths(self, repo: Path) -> tuple[str, ...]:
        payload = self._git(
            repo, "ls-files", "--others", "--exclude-standard", "-z", "--"
        )
        return tuple(
            item.decode("utf-8", "strict") for item in payload.split(b"\0") if item
        )

    def _ignored_state(self, repo: Path) -> _IgnoredState:
        # ``--directory`` collapses wholly ignored trees such as ``.venv/`` so
        # a dependency environment cannot explode the report. The byte and
        # path caps bound retained evidence; overflow is itself a violation.
        payload = self._git(
            repo,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "--no-empty-directory",
            "-z",
            "--",
            stdout_limit=_MAX_IGNORED_STATE_BYTES,
        )
        overflow = len(payload) > _MAX_IGNORED_STATE_BYTES
        paths: list[str] = []
        cursor = 0
        retained_bytes = min(len(payload), _MAX_IGNORED_STATE_BYTES)
        while cursor < retained_bytes and len(paths) < _MAX_IGNORED_STATE_PATHS:
            end = payload.find(b"\0", cursor, retained_bytes)
            if end < 0:
                overflow = True
                break
            raw_path = payload[cursor:end]
            cursor = end + 1
            if not raw_path:
                continue
            try:
                path = raw_path.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise ValueError("ignored path is not valid UTF-8") from exc
            normalized = path.rstrip("/")
            if not normalized:
                raise ValueError("ignored path is not a safe relative path")
            shape_violations: list[GuardViolation] = []
            self._check_path_shape(normalized, shape_violations)
            if shape_violations:
                raise ValueError("ignored path is not a safe relative POSIX path")
            paths.append(path)
        if cursor < len(payload):
            overflow = True
        retained = tuple(sorted(set(paths)))
        identity = hashlib.sha256()
        self._digest_frame(
            identity,
            b"domain",
            b"vista-world-daily-maintainer-ignored-state-v1",
        )
        self._digest_frame(
            identity,
            b"listing-sha256",
            hashlib.sha256(payload).digest(),
        )
        self._digest_frame(identity, b"overflow", b"1" if overflow else b"0")
        for path in retained:
            self._digest_ignored_path(identity, repo, path)
        return _IgnoredState(
            present=bool(payload),
            paths=retained,
            identity_sha256=identity.digest(),
            overflow=overflow,
        )

    def _digest_ignored_path(self, digest: _Digest, repo: Path, path: str) -> None:
        self._digest_frame(digest, b"path", path.encode("utf-8"))
        current = repo / path.rstrip("/")
        try:
            metadata = current.lstat()
        except OSError:
            self._digest_frame(digest, b"type", b"missing")
            return
        normalized_mode = stat.S_IFMT(metadata.st_mode) | (metadata.st_mode & 0o7777)
        self._digest_frame(digest, b"mode", normalized_mode.to_bytes(4, "big"))
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(current).encode("utf-8", "surrogateescape")
            self._digest_frame(digest, b"type", b"symlink")
            self._digest_frame(digest, b"target", target)
        elif stat.S_ISREG(metadata.st_mode):
            self._digest_frame(digest, b"type", b"file")
            self._digest_frame(digest, b"size", metadata.st_size.to_bytes(8, "big"))
            if metadata.st_size <= _MAX_IGNORED_FILE_HASH_BYTES:
                self._digest_frame(digest, b"content", self._file_sha256(current))
            else:
                self._digest_frame(digest, b"content", b"oversized")
        elif stat.S_ISDIR(metadata.st_mode):
            self._digest_frame(digest, b"type", b"directory")
        else:
            self._digest_frame(digest, b"type", b"other")

    def _numstat(self, repo: Path, base_sha: str) -> dict[str, tuple[int, int, bool]]:
        payload = self._git(
            repo,
            "diff",
            "--numstat",
            "-z",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            base_sha,
            "--",
        )
        result: dict[str, tuple[int, int, bool]] = {}
        for record in payload.split(b"\0"):
            if not record:
                continue
            fields = record.split(b"\t", 2)
            if len(fields) != 3:
                raise ValueError("unexpected git numstat output")
            added, deleted, raw_path = fields
            path = raw_path.decode("utf-8", "strict")
            binary = added == b"-" or deleted == b"-"
            result[path] = (
                0 if binary else int(added),
                0 if binary else int(deleted),
                binary,
            )
        return result

    @staticmethod
    def _check_path_shape(path: str, violations: list[GuardViolation]) -> None:
        pure = PurePosixPath(path)
        if (
            not path
            or path.startswith(("/", "\\"))
            or "\\" in path
            or _PATH_CONTROL.search(path)
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            violations.append(
                GuardViolation(
                    "unsafe_path", path, "path is not a safe relative POSIX path"
                )
            )

    @staticmethod
    def _is_protected(path: str) -> bool:
        pure = PurePosixPath(path)
        parts = pure.parts
        lowered = tuple(part.lower() for part in parts)
        basename = lowered[-1] if lowered else ""
        if has_v1_forbidden_authority(path):
            return True
        if parts and parts[0].lower() in _PROTECTED_TOP_LEVEL:
            return True
        if path == ".mcp.json" or basename.startswith(".env"):
            return True
        if (
            basename in _CREDENTIAL_BASENAMES
            or basename.startswith(".yarnrc")
            or any(
                len(lowered) >= len(suffix) and lowered[-len(suffix) :] == suffix
                for suffix in _CREDENTIAL_PATH_SUFFIXES
            )
        ):
            return True
        if basename in {".gitattributes", ".gitignore", ".gitmodules", ".mailmap"}:
            return True
        if (
            path.startswith("automation/daily-maintainer/")
            or path == "automation/daily-maintainer"
        ):
            return True
        if path.startswith("ops/systemd/") or path == "ops/systemd":
            return True
        if is_v1_protected_authority_basename(basename):
            return True
        if basename in {
            "backlog.yaml",
            "validation-profiles.yaml",
            "validation_profiles.yaml",
        }:
            return True
        if "content" in lowered and pure.suffix.lower() in {".uasset", ".umap"}:
            return True
        if any(part in {"secrets", "credentials"} for part in lowered):
            return True
        if any(
            part
            in {
                "accepted",
                "artifacts",
                "evidence",
                "journal",
                "ledger",
                "outputs",
                "receipts",
                "reports",
                "runs",
            }
            for part in lowered
        ):
            return True
        if _EVIDENCE_NAME.search(basename):
            return True
        if _RUNTIME_NAME.search(basename) and not DiffGuard._is_test_path(path):
            return True
        if any(
            part
            in {
                "assets",
                "prompts",
                "runtime",
                "ue",
                "unreal",
                "unreal_plugins",
                "world-packs",
                "world_packs",
            }
            for part in lowered
        ):
            return True
        if pure.suffix.lower() in {".service", ".socket", ".timer"}:
            return True
        return pure.suffix.lower() in _BINARY_SUFFIXES

    @staticmethod
    def _is_test_path(path: str) -> bool:
        return is_v1_test_scope(path)

    def _check_current_file(
        self,
        repo: Path,
        current: Path,
        path: str,
        limits: GuardLimits,
        violations: list[GuardViolation],
    ) -> None:
        probe = repo
        for part in PurePosixPath(path).parts:
            probe /= part
            try:
                metadata = probe.lstat()
            except FileNotFoundError:
                violations.append(
                    GuardViolation("missing_file", path, "changed path is missing")
                )
                return
            if stat.S_ISLNK(metadata.st_mode):
                violations.append(
                    GuardViolation(
                        "symlink", path, "changed path or parent is a symlink"
                    )
                )
                return
        try:
            resolved = current.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            violations.append(
                GuardViolation("missing_file", path, "changed path cannot be resolved")
            )
            return
        if repo not in resolved.parents:
            violations.append(
                GuardViolation(
                    "symlink_escape", path, "changed path escapes the worktree"
                )
            )
            return
        metadata = current.stat()
        if not stat.S_ISREG(metadata.st_mode):
            violations.append(
                GuardViolation(
                    "non_regular_file", path, "changed path is not a regular file"
                )
            )
            return
        if metadata.st_size > limits.max_file_bytes:
            violations.append(
                GuardViolation(
                    "large_file",
                    path,
                    f"file size {metadata.st_size} exceeds limit {limits.max_file_bytes}",
                )
            )
        if self._looks_binary(current):
            violations.append(
                GuardViolation("binary_file", path, "file content is binary")
            )

    def _base_is_symlink(self, repo: Path, base_sha: str, path: str) -> bool:
        payload = self._git(repo, "ls-tree", "-z", base_sha, "--", path)
        return payload.startswith(b"120000 ")

    @staticmethod
    def _looks_binary(path: Path) -> bool:
        try:
            payload = path.read_bytes()
        except OSError:
            return True
        if b"\0" in payload:
            return True
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            return True
        return False

    def _untracked_numstat(self, path: Path) -> tuple[int, int, bool]:
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                return (0, 0, True)
        except OSError:
            return (0, 0, True)
        if self._looks_binary(path):
            return (0, 0, True)
        try:
            contents = path.read_text(encoding="utf-8")
        except OSError:
            return (0, 0, True)
        return (len(contents.splitlines()), 0, False)

    def _check_diff_content(
        self,
        repo: Path,
        base_sha: str,
        untracked: tuple[str, ...],
        violations: list[GuardViolation],
    ) -> None:
        patch = self._git(
            repo,
            "diff",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            base_sha,
            "--",
        ).decode("utf-8", "replace")
        current_path: str | None = None
        old_path: str | None = None
        for line in patch.splitlines():
            if line.startswith("--- "):
                old_path = self._diff_header_path(line[4:])
                continue
            if line.startswith("+++ "):
                current_path = self._diff_header_path(line[4:]) or old_path
                continue
            if not current_path or line.startswith(("@@", "diff ", "index ")):
                continue
            if line.startswith("-") and not line.startswith("---"):
                if self._is_test_path(current_path):
                    violations.append(
                        GuardViolation(
                            "test_line_removed",
                            current_path,
                            "test-line deletion requires human review",
                        )
                    )
                    if _ASSERTION.search(line[1:]):
                        violations.append(
                            GuardViolation(
                                "test_assertion_removed",
                                current_path,
                                "test assertion deletion is forbidden",
                            )
                        )
            elif line.startswith("+") and not line.startswith("+++"):
                added = line[1:]
                self._check_added_line(current_path, added, violations)

        for path in untracked:
            current = repo / path
            if (
                current.is_symlink()
                or not current.is_file()
                or self._looks_binary(current)
            ):
                continue
            try:
                for line in current.read_text(encoding="utf-8").splitlines():
                    self._check_added_line(path, line, violations)
            except OSError:
                continue

        deleted_tests = [
            item
            for item in self._changed_statuses(repo, base_sha).items()
            if item[1] == "D" and self._is_test_path(item[0])
        ]
        for path, _status in deleted_tests:
            violations.append(
                GuardViolation(
                    "test_file_deleted", path, "test file deletion is forbidden"
                )
            )

    def _check_schema_changes(
        self,
        _repo: Path,
        _base_sha: str,
        statuses: dict[str, str],
        violations: list[GuardViolation],
    ) -> None:
        for path, status_code in sorted(statuses.items()):
            if not self._is_schema_path(path) or status_code in {"?", "A"}:
                continue
            violations.append(
                GuardViolation(
                    "schema_weakening",
                    path,
                    "existing schema modification requires human review",
                )
            )

    @staticmethod
    def _is_schema_path(path: str) -> bool:
        lowered = path.lower()
        return (
            lowered.endswith((".schema.json", ".schema.yaml", ".schema.yml"))
            or "schemas" in PurePosixPath(lowered).parts
        )

    @staticmethod
    def _diff_header_path(raw: str) -> str | None:
        if raw == "/dev/null":
            return None
        if raw.startswith(("a/", "b/")):
            return raw[2:]
        return raw

    def _check_added_line(
        self,
        path: str,
        line: str,
        violations: list[GuardViolation],
    ) -> None:
        if line.endswith((" ", "\t")):
            violations.append(
                GuardViolation(
                    "whitespace_error", path, "added line has trailing whitespace"
                )
            )
        if line.startswith(("<<<<<<< ", "======= ", ">>>>>>> ")):
            violations.append(
                GuardViolation(
                    "conflict_marker", path, "unresolved merge marker is forbidden"
                )
            )
        if any(pattern.search(line) for pattern in _SECRET_PATTERNS):
            violations.append(
                GuardViolation(
                    "secret_detected", path, "added content resembles a secret"
                )
            )
        if self._is_test_path(path) and _TEST_SKIP.search(line):
            violations.append(
                GuardViolation(
                    "test_skip_added", path, "new skip/xfail/todo is forbidden"
                )
            )
        if self._is_test_path(path) and _TEST_FOCUS.search(line):
            violations.append(
                GuardViolation(
                    "test_focus_added",
                    path,
                    "focused test execution is forbidden",
                )
            )

    def _patch_digest(
        self,
        repo: Path,
        base_sha: str,
        untracked: tuple[str, ...],
        ignored: _IgnoredState,
    ) -> str:
        digest = hashlib.sha256()
        self._digest_frame(
            digest,
            b"domain",
            b"vista-world-daily-maintainer-patch-v3",
        )
        self._digest_frame(digest, b"base-sha", base_sha.encode("ascii"))
        self._digest_frame(digest, b"ignored-state", ignored.identity_sha256)
        self._digest_frame(
            digest,
            b"tracked-diff",
            self._git(
                repo,
                "diff",
                "--binary",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                base_sha,
                "--",
            ),
        )
        for path in sorted(untracked):
            self._digest_frame(digest, b"untracked-path", path.encode("utf-8"))
            current = repo / path
            try:
                metadata = current.lstat()
            except OSError:
                self._digest_frame(digest, b"untracked-type", b"missing")
                continue
            normalized_mode = stat.S_IFMT(metadata.st_mode) | (
                metadata.st_mode & 0o7777
            )
            self._digest_frame(
                digest,
                b"untracked-mode",
                normalized_mode.to_bytes(4, "big"),
            )
            if stat.S_ISLNK(metadata.st_mode):
                payload = os.readlink(current).encode("utf-8", "surrogateescape")
                self._digest_frame(digest, b"untracked-type", b"symlink")
                self._digest_frame(digest, b"untracked-content", payload)
            elif stat.S_ISREG(metadata.st_mode):
                self._digest_frame(digest, b"untracked-type", b"file")
                self._digest_frame(
                    digest,
                    b"untracked-content-length",
                    metadata.st_size.to_bytes(8, "big"),
                )
                self._digest_frame(
                    digest,
                    b"untracked-content-sha256",
                    self._file_sha256(current),
                )
            else:
                self._digest_frame(digest, b"untracked-type", b"other")
        return digest.hexdigest()

    @staticmethod
    def _file_sha256(path: Path) -> bytes:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.digest()

    @staticmethod
    def _digest_frame(
        digest: _Digest,
        label: bytes,
        payload: bytes,
    ) -> None:
        digest.update(len(label).to_bytes(4, "big"))
        digest.update(label)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    @staticmethod
    def _deduplicate_violations(
        violations: list[GuardViolation],
    ) -> list[GuardViolation]:
        result: list[GuardViolation] = []
        seen: set[tuple[str, str | None, str]] = set()
        for item in violations:
            key = (item.code, item.path, item.detail)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result
