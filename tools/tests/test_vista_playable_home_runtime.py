from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.runtime.vista_playable_home import launch, preflight, profile_entrypoint, stop, sunshine_app
from tools.runtime.vista_playable_home.runtime import (
    GameRuntimeConfig,
    R2_CAMERA_PROFILE,
    R2_DISPLAY,
    R2_FPS,
    R2_GPU,
    R2_HEIGHT,
    R2_RUNTIME_PROFILE,
    R2_SCHEMA,
    R2_VISTA_WORLD_PORT,
    R2_WIDTH,
    RuntimeSafetyError,
    allocate_runtime_attempt,
    atomic_write_json,
    build_game_command,
    inspect_toolchain,
    process_identity,
    probe_typed_runtime,
    publish_current_runtime,
    redacted_plan,
    resolve_current_runtime_state,
    sanitized_environment,
    validate_config,
    validate_display,
    validate_gpu,
    validate_map,
    validate_typed_readiness_response,
    validate_vista_world_port,
)


class VistaPlayableHomeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_config(self) -> GameRuntimeConfig:
        workspace = self.root / "run"
        project = workspace / "project" / "Home.uproject"
        project.parent.mkdir(parents=True)
        project.write_text(json.dumps({"Plugins": []}) + "\n", encoding="utf-8")
        editor = self.root / "UE" / "Engine" / "Binaries" / "Linux" / "UnrealEditor"
        editor.parent.mkdir(parents=True)
        editor.write_text("#!/bin/sh\n", encoding="utf-8")
        editor.chmod(0o755)
        return GameRuntimeConfig(
            workspace=workspace,
            project=project,
            ue_editor=editor,
            map_path="/Game/VISTA/PlayableHome/r1/Maps/VistaPlayableHome",
        )

    def test_game_command_is_visible_game_mode_without_editor_or_offscreen(self) -> None:
        with mock.patch(
            "tools.runtime.vista_playable_home.runtime.port_is_available",
            return_value=True,
        ):
            config = validate_config(self.make_config(), create_workspace=False)
        command = build_game_command(config)
        self.assertIn("-game", command)
        self.assertIn("-Windowed", command)
        self.assertFalse(any("RenderOffScreen" in item for item in command))
        self.assertFalse(any("PixelStreaming" in item for item in command))
        self.assertIn("-ddc=InstalledNoZenLocalFallback", command)
        self.assertIn("-VistaWorldPort=55620", command)
        self.assertEqual(command[0], str(config.ue_editor))

    def test_cold_nas_launch_has_bounded_turnkey_headroom(self) -> None:
        self.assertGreaterEqual(launch.DEFAULT_READY_TIMEOUT_S, 300.0)
        self.assertLessEqual(launch.DEFAULT_READY_TIMEOUT_S, 600.0)

    def test_reserved_gpu_one_is_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeSafetyError, "reserved"):
            validate_gpu(1)
        self.assertEqual(validate_gpu(0), 0)

    def test_existing_runtime_ports_are_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeSafetyError, "reserved"):
            validate_vista_world_port(55570)
        with (
            mock.patch(
                "tools.runtime.vista_playable_home.runtime.port_is_available",
                return_value=False,
            ),
            self.assertRaisesRegex(RuntimeSafetyError, "already in use"),
        ):
            validate_vista_world_port(55620)

    def test_map_display_and_paths_fail_closed(self) -> None:
        self.assertEqual(validate_display(":117"), ":117")
        for value in ("117", ":-1", "localhost:0", ":5000"):
            with self.subTest(value=value), self.assertRaises(RuntimeSafetyError):
                validate_display(value)
        with self.assertRaises(RuntimeSafetyError):
            validate_map("/Game/../Secret")
        outside = self.root / "outside.uproject"
        outside.write_text("{}\n", encoding="utf-8")
        config = self.make_config()
        with self.assertRaisesRegex(RuntimeSafetyError, "contained"):
            validate_config(GameRuntimeConfig(**{**config.__dict__, "project": outside}), create_workspace=False)
        lexical_link = self.root / "linked-run"
        lexical_link.symlink_to(config.workspace, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeSafetyError, "symlink"):
            validate_config(GameRuntimeConfig(**{**config.__dict__, "workspace": lexical_link}), create_workspace=False)

    def test_plan_contains_no_arbitrary_command_or_secret(self) -> None:
        with mock.patch(
            "tools.runtime.vista_playable_home.runtime.port_is_available",
            return_value=True,
        ):
            config = validate_config(self.make_config(), create_workspace=False)
        rendered = json.dumps(redacted_plan(config))
        self.assertIn("unreal-editor-game-preview", rendered)
        self.assertNotIn("ANTHROPIC", rendered)
        self.assertNotIn("OPENAI", rendered)
        environment = sanitized_environment(config)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)

    def test_realistic_r2_runtime_is_closed_and_attempt_local(self) -> None:
        base = self.make_config()
        r2 = GameRuntimeConfig(
            **{
                **base.__dict__,
                "runtime_profile": R2_RUNTIME_PROFILE,
                "display": R2_DISPLAY,
                "gpu": R2_GPU,
                "vista_world_port": R2_VISTA_WORLD_PORT,
                "width": R2_WIDTH,
                "height": R2_HEIGHT,
                "fps": R2_FPS,
            }
        )
        with mock.patch(
            "tools.runtime.vista_playable_home.runtime.port_is_available",
            return_value=True,
        ):
            config = validate_config(r2, create_workspace=False)

        command = build_game_command(config)
        self.assertIn(f"-VistaCameraProfile={R2_CAMERA_PROFILE}", command)
        self.assertIn(f"-VistaWorldPort={R2_VISTA_WORLD_PORT}", command)
        self.assertIn(f"-ResX={R2_WIDTH}", command)
        self.assertIn(f"-ResY={R2_HEIGHT}", command)
        self.assertIn(f"-graphicsadapter={R2_GPU}", command)
        self.assertIn(
            f"-UserDir={config.workspace / 'runtime-user' / 'ue-user'}",
            command,
        )

        environment = sanitized_environment(config)
        user_root = config.workspace / "runtime-user"
        self.assertEqual(environment["DISPLAY"], R2_DISPLAY)
        self.assertEqual(environment["HOME"], str(user_root / "home"))
        self.assertEqual(environment["TMPDIR"], str(user_root / "tmp"))
        self.assertEqual(environment["XDG_DATA_HOME"], str(user_root / "xdg-data"))
        self.assertEqual(environment["VISTA_RUNTIME_PROFILE"], R2_RUNTIME_PROFILE)
        self.assertEqual(environment["VISTA_CAMERA_PROFILE"], R2_CAMERA_PROFILE)

        plan = redacted_plan(config)
        self.assertEqual(plan["schema"], R2_SCHEMA)
        self.assertEqual(plan["mode"], "unreal-editor-game-preview-realistic")
        self.assertEqual(plan["config"]["runtime_profile"], R2_RUNTIME_PROFILE)
        self.assertEqual(plan["config"]["camera_profile"], R2_CAMERA_PROFILE)
        self.assertTrue(plan["security"]["runtime_profile_closed"])

    def test_realistic_r2_runtime_rejects_any_fixed_tuple_drift(self) -> None:
        base = self.make_config()
        values = {
            "runtime_profile": R2_RUNTIME_PROFILE,
            "display": R2_DISPLAY,
            "gpu": R2_GPU,
            "vista_world_port": R2_VISTA_WORLD_PORT,
            "width": R2_WIDTH,
            "height": R2_HEIGHT,
            "fps": R2_FPS,
        }
        for field, wrong in (
            ("display", ":120"),
            ("gpu", 2),
            ("vista_world_port", 55631),
            ("width", 1280),
            ("height", 720),
            ("fps", 59),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                RuntimeSafetyError, "1920x1080"
            ):
                build_game_command(
                    GameRuntimeConfig(
                        **{
                            **base.__dict__,
                            **values,
                            field: wrong,
                        }
                    )
                )

    def test_launch_cli_selects_r2_defaults_without_changing_legacy_defaults(self) -> None:
        base = self.make_config()
        required = [
            "--workspace",
            str(base.workspace),
            "--project",
            str(base.project),
            "--ue-editor",
            str(base.ue_editor),
            "--map",
            base.map_path,
        ]
        legacy = launch.config_from_args(launch.parser().parse_args(required))
        self.assertEqual(
            (
                legacy.runtime_profile,
                legacy.display,
                legacy.gpu,
                legacy.vista_world_port,
                legacy.width,
                legacy.height,
                legacy.fps,
            ),
            (None, ":117", 0, 55620, 1280, 720, 60),
        )
        r2 = launch.config_from_args(
            launch.parser().parse_args(
                [*required, "--runtime-profile", R2_RUNTIME_PROFILE]
            )
        )
        self.assertEqual(
            (
                r2.runtime_profile,
                r2.display,
                r2.gpu,
                r2.vista_world_port,
                r2.width,
                r2.height,
                r2.fps,
            ),
            (
                R2_RUNTIME_PROFILE,
                R2_DISPLAY,
                R2_GPU,
                R2_VISTA_WORLD_PORT,
                R2_WIDTH,
                R2_HEIGHT,
                R2_FPS,
            ),
        )

    def test_legacy_runtime_plan_shape_does_not_gain_r2_fields(self) -> None:
        plan = redacted_plan(self.make_config())
        self.assertNotIn("runtime_profile", plan["config"])
        self.assertNotIn("camera_profile", plan["config"])
        self.assertNotIn("runtime_profile_closed", plan["security"])
        self.assertFalse(
            any("VistaCameraProfile" in item for item in plan["command"])
        )

    def test_toolchain_report_is_honest(self) -> None:
        config = self.make_config()
        report = inspect_toolchain(config.ue_editor)
        self.assertFalse(report["cook_ready"])
        self.assertIn("run_uat", report["present"])

    def test_toolchain_accepts_source_built_uht_layout(self) -> None:
        config = self.make_config()
        engine_root = config.ue_editor.parents[3]
        for relative in (
            "Engine/Build/BatchFiles/RunUAT.sh",
            "Engine/Build/BatchFiles/Linux/Build.sh",
            "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool",
        ):
            target = engine_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#!/bin/sh\n", encoding="utf-8")
        (engine_root / "Engine/Source/Programs/UnrealHeaderTool").mkdir(parents=True)
        report = inspect_toolchain(config.ue_editor)
        self.assertTrue(report["cook_ready"])
        self.assertTrue(report["present"]["unreal_header_tool"])
        self.assertTrue(report["paths"]["unreal_header_tool"].endswith("Source/Programs/UnrealHeaderTool"))

    def test_atomic_state_is_private(self) -> None:
        target = self.root / "state.json"
        atomic_write_json(target, {"ok": True})
        self.assertEqual(json.loads(target.read_text()), {"ok": True})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_runtime_attempts_are_repeatable_and_current_pointer_is_contained(self) -> None:
        config = self.make_config()
        first = allocate_runtime_attempt(config.workspace)
        first_state = first / "runtime-state.json"
        atomic_write_json(first_state, {
            "status": "stopped",
            "process": {"pid": 2147483647, "start_ticks": 1},
        })
        pointer = publish_current_runtime(config.workspace, first_state)
        self.assertEqual(stat.S_IMODE(pointer.stat().st_mode), 0o600)
        resolved, state = resolve_current_runtime_state(config.workspace)
        self.assertEqual(resolved, first_state)
        self.assertEqual(state["status"], "stopped")
        second = allocate_runtime_attempt(config.workspace)
        self.assertNotEqual(first, second)
        self.assertTrue(first_state.is_file())

    def test_runtime_attempt_refuses_a_live_current_identity(self) -> None:
        config = self.make_config()
        attempt = allocate_runtime_attempt(config.workspace)
        state_path = attempt / "runtime-state.json"
        atomic_write_json(state_path, {
            "status": "running",
            "process": process_identity(os.getpid(), "test-runtime"),
        })
        publish_current_runtime(config.workspace, state_path)
        with self.assertRaisesRegex(RuntimeSafetyError, "already live"):
            allocate_runtime_attempt(config.workspace)

    def test_stop_signals_supervisor_pid_without_its_process_group(self) -> None:
        identity = {"pid": 4321, "start_ticks": 99, "process_group": 7777}
        with (
            mock.patch.object(stop, "identity_is_live", return_value=True),
            mock.patch.object(stop, "process_start_ticks", return_value=99),
            mock.patch.object(stop.os, "kill") as kill,
            mock.patch.object(stop.os, "killpg") as kill_group,
        ):
            self.assertTrue(stop.signal_owned_process(identity, 15))
        kill.assert_called_once_with(4321, 15)
        kill_group.assert_not_called()

    def test_typed_readiness_probe_proves_revision_and_zero_generation(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        captured: dict[str, object] = {}

        def serve() -> None:
            connection, _address = listener.accept()
            with connection:
                request = b""
                while not request.endswith(b"\n"):
                    request += connection.recv(4096)
                payload = json.loads(request)
                captured.update(payload)
                command_id = payload["params"]["command_id"]
                connection.sendall(json.dumps({
                    "command_id": command_id,
                    "status": "success",
                    "code": "READY",
                    "world_revision": "vista_playable_home_r1",
                    "session_generation": 0,
                    "event_status": "idle",
                    "active_event": None,
                }).encode("utf-8"))
            listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        response = probe_typed_runtime(listener.getsockname()[1])
        thread.join(timeout=2)
        self.assertEqual(response["code"], "READY")
        self.assertEqual(captured["type"], "vista_world_action")
        self.assertEqual(set(captured["params"]), {"operation", "command_id"})
        self.assertEqual(captured["params"]["operation"], "status")

    def test_typed_readiness_rejects_wrong_revision_or_generation(self) -> None:
        base = {
            "command_id": "vwc-" + "a" * 24,
            "status": "success",
            "code": "READY",
            "world_revision": "wrong",
            "session_generation": 1,
            "event_status": "idle",
            "active_event": None,
        }
        with self.assertRaisesRegex(RuntimeSafetyError, "identity"):
            validate_typed_readiness_response(
                base,
                command_id=base["command_id"],
            )

    def test_sunshine_entry_replaces_only_named_app(self) -> None:
        payload = {"env": {"PATH": "x"}, "apps": [{"name": "Desktop"}, {"name": "VISTA World", "cmd": "old"}]}
        entry = sunshine_app.build_entry(
            python=Path("/usr/bin/python3"),
            launcher=Path("/repo/profile_entrypoint.py"),
            profile=Path("/run/profile.json"),
            working_dir=Path("/repo"),
        )
        merged = sunshine_app.merge_entry(payload, entry)
        self.assertEqual([app["name"] for app in merged["apps"]], ["Desktop", "VISTA World"])
        self.assertEqual(merged["env"], payload["env"])
        self.assertIn("--profile", merged["apps"][-1]["cmd"])

    def test_sunshine_install_backs_up_and_writes_valid_json(self) -> None:
        apps = self.root / "apps.json"
        apps.write_text('{"apps": []}\n', encoding="utf-8")
        backup = sunshine_app.install(apps, {"apps": [{"name": "VISTA World"}]})
        self.assertTrue(backup.is_file())
        self.assertEqual(json.loads(apps.read_text())["apps"][0]["name"], "VISTA World")

    def test_profile_rejects_unknown_fields_and_maps_closed_fields(self) -> None:
        profile = self.root / "profile.json"
        profile.write_text(
            json.dumps({
                "workspace": "/run/home",
                "project": "/run/home/Home.uproject",
                "ue_editor": "/ue/Engine/Binaries/Linux/UnrealEditor",
                "map": "/Game/VISTA/Home",
                "gpu": 0,
            }),
            encoding="utf-8",
        )
        profile.chmod(0o600)
        arguments = profile_entrypoint.load_profile(profile)
        self.assertIn("--workspace", arguments)
        self.assertIn("--gpu", arguments)
        profile.write_text(json.dumps({"workspace": "/x", "shell": "rm -rf /"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unknown"):
            profile_entrypoint.load_profile(profile)

    def test_preflight_classifies_view_only_without_input_devices(self) -> None:
        editor = self.make_config().ue_editor
        with (
            mock.patch.object(preflight, "device_access", side_effect=lambda path: {"path": str(path), "exists": True, "readable": False, "writable": False, "ready": False}),
            mock.patch.object(preflight, "display_access", return_value={"connectable": True}),
            mock.patch.object(preflight, "sunshine_inspection", return_value={"binary": "/bin/sunshine"}),
            mock.patch.object(preflight, "listener", return_value=True),
            mock.patch.object(preflight, "tailscale_inspection", return_value={"backend_state": "Running"}),
            mock.patch.object(preflight, "nvidia_inspection", return_value={"gpus": [{"index": 0, "reserved": False}]}),
        ):
            report = preflight.build_report(
                ue_editor=editor,
                display=":117",
                sunshine_config=self.root,
                sunshine_host="127.0.0.1",
                sunshine_port=47989,
            )
        self.assertTrue(report["preview_ready"])
        self.assertFalse(report["moonlight_control_ready"])
        self.assertIn("moonlight_input_view_only", report["blockers"])


if __name__ == "__main__":
    unittest.main()
