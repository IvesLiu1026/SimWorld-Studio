from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock


from tools.ue.vista_playable_home import package_receipt as package


class PackageReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = pathlib.Path(self.temporary.name).resolve()
        self.attempt = base / "package-linux-development" / "attempt-04-no-afs-clean"
        self.archive = self.attempt / "archive" / "Linux"
        self.launcher = self.archive / "VistaPlayableHome.sh"
        self.executable = (
            self.archive
            / "VistaPlayableHome"
            / "Binaries"
            / "Linux"
            / "VistaPlayableHome"
        )
        self.pak = (
            self.archive
            / "VistaPlayableHome"
            / "Content"
            / "Paks"
            / "VistaPlayableHome-Linux.pak"
        )
        self.pak.parent.mkdir(parents=True)
        self.executable.parent.mkdir(parents=True)
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.executable.write_bytes(b"ELF-fixture\n")
        self.executable.chmod(0o700)
        self.pak.write_bytes(b"PAK-fixture\n")
        self.pak.chmod(0o644)
        project = self.attempt / package.PROJECT_RELATIVE
        project.parent.mkdir(parents=True)
        project.write_text(
            json.dumps(
                {
                    "Plugins": [
                        {"Name": "VistaPlayableHome", "Enabled": True},
                        {"Name": "AndroidFileServer", "Enabled": False},
                        {"Name": "PythonScriptPlugin", "Enabled": False},
                        {"Name": "EditorScriptingUtilities", "Enabled": False},
                        {"Name": "Interchange", "Enabled": False},
                    ],
                    "Modules": [
                        {
                            "LoadingPhase": "Default",
                            "Name": "VistaPlayableHomeHost",
                            "Type": "Runtime",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        config = self.attempt / package.PROJECT_CONFIG_RELATIVE
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "\n".join(
                (
                    "[/Script/EngineSettings.GameMapsSettings]",
                    f"GameDefaultMap={package.EXPECTED_MAP_PATH}",
                    "GlobalDefaultGameMode=/Script/VistaPlayableHome.VistaPlayableHomeGameMode",
                    "[/Script/AndroidFileServerEditor.AndroidFileServerRuntimeSettings]",
                    "bEnablePlugin=False",
                    "bAllowNetworkConnection=False",
                    "bCompileAFSProject=False",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        config.chmod(0o644)
        (self.attempt / "runuat.log").write_text(
            " ".join(
                (
                    "BuildCookRun",
                    "-platform=Linux",
                    "-clientconfig=Development",
                    f"-map={package.EXPECTED_MAP_PATH}",
                    "-pak",
                    "-skipiostore",
                    "-archive",
                )
            )
            + "\n"
            + "\n".join(package.UAT_SUCCESS_PHASES)
            + "\n",
            encoding="utf-8",
        )
        self.source_result = base / "result-receipt.json"
        self.source_result.write_text(
            json.dumps(
                {
                    "schema_version": package.SOURCE_BUILD_SCHEMA,
                    "status": "accepted_candidate",
                    "map_path": package.EXPECTED_MAP_PATH,
                    "revision": package.EXPECTED_REVISION,
                    "attempt_root": str(base),
                }
            ),
            encoding="utf-8",
        )
        self.source_acceptance = base / "runtime-acceptance-final.json"
        self.source_acceptance.write_text(
            json.dumps(
                {
                    "schema": package.SOURCE_ACCEPTANCE_SCHEMA,
                    "status": "accepted",
                    "bindings": {
                        "build_result": str(self.source_result),
                        "build_result_sha256": package.sha256_file(self.source_result),
                        "source_commit": "a" * 40,
                        "map_path": package.EXPECTED_MAP_PATH,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.unreal_pak = base / "UE" / "Engine" / "Binaries" / "Linux" / "UnrealPak"
        self.unreal_pak.parent.mkdir(parents=True)
        self.unreal_pak.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.unreal_pak.chmod(0o700)

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            attempt_root=self.attempt,
            source_build_result=self.source_result,
            source_build_result_sha256=package.sha256_file(self.source_result),
            source_acceptance=self.source_acceptance,
            source_acceptance_sha256=package.sha256_file(self.source_acceptance),
            source_commit="a" * 40,
            map_path=package.EXPECTED_MAP_PATH,
            unreal_pak=self.unreal_pak,
        )

    def enable_r2_source_chain(self) -> None:
        source_result = {
            "schema_version": package.SOURCE_BUILD_SCHEMA,
            "status": "accepted_candidate",
            "timestamp_utc": "2026-08-16T08:00:00+00:00",
            "attempt_root": str(self.source_result.parent),
            "revision": package.EXPECTED_REVISION,
            "map_path": package.EXPECTED_MAP_PATH,
            "execution_sha256": "1" * 64,
            "import_receipt_sha256": "2" * 64,
            "scene_receipt_sha256": "3" * 64,
            "copy_methods": {"copy": 3},
            "runtime_play_proof": "pending",
            "visual_profile_id": package.R2_RUNTIME_PROFILE,
            "visual_profile_sha256": "4" * 64,
            "visual_profile_content_digest": "5" * 64,
            "renderer_profile_request_sha256": "6" * 64,
            "renderer_profile_request_content_digest": "7" * 64,
            "renderer_runtime_observation": "pending",
            "base_scene_receipt_sha256": "3" * 64,
            "presentation_import_receipt_sha256": "8" * 64,
            "presentation_scene_receipt_sha256": "9" * 64,
            "presentation_manifest_sha256": "a" * 64,
            "presentation_artifact_receipt_sha256": "b" * 64,
            "presentation_bundle_count": package.R2_PRESENTATION_BUNDLE_COUNT,
            "presentation_collision_policy": package.R2_PRESENTATION_COLLISION_POLICY,
            "presentation_ue_import_observation": "verified_by_commandlet",
            "presentation_runtime_play_proof": "pending",
        }
        source_result["content_digest"] = package._content_digest(source_result)
        self.source_result.write_text(json.dumps(source_result), encoding="utf-8")
        source_pin = package.sha256_file(self.source_result)
        acceptance = {
            "schema": package.R2_SOURCE_ACCEPTANCE_SCHEMA,
            "status": "accepted",
            "created_at": "2026-08-16T08:10:00+00:00",
            "completed_at": "2026-08-16T08:11:00+00:00",
            "output": str(self.source_acceptance),
            "bindings": {
                "workspace": str(self.source_result.parent),
                "runtime_state": str(self.source_result.parent / "runtime-state.json"),
                "runtime_state_sha256": "c" * 64,
                "build_result": str(self.source_result),
                "build_result_sha256": source_pin,
                "repo_root": str(self.source_result.parent / "repo"),
                "source_commit": "a" * 40,
                "source_clean": True,
                "host": "127.0.0.1",
                "port": package.R2_VISTA_WORLD_PORT,
                "world_revision": package.EXPECTED_REVISION,
                "map_path": package.EXPECTED_MAP_PATH,
                "project": str(self.source_result.parent / "project.uproject"),
                "runtime_profile": package.R2_RUNTIME_PROFILE,
                "camera_profile": package.R2_CAMERA_PROFILE,
                "display": package.R2_DISPLAY,
                "gpu": package.R2_GPU,
                "width": package.R2_WIDTH,
                "height": package.R2_HEIGHT,
                "fps": package.R2_FPS,
                "launch_plan": str(self.source_result.parent / "launch-plan.json"),
                "launch_plan_sha256": "d" * 64,
            },
            "initial_generation": 0,
            "final_generation": 9,
            "checks": [{"step": "fixture.observed"}],
            "error": None,
        }
        self.source_acceptance.write_text(json.dumps(acceptance), encoding="utf-8")

    @staticmethod
    def runner(name: str, arguments, timeout: float) -> package.ToolResult:
        del timeout
        if name == "file":
            output = f"{arguments[-1]}: ELF 64-bit LSB pie executable, x86-64\n"
        elif name == "readelf" and arguments[0] == "-h":
            output = "Class: ELF64\nType: DYN (Position-Independent Executable)\nMachine: Advanced Micro Devices X86-64\n"
        elif name == "readelf" and arguments[0] == "-l":
            output = "[Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]\n"
        elif name == "ldd":
            output = "linux-vdso.so.1 =>  (0x00007fff)\nlibc.so.6 => /lib/libc.so.6\n"
        elif name == "UnrealPak":
            output = (
                '"../../../VistaPlayableHome/Content/VISTA/PlayableHome/'
                'vista_playable_home_r1/Maps/VistaPlayableHome.umap" offset: 0\n'
            )
        else:  # pragma: no cover - demonstrates the closed allowlist in a failure.
            raise AssertionError(name)
        return package.ToolResult(name=name, returncode=0, stdout=output.encode())

    def test_fixed_package_is_verified_and_receipt_is_o_excl_canonical(self) -> None:
        inputs = package.validate_inputs(self.args())
        receipt = package.verify_package(inputs, self.runner)
        receipt_sha = package.write_exclusive_receipt(inputs.output, receipt)

        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["bindings"]["source_commit"], "a" * 40)
        self.assertEqual(receipt["bindings"]["map_path"], package.EXPECTED_MAP_PATH)
        self.assertEqual(receipt["archive"]["secret_scan"]["matches"], 0)
        self.assertEqual(
            receipt["trusted_upstream"]["engine_root"],
            str(self.unreal_pak.parents[3]),
        )
        self.assertEqual(
            receipt["trusted_upstream"]["unreal_pak_sha256"],
            package.sha256_file(self.unreal_pak),
        )
        self.assertEqual(receipt["tools"]["ldd"]["missing"], 0)
        self.assertTrue(
            receipt["tools"]["unreal_pak"]["map_entry"].endswith(
                "VistaPlayableHome.umap"
            )
        )
        self.assertEqual(receipt_sha, package.sha256_file(inputs.output))
        self.assertEqual(stat.S_IMODE(inputs.output.stat().st_mode), 0o600)
        self.assertEqual(inputs.output.read_bytes(), package.canonical_json(receipt))
        with self.assertRaises(FileExistsError):
            package.write_exclusive_receipt(inputs.output, receipt)

    def test_receipt_mode_is_deterministic_under_restrictive_umask(self) -> None:
        inputs = package.validate_inputs(self.args())
        receipt = package.verify_package(inputs, self.runner)

        previous_umask = os.umask(0o777)
        try:
            package.write_exclusive_receipt(inputs.output, receipt)
        finally:
            os.umask(previous_umask)

        self.assertEqual(stat.S_IMODE(inputs.output.stat().st_mode), 0o600)
        self.assertEqual(inputs.output.read_bytes(), package.canonical_json(receipt))

    def test_receipt_mode_failure_removes_reserved_output(self) -> None:
        inputs = package.validate_inputs(self.args())
        receipt = package.verify_package(inputs, self.runner)

        with mock.patch.object(
            package.os, "fchmod", side_effect=OSError("fixture fchmod failure")
        ):
            with self.assertRaises(OSError):
                package.write_exclusive_receipt(inputs.output, receipt)

        self.assertFalse(inputs.output.exists())

    def test_fdopen_close_then_raise_preserves_error_and_removes_output(self) -> None:
        inputs = package.validate_inputs(self.args())
        receipt = package.verify_package(inputs, self.runner)

        def close_then_raise(descriptor, *_args, **_kwargs):
            os.close(descriptor)
            raise RuntimeError("fixture fdopen failure")

        with mock.patch.object(package.os, "fdopen", side_effect=close_then_raise):
            with self.assertRaisesRegex(RuntimeError, "fixture fdopen failure"):
                package.write_exclusive_receipt(inputs.output, receipt)

        self.assertFalse(inputs.output.exists())

    def test_realistic_r2_package_retains_the_observed_source_chain(self) -> None:
        self.enable_r2_source_chain()
        inputs = package.validate_inputs(self.args())
        self.assertEqual(inputs.runtime_profile, package.R2_RUNTIME_PROFILE)
        receipt = package.verify_package(inputs, self.runner)
        self.assertEqual(receipt["schema"], package.R2_EXACT_MODE_RECEIPT_SCHEMA)
        bindings = receipt["bindings"]
        self.assertEqual(bindings["runtime_profile"], package.R2_RUNTIME_PROFILE)
        self.assertEqual(bindings["camera_profile"], package.R2_CAMERA_PROFILE)
        self.assertEqual(bindings["accepted_display"], package.R2_DISPLAY)
        self.assertEqual(
            bindings["accepted_vista_world_port"], package.R2_VISTA_WORLD_PORT
        )
        self.assertEqual(bindings["visual_profile_sha256"], "4" * 64)
        self.assertEqual(bindings["presentation_manifest_sha256"], "a" * 64)
        self.assertEqual(
            receipt["archive"]["schema"], package.ARCHIVE_SCHEMA_EXACT_MODE_V2
        )
        self.assertEqual(
            receipt["archive"]["algorithm"],
            package.ARCHIVE_ALGORITHM_EXACT_MODE_V2,
        )
        for name, path in (
            ("launcher", self.launcher),
            ("executable", self.executable),
            ("pak", self.pak),
        ):
            self.assertEqual(
                receipt["artifacts"][name]["mode"],
                stat.S_IMODE(path.stat().st_mode),
            )
        self.assertEqual(
            receipt["trusted_upstream"]["unreal_pak_mode"],
            stat.S_IMODE(self.unreal_pak.stat().st_mode),
        )
        self.assertEqual(
            receipt["project_policy"]["project_descriptor_mode"],
            stat.S_IMODE(inputs.project_descriptor.stat().st_mode),
        )
        self.assertEqual(
            receipt["project_policy"]["project_config_mode"],
            stat.S_IMODE(inputs.project_config.stat().st_mode),
        )

    def test_exact_archive_tree_attests_non_executable_mode_bits(self) -> None:
        first = self.archive / "VistaPlayableHome" / "Content" / "mode-0644.bin"
        second = self.archive / "VistaPlayableHome" / "Content" / "mode-0600.bin"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"mode fixture one\n")
        second.write_bytes(b"mode fixture two\n")
        first.chmod(0o644)
        second.chmod(0o600)

        legacy_before = package.inspect_archive(self.archive)
        exact_before = package.inspect_archive(self.archive, exact_modes=True)
        self.assertNotIn("schema", legacy_before)
        self.assertEqual(legacy_before["algorithm"], package.ARCHIVE_ALGORITHM_V1)
        self.assertEqual(exact_before["schema"], package.ARCHIVE_SCHEMA_EXACT_MODE_V2)

        first.chmod(0o600)
        legacy_after_first = package.inspect_archive(self.archive)
        exact_after_first = package.inspect_archive(self.archive, exact_modes=True)
        self.assertEqual(
            legacy_before["tree_sha256"], legacy_after_first["tree_sha256"]
        )
        self.assertNotEqual(
            exact_before["tree_sha256"], exact_after_first["tree_sha256"]
        )

        second.chmod(0o640)
        exact_after_second = package.inspect_archive(self.archive, exact_modes=True)
        self.assertNotEqual(
            exact_after_first["tree_sha256"], exact_after_second["tree_sha256"]
        )

    def test_exact_package_final_closure_rejects_phase_mode_exchange(self) -> None:
        self.enable_r2_source_chain()
        inputs = package.validate_inputs(self.args())
        cases = (
            (inputs.project_config, 0o600, "file", "PACKAGE_CHANGED"),
            (inputs.pak, 0o600, "UnrealPak", "PACKAGE_CHANGED"),
            (inputs.unreal_pak, 0o600, "UnrealPak", "UNREALPAK_CHANGED"),
        )
        for target, changed_mode, trigger, expected_code in cases:
            original_mode = stat.S_IMODE(target.stat().st_mode)
            with self.subTest(target=target.name):

                def exchanging_runner(name, arguments, timeout):
                    result = self.runner(name, arguments, timeout)
                    if name == trigger:
                        target.chmod(changed_mode)
                    return result

                with self.assertRaisesRegex(
                    package.PackageReceiptError,
                    expected_code,
                ):
                    package.verify_package(inputs, exchanging_runner)
                target.chmod(original_mode)

    def test_realistic_r2_source_profile_or_digest_drift_is_rejected(self) -> None:
        self.enable_r2_source_chain()
        acceptance = json.loads(self.source_acceptance.read_text(encoding="utf-8"))
        acceptance["bindings"]["camera_profile"] = "default"
        self.source_acceptance.write_text(json.dumps(acceptance), encoding="utf-8")
        with self.assertRaisesRegex(
            package.PackageReceiptError, "SOURCE_ACCEPTANCE_INVALID"
        ):
            package.validate_inputs(self.args())

        self.enable_r2_source_chain()
        source = json.loads(self.source_result.read_text(encoding="utf-8"))
        source["presentation_manifest_sha256"] = "f" * 64
        self.source_result.write_text(json.dumps(source), encoding="utf-8")
        acceptance = json.loads(self.source_acceptance.read_text(encoding="utf-8"))
        acceptance["bindings"]["build_result_sha256"] = package.sha256_file(
            self.source_result
        )
        self.source_acceptance.write_text(json.dumps(acceptance), encoding="utf-8")
        with self.assertRaisesRegex(
            package.PackageReceiptError, "SOURCE_RESULT_INVALID"
        ):
            package.validate_inputs(self.args())

    def test_tool_calls_are_fixed_argv_without_shell_surface(self) -> None:
        inputs = package.validate_inputs(self.args())
        calls: list[tuple[str, list[str]]] = []

        def recording_runner(
            name: str, arguments, timeout: float
        ) -> package.ToolResult:
            calls.append((name, list(arguments)))
            return self.runner(name, arguments, timeout)

        package.verify_package(inputs, recording_runner)

        self.assertEqual(
            [name for name, _arguments in calls],
            ["file", "readelf", "readelf", "ldd", "UnrealPak"],
        )
        self.assertEqual(calls[0][1], [str(self.executable)])
        self.assertEqual(calls[1][1], ["-h", str(self.executable)])
        self.assertEqual(calls[2][1], ["-l", str(self.executable)])
        self.assertEqual(calls[3][1], [str(self.executable)])
        self.assertEqual(calls[4][1], [str(self.unreal_pak), str(self.pak), "-List"])
        self.assertNotIn("shell", package.run_fixed_tool.__code__.co_varnames)

    def test_source_pin_map_and_exact_attempt_identity_fail_closed(self) -> None:
        args = self.args()
        args.source_build_result_sha256 = "0" * 64
        with self.assertRaisesRegex(package.PackageReceiptError, "SOURCE_PIN_MISMATCH"):
            package.validate_inputs(args)

        args = self.args()
        args.map_path = "/Game/Other/Map"
        with self.assertRaisesRegex(package.PackageReceiptError, "MAP_MISMATCH"):
            package.validate_inputs(args)

        args = self.args()
        args.attempt_root = self.attempt.parent / "unsafe_attempt"
        args.attempt_root.mkdir()
        with self.assertRaisesRegex(
            package.PackageReceiptError, "ATTEMPT_IDENTITY_INVALID"
        ):
            package.validate_inputs(args)

    def test_uat_phase_or_exact_map_entry_drift_is_rejected(self) -> None:
        inputs = package.validate_inputs(self.args())
        (self.attempt / "runuat.log").write_text(
            "BuildCookRun -platform=Linux -clientconfig=Development "
            f"-map={package.EXPECTED_MAP_PATH} -pak -skipiostore -archive\n"
            + "\n".join(
                phase
                for phase in package.UAT_SUCCESS_PHASES
                if phase != "********** PACKAGE COMMAND COMPLETED **********"
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            package.PackageReceiptError, "UAT_PHASES_INCOMPLETE"
        ):
            package.verify_package(inputs, self.runner)

        (self.attempt / "runuat.log").write_text(
            "BuildCookRun -platform=Linux -clientconfig=Development "
            f"-map={package.EXPECTED_MAP_PATH} -pak -skipiostore -archive\n"
            + "\n".join(package.UAT_SUCCESS_PHASES)
            + "\n",
            encoding="utf-8",
        )

        def wrong_map(name: str, arguments, timeout: float) -> package.ToolResult:
            result = self.runner(name, arguments, timeout)
            if name == "UnrealPak":
                return package.ToolResult(
                    name=name, returncode=0, stdout=b'"../../../Other.umap"\n'
                )
            return result

        with self.assertRaisesRegex(package.PackageReceiptError, "PAK_MAP_MISSING"):
            package.verify_package(inputs, wrong_map)

    def test_nonzero_uat_ldd_gap_and_project_token_are_rejected(self) -> None:
        inputs = package.validate_inputs(self.args())
        log = self.attempt / "runuat.log"
        log.write_text(
            log.read_text(encoding="utf-8").replace(
                "AutomationTool exiting with ExitCode=0 (Success)",
                "AutomationTool exiting with ExitCode=1 (Error_Unknown)",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(package.PackageReceiptError, "UAT_EXIT_INVALID"):
            package.inspect_uat_log(log)

        log.write_text(
            "BuildCookRun -platform=Linux -clientconfig=Development "
            f"-map={package.EXPECTED_MAP_PATH} -pak -skipiostore -archive\n"
            + "\n".join(package.UAT_SUCCESS_PHASES)
            + "\n",
            encoding="utf-8",
        )

        def missing_library(name: str, arguments, timeout: float) -> package.ToolResult:
            result = self.runner(name, arguments, timeout)
            if name == "ldd":
                return package.ToolResult(
                    name=name, returncode=0, stdout=b"libMissing.so => not found\n"
                )
            return result

        with self.assertRaisesRegex(
            package.PackageReceiptError, "LDD_DEPENDENCY_MISSING"
        ):
            package.verify_package(inputs, missing_library)

        inputs.project_config.write_text(
            inputs.project_config.read_text(encoding="utf-8")
            + "SecurityToken=never-echo-this-value\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            package.PackageReceiptError, "PROJECT_SECRET_REFUSED"
        ) as caught:
            package.inspect_project_policy(inputs)
        self.assertNotIn("never-echo-this-value", str(caught.exception))

    def test_archive_secret_and_symlink_are_rejected_without_secret_echo(self) -> None:
        leaked = self.archive / "leaked.ini"
        leaked.write_bytes(b"[/Script/AndroidFileServer]\nSecurityToken=do-not-print\n")
        with self.assertRaisesRegex(
            package.PackageReceiptError, "SECRET_SCAN_FAILED"
        ) as caught:
            package.inspect_archive(self.archive)
        self.assertNotIn("do-not-print", str(caught.exception))

        leaked.unlink()
        link = self.archive / "unsafe-link"
        link.symlink_to(self.launcher)
        with self.assertRaisesRegex(
            package.PackageReceiptError, "ARCHIVE_(?:ENTRY|SYMLINK)_REFUSED"
        ):
            package.inspect_archive(self.archive)

    def test_byte_identical_engine_false_positive_has_bounded_exemption(self) -> None:
        relative = pathlib.Path(
            "Engine/Binaries/ThirdParty/Vulkan/Linux/libVkLayer_khronos_validation.so"
        )
        archived = self.archive / relative
        upstream = self.unreal_pak.parents[3] / relative
        archived.parent.mkdir(parents=True)
        upstream.parent.mkdir(parents=True)
        token_like_bytes = b"trusted-engine-fixture\x00sk-" + b"A" * 40 + b"\x00"
        archived.write_bytes(token_like_bytes)
        upstream.write_bytes(token_like_bytes)

        observation = package.inspect_archive(
            self.archive, trusted_engine_root=self.unreal_pak.parents[3]
        )
        scan = observation["secret_scan"]
        self.assertEqual(scan["matches"], 0)
        self.assertEqual(scan["pattern_hits"], 1)
        self.assertEqual(scan["trusted_upstream_exemption_count"], 1)
        exemption = scan["trusted_upstream_exemptions"][0]
        self.assertEqual(exemption["archive_relative_path"], relative.as_posix())
        self.assertEqual(exemption["upstream_relative_path"], relative.as_posix())
        self.assertEqual(exemption["rules"], ["openai_token"])
        self.assertNotIn((b"sk-" + b"A" * 40).decode(), json.dumps(exemption))

        archived.write_bytes(token_like_bytes + b"modified")
        with self.assertRaisesRegex(package.PackageReceiptError, "SECRET_SCAN_FAILED"):
            package.inspect_archive(
                self.archive, trusted_engine_root=self.unreal_pak.parents[3]
            )

    def test_engine_path_exemption_never_applies_to_package_content(self) -> None:
        project_file = self.archive / "VistaPlayableHome" / "Config" / "Runtime.ini"
        project_file.parent.mkdir(parents=True)
        project_file.write_bytes(b"sk-" + b"B" * 40)
        with self.assertRaisesRegex(package.PackageReceiptError, "SECRET_SCAN_FAILED"):
            package.inspect_archive(
                self.archive, trusted_engine_root=self.unreal_pak.parents[3]
            )

    def test_archive_walk_error_is_not_silently_accepted(self) -> None:
        def broken_walk(*_args, **kwargs):
            kwargs["onerror"](PermissionError("fixture-only path"))
            return iter(())

        with (
            mock.patch.object(package.os, "walk", side_effect=broken_walk),
            self.assertRaisesRegex(
                package.PackageReceiptError, "ARCHIVE_ENUMERATION_FAILED"
            ),
        ):
            package.inspect_archive(self.archive)


if __name__ == "__main__":
    unittest.main()
