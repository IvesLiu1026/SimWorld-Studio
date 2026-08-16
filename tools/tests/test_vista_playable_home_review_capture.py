from __future__ import annotations

import argparse
import binascii
import copy
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
            {"capture_profile", "visual_profile", "visual_profile_sha256"}
            <= destinations
        )

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
        self.assertNotIn("graphics_adapter", execution["engine"])

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


class ReviewCaptureInputTests(unittest.TestCase):
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
                receipt = capture.build_receipt(inputs, "a" * 64, outcomes)

            self.assertEqual(receipt["schema_version"], capture.R2_RECEIPT_SCHEMA)
            self.assertEqual(
                receipt["status"],
                "captured_pending_runtime_observation",
            )
            self.assertEqual(receipt["capture"]["shot_ids"], list(capture.R2_ORDERED_SHOT_IDS))
            self.assertEqual(receipt["capture"]["runtime_observation_status"], "pending")
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
            local.mkdir(mode=0o700)
            scratch_root = local / "vista-home-review-fixture"
            scratch_root.mkdir(mode=0o700)
            scratch_dir = scratch_root / "worker-04"
            scratch_dir.mkdir(mode=0o700)
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
            local.mkdir(mode=0o700)
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
            local.mkdir(mode=0o700)
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
            local.mkdir(mode=0o700)
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
            local.mkdir(mode=0o700)
            execution_raw = capture.canonical_json(capture.build_execution(inputs))
            execution_sha = capture.sha256_bytes(execution_raw)
            with mock.patch.object(capture, "LOCAL_SCRATCH_PARENT", local):
                capture._prepare_output(inputs, execution_raw)
                scratch_root = capture._create_scratch_root(inputs)
                try:
                    worker = capture._prepare_worker_runs(inputs, execution_sha, scratch_root)[0]
                    self.write_fake_worker_success(inputs, worker, seed=1)
                    ue_result, _sha = capture._load_worker_result(inputs, worker)
                    final = inputs.output_dir / worker.camera["relative_path"]
                    sentinel = b"must-not-overwrite"
                    final.write_bytes(sentinel)
                    with self.assertRaisesRegex(capture.ReviewCaptureError, "OUTPUT_EXISTS"):
                        capture._accept_worker_png(inputs, worker, ue_result)
                    self.assertEqual(final.read_bytes(), sentinel)
                finally:
                    capture._remove_scratch_root(scratch_root)

    def test_stable_valid_worker_proof_terminates_owned_child_and_returns_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            attempt = workspace / "attempt"
            attempt.mkdir()
            inputs = self.make_valid_inputs(attempt)
            local = workspace / "local-scratch"
            local.mkdir(mode=0o700)
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
                scratch_root = capture._create_scratch_root(inputs)
                try:
                    worker = capture._prepare_worker_runs(inputs, execution_sha, scratch_root)[0]
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
                    capture._remove_scratch_root(scratch_root)

    def test_partial_or_invalid_worker_proof_cannot_synthesize_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory).resolve()
            attempt = workspace / "attempt"
            attempt.mkdir()
            inputs = self.make_valid_inputs(attempt)
            local = workspace / "local-scratch"
            local.mkdir(mode=0o700)
            execution_raw = capture.canonical_json(capture.build_execution(inputs))
            execution_sha = capture.sha256_bytes(execution_raw)

            with mock.patch.object(capture, "LOCAL_SCRATCH_PARENT", local):
                capture._prepare_output(inputs, execution_raw)
                scratch_root = capture._create_scratch_root(inputs)
                try:
                    worker = capture._prepare_worker_runs(inputs, execution_sha, scratch_root)[0]
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
                    capture._remove_scratch_root(scratch_root)

    def test_input_pin_drift_is_rejected_before_child_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            inputs = self.make_valid_inputs(root)
            inputs.map_asset.write_bytes(b"drifted umap")
            with self.assertRaisesRegex(capture.ReviewCaptureError, "PIN_MISMATCH"):
                capture._verify_input_pins(inputs)


if __name__ == "__main__":
    unittest.main()
