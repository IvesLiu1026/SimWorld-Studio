from __future__ import annotations

import argparse
import array
import json
import os
import pathlib
import socket
import stat
import subprocess
import sys
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
        self.executable.write_text(
            "#!/usr/bin/python3\nimport time\ntime.sleep(60)\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o700)
        self.pak = (
            self.attempt
            / "archive/Linux/VistaPlayableHome/Content/Paks/VistaPlayableHome-Linux.pak"
        )
        self.pak.parent.mkdir(parents=True)
        self.pak.write_bytes(b"PAK-fixture\n")
        self.pak.chmod(0o600)
        self.mode_0644 = (
            self.attempt
            / "archive/Linux/VistaPlayableHome/Content/Fixtures/mode-0644.bin"
        )
        self.mode_0600 = self.mode_0644.with_name("mode-0600.bin")
        self.mode_0644.parent.mkdir(parents=True)
        self.mode_0644.write_bytes(b"mode fixture 0644\n")
        self.mode_0644.chmod(0o644)
        self.mode_0600.write_bytes(b"mode fixture 0600\n")
        self.mode_0600.chmod(0o600)
        self.project_descriptor = self.attempt / package.PROJECT_RELATIVE
        self.project_descriptor.parent.mkdir(parents=True)
        self.project_descriptor.write_text("{}\n", encoding="utf-8")
        self.project_descriptor.chmod(0o644)
        self.project_config = self.attempt / package.PROJECT_CONFIG_RELATIVE
        self.project_config.parent.mkdir(parents=True)
        self.project_config.write_text("[fixture]\n", encoding="utf-8")
        self.project_config.chmod(0o640)
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
                    "relative_path": self.executable.relative_to(
                        self.attempt
                    ).as_posix(),
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
        self.receipt_path.chmod(0o600)

    def enable_r2_receipt(self, *, exact_modes: bool) -> None:
        self.package_receipt["schema"] = (
            smoke.R2_EXACT_MODE_PACKAGE_RECEIPT_SCHEMA
            if exact_modes
            else smoke.R2_PACKAGE_RECEIPT_SCHEMA
        )
        self.package_receipt["bindings"].update(
            {
                "runtime_profile": package.R2_RUNTIME_PROFILE,
                "camera_profile": package.R2_CAMERA_PROFILE,
                "accepted_display": package.R2_DISPLAY,
                "accepted_gpu": package.R2_GPU,
                "accepted_vista_world_port": package.R2_VISTA_WORLD_PORT,
                "accepted_width": package.R2_WIDTH,
                "accepted_height": package.R2_HEIGHT,
                "accepted_fps": package.R2_FPS,
            }
        )
        if exact_modes:
            self.package_receipt["archive"] = package.inspect_archive(
                self.attempt / "archive" / "Linux",
                trusted_engine_root=self.engine_root,
                exact_modes=True,
            )
            for name, path in (
                ("launcher", self.launcher),
                ("executable", self.executable),
                ("pak", self.pak),
            ):
                self.package_receipt["artifacts"][name]["mode"] = stat.S_IMODE(
                    path.stat().st_mode
                )
            self.package_receipt["trusted_upstream"].update(
                {
                    "mode_policy": "sealed-exact-stat-imode/v1",
                    "unreal_pak_mode": stat.S_IMODE(self.unreal_pak.stat().st_mode),
                }
            )
            self.package_receipt["project_policy"] = {
                "project_descriptor": str(self.project_descriptor),
                "project_descriptor_sha256": smoke.sha256_file(self.project_descriptor),
                "project_config": str(self.project_config),
                "project_config_sha256": smoke.sha256_file(self.project_config),
                "enabled_plugins": ["VistaPlayableHome"],
                "disabled_plugins": [
                    "AndroidFileServer",
                    "EditorScriptingUtilities",
                    "Interchange",
                    "PythonScriptPlugin",
                ],
                "host_module": "VistaPlayableHomeHost",
                "android_file_server_enabled": False,
                "mode_policy": "sealed-exact-stat-imode/v1",
                "project_descriptor_mode": stat.S_IMODE(
                    self.project_descriptor.stat().st_mode
                ),
                "project_config_mode": stat.S_IMODE(self.project_config.stat().st_mode),
            }
        self.receipt_path.write_bytes(smoke.canonical_json(self.package_receipt))
        self.receipt_path.chmod(0o600)

    def proc_snapshot(self, inode: int, process_groups: dict[int, int]) -> pathlib.Path:
        root = (
            pathlib.Path(self.temporary.name)
            / f"proc-{len(list(pathlib.Path(self.temporary.name).glob('proc-*')))}"
        )
        uid = os.geteuid()
        for pid, process_group in process_groups.items():
            process = root / str(pid)
            descriptors = process / "fd"
            descriptors.mkdir(parents=True)
            (process / "status").write_text(
                f"Name:\tfixture\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
                encoding="utf-8",
            )
            (process / "stat").write_text(
                f"{pid} (fixture) S 1 {process_group} {process_group} 0\n",
                encoding="utf-8",
            )
            (descriptors / "3").symlink_to(f"socket:[{inode}]")
        return root

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

        self.assertEqual(command[0], str(self.executable))
        self.assertEqual(command[1], smoke.PACKAGE_PROJECT_ARGUMENT)
        self.assertEqual(command[2], smoke.EXPECTED_MAP_PATH)
        self.assertNotIn(str(self.launcher), command)
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

    def test_realistic_r2_package_receipt_is_admitted_only_with_fixed_binding(
        self,
    ) -> None:
        self.enable_r2_receipt(exact_modes=False)
        inputs = self.inputs()
        self.assertEqual(inputs.receipt["schema"], smoke.R2_PACKAGE_RECEIPT_SCHEMA)
        self.assertFalse(inputs.exact_mode_attestation)

        self.package_receipt["bindings"]["accepted_gpu"] = 1
        self.receipt_path.write_bytes(smoke.canonical_json(self.package_receipt))
        with self.assertRaisesRegex(
            smoke.PackagedSmokeError, "PACKAGE_PROFILE_INVALID"
        ):
            self.inputs()

    def test_receipt_mode_is_deterministic_under_restrictive_umask(self) -> None:
        output = self.attempt / "smoke-receipt-umask.json"
        receipt = {"schema": "fixture/v1", "status": "accepted"}

        previous_umask = os.umask(0o777)
        try:
            receipt_sha = smoke._write_receipt(output, receipt)
        finally:
            os.umask(previous_umask)

        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(output.read_bytes(), smoke.canonical_json(receipt))
        self.assertEqual(receipt_sha, smoke.sha256_file(output))

    def test_receipt_mode_failure_removes_reserved_output(self) -> None:
        output = self.attempt / "smoke-receipt-fchmod-failure.json"
        receipt = {"schema": "fixture/v1", "status": "accepted"}

        with mock.patch.object(
            smoke.os, "fchmod", side_effect=OSError("fixture fchmod failure")
        ):
            with self.assertRaises(OSError):
                smoke._write_receipt(output, receipt)

        self.assertFalse(output.exists())

    def test_fdopen_close_then_raise_preserves_error_and_removes_output(self) -> None:
        output = self.attempt / "smoke-receipt-fdopen-failure.json"
        receipt = {"schema": "fixture/v1", "status": "accepted"}

        def close_then_raise(descriptor, *_args, **_kwargs):
            os.close(descriptor)
            raise RuntimeError("fixture fdopen failure")

        with mock.patch.object(smoke.os, "fdopen", side_effect=close_then_raise):
            with self.assertRaisesRegex(RuntimeError, "fixture fdopen failure"):
                smoke._write_receipt(output, receipt)

        self.assertFalse(output.exists())

    def test_exact_r2_v3_smoke_reverifies_non_named_and_project_modes(self) -> None:
        self.enable_r2_receipt(exact_modes=True)
        inputs = self.inputs()
        self.assertTrue(inputs.exact_mode_attestation)
        self.assertEqual(
            inputs.receipt_schema,
            smoke.R2_EXACT_MODE_PACKAGE_RECEIPT_SCHEMA,
        )
        self.assertEqual(
            inputs.archive_schema,
            package.ARCHIVE_SCHEMA_EXACT_MODE_V2,
        )
        smoke.verify_sealed_archive(inputs)

        for path, changed_mode in (
            (self.mode_0644, 0o600),
            (self.mode_0600, 0o640),
            (self.project_config, 0o600),
        ):
            original_mode = stat.S_IMODE(path.stat().st_mode)
            with self.subTest(path=path.name, changed_mode=oct(changed_mode)):
                path.chmod(changed_mode)
                with self.assertRaisesRegex(
                    smoke.PackagedSmokeError,
                    "PACKAGE_ARCHIVE_DRIFT",
                ):
                    smoke.verify_sealed_archive(inputs)
                path.chmod(original_mode)

        smoke.verify_sealed_archive(inputs)

    def test_exact_r2_v3_smoke_rejects_mode_exchange_during_runtime_phase(self) -> None:
        self.enable_r2_receipt(exact_modes=True)
        inputs = self.inputs("attempt-06")
        probe_calls = 0

        def ready(port: int, *, expected_revision: str, timeout: float):
            nonlocal probe_calls
            probe_calls += 1
            if probe_calls == 1:
                self.project_config.chmod(0o600)
            return {
                "command_id": "vwc-" + "d" * 24,
                "status": "success",
                "code": "READY",
                "world_revision": expected_revision,
                "session_generation": 0,
                "event_status": "inactive",
                "active_event": None,
            }

        receipt, _receipt_sha = smoke.run_smoke(
            inputs,
            probe=ready,
            listener_prover=lambda port, process_group: {
                "host": "127.0.0.1",
                "port": port,
                "process_group": process_group,
                "socket_inode": 321,
                "owner_pids": [process_group],
            },
        )

        self.assertEqual(probe_calls, 2)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error"]["code"], "PACKAGE_ARCHIVE_DRIFT")

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
        self.assertEqual(
            receipt["bindings"]["executable_sha256"],
            smoke.sha256_file(self.executable),
        )
        self.assertEqual(receipt["launch"]["target"], str(self.executable))
        self.assertEqual(receipt["launch"]["package_launcher"], str(self.launcher))
        self.assertFalse(receipt["launch"]["package_launcher_executed"])
        self.assertEqual(
            receipt["launch"]["target_policy"],
            "direct-sealed-executable-no-shell/v1",
        )
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

    def test_sealed_failing_package_launcher_is_not_executed(self) -> None:
        self.launcher.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        launcher_record = self.package_receipt["artifacts"]["launcher"]
        launcher_record["sha256"] = smoke.sha256_file(self.launcher)
        launcher_record["bytes"] = self.launcher.stat().st_size
        self.package_receipt["archive"] = package.inspect_archive(
            self.attempt / "archive" / "Linux",
            trusted_engine_root=self.engine_root,
        )
        self.receipt_path.write_bytes(smoke.canonical_json(self.package_receipt))
        self.receipt_path.chmod(0o600)
        inputs = self.inputs("attempt-07")

        def ready(_port: int, *, expected_revision: str, timeout: float):
            self.assertEqual(timeout, 1.0)
            return {
                "command_id": "vwc-" + "e" * 24,
                "status": "success",
                "code": "READY",
                "world_revision": expected_revision,
                "session_generation": 0,
                "event_status": "inactive",
                "active_event": None,
            }

        receipt, _receipt_sha = smoke.run_smoke(
            inputs,
            probe=ready,
            listener_prover=lambda port, process_group: {
                "host": "127.0.0.1",
                "port": port,
                "process_group": process_group,
                "socket_inode": 456,
                "owner_pids": [process_group],
            },
        )

        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["launch"]["target"], str(self.executable))
        self.assertFalse(receipt["launch"]["package_launcher_executed"])
        self.assertNotEqual(receipt["termination"]["exit_code"], 99)

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
                smoke, "process_effective_uid", return_value=os.geteuid()
            ),
            mock.patch.object(smoke, "_global_socket_owners", return_value={111: []}),
            self.assertRaisesRegex(
                smoke.PackagedSmokeError, "LISTENER_OWNERSHIP_INVALID"
            ),
        ):
            smoke.prove_loopback_listener_ownership(55777, 424242)

    def test_same_group_child_listener_is_rejected_by_exact_process_proof(self) -> None:
        managed_pid = 424242
        child_pid = 424243
        with (
            mock.patch.object(smoke, "_listening_loopback_inodes", return_value={111}),
            mock.patch.object(
                smoke, "process_effective_uid", return_value=os.geteuid()
            ),
            mock.patch.object(
                smoke,
                "_global_socket_owners",
                return_value={
                    111: [{"pid": child_pid, "process_group": managed_pid}]
                },
            ),
            self.assertRaisesRegex(
                smoke.PackagedSmokeError, "LISTENER_OWNERSHIP_INVALID"
            ),
        ):
            smoke.prove_loopback_listener_ownership(55777, managed_pid)

    def test_unreadable_managed_descriptor_table_fails_closed(self) -> None:
        proc_root = pathlib.Path(self.temporary.name) / "fake-proc"
        process = proc_root / "424242"
        process.mkdir(parents=True)
        uid = os.geteuid()
        (process / "status").write_text(
            f"Name:\tfixture\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
            encoding="utf-8",
        )
        (process / "stat").write_text(
            "424242 (fixture) S 1 424242 424242 0\n",
            encoding="utf-8",
        )
        # A regular file in place of fd deterministically exercises the same
        # fail-closed path as an unreadable same-UID /proc/<pid>/fd directory.
        (process / "fd").write_text("unreadable fixture\n", encoding="utf-8")
        with (
            mock.patch.object(smoke, "PROC_ROOT", proc_root),
            mock.patch.object(smoke, "_listening_loopback_inodes", return_value={111}),
            self.assertRaisesRegex(
                smoke.PackagedSmokeError,
                "LISTENER_VISIBILITY_INCOMPLETE",
            ),
        ):
            smoke.prove_loopback_listener_ownership(55777, 424242)

    def test_unreadable_unrelated_same_uid_descriptor_table_is_scoped_out(
        self,
    ) -> None:
        inode = 111
        managed_pid = 424242
        unrelated_pid = 424243
        proc_root = self.proc_snapshot(inode, {managed_pid: managed_pid})
        uid = os.geteuid()
        unrelated = proc_root / str(unrelated_pid)
        unrelated.mkdir()
        (unrelated / "status").write_text(
            f"Name:\tfixture\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
            encoding="utf-8",
        )
        (unrelated / "stat").write_text(
            f"{unrelated_pid} (fixture) S 1 {unrelated_pid} {unrelated_pid} 0\n",
            encoding="utf-8",
        )
        # A regular file deterministically models a non-dumpable unrelated
        # process whose /proc/<pid>/fd cannot be enumerated.
        (unrelated / "fd").write_text("unreadable fixture\n", encoding="utf-8")
        with (
            mock.patch.object(smoke, "PROC_ROOT", proc_root),
            mock.patch.object(
                smoke, "_listening_loopback_inodes", return_value={inode}
            ),
        ):
            proof = smoke.prove_loopback_listener_ownership(55777, managed_pid)

        self.assertEqual(proof["process_group"], managed_pid)
        self.assertEqual(proof["owner_pids"], [managed_pid])

    def test_listener_inode_change_during_proof_fails_closed(self) -> None:
        with (
            mock.patch.object(
                smoke,
                "_listening_loopback_inodes",
                side_effect=[{111}, {222}],
            ),
            mock.patch.object(
                smoke, "process_effective_uid", return_value=os.geteuid()
            ),
            mock.patch.object(
                smoke,
                "_global_socket_owners",
                return_value={111: [{"pid": 424242, "process_group": 424242}]},
            ),
            self.assertRaisesRegex(
                smoke.PackagedSmokeError,
                "listener identity changed",
            ),
        ):
            smoke.prove_loopback_listener_ownership(55777, 424242)

    def test_real_loopback_listener_is_attributed_to_its_process_group(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            inode = os.fstat(listener.fileno()).st_ino
            proc_root = self.proc_snapshot(inode, {os.getpid(): os.getpid()})
            with (
                mock.patch.object(smoke, "PROC_ROOT", proc_root),
                mock.patch.object(
                    smoke, "_listening_loopback_inodes", return_value={inode}
                ),
            ):
                proof = smoke.prove_loopback_listener_ownership(
                    listener.getsockname()[1], os.getpid()
                )
        self.assertEqual(proof["process_group"], os.getpid())
        self.assertIn(os.getpid(), proof["owner_pids"])

    def test_inherited_listener_fd_outside_sealed_group_is_rejected(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os,sys,time;"
                        "os.fstat(int(sys.argv[1]));"
                        "print('ready', flush=True);"
                        "time.sleep(30)"
                    ),
                    str(listener.fileno()),
                ],
                pass_fds=(listener.fileno(),),
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertIsNotNone(child.stdout)
                self.assertEqual(child.stdout.readline().strip(), "ready")
                inode = os.fstat(listener.fileno()).st_ino
                proc_root = self.proc_snapshot(
                    inode,
                    {os.getpid(): os.getpid(), child.pid: child.pid},
                )
                with (
                    mock.patch.object(smoke, "PROC_ROOT", proc_root),
                    mock.patch.object(
                        smoke, "_listening_loopback_inodes", return_value={inode}
                    ),
                    self.assertRaisesRegex(
                        smoke.PackagedSmokeError,
                        "LISTENER_OWNERSHIP_INVALID",
                    ),
                ):
                    smoke.prove_loopback_listener_ownership(
                        listener.getsockname()[1], os.getpid()
                    )
            finally:
                child.terminate()
                child.communicate(timeout=5)

    @unittest.skipUnless(
        hasattr(socket, "SCM_RIGHTS"),
        "SCM_RIGHTS is required for descriptor-handoff proof",
    )
    def test_scm_rights_listener_handoff_outside_group_is_rejected(self) -> None:
        sender, receiver = socket.socketpair()
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
            sender,
            receiver,
        ):
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import array,socket,sys,time;"
                        "channel=socket.socket(fileno=int(sys.argv[1]));"
                        "_,ancillary,_,_=channel.recvmsg(1,socket.CMSG_SPACE(4));"
                        "fds=array.array('i');"
                        "fds.frombytes(ancillary[0][2][:fds.itemsize]);"
                        "held=socket.socket(fileno=fds[0]);"
                        "print('ready', flush=True);"
                        "time.sleep(30)"
                    ),
                    str(receiver.fileno()),
                ],
                pass_fds=(receiver.fileno(),),
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            receiver.close()
            rights = array.array("i", [listener.fileno()])
            sender.sendmsg(
                [b"x"],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes())],
            )
            try:
                self.assertIsNotNone(child.stdout)
                self.assertEqual(child.stdout.readline().strip(), "ready")
                inode = os.fstat(listener.fileno()).st_ino
                proc_root = self.proc_snapshot(
                    inode,
                    {os.getpid(): os.getpid(), child.pid: child.pid},
                )
                with (
                    mock.patch.object(smoke, "PROC_ROOT", proc_root),
                    mock.patch.object(
                        smoke, "_listening_loopback_inodes", return_value={inode}
                    ),
                    self.assertRaisesRegex(
                        smoke.PackagedSmokeError,
                        "LISTENER_OWNERSHIP_INVALID",
                    ),
                ):
                    smoke.prove_loopback_listener_ownership(
                        listener.getsockname()[1], os.getpid()
                    )
            finally:
                child.terminate()
                child.communicate(timeout=5)

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
        self.package_receipt["artifacts"]["launcher"]["sha256"] = smoke.sha256_file(
            self.launcher
        )
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
        self.assertEqual(smoke.plan(inputs)["target"], str(self.executable))
        self.assertFalse(smoke.plan(inputs)["package_launcher_executed"])


if __name__ == "__main__":
    unittest.main()
