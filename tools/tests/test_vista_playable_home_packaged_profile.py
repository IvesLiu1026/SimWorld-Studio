from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from tools.runtime.vista_playable_home import packaged_entrypoint
from tools.runtime.vista_playable_home import packaged_profile
from tools.runtime.vista_playable_home import runtime
from tools.runtime.vista_playable_home import sunshine_app
from tools.ue.vista_playable_home import package_receipt as package_verifier


class PackagedProfileFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.attempt = root / "package-linux-development" / "attempt-04-no-afs-clean"
        self.archive = self.attempt / "archive" / "Linux"
        self.launcher = self.archive / "VistaPlayableHome.sh"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.executable = (
            self.archive
            / "VistaPlayableHome"
            / "Binaries"
            / "Linux"
            / "VistaPlayableHome"
        )
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text(
            "#!/bin/sh\ntrap 'exit 0' TERM INT\nsleep 60 &\nwait\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o700)
        self.pak = (
            self.archive
            / "VistaPlayableHome"
            / "Content"
            / "Paks"
            / "VistaPlayableHome-Linux.pak"
        )
        self.pak.parent.mkdir(parents=True)
        self.pak.write_bytes(b"fixture-pak\n")
        self.pak.chmod(0o600)
        self.mode_0644 = (
            self.archive
            / "VistaPlayableHome"
            / "Content"
            / "Fixtures"
            / "mode-0644.bin"
        )
        self.mode_0600 = self.mode_0644.with_name("mode-0600.bin")
        self.mode_0644.parent.mkdir(parents=True)
        self.mode_0644.write_bytes(b"mode fixture 0644\n")
        self.mode_0644.chmod(0o644)
        self.mode_0600.write_bytes(b"mode fixture 0600\n")
        self.mode_0600.chmod(0o600)
        self.project_descriptor = self.attempt / package_verifier.PROJECT_RELATIVE
        self.project_descriptor.parent.mkdir(parents=True)
        self.project_descriptor.write_text("{}\n", encoding="utf-8")
        self.project_descriptor.chmod(0o644)
        self.project_config = self.attempt / package_verifier.PROJECT_CONFIG_RELATIVE
        self.project_config.parent.mkdir(parents=True)
        self.project_config.write_text("[fixture]\n", encoding="utf-8")
        self.project_config.chmod(0o640)
        self.icd = root / "nvidia_icd.json"
        self.icd.write_text('{"file_format_version":"1.0.0"}\n', encoding="utf-8")
        self.icd.chmod(0o644)
        self.engine_root = root / "UE"
        self.unreal_pak = (
            self.engine_root / "Engine" / "Binaries" / "Linux" / "UnrealPak"
        )
        self.unreal_pak.parent.mkdir(parents=True)
        self.unreal_pak.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.unreal_pak.chmod(0o700)
        self.receipt_path = self.attempt / "package-receipt.json"
        self.profile_path = self.attempt / "sunshine-profile-packaged-test.json"
        self.receipt = self._receipt()
        self.write_receipt()

    def _artifact(self, path: Path, *, exact_modes: bool = False) -> dict[str, object]:
        metadata = path.stat()
        record: dict[str, object] = {
            "relative_path": path.relative_to(self.attempt).as_posix(),
            "sha256": packaged_profile.sha256_file(path),
            "bytes": metadata.st_size,
            "executable": bool(metadata.st_mode & 0o111),
        }
        if exact_modes:
            record["mode"] = stat.S_IMODE(metadata.st_mode)
        return record

    def _receipt(self) -> dict[str, object]:
        archive = package_verifier.inspect_archive(
            self.archive,
            trusted_engine_root=self.engine_root,
        )
        return {
            "schema": package_verifier.RECEIPT_SCHEMA,
            "status": "accepted",
            "created_at": "2026-08-15T00:00:00+00:00",
            "attempt_root": str(self.attempt),
            "bindings": {
                "source_build_result": str(self.root / "result-receipt.json"),
                "source_build_result_sha256": "1" * 64,
                "source_commit": "2" * 40,
                "source_runtime_acceptance": str(self.root / "acceptance.json"),
                "source_runtime_acceptance_sha256": "3" * 64,
                "map_path": packaged_profile.EXPECTED_MAP_PATH,
                "world_revision": packaged_profile.EXPECTED_WORLD_REVISION,
            },
            "artifacts": {
                "archive_root": str(self.archive),
                "launcher": self._artifact(self.launcher),
                "executable": self._artifact(self.executable),
                "pak": self._artifact(self.pak),
            },
            "uat": {},
            "project_policy": {},
            "tools": {},
            "trusted_upstream": {
                "policy": "engine-root-derived-from-pinned-unrealpak/v1",
                "engine_root": str(self.engine_root),
                "unreal_pak": str(self.unreal_pak),
                "unreal_pak_sha256": packaged_profile.sha256_file(self.unreal_pak),
            },
            "archive": archive,
            "output": str(self.receipt_path),
        }

    def write_receipt(self, value: object | None = None) -> str:
        raw = packaged_profile.canonical_json(self.receipt if value is None else value)
        self.receipt_path.write_bytes(raw)
        self.receipt_path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    def enable_r2_receipt(self, *, exact_modes: bool) -> None:
        self.receipt["schema"] = (
            package_verifier.R2_EXACT_MODE_RECEIPT_SCHEMA
            if exact_modes
            else package_verifier.R2_RECEIPT_SCHEMA
        )
        self.receipt["bindings"].update(
            {
                "runtime_profile": runtime.R2_RUNTIME_PROFILE,
                "camera_profile": runtime.R2_CAMERA_PROFILE,
                "visual_profile_id": runtime.R2_RUNTIME_PROFILE,
                "visual_profile_sha256": "4" * 64,
                "visual_profile_content_digest": "5" * 64,
                "renderer_profile_request_sha256": "6" * 64,
                "renderer_profile_request_content_digest": "7" * 64,
                "presentation_import_receipt_sha256": "8" * 64,
                "presentation_scene_receipt_sha256": "9" * 64,
                "presentation_manifest_sha256": "a" * 64,
                "presentation_artifact_receipt_sha256": "b" * 64,
                "accepted_display": runtime.R2_DISPLAY,
                "accepted_gpu": runtime.R2_GPU,
                "accepted_vista_world_port": runtime.R2_VISTA_WORLD_PORT,
                "accepted_width": runtime.R2_WIDTH,
                "accepted_height": runtime.R2_HEIGHT,
                "accepted_fps": runtime.R2_FPS,
                "presentation_bundle_count": (
                    package_verifier.R2_PRESENTATION_BUNDLE_COUNT
                ),
                "presentation_collision_policy": (
                    package_verifier.R2_PRESENTATION_COLLISION_POLICY
                ),
            }
        )
        if exact_modes:
            self.receipt["archive"] = package_verifier.inspect_archive(
                self.archive,
                trusted_engine_root=self.engine_root,
                exact_modes=True,
            )
            self.receipt["artifacts"] = {
                "archive_root": str(self.archive),
                "launcher": self._artifact(self.launcher, exact_modes=True),
                "executable": self._artifact(self.executable, exact_modes=True),
                "pak": self._artifact(self.pak, exact_modes=True),
            }
            self.receipt["trusted_upstream"].update(
                {
                    "mode_policy": packaged_profile.EXACT_MODE_POLICY,
                    "unreal_pak_mode": stat.S_IMODE(self.unreal_pak.stat().st_mode),
                }
            )
            self.receipt["project_policy"] = {
                "project_descriptor": str(self.project_descriptor),
                "project_descriptor_sha256": packaged_profile.sha256_file(
                    self.project_descriptor
                ),
                "project_config": str(self.project_config),
                "project_config_sha256": packaged_profile.sha256_file(
                    self.project_config
                ),
                "enabled_plugins": ["VistaPlayableHome"],
                "disabled_plugins": [
                    "AndroidFileServer",
                    "EditorScriptingUtilities",
                    "Interchange",
                    "PythonScriptPlugin",
                ],
                "host_module": "VistaPlayableHomeHost",
                "android_file_server_enabled": False,
                "mode_policy": packaged_profile.EXACT_MODE_POLICY,
                "project_descriptor_mode": stat.S_IMODE(
                    self.project_descriptor.stat().st_mode
                ),
                "project_config_mode": stat.S_IMODE(self.project_config.stat().st_mode),
            }
        self.write_receipt()

    @property
    def receipt_sha256(self) -> str:
        return packaged_profile.sha256_file(self.receipt_path)

    def write_profile(
        self,
        runtime_profile: str | None = None,
        *,
        exact_modes: bool = False,
    ) -> packaged_profile.ProfileWriteResult:
        if runtime_profile == runtime.R2_RUNTIME_PROFILE:
            self.enable_r2_receipt(exact_modes=exact_modes)
        return packaged_profile.write_profile(
            self.attempt,
            self.receipt_sha256,
            self.icd,
            self.profile_path,
            runtime_profile=runtime_profile,
        )

    def load_profile(self) -> packaged_profile.PackagedProfileInputs:
        return packaged_profile.load_profile(
            self.profile_path,
            packaged_profile.sha256_file(self.profile_path),
        )


class PackagedProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fixture = PackagedProfileFixture(self.root)

    def test_profile_is_private_canonical_and_receipt_archive_bound(self) -> None:
        result = self.fixture.write_profile()
        raw = self.fixture.profile_path.read_bytes()
        payload = json.loads(raw)

        self.assertEqual(set(payload), packaged_profile.PROFILE_KEYS)
        self.assertEqual(raw, packaged_profile.canonical_json(payload))
        self.assertEqual(stat.S_IMODE(self.fixture.profile_path.stat().st_mode), 0o600)
        self.assertEqual(result.profile_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.package_receipt_sha256, self.fixture.receipt_sha256)
        self.assertEqual(
            result.archive_tree_sha256,
            self.fixture.receipt["archive"]["tree_sha256"],
        )

        loaded = packaged_profile.load_profile(
            self.fixture.profile_path,
            result.profile_sha256,
        )
        self.assertEqual(loaded.package.executable, self.fixture.executable)
        self.assertEqual(loaded.package.pak, self.fixture.pak)
        self.assertEqual(loaded.package.map_path, packaged_profile.EXPECTED_MAP_PATH)
        self.assertEqual(loaded.package.unreal_pak, self.fixture.unreal_pak)
        self.assertEqual(
            loaded.nvidia_icd_sha256,
            packaged_profile.sha256_file(self.fixture.icd),
        )

    def test_direct_command_has_no_editor_uproject_game_flag_or_shell_launcher(
        self,
    ) -> None:
        self.fixture.write_profile()
        inputs = self.fixture.load_profile()
        command = packaged_entrypoint.build_command(inputs)

        self.assertEqual(command[0], str(self.fixture.executable))
        self.assertEqual(command[1], "VistaPlayableHome")
        self.assertEqual(command[2], packaged_profile.EXPECTED_MAP_PATH)
        self.assertNotEqual(command[0], str(self.fixture.launcher))
        self.assertNotIn("-game", command)
        self.assertFalse(any("UnrealEditor" in argument for argument in command))
        self.assertFalse(any(argument.endswith(".uproject") for argument in command))
        self.assertIn("-graphicsadapter=0", command)
        self.assertIn("-VistaWorldPort=55620", command)
        self.assertIn("-ResX=1280", command)
        self.assertIn("-ResY=720", command)

    def test_legacy_r2_v2_profile_remains_loadable_and_runtime_closed(self) -> None:
        result = self.fixture.write_profile(runtime.R2_RUNTIME_PROFILE)
        inputs = packaged_profile.load_profile(
            self.fixture.profile_path,
            result.profile_sha256,
        )
        payload = json.loads(self.fixture.profile_path.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), packaged_profile.R2_PROFILE_KEYS)
        self.assertEqual(payload["schema"], packaged_profile.R2_PROFILE_SCHEMA)
        self.assertEqual(payload["runtime_profile"], runtime.R2_RUNTIME_PROFILE)
        self.assertEqual(payload["camera_profile"], runtime.R2_CAMERA_PROFILE)
        self.assertEqual(payload["display"], runtime.R2_DISPLAY)
        self.assertEqual(payload["vista_world_port"], runtime.R2_VISTA_WORLD_PORT)
        self.assertEqual(payload["width"], runtime.R2_WIDTH)
        self.assertEqual(payload["height"], runtime.R2_HEIGHT)
        self.assertEqual(
            inputs.package.receipt_schema,
            package_verifier.R2_RECEIPT_SCHEMA,
        )
        self.assertFalse(inputs.package.exact_mode_attestation)
        self.assertFalse(inputs.exact_mode_attestation)

        command = packaged_entrypoint.build_command(inputs)
        self.assertIn(
            f"-VistaCameraProfile={runtime.R2_CAMERA_PROFILE}",
            command,
        )
        self.assertIn(f"-VistaWorldPort={runtime.R2_VISTA_WORLD_PORT}", command)
        self.assertIn(f"-ResX={runtime.R2_WIDTH}", command)
        self.assertIn(f"-ResY={runtime.R2_HEIGHT}", command)
        environment = packaged_entrypoint.sanitized_environment(inputs)
        user_root = self.fixture.attempt / "interactive-user"
        self.assertEqual(environment["DISPLAY"], runtime.R2_DISPLAY)
        self.assertEqual(environment["TMPDIR"], str(user_root / "tmp"))
        self.assertEqual(environment["XDG_DATA_HOME"], str(user_root / "xdg-data"))
        self.assertEqual(
            environment["VISTA_RUNTIME_PROFILE"], runtime.R2_RUNTIME_PROFILE
        )
        self.assertEqual(environment["VISTA_CAMERA_PROFILE"], runtime.R2_CAMERA_PROFILE)
        plan = packaged_entrypoint.launch_plan(inputs)
        self.assertEqual(plan["schema"], packaged_entrypoint.R2_PLAN_SCHEMA)
        self.assertEqual(plan["mode"], packaged_profile.R2_PROFILE_MODE)
        self.assertEqual(plan["runtime"]["runtime_profile"], runtime.R2_RUNTIME_PROFILE)
        self.assertEqual(plan["runtime"]["camera_profile"], runtime.R2_CAMERA_PROFILE)
        self.assertEqual(
            plan["runtime"]["vista_world_port"], runtime.R2_VISTA_WORLD_PORT
        )

    def test_exact_mode_r2_v3_profile_seals_archive_and_named_file_modes(self) -> None:
        result = self.fixture.write_profile(
            runtime.R2_RUNTIME_PROFILE,
            exact_modes=True,
        )
        payload = json.loads(self.fixture.profile_path.read_text(encoding="utf-8"))
        inputs = packaged_profile.load_profile(
            self.fixture.profile_path,
            result.profile_sha256,
        )

        self.assertEqual(set(payload), packaged_profile.R2_EXACT_MODE_PROFILE_KEYS)
        self.assertEqual(
            payload["schema"], packaged_profile.R2_EXACT_MODE_PROFILE_SCHEMA
        )
        self.assertEqual(
            payload["archive_schema"],
            package_verifier.ARCHIVE_SCHEMA_EXACT_MODE_V2,
        )
        self.assertEqual(
            payload["archive_algorithm"],
            package_verifier.ARCHIVE_ALGORITHM_EXACT_MODE_V2,
        )
        self.assertEqual(payload["mode_policy"], packaged_profile.EXACT_MODE_POLICY)
        self.assertEqual(payload["package_receipt_mode"], 0o600)
        self.assertEqual(payload["profile_file_mode"], 0o600)
        self.assertEqual(
            payload["nvidia_icd_mode"],
            stat.S_IMODE(self.fixture.icd.stat().st_mode),
        )
        self.assertTrue(inputs.exact_mode_attestation)
        self.assertTrue(inputs.package.exact_mode_attestation)
        self.assertEqual(
            inputs.package.receipt_schema,
            package_verifier.R2_EXACT_MODE_RECEIPT_SCHEMA,
        )
        self.assertEqual(
            inputs.package.launcher_mode,
            stat.S_IMODE(self.fixture.launcher.stat().st_mode),
        )
        self.assertEqual(
            inputs.package.executable_mode,
            stat.S_IMODE(self.fixture.executable.stat().st_mode),
        )
        self.assertEqual(
            inputs.package.pak_mode,
            stat.S_IMODE(self.fixture.pak.stat().st_mode),
        )
        self.assertEqual(
            inputs.package.project_config_mode,
            stat.S_IMODE(self.fixture.project_config.stat().st_mode),
        )

    def test_exact_mode_r2_v3_profile_rejects_mode_only_drift(self) -> None:
        result = self.fixture.write_profile(
            runtime.R2_RUNTIME_PROFILE,
            exact_modes=True,
        )

        for path, changed_mode in (
            (self.fixture.mode_0644, 0o600),
            (self.fixture.mode_0600, 0o640),
            (self.fixture.project_config, 0o600),
            (self.fixture.icd, 0o600),
        ):
            original_mode = stat.S_IMODE(path.stat().st_mode)
            with self.subTest(path=path.name, changed_mode=oct(changed_mode)):
                path.chmod(changed_mode)
                with self.assertRaises(packaged_profile.PackagedProfileError):
                    packaged_profile.load_profile(
                        self.fixture.profile_path,
                        result.profile_sha256,
                    )
                path.chmod(original_mode)

        loaded = packaged_profile.load_profile(
            self.fixture.profile_path,
            result.profile_sha256,
        )
        self.assertTrue(loaded.exact_mode_attestation)

    def test_load_profile_rejects_profile_mode_exchange_between_phases(self) -> None:
        result = self.fixture.write_profile(
            runtime.R2_RUNTIME_PROFILE,
            exact_modes=True,
        )
        original_validate = packaged_profile.validate_package_attempt

        def exchanging_validate(*args, **kwargs):
            binding = original_validate(*args, **kwargs)
            self.fixture.profile_path.chmod(0o640)
            return binding

        with (
            mock.patch.object(
                packaged_profile,
                "validate_package_attempt",
                side_effect=exchanging_validate,
            ),
            self.assertRaisesRegex(
                packaged_profile.PackagedProfileError,
                "PROFILE_IDENTITY_CHANGED",
            ),
        ):
            packaged_profile.load_profile(
                self.fixture.profile_path,
                result.profile_sha256,
            )
        self.fixture.profile_path.chmod(0o600)

    def test_package_and_requested_runtime_profiles_must_match(self) -> None:
        with self.assertRaisesRegex(
            packaged_profile.PackagedProfileError, "PACKAGE_PROFILE_MISMATCH"
        ):
            packaged_profile.write_profile(
                self.fixture.attempt,
                self.fixture.receipt_sha256,
                self.fixture.icd,
                self.fixture.profile_path,
                runtime_profile=runtime.R2_RUNTIME_PROFILE,
            )

        self.fixture.receipt["schema"] = package_verifier.R2_RECEIPT_SCHEMA
        self.fixture.receipt["bindings"].update(
            {
                "runtime_profile": runtime.R2_RUNTIME_PROFILE,
                "camera_profile": runtime.R2_CAMERA_PROFILE,
                "visual_profile_id": runtime.R2_RUNTIME_PROFILE,
                **{
                    field: "e" * 64 for field in package_verifier.R2_BUILD_DIGEST_FIELDS
                },
                "accepted_display": runtime.R2_DISPLAY,
                "accepted_gpu": runtime.R2_GPU,
                "accepted_vista_world_port": runtime.R2_VISTA_WORLD_PORT,
                "accepted_width": runtime.R2_WIDTH,
                "accepted_height": runtime.R2_HEIGHT,
                "accepted_fps": runtime.R2_FPS,
                "presentation_bundle_count": (
                    package_verifier.R2_PRESENTATION_BUNDLE_COUNT
                ),
                "presentation_collision_policy": (
                    package_verifier.R2_PRESENTATION_COLLISION_POLICY
                ),
            }
        )
        r2_pin = self.fixture.write_receipt()
        with self.assertRaisesRegex(
            packaged_profile.PackagedProfileError, "PACKAGE_PROFILE_MISMATCH"
        ):
            packaged_profile.write_profile(
                self.fixture.attempt,
                r2_pin,
                self.fixture.icd,
                self.fixture.profile_path,
            )

    def test_realistic_r2_profile_fixed_tuple_tampering_is_refused(self) -> None:
        result = self.fixture.write_profile(runtime.R2_RUNTIME_PROFILE)
        original = json.loads(self.fixture.profile_path.read_text(encoding="utf-8"))
        for field, value in (
            ("runtime_profile", "realistic_interior_r3"),
            ("camera_profile", "default"),
            ("display", ":120"),
            ("vista_world_port", runtime.R2_VISTA_WORLD_PORT + 1),
            ("width", 1280),
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(original)
                candidate[field] = value
                raw = packaged_profile.canonical_json(candidate)
                self.fixture.profile_path.write_bytes(raw)
                self.fixture.profile_path.chmod(0o600)
                with self.assertRaises(packaged_profile.PackagedProfileError):
                    packaged_profile.load_profile(
                        self.fixture.profile_path,
                        hashlib.sha256(raw).hexdigest(),
                    )
                self.fixture.profile_path.write_bytes(
                    packaged_profile.canonical_json(original)
                )
                self.fixture.profile_path.chmod(0o600)
        self.assertEqual(
            packaged_profile.sha256_file(self.fixture.profile_path),
            result.profile_sha256,
        )

    def test_environment_is_fixed_x11_gpu_zero_and_does_not_copy_secrets(self) -> None:
        self.fixture.write_profile()
        inputs = self.fixture.load_profile()
        with mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "do-not-copy",
                "OPENAI_API_KEY": "do-not-copy",
                "STUDIO_ACCESS_TOKEN": "do-not-copy",
                "DISPLAY": ":999",
                "CUDA_VISIBLE_DEVICES": "1",
            },
            clear=True,
        ):
            environment = packaged_entrypoint.sanitized_environment(inputs)

        self.assertEqual(environment["DISPLAY"], ":117")
        self.assertEqual(environment["VISTA_RUNTIME_GPU"], "0")
        self.assertEqual(environment["VK_ICD_FILENAMES"], str(self.fixture.icd))
        self.assertEqual(environment["PATH"], packaged_entrypoint.TRUSTED_PATH)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("STUDIO_ACCESS_TOKEN", environment)
        self.assertNotIn("CUDA_VISIBLE_DEVICES", environment)

    def test_receipt_profile_and_mode_pins_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            packaged_profile.PackagedProfileError, "PIN_MISMATCH"
        ):
            packaged_profile.write_profile(
                self.fixture.attempt,
                "0" * 64,
                self.fixture.icd,
                self.fixture.profile_path,
            )
        self.assertFalse(self.fixture.profile_path.exists())

        result = self.fixture.write_profile()
        with self.assertRaisesRegex(
            packaged_profile.PackagedProfileError, "PIN_MISMATCH"
        ):
            packaged_profile.load_profile(self.fixture.profile_path, "0" * 64)

        self.fixture.profile_path.chmod(0o644)
        with self.assertRaisesRegex(packaged_profile.PackagedProfileError, "0600"):
            packaged_profile.load_profile(
                self.fixture.profile_path, result.profile_sha256
            )

    def test_archive_drift_is_refused_when_loading_existing_profile(self) -> None:
        result = self.fixture.write_profile()
        self.fixture.pak.write_bytes(self.fixture.pak.read_bytes() + b"tamper")
        with self.assertRaises(packaged_profile.PackagedProfileError):
            packaged_profile.load_profile(
                self.fixture.profile_path, result.profile_sha256
            )

    def test_fixed_value_or_unknown_field_tampering_is_refused_even_when_rehashed(
        self,
    ) -> None:
        self.fixture.write_profile()
        original = json.loads(self.fixture.profile_path.read_text(encoding="utf-8"))
        cases = []
        gpu = copy.deepcopy(original)
        gpu["gpu"] = 1
        cases.append(("gpu", gpu, "fixed"))
        unknown = copy.deepcopy(original)
        unknown["shell"] = "rm -rf /"
        cases.append(("unknown", unknown, "fields"))
        for label, payload, message in cases:
            with self.subTest(label=label):
                raw = packaged_profile.canonical_json(payload)
                self.fixture.profile_path.write_bytes(raw)
                self.fixture.profile_path.chmod(0o600)
                with self.assertRaisesRegex(
                    packaged_profile.PackagedProfileError,
                    message,
                ):
                    packaged_profile.load_profile(
                        self.fixture.profile_path,
                        hashlib.sha256(raw).hexdigest(),
                    )

    def test_profile_output_is_scoped_and_never_replaced(self) -> None:
        outside = self.root / "sunshine-profile-packaged.json"
        with self.assertRaisesRegex(
            packaged_profile.PackagedProfileError, "direct child"
        ):
            packaged_profile.write_profile(
                self.fixture.attempt,
                self.fixture.receipt_sha256,
                self.fixture.icd,
                outside,
            )
        self.fixture.write_profile()
        before = self.fixture.profile_path.read_bytes()
        with self.assertRaisesRegex(
            packaged_profile.PackagedProfileError, "already exists"
        ):
            self.fixture.write_profile()
        self.assertEqual(self.fixture.profile_path.read_bytes(), before)

    def test_sunshine_entry_can_pin_packaged_profile_without_changing_preview(
        self,
    ) -> None:
        preview = sunshine_app.build_entry(
            python=Path("/usr/bin/python3"),
            launcher=Path("/repo/profile_entrypoint.py"),
            profile=Path("/run/sunshine-profile.json"),
            working_dir=Path("/repo"),
        )
        packaged = sunshine_app.build_entry(
            python=Path("/usr/bin/python3"),
            launcher=Path("/repo/packaged_entrypoint.py"),
            profile=Path("/run/sunshine-profile-packaged.json"),
            working_dir=Path("/repo"),
            profile_sha256="a" * 64,
            exit_timeout=90,
        )
        self.assertNotIn("--profile-sha256", preview["cmd"])
        self.assertIn("--profile-sha256 " + "a" * 64, packaged["cmd"])
        self.assertEqual(preview["exit-timeout"], "20")
        self.assertEqual(packaged["exit-timeout"], "90")
        payload = {
            "apps": [{"name": "Desktop"}, {"name": "VISTA World", "cmd": "preview"}]
        }
        merged = sunshine_app.merge_entry(payload, packaged)
        self.assertEqual(
            [item["name"] for item in merged["apps"]], ["Desktop", "VISTA World"]
        )
        self.assertEqual(merged["apps"][-1]["cmd"], packaged["cmd"])
        with self.assertRaisesRegex(sunshine_app.SunshineConfigError, "SHA-256"):
            sunshine_app.build_entry(
                python=Path("/usr/bin/python3"),
                launcher=Path("/repo/packaged_entrypoint.py"),
                profile=Path("/run/profile.json"),
                working_dir=Path("/repo"),
                profile_sha256="not-a-digest",
            )

    def test_owned_supervisor_stops_only_its_real_packaged_process_group(self) -> None:
        self.fixture.write_profile()
        inputs = self.fixture.load_profile()
        stop_checks = 0

        def stop_requested() -> bool:
            nonlocal stop_checks
            stop_checks += 1
            return stop_checks >= 3

        def ready(process, *, stop_requested):
            self.assertIsNone(process.poll())
            self.assertFalse(stop_requested())
            return {
                "command_id": "vwc-" + "a" * 24,
                "status": "success",
                "code": "READY",
                "world_revision": packaged_profile.EXPECTED_WORLD_REVISION,
                "session_generation": 0,
                "event_status": "idle",
                "active_event": None,
            }

        output = io.StringIO()
        listener_calls: list[tuple[int, int]] = []

        def listener(port: int, process_group: int) -> dict[str, object]:
            listener_calls.append((port, process_group))
            return {
                "host": "127.0.0.1",
                "port": port,
                "process_group": process_group,
                "socket_inode": 123,
                "owner_pids": [process_group],
            }

        with (
            mock.patch.object(
                packaged_entrypoint,
                "validate_vista_world_port",
                return_value=packaged_profile.EXPECTED_PORT,
            ),
            mock.patch.object(packaged_entrypoint.time, "sleep", return_value=None),
            contextlib.redirect_stdout(output),
        ):
            code = packaged_entrypoint.run_packaged(
                inputs,
                stop_requested=stop_requested,
                readiness_waiter=ready,
                listener_prover=listener,
            )

        self.assertEqual(code, 0)
        rendered = json.loads(output.getvalue())
        state_path = Path(rendered["state"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["mode"], packaged_profile.PROFILE_MODE)
        self.assertEqual(state["package_receipt_sha256"], self.fixture.receipt_sha256)
        self.assertEqual(
            state["nvidia_icd_sha256"],
            packaged_profile.sha256_file(self.fixture.icd),
        )
        self.assertTrue(state["archive_reverified_after_readiness"])
        self.assertEqual(
            state["readiness"]["listener_ownership"]["host"],
            "127.0.0.1",
        )
        self.assertEqual(
            listener_calls,
            [(packaged_profile.EXPECTED_PORT, state["process"]["pid"])],
        )
        self.assertFalse(runtime.identity_is_live(state["process"]))
        pointer_path, pointer_state = runtime.resolve_current_runtime_state(
            self.fixture.attempt
        )
        self.assertEqual(pointer_path, state_path)
        self.assertEqual(pointer_state["status"], "stopped")

    def test_realistic_r2_packaged_state_binds_profile_plan_and_port(self) -> None:
        self.fixture.write_profile(runtime.R2_RUNTIME_PROFILE)
        inputs = self.fixture.load_profile()
        stop_checks = 0

        def stop_requested() -> bool:
            nonlocal stop_checks
            stop_checks += 1
            return stop_checks >= 3

        def ready(process, *, stop_requested):
            self.assertIsNone(process.poll())
            self.assertFalse(stop_requested())
            return {
                "command_id": "vwc-" + "c" * 24,
                "status": "success",
                "code": "READY",
                "world_revision": packaged_profile.EXPECTED_WORLD_REVISION,
                "session_generation": 0,
                "event_status": "idle",
                "active_event": None,
            }

        listener_calls: list[tuple[int, int]] = []

        def listener(port: int, process_group: int) -> dict[str, object]:
            listener_calls.append((port, process_group))
            return {
                "host": "127.0.0.1",
                "port": port,
                "process_group": process_group,
                "socket_inode": 789,
                "owner_pids": [process_group],
            }

        output = io.StringIO()
        with (
            mock.patch.object(
                packaged_entrypoint,
                "validate_vista_world_port",
                return_value=runtime.R2_VISTA_WORLD_PORT,
            ),
            mock.patch.object(packaged_entrypoint.time, "sleep", return_value=None),
            contextlib.redirect_stdout(output),
        ):
            code = packaged_entrypoint.run_packaged(
                inputs,
                stop_requested=stop_requested,
                readiness_waiter=ready,
                listener_prover=listener,
            )

        self.assertEqual(code, 0)
        state_path = Path(json.loads(output.getvalue())["state"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["schema"], packaged_entrypoint.R2_STATE_SCHEMA)
        self.assertEqual(state["mode"], packaged_profile.R2_PROFILE_MODE)
        self.assertEqual(state["runtime_profile"], runtime.R2_RUNTIME_PROFILE)
        self.assertEqual(state["camera_profile"], runtime.R2_CAMERA_PROFILE)
        self.assertEqual(state["display"], runtime.R2_DISPLAY)
        self.assertEqual(state["vista_world_port"], runtime.R2_VISTA_WORLD_PORT)
        self.assertEqual(state["width"], runtime.R2_WIDTH)
        self.assertEqual(state["height"], runtime.R2_HEIGHT)
        plan_path = state_path.parent / "launch-plan.json"
        self.assertEqual(
            state["launch_plan_sha256"],
            packaged_profile.sha256_file(plan_path),
        )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["schema"], packaged_entrypoint.R2_PLAN_SCHEMA)
        self.assertEqual(
            listener_calls,
            [(runtime.R2_VISTA_WORLD_PORT, state["process"]["pid"])],
        )
        self.assertFalse(runtime.identity_is_live(state["process"]))

    def test_post_readiness_archive_drift_terminates_owned_process_and_marks_failed(
        self,
    ) -> None:
        self.fixture.write_profile()
        inputs = self.fixture.load_profile()

        def drift_after_ready(process, *, stop_requested):
            self.assertIsNone(process.poll())
            self.fixture.pak.write_bytes(self.fixture.pak.read_bytes() + b"drift")
            return {
                "command_id": "vwc-" + "b" * 24,
                "status": "success",
                "code": "READY",
                "world_revision": packaged_profile.EXPECTED_WORLD_REVISION,
                "session_generation": 0,
                "event_status": "idle",
                "active_event": None,
            }

        with (
            mock.patch.object(
                packaged_entrypoint,
                "validate_vista_world_port",
                return_value=packaged_profile.EXPECTED_PORT,
            ),
            self.assertRaises(packaged_profile.PackagedProfileError),
        ):
            packaged_entrypoint.run_packaged(
                inputs,
                stop_requested=lambda: False,
                readiness_waiter=drift_after_ready,
                listener_prover=lambda port, process_group: {
                    "host": "127.0.0.1",
                    "port": port,
                    "process_group": process_group,
                    "socket_inode": 456,
                    "owner_pids": [process_group],
                },
            )

        state_path, state = runtime.resolve_current_runtime_state(self.fixture.attempt)
        self.assertEqual(state["status"], "failed")
        self.assertFalse(runtime.identity_is_live(state["process"]))
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)

    def test_entrypoint_has_no_runtime_override_or_arbitrary_command_arguments(
        self,
    ) -> None:
        destinations = {
            action.dest for action in packaged_entrypoint.build_parser()._actions
        }
        profile_destinations = {
            action.dest for action in packaged_profile.build_parser()._actions
        }
        self.assertEqual(destinations, {"help", "profile", "profile_sha256"})
        self.assertNotIn("map", destinations)
        self.assertNotIn("gpu", destinations)
        self.assertNotIn("port", destinations)
        self.assertNotIn("command", destinations)
        self.assertNotIn("shell", profile_destinations)
        self.assertNotIn("command", profile_destinations)


if __name__ == "__main__":
    unittest.main()
