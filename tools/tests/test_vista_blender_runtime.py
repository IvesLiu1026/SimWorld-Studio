from __future__ import annotations

import copy
import hashlib
import io
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.runtime.vista_blender_world import (
    launch,
    live_qa,
    patch_legacy_player,
    verify_browser_input,
)
from tools.runtime.vista_blender_world import runtime as runtime_module
from tools.runtime.vista_blender_world.runtime import (
    DEFAULT_CIRRUS_HTTP_PORT,
    DEFAULT_CIRRUS_SFU_PORT,
    DEFAULT_CIRRUS_STREAMER_PORT,
    DEFAULT_STUDIO_PORT,
    DEFAULT_UE_MCP_PORT,
    LOOPBACK_HOST,
    Ports,
    RuntimePaths,
    RuntimeSafetyError,
    assert_owned_loopback_listener,
    build_cirrus_config,
    build_ue_command,
    ensure_access_token,
    host_is_loopback,
    listener_pids_for_port,
    redacted_runtime_plan,
    safe_public_state,
    sanitized_child_environment,
    validate_map,
    validate_model_policy,
    validate_paths,
    validate_render_settings,
    wait_for_loopback_listener,
)


class VistaBlenderRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_layout(self, project_name: str = "fresh-project") -> RuntimePaths:
        workspace = self.root / "run"
        project_dir = workspace / "ue" / project_name
        project_dir.mkdir(parents=True)
        project = project_dir / "gym_citynav.uproject"
        project.write_text(
            json.dumps(
                {
                    "Plugins": [
                        {"Name": "UnrealMCP", "Enabled": True},
                        {"Name": "PixelStreaming", "Enabled": True},
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        ue_editor = (
            self.root / "runtime" / "Engine" / "Binaries" / "Linux" / "UnrealEditor"
        )
        ue_editor.parent.mkdir(parents=True, exist_ok=True)
        ue_editor.write_text("#!/bin/sh\n", encoding="utf-8")
        ue_editor.chmod(0o755)
        cirrus_dir = self.root / "runtime" / "cirrus"
        cirrus_dir.mkdir(parents=True, exist_ok=True)
        (cirrus_dir / "cirrus.js").write_text(
            "// VISTA_LOOPBACK_PATCH_V1: reviewed\n", encoding="utf-8"
        )
        cirrus_sha = hashlib.sha256((cirrus_dir / "cirrus.js").read_bytes()).hexdigest()
        runtime_module.EXPECTED_CIRRUS_SHA256 = cirrus_sha
        (cirrus_dir / "cirrus.js.vista-receipt.json").write_text(
            json.dumps(
                {
                    "schema": "vista-cirrus-loopback-patch/v1",
                    "patch_version": "VISTA_LOOPBACK_PATCH_V1",
                    "patched_sha256": cirrus_sha,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        studio = workspace / "studio-workspace"
        (studio / "web" / "server").mkdir(parents=True, exist_ok=True)
        (studio / "web" / "server" / "index.js").write_text("\n", encoding="utf-8")
        player_payload = b"reviewed fixed-viewport VISTA player fixture\n"
        runtime_module.EXPECTED_STUDIO_PLAYER_SHA256 = hashlib.sha256(
            player_payload
        ).hexdigest()
        for player_dir in ("public", "dist"):
            target = studio / "web" / player_dir / "ue-player.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(player_payload)
        node = self.root / "bin" / "node"
        node.parent.mkdir(parents=True, exist_ok=True)
        node.write_text("#!/bin/sh\n", encoding="utf-8")
        node.chmod(0o755)
        return RuntimePaths(
            workspace=workspace,
            project=project,
            ue_editor=ue_editor,
            cirrus_dir=cirrus_dir,
            studio_workspace=studio,
            node_bin=node,
            token_file=workspace / "access-token",
        )

    def test_default_ports_are_dedicated_and_distinct(self) -> None:
        ports = Ports().validate()
        self.assertEqual(
            ports.values(),
            (
                DEFAULT_STUDIO_PORT,
                DEFAULT_UE_MCP_PORT,
                DEFAULT_CIRRUS_HTTP_PORT,
                DEFAULT_CIRRUS_STREAMER_PORT,
                DEFAULT_CIRRUS_SFU_PORT,
            ),
        )
        self.assertEqual(len(set(ports.values())), 5)

    def test_cirrus_config_is_loopback_only(self) -> None:
        config = build_cirrus_config(Ports())
        self.assertEqual(config["BindAddress"], LOOPBACK_HOST)
        self.assertFalse(config["UseMatchmaker"])
        self.assertNotIn("PublicIp", config)

    def test_project_must_be_fresh_and_contained(self) -> None:
        paths = self.make_layout()
        validated = validate_paths(paths)
        self.assertTrue(validated.project.is_relative_to(validated.workspace))
        outside = self.root / "outside.uproject"
        outside.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeSafetyError, "contained by workspace"):
            validate_paths(RuntimePaths(**{**paths.__dict__, "project": outside}))

    def test_studio_workspace_must_be_a_disposable_contained_copy(self) -> None:
        paths = self.make_layout()
        outside = self.root / "source-studio"
        (outside / "web" / "server").mkdir(parents=True)
        (outside / "web" / "server" / "index.js").write_text("\n", encoding="utf-8")
        with self.assertRaisesRegex(
            RuntimeSafetyError, "Studio workspace must be contained"
        ):
            validate_paths(
                RuntimePaths(**{**paths.__dict__, "studio_workspace": outside})
            )

    def test_r7_r8_archive_and_canonical_projects_are_refused(self) -> None:
        for marker in ("disposable-project-r7", "project-r8", "archive", "canonical"):
            with self.subTest(marker=marker):
                other_root = self.root / marker.replace("/", "_")
                other_root.mkdir(exist_ok=True)
                paths = self.make_layout(marker)
                with self.assertRaisesRegex(RuntimeSafetyError, "forbidden component"):
                    validate_paths(paths)

    def test_symlink_cannot_hide_forbidden_project(self) -> None:
        paths = self.make_layout()
        forbidden = paths.workspace / "ue" / "canonical"
        forbidden.mkdir(parents=True)
        target = forbidden / "world.uproject"
        target.write_text("{}\n", encoding="utf-8")
        link = paths.workspace / "ue" / "fresh-link.uproject"
        link.symlink_to(target)
        with self.assertRaisesRegex(RuntimeSafetyError, "forbidden component"):
            validate_paths(RuntimePaths(**{**paths.__dict__, "project": link}))

    def test_unreviewed_cirrus_is_refused(self) -> None:
        paths = self.make_layout()
        (paths.cirrus_dir / "cirrus.js").write_text(
            "// upstream only\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeSafetyError, "not the reviewed loopback"):
            validate_paths(paths)

    def test_unreviewed_or_divergent_studio_player_is_refused(self) -> None:
        paths = self.make_layout()
        player = paths.studio_workspace / "web/dist/ue-player.html"
        player.write_text("wrong player\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeSafetyError, "fixed-viewport"):
            validate_paths(paths)

    def test_legacy_player_patch_is_exact_dry_run_apply_and_idempotent(self) -> None:
        studio = self.root / "studio"
        fixture = b"prefix\n" + patch_legacy_player.OLD_BLOCK + b"suffix\n"
        patched = b"prefix\n" + patch_legacy_player.NEW_BLOCK + b"suffix\n"
        with (
            mock.patch.object(
                patch_legacy_player,
                "INPUT_SHA256",
                hashlib.sha256(fixture).hexdigest(),
            ),
            mock.patch.object(
                patch_legacy_player,
                "OUTPUT_SHA256",
                hashlib.sha256(patched).hexdigest(),
            ),
        ):
            for relative in patch_legacy_player.PLAYER_PATHS:
                target = studio / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(fixture)
            dry_run = patch_legacy_player.patch_studio_player(studio, apply=False)
            self.assertEqual(dry_run["status"], "ready_to_patch")
            self.assertFalse(dry_run["applied"])
            self.assertEqual(
                (studio / patch_legacy_player.PLAYER_PATHS[0]).read_bytes(),
                fixture,
            )
            applied = patch_legacy_player.patch_studio_player(studio, apply=True)
            self.assertEqual(applied["status"], "patched")
            self.assertTrue(applied["applied"])
            for relative in patch_legacy_player.PLAYER_PATHS:
                self.assertEqual((studio / relative).read_bytes(), patched)
            repeated = patch_legacy_player.patch_studio_player(studio, apply=True)
            self.assertEqual(repeated["status"], "already_patched")
            self.assertFalse(repeated["applied"])

    def test_project_with_disabled_runtime_plugins_is_refused(self) -> None:
        paths = self.make_layout()
        paths.project.write_text(
            json.dumps(
                {
                    "Plugins": [
                        {"Name": "UnrealMCP", "Enabled": False},
                        {"Name": "PixelStreaming", "Enabled": True},
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeSafetyError, "explicitly enable.*UnrealMCP"):
            validate_paths(paths)

    def test_token_is_created_0600_and_insecure_existing_mode_fails(self) -> None:
        token_path = self.root / "token"
        token = ensure_access_token(token_path)
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        token_path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeSafetyError, "exactly 0600"):
            ensure_access_token(token_path)

    def test_map_contract_rejects_filesystem_or_parent_paths(self) -> None:
        self.assertEqual(
            validate_map("/Game/VISTA/Scenes/MMG040_Office_BlenderR1"),
            "/Game/VISTA/Scenes/MMG040_Office_BlenderR1",
        )
        for value in (
            "Content/Map",
            "/tmp/a.umap",
            "/Game/VISTA/../Secret",
            "/Game/Bad Map",
        ):
            with self.subTest(value=value), self.assertRaises(RuntimeSafetyError):
                validate_map(value)

    def test_live_mode_requires_exact_claude_binary(self) -> None:
        self.assertEqual(validate_model_policy("off", False, None), "off")
        with self.assertRaisesRegex(RuntimeSafetyError, "requires an exact Claude"):
            validate_model_policy("live", True, None)
        with self.assertRaisesRegex(RuntimeSafetyError, "only with model mode live"):
            validate_model_policy("off", True, None)

    def test_render_contract_is_validated_before_launch(self) -> None:
        self.assertEqual(validate_render_settings(1280, 720, 60), (1280, 720, 60))
        with self.assertRaises(RuntimeSafetyError):
            validate_render_settings(1280, 720, 24)
        with self.assertRaises(RuntimeSafetyError):
            validate_render_settings(100, 100, 60)

    def test_ue_ready_timeout_is_bounded_for_cold_shader_cache(self) -> None:
        self.assertEqual(launch.validate_ue_ready_timeout(600), 600)
        self.assertEqual(launch.validate_ue_ready_timeout(900), 900)
        for value in (0, 59, 901):
            with self.subTest(value=value), self.assertRaises(RuntimeSafetyError):
                launch.validate_ue_ready_timeout(value)

    def test_ue_command_keeps_nowrite_and_exact_map(self) -> None:
        paths = validate_paths(self.make_layout())
        command = build_ue_command(
            paths,
            "/Game/VISTA/Scenes/MMG040_Office_BlenderR1",
            Ports(),
            gpu=1,
            width=1280,
            height=720,
            fps=60,
        )
        self.assertEqual(command[2], "/Game/VISTA/Scenes/MMG040_Office_BlenderR1")
        self.assertIn("-MCPPort=55582", command)
        self.assertIn("-PixelStreamingURL=ws://127.0.0.1:8616", command)
        self.assertIn("-NOWRITE", command)
        self.assertIn("-graphicsadapter=1", command)

    def test_plan_and_receipt_helpers_do_not_contain_token_values(self) -> None:
        paths = validate_paths(self.make_layout())
        plan = redacted_runtime_plan(
            paths=paths,
            map_path="/Game/VISTA/Scenes/MMG040_Office_BlenderR1",
            ports=Ports(),
            gpu=1,
            model_mode="off",
            coding_agents=False,
        )
        encoded = json.dumps(plan)
        self.assertIn("token_file", encoded)
        self.assertNotIn("token_value", encoded)
        with self.assertRaisesRegex(RuntimeSafetyError, "secret-bearing receipt key"):
            safe_public_state({"token_value": "must-not-write"})

    def test_child_environment_drops_inherited_credentials_and_authority(self) -> None:
        child = sanitized_child_environment(
            {
                "PATH": "/usr/bin",
                "HOME": "/tmp/home",
                "LANG": "C.UTF-8",
                "ANTHROPIC_API_KEY": "must-not-propagate",
                "STUDIO_ACCESS_TOKEN": "must-not-propagate",
                "STUDIO_TRANSPORT_PROFILE": "public_webrtc",
                "POSTGRES_URL": "must-not-propagate",
                "SSH_AUTH_SOCK": "/tmp/agent.sock",
            }
        )
        self.assertEqual(
            child, {"PATH": "/usr/bin", "HOME": "/tmp/home", "LANG": "C.UTF-8"}
        )

    def test_loopback_host_classification_rejects_wildcards_and_public_ips(
        self,
    ) -> None:
        self.assertTrue(host_is_loopback("127.0.0.1"))
        self.assertTrue(host_is_loopback("::1"))
        self.assertFalse(host_is_loopback("0.0.0.0"))
        self.assertFalse(host_is_loopback("::"))
        self.assertFalse(host_is_loopback("140.113.215.82"))

    def test_listener_pid_parser_collects_exact_unique_ss_owners(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout=(
                "LISTEN 0 511 127.0.0.1:8615 0.0.0.0:* "
                'users:(("node",pid=31001,fd=21),("node",pid=31001,fd=22))\n'
                "LISTEN 0 1 127.0.0.1:8615 0.0.0.0:* "
                'users:(("helper",pid=31002,fd=7))\n'
                "malformed owner pid=not-a-number\n"
            ),
        )
        with mock.patch.object(
            runtime_module.subprocess, "run", return_value=completed
        ):
            self.assertEqual(listener_pids_for_port(8615), {31001, 31002})

    def test_owned_loopback_listener_rejects_pid_mismatch(self) -> None:
        with (
            mock.patch.object(
                runtime_module, "assert_loopback_listener", return_value=["127.0.0.1"]
            ),
            mock.patch.object(
                runtime_module, "listener_pids_for_port", return_value={44002}
            ),
            self.assertRaisesRegex(
                RuntimeSafetyError, "does not match recorded owner 44001"
            ),
        ):
            assert_owned_loopback_listener(55582, 44001)

    def test_wait_for_listener_honors_cancellation_before_polling(self) -> None:
        with (
            mock.patch.object(runtime_module, "listeners_for_port") as listeners,
            self.assertRaisesRegex(RuntimeSafetyError, "stop was requested"),
        ):
            wait_for_loopback_listener(
                55582,
                timeout_seconds=30,
                cancelled=lambda: True,
            )
        listeners.assert_not_called()

    def test_private_runtime_directory_refuses_symlink_file_and_wrong_mode(
        self,
    ) -> None:
        workspace = self.root / "private-run"
        workspace.mkdir(mode=0o700)
        external = self.root / "external-runtime"
        external.mkdir(mode=0o700)
        runtime_path = workspace / "runtime"
        runtime_path.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeSafetyError, "must not be a symlink"):
            launch.ensure_private_directory(runtime_path, workspace)

        runtime_path.unlink()
        runtime_path.write_text("not a directory\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeSafetyError, "not a user-owned directory"):
            launch.ensure_private_directory(runtime_path, workspace)

        runtime_path.unlink()
        runtime_path.mkdir(mode=0o755)
        runtime_path.chmod(0o755)
        with self.assertRaisesRegex(RuntimeSafetyError, "mode must be exactly 0700"):
            launch.ensure_private_directory(runtime_path, workspace)

    def test_private_log_refuses_symlink_and_existing_path(self) -> None:
        logs = self.root / "logs"
        logs.mkdir(mode=0o700)
        fresh = logs / "fresh.log"
        handle = launch.open_exclusive_private_log(fresh)
        handle.write("owned\n")
        handle.close()
        self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o600)
        with self.assertRaisesRegex(RuntimeSafetyError, "non-fresh runtime log"):
            launch.open_exclusive_private_log(fresh)

        target = logs / "target.log"
        target.write_text("do not follow\n", encoding="utf-8")
        link = logs / "symlink.log"
        link.symlink_to(target)
        with self.assertRaisesRegex(RuntimeSafetyError, "non-fresh runtime log"):
            launch.open_exclusive_private_log(link)

    def test_launch_child_environment_contract_separates_ue_from_studio_token(
        self,
    ) -> None:
        paths = self.make_layout()
        calls: list[dict[str, object]] = []

        class ExitedChild:
            def __init__(self, pid: int) -> None:
                self.pid = pid
                self.returncode = 9

            def poll(self) -> int:
                return self.returncode

        def capture_child(
            command: list[str], *, env: dict[str, str], cwd: Path, log_path: Path
        ) -> tuple[ExitedChild, io.StringIO]:
            calls.append(
                {
                    "command": list(command),
                    "env": dict(env),
                    "cwd": cwd,
                    "log_path": log_path,
                }
            )
            return ExitedChild(51000 + len(calls)), io.StringIO()

        def identity(pid: int, role: str) -> dict[str, object]:
            return {
                "pid": pid,
                "role": role,
                "start_ticks": 1,
                "executable": "/owned/test",
                "process_group": pid,
            }

        arguments = [
            "--workspace",
            str(paths.workspace),
            "--project",
            str(paths.project),
            "--map",
            "/Game/VISTA/Scenes/MMG040_Office_BlenderR1",
            "--ue-editor",
            str(paths.ue_editor),
            "--cirrus-dir",
            str(paths.cirrus_dir),
            "--studio-workspace",
            str(paths.studio_workspace),
            "--node-bin",
            str(paths.node_bin),
            "--model-mode",
            "mock",
        ]
        with (
            mock.patch.object(launch, "assert_ports_available"),
            mock.patch.object(launch, "ensure_access_token", return_value="s" * 48),
            mock.patch.object(launch, "atomic_write_json"),
            mock.patch.object(launch, "write_state"),
            mock.patch.object(
                launch,
                "open_exclusive_private_log",
                side_effect=lambda _path: io.StringIO(),
            ),
            mock.patch.object(launch, "start_child", side_effect=capture_child),
            mock.patch.object(launch, "process_start_ticks", return_value=1),
            mock.patch.object(launch, "process_identity", side_effect=identity),
            mock.patch.object(launch, "wait_for_loopback_listener"),
            mock.patch.object(launch, "terminate_owned"),
            mock.patch.object(launch.signal, "signal"),
        ):
            self.assertEqual(launch.main(arguments), 1)
            arguments[-1] = "off"
            self.assertEqual(launch.main(arguments), 1)

        self.assertEqual(len(calls), 6)
        cirrus_env = calls[0]["env"]
        ue_env = calls[1]["env"]
        studio_env = calls[2]["env"]
        off_studio_env = calls[5]["env"]
        self.assertEqual(cirrus_env["STUDIO_ACCESS_TOKEN"], "s" * 48)
        self.assertNotIn("STUDIO_ACCESS_TOKEN", ue_env)
        self.assertNotIn("STUDIO_ACCESS_TOKEN_FILE", ue_env)
        self.assertEqual(studio_env["STUDIO_ACCESS_TOKEN"], "s" * 48)
        self.assertEqual(studio_env["UE_PROJECT_PATH"], str(paths.project.resolve()))
        self.assertTrue(str(studio_env["UE_PROJECT_PATH"]).endswith(".uproject"))
        self.assertEqual(studio_env["MOCK_MODE"], "1")
        self.assertEqual(off_studio_env["MOCK_MODE"], "0")

    def test_live_qa_marker_parse_and_arbitrary_script_refusal(self) -> None:
        payload = {"action": "state", "pie": True}
        reply = {
            "result": {
                "python_logs": [
                    "LogPython: "
                    + live_qa.MARKER_PREFIX
                    + ":"
                    + json.dumps(payload, separators=(",", ":"))
                ]
            }
        }
        self.assertEqual(live_qa.parse_marker(reply), payload)
        with self.assertRaisesRegex(RuntimeSafetyError, "refusing non-fixed"):
            live_qa.send_fixed_script(55582, "print('arbitrary')")
        self.assertEqual(
            set(live_qa.FIXED_SCRIPTS),
            {
                "state",
                "verify",
                "stop",
                "diagnose-start",
                "stage-start-candidate",
            },
        )
        self.assertIn("get_unscaled_capsule_radius", live_qa.START_DIAGNOSTIC_SCRIPT)
        self.assertNotIn("eval(", live_qa.START_DIAGNOSTIC_SCRIPT)
        self.assertNotIn("exec(", live_qa.START_DIAGNOSTIC_SCRIPT)
        self.assertIn(
            "unreal.Vector(x=150.0, y=-150.0, z=100.0)",
            live_qa.STAGE_START_CANDIDATE_SCRIPT,
        )
        self.assertIn("saved': False", live_qa.STAGE_START_CANDIDATE_SCRIPT)

    def test_live_acceptance_passes_grounded_world_and_rejects_falling_entities(
        self,
    ) -> None:
        accepted = {
            "action": "state",
            "pie": True,
            "possessed": True,
            "world": live_qa.EXPECTED_WORLD,
            "pawn_class": live_qa.EXPECTED_PAWN_CLASS,
            "pawn_location_cm": [150.0, -150.0, 96.0],
            "on_ground": True,
            "physics_prop": {
                "location_cm": [140.0, 50.0, 62.0],
                "simulate_physics": True,
            },
            "floor": {
                "origin_cm": [0.0, 0.0, -5.0],
                "extent_cm": [600.0, 450.0, 10.0],
                "collision_enabled": "QueryAndPhysics",
            },
        }
        self.assertTrue(live_qa.live_acceptance_passed(accepted))

        fallen_pawn = copy.deepcopy(accepted)
        fallen_pawn["pawn_location_cm"][2] = -50.0
        self.assertFalse(live_qa.live_acceptance_passed(fallen_pawn))

        fallback_origin = copy.deepcopy(accepted)
        fallback_origin["pawn_location_cm"] = [0.0, 0.0, 96.0]
        self.assertFalse(live_qa.live_acceptance_passed(fallback_origin))

        fallen_box = copy.deepcopy(accepted)
        fallen_box["physics_prop"]["location_cm"][2] = -50.0
        self.assertFalse(live_qa.live_acceptance_passed(fallen_box))

    def test_live_qa_failure_is_written_before_error_is_raised(self) -> None:
        workspace = self.root / "owned-runtime"
        (workspace / "receipts").mkdir(parents=True)
        state = {
            "created_at": "2026-08-10T00:00:00+00:00",
            "ready_at": "2026-08-10T00:01:00+00:00",
            "map": "/Game/VISTA/Scenes/MMG040_Office_BlenderR1",
        }
        with (
            mock.patch.object(
                live_qa,
                "load_owned_runtime",
                return_value=(workspace, state, 55582),
            ),
            mock.patch.object(
                live_qa,
                "send_fixed_script",
                side_effect=RuntimeSafetyError("synthetic fixed-script failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeSafetyError, "failure receipt"):
                live_qa.run_action(workspace, "verify", 5.0)

        receipts = list((workspace / "receipts").glob("live-qa-verify-*.json"))
        self.assertEqual(len(receipts), 1)
        payload = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["acceptance"], "failed")
        self.assertEqual(payload["error"], "synthetic fixed-script failure")
        self.assertIsNone(payload["command"])

    def test_browser_input_acceptance_requires_grounded_bounded_displacement(self) -> None:
        receipts = self.root / "receipts"
        evidence = self.root / "evidence"
        receipts.mkdir()
        evidence.mkdir()
        trace = evidence / "trace.zip"
        trace.write_bytes(b"synthetic-playwright-trace")

        def state(created_at: str, engine_time: float, location: list[float]) -> dict:
            observed = {
                "action": "state",
                "pie": True,
                "possessed": True,
                "on_ground": True,
                "world": verify_browser_input.EXPECTED_WORLD,
                "pawn_class": live_qa.EXPECTED_PAWN_CLASS,
                "pawn_location_cm": location,
                "engine_time_s": engine_time,
            }
            return {
                "schema": live_qa.RECEIPT_SCHEMA,
                "created_at": created_at,
                "action": "state",
                "acceptance": "observed",
                "error": None,
                "runtime_created_at": "2026-08-10T00:00:00+00:00",
                "runtime_ready_at": "2026-08-10T00:01:00+00:00",
                "observed": observed,
            }

        before = receipts / "before.json"
        after = receipts / "after.json"
        before.write_text(
            json.dumps(state("2026-08-10T00:02:00+00:00", 10.0, [150.0, -150.0, 92.3])),
            encoding="utf-8",
        )
        after.write_text(
            json.dumps(state("2026-08-10T00:02:04+00:00", 14.0, [190.0, -175.0, 92.4])),
            encoding="utf-8",
        )
        accepted = verify_browser_input.build_acceptance(before, after, trace)
        self.assertEqual(accepted["acceptance"], "passed")
        self.assertGreater(accepted["displacement_cm"], 5.0)

        after.write_text(
            json.dumps(state("2026-08-10T00:02:04+00:00", 14.0, [150.0, -150.0, 92.3])),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeSafetyError, "displacement"):
            verify_browser_input.build_acceptance(before, after, trace)

    def test_browser_trace_rejects_active_token_and_network_logs(self) -> None:
        workspace = self.root / "trace-run"
        evidence = workspace / "evidence"
        evidence.mkdir(parents=True)
        token = b"t" * 64
        token_path = workspace / "access-token"
        token_path.write_bytes(token + b"\n")
        token_path.chmod(0o600)

        safe = evidence / "safe.zip"
        with zipfile.ZipFile(safe, "w") as archive:
            archive.writestr("trace.action", b"bounded keyboard action")
        self.assertEqual(
            verify_browser_input.verify_trace_secret_hygiene(workspace, safe),
            {"active_token_present": False, "network_log_included": False},
        )

        token_bearing = evidence / "token.trace"
        token_bearing.write_bytes(b"prefix" + token + b"suffix")
        with self.assertRaisesRegex(RuntimeSafetyError, "active access token"):
            verify_browser_input.verify_trace_secret_hygiene(
                workspace, token_bearing
            )

        network = evidence / "network.zip"
        with zipfile.ZipFile(network, "w") as archive:
            archive.writestr("trace.network", b"no literal token required")
        with self.assertRaisesRegex(RuntimeSafetyError, "network logs"):
            verify_browser_input.verify_trace_secret_hygiene(workspace, network)

    def test_cli_preflight_does_not_create_token_or_start_state(self) -> None:
        paths = self.make_layout()
        output = io.StringIO()
        with (
            mock.patch.object(launch, "assert_ports_available"),
            redirect_stdout(output),
        ):
            result = launch.main(
                [
                    "--workspace",
                    str(paths.workspace),
                    "--project",
                    str(paths.project),
                    "--map",
                    "/Game/VISTA/Scenes/MMG040_Office_BlenderR1",
                    "--ue-editor",
                    str(paths.ue_editor),
                    "--cirrus-dir",
                    str(paths.cirrus_dir),
                    "--studio-workspace",
                    str(paths.studio_workspace),
                    "--node-bin",
                    str(paths.node_bin),
                    "--preflight-only",
                ]
            )
        self.assertEqual(result, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["bind_host"], "127.0.0.1")
        self.assertEqual(plan["gpu"], 1)
        self.assertFalse(paths.token_file.exists())
        self.assertFalse((paths.workspace / "runtime-state.json").exists())


if __name__ == "__main__":
    unittest.main()
