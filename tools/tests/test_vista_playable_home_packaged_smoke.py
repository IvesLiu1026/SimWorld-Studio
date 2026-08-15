from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import tempfile
import unittest
from unittest import mock


from tools.runtime.vista_playable_home import packaged_smoke as smoke
from tools.ue.vista_playable_home import package_receipt as package
class PackagedSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = pathlib.Path(self.temporary.name).resolve()
        self.attempt = base / "package-linux-development" / "attempt-04-no-afs-clean"
        self.launcher = self.attempt / smoke.LAUNCHER_RELATIVE
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.executable = (
            self.attempt
            / "archive/Linux/VistaPlayableHome/Binaries/Linux/VistaPlayableHome"
        )
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b"ELF-fixture\n")
        self.executable.chmod(0o700)
        self.pak = (
            self.attempt
            / "archive/Linux/VistaPlayableHome/Content/Paks/VistaPlayableHome-Linux.pak"
        )
        self.pak.parent.mkdir(parents=True)
        self.pak.write_bytes(b"PAK-fixture\n")
        self.engine_root = base / "UE"
        self.unreal_pak = self.engine_root / "Engine/Binaries/Linux/UnrealPak"
        self.unreal_pak.parent.mkdir(parents=True)
        self.unreal_pak.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.unreal_pak.chmod(0o700)
        engine_relative = pathlib.Path(
            "Engine/Binaries/ThirdParty/Vulkan/Linux/libVkLayer_khronos_validation.so"
        )
        archived_engine_file = self.attempt / "archive" / "Linux" / engine_relative
        upstream_engine_file = self.engine_root / engine_relative
        archived_engine_file.parent.mkdir(parents=True)
        upstream_engine_file.parent.mkdir(parents=True)
        token_like_engine_bytes = b"engine-fixture\x00sk-" + b"Z" * 40 + b"\x00"
        archived_engine_file.write_bytes(token_like_engine_bytes)
        upstream_engine_file.write_bytes(token_like_engine_bytes)
        archive_observation = package.inspect_archive(
            self.attempt / "archive" / "Linux",
            trusted_engine_root=self.engine_root,
        )
        self.receipt_path = self.attempt / smoke.PACKAGE_RECEIPT_RELATIVE
        self.package_receipt = {
            "schema": smoke.PACKAGE_RECEIPT_SCHEMA,
            "status": "accepted",
            "attempt_root": str(self.attempt),
            "bindings": {
                "map_path": smoke.EXPECTED_MAP_PATH,
                "world_revision": smoke.DEFAULT_WORLD_REVISION,
                "source_commit": "a" * 40,
            },
            "artifacts": {
                "launcher": {
                    "relative_path": smoke.LAUNCHER_RELATIVE.as_posix(),
                    "sha256": smoke.sha256_file(self.launcher),
                    "bytes": self.launcher.stat().st_size,
                    "executable": True,
                },
                "executable": {
                    "relative_path": self.executable.relative_to(self.attempt).as_posix(),
                    "sha256": smoke.sha256_file(self.executable),
                    "bytes": self.executable.stat().st_size,
                    "executable": True,
                },
                "pak": {
                    "relative_path": self.pak.relative_to(self.attempt).as_posix(),
                    "sha256": smoke.sha256_file(self.pak),
                    "bytes": self.pak.stat().st_size,
                    "executable": False,
                },
            },
            "archive": {
                **archive_observation,
            },
            "trusted_upstream": {
                "policy": "engine-root-derived-from-pinned-unrealpak/v1",
                "engine_root": str(self.engine_root),
                "unreal_pak": str(self.unreal_pak),
                "unreal_pak_sha256": smoke.sha256_file(self.unreal_pak),
            },
        }
        self.receipt_path.write_bytes(smoke.canonical_json(self.package_receipt))

    def args(self, attempt: str = "attempt-01") -> argparse.Namespace:
        return argparse.Namespace(
            package_attempt=self.attempt,
            package_receipt_sha256=smoke.sha256_file(self.receipt_path),
            output_dir=self.attempt / "smoke" / attempt,
            vista_world_port=55777,
            timeout_seconds=5.0,
            apply=False,
        )

    def inputs(self, attempt: str = "attempt-01") -> smoke.SmokeInputs:
        with mock.patch.object(smoke, "validate_vista_world_port", return_value=55777):
            return smoke.validate_inputs(self.args(attempt))

    def test_command_and_environment_are_fixed_nullrhi_and_secret_free(self) -> None:
        inputs = self.inputs()
        command = smoke.build_command(inputs)
        with mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "do-not-copy",
                "OPENAI_API_KEY": "do-not-copy",
                "STUDIO_ACCESS_TOKEN": "do-not-copy",
                "DISPLAY": ":117",
                "WAYLAND_DISPLAY": "wayland-0",
                "PATH": "/usr/bin:/bin",
            },
            clear=True,
        ):
            environment = smoke.sanitized_environment(inputs)

        self.assertEqual(command[0], str(self.launcher))
        self.assertEqual(command[1], smoke.EXPECTED_MAP_PATH)
        self.assertIn("-nullrhi", command)
        self.assertIn("-VistaWorldPort=55777", command)
        self.assertNotIn("-game", command)
        self.assertFalse(any("graphicsadapter" in value.lower() for value in command))
        self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "")
        self.assertNotIn("DISPLAY", environment)
        self.assertNotIn("WAYLAND_DISPLAY", environment)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("STUDIO_ACCESS_TOKEN", environment)

    def test_real_owned_process_group_is_probed_terminated_and_sealed(self) -> None:
        inputs = self.inputs()
        probe_calls = 0

        def ready(port: int, *, expected_revision: str, timeout: float):
            nonlocal probe_calls
            probe_calls += 1
            self.assertEqual(port, 55777)
            self.assertEqual(expected_revision, smoke.DEFAULT_WORLD_REVISION)
            self.assertEqual(timeout, 1.0)
            return {
                "command_id": "vwc-" + "a" * 24,
                "status": "success",
                "code": "READY",
                "world_revision": smoke.DEFAULT_WORLD_REVISION,
                "session_generation": 0,
                "event_status": "inactive",
                "active_event": None,
            }

        receipt, receipt_sha = smoke.run_smoke(
            inputs,
            probe=ready,
            listener_prover=lambda port, process_group: {
                "host": "127.0.0.1",
                "port": port,
                "process_group": process_group,
                "socket_inode": 123,
                "owner_pids": [process_group],
            },
        )

        output = inputs.output_dir / "smoke-receipt.json"
        self.assertEqual(receipt["status"], "accepted")
        self.assertTrue(receipt["termination"]["process_exited"])
        self.assertEqual(receipt["bindings"]["host"], "127.0.0.1")
        self.assertEqual(receipt["bindings"]["port"], 55777)
        self.assertEqual(probe_calls, 2)
        self.assertEqual(receipt["readiness"]["probe_count"], 2)
        self.assertEqual(
            receipt["archive_verification"]["before_launch"][
                "trusted_upstream_exemption_count"
            ],
            1,
        )
        self.assertEqual(
            receipt["archive_verification"]["after_termination"][
                "trusted_upstream_exemption_count"
            ],
            1,
        )
        self.assertEqual(receipt_sha, smoke.sha256_file(output))
        self.assertEqual(output.read_bytes(), smoke.canonical_json(receipt))
        self.assertFalse((self.attempt / "game-runtime" / "current.json").exists())
        with self.assertRaises(FileExistsError):
            smoke._write_receipt(output, receipt)

    def test_probe_failure_still_terminates_and_seals_failed_receipt(self) -> None:
        inputs = self.inputs("attempt-03")

        def refuse(*_args, **_kwargs):
            raise smoke.PackagedSmokeError("FORCED_PROBE_FAILURE", "fixture refusal")

        receipt, receipt_sha = smoke.run_smoke(
            inputs,
            probe=refuse,
            listener_prover=lambda _port, _process_group: {},
        )

        output = inputs.output_dir / "smoke-receipt.json"
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error"]["code"], "FORCED_PROBE_FAILURE")
        self.assertTrue(receipt["termination"]["process_exited"])
        self.assertEqual(receipt_sha, smoke.sha256_file(output))
        self.assertFalse((self.attempt / "game-runtime" / "current.json").exists())

    def test_executable_and_pak_drift_cannot_reach_accepted_smoke(self) -> None:
        executable_inputs = self.inputs("attempt-04")
        original_executable = self.executable.read_bytes()
        self.executable.write_bytes(original_executable + b"tamper")
        executable_receipt, _sha = smoke.run_smoke(
            executable_inputs,
            listener_prover=lambda _port, _process_group: {},
        )
        self.assertEqual(executable_receipt["status"], "failed")
        self.assertEqual(executable_receipt["error"]["code"], "PACKAGE_ARCHIVE_DRIFT")

        self.executable.write_bytes(original_executable)
        self.executable.chmod(0o700)
        pak_inputs = self.inputs("attempt-05")
        self.pak.write_bytes(self.pak.read_bytes() + b"tamper")
        pak_receipt, _sha = smoke.run_smoke(
            pak_inputs,
            listener_prover=lambda _port, _process_group: {},
        )
        self.assertEqual(pak_receipt["status"], "failed")
        self.assertEqual(pak_receipt["error"]["code"], "PACKAGE_ARCHIVE_DRIFT")

    def test_foreign_listener_is_rejected_by_process_group_proof(self) -> None:
        with (
            mock.patch.object(smoke, "_listening_loopback_inodes", return_value={111}),
            mock.patch.object(
                smoke, "_process_group_socket_owners", return_value={111: []}
            ),
            self.assertRaisesRegex(smoke.PackagedSmokeError, "LISTENER_OWNERSHIP_INVALID"),
        ):
            smoke.prove_loopback_listener_ownership(55777, 424242)

    def test_real_loopback_listener_is_attributed_to_its_process_group(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            proof = smoke.prove_loopback_listener_ownership(
                listener.getsockname()[1], os.getpgrp()
            )
        self.assertEqual(proof["process_group"], os.getpgrp())
        self.assertIn(os.getpid(), proof["owner_pids"])

    def test_receipt_pin_launcher_pin_and_output_scope_fail_closed(self) -> None:
        args = self.args()
        args.package_receipt_sha256 = "0" * 64
        with (
            mock.patch.object(smoke, "validate_vista_world_port", return_value=55777),
            self.assertRaisesRegex(smoke.PackagedSmokeError, "PACKAGE_PIN_MISMATCH"),
        ):
            smoke.validate_inputs(args)

        self.launcher.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        with (
            mock.patch.object(smoke, "validate_vista_world_port", return_value=55777),
            self.assertRaisesRegex(smoke.PackagedSmokeError, "LAUNCHER_PIN_MISMATCH"),
        ):
            smoke.validate_inputs(self.args())

        self.launcher.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.package_receipt["artifacts"]["launcher"]["sha256"] = smoke.sha256_file(self.launcher)
        self.receipt_path.write_bytes(smoke.canonical_json(self.package_receipt))
        args = self.args()
        args.output_dir = self.attempt.parent / "outside" / "attempt-01"
        with (
            mock.patch.object(smoke, "validate_vista_world_port", return_value=55777),
            self.assertRaisesRegex(smoke.PackagedSmokeError, "OUTPUT_IDENTITY_INVALID"),
        ):
            smoke.validate_inputs(args)

    def test_preflight_plan_has_no_arbitrary_command_surface(self) -> None:
        inputs = self.inputs("attempt-02")
        rendered = json.dumps(smoke.plan(inputs), sort_keys=True)
        parser_destinations = {action.dest for action in smoke.build_parser()._actions}

        self.assertNotIn("command", parser_destinations)
        self.assertNotIn("shell", parser_destinations)
        self.assertNotIn("environment", parser_destinations)
        self.assertEqual(
            smoke.sanitized_environment(inputs)["HOME"],
            str(inputs.output_dir / "home"),
        )
        self.assertNotIn("STUDIO_ACCESS_TOKEN", rendered)


if __name__ == "__main__":
    unittest.main()
