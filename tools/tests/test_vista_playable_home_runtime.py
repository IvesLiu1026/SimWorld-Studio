from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.runtime.vista_playable_home import preflight, profile_entrypoint, sunshine_app
from tools.runtime.vista_playable_home.runtime import (
    GameRuntimeConfig,
    RuntimeSafetyError,
    atomic_write_json,
    build_game_command,
    inspect_toolchain,
    process_identity,
    redacted_plan,
    sanitized_environment,
    validate_config,
    validate_display,
    validate_gpu,
    validate_map,
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
        config = validate_config(self.make_config(), create_workspace=False)
        command = build_game_command(config)
        self.assertIn("-game", command)
        self.assertIn("-Windowed", command)
        self.assertFalse(any("RenderOffScreen" in item for item in command))
        self.assertFalse(any("PixelStreaming" in item for item in command))
        self.assertEqual(command[0], str(config.ue_editor))

    def test_reserved_gpu_one_is_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeSafetyError, "reserved"):
            validate_gpu(1)
        self.assertEqual(validate_gpu(0), 0)

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

    def test_plan_contains_no_arbitrary_command_or_secret(self) -> None:
        config = validate_config(self.make_config(), create_workspace=False)
        rendered = json.dumps(redacted_plan(config))
        self.assertIn("unreal-editor-game-preview", rendered)
        self.assertNotIn("ANTHROPIC", rendered)
        self.assertNotIn("OPENAI", rendered)
        environment = sanitized_environment(config)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)

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
