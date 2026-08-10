from __future__ import annotations

import binascii
import hashlib
import json
import pathlib
import struct
import sys
import tempfile
import unittest
import zlib

TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
BLENDER_TOOLS_DIR = TOOLS_DIR / "blender"
sys.path.insert(0, str(BLENDER_TOOLS_DIR))

import build_vista_mmg040_office as builder
import validate_vista_asset as validator


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def rgb_png(width: int = 320, height: int = 320, *, blank: bool = False) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if blank:
                rows.extend((42, 42, 42))
            else:
                rows.extend(((x * 3) % 256, (y * 5) % 256, ((x + y) * 2) % 256))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        validator.PNG_SIGNATURE
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + png_chunk(b"IEND", b"")
    )


def glb_bytes(document: dict, binary: bytes, *, include_bin: bool = True) -> bytes:
    glb_document = json.loads(json.dumps(document))
    glb_document["buffers"][0].pop("uri", None)
    json_payload = json.dumps(
        glb_document, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    json_payload += b" " * ((-len(json_payload)) % 4)
    chunks = (
        struct.pack("<II", len(json_payload), validator.GLB_JSON_CHUNK) + json_payload
    )
    if include_bin:
        binary_payload = binary + b"\0" * ((-len(binary)) % 4)
        chunks += (
            struct.pack("<II", len(binary_payload), validator.GLB_BIN_CHUNK)
            + binary_payload
        )
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class BundleFixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.mkdir(mode=0o700, exist_ok=True)
        binary = bytearray()
        buffer_views: list[dict] = []
        accessors: list[dict] = []

        def add_payload(payload: bytes, *, target: int) -> int:
            binary.extend(b"\0" * ((-len(binary)) % 4))
            offset = len(binary)
            binary.extend(payload)
            buffer_views.append(
                {
                    "buffer": 0,
                    "byteOffset": offset,
                    "byteLength": len(payload),
                    "target": target,
                }
            )
            return len(buffer_views) - 1

        def add_positions(points: list[tuple[float, float, float]]) -> int:
            payload = b"".join(struct.pack("<3f", *point) for point in points)
            view = add_payload(payload, target=34962)
            accessors.append(
                {
                    "bufferView": view,
                    "componentType": 5126,
                    "count": len(points),
                    "type": "VEC3",
                    "min": [min(point[axis] for point in points) for axis in range(3)],
                    "max": [max(point[axis] for point in points) for axis in range(3)],
                }
            )
            return len(accessors) - 1

        def add_indices(values: tuple[int, ...]) -> int:
            view = add_payload(
                struct.pack("<" + "H" * len(values), *values), target=34963
            )
            accessors.append(
                {
                    "bufferView": view,
                    "componentType": 5123,
                    "count": len(values),
                    "type": "SCALAR",
                }
            )
            return len(accessors) - 1

        cabinet_position = add_positions(
            [
                (-0.6, 0.0, 0.3),
                (0.6, 0.0, -0.3),
                (-0.6, 2.34, -0.3),
            ]
        )
        cabinet_indices = add_indices((0, 1, 2))
        room_position = add_positions(
            [
                (-2.6, -0.08, 2.5),
                (2.6, -0.08, -2.55),
                (-2.6, 2.76, -2.55),
            ]
        )
        room_indices = add_indices((0, 1, 2))
        chair_position = add_positions(
            [
                (-0.4, 0.0, 0.4),
                (0.4, 0.0, -0.4),
                (-0.4, 1.2, -0.4),
            ]
        )
        chair_indices = add_indices((0, 1, 2))
        binary.extend(b"\0" * ((-len(binary)) % 4))
        self.bin_bytes = bytes(binary)
        self.document = {
            "asset": {"version": "2.0", "generator": "synthetic-vista-test"},
            "buffers": [
                {"uri": "vista_mmg040_office.bin", "byteLength": len(self.bin_bytes)}
            ],
            "bufferViews": buffer_views,
            "accessors": accessors,
            "materials": [
                {"name": "VISTA_M_CabinetPowdercoat"},
                {"name": "VISTA_M_Wall_WarmWhite"},
                {"name": "VISTA_M_ChairSeatFabric"},
            ],
            "meshes": [
                {
                    "name": "VISTA_Cabinet_Test_Mesh",
                    "primitives": [
                        {
                            "attributes": {"POSITION": cabinet_position},
                            "indices": cabinet_indices,
                            "material": 0,
                        }
                    ],
                },
                {
                    "name": "VISTA_Room_Test_Mesh",
                    "primitives": [
                        {
                            "attributes": {"POSITION": room_position},
                            "indices": room_indices,
                            "material": 1,
                        }
                    ],
                },
                {
                    "name": "VISTA_Chair_Test_Mesh",
                    "primitives": [
                        {
                            "attributes": {"POSITION": chair_position},
                            "indices": chair_indices,
                            "material": 2,
                        }
                    ],
                },
            ],
            "nodes": [
                {
                    "name": "VISTA_Cabinet_Root",
                    "children": [1],
                    "translation": [0.0, 0.0, -2.14],
                },
                {"name": "VISTA_Cabinet_Test", "mesh": 0},
                {"name": "VISTA_Room_Root", "children": [3]},
                {"name": "VISTA_Room_Test", "mesh": 1},
                {
                    "name": "VISTA_Chair_Root",
                    "children": [5],
                    "translation": [-0.4, 0.0, 0.8],
                },
                {"name": "VISTA_Chair_Test", "mesh": 2},
            ],
            "scenes": [{"nodes": [0, 2, 4]}],
            "scene": 0,
        }
        self.files: dict[str, bytes] = {
            "source.blend": b"BLENDER-vista-test-fixture",
            "vista_mmg040_office.glb": glb_bytes(self.document, self.bin_bytes),
            "vista_mmg040_office.gltf": json.dumps(
                self.document, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
            "vista_mmg040_office.bin": self.bin_bytes,
            "preview-overview.png": rgb_png(),
            "preview-detail.png": rgb_png(),
        }
        self.manifest: dict = {
            "schema": validator.SCHEMA,
            "asset_id": validator.EXPECTED_ASSET_ID,
            "build": {
                "seed": 4040,
                "timestamp_utc": "2026-08-11T00:00:00Z",
                "blender": {"version": "4.5.8", "version_string": "4.5.8 LTS"},
            },
            "units": {
                "system": "METRIC",
                "length": "meter",
                "scale_length": 1.0,
                "up_axis": "Z",
            },
            "source": {
                "description": "Synthetic test fixture for the procedural VISTA bundle",
                "license": "Apache-2.0",
                "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
                "generator": "SimWorld Studio test",
            },
            "script": {
                "path": validator.EXPECTED_SCRIPT,
                "sha256": validator.sha256_file(
                    BLENDER_TOOLS_DIR / "build_vista_mmg040_office.py"
                ),
            },
            "collections": sorted(validator.EXPECTED_COLLECTIONS),
            "geometry": {
                "bounds": {
                    "min": [-2.6, -2.5, -0.08],
                    "max": [2.6, 2.55, 2.76],
                    "dimensions": [5.2, 5.05, 2.84],
                },
                "mesh_count": 3,
                "material_count": 3,
                "triangle_count": 3,
                "mesh_names": [
                    "VISTA_Cabinet_Test_Mesh",
                    "VISTA_Chair_Test_Mesh",
                    "VISTA_Room_Test_Mesh",
                ],
                "material_names": [
                    "VISTA_M_CabinetPowdercoat",
                    "VISTA_M_ChairSeatFabric",
                    "VISTA_M_Wall_WarmWhite",
                ],
            },
            "assets": {
                "ergonomic_office_chair": {
                    "collection": "VISTA_ErgonomicOfficeChair",
                    "root_node": "VISTA_Chair_Root",
                    "origin_m": [-0.4, -0.8, 0.0],
                    "ue_placement": {
                        "location_cm": [220.0, -40.0, 0.0],
                        "rotation_deg": [0.0, 0.0, 0.0],
                        "scale": [1.0, 1.0, 1.0],
                    },
                    "bounds": {
                        "min": [-0.4, -0.4, 0.0],
                        "max": [0.4, 0.4, 1.2],
                        "dimensions": [0.8, 0.8, 1.2],
                    },
                    "mesh_count": 1,
                    "material_count": 1,
                    "triangle_count": 1,
                    "mesh_names": ["VISTA_Chair_Test_Mesh"],
                    "material_names": ["VISTA_M_ChairSeatFabric"],
                },
                "tall_office_cabinet": {
                    "collection": "VISTA_TallOfficeCabinet",
                    "root_node": "VISTA_Cabinet_Root",
                    "origin_m": [0.0, 2.14, 0.0],
                    "ue_placement": {
                        "location_cm": [514.0, 0.0, 0.0],
                        "rotation_deg": [0.0, 0.0, 0.0],
                        "scale": [1.0, 1.0, 1.0],
                    },
                    "bounds": {
                        "min": [-0.6, -0.3, 0.0],
                        "max": [0.6, 0.3, 2.34],
                        "dimensions": [1.2, 0.6, 2.34],
                    },
                    "mesh_count": 1,
                    "material_count": 1,
                    "triangle_count": 1,
                    "mesh_names": ["VISTA_Cabinet_Test_Mesh"],
                    "material_names": ["VISTA_M_CabinetPowdercoat"],
                },
                "room_shell_kit": {
                    "collection": "VISTA_RoomShell",
                    "root_node": "VISTA_Room_Root",
                    "origin_m": [0.0, 0.0, 0.0],
                    "ue_placement": {
                        "location_cm": [300.0, 0.0, 0.0],
                        "rotation_deg": [0.0, 0.0, 0.0],
                        "scale": [1.0, 1.0, 1.0],
                    },
                    "bounds": {
                        "min": [-2.6, -2.5, -0.08],
                        "max": [2.6, 2.55, 2.76],
                        "dimensions": [5.2, 5.05, 2.84],
                    },
                    "mesh_count": 1,
                    "material_count": 1,
                    "triangle_count": 1,
                    "mesh_names": ["VISTA_Room_Test_Mesh"],
                    "material_names": ["VISTA_M_Wall_WarmWhite"],
                },
            },
            "outputs": {},
        }
        self.write()

    @property
    def manifest_path(self) -> pathlib.Path:
        return self.root / "manifest.json"

    def write(self) -> None:
        for name, raw in self.files.items():
            (self.root / name).write_bytes(raw)
        outputs = {}
        for key, (name, media_type) in validator.EXPECTED_OUTPUTS.items():
            raw = self.files[name]
            outputs[key] = {
                "path": name,
                "bytes": len(raw),
                "sha256": sha(raw),
                "media_type": media_type,
            }
            if key.startswith("preview_"):
                outputs[key].update({"width": 320, "height": 320})
        self.manifest["outputs"] = outputs
        self.manifest_path.write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def replace_glb_document(self) -> None:
        self.files["vista_mmg040_office.glb"] = glb_bytes(self.document, self.bin_bytes)
        self.write()

    def replace_gltf_document(self) -> None:
        self.files["vista_mmg040_office.gltf"] = json.dumps(
            self.document, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.write()

    def replace_binary(self, binary: bytes) -> None:
        if len(binary) != len(self.bin_bytes):
            raise ValueError(
                "Test payload mutations must preserve the manifest fixture buffer length"
            )
        self.bin_bytes = binary
        self.files["vista_mmg040_office.bin"] = binary
        self.files["vista_mmg040_office.glb"] = glb_bytes(self.document, binary)
        self.write()


class VistaBlenderAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = BundleFixture(pathlib.Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_validation_error(
        self, code: str, callback
    ) -> validator.AssetValidationError:
        with self.assertRaises(validator.AssetValidationError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_manifest_and_glb_happy_path(self) -> None:
        result = validator.validate_path(self.fixture.manifest_path)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["glb"]["mesh_count"], 3)
        self.assertEqual(result["glb"]["triangle_count"], 3)
        self.assertTrue(result["gltf"]["equivalent_geometry"])
        self.assertFalse(result["glb"]["contains_cameras"])
        self.assertFalse(result["glb"]["contains_lights"])
        for computed, expected in zip(result["glb"]["bounds_min"], [-2.6, -2.5, -0.08]):
            self.assertAlmostEqual(computed, expected, places=5)
        for computed, expected in zip(result["glb"]["bounds_max"], [2.6, 2.55, 2.76]):
            self.assertAlmostEqual(computed, expected, places=5)
        self.assertGreater(result["previews"]["preview_overview"]["luminance_range"], 8)
        selected = validator.validate_path(
            self.fixture.root / "vista_mmg040_office.glb"
        )
        self.assertEqual(selected["manifest_sha256"], result["manifest_sha256"])

    def test_glb_requires_a_real_bin_chunk(self) -> None:
        self.fixture.files["vista_mmg040_office.glb"] = glb_bytes(
            self.fixture.document,
            self.fixture.bin_bytes,
            include_bin=False,
        )
        self.fixture.write()
        self.assert_validation_error(
            "GLB_BIN_REQUIRED",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_glb_buffer_and_buffer_view_ranges_are_enforced(self) -> None:
        self.fixture.document["buffers"][0]["byteLength"] = (
            len(self.fixture.bin_bytes) + 4
        )
        self.fixture.replace_glb_document()
        self.assert_validation_error(
            "GLB_BUFFER_INVALID",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

        self.fixture.document["buffers"][0]["byteLength"] = len(self.fixture.bin_bytes)
        self.fixture.document["bufferViews"][0]["byteOffset"] = (
            len(self.fixture.bin_bytes) - 4
        )
        self.fixture.replace_glb_document()
        self.assert_validation_error(
            "GLB_BUFFER_VIEW_INVALID",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_accessor_offsets_and_decoded_payload_are_enforced(self) -> None:
        self.fixture.document["accessors"][0]["byteOffset"] = 8
        self.fixture.replace_glb_document()
        self.assert_validation_error(
            "GLB_ACCESSOR_INVALID",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

        del self.fixture.document["accessors"][0]["byteOffset"]
        binary = bytearray(self.fixture.bin_bytes)
        position_offset = self.fixture.document["bufferViews"][0]["byteOffset"]
        struct.pack_into("<f", binary, position_offset, float("nan"))
        self.fixture.replace_binary(bytes(binary))
        self.assert_validation_error(
            "GLB_ACCESSOR_PAYLOAD_INVALID",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_accessor_declared_bounds_must_match_bin_payload(self) -> None:
        self.fixture.document["accessors"][0]["max"][0] = 0.7
        self.fixture.replace_glb_document()
        self.assert_validation_error(
            "GLB_ACCESSOR_BOUNDS_MISMATCH",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_decoded_indices_must_reference_position_payload(self) -> None:
        binary = bytearray(self.fixture.bin_bytes)
        index_offset = self.fixture.document["bufferViews"][1]["byteOffset"]
        struct.pack_into("<H", binary, index_offset + 4, 3)
        self.fixture.replace_binary(bytes(binary))
        self.assert_validation_error(
            "GLB_GEOMETRY_INVALID",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_node_transformed_bounds_and_scale_must_match_manifest(self) -> None:
        self.fixture.document["nodes"][2]["scale"] = [2.0, 1.0, 1.0]
        self.fixture.replace_glb_document()
        self.assert_validation_error(
            "GLB_MANIFEST_BOUNDS_MISMATCH",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_manifest_bounds_cannot_claim_a_different_metric_extent(self) -> None:
        self.fixture.manifest["geometry"]["bounds"] = {
            "min": [-3.0, -2.5, -0.08],
            "max": [2.6, 2.55, 2.76],
            "dimensions": [5.6, 5.05, 2.84],
        }
        self.fixture.write()
        self.assert_validation_error(
            "GLB_MANIFEST_BOUNDS_MISMATCH",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_output_hash_tampering_fails_closed(self) -> None:
        with (self.fixture.root / "source.blend").open("ab") as handle:
            handle.write(b"tamper")
        self.assert_validation_error(
            "OUTPUT_SIZE_MISMATCH",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_blank_preview_is_rejected_after_valid_hash_binding(self) -> None:
        self.fixture.files["preview-detail.png"] = rgb_png(blank=True)
        self.fixture.write()
        self.assert_validation_error(
            "PREVIEW_BLANK", lambda: validator.validate_path(self.fixture.manifest_path)
        )

    def test_camera_and_light_extensions_are_forbidden(self) -> None:
        self.fixture.document["cameras"] = [
            {"type": "perspective", "perspective": {"yfov": 0.8, "znear": 0.1}}
        ]
        self.fixture.document["nodes"][1]["camera"] = 0
        self.fixture.replace_glb_document()
        self.assert_validation_error(
            "GLB_CAMERA_FORBIDDEN",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

        del self.fixture.document["cameras"]
        del self.fixture.document["nodes"][1]["camera"]
        self.fixture.document["extensionsUsed"] = ["KHR_lights_punctual"]
        self.fixture.replace_glb_document()
        self.assert_validation_error(
            "GLB_LIGHT_FORBIDDEN",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_separate_gltf_must_match_canonical_glb(self) -> None:
        self.fixture.document["materials"][0]["name"] = "UnexpectedMaterial"
        self.fixture.replace_gltf_document()
        self.assert_validation_error(
            "GLTF_GLB_MISMATCH",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_script_hash_and_ue_placement_are_bound(self) -> None:
        self.fixture.manifest["script"]["sha256"] = "0" * 64
        self.fixture.write()
        self.assert_validation_error(
            "SCRIPT_BINDING_INVALID",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

        self.fixture.manifest["script"]["sha256"] = validator.sha256_file(
            BLENDER_TOOLS_DIR / "build_vista_mmg040_office.py"
        )
        self.fixture.manifest["assets"]["tall_office_cabinet"]["ue_placement"][
            "location_cm"
        ] = [520.0, 0.0, 0.0]
        self.fixture.write()
        self.assert_validation_error(
            "MANIFEST_ASSETS_INVALID",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_chair_asset_and_placement_are_mandatory(self) -> None:
        chair = self.fixture.manifest["assets"].pop("ergonomic_office_chair")
        self.fixture.write()
        self.assert_validation_error(
            "MANIFEST_ASSETS_INVALID",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

        self.fixture.manifest["assets"]["ergonomic_office_chair"] = chair
        self.fixture.manifest["assets"]["ergonomic_office_chair"]["ue_placement"][
            "location_cm"
        ] = [150.0, -150.0, 0.0]
        self.fixture.write()
        self.assert_validation_error(
            "MANIFEST_ASSETS_INVALID",
            lambda: validator.validate_path(self.fixture.manifest_path),
        )

    def test_generator_contract_is_fixed_and_headless_reproducible(self) -> None:
        self.assertEqual(builder.EXPECTED_BLENDER_VERSION, (4, 5, 8))
        self.assertEqual(builder.CANONICAL_SEED, 4040)
        self.assertEqual(builder.OUTPUT_FILENAMES["glb"], "vista_mmg040_office.glb")
        self.assertEqual(builder.OUTPUT_FILENAMES["gltf"], "vista_mmg040_office.gltf")
        self.assertEqual(
            builder.blender_origin_to_ue_placement(builder.CABINET_ASSEMBLY_ORIGIN_M),
            {
                "location_cm": [514.0, 0.0, 0.0],
                "rotation_deg": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
        )
        self.assertEqual(
            builder.blender_origin_to_ue_placement(builder.CHAIR_ASSEMBLY_ORIGIN_M),
            {
                "location_cm": [220.0, -40.0, 0.0],
                "rotation_deg": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
        )
        views = builder.derive_preview_camera_transforms(
            self.fixture.manifest["geometry"]["bounds"],
            self.fixture.manifest["assets"]["ergonomic_office_chair"]["bounds"],
            builder.CHAIR_ASSEMBLY_ORIGIN_M,
        )
        self.assertEqual(set(views), {"overview", "detail"})
        self.assertGreater(
            views["overview"]["location"][0],
            self.fixture.manifest["geometry"]["bounds"]["max"][0],
        )
        self.assertLess(
            views["detail"]["location"][1], builder.CHAIR_ASSEMBLY_ORIGIN_M[1]
        )
        with self.assertRaises(RuntimeError):
            builder.prepare_output_root(pathlib.Path("relative-output"), 4040)
        with self.assertRaises(RuntimeError):
            builder.prepare_output_root(
                pathlib.Path(self.temporary.name) / "wrong-seed", 1
            )

        source = (BLENDER_TOOLS_DIR / "build_vista_mmg040_office.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("export_cameras=False", source)
        self.assertIn("export_lights=False", source)
        self.assertIn('export_format="GLB"', source)
        self.assertIn('export_format="GLTF_SEPARATE"', source)
        self.assertIn("random.Random(seed)", source)
        self.assertIn("VISTA_Cabinet_LeftSide", source)  # primary silhouette
        self.assertIn("DoorInset", source)  # medium recessed detail
        self.assertIn("WearFleck", source)  # fine deterministic detail
        self.assertIn("VISTA_Chair_BaseSpoke", source)  # primary chair silhouette
        self.assertIn("VISTA_Chair_SeatCushion", source)  # upholstered form
        self.assertIn("VISTA_Chair_BackWeave", source)  # fine mesh-back detail
        self.assertIn("VISTA_Chair_CasterWheel", source)  # functional base detail
        self.assertIn("derive_preview_camera_transforms", source)

    def test_wrapper_pins_factory_startup_version_and_append_only_root(self) -> None:
        wrapper = (BLENDER_TOOLS_DIR / "run_vista_blender_build.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("blender-4.5.8-linux-x64/blender", wrapper)
        self.assertIn("--factory-startup", wrapper)
        self.assertIn("VISTA_BLENDER_ALLOWED_ROOT", wrapper)
        self.assertIn("Refusing to replace append-only Blender artifact", wrapper)
        self.assertIn("validate_vista_asset.py", wrapper)


if __name__ == "__main__":
    unittest.main()
