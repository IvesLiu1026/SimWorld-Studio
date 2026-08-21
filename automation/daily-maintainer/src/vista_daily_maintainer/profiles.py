from __future__ import annotations

import os
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping


_PROFILE_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "zsh", "ksh"})


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
