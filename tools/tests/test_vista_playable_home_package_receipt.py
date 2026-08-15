from __future__ import annotations

import argparse
import json
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
        self.unreal_pak = base / "UnrealPak"
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
        self.assertEqual(receipt["tools"]["ldd"]["missing"], 0)
        self.assertTrue(receipt["tools"]["unreal_pak"]["map_entry"].endswith("VistaPlayableHome.umap"))
        self.assertEqual(receipt_sha, package.sha256_file(inputs.output))
        self.assertEqual(stat.S_IMODE(inputs.output.stat().st_mode), 0o600)
        self.assertEqual(inputs.output.read_bytes(), package.canonical_json(receipt))
        with self.assertRaises(FileExistsError):
            package.write_exclusive_receipt(inputs.output, receipt)

    def test_tool_calls_are_fixed_argv_without_shell_surface(self) -> None:
        inputs = package.validate_inputs(self.args())
        calls: list[tuple[str, list[str]]] = []

        def recording_runner(name: str, arguments, timeout: float) -> package.ToolResult:
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
        with self.assertRaisesRegex(package.PackageReceiptError, "ATTEMPT_IDENTITY_INVALID"):
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
        with self.assertRaisesRegex(package.PackageReceiptError, "UAT_PHASES_INCOMPLETE"):
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
                return package.ToolResult(name=name, returncode=0, stdout=b'"../../../Other.umap"\n')
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

        with self.assertRaisesRegex(package.PackageReceiptError, "LDD_DEPENDENCY_MISSING"):
            package.verify_package(inputs, missing_library)

        inputs.project_config.write_text(
            inputs.project_config.read_text(encoding="utf-8")
            + "SecurityToken=never-echo-this-value\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(package.PackageReceiptError, "PROJECT_SECRET_REFUSED") as caught:
            package.inspect_project_policy(inputs)
        self.assertNotIn("never-echo-this-value", str(caught.exception))

    def test_archive_secret_and_symlink_are_rejected_without_secret_echo(self) -> None:
        leaked = self.archive / "leaked.ini"
        leaked.write_bytes(b"[/Script/AndroidFileServer]\nSecurityToken=do-not-print\n")
        with self.assertRaisesRegex(package.PackageReceiptError, "SECRET_SCAN_FAILED") as caught:
            package.inspect_archive(self.archive)
        self.assertNotIn("do-not-print", str(caught.exception))

        leaked.unlink()
        link = self.archive / "unsafe-link"
        link.symlink_to(self.launcher)
        with self.assertRaisesRegex(package.PackageReceiptError, "ARCHIVE_(?:ENTRY|SYMLINK)_REFUSED"):
            package.inspect_archive(self.archive)

    def test_archive_walk_error_is_not_silently_accepted(self) -> None:
        def broken_walk(*_args, **kwargs):
            kwargs["onerror"](PermissionError("fixture-only path"))
            return iter(())

        with (
            mock.patch.object(package.os, "walk", side_effect=broken_walk),
            self.assertRaisesRegex(package.PackageReceiptError, "ARCHIVE_ENUMERATION_FAILED"),
        ):
            package.inspect_archive(self.archive)


if __name__ == "__main__":
    unittest.main()
