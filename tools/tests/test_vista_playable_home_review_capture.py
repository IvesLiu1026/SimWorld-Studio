from __future__ import annotations

import argparse
import binascii
import copy
import dataclasses
import inspect
import json
import os
import pathlib
import stat
import struct
import tempfile
import unittest
import zlib
from unittest import mock


from tools.ue.vista_playable_home import capture_review_views as capture
from tools.worlds import playable_home as world_contract
from world_packs.vista_playable_home_r1.visual_profiles import (
    contract as visual_profile_contract,
)


ROOT = pathlib.Path(__file__).resolve().parents[2]
PACK = ROOT / "world_packs/vista_playable_home_r1"
R2_PROFILE_SOURCE = (
    PACK / "visual_profiles" / "realistic_interior_r2.json"
)
MOCK_NFS_SOURCE_SHA256 = "e" * 64


def mkdir_private(path: pathlib.Path, *, parents: bool = False) -> None:
    """Build a 0700 fixture even on NAS mounts with inheritable default ACLs."""

    path.mkdir(mode=0o700, parents=parents)
    path.chmod(0o700)


def transform(seed: int) -> dict:
    return {
        "location_cm": [float(seed * 10), float(seed * -5), 170.0],
        "rotation_deg": [-10.0, 0.0, float(seed * 15)],
        "scale": [1.0, 1.0, 1.0],
    }


def build_plan() -> dict:
    rooms = []
    for ordinal, (kind, room_id, camera_id) in enumerate(capture.FIXED_REVIEW_CAMERAS, start=1):
        rooms.append(
            {
                "kind": kind,
                "room_id": room_id,
                "review_cameras": [
                    {
                        "camera_id": camera_id,
                        "world_transform_cm": transform(ordinal),
                        "fov_deg": 65.0 + ordinal,
                    }
                ],
            }
        )
    return {
        "schema_version": capture.BUILD_PLAN_SCHEMA,
        "house": {
            "house_id": capture.EXPECTED_HOUSE_ID,
            "revision": capture.EXPECTED_REVISION,
        },
        "content_digest": "1" * 64,
        "rooms": rooms,
        "unreal": {"map_path": capture.EXPECTED_MAP_PATH},
    }


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def rgb_png(width: int, height: int, *, solid: bool = False, seed: int = 0) -> bytes:
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)
        for x in range(width):
            if solid:
                rgb = (0, 0, 0)
            else:
                rgb = (
                    (x * 31 + y * 7 + seed * 13) % 256,
                    (x * 11 + y * 43 + seed * 29) % 256,
                    (x * 17 + y * 23 + seed * 47) % 256,
                )
            scanlines.extend(rgb)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(bytes(scanlines)))
        + png_chunk(b"IEND", b"")
    )


class ReviewCameraPlanTests(unittest.TestCase):
    def test_fixed_plan_compiles_exact_six_camera_actor_tags(self) -> None:
        cameras = capture.compile_fixed_cameras(build_plan(), capture.EXPECTED_MAP_PATH)

        self.assertEqual([camera["room_kind"] for camera in cameras], [item[0] for item in capture.FIXED_REVIEW_CAMERAS])
        self.assertEqual(len(cameras), 6)
        self.assertTrue(all(camera["semantic_tag"].startswith("VistaSemanticId=home.r1/room.") for camera in cameras))
        self.assertEqual(len({camera["semantic_id"] for camera in cameras}), 6)
        self.assertEqual([camera["ordinal"] for camera in cameras], list(range(1, 7)))

    def test_room_camera_and_map_drift_fail_closed(self) -> None:
        invalid = build_plan()
        invalid["rooms"].pop()
        with self.assertRaisesRegex(capture.ReviewCaptureError, "ROOM_SET_INVALID"):
            capture.compile_fixed_cameras(invalid, capture.EXPECTED_MAP_PATH)

        invalid = build_plan()
        invalid["rooms"][0]["review_cameras"].append(copy.deepcopy(invalid["rooms"][0]["review_cameras"][0]))
        with self.assertRaisesRegex(capture.ReviewCaptureError, "CAMERA_SET_INVALID"):
            capture.compile_fixed_cameras(invalid, capture.EXPECTED_MAP_PATH)

        with self.assertRaisesRegex(capture.ReviewCaptureError, "MAP_MISMATCH"):
            capture.compile_fixed_cameras(build_plan(), "/Game/Caller/ArbitraryMap")

    def test_cli_and_editor_command_have_no_caller_python_surface(self) -> None:
        parser = capture.build_parser()
        destinations = {action.dest for action in parser._actions}
        self.assertFalse({"script", "python", "python_script", "execute_python_script"} & destinations)
        self.assertTrue(
            {
                "capture_profile",
                "visual_profile",
                "visual_profile_sha256",
                "scratch_policy_root",
                "scratch_parent",
            }
            <= destinations
        )
        runbook = (
            ROOT / "tools/ue/vista_playable_home/README.md"
        ).read_text(encoding="utf-8")
        for option in (
            "--capture-profile realistic_interior_r2",
            "--visual-profile ",
            "--visual-profile-sha256 ",
            "--scratch-policy-root ",
            "--scratch-parent ",
        ):
            self.assertIn(option, runbook)

        root = pathlib.Path("/tmp/vista-review-fixture")
        inputs = capture.CaptureInputs(
            attempt_root=root,
            project=root / "project" / capture.EXPECTED_PROJECT_NAME,
            project_sha256="2" * 64,
            map_asset=root / capture.EXPECTED_MAP_ASSET_RELATIVE,
            map_asset_sha256="3" * 64,
            build_plan=root / "contracts" / "build-plan.json",
            build_plan_sha256="4" * 64,
            plan=build_plan(),
            build_result=root / capture.EXPECTED_BUILD_RESULT_NAME,
            build_result_sha256="5" * 64,
            map_path=capture.EXPECTED_MAP_PATH,
            unreal_editor=pathlib.Path("/opt/Unreal/Engine/Binaries/Linux/UnrealEditor"),
            unreal_editor_sha256="6" * 64,
            output_dir=root / "review-cameras" / "attempt-01",
            display=":117",
            graphics_adapter=0,
            timeout_seconds=300,
            script=pathlib.Path(capture.__file__).resolve(),
            script_sha256="7" * 64,
            nvidia_icd_sha256="8" * 64,
            ddc_seed=None,
            ddc_seed_tree_sha256=None,
            cameras=tuple(capture.compile_fixed_cameras(build_plan(), capture.EXPECTED_MAP_PATH)),
        )
        command = capture.build_editor_command(inputs)
        script_options = [value for value in command if value.startswith("-ExecutePythonScript=")]
        self.assertEqual(script_options, [f"-ExecutePythonScript={pathlib.Path(capture.__file__).resolve()}"])
        self.assertNotIn("-game", command)
        self.assertIn("-Windowed", command)
        self.assertIn("-ResX=1280", command)
        self.assertIn("-ResY=720", command)
        execution = capture.build_execution(inputs)
        self.assertEqual(execution["schema_version"], capture.EXECUTION_SCHEMA)
        self.assertEqual(
            set(execution["capture"]),
            {"width", "height", "room_kinds", "cameras"},
        )
        self.assertNotIn("visual_profile", execution)
        self.assertNotIn("scratch", execution)
        self.assertNotIn("graphics_adapter", execution["engine"])
        self.assertTrue(
            execution["policy"]["native_png_uses_private_local_scratch"]
        )
        self.assertNotIn(
            "native_png_uses_private_nas_retained_evidence",
            execution["policy"],
        )
        stable_r1 = dataclasses.replace(
            inputs,
            script=pathlib.Path("/stable/capture_review_views.py"),
        )
        self.assertEqual(
            capture.sha256_bytes(
                capture.canonical_json(capture.build_execution(stable_r1))
            ),
            "5a6c2e503b579cd072d7c4f92e498d9fda0b85b11eeff04128e86a084de8dc1d",
        )

    def test_worker_uses_post_tick_fixed_actor_capture(self) -> None:
        source = inspect.getsource(capture._unreal_worker)
        self.assertIn("get_all_level_actors", source)
        self.assertIn("unreal.CameraActor", source)
        self.assertIn("semantic_tag", source)
        self.assertIn("register_slate_post_tick_callback", source)
        self.assertIn("set_keep_python_script_alive(True)", source)
        self.assertIn("HighResShot", source)
        self.assertIn("execute_console_command", source)
        self.assertEqual(source.count("execute_console_command(world, command)"), 1)
        self.assertIn('state["shot_requested"]', source)
        self.assertIn('selected_camera = worker_execution["camera"]', source)
        self.assertIn('image_path = Path(worker_execution["scratch_png"])', source)
        self.assertNotIn('Path(execution["output_root"]) / camera["relative_path"]', source)
        self.assertNotIn("AutomationLibrary.take_high_res_screenshot(WIDTH", source)
        self.assertNotIn("exec(", source)

        lifecycle_source = inspect.getsource(capture.run_editor)
        self.assertIn("finally:", lifecycle_source)
        self.assertIn("_terminate_owned(process)", lifecycle_source)


class ReviewPngValidationTests(unittest.TestCase):
    def test_rgb_png_is_decoded_and_proven_nonblank(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "room.png"
            path.write_bytes(rgb_png(16, 16))
            result = capture.inspect_png(path, expected_width=16, expected_height=16)

        self.assertEqual((result.width, result.height), (16, 16))
        self.assertGreaterEqual(result.unique_rgb_count_capped, 16)
        self.assertGreaterEqual(result.luma_max - result.luma_min, 8)
        self.assertTrue(result.nonblank)

    def test_blank_wrong_dimensions_and_crc_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            blank = root / "blank.png"
            blank.write_bytes(rgb_png(16, 16, solid=True))
            with self.assertRaisesRegex(capture.ReviewCaptureError, "PNG_BLANK"):
                capture.inspect_png(blank, expected_width=16, expected_height=16)

            dimensions = root / "dimensions.png"
            dimensions.write_bytes(rgb_png(8, 8))
            with self.assertRaisesRegex(capture.ReviewCaptureError, "PNG_DIMENSIONS"):
                capture.inspect_png(dimensions, expected_width=16, expected_height=16)

            corrupt = root / "corrupt.png"
            raw = bytearray(rgb_png(16, 16))
            raw[-5] ^= 1
            corrupt.write_bytes(raw)
            with self.assertRaisesRegex(capture.ReviewCaptureError, "PNG chunk CRC differs"):
                capture.inspect_png(corrupt, expected_width=16, expected_height=16)


class MountInfoParserTests(unittest.TestCase):
    @staticmethod
    def mountinfo(
        filesystem_type: bytes = b"nfs4",
        *,
        source: bytes = b"server:/export\\040with-space",
    ) -> bytes:
        return (
            b"680 42 0:99 / /mnt/NAS2 rw,relatime shared:7 - "
            + filesystem_type
            + b" "
            + source
            + b" rw,vers=4.2\n"
        )

    def test_strict_mountinfo_decodes_escapes_and_closes_filesystem_policy(
        self,
    ) -> None:
        expected_source = capture.sha256_bytes(b"server:/export with-space")
        for filesystem_type in (b"nfs", b"nfs4", b"ext4"):
            with self.subTest(filesystem_type=filesystem_type):
                identity = capture._parse_mountinfo_identity(
                    self.mountinfo(filesystem_type),
                    680,
                )
                self.assertEqual(
                    identity,
                    (680, filesystem_type.decode("ascii"), expected_source),
                )
                if filesystem_type == b"ext4":
                    with self.assertRaisesRegex(
                        capture.ReviewCaptureError,
                        "STORAGE_INVALID",
                    ):
                        capture._require_r2_nas_mount(identity)
                else:
                    capture._require_r2_nas_mount(identity)

    def test_strict_mountinfo_rejects_duplicate_and_malformed_records(self) -> None:
        valid = self.mountinfo()
        malformed = {
            "duplicate": valid + valid,
            "separator_before_fixed_fields": (
                b"680 42 0:99 / /mnt/NAS2 - nfs4 server:/export rw\n"
            ),
            "extra_post_field": valid.rstrip(b"\n") + b" extra\n",
            "invalid_parent_id": valid.replace(b"680 42 ", b"680 zero "),
            "invalid_major_minor": valid.replace(b"0:99", b"0:x"),
            "relative_root": valid.replace(b"0:99 / /mnt", b"0:99 relative /mnt"),
            "relative_mount_point": valid.replace(
                b"/ /mnt/NAS2", b"/ relative"
            ),
            "invalid_escape": valid.replace(b"\\040", b"\\04x"),
            "unrecognized_optional": valid.replace(
                b"shared:7 -", b"future:7 -"
            ),
            "empty_option": valid.replace(b"rw,relatime", b"rw,,relatime"),
        }
        for label, raw in malformed.items():
            with self.subTest(label=label), self.assertRaises(
                capture.ReviewCaptureError
            ):
                capture._parse_mountinfo_identity(raw, 680)


class ReviewCaptureInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._mount_identity_patcher = mock.patch.object(
            capture,
            "_fd_mount_identity",
            side_effect=self._mock_nfs_mount_identity,
        )
        self._mount_identity_patcher.start()
        self._r2_allowed_temp = tempfile.TemporaryDirectory()
        self.r2_allowed_run_root = pathlib.Path(
            self._r2_allowed_temp.name
        ).resolve()
        self.r2_allowed_run_root.chmod(0o700)

    def tearDown(self) -> None:
        self._mount_identity_patcher.stop()
        self._r2_allowed_temp.cleanup()

    @staticmethod
    def _mock_nfs_mount_identity(descriptor: int) -> tuple[int, str, str]:
        return (
            capture._fd_mount_id(descriptor),
            "nfs4",
            MOCK_NFS_SOURCE_SHA256,
        )

    def make_args(self, root: pathlib.Path) -> argparse.Namespace:
        project_dir = root / "project"
        contracts_dir = root / "contracts"
        review_dir = root / "review-cameras"
        project_dir.mkdir()
        contracts_dir.mkdir()
        review_dir.mkdir()
        project = project_dir / capture.EXPECTED_PROJECT_NAME
        project.write_text(
            json.dumps(
                {
                    "Plugins": [
                        {"Name": "VistaPlayableHome", "Enabled": True},
                        {"Name": "PythonScriptPlugin", "Enabled": True},
                        {"Name": "EditorScriptingUtilities", "Enabled": True},
                    ]
                }
            ),
            encoding="utf-8",
        )
        plan_path = contracts_dir / "build-plan.json"
        plan_path.write_bytes(capture.canonical_json(build_plan()))
        map_asset = root / capture.EXPECTED_MAP_ASSET_RELATIVE
        map_asset.parent.mkdir(parents=True)
        map_asset.write_bytes(b"synthetic umap")
        (root / capture.EXPECTED_BUILD_RESULT_NAME).write_bytes(
            capture.canonical_json(
                {
                    "schema_version": capture.EXPECTED_BUILD_RESULT_SCHEMA,
                    "status": "accepted_candidate",
                    "attempt_root": str(root),
                    "revision": capture.EXPECTED_REVISION,
                    "map_path": capture.EXPECTED_MAP_PATH,
                }
            )
        )
        engine_dir = root / "engine"
        engine_dir.mkdir()
        editor = engine_dir / f"UnrealEditor-{root.name}"
        editor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        editor.chmod(editor.stat().st_mode | stat.S_IXUSR)
        self.addCleanup(lambda: editor.unlink(missing_ok=True))
        return argparse.Namespace(
            attempt_root=str(root),
            project=str(project),
            build_plan=str(plan_path),
            build_plan_sha256=capture.sha256_file(plan_path),
            map_path=capture.EXPECTED_MAP_PATH,
            unreal_editor=str(editor),
            output_dir=str(review_dir / "attempt-01"),
            display=":117",
            graphics_adapter=0,
            timeout_seconds=300,
            ddc_seed=None,
            ddc_seed_tree_sha256=None,
            apply=False,
        )

    def make_valid_inputs(self, root: pathlib.Path) -> capture.CaptureInputs:
        args = self.make_args(root)
        fake_editor = pathlib.Path(args.unreal_editor)
        fixed_editor = fake_editor.with_name("UnrealEditor")
        fake_editor.rename(fixed_editor)
        args.unreal_editor = str(fixed_editor)
        return capture.validate_inputs(args)

    def make_r2_args(self, root: pathlib.Path) -> argparse.Namespace:
        args = self.make_args(root)
        plan = world_contract.compile_build_plan(
            world_contract.load_json(PACK / "house.json"),
            world_contract.load_events(PACK / "events"),
        )
        plan_path = pathlib.Path(args.build_plan)
        plan_path.write_bytes(capture.canonical_json(plan))
        args.build_plan_sha256 = capture.sha256_file(plan_path)
        profile_path = root / capture.EXPECTED_VISUAL_PROFILE_RELATIVE
        profile_path.write_bytes(R2_PROFILE_SOURCE.read_bytes())
        profile = visual_profile_contract.load_json(profile_path)
        profile_sha256 = capture.sha256_file(profile_path)
        build_result_path = root / capture.EXPECTED_BUILD_RESULT_NAME
        result = json.loads(build_result_path.read_text(encoding="utf-8"))
        result.update(
            {
                "visual_profile_id": capture.R2_CAPTURE_PROFILE,
                "visual_profile_sha256": profile_sha256,
                "visual_profile_content_digest": profile["content_digest"],
                "renderer_profile_request_sha256": "7" * 64,
                "renderer_profile_request_content_digest": "8" * 64,
                "renderer_runtime_observation": "pending",
                "base_scene_receipt_sha256": "9" * 64,
                "presentation_import_receipt_sha256": "a" * 64,
                "presentation_scene_receipt_sha256": "b" * 64,
                "presentation_manifest_sha256": "c" * 64,
                "presentation_artifact_receipt_sha256": "d" * 64,
                "presentation_bundle_count": 3,
                "presentation_collision_policy": (
                    "presentation_no_collision_use_hidden_r1_proxies"
                ),
                "presentation_ue_import_observation": "verified_by_commandlet",
                "presentation_runtime_play_proof": "pending",
            }
        )
        build_result_path.write_bytes(capture.canonical_json(result))
        args.capture_profile = capture.R2_CAPTURE_PROFILE
        args.visual_profile = str(profile_path)
        args.visual_profile_sha256 = profile_sha256
        args.display = capture.R2_DISPLAY
        scratch_parent = self.r2_allowed_run_root / f"run-{root.name}"
        mkdir_private(scratch_parent)
        args.scratch_policy_root = str(self.r2_allowed_run_root)
        args.scratch_parent = str(scratch_parent)
        return args

    def test_inputs_require_real_unreal_name_and_fresh_append_only_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_args(root)
            with self.assertRaisesRegex(capture.ReviewCaptureError, "engine executable must be UnrealEditor"):
                capture.validate_inputs(args)

            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            inputs = capture.validate_inputs(args)
            self.assertEqual(len(inputs.cameras), 6)

            pathlib.Path(args.output_dir).mkdir()
            with self.assertRaisesRegex(capture.ReviewCaptureError, "OUTPUT_EXISTS"):
                capture.validate_inputs(args)

    def test_build_plan_sha_and_local_display_are_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))

            args.build_plan_sha256 = "0" * 64
            with self.assertRaisesRegex(capture.ReviewCaptureError, "PIN_MISMATCH"):
                capture.validate_inputs(args)

            args.build_plan_sha256 = capture.sha256_file(pathlib.Path(args.build_plan))
            args.display = "remote.example:0"
            with self.assertRaisesRegex(capture.ReviewCaptureError, "DISPLAY_INVALID"):
                capture.validate_inputs(args)

    def test_r2_profile_is_sha_bound_six_shot_1080p_and_adapter_zero(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_r2_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))

            inputs = capture.validate_inputs(args)
            execution = capture.build_execution(inputs)
            command = capture.build_editor_command(inputs)

            self.assertEqual(inputs.capture_profile, capture.R2_CAPTURE_PROFILE)
            self.assertEqual(
                tuple(camera["camera_id"] for camera in inputs.cameras),
                capture.R2_ORDERED_SHOT_IDS,
            )
            self.assertEqual(execution["schema_version"], capture.R2_EXECUTION_SCHEMA)
            self.assertEqual(execution["capture"]["shot_ids"], list(capture.R2_ORDERED_SHOT_IDS))
            self.assertEqual(
                (execution["capture"]["width"], execution["capture"]["height"]),
                (1920, 1080),
            )
            self.assertEqual(execution["engine"]["graphics_adapter"], 0)
            self.assertEqual(execution["engine"]["display"], ":119")
            self.assertEqual(
                execution["scratch"]["storage_class"],
                "private_nas_retained_evidence",
            )
            self.assertEqual(
                execution["scratch"]["lifecycle"],
                capture.R2_SCRATCH_LIFECYCLE,
            )
            self.assertEqual(
                execution["scratch"]["cleanup_policy"],
                capture.R2_SCRATCH_CLEANUP_POLICY,
            )
            self.assertFalse(
                execution["scratch"][
                    "receipt_discloses_scratch_absolute_path"
                ]
            )
            self.assertNotIn(
                "receipt_discloses_absolute_path",
                execution["scratch"],
            )
            self.assertNotIn(
                args.scratch_parent,
                capture.canonical_json(execution).decode("utf-8"),
            )
            self.assertTrue(
                execution["policy"][
                    "native_png_uses_private_nas_retained_evidence"
                ]
            )
            self.assertTrue(execution["policy"]["scratch_retained_append_only"])
            self.assertTrue(
                execution["policy"]["scratch_cleanup_descriptor_close_only"]
            )
            self.assertNotIn(
                "native_png_uses_private_local_scratch",
                execution["policy"],
            )
            self.assertEqual(
                execution["visual_profile"]["sha256"],
                args.visual_profile_sha256,
            )
            self.assertEqual(execution["capture"]["runtime_observation_status"], "pending")
            self.assertIn("-ResX=1920", command)
            self.assertIn("-ResY=1080", command)
            self.assertIn("-graphicsadapter=0", command)
            worker_manifest = (
                inputs.output_dir
                / capture.WORKERS_DIR
                / "01"
                / capture.EXECUTION_FILE
            )
            environment = capture.build_editor_environment(
                inputs,
                worker_manifest,
                "5" * 64,
            )
            self.assertEqual(environment["HOME"], str(inputs.output_dir / "ue-user"))
            self.assertEqual(environment["TMPDIR"], str(inputs.output_dir / "tmp"))
            self.assertEqual(environment["TMP"], str(inputs.output_dir / "tmp"))
            self.assertEqual(environment["TEMP"], str(inputs.output_dir / "tmp"))
            self.assertEqual(
                environment["XDG_DATA_HOME"],
                str(inputs.output_dir / "xdg-data"),
            )

            args.graphics_adapter = 1
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "pinned to graphics adapter 0",
            ):
                capture.validate_inputs(args)

            args.graphics_adapter = 0
            args.display = ":118"
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "pinned to DISPLAY :119",
            ):
                capture.validate_inputs(args)

            execution_raw = capture.canonical_json(execution)
            capture._prepare_output(inputs, execution_raw)
            self.assertEqual(
                stat.S_IMODE((inputs.output_dir / "tmp").stat().st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE((inputs.output_dir / "xdg-data").stat().st_mode),
                0o700,
            )

    def test_r2_profile_pair_location_order_and_build_binding_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_r2_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))

            profile = visual_profile_contract.load_json(args.visual_profile)
            reordered = copy.deepcopy(profile)
            reordered["review_shots"][0], reordered["review_shots"][1] = (
                reordered["review_shots"][1],
                reordered["review_shots"][0],
            )
            reordered = visual_profile_contract.seal_document(reordered)
            plan = world_contract.compile_build_plan(
                world_contract.load_json(PACK / "house.json"),
                world_contract.load_events(PACK / "events"),
            )
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "exact ordered six-shot",
            ):
                capture._compile_r2_capture_cameras(
                    reordered,
                    plan,
                    capture.EXPECTED_MAP_PATH,
                )

            args.visual_profile_sha256 = None
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "must be supplied together",
            ):
                capture.validate_inputs(args)

            args.visual_profile_sha256 = capture.sha256_file(
                pathlib.Path(args.visual_profile)
            )
            result_path = root / capture.EXPECTED_BUILD_RESULT_NAME
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["visual_profile_sha256"] = "0" * 64
            result_path.write_bytes(capture.canonical_json(result))
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "r2 visual-profile binding differs",
            ):
                capture.validate_inputs(args)

    def test_r2_scratch_parent_is_required_closed_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_r2_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            valid_parent = pathlib.Path(args.scratch_parent)

            args.scratch_policy_root = None
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "requires --scratch-policy-root and --scratch-parent",
            ):
                capture.validate_inputs(args)

            args.scratch_policy_root = str(self.r2_allowed_run_root)
            args.scratch_parent = None
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "requires --scratch-policy-root and --scratch-parent",
            ):
                capture.validate_inputs(args)

            args.scratch_parent = str(valid_parent)
            valid_parent.chmod(0o755)
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "mode 0700",
            ):
                capture.validate_inputs(args)
            valid_parent.chmod(0o700)

            link = self.r2_allowed_run_root / "linked-scratch"
            link.symlink_to(valid_parent, target_is_directory=True)
            args.scratch_parent = str(link)
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "SYMLINK_REJECTED",
            ):
                capture.validate_inputs(args)

            args.scratch_parent = str(
                valid_parent / ".." / "escaped-scratch"
            )
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "absolute and normalized",
            ):
                capture.validate_inputs(args)

            args.scratch_parent = "/tmp"
            with self.assertRaises(capture.ReviewCaptureError):
                capture.validate_inputs(args)

            args.scratch_parent = os.environ["HOME"]
            with self.assertRaises(capture.ReviewCaptureError):
                capture.validate_inputs(args)

            r1_root = root / "r1"
            r1_root.mkdir()
            r1_args = self.make_args(r1_root)
            r1_args.scratch_policy_root = str(self.r2_allowed_run_root)
            r1_args.scratch_parent = str(valid_parent)
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "fixed r1 capture does not accept",
            ):
                capture.validate_inputs(r1_args)

    def test_r2_policy_accepts_mapped_nas_owner_but_rejects_mount_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_r2_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))

            with mock.patch.object(os, "geteuid", return_value=987654321):
                inputs = capture.validate_inputs(args)
            execution = capture.build_execution(inputs)
            self.assertEqual(
                execution["scratch"]["mapped_owner_uid"],
                self.r2_allowed_run_root.stat().st_uid,
            )
            self.assertEqual(
                execution["scratch"]["policy_root_mount_id"],
                execution["scratch"]["parent_mount_id"],
            )
            self.assertEqual(execution["scratch"]["filesystem_type"], "nfs4")
            self.assertEqual(
                execution["scratch"]["mount_source_sha256"],
                MOCK_NFS_SOURCE_SHA256,
            )

            parent_identity = (
                pathlib.Path(args.scratch_parent).stat().st_dev,
                pathlib.Path(args.scratch_parent).stat().st_ino,
            )

            def nested_mount(descriptor: int) -> tuple[int, str, str]:
                metadata = os.fstat(descriptor)
                observed = self._mock_nfs_mount_identity(descriptor)
                if (metadata.st_dev, metadata.st_ino) == parent_identity:
                    return observed[0] + 1, observed[1], observed[2]
                return observed

            with (
                mock.patch.object(
                    capture,
                    "_fd_mount_identity",
                    side_effect=nested_mount,
                ),
                self.assertRaisesRegex(
                    capture.ReviewCaptureError,
                    "MOUNT_MISMATCH",
                ),
            ):
                capture.validate_inputs(args)

            with (
                mock.patch.object(
                    capture,
                    "_fd_mount_identity",
                    side_effect=lambda descriptor: (
                        capture._fd_mount_id(descriptor),
                        "ext4",
                        "f" * 64,
                    ),
                ),
                self.assertRaisesRegex(
                    capture.ReviewCaptureError,
                    "STORAGE_INVALID",
                ),
            ):
                capture.validate_inputs(args)

    def test_r2_scratch_and_attempt_trees_are_disjoint_both_ways(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            policy = workspace / "policy"
            mkdir_private(policy)
            parent = policy / "scratch"
            mkdir_private(parent)
            nested_attempt = parent / "attempt"
            mkdir_private(nested_attempt)
            args = self.make_r2_args(nested_attempt)
            args.scratch_policy_root = str(policy)
            args.scratch_parent = str(parent)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "trees must be disjoint",
            ):
                capture.validate_inputs(args)

        with tempfile.TemporaryDirectory() as directory:
            attempt = pathlib.Path(directory).resolve()
            args = self.make_r2_args(attempt)
            nested_policy = attempt / "scratch-policy"
            mkdir_private(nested_policy)
            nested_parent = nested_policy / "scratch"
            mkdir_private(nested_parent)
            args.scratch_policy_root = str(nested_policy)
            args.scratch_parent = str(nested_parent)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            with self.assertRaisesRegex(
                capture.ReviewCaptureError,
                "trees must be disjoint",
            ):
                capture.validate_inputs(args)

    def test_r2_close_is_idempotent_and_retains_append_only_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_r2_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            inputs = capture.validate_inputs(args)
            protected = inputs.scratch_parent / "protected"
            mkdir_private(protected)
            (protected / "marker").write_bytes(b"keep")

            first = capture._create_scratch_root(inputs)
            second = capture._create_scratch_root(inputs)
            self.assertNotEqual(first.path, second.path)
            self.assertNotEqual((first.device, first.inode), (second.device, second.inode))
            self.assertEqual(stat.S_IMODE(first.path.stat().st_mode), 0o700)
            self.assertEqual(first.parent, inputs.scratch_parent)
            marker = first.path / "retained-marker"
            marker.write_bytes(b"retain")
            owned_child_fd = first.child_fd
            self.assertIsNotNone(owned_child_fd)
            assert owned_child_fd is not None

            capture._remove_scratch_root(first)
            self.assertTrue(first.path.is_dir())
            self.assertEqual(marker.read_bytes(), b"retain")
            self.assertTrue(first.closed)
            self.assertIsNone(first.child_fd)
            self.assertIsNone(first.parent_fd)
            self.assertIsNone(first.policy_root_fd)
            self.assertEqual(first.worker_fds, {})
            probe = os.open("/dev/null", os.O_RDONLY)
            if probe != owned_child_fd:
                os.dup2(probe, owned_child_fd)
                os.close(probe)
            capture._remove_scratch_root(first)
            os.fstat(owned_child_fd)
            os.close(owned_child_fd)
            self.assertTrue(second.path.is_dir())
            self.assertTrue((protected / "marker").is_file())

            moved = second.parent / f"moved-{second.path.name}"
            second.path.rename(moved)
            mkdir_private(second.path)
            (second.path / "replacement").write_bytes(b"do-not-delete")
            capture._remove_scratch_root(second)
            self.assertTrue((second.path / "replacement").is_file())
            self.assertTrue(moved.is_dir())
            self.assertTrue((protected / "marker").is_file())

    def test_r2_child_fchmod_overrides_inherited_default_acl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_r2_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            inputs = capture.validate_inputs(args)
            self.addCleanup(inputs.close)
            authority = inputs.scratch_authority
            self.assertIsNotNone(authority)
            assert authority is not None
            real_mkdir = os.mkdir
            inherited_modes: list[int] = []

            def inherit_permissive_default_acl(
                path: os.PathLike[str] | str,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> None:
                real_mkdir(path, mode, dir_fd=dir_fd)
                if (
                    isinstance(path, str)
                    and path.startswith(capture.R2_SCRATCH_PREFIX)
                    and dir_fd == authority.parent_fd
                ):
                    os.chmod(path, 0o777, dir_fd=dir_fd)
                    inherited_modes.append(
                        stat.S_IMODE(
                            os.stat(
                                path,
                                dir_fd=dir_fd,
                                follow_symlinks=False,
                            ).st_mode
                        )
                    )

            with mock.patch.object(
                os,
                "mkdir",
                side_effect=inherit_permissive_default_acl,
            ):
                ownership = capture._create_scratch_root(inputs)
            try:
                self.assertEqual(inherited_modes, [0o777])
                self.assertIsNotNone(ownership.child_fd)
                assert ownership.child_fd is not None
                self.assertEqual(
                    stat.S_IMODE(os.fstat(ownership.child_fd).st_mode),
                    0o700,
                )
                self.assertEqual(stat.S_IMODE(ownership.path.stat().st_mode), 0o700)
            finally:
                capture._remove_scratch_root(ownership)
            self.assertTrue(ownership.path.is_dir())

    def test_r2_policy_root_swap_is_rejected_after_relative_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            attempt = workspace / "attempt"
            attempt.mkdir()
            args = self.make_r2_args(attempt)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            policy = workspace / "policy"
            mkdir_private(policy)
            parent = policy / "scratch"
            mkdir_private(parent)
            args.scratch_policy_root = str(policy)
            args.scratch_parent = str(parent)
            moved = workspace / "moved-policy"
            real_open_relative = capture._open_relative_directory_fd
            swapped = False

            def swap_policy_after_root_open(
                root_fd: int,
                parts: tuple[str, ...],
                label: str,
            ) -> tuple[int, os.stat_result]:
                nonlocal swapped
                if not swapped and label == "r2 scratch parent":
                    swapped = True
                    policy.rename(moved)
                    mkdir_private(policy)
                    replacement = policy / "scratch"
                    mkdir_private(replacement)
                    (replacement / "replacement").write_bytes(b"do-not-trust")
                return real_open_relative(root_fd, parts, label)

            with (
                mock.patch.object(
                    capture,
                    "_open_relative_directory_fd",
                    side_effect=swap_policy_after_root_open,
                ),
                self.assertRaisesRegex(
                    capture.ReviewCaptureError,
                    "AUTHORITY_MISMATCH",
                ),
            ):
                capture.validate_inputs(args)
            self.assertTrue(swapped)
            self.assertEqual(
                (policy / "scratch" / "replacement").read_bytes(),
                b"do-not-trust",
            )
            self.assertTrue((moved / "scratch").is_dir())

    def test_r2_worker_path_replacement_is_not_host_acceptance_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_r2_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            inputs = capture.validate_inputs(args)
            execution_raw = capture.canonical_json(capture.build_execution(inputs))
            execution_sha = capture.sha256_bytes(execution_raw)
            capture._prepare_output(inputs, execution_raw)
            ownership = capture._create_scratch_root(inputs)
            try:
                worker = capture._prepare_worker_runs(
                    inputs,
                    execution_sha,
                    ownership,
                )[0]
                self.write_fake_worker_success(inputs, worker, seed=3)
                ue_result, _ = capture._load_worker_result(inputs, worker)
                moved = ownership.path / "moved-worker-01"
                worker.scratch_dir.rename(moved)
                mkdir_private(worker.scratch_dir)
                (worker.scratch_dir / "capture.png").write_bytes(b"replacement")
                with self.assertRaisesRegex(
                    capture.ReviewCaptureError,
                    "OWNERSHIP_MISMATCH",
                ):
                    capture._accept_worker_png(inputs, worker, ue_result)
                self.assertEqual(
                    (worker.scratch_dir / "capture.png").read_bytes(),
                    b"replacement",
                )
                self.assertTrue((moved / "capture.png").is_file())
            finally:
                capture._remove_scratch_root(ownership)
            self.assertTrue(ownership.path.is_dir())

    def test_r2_create_failures_retain_partial_child_and_close_opened_fd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_r2_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            inputs = capture.validate_inputs(args)
            self.addCleanup(inputs.close)
            authority = inputs.scratch_authority
            self.assertIsNotNone(authority)
            assert authority is not None
            real_open = os.open
            failed_open = False

            def reject_first_child_open(
                path: os.PathLike[str] | str,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal failed_open
                if (
                    not failed_open
                    and isinstance(path, str)
                    and path.startswith(capture.R2_SCRATCH_PREFIX)
                    and dir_fd == authority.parent_fd
                ):
                    failed_open = True
                    raise OSError("injected child open failure")
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with (
                mock.patch.object(os, "open", side_effect=reject_first_child_open),
                self.assertRaisesRegex(
                    capture.ReviewCaptureError,
                    "append-only partial evidence was retained",
                ),
            ):
                capture._create_scratch_root(inputs)
            self.assertTrue(failed_open)
            retained = [
                child
                for child in inputs.scratch_parent.iterdir()
                if child.name.startswith(capture.R2_SCRATCH_PREFIX)
            ]
            self.assertEqual(len(retained), 1)
            self.assertTrue(retained[0].is_dir())

            real_fsync = os.fsync
            failed_fsync = False
            opened_child_fds: list[int] = []

            def track_child_open(
                path: os.PathLike[str] | str,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                if (
                    isinstance(path, str)
                    and path.startswith(capture.R2_SCRATCH_PREFIX)
                    and dir_fd == authority.parent_fd
                ):
                    opened_child_fds.append(descriptor)
                return descriptor

            def reject_first_parent_fsync(descriptor: int) -> None:
                nonlocal failed_fsync
                if descriptor == authority.parent_fd and not failed_fsync:
                    failed_fsync = True
                    raise OSError("injected parent fsync failure")
                real_fsync(descriptor)

            with (
                mock.patch.object(os, "open", side_effect=track_child_open),
                mock.patch.object(os, "fsync", side_effect=reject_first_parent_fsync),
                self.assertRaisesRegex(
                    capture.ReviewCaptureError,
                    "append-only partial evidence was retained",
                ),
            ):
                capture._create_scratch_root(inputs)
            self.assertTrue(failed_fsync)
            self.assertEqual(len(opened_child_fds), 1)
            with self.assertRaises(OSError):
                os.fstat(opened_child_fds[0])
            retained = [
                child
                for child in inputs.scratch_parent.iterdir()
                if child.name.startswith(capture.R2_SCRATCH_PREFIX)
            ]
            self.assertEqual(len(retained), 2)
            self.assertTrue(all(child.is_dir() for child in retained))

    def test_r2_receipt_never_claims_unmeasured_camera_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_r2_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            inputs = capture.validate_inputs(args)
            inputs.output_dir.mkdir()
            (inputs.output_dir / capture.IMAGES_DIR).mkdir()
            (inputs.output_dir / capture.WORKERS_DIR).mkdir()
            outcomes = []
            for camera in inputs.cameras:
                ordinal = camera["ordinal"]
                worker_dir = inputs.output_dir / capture.WORKERS_DIR / f"{ordinal:02d}"
                worker_dir.mkdir()
                manifest_path = worker_dir / capture.EXECUTION_FILE
                manifest_path.write_bytes(b"worker manifest")
                result_path = worker_dir / capture.UE_RESULT_FILE
                result_path.write_bytes(f"worker result {ordinal}".encode())
                editor_log = worker_dir / capture.EDITOR_LOG_FILE
                editor_stdout = worker_dir / capture.EDITOR_STDOUT_FILE
                editor_log.write_bytes(b"editor log")
                editor_stdout.write_bytes(b"editor stdout")
                final_path = inputs.output_dir / camera["relative_path"]
                raw = f"distinct image {ordinal}".encode()
                final_path.write_bytes(raw)
                worker = capture.WorkerRun(
                    ordinal=ordinal,
                    camera=dict(camera),
                    worker_dir=worker_dir,
                    manifest_path=manifest_path,
                    manifest_sha256=capture.sha256_file(manifest_path),
                    scratch_dir=root / f"scratch-{ordinal}",
                    scratch_png=root / f"scratch-{ordinal}/capture.png",
                    result_path=result_path,
                    editor_log=editor_log,
                    editor_stdout=editor_stdout,
                )
                outcomes.append(
                    capture.WorkerOutcome(
                        worker=worker,
                        ue_result={"engine_version": "5.7.0-test"},
                        ue_result_sha256=capture.sha256_file(result_path),
                        image={
                            "ordinal": ordinal,
                            "room_kind": camera["room_kind"],
                            "room_id": camera["room_id"],
                            "camera_id": camera["camera_id"],
                            "semantic_id": camera["semantic_id"],
                            "bytes": len(raw),
                            "sha256": capture.sha256_bytes(raw),
                        },
                    )
                )
            with (
                mock.patch.object(capture, "_verify_input_pins"),
                mock.patch.object(capture, "_load_json", return_value=({}, b"")),
                mock.patch.object(capture, "inspect_png_bytes"),
            ):
                scratch_ownership = capture._create_scratch_root(inputs)
                try:
                    receipt = capture.build_receipt(
                        inputs,
                        "a" * 64,
                        outcomes,
                        scratch_ownership=scratch_ownership,
                    )
                finally:
                    capture._remove_scratch_root(scratch_ownership)

            self.assertEqual(receipt["schema_version"], capture.R2_RECEIPT_SCHEMA)
            self.assertEqual(
                receipt["status"],
                "captured_pending_runtime_observation",
            )
            self.assertEqual(receipt["capture"]["shot_ids"], list(capture.R2_ORDERED_SHOT_IDS))
            self.assertEqual(receipt["capture"]["runtime_observation_status"], "pending")
            self.assertEqual(
                receipt["scratch"]["storage_class"],
                "private_nas_retained_evidence",
            )
            self.assertEqual(
                receipt["scratch"]["lifecycle"],
                capture.R2_SCRATCH_LIFECYCLE,
            )
            self.assertEqual(
                receipt["scratch"]["cleanup_policy"],
                capture.R2_SCRATCH_CLEANUP_POLICY,
            )
            self.assertEqual(
                receipt["scratch"]["cleanup_status_at_receipt"],
                "retained",
            )
            self.assertEqual(
                receipt["scratch"]["policy_root_mount_id"],
                receipt["scratch"]["parent_mount_id"],
            )
            self.assertEqual(
                receipt["scratch"]["parent_mount_id"],
                receipt["scratch"]["owned_child_mount_id"],
            )
            self.assertEqual(receipt["scratch"]["filesystem_type"], "nfs4")
            self.assertEqual(
                receipt["scratch"]["mount_source_sha256"],
                MOCK_NFS_SOURCE_SHA256,
            )
            self.assertFalse(
                receipt["scratch"]["scratch_absolute_path_disclosed"]
            )
            self.assertNotIn("absolute_path_disclosed", receipt["scratch"])
            self.assertEqual(receipt["attempt_root"], str(inputs.attempt_root))
            self.assertEqual(receipt["output_root"], str(inputs.output_dir))
            receipt_text = capture.canonical_json(receipt).decode("utf-8")
            self.assertNotIn(str(scratch_ownership.path), receipt_text)
            self.assertNotIn(str(scratch_ownership.parent), receipt_text)
            self.assertTrue(
                receipt["verification"][
                    "native_png_private_nas_retained_evidence"
                ]
            )
            self.assertTrue(receipt["verification"]["scratch_retained_append_only"])
            self.assertTrue(
                receipt["verification"]["scratch_cleanup_descriptor_close_only"]
            )
            self.assertNotIn(
                "native_png_private_local_scratch",
                receipt["verification"],
            )
            for key in (
                "near_field_clearance_observation",
                "foreground_occlusion_observation",
                "expected_hero_visibility_observation",
                "forbidden_foreground_observation",
                "physical_exposure_observation",
            ):
                self.assertEqual(receipt["verification"][key], "pending")

    def test_editor_environment_is_allowlisted_and_pins_nvidia_icd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            args = self.make_args(root)
            fake_editor = pathlib.Path(args.unreal_editor)
            fixed_editor = fake_editor.with_name("UnrealEditor")
            fake_editor.rename(fixed_editor)
            args.unreal_editor = str(fixed_editor)
            self.addCleanup(lambda: fixed_editor.unlink(missing_ok=True))
            inputs = capture.validate_inputs(args)

            manifest = inputs.output_dir / capture.WORKERS_DIR / "01" / capture.EXECUTION_FILE
            previous = os.environ.get("ANTHROPIC_API_KEY")
            os.environ["ANTHROPIC_API_KEY"] = "must-not-cross-boundary"
            try:
                environment = capture.build_editor_environment(inputs, manifest, "5" * 64)
            finally:
                if previous is None:
                    os.environ.pop("ANTHROPIC_API_KEY", None)
                else:
                    os.environ["ANTHROPIC_API_KEY"] = previous

        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertEqual(environment["DISPLAY"], ":117")
        self.assertEqual(environment["VK_ICD_FILENAMES"], str(capture.NVIDIA_VULKAN_ICD))
        self.assertEqual(environment[capture.WORKER_ENV], "1")
        self.assertEqual(environment[capture.EXECUTION_SHA_ENV], "5" * 64)
        self.assertEqual(environment[capture.EXECUTION_ENV], str(manifest))
        self.assertNotIn("TMPDIR", environment)
        self.assertNotIn("TMP", environment)
        self.assertNotIn("TEMP", environment)
        self.assertNotIn("XDG_DATA_HOME", environment)

    def write_fake_worker_success(
        self,
        inputs: capture.CaptureInputs,
        worker: capture.WorkerRun,
        *,
        seed: int,
    ) -> None:
        worker.editor_log.write_bytes(f"editor-{worker.ordinal}\n".encode())
        worker.editor_stdout.write_bytes(f"stdout-{worker.ordinal}\n".encode())
        raw = rgb_png(capture.WIDTH, capture.HEIGHT, seed=seed)
        worker.scratch_png.write_bytes(raw)
        manifest = json.loads(worker.manifest_path.read_text(encoding="utf-8"))
        camera = worker.camera
        result = capture._worker_result(
            manifest,
            worker.manifest_sha256,
            status="captured_candidate",
            captures=[
                {
                    "ordinal": camera["ordinal"],
                    "room_kind": camera["room_kind"],
                    "room_id": camera["room_id"],
                    "camera_id": camera["camera_id"],
                    "semantic_id": camera["semantic_id"],
                    "actor_label": f"CameraActor_{worker.ordinal}",
                    "capture_method": capture.CAPTURE_METHOD,
                    "actual_transform": camera["expected_transform"],
                    "actual_fov_deg": camera["expected_fov_deg"],
                    "relative_path": camera["relative_path"],
                    "bytes": len(raw),
                    "sha256": capture.sha256_bytes(raw),
                    "native_png_path": str(worker.scratch_png),
                }
            ],
            camera_actor_set_exact=True,
            error=None,
            engine_version="5.7.0-test",
            project_path=str(inputs.project),
            map_path=inputs.map_path,
        )
        capture._worker_write_result(manifest, result)

    def test_ordinal_manifest_binds_one_camera_and_private_safe_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            attempt = workspace / "attempt"
            attempt.mkdir()
            inputs = self.make_valid_inputs(attempt)
            local = workspace / "local-scratch"
            mkdir_private(local)
            scratch_root = local / "vista-home-review-fixture"
            mkdir_private(scratch_root)
            scratch_dir = scratch_root / "worker-04"
            mkdir_private(scratch_dir)
            scratch_png = scratch_dir / "capture.png"

            with mock.patch.object(capture, "LOCAL_SCRATCH_PARENT", local):
                manifest = capture.build_worker_execution(inputs, "a" * 64, 4, scratch_png)
                with self.assertRaisesRegex(capture.ReviewCaptureError, "ORDINAL_INVALID"):
                    capture.build_worker_execution(inputs, "a" * 64, 0, scratch_png)
                with self.assertRaisesRegex(capture.ReviewCaptureError, "SCRATCH_INVALID"):
                    capture._validate_scratch_png(
                        local / 'vista-home-review-fixture/worker-04/bad" name.png',
                        ordinal=4,
                        attempt_root=attempt,
                        require_parent=False,
                    )
                with self.assertRaisesRegex(capture.ReviewCaptureError, "SCRATCH_INVALID"):
                    capture._validate_scratch_png(
                        pathlib.Path("/mnt/NAS2/worker-04/capture.png"),
                        ordinal=4,
                        attempt_root=attempt,
                        require_parent=False,
                    )

        self.assertEqual(manifest["ordinal"], 4)
        self.assertEqual(manifest["camera"]["ordinal"], 4)
        self.assertNotIn("cameras", manifest)
        self.assertEqual(manifest["scratch_png"], str(scratch_png))
        self.assertTrue(manifest["policy"]["at_most_one_native_highres_shot"])

    def test_host_aggregates_six_sequential_children_and_exact_hash_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            attempt = workspace / "attempt"
            attempt.mkdir()
            inputs = self.make_valid_inputs(attempt)
            local = workspace / "local-scratch"
            mkdir_private(local)
            execution_raw = capture.canonical_json(capture.build_execution(inputs))
            execution_sha = capture.sha256_bytes(execution_raw)
            lifecycle: list[tuple[str, int]] = []

            def fake_run(_inputs: capture.CaptureInputs, worker: capture.WorkerRun) -> int:
                lifecycle.append(("start", worker.ordinal))
                for previous in range(1, worker.ordinal):
                    previous_camera = inputs.cameras[previous - 1]
                    self.assertTrue((inputs.output_dir / previous_camera["relative_path"]).is_file())
                self.assertFalse((inputs.output_dir / capture.RECEIPT_FILE).exists())
                self.write_fake_worker_success(inputs, worker, seed=worker.ordinal)
                lifecycle.append(("end", worker.ordinal))
                return 0

            with (
                mock.patch.object(capture, "LOCAL_SCRATCH_PARENT", local),
                mock.patch.object(capture, "run_editor", side_effect=fake_run),
            ):
                result = capture.execute_capture(inputs, execution_raw, execution_sha)

            receipt_path = pathlib.Path(result["receipt"])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                lifecycle,
                [(phase, ordinal) for ordinal in range(1, 7) for phase in ("start", "end")],
            )
            self.assertEqual(result["image_count"], 6)
            self.assertEqual([item["ordinal"] for item in receipt["bindings"]["worker_results"]], list(range(1, 7)))
            self.assertEqual(len(receipt["logs"]), 6)
            self.assertEqual(len(receipt["capture"]["images"]), 6)
            self.assertEqual(len({item["sha256"] for item in receipt["capture"]["images"]}), 6)
            self.assertTrue(all(item["native_and_final_sha256_equal"] for item in receipt["capture"]["images"]))
            self.assertFalse((inputs.output_dir / capture.UE_RESULT_FILE).exists())
            self.assertEqual(list(local.iterdir()), [])
            self.assertNotIn("scratch", receipt)
            self.assertTrue(
                receipt["verification"]["native_png_private_local_scratch"]
            )
            for ordinal in range(1, 7):
                manifest = json.loads(
                    (inputs.output_dir / capture.WORKERS_DIR / f"{ordinal:02d}" / capture.EXECUTION_FILE).read_text(
                        encoding="utf-8"
                    )
                )
                result_payload = json.loads(
                    (inputs.output_dir / capture.WORKERS_DIR / f"{ordinal:02d}" / capture.UE_RESULT_FILE).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(manifest["ordinal"], ordinal)
                self.assertEqual(len(result_payload["captures"]), 1)
                final = inputs.output_dir / inputs.cameras[ordinal - 1]["relative_path"]
                self.assertEqual(capture.sha256_file(final), result_payload["captures"][0]["sha256"])

    def test_child_failure_stops_sequence_and_never_writes_aggregate_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            attempt = workspace / "attempt"
            attempt.mkdir()
            inputs = self.make_valid_inputs(attempt)
            local = workspace / "local-scratch"
            mkdir_private(local)
            execution_raw = capture.canonical_json(capture.build_execution(inputs))
            execution_sha = capture.sha256_bytes(execution_raw)
            launched: list[int] = []

            def fake_run(_inputs: capture.CaptureInputs, worker: capture.WorkerRun) -> int:
                launched.append(worker.ordinal)
                worker.editor_log.write_bytes(b"editor\n")
                worker.editor_stdout.write_bytes(b"stdout\n")
                if worker.ordinal == 3:
                    return 9
                self.write_fake_worker_success(inputs, worker, seed=worker.ordinal)
                return 0

            with (
                mock.patch.object(capture, "LOCAL_SCRATCH_PARENT", local),
                mock.patch.object(capture, "run_editor", side_effect=fake_run),
                self.assertRaisesRegex(capture.ReviewCaptureError, "child 3 exited with status 9"),
            ):
                capture.execute_capture(inputs, execution_raw, execution_sha)

            self.assertEqual(launched, [1, 2, 3])
            self.assertFalse((inputs.output_dir / capture.RECEIPT_FILE).exists())
            self.assertEqual(list(local.iterdir()), [])

    def test_duplicate_room_images_fail_before_aggregate_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            attempt = workspace / "attempt"
            attempt.mkdir()
            inputs = self.make_valid_inputs(attempt)
            local = workspace / "local-scratch"
            mkdir_private(local)
            execution_raw = capture.canonical_json(capture.build_execution(inputs))
            execution_sha = capture.sha256_bytes(execution_raw)

            def fake_run(_inputs: capture.CaptureInputs, worker: capture.WorkerRun) -> int:
                self.write_fake_worker_success(inputs, worker, seed=0)
                return 0

            with (
                mock.patch.object(capture, "LOCAL_SCRATCH_PARENT", local),
                mock.patch.object(capture, "run_editor", side_effect=fake_run),
                self.assertRaisesRegex(capture.ReviewCaptureError, "PNG_DUPLICATE"),
            ):
                capture.execute_capture(inputs, execution_raw, execution_sha)

            self.assertFalse((inputs.output_dir / capture.RECEIPT_FILE).exists())

    def test_final_png_copy_is_o_excl_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            attempt = workspace / "attempt"
            attempt.mkdir()
            inputs = self.make_valid_inputs(attempt)
            local = workspace / "local-scratch"
            mkdir_private(local)
            execution_raw = capture.canonical_json(capture.build_execution(inputs))
            execution_sha = capture.sha256_bytes(execution_raw)
            with mock.patch.object(capture, "LOCAL_SCRATCH_PARENT", local):
                capture._prepare_output(inputs, execution_raw)
                scratch_ownership = capture._create_scratch_root(inputs)
                try:
                    worker = capture._prepare_worker_runs(
                        inputs,
                        execution_sha,
                        scratch_ownership.path,
                    )[0]
                    self.write_fake_worker_success(inputs, worker, seed=1)
                    ue_result, _sha = capture._load_worker_result(inputs, worker)
                    final = inputs.output_dir / worker.camera["relative_path"]
                    sentinel = b"must-not-overwrite"
                    final.write_bytes(sentinel)
                    with self.assertRaisesRegex(capture.ReviewCaptureError, "OUTPUT_EXISTS"):
                        capture._accept_worker_png(inputs, worker, ue_result)
                    self.assertEqual(final.read_bytes(), sentinel)
                finally:
                    capture._remove_scratch_root(scratch_ownership)

    def test_stable_valid_worker_proof_terminates_owned_child_and_returns_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            attempt = workspace / "attempt"
            attempt.mkdir()
            inputs = self.make_valid_inputs(attempt)
            local = workspace / "local-scratch"
            mkdir_private(local)
            execution_raw = capture.canonical_json(capture.build_execution(inputs))
            execution_sha = capture.sha256_bytes(execution_raw)

            class RunningProcess:
                pid = 424242

                @staticmethod
                def poll() -> None:
                    return None

            process = RunningProcess()
            with mock.patch.object(capture, "LOCAL_SCRATCH_PARENT", local):
                capture._prepare_output(inputs, execution_raw)
                scratch_ownership = capture._create_scratch_root(inputs)
                try:
                    worker = capture._prepare_worker_runs(
                        inputs,
                        execution_sha,
                        scratch_ownership.path,
                    )[0]
                    self.write_fake_worker_success(inputs, worker, seed=1)
                    worker.editor_stdout.unlink()
                    with (
                        mock.patch.object(capture.subprocess, "Popen", return_value=process),
                        mock.patch.object(capture, "_terminate_owned") as terminate,
                        mock.patch.object(capture, "WORKER_PROOF_STABILITY_SECONDS", 0.0),
                        mock.patch.object(capture, "WORKER_PROOF_POLL_INTERVAL_SECONDS", 0.0),
                        mock.patch.object(
                            capture,
                            "_probe_worker_success",
                            wraps=capture._probe_worker_success,
                        ) as probe,
                    ):
                        returncode = capture.run_editor(inputs, worker)

                    self.assertEqual(returncode, 0)
                    self.assertGreaterEqual(probe.call_count, 2)
                    terminate.assert_called_once_with(process)
                finally:
                    capture._remove_scratch_root(scratch_ownership)

    def test_partial_or_invalid_worker_proof_cannot_synthesize_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            attempt = workspace / "attempt"
            attempt.mkdir()
            inputs = self.make_valid_inputs(attempt)
            local = workspace / "local-scratch"
            mkdir_private(local)
            execution_raw = capture.canonical_json(capture.build_execution(inputs))
            execution_sha = capture.sha256_bytes(execution_raw)

            with mock.patch.object(capture, "LOCAL_SCRATCH_PARENT", local):
                capture._prepare_output(inputs, execution_raw)
                scratch_ownership = capture._create_scratch_root(inputs)
                try:
                    worker = capture._prepare_worker_runs(
                        inputs,
                        execution_sha,
                        scratch_ownership.path,
                    )[0]
                    self.write_fake_worker_success(inputs, worker, seed=1)
                    good_result = worker.result_path.read_bytes()
                    good_png = worker.scratch_png.read_bytes()

                    worker.result_path.write_bytes(b'{"schema_version":')
                    self.assertIsNone(capture._probe_worker_success(inputs, worker))

                    worker.result_path.write_bytes(good_result)
                    worker.scratch_png.write_bytes(good_png[:100])
                    self.assertIsNone(capture._probe_worker_success(inputs, worker))

                    worker.scratch_png.write_bytes(good_png)
                    invalid = json.loads(good_result)
                    invalid["execution_sha256"] = "0" * 64
                    worker.result_path.write_bytes(capture.canonical_json(invalid))
                    self.assertIsNone(capture._probe_worker_success(inputs, worker))

                    class ExitingProcess:
                        pid = 434343

                        def __init__(self) -> None:
                            self.returncodes = iter((None, 17))

                        def poll(self) -> int | None:
                            return next(self.returncodes, 17)

                    process = ExitingProcess()
                    worker.editor_stdout.unlink()
                    with (
                        mock.patch.object(capture.subprocess, "Popen", return_value=process),
                        mock.patch.object(capture, "_terminate_owned") as terminate,
                        mock.patch.object(capture, "WORKER_PROOF_POLL_INTERVAL_SECONDS", 0.0),
                    ):
                        returncode = capture.run_editor(inputs, worker)
                    self.assertEqual(returncode, 17)
                    terminate.assert_called_once_with(process)
                finally:
                    capture._remove_scratch_root(scratch_ownership)

    def test_input_pin_drift_is_rejected_before_child_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            inputs = self.make_valid_inputs(root)
            inputs.map_asset.write_bytes(b"drifted umap")
            with self.assertRaisesRegex(capture.ReviewCaptureError, "PIN_MISMATCH"):
                capture._verify_input_pins(inputs)


if __name__ == "__main__":
    unittest.main()
