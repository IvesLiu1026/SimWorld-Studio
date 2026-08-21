from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping


_PROFILE_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_SYSTEM_EXECUTABLE_DIRS = (
    Path("/usr/local/sbin"),
    Path("/usr/local/bin"),
    Path("/usr/sbin"),
    Path("/usr/bin"),
    Path("/sbin"),
    Path("/bin"),
)


class TrustedExecutables:
    """Executable allowlist pinned independently of inherited ``PATH``.

    Each target is bound to its resolved path, owner, mode, device/inode,
    metadata and content digest.  Those facts are revalidated before the path
    is returned for use.  This narrows mutable-PATH and replacement attacks,
    but the final check-to-exec race still requires the immutable outer T13
    sandbox/mount boundary.
    """

    def __init__(self, executables: Mapping[str, Path]) -> None:
        resolved: dict[str, _ExecutablePin] = {}
        for name, raw_path in executables.items():
            if not re.fullmatch(r"[A-Za-z0-9._+-]+", name):
                raise ValueError(f"invalid trusted executable name: {name!r}")
            path = Path(raw_path)
            if not path.is_absolute():
                raise ValueError(f"trusted executable must be absolute: {name}")
            try:
                path = path.resolve(strict=True)
                pin = _ExecutablePin.capture(path)
            except OSError as exc:
                raise ValueError(f"trusted executable does not exist: {name}") from exc
            resolved[name] = pin
        self._executables: Mapping[str, _ExecutablePin] = MappingProxyType(resolved)

    @classmethod
    def system_defaults(cls) -> TrustedExecutables:
        search_path = os.pathsep.join(str(item) for item in _SYSTEM_EXECUTABLE_DIRS)
        values: dict[str, Path] = {}
        for name in ("git", "sh", "node", "npm", "uv"):
            found = shutil.which(name, path=search_path)
            if found:
                values[name] = Path(found)
        interpreter = Path(sys.executable).resolve(strict=True)
        values["python"] = interpreter
        values[interpreter.name] = interpreter
        return cls(values)

    def materialize_bin(self, directory: Path) -> Path:
        directory.mkdir(mode=0o700)
        for name, pin in self._executables.items():
            executable = pin.revalidate()
            target = directory / name
            target.symlink_to(executable)
        return directory

    def resolve(self, requested: str) -> Path:
        candidate = Path(requested)
        if candidate.is_absolute():
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as exc:
                raise ValueError(
                    f"trusted executable path does not exist: {requested}"
                ) from exc
            for pin in self._executables.values():
                if resolved == pin.path:
                    return pin.revalidate()
            raise ValueError(
                f"executable is not in trusted executable allowlist: {requested}"
            )
        try:
            return self._executables[requested].revalidate()
        except KeyError as exc:
            raise ValueError(
                f"executable is not in trusted executable allowlist: {requested}"
            ) from exc

    def resolve_argv(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        return (str(self.resolve(argv[0])), *argv[1:])


@dataclass(frozen=True)
class _ExecutablePin:
    path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str

    @classmethod
    def capture(cls, path: Path) -> _ExecutablePin:
        descriptor = cls._open(path)
        try:
            metadata = os.fstat(descriptor)
            cls._validate_metadata(path, metadata)
            digest = cls._digest_descriptor(descriptor)
        finally:
            os.close(descriptor)
        return cls(
            path=path,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            uid=metadata.st_uid,
            gid=metadata.st_gid,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
            sha256=digest,
        )

    def revalidate(self) -> Path:
        try:
            current = self.capture(self.path)
        except OSError as exc:
            raise ValueError(
                f"trusted executable is no longer available: {self.path}"
            ) from exc
        if current != self:
            raise ValueError(f"trusted executable identity changed: {self.path}")
        return self.path

    @staticmethod
    def _open(path: Path) -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return os.open(path, flags)

    @staticmethod
    def _validate_metadata(path: Path, metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
            raise ValueError(f"trusted executable is not executable: {path}")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ValueError(
                f"trusted executable cannot be group/world writable: {path}"
            )

    @staticmethod
    def _digest_descriptor(descriptor: int) -> str:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 64 * 1024):
            digest.update(chunk)
        return digest.hexdigest()


@dataclass(frozen=True)
class ValidationProfile:
    """A code-owned validation command.

    Candidate manifests can reference ``profile_id`` only. They never provide
    ``cwd``, ``argv``, environment variables, or a shell flag.
    """

    profile_id: str
    cwd: str
    argv: tuple[str, ...]
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not _PROFILE_ID.fullmatch(
            self.profile_id
        ):
            raise ValueError(f"invalid validation profile ID: {self.profile_id!r}")
        if (
            not isinstance(self.cwd, str)
            or not self.cwd
            or self.cwd.startswith(("/", "\\"))
            or "\\" in self.cwd
        ):
            raise ValueError("validation profile cwd must be a relative POSIX path")
        if any(
            part in {"", ".", ".."} for part in self.cwd.split("/") if self.cwd != "."
        ):
            raise ValueError("validation profile cwd cannot traverse the repository")
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("validation profile argv must be a non-empty tuple")
        if any(
            not isinstance(arg, str) or not arg or "\x00" in arg for arg in self.argv
        ):
            raise ValueError("validation profile argv contains an invalid argument")
        executable = os.path.basename(self.argv[0])
        if executable in _SHELL_EXECUTABLES and "-c" in self.argv[1:]:
            raise ValueError("validation profiles cannot use shell -c")
        if (
            isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 1800
        ):
            raise ValueError(
                "validation profile timeout must be between 1 and 1800 seconds"
            )


class ValidationProfileRegistry:
    """Immutable lookup for human-reviewed validation profiles."""

    def __init__(self, profiles: Iterable[ValidationProfile]) -> None:
        values: dict[str, ValidationProfile] = {}
        for profile in profiles:
            if profile.profile_id in values:
                raise ValueError(f"duplicate validation profile: {profile.profile_id}")
            values[profile.profile_id] = profile
        self._profiles: Mapping[str, ValidationProfile] = MappingProxyType(values)

    def __contains__(self, profile_id: object) -> bool:
        return profile_id in self._profiles

    def __iter__(self) -> Iterator[str]:
        return iter(self._profiles)

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self._profiles)

    def resolve(self, profile_id: str) -> ValidationProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise ValueError(f"unknown validation profile: {profile_id}") from exc


BUILTIN_VALIDATION_PROFILES = ValidationProfileRegistry(
    (
        ValidationProfile(
            profile_id="daily-maintainer-core-tests",
            cwd="automation/daily-maintainer",
            argv=(
                "uv",
                "run",
                "python",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-t",
                ".",
                "-p",
                "test_*.py",
            ),
        ),
        ValidationProfile(
            profile_id="tools-python-offline",
            cwd="tools",
            argv=(
                "uv",
                "run",
                "--group",
                "dev",
                "python",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_*.py",
            ),
            timeout_seconds=600,
        ),
        ValidationProfile(
            profile_id="web-server-unit",
            cwd="simworld_studio_workspace/web",
            argv=("npm", "run", "test:server:unit"),
            timeout_seconds=600,
        ),
        ValidationProfile(
            profile_id="web-server-contracts",
            cwd="simworld_studio_workspace/web",
            argv=("node", "server/tests/test-runner.js", "unit"),
            timeout_seconds=600,
        ),
        ValidationProfile(
            profile_id="web-frontend-build",
            cwd="simworld_studio_workspace/web",
            argv=("npm", "run", "build"),
            timeout_seconds=900,
        ),
        ValidationProfile(
            profile_id="unreal-content-contract",
            cwd="unreal_plugins/VistaAnimationContentApi",
            argv=("node", "--test", "Tests/offline-contract.test.mjs"),
            timeout_seconds=600,
        ),
        ValidationProfile(
            profile_id="unreal-content-profile",
            cwd="unreal_plugins/VistaAnimationContentApi",
            argv=("node", "--test", "Tests/mmg040-content-profile.test.mjs"),
            timeout_seconds=600,
        ),
        ValidationProfile(
            profile_id="unreal-install-script-syntax",
            cwd="unreal_plugins/VistaAnimationContentApi",
            argv=("sh", "-n", "Scripts/install-plugin.sh"),
        ),
        ValidationProfile(
            profile_id="unreal-build-script-syntax",
            cwd="unreal_plugins/VistaAnimationContentApi",
            argv=("sh", "-n", "Scripts/build-plugin.sh"),
        ),
    )
)
