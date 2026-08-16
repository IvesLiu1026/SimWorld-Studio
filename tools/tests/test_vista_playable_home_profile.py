from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.runtime.vista_playable_home import profile, profile_entrypoint, runtime


class PlayableHomeProfileFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        project = self.workspace / "project" / "Home.uproject"
        project.parent.mkdir()
        project.write_text("{}\n", encoding="utf-8")
        editor = root / "UE" / "Engine" / "Binaries" / "Linux" / "UnrealEditor"
        editor.parent.mkdir(parents=True)
        editor.write_text("#!/bin/sh\n", encoding="utf-8")
        editor.chmod(0o755)
        self.config = runtime.GameRuntimeConfig(
            workspace=self.workspace,
            project=project,
            ue_editor=editor,
            map_path="/Game/VISTA/PlayableHome/r1/Maps/VistaPlayableHome",
            display=":117",
            gpu=0,
            vista_world_port=runtime.DEFAULT_VISTA_WORLD_PORT,
            width=1280,
            height=720,
            fps=60,
            title="VISTA World",
        )
        self.plan = runtime.redacted_plan(self.config)
        self.plan_dir = self.workspace / "game-runtime" / "attempt-fixture"
        self.plan_dir.mkdir(parents=True)
        self.plan_path = self.plan_dir / "launch-plan.json"
        self.output = self.workspace / "sunshine-profile-test_01.json"
        self.plan_sha256 = self.write_plan(self.plan)

    def write_plan(self, value: object | None = None, *, raw: bytes | None = None) -> str:
        if raw is None:
            raw = (
                json.dumps(
                    self.plan if value is None else value,
                    indent=2,
                    sort_keys=True,
                    allow_nan=True,
                )
                + "\n"
            ).encode("utf-8")
        self.plan_path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()


class VistaPlayableHomeProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.fixture = PlayableHomeProfileFixture(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_happy_plan_writes_canonical_private_profile_and_loads_arguments(self) -> None:
        with mock.patch.object(
            runtime,
            "port_is_available",
            side_effect=AssertionError("profile writer must not probe a live port"),
        ):
            result = profile.write_profile(
                self.fixture.plan_path,
                self.fixture.plan_sha256,
                self.fixture.output,
            )
        payload = json.loads(self.fixture.output.read_text(encoding="utf-8"))
        self.assertEqual(
            set(payload),
            {
                "workspace",
                "project",
                "ue_editor",
                "map",
                "display",
                "gpu",
                "vista_world_port",
                "width",
                "height",
                "fps",
            },
        )
        self.assertEqual(payload["map"], self.fixture.config.map_path)
        self.assertNotIn("map_path", payload)
        self.assertNotIn("title", payload)
        self.assertEqual(
            self.fixture.output.read_bytes(), profile.canonical_json_bytes(payload)
        )
        self.assertEqual(stat.S_IMODE(self.fixture.output.stat().st_mode), 0o600)
        self.assertEqual(
            result.profile_sha256,
            hashlib.sha256(self.fixture.output.read_bytes()).hexdigest(),
        )
        arguments = profile_entrypoint.load_profile(self.fixture.output)
        self.assertEqual(
            arguments,
            [
                "--workspace",
                str(self.fixture.config.workspace),
                "--project",
                str(self.fixture.config.project),
                "--ue-editor",
                str(self.fixture.config.ue_editor),
                "--map",
                self.fixture.config.map_path,
                "--display",
                self.fixture.config.display,
                "--gpu",
                "0",
                "--vista-world-port",
                str(runtime.DEFAULT_VISTA_WORLD_PORT),
                "--width",
                "1280",
                "--height",
                "720",
                "--fps",
                "60",
            ],
        )

    def test_realistic_r2_plan_writes_a_pinned_profile(self) -> None:
        r2_config = runtime.GameRuntimeConfig(
            **{
                **self.fixture.config.__dict__,
                "runtime_profile": runtime.R2_RUNTIME_PROFILE,
                "display": runtime.R2_DISPLAY,
                "gpu": runtime.R2_GPU,
                "vista_world_port": runtime.R2_VISTA_WORLD_PORT,
                "width": runtime.R2_WIDTH,
                "height": runtime.R2_HEIGHT,
                "fps": runtime.R2_FPS,
            }
        )
        plan = runtime.redacted_plan(r2_config)
        digest = self.fixture.write_plan(plan)
        with mock.patch.object(
            runtime,
            "port_is_available",
            side_effect=AssertionError("profile writer must not probe a live port"),
        ):
            result = profile.write_profile(
                self.fixture.plan_path,
                digest,
                self.fixture.output,
            )
        payload = json.loads(self.fixture.output.read_text(encoding="utf-8"))
        self.assertEqual(
            set(payload),
            {
                "workspace",
                "project",
                "ue_editor",
                "map",
                "display",
                "gpu",
                "vista_world_port",
                "width",
                "height",
                "fps",
                "runtime_profile",
                "camera_profile",
            },
        )
        self.assertEqual(payload["runtime_profile"], runtime.R2_RUNTIME_PROFILE)
        self.assertEqual(payload["camera_profile"], runtime.R2_CAMERA_PROFILE)
        self.assertEqual(payload["display"], runtime.R2_DISPLAY)
        self.assertEqual(payload["vista_world_port"], runtime.R2_VISTA_WORLD_PORT)
        self.assertEqual(payload["width"], runtime.R2_WIDTH)
        self.assertEqual(payload["height"], runtime.R2_HEIGHT)
        self.assertEqual(
            result.profile_sha256,
            hashlib.sha256(self.fixture.output.read_bytes()).hexdigest(),
        )
        arguments = profile_entrypoint.load_profile(self.fixture.output)
        self.assertIn("--runtime-profile", arguments)
        profile_index = arguments.index("--runtime-profile")
        self.assertEqual(
            arguments[profile_index + 1], runtime.R2_RUNTIME_PROFILE
        )
        self.assertNotIn("--camera-profile", arguments)

    def test_profile_entrypoint_rejects_partial_or_unknown_r2_binding(self) -> None:
        base = {
            "workspace": "/run/home",
            "project": "/run/home/Home.uproject",
            "ue_editor": "/ue/Engine/Binaries/Linux/UnrealEditor",
            "map": "/Game/VISTA/Home",
        }
        profile_path = self.root / "entrypoint-profile.json"
        for label, update in (
            ("missing_camera", {"runtime_profile": runtime.R2_RUNTIME_PROFILE}),
            ("missing_runtime", {"camera_profile": runtime.R2_CAMERA_PROFILE}),
            (
                "wrong_camera",
                {
                    "runtime_profile": runtime.R2_RUNTIME_PROFILE,
                    "camera_profile": "default",
                },
            ),
            (
                "wrong_runtime",
                {
                    "runtime_profile": "realistic_interior_r3",
                    "camera_profile": runtime.R2_CAMERA_PROFILE,
                },
            ),
        ):
            with self.subTest(label=label):
                profile_path.write_text(
                    json.dumps({**base, **update}), encoding="utf-8"
                )
                profile_path.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "r2 runtime/camera"):
                    profile_entrypoint.load_profile(profile_path)

    def test_realistic_r2_plan_rejects_profile_port_and_camera_drift(self) -> None:
        r2_config = runtime.GameRuntimeConfig(
            **{
                **self.fixture.config.__dict__,
                "runtime_profile": runtime.R2_RUNTIME_PROFILE,
                "display": runtime.R2_DISPLAY,
                "gpu": runtime.R2_GPU,
                "vista_world_port": runtime.R2_VISTA_WORLD_PORT,
                "width": runtime.R2_WIDTH,
                "height": runtime.R2_HEIGHT,
                "fps": runtime.R2_FPS,
            }
        )
        original = runtime.redacted_plan(r2_config)
        cases: list[tuple[str, object]] = [
            ("runtime_profile", "realistic_interior_r3"),
            ("camera_profile", "default"),
            ("vista_world_port", runtime.R2_VISTA_WORLD_PORT + 1),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                candidate = copy.deepcopy(original)
                candidate["config"][field] = value
                digest = self.fixture.write_plan(candidate)
                with self.assertRaises(profile.ProfileError):
                    profile.validate_launch_plan(self.fixture.plan_path, digest)

    def test_legacy_profile_contract_shape_remains_closed(self) -> None:
        self.assertNotIn("runtime_profile", profile.PROFILE_FIELDS)
        self.assertNotIn("camera_profile", profile.PROFILE_FIELDS)
        self.assertEqual(
            profile.R2_PROFILE_FIELDS - profile.PROFILE_FIELDS,
            {"runtime_profile", "camera_profile"},
        )

    def test_cli_prints_source_and_profile_digests(self) -> None:
        output = self.fixture.workspace / "sunshine-profile.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = profile.main(
                [
                    "--launch-plan",
                    str(self.fixture.plan_path),
                    "--launch-plan-sha256",
                    self.fixture.plan_sha256,
                    "--output",
                    str(output),
                ]
            )
        rendered = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(rendered["source"]["sha256"], self.fixture.plan_sha256)
        self.assertEqual(rendered["output"], str(output))
        self.assertEqual(
            rendered["profile_sha256"], hashlib.sha256(output.read_bytes()).hexdigest()
        )

    def test_launch_plan_digest_drift_is_refused_without_output(self) -> None:
        with self.assertRaisesRegex(profile.ProfileError, "SHA-256 differs"):
            profile.write_profile(
                self.fixture.plan_path,
                "0" * 64,
                self.fixture.output,
            )
        self.assertFalse(self.fixture.output.exists())

    def test_command_config_and_security_drift_are_refused(self) -> None:
        candidates: list[tuple[str, dict[str, object], str]] = []
        command_drift = copy.deepcopy(self.fixture.plan)
        command_drift["command"] = [*command_drift["command"], "-unsafe"]
        candidates.append(("command", command_drift, "command/config"))
        config_drift = copy.deepcopy(self.fixture.plan)
        config_drift["config"]["width"] = 1920
        candidates.append(("config", config_drift, "command/config"))
        security_drift = copy.deepcopy(self.fixture.plan)
        security_drift["security"]["arbitrary_command"] = True
        candidates.append(("security", security_drift, "security"))
        for label, candidate, message in candidates:
            with self.subTest(label=label):
                digest = self.fixture.write_plan(candidate)
                with self.assertRaisesRegex(profile.ProfileError, message):
                    profile.write_profile(
                        self.fixture.plan_path,
                        digest,
                        self.fixture.output,
                    )
                self.assertFalse(self.fixture.output.exists())

    def test_plan_resource_paths_refuse_symlink_missing_escape_and_non_executable(self) -> None:
        original_project = self.fixture.config.project
        original_editor = self.fixture.config.ue_editor

        linked_project = self.fixture.workspace / "project" / "Linked.uproject"
        linked_project.symlink_to(original_project)
        outside_project = self.root / "Outside.uproject"
        outside_project.write_text("{}\n", encoding="utf-8")
        non_executable_editor = (
            self.root
            / "UE-nonexec"
            / "Engine"
            / "Binaries"
            / "Linux"
            / "UnrealEditor"
        )
        non_executable_editor.parent.mkdir(parents=True)
        non_executable_editor.write_text("#!/bin/sh\n", encoding="utf-8")
        non_executable_editor.chmod(0o600)
        linked_editor = self.root / "linked-editor"
        linked_editor.symlink_to(original_editor)
        wrong_editor = (
            self.root / "wrong" / "Binaries" / "Linux" / "UnrealEditor"
        )
        wrong_editor.parent.mkdir(parents=True)
        wrong_editor.write_text("#!/bin/sh\n", encoding="utf-8")
        wrong_editor.chmod(0o755)

        real_icd = self.root / "nvidia.json"
        real_icd.write_text("{}\n", encoding="utf-8")
        linked_icd = self.root / "nvidia-link.json"
        linked_icd.symlink_to(real_icd)
        real_compat = self.root / "nvidia-compat"
        real_compat.mkdir()
        linked_compat = self.root / "nvidia-compat-link"
        linked_compat.symlink_to(real_compat, target_is_directory=True)

        cases: list[tuple[str, runtime.GameRuntimeConfig]] = [
            (
                "project_symlink",
                runtime.GameRuntimeConfig(
                    **{**self.fixture.config.__dict__, "project": linked_project}
                ),
            ),
            (
                "project_missing",
                runtime.GameRuntimeConfig(
                    **{
                        **self.fixture.config.__dict__,
                        "project": self.fixture.workspace / "missing.uproject",
                    }
                ),
            ),
            (
                "project_escape",
                runtime.GameRuntimeConfig(
                    **{**self.fixture.config.__dict__, "project": outside_project}
                ),
            ),
            (
                "editor_symlink",
                runtime.GameRuntimeConfig(
                    **{**self.fixture.config.__dict__, "ue_editor": linked_editor}
                ),
            ),
            (
                "editor_missing",
                runtime.GameRuntimeConfig(
                    **{
                        **self.fixture.config.__dict__,
                        "ue_editor": self.root
                        / "UE-missing"
                        / "Engine"
                        / "Binaries"
                        / "Linux"
                        / "UnrealEditor",
                    }
                ),
            ),
            (
                "editor_non_executable",
                runtime.GameRuntimeConfig(
                    **{
                        **self.fixture.config.__dict__,
                        "ue_editor": non_executable_editor,
                    }
                ),
            ),
            (
                "editor_wrong_identity",
                runtime.GameRuntimeConfig(
                    **{**self.fixture.config.__dict__, "ue_editor": wrong_editor}
                ),
            ),
            (
                "nvidia_icd_symlink",
                runtime.GameRuntimeConfig(
                    **{**self.fixture.config.__dict__, "nvidia_icd": linked_icd}
                ),
            ),
            (
                "nvidia_icd_missing",
                runtime.GameRuntimeConfig(
                    **{
                        **self.fixture.config.__dict__,
                        "nvidia_icd": self.root / "missing-nvidia.json",
                    }
                ),
            ),
            (
                "nvidia_compat_symlink",
                runtime.GameRuntimeConfig(
                    **{
                        **self.fixture.config.__dict__,
                        "nvidia_compat": linked_compat,
                    }
                ),
            ),
            (
                "nvidia_compat_missing",
                runtime.GameRuntimeConfig(
                    **{
                        **self.fixture.config.__dict__,
                        "nvidia_compat": self.root / "missing-compat",
                    }
                ),
            ),
        ]
        for label, config in cases:
            with self.subTest(label=label):
                digest = self.fixture.write_plan(runtime.redacted_plan(config))
                with self.assertRaises(profile.ProfileError):
                    profile.write_profile(
                        self.fixture.plan_path,
                        digest,
                        self.fixture.output,
                    )
                self.assertFalse(self.fixture.output.exists())

        # Keep these references used explicitly so the valid fixture identity
        # remains obvious next to all rejected variants.
        self.assertTrue(original_project.is_file())
        self.assertTrue(os.access(original_editor, os.X_OK))

    def test_unknown_duplicate_and_nonfinite_launch_plan_json_are_refused(self) -> None:
        unknown = copy.deepcopy(self.fixture.plan)
        unknown["unexpected"] = True
        compact = json.dumps(self.fixture.plan, separators=(",", ":"))
        duplicate = (compact[0] + '"schema":"duplicate",' + compact[1:]).encode("utf-8")
        nonfinite = copy.deepcopy(self.fixture.plan)
        nonfinite["config"]["gpu"] = float("nan")
        cases: list[tuple[str, bytes]] = [
            (
                "unknown",
                (json.dumps(unknown, separators=(",", ":")) + "\n").encode("utf-8"),
            ),
            ("duplicate", duplicate),
            (
                "nonfinite",
                (json.dumps(nonfinite, separators=(",", ":"), allow_nan=True) + "\n").encode(
                    "utf-8"
                ),
            ),
        ]
        for label, raw in cases:
            with self.subTest(label=label):
                digest = self.fixture.write_plan(raw=raw)
                with self.assertRaises(profile.ProfileError):
                    profile.write_profile(
                        self.fixture.plan_path,
                        digest,
                        self.fixture.output,
                    )
                self.assertFalse(self.fixture.output.exists())

    def test_output_escape_symlink_and_o_excl_are_refused(self) -> None:
        outside = self.root / "sunshine-profile-outside.json"
        with self.assertRaisesRegex(profile.ProfileError, "direct child"):
            profile.write_profile(
                self.fixture.plan_path, self.fixture.plan_sha256, outside
            )

        alias = self.root / "workspace-alias"
        alias.symlink_to(self.fixture.workspace, target_is_directory=True)
        with self.assertRaisesRegex(profile.ProfileError, "direct child"):
            profile.write_profile(
                self.fixture.plan_path,
                self.fixture.plan_sha256,
                alias / "sunshine-profile.json",
            )

        self.fixture.output.write_text("do-not-replace\n", encoding="utf-8")
        with self.assertRaisesRegex(profile.ProfileError, "already exists"):
            profile.write_profile(
                self.fixture.plan_path,
                self.fixture.plan_sha256,
                self.fixture.output,
            )
        self.assertEqual(self.fixture.output.read_text(), "do-not-replace\n")

        self.fixture.output.unlink()
        target = self.fixture.workspace / "other.json"
        target.write_text("keep\n", encoding="utf-8")
        self.fixture.output.symlink_to(target)
        with self.assertRaisesRegex(profile.ProfileError, "already exists"):
            profile.write_profile(
                self.fixture.plan_path,
                self.fixture.plan_sha256,
                self.fixture.output,
            )
        self.assertEqual(target.read_text(), "keep\n")

    def test_launch_plan_symlink_identity_is_refused(self) -> None:
        raw = self.fixture.plan_path.read_bytes()
        source = self.fixture.plan_dir / "source.json"
        source.write_bytes(raw)
        self.fixture.plan_path.unlink()
        self.fixture.plan_path.symlink_to(source)
        with self.assertRaisesRegex(profile.ProfileError, "non-symlink"):
            profile.write_profile(
                self.fixture.plan_path,
                hashlib.sha256(raw).hexdigest(),
                self.fixture.output,
            )

    def test_entrypoint_rejects_mode_alias_unknown_duplicate_nonfinite_and_bad_types(self) -> None:
        profile.write_profile(
            self.fixture.plan_path,
            self.fixture.plan_sha256,
            self.fixture.output,
        )
        valid = json.loads(self.fixture.output.read_text())

        self.fixture.output.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            profile_entrypoint.load_profile(self.fixture.output)
        self.fixture.output.chmod(0o600)

        alias = self.root / "profile-alias.json"
        alias.symlink_to(self.fixture.output)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            profile_entrypoint.load_profile(alias)

        cases: list[tuple[str, bytes, str]] = []
        unknown = {**valid, "shell": "no"}
        cases.append(
            ("unknown", profile.canonical_json_bytes(unknown), "unknown")
        )
        compact = json.dumps(valid, separators=(",", ":"))
        duplicate = (compact[0] + '"gpu":0,' + compact[1:]).encode("utf-8")
        cases.append(("duplicate", duplicate, "strict JSON"))
        nonfinite = {**valid, "gpu": float("inf")}
        cases.append(
            (
                "nonfinite",
                (json.dumps(nonfinite, allow_nan=True) + "\n").encode("utf-8"),
                "strict JSON",
            )
        )
        bad_type = {**valid, "gpu": True}
        cases.append(
            ("bad_type", profile.canonical_json_bytes(bad_type), "must be an integer")
        )
        for label, raw, message in cases:
            with self.subTest(label=label):
                self.fixture.output.write_bytes(raw)
                self.fixture.output.chmod(0o600)
                with self.assertRaisesRegex(ValueError, message):
                    profile_entrypoint.load_profile(self.fixture.output)


if __name__ == "__main__":
    unittest.main()
