import importlib.util
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from simworld_arena.launcher import (
    CIRRUS_LOOPBACK_PATCH_MARKER,
    EXPECTED_ORIGINAL_CIRRUS_SHA256,
    REQUIRED_WORKSPACE_SECURITY_PATHS,
    VISTA_DEMO_MAP,
    build_ue_map_url,
    configure_vista_demo_server_environment,
    detected_gpu_indices,
    ensure_private_workspace_directory,
    _parse_private_relative,
    generate_mcp_config,
    get_nvidia_headless_icd,
    has_unrealcv_plugin,
    make_cirrus_config,
    is_reviewed_idle_graphics_process,
    make_model_off_child_environment,
    make_ue_command,
    prepare_isolated_demo_environment,
    prepare_nvidia_compat_libraries,
    require_gpu_idle,
    require_loopback_listeners,
    require_ports_free,
    resolve_project_map,
    sha256_dependency_tree,
    sha256_file,
    sha256_server_source_tree,
    sha256_tree,
    setup_workspace,
    start_managed_process,
    terminate_managed_process_group,
    ue_startup_timeout_seconds,
    ue_fps_log_confirms,
    validate_gpu_index,
    validate_model_off_child_environment,
    validate_cirrus_loopback_patch,
    validate_prepared_workspace,
    validate_vista_demo_assets,
    validate_vista_demo_map,
    wait_for_port,
)


REPOSITORY = Path(__file__).resolve().parents[2]
STAGE_TOOL_PATH = REPOSITORY / "tools" / "stage_vista_workspace.py"
STAGE_TOOL_SPEC = importlib.util.spec_from_file_location("stage_vista_workspace", STAGE_TOOL_PATH)
assert STAGE_TOOL_SPEC and STAGE_TOOL_SPEC.loader
STAGE_TOOL = importlib.util.module_from_spec(STAGE_TOOL_SPEC)
STAGE_TOOL_SPEC.loader.exec_module(STAGE_TOOL)


class LauncherSecurityTests(unittest.TestCase):
    def test_private_workspace_paths_reject_normalized_aliases_and_root_before_chmod(self):
        for invalid in ("", ".", "..", "a//b", "./a", "a/.", "a/"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(RuntimeError, "invalid"):
                    _parse_private_relative(invalid)
        self.assertEqual(_parse_private_relative("a/b").parts, ("a", "b"))

        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package"
            package.mkdir()
            with mock.patch("simworld_arena.launcher.os.fchmod", wraps=os.fchmod) as fchmod:
                with self.assertRaisesRegex(RuntimeError, "filesystem root"):
                    setup_workspace(Path("/"), package)
            fchmod.assert_not_called()

    def test_workspace_setup_rejects_resolve_to_open_ancestor_swap_without_chmod(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ancestor = base / "launcher-trusted-ancestor"
            original_parent = ancestor / "parent"
            original_parent.mkdir(parents=True, mode=0o755)
            original_parent.chmod(0o755)
            workspace = original_parent / "workspace"
            displaced = base / "launcher-ancestor-displaced"
            outside = base / "launcher-outside"
            outside_parent = outside / "parent"
            outside_parent.mkdir(parents=True, mode=0o755)
            outside_parent.chmod(0o755)
            original_mode = stat.S_IMODE(original_parent.stat().st_mode)
            outside_mode = stat.S_IMODE(outside_parent.stat().st_mode)
            package = base / "package"
            package.mkdir()
            real_open = os.open
            swapped = False

            def swap_intermediate_before_open(target, *args, **kwargs):
                nonlocal swapped
                if (
                    not swapped
                    and str(target) == ancestor.name
                    and kwargs.get("dir_fd") is not None
                ):
                    swapped = True
                    ancestor.rename(displaced)
                    ancestor.symlink_to(outside, target_is_directory=True)
                return real_open(target, *args, **kwargs)

            with mock.patch(
                "simworld_arena.launcher.os.open",
                side_effect=swap_intermediate_before_open,
            ):
                with self.assertRaisesRegex(RuntimeError, "unavailable or unsafe"):
                    setup_workspace(workspace, package)

            self.assertTrue(swapped)
            self.assertEqual(stat.S_IMODE((displaced / "parent").stat().st_mode), original_mode)
            self.assertEqual(stat.S_IMODE(outside_parent.stat().st_mode), outside_mode)
            self.assertEqual(list((displaced / "parent").iterdir()), [])
            self.assertEqual(list(outside_parent.iterdir()), [])

    def test_workspace_setup_provisions_private_review_evidence_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace = base / "workspace"
            package = base / "package"
            package.mkdir()

            self.assertEqual(setup_workspace(workspace, package), workspace.resolve())
            for relative in (
                "tmp/review-evidence",
                "tmp/review-evidence/text",
                "tmp/review-evidence/visual",
            ):
                directory = workspace / relative
                self.assertTrue(directory.is_dir())
                self.assertFalse(directory.is_symlink())
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                self.assertEqual(directory.stat().st_uid, os.geteuid())

            visual = workspace / "tmp" / "review-evidence" / "visual"
            visual.chmod(0o755)
            setup_workspace(workspace, package)
            self.assertEqual(stat.S_IMODE(visual.stat().st_mode), 0o700)

            visual.rmdir()
            outside = base / "outside"
            outside.mkdir()
            visual.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "unavailable or unsafe"):
                setup_workspace(workspace, package)

    def test_workspace_setup_rejects_tmp_symlinks_and_parent_swap_without_outside_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            package = base / "package"
            package.mkdir()

            for leaf_name in ("screens", "thumbnails"):
                workspace = base / f"workspace-{leaf_name}"
                setup_workspace(workspace, package)
                leaf = workspace / "tmp" / leaf_name
                leaf.rmdir()
                outside = base / f"outside-{leaf_name}"
                outside.mkdir()
                leaf.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(RuntimeError, "unavailable or unsafe"):
                    setup_workspace(workspace, package)
                self.assertEqual(list(outside.iterdir()), [])

            workspace = base / "workspace-tmp"
            workspace.mkdir(mode=0o700)
            outside_tmp = base / "outside-tmp"
            outside_tmp.mkdir()
            (workspace / "tmp").symlink_to(outside_tmp, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "unavailable or unsafe"):
                setup_workspace(workspace, package)
            self.assertEqual(list(outside_tmp.iterdir()), [])

            race_workspace = base / "workspace-race"
            setup_workspace(race_workspace, package)
            (race_workspace / "tmp" / "screens").rmdir()
            displaced = race_workspace / "tmp-displaced"
            outside_race = base / "outside-race"
            outside_race.mkdir()
            original_mkdir = os.mkdir
            swapped = False

            def swap_parent_before_mkdir(target, *args, **kwargs):
                nonlocal swapped
                if not swapped and str(target) == "screens" and kwargs.get("dir_fd") is not None:
                    swapped = True
                    os.rename(race_workspace / "tmp", displaced)
                    os.symlink(outside_race, race_workspace / "tmp", target_is_directory=True)
                return original_mkdir(target, *args, **kwargs)

            with mock.patch("simworld_arena.launcher.os.mkdir", side_effect=swap_parent_before_mkdir):
                with self.assertRaisesRegex(RuntimeError, "changed|unavailable or unsafe"):
                    setup_workspace(race_workspace, package)
            self.assertTrue(swapped)
            self.assertEqual(list(outside_race.iterdir()), [])
            self.assertTrue((displaced / "screens").is_dir())

    def test_workspace_setup_keeps_post_validation_mutations_on_open_descriptors(self):
        for swapped_relative in ("web/server", "web"):
            with self.subTest(swapped_relative=swapped_relative):
                with tempfile.TemporaryDirectory() as temporary:
                    base = Path(temporary)
                    package = base / "package"
                    package_server = package / "server"
                    package_server.mkdir(parents=True)
                    (package_server / "sentinel.js").write_text("descriptor anchored\n")
                    workspace = base / "workspace"
                    setup_workspace(workspace, base / "empty-package")
                    (workspace / ".studio_version").unlink()

                    target = workspace / swapped_relative
                    displaced = workspace / f"{swapped_relative.replace('/', '-')}-displaced"
                    outside = base / f"outside-{swapped_relative.replace('/', '-')}"
                    outside.mkdir()
                    original_copy = __import__(
                        "simworld_arena.launcher", fromlist=["_copy_file_at"]
                    )._copy_file_at
                    swapped = False

                    def swap_after_validation(source, directory_fd, name):
                        nonlocal swapped
                        if not swapped:
                            swapped = True
                            target.rename(displaced)
                            target.symlink_to(outside, target_is_directory=True)
                        return original_copy(source, directory_fd, name)

                    with mock.patch(
                        "simworld_arena.launcher._copy_file_at",
                        side_effect=swap_after_validation,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "changed"):
                            setup_workspace(workspace, package)

                    self.assertTrue(swapped)
                    self.assertEqual(list(outside.iterdir()), [])
                    if swapped_relative == "web/server":
                        copied = displaced / "sentinel.js"
                    else:
                        copied = displaced / "server" / "sentinel.js"
                    self.assertEqual(copied.read_text(), "descriptor anchored\n")

    def test_private_directory_faults_close_parent_and_child_descriptors(self):
        operations = ("fstat", "fchmod", "fsync")
        for operation in operations:
            for attempt in range(12):
                with self.subTest(operation=operation, attempt=attempt):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        baseline = len(os.listdir("/proc/self/fd"))
                        real_open = os.open
                        real_operation = getattr(os, operation)
                        opened = {}

                        def tracking_open(path, *args, **kwargs):
                            descriptor = real_open(path, *args, **kwargs)
                            if str(path) == "child" and kwargs.get("dir_fd") is not None:
                                opened["child"] = descriptor
                            elif kwargs.get("dir_fd") is None and Path(path) == root:
                                opened["parent"] = descriptor
                            return descriptor

                        def fail_child(descriptor, *args, **kwargs):
                            if descriptor == opened.get("child"):
                                raise OSError(f"injected child {operation} failure")
                            return real_operation(descriptor, *args, **kwargs)

                        with mock.patch(
                            "simworld_arena.launcher.os.open",
                            side_effect=tracking_open,
                        ), mock.patch(
                            f"simworld_arena.launcher.os.{operation}",
                            side_effect=fail_child,
                        ):
                            with self.assertRaisesRegex(OSError, f"child {operation}"):
                                ensure_private_workspace_directory(root, "child")

                        for descriptor in opened.values():
                            with self.assertRaises(OSError):
                                os.fstat(descriptor)
                        self.assertEqual(len(os.listdir("/proc/self/fd")), baseline)

    def test_private_directory_cleanup_never_masks_the_primary_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = len(os.listdir("/proc/self/fd"))
            real_open = os.open
            real_fstat = os.fstat
            real_close = os.close
            opened = {}

            def tracking_open(path, *args, **kwargs):
                descriptor = real_open(path, *args, **kwargs)
                if str(path) == "child" and kwargs.get("dir_fd") is not None:
                    opened["child"] = descriptor
                return descriptor

            def fail_child_fstat(descriptor):
                if descriptor == opened.get("child"):
                    raise OSError("primary child fstat failure")
                return real_fstat(descriptor)

            def close_then_report_failure(descriptor):
                real_close(descriptor)
                if descriptor == opened.get("child"):
                    raise OSError("secondary cleanup close failure")

            with mock.patch(
                "simworld_arena.launcher.os.open",
                side_effect=tracking_open,
            ), mock.patch(
                "simworld_arena.launcher.os.fstat",
                side_effect=fail_child_fstat,
            ), mock.patch(
                "simworld_arena.launcher.os.close",
                side_effect=close_then_report_failure,
            ):
                with self.assertRaisesRegex(OSError, "primary child fstat failure"):
                    ensure_private_workspace_directory(root, "child")

            self.assertEqual(len(os.listdir("/proc/self/fd")), baseline)

    def test_vista_demo_allows_one_bounded_cold_shader_compile(self):
        self.assertEqual(ue_startup_timeout_seconds(vista_demo=True), 300)
        self.assertEqual(ue_startup_timeout_seconds(vista_demo=False), 120)

    def test_nvidia_compat_links_resolve_root_owned_driver_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            compat = prepare_nvidia_compat_libraries(Path(temporary))
            for name in ("libcuda.so", "libnvcuvid.so"):
                link = compat / name
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve().stat().st_uid, 0)
            environment = dict(os.environ, LD_LIBRARY_PATH=str(compat))
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import ctypes; "
                    "[ctypes.CDLL(name) for name in "
                    "('libcuda.so','libnvcuvid.so')]",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_project_map_must_exist_inside_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary) / "project"
            content = project_root / "Content" / "Maps"
            content.mkdir(parents=True)
            project = project_root / "project.uproject"
            project.write_text("{}")
            (content / "Empty.umap").write_bytes(b"map")

            self.assertEqual(
                resolve_project_map(project, "/Game/Maps/Empty"),
                "/Game/Maps/Empty.umap",
            )
            self.assertEqual(
                resolve_project_map(project, "/Game/Maps/Empty.umap"),
                "/Game/Maps/Empty.umap",
            )
            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                resolve_project_map(project, "/Game/Main")
            with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                resolve_project_map(project, "/Game/../outside")

    def test_vista_demo_assets_are_hash_pinned(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary) / "project"
            project_root.mkdir()
            project = project_root / "project.uproject"
            project.write_text("{}")
            relative_assets = {
                "Demo/Pawn.uasset": b"pawn",
                "Demo/GameMode.uasset": b"game-mode",
                "Demo/Input.uasset": b"input",
                "Demo/Animations/Walk.uasset": b"walk-animation",
            }
            expected = {}
            for relative, payload in relative_assets.items():
                asset = project_root / "Content" / relative
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_bytes(payload)
                expected[relative] = sha256_file(asset)

            expected_assets = {
                relative: digest
                for relative, digest in expected.items()
                if relative != "Demo/Animations/Walk.uasset"
            }
            expected_trees = {"Demo": sha256_tree(project_root / "Content" / "Demo")}
            with mock.patch(
                "simworld_arena.launcher.VISTA_DEMO_ASSETS", expected_assets
            ), mock.patch("simworld_arena.launcher.VISTA_DEMO_ASSET_TREES", expected_trees):
                validate_vista_demo_assets(project)
                (project_root / "Content" / "Demo/Pawn.uasset").write_bytes(b"tampered")
                with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                    validate_vista_demo_assets(project)
                (project_root / "Content" / "Demo/Pawn.uasset").write_bytes(b"pawn")
                validate_vista_demo_assets(project)
                (project_root / "Content" / "Demo/Animations/Walk.uasset").write_bytes(
                    b"tampered-dependency"
                )
                with self.assertRaisesRegex(RuntimeError, "tree SHA-256 mismatch"):
                    validate_vista_demo_assets(project)

    def test_vista_demo_empty_map_is_hash_pinned(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary) / "project"
            map_path = project_root / "Content" / "Maps" / "Empty.umap"
            map_path.parent.mkdir(parents=True)
            map_path.write_bytes(b"fixed-empty-map")
            project = project_root / "project.uproject"
            project.write_text("{}")
            expected = sha256_file(map_path)
            with mock.patch("simworld_arena.launcher.VISTA_DEMO_MAP_SHA256", expected):
                validate_vista_demo_map(project, f"{VISTA_DEMO_MAP}.umap")
                map_path.write_bytes(b"tampered-map")
                with self.assertRaisesRegex(RuntimeError, "map SHA-256 mismatch"):
                    validate_vista_demo_map(project, f"{VISTA_DEMO_MAP}.umap")
                with self.assertRaisesRegex(RuntimeError, "fixed map"):
                    validate_vista_demo_map(project, "/Game/Maps/Other.umap")

    def test_vista_demo_map_url_uses_only_fixed_game_mode(self):
        self.assertEqual(
            build_ue_map_url("/Game/Maps/Empty.umap", True),
            "/Game/Maps/Empty.umap",
        )
        self.assertEqual(
            build_ue_map_url("/Game/Maps/Empty.umap", False),
            "/Game/Maps/Empty.umap",
        )
        with self.assertRaisesRegex(ValueError, "fixed map"):
            build_ue_map_url("/Game/Maps/Other.umap", True)

    def test_ue_command_disables_analytics_and_pairs_fps(self):
        command = make_ue_command(
            ue_editor="/runtime/UnrealEditor",
            project_file="/runtime/project.uproject",
            ue_map="/Game/Maps/Empty.umap",
            mcp_port=55560,
            gpu_index=1,
            cirrus_ws_port=8586,
            fps=60,
            vista_demo=True,
            local_data_cache_path="/private/cache/ue-ddc",
            user_dir="/private/runtime/vista-demo-sandbox/ue-user",
        )
        self.assertIn("-NoAnalytics", command)
        self.assertIn(
            "-ini:EditorSettings:[/Script/UnrealEd.AnalyticsPrivacySettings]:"
            "bSendUsageData=False",
            command,
        )
        self.assertNotIn("-FPSMAX=60", command)
        self.assertIn("-ExecCmds=t.MaxFPS 60,t.MaxFPS", command)
        self.assertIn("-PixelStreamingWebRTCFps=60", command)
        self.assertEqual(command[2], "/Game/Maps/Empty.umap")
        self.assertNotIn("?game=", " ".join(command))
        self.assertNotIn("GlobalDefaultGameMode", " ".join(command))
        self.assertNotIn("-NOAUTOINIUPDATE", command)
        self.assertIn("-NOWRITE", command)
        self.assertNotIn("-Immersive", command)
        self.assertIn(
            "-ini:Engine:[/Script/Engine.RendererSettings]:"
            "r.Shadow.Virtual.Enable=0",
            command,
        )
        self.assertIn(
            "-ini:EditorPerProjectUserSettings:"
            "[/Script/UnrealEd.EditorLoadingSavingSettings]:bAutoSaveEnable=False",
            command,
        )
        self.assertIn("-LocalDataCachePath=/private/cache/ue-ddc", command)
        self.assertIn("-SaveToUserDir", command)
        self.assertIn(
            "-UserDir=/private/runtime/vista-demo-sandbox/ue-user",
            command,
        )
        non_demo_command = make_ue_command(
            ue_editor="editor",
            project_file="project",
            ue_map="/Game/Maps/Empty.umap",
            mcp_port=55560,
            gpu_index=0,
            cirrus_ws_port=8586,
            fps=30,
            vista_demo=False,
        )
        self.assertNotIn("-Immersive", non_demo_command)
        self.assertNotIn("bAutoSaveEnable=False", " ".join(non_demo_command))
        self.assertNotIn("-SaveToUserDir", non_demo_command)
        self.assertFalse(any(argument.startswith("-UserDir=") for argument in non_demo_command))
        with self.assertRaisesRegex(ValueError, "30 or 60"):
            make_ue_command(
                ue_editor="editor",
                project_file="project",
                ue_map="/Game/Maps/Empty.umap",
                mcp_port=55560,
                gpu_index=0,
                cirrus_ws_port=8586,
                fps=15,
                vista_demo=False,
            )
        with self.assertRaisesRegex(ValueError, "non-negative"):
            make_ue_command(
                ue_editor="editor",
                project_file="project",
                ue_map="/Game/Maps/Empty.umap",
                mcp_port=55560,
                gpu_index=-1,
                cirrus_ws_port=8586,
                fps=60,
                vista_demo=False,
            )
        with self.assertRaisesRegex(ValueError, "isolated local data cache"):
            make_ue_command(
                ue_editor="editor",
                project_file="project",
                ue_map="/Game/Maps/Empty.umap",
                mcp_port=55560,
                gpu_index=0,
                cirrus_ws_port=8586,
                fps=60,
                vista_demo=True,
            )
        with self.assertRaisesRegex(ValueError, "isolated Unreal user directory"):
            make_ue_command(
                ue_editor="editor",
                project_file="project",
                ue_map="/Game/Maps/Empty.umap",
                mcp_port=55560,
                gpu_index=0,
                cirrus_ws_port=8586,
                fps=60,
                vista_demo=True,
                local_data_cache_path="/private/cache/ue-ddc",
            )

    def test_ue_fps_confirmation_requires_runtime_query_not_command_line_echo(self):
        command_line_only = (
            "LogInit: Command Line: -ExecCmds=t.MaxFPS 60,t.MaxFPS "
            "-PixelStreamingWebRTCFps=60\n"
        )
        self.assertFalse(ue_fps_log_confirms(command_line_only, 60))
        self.assertFalse(
            ue_fps_log_confirms(
                "[2026.07.13-17.10.20:000][  0]LogInit: Command Line: "
                '-ExecCmds=t.MaxFPS 60,t.MaxFPS LastSetBy: Console\n',
                60,
            )
        )
        self.assertFalse(
            ue_fps_log_confirms(
                '[not a UE timestamp]t.MaxFPS = "60" LastSetBy: Console\n',
                60,
            )
        )
        self.assertFalse(
            ue_fps_log_confirms(
                "[2026.07.13-17.10.26:722][  1]\n"
                't.MaxFPS = "60" LastSetBy: Console\n',
                60,
            )
        )
        self.assertFalse(
            ue_fps_log_confirms(
                '[2026.07.13-17.10.26:722][  1]t.MaxFPS = "60"\n'
                "LastSetBy: Console\n",
                60,
            )
        )
        self.assertFalse(
            ue_fps_log_confirms(
                '[2026.07.13-17.10.26:722][  1]t.MaxFPS = "60" '
                "LastSetBy: ProjectSetting\n",
                60,
            )
        )
        self.assertTrue(
            ue_fps_log_confirms(
                command_line_only
                + 'LogConsoleResponse: Display: t.MaxFPS = "60.000000"      '
                "LastSetBy: Console\n",
                60,
            )
        )
        self.assertTrue(
            ue_fps_log_confirms(
                '[2026.07.13-17.10.26:722][  1]t.MaxFPS = "60"      '
                "LastSetBy: Console\n",
                60,
            )
        )
        self.assertFalse(
            ue_fps_log_confirms(
                'LogConsoleResponse: Display: t.MaxFPS = "30"      LastSetBy: Console\n',
                60,
            )
        )

    def test_model_off_children_do_not_inherit_provider_credentials(self):
        source = {
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "secret",
            "AWS_SESSION_TOKEN": "secret",
        }
        self.assertEqual(
            make_model_off_child_environment(source),
            {"PATH": "/usr/bin"},
        )

    def test_demo_server_fps_environment_is_explicit_and_bounded(self):
        environment = {"VISTA_DEMO_FPS": "spoofed"}
        self.assertEqual(
            configure_vista_demo_server_environment(environment, vista_demo=True, fps=60),
            {"VISTA_DEMO_ENABLED": "1", "VISTA_DEMO_FPS": "60"},
        )
        self.assertEqual(
            configure_vista_demo_server_environment(environment, vista_demo=False, fps=30),
            {"VISTA_DEMO_ENABLED": "0"},
        )
        with self.assertRaisesRegex(ValueError, "30 or 60"):
            configure_vista_demo_server_environment(environment, vista_demo=True, fps=15)

    def test_demo_child_environment_is_credential_free_and_workspace_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            source = {
                "PATH": "/usr/bin",
                "HOME": "/home/user",
                "XDG_CONFIG_HOME": "/home/user/.config",
                "DISPLAY": ":99",
                "WAYLAND_DISPLAY": "wayland-9",
                "XAUTHORITY": "/home/user/.Xauthority",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "CLAUDE_CODE_OAUTH_TOKEN": "secret",
                "GITHUB_TOKEN": "secret",
                "GOOGLE_APPLICATION_CREDENTIALS": "/home/user/google.json",
                "AWS_PROFILE": "research",
            }
            environment = prepare_isolated_demo_environment(workspace, source)
            self.assertEqual(environment["PATH"], "/usr/bin")
            for key in (
                "DISPLAY",
                "WAYLAND_DISPLAY",
                "XAUTHORITY",
                "DBUS_SESSION_BUS_ADDRESS",
            ):
                self.assertNotIn(key, environment)
            for key in (
                "HOME",
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
                "XDG_DATA_HOME",
                "XDG_STATE_HOME",
                "XDG_RUNTIME_DIR",
                "TMPDIR",
                "CUDA_CACHE_PATH",
                "__GL_SHADER_DISK_CACHE_PATH",
            ):
                path = Path(environment[key])
                self.assertTrue(path.is_dir(), key)
                self.assertTrue(path.is_relative_to(workspace.resolve()), key)
                self.assertEqual(path.stat().st_mode & 0o777, 0o700, key)
            ddc_path = Path(environment["XDG_CACHE_HOME"]) / "UnrealEngine" / "DDC"
            self.assertTrue(ddc_path.is_dir())
            self.assertEqual(ddc_path.stat().st_mode & 0o777, 0o700)
            ue_user_dir = Path(environment["HOME"]).parent / "ue-user"
            self.assertTrue(ue_user_dir.is_dir())
            self.assertTrue(ue_user_dir.is_relative_to(workspace.resolve()))
            self.assertEqual(ue_user_dir.stat().st_mode & 0o777, 0o700)
            for key in source:
                if "TOKEN" in key or key in (
                    "GOOGLE_APPLICATION_CREDENTIALS",
                    "AWS_PROFILE",
                ):
                    self.assertNotIn(key, environment)
            environment["STUDIO_ACCESS_TOKEN"] = "local-only"
            validate_model_off_child_environment(environment)
            with self.assertRaisesRegex(RuntimeError, "sensitive keys"):
                validate_model_off_child_environment({"GITHUB_TOKEN": "leaked"})

    def test_model_off_mcp_config_has_no_agent_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "web").mkdir()
            path = generate_mcp_config(workspace, "127.0.0.1", "55560")
            self.assertEqual(json.loads(Path(path).read_text()), {"mcpServers": {}})

    def test_gpu_index_and_idle_preflight_fail_closed(self):
        inventory = ["0, NVIDIA A, 24576 MiB", "2, NVIDIA B, 24576 MiB"]
        self.assertEqual(detected_gpu_indices(inventory), [0, 2])
        self.assertEqual(validate_gpu_index(2, inventory), 2)
        for invalid in (-1, 1, 3):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                RuntimeError, "non-negative|unavailable"
            ):
                validate_gpu_index(invalid, inventory)
        with self.assertRaisesRegex(RuntimeError, "Malformed"):
            detected_gpu_indices(["not-a-gpu-row"])

        idle = mock.Mock(returncode=0, stdout="2, GPU-bbb, 128, 0\n", stderr="")
        no_processes = mock.Mock(
            returncode=0,
            stdout=(
                "<nvidia_smi_log><gpu><uuid>GPU-bbb</uuid>"
                "<processes /></gpu></nvidia_smi_log>"
            ),
            stderr="",
        )
        with mock.patch(
            "simworld_arena.launcher.subprocess.run", side_effect=[idle, no_processes]
        ):
            state = require_gpu_idle(2)
        self.assertEqual(state["uuid"], "GPU-bbb")
        self.assertEqual(state["memory_used_mib"], 128)

        compute = mock.Mock(
            returncode=0,
            stdout=(
                "<nvidia_smi_log><gpu><uuid>GPU-bbb</uuid><processes>"
                "<process_info><pid>4242</pid><type>C</type>"
                "<process_name>/usr/bin/python</process_name>"
                "<used_memory>8192 MiB</used_memory></process_info>"
                "</processes></gpu></nvidia_smi_log>"
            ),
            stderr="",
        )
        with mock.patch(
            "simworld_arena.launcher.subprocess.run", side_effect=[idle, compute]
        ), self.assertRaisesRegex(RuntimeError, "active process IDs/types"):
            require_gpu_idle(2)

        graphics = mock.Mock(
            returncode=0,
            stdout=(
                "<nvidia_smi_log><gpu><uuid>GPU-bbb</uuid><processes>"
                "<process_info><pid>5252</pid><type>G</type>"
                "<process_name>/opt/other-renderer</process_name>"
                "<used_memory>1 MiB</used_memory></process_info>"
                "</processes></gpu></nvidia_smi_log>"
            ),
            stderr="",
        )
        with mock.patch(
            "simworld_arena.launcher.subprocess.run", side_effect=[idle, graphics]
        ), self.assertRaisesRegex(RuntimeError, "active process IDs/types"):
            require_gpu_idle(2)

        reviewed_xorg = mock.Mock(
            returncode=0,
            stdout=(
                "<nvidia_smi_log><gpu><uuid>GPU-bbb</uuid><processes>"
                "<process_info><pid>2302</pid><type>G</type>"
                "<process_name>/usr/lib/xorg/Xorg</process_name>"
                "<used_memory>4 MiB</used_memory></process_info>"
                "</processes></gpu></nvidia_smi_log>"
            ),
            stderr="",
        )
        with mock.patch(
            "simworld_arena.launcher.subprocess.run", side_effect=[idle, reviewed_xorg]
        ), mock.patch(
            "simworld_arena.launcher.is_reviewed_idle_graphics_process", return_value=True
        ):
            self.assertEqual(require_gpu_idle(2)["index"], 2)

        busy_memory = mock.Mock(returncode=0, stdout="2, GPU-bbb, 2048, 0\n", stderr="")
        with mock.patch(
            "simworld_arena.launcher.subprocess.run",
            side_effect=[busy_memory, no_processes],
        ), self.assertRaisesRegex(RuntimeError, "idle limit"):
            require_gpu_idle(2)

        failed_query = mock.Mock(returncode=1, stdout="", stderr="driver error")
        with mock.patch(
            "simworld_arena.launcher.subprocess.run", return_value=failed_query
        ), self.assertRaisesRegex(RuntimeError, "audit failed"):
            require_gpu_idle(2)

    def test_reviewed_idle_graphics_process_is_exact_and_bounded(self):
        def reviewed(process_name, process_user, used_memory=4, *, binary_mode=0o100755):
            process_uid = 120 if process_user == "gdm" else 0
            process_stat = SimpleNamespace(st_uid=process_uid)
            binary_stat = SimpleNamespace(st_uid=0, st_mode=binary_mode)
            with mock.patch(
                "simworld_arena.launcher.Path.stat",
                side_effect=[process_stat, binary_stat],
            ), mock.patch(
                "simworld_arena.launcher.Path.is_file", return_value=True
            ), mock.patch(
                "simworld_arena.launcher.pwd.getpwuid",
                return_value=SimpleNamespace(pw_name=process_user),
            ):
                return is_reviewed_idle_graphics_process(
                    pid=2302,
                    process_type="G",
                    process_name=process_name,
                    used_memory_mib=used_memory,
                )

        self.assertTrue(reviewed("/usr/lib/xorg/Xorg", "root", 43))
        self.assertTrue(reviewed("/usr/bin/gnome-shell", "gdm", 15))
        self.assertFalse(reviewed("/usr/lib/xorg/Xorg", "gdm"))
        self.assertFalse(reviewed("/usr/bin/gnome-shell", "root"))
        self.assertFalse(reviewed("/usr/bin/gnome-shell", "gdm", 65))
        self.assertFalse(
            reviewed("/usr/bin/gnome-shell", "gdm", binary_mode=0o100775)
        )

    def test_children_start_in_owned_process_groups_and_cleanup_uses_killpg(self):
        registered = []
        child = mock.Mock(pid=43210)
        with mock.patch(
            "simworld_arena.launcher.subprocess.Popen", return_value=child
        ) as popen:
            self.assertIs(start_managed_process(["child"], registered, cwd="/tmp"), child)
        self.assertEqual(registered, [child])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

        child.poll.return_value = 0
        child.wait.return_value = 0
        with mock.patch(
            "simworld_arena.launcher._process_group_exists",
            side_effect=[True, False, False],
        ), mock.patch("simworld_arena.launcher.os.killpg") as killpg:
            terminate_managed_process_group(child)
        killpg.assert_called_once_with(child.pid, signal.SIGTERM)

        stubborn = mock.Mock(pid=43211)
        stubborn.poll.return_value = None
        stubborn.wait.side_effect = [subprocess.TimeoutExpired("child", 1), 0]
        with mock.patch(
            "simworld_arena.launcher._process_group_exists",
            side_effect=[True, True, False, False],
        ), mock.patch("simworld_arena.launcher.os.killpg") as killpg:
            terminate_managed_process_group(stubborn, term_timeout=1, kill_timeout=1)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(stubborn.pid, signal.SIGTERM),
                mock.call(stubborn.pid, signal.SIGKILL),
            ],
        )

        launcher_source = (
            Path(__file__).resolve().parents[1] / "simworld_arena" / "launcher.py"
        ).read_text()
        self.assertEqual(launcher_source.count("subprocess.Popen("), 1)
        self.assertIn("start_new_session=True", launcher_source)

    def test_owned_process_group_cleanup_terminates_a_real_local_child(self):
        registered = []
        child = start_managed_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            registered,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertEqual(os.getpgid(child.pid), child.pid)
            terminate_managed_process_group(child, term_timeout=2, kill_timeout=1)
            self.assertIsNotNone(child.poll())
            with self.assertRaises(ProcessLookupError):
                os.killpg(child.pid, 0)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)

    def test_staging_copy_excludes_untracked_and_rejects_tracked_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            server = repository / "simworld_studio_workspace" / "web" / "server"
            server.mkdir(parents=True)
            (server / "tracked.js").write_text("tracked\n")
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
            subprocess.run(["git", "add", "simworld_studio_workspace/web/server/tracked.js"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            (server / "untracked.js").write_text("must-not-copy\n")

            destination = Path(temporary) / "workspace"
            destination.mkdir()
            copied = STAGE_TOOL.copy_tracked_workspace(
                repository,
                destination,
                copy_paths=("web/server",),
            )
            self.assertEqual(copied, 1)
            self.assertEqual(
                (destination / "web" / "server" / "tracked.js").read_text(),
                "tracked\n",
            )
            self.assertFalse((destination / "web" / "server" / "untracked.js").exists())

            link = server / "tracked-link"
            link.symlink_to("tracked.js")
            subprocess.run(["git", "add", "simworld_studio_workspace/web/server/tracked-link"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "symlink fixture"], cwd=repository, check=True)
            second_destination = Path(temporary) / "workspace-with-link"
            second_destination.mkdir()
            with self.assertRaisesRegex(RuntimeError, "non-file entry"):
                STAGE_TOOL.copy_tracked_workspace(
                    repository,
                    second_destination,
                    copy_paths=("web/server",),
                )

    def test_dependency_tree_hashes_safe_links_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "node_modules"
            package = root / "package"
            binary = root / ".bin"
            package.mkdir(parents=True)
            binary.mkdir()
            (package / "cli.js").write_text("module.exports = 1;\n")
            (binary / "cli").symlink_to("../package/cli.js")
            original = sha256_dependency_tree(root)
            self.assertEqual(STAGE_TOOL.sha256_dependency_tree(root), original)
            (package / "cli.js").write_text("module.exports = 2;\n")
            self.assertNotEqual(sha256_dependency_tree(root), original)
            (binary / "escape").symlink_to("../../../outside")
            with self.assertRaisesRegex(RuntimeError, "escapes or is broken"):
                sha256_dependency_tree(root)

    def test_unrealcv_requires_descriptor_and_linux_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_root = root / "gym_citynav"
            project_root.mkdir()
            project = project_root / "gym_citynav.uproject"
            project.write_text("{}")
            self.assertFalse(has_unrealcv_plugin(root, project))

            plugin = project_root / "Plugins" / "UnrealCV"
            plugin.mkdir(parents=True)
            (plugin / "UnrealCV.uplugin").write_text("{}")
            self.assertFalse(has_unrealcv_plugin(root, project))
            binaries = plugin / "Binaries" / "Linux"
            binaries.mkdir(parents=True)
            (binaries / "libUnrealEditor-UnrealCV.so").write_bytes(b"binary")
            self.assertTrue(has_unrealcv_plugin(root, project))

    def test_launcher_disables_unreal_network_messaging(self):
        launcher_source = (
            Path(__file__).resolve().parents[1] / "simworld_arena" / "launcher.py"
        ).read_text()
        self.assertNotIn('"-Messaging"', launcher_source)
        self.assertIn('"-UDPMESSAGING_TRANSPORT_ENABLE=0"', launcher_source)
        self.assertIn("/Script/TcpMessaging.TcpMessagingSettings", launcher_source)
        self.assertIn("EnableTransport=False", launcher_source)

    def test_headless_nvidia_icd_is_pinned_to_egl(self):
        manifest = get_nvidia_headless_icd()
        payload = json.loads(manifest.read_text())
        self.assertEqual(
            payload["ICD"]["library_path"],
            "/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.0",
        )
        self.assertEqual(payload["ICD"]["api_version"], "1.4.325")

    def test_checked_in_manifest_matches_source(self):
        repository = Path(__file__).resolve().parents[2]
        manifest = json.loads(
            (repository / "packaging" / "simworld_arena" / "security-manifest.json").read_text()
        )
        self.assertEqual(
            frozenset(manifest["workspace_sha256"]),
            REQUIRED_WORKSPACE_SECURITY_PATHS,
        )
        for relative, expected in manifest["workspace_sha256"].items():
            source = repository / "simworld_studio_workspace" / relative
            self.assertEqual(sha256_file(source), expected, relative)

    def test_prepared_workspace_requires_security_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            server = workspace / "web" / "server"
            server.mkdir(parents=True)
            public = workspace / "web" / "public"
            public.mkdir()
            (workspace / ".studio_version").write_text("0.2.0\n")
            (workspace / "web" / "mcp.json").write_text('{"mcpServers": {}}\n')
            (workspace / "web" / "package-lock.json").write_text('{"lockfileVersion":3}\n')
            (public / "ue-player.html").write_text("<!doctype html><title>UE</title>\n")
            (server / "runtime-security.js").write_text("module.exports = {};\n")
            (server / "vista-runtime-broker.js").write_text("module.exports = {};\n")
            (server / "agent-sandbox.js").write_text("module.exports = {};\n")
            (server / "package-lock.json").write_text('{"lockfileVersion":3}\n')
            (server / "index.js").write_text(
                "requestLoopbackGuard; createModelGate; app.listen(PORT,STUDIO_HOST,()=>{});\n"
            )
            for relative in sorted(REQUIRED_WORKSPACE_SECURITY_PATHS):
                candidate = workspace / relative
                candidate.parent.mkdir(parents=True, exist_ok=True)
                if not candidate.exists():
                    candidate.write_text(f"reviewed fixture: {relative}\n")
            dist = workspace / "web" / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<!doctype html>\n")
            express = server / "node_modules" / "express"
            express.mkdir(parents=True)
            (express / "package.json").write_text('{"name":"express"}\n')
            self.assertEqual(
                STAGE_TOOL.sha256_server_source_tree(server),
                sha256_server_source_tree(server),
            )
            manifest = workspace / "security-manifest.json"
            manifest_hashes = {
                relative: sha256_file(workspace / relative)
                for relative in sorted(REQUIRED_WORKSPACE_SECURITY_PATHS)
            }
            manifest.write_text(
                json.dumps(
                    {
                        "schema": "vista-simworld-security-manifest/v1",
                        "workspace_sha256": manifest_hashes,
                    }
                )
            )
            (workspace.parent / "source-receipt.json").write_text(
                json.dumps(
                    {
                        "schema": "vista-simworld-staged-workspace/v1",
                        "source_copy": {
                            "policy": "git-archive-head-allowlist/v1",
                            "tracked_file_count": 7,
                        },
                        "lockfiles": {
                            "web/package-lock.json": sha256_file(
                                workspace / "web" / "package-lock.json"
                            ),
                            "web/server/package-lock.json": sha256_file(
                                server / "package-lock.json"
                            ),
                        },
                        "security_files": {
                            "index.js": sha256_file(server / "index.js"),
                            "runtime-security.js": sha256_file(server / "runtime-security.js"),
                            "vista-runtime-broker.js": sha256_file(
                                server / "vista-runtime-broker.js"
                            ),
                            "agent-sandbox.js": sha256_file(server / "agent-sandbox.js"),
                            "web/mcp.json": sha256_file(workspace / "web" / "mcp.json"),
                            "web/public/ue-player.html": sha256_file(
                                public / "ue-player.html"
                            ),
                        },
                        "artifacts": {
                            "web/dist/index.html": sha256_file(dist / "index.html"),
                            "web/dist/tree_sha256": sha256_tree(dist),
                            "web/server/node_modules/express/package.json": sha256_file(
                                express / "package.json"
                            ),
                            "web/server/source_tree_sha256": sha256_server_source_tree(
                                server
                            ),
                            "web/server/node_modules/tree_sha256": sha256_dependency_tree(
                                server / "node_modules"
                            ),
                        },
                        "launcher_validation": {
                            "validator": "simworld_arena.launcher.validate_prepared_workspace",
                            "launcher_sha256": sha256_file(
                                Path(__file__).resolve().parents[1]
                                / "simworld_arena"
                                / "launcher.py"
                            ),
                            "status": "passed",
                        },
                    }
                )
            )
            self.assertEqual(
                validate_prepared_workspace(workspace, manifest), workspace.resolve()
            )

            valid_manifest = json.loads(manifest.read_text())
            missing_manifest = json.loads(json.dumps(valid_manifest))
            missing_manifest["workspace_sha256"].pop("web/server/review-provider.js")
            manifest.write_text(json.dumps(missing_manifest))
            with self.assertRaisesRegex(RuntimeError, "invalid required path set"):
                validate_prepared_workspace(workspace, manifest)

            empty_manifest = json.loads(json.dumps(valid_manifest))
            empty_manifest["workspace_sha256"] = {}
            manifest.write_text(json.dumps(empty_manifest))
            with self.assertRaisesRegex(RuntimeError, "non-empty object"):
                validate_prepared_workspace(workspace, manifest)

            invalid_digest_manifest = json.loads(json.dumps(valid_manifest))
            invalid_digest_manifest["workspace_sha256"]["web/server/review-provider.js"] = "A" * 64
            manifest.write_text(json.dumps(invalid_digest_manifest))
            with self.assertRaisesRegex(RuntimeError, "invalid SHA-256 digest"):
                validate_prepared_workspace(workspace, manifest)

            unsafe_path_manifest = json.loads(json.dumps(valid_manifest))
            unsafe_path_manifest["workspace_sha256"]["../outside.js"] = (
                unsafe_path_manifest["workspace_sha256"].pop("web/server/review-provider.js")
            )
            manifest.write_text(json.dumps(unsafe_path_manifest))
            with self.assertRaisesRegex(RuntimeError, "unsafe workspace path"):
                validate_prepared_workspace(workspace, manifest)

            manifest.write_text(json.dumps(valid_manifest))

            frontend_dependencies = workspace / "web" / "node_modules"
            frontend_dependencies.mkdir()
            with self.assertRaisesRegex(RuntimeError, "frontend build dependencies"):
                validate_prepared_workspace(workspace, manifest)
            frontend_dependencies.rmdir()

            (server / "vista-runtime-broker.js").write_text("module.exports = {bad:true};\n")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                validate_prepared_workspace(workspace, manifest)
            (server / "vista-runtime-broker.js").write_text("module.exports = {};\n")

            (server / "unexpected-source.js").write_text("module.exports = {};\n")
            with self.assertRaisesRegex(RuntimeError, "source receipt is invalid"):
                validate_prepared_workspace(workspace, manifest)
            (server / "unexpected-source.js").unlink()

            (express / "package.json").write_text('{"name":"tampered"}\n')
            with self.assertRaisesRegex(RuntimeError, "source receipt is invalid"):
                validate_prepared_workspace(workspace, manifest)
            (express / "package.json").write_text('{"name":"express"}\n')

            (public / "ue-player.html").write_text("<script>tampered()</script>\n")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                validate_prepared_workspace(workspace, manifest)
            (public / "ue-player.html").write_text("<!doctype html><title>UE</title>\n")

            (workspace / "web" / "mcp.json").write_text(
                '{"mcpServers":{"simworld":{"command":"node"}}}\n'
            )
            with self.assertRaisesRegex(RuntimeError, "must contain no servers"):
                validate_prepared_workspace(workspace, manifest)
            (workspace / "web" / "mcp.json").write_text('{"mcpServers": {}}\n')

            (server / "index.js").write_text('app.listen(PORT,"0.0.0.0",()=>{});\n')
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                validate_prepared_workspace(workspace, manifest)

    def test_stock_cirrus_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            cirrus = Path(temporary) / "cirrus.js"
            cirrus.write_text("http.listen(httpPort);\n")
            with self.assertRaisesRegex(RuntimeError, "loopback patch"):
                validate_cirrus_loopback_patch(cirrus)
            patched = (
                f"// {CIRRUS_LOOPBACK_PATCH_MARKER}\n"
                "BindAddress; http.listen(httpPort, bindAddress); "
                "https.listen(httpsPort, bindAddress); ({ host: bindAddress });\n"
            )
            cirrus.write_text(patched)
            patched_sha256 = sha256_file(cirrus)
            cirrus.with_name("cirrus.js.vista-receipt.json").write_text(
                json.dumps(
                    {
                        "schema": "vista-cirrus-loopback-patch/v1",
                        "patch_version": CIRRUS_LOOPBACK_PATCH_MARKER,
                        "original_sha256": EXPECTED_ORIGINAL_CIRRUS_SHA256,
                        "patched_sha256": patched_sha256,
                    }
                )
            )
            with mock.patch(
                "simworld_arena.launcher.EXPECTED_PATCHED_CIRRUS_SHA256", patched_sha256
            ):
                validate_cirrus_loopback_patch(cirrus)

    def test_cirrus_config_has_loopback_bind_address(self):
        args = SimpleNamespace(cirrus_http_port=8585, cirrus_ws_port=8586, cirrus_sfu_port=8889)
        config = make_cirrus_config(args)
        self.assertEqual(config["BindAddress"], "127.0.0.1")
        self.assertEqual(json.loads(json.dumps(config)), config)

    def test_port_preflight_rejects_duplicates_and_occupied_ports(self):
        with self.assertRaisesRegex(RuntimeError, "unique port"):
            require_ports_free({"web": 3002, "mcp": 3002})
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError, "already listening"):
                require_ports_free({"test": port})
            require_loopback_listeners({"test": port})
        finally:
            listener.close()

    def test_listener_audit_rejects_wildcard_bind(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError, "not IPv4-loopback-only"):
                require_loopback_listeners({"test": port})
        finally:
            listener.close()

    def test_wait_for_port_aborts_when_child_exits(self):
        process = mock.Mock()
        process.poll.return_value = 17
        start = time.monotonic()

        with mock.patch("simworld_arena.launcher.socket.socket") as socket_factory:
            self.assertFalse(wait_for_port(65535, timeout=30, process=process))

        self.assertLess(time.monotonic() - start, 1)
        socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
    require_gpu_idle,
