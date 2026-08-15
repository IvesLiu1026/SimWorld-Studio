from __future__ import annotations

import argparse
import binascii
import copy
import inspect
import json
import pathlib
import stat
import struct
import tempfile
import unittest
import zlib


from tools.ue.vista_playable_home import capture_review_views as capture


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


def rgb_png(width: int, height: int, *, solid: bool = False) -> bytes:
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)
        for x in range(width):
            if solid:
                rgb = (0, 0, 0)
            else:
                rgb = ((x * 31 + y * 7) % 256, (x * 11 + y * 43) % 256, (x * 17 + y * 23) % 256)
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

        root = pathlib.Path("/tmp/vista-review-fixture")
        inputs = capture.CaptureInputs(
            attempt_root=root,
            project=root / "project" / capture.EXPECTED_PROJECT_NAME,
            project_sha256="2" * 64,
            build_plan=root / "contracts" / "build-plan.json",
            build_plan_sha256="3" * 64,
            plan=build_plan(),
            map_path=capture.EXPECTED_MAP_PATH,
            unreal_editor=pathlib.Path("/opt/Unreal/Engine/Binaries/Linux/UnrealEditor"),
            output_dir=root / "review-cameras" / "attempt-01",
            display=":117",
            graphics_adapter=0,
            timeout_seconds=300,
            script=pathlib.Path(capture.__file__).resolve(),
            script_sha256="4" * 64,
            cameras=tuple(capture.compile_fixed_cameras(build_plan(), capture.EXPECTED_MAP_PATH)),
        )
        command = capture.build_editor_command(inputs)
        script_options = [value for value in command if value.startswith("-ExecutePythonScript=")]
        self.assertEqual(script_options, [f"-ExecutePythonScript={pathlib.Path(capture.__file__).resolve()}"])
        self.assertNotIn("-game", command)
        self.assertIn("-Windowed", command)
        self.assertIn("-ResX=1280", command)
        self.assertIn("-ResY=720", command)

    def test_worker_uses_post_tick_fixed_actor_capture(self) -> None:
        source = inspect.getsource(capture._unreal_worker)
        self.assertIn("get_all_level_actors", source)
        self.assertIn("unreal.CameraActor", source)
        self.assertIn("semantic_tag", source)
        self.assertIn("register_slate_post_tick_callback", source)
        self.assertIn("set_keep_python_script_alive(True)", source)
        self.assertIn("AutomationLibrary.take_high_res_screenshot", source)
        self.assertNotIn("exec(", source)


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
        editor = root.parent / f"UnrealEditor-{root.name}"
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
            apply=False,
        )

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


if __name__ == "__main__":
    unittest.main()
