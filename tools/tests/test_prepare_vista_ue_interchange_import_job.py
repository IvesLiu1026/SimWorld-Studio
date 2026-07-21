from __future__ import annotations

import contextlib
import hashlib
import io
import json
import pathlib
import stat
import sys
import tempfile
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import fetch_vista_bootstrap_assets as acquisition  # noqa: E402
import prepare_vista_ue_interchange_import_job as preparation  # noqa: E402


def digest(raw: bytes, algorithm: str) -> str:
    return hashlib.new(algorithm, raw).hexdigest()


class Fixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.chmod(0o700)
        self.acquisition_parent = root / "acquisitions"
        self.acquisition_parent.mkdir(mode=0o700)
        self.acquisition_dir = self.acquisition_parent / "fixture-assets"
        self.publication_parent = root / "publications"
        self.publication_parent.mkdir(mode=0o700)
        self.output_dir = self.publication_parent / "fixture-job-r1"
        self.binary = b"\0" * 36
        self.image = b"synthetic-jpeg"
        self.gltf: dict = {
            "asset": {"version": "2.0", "generator": "synthetic-test"},
            "buffers": [{"uri": "model.bin", "byteLength": len(self.binary)}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(self.binary)}],
            "accessors": [
                {
                    "bufferView": 0,
                    "componentType": 5126,
                    "count": 3,
                    "type": "VEC3",
                    "min": [-1.0, 0.0, -0.5],
                    "max": [1.0, 2.0, 0.5],
                }
            ],
            "images": [{"uri": "textures/diff.jpg"}],
            "textures": [{"source": 0}],
            "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "material": 0}]}],
            "nodes": [{"mesh": 0}],
            "scenes": [{"nodes": [0]}],
            "scene": 0,
        }
        self.raw_gltf: bytes | None = None
        self.manifest_path = root / "source-manifest-input.json"
        self.manifest: dict = {}
        self.refresh_manifest()

    def refresh_manifest(self) -> None:
        gltf_raw = self.raw_gltf or preparation.canonical_json(self.gltf)
        self.payloads = {
            acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.gltf": gltf_raw,
            acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.bin": self.binary,
            acquisition.DOWNLOAD_PREFIX + "fixture_asset/textures/diff.jpg": self.image,
        }
        files = []
        for relative in ("model.gltf", "model.bin", "textures/diff.jpg"):
            url = acquisition.DOWNLOAD_PREFIX + "fixture_asset/" + relative
            raw = self.payloads[url]
            files.append(
                {
                    "path": "fixture_asset/" + relative,
                    "url": url,
                    "bytes": len(raw),
                    "md5": digest(raw, "md5"),
                    "sha256": digest(raw, "sha256"),
                }
            )
        self.manifest = {
            "schema": acquisition.SCHEMA,
            "revision": "fixture-v1",
            "provider": "Poly Haven",
            "provider_url": "https://polyhaven.com/",
            "license": acquisition.EXPECTED_LICENSE,
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "attribution": "Synthetic test fixture",
            "assets": [
                {
                    "asset_id": "polyhaven_fixture_asset",
                    "source_asset_id": "fixture_asset",
                    "source_page": acquisition.SOURCE_PREFIX + "fixture_asset",
                    "semantic_roles": ["stable_step_stool"],
                    "format": "gltf-2.0",
                    "files": files,
                }
            ],
        }
        self.manifest_path.write_bytes(preparation.canonical_json(self.manifest))
        self.manifest_path.chmod(0o600)

    def opener(self, url: str):
        @contextlib.contextmanager
        def opened():
            yield io.BytesIO(self.payloads[url])

        return opened()

    def acquire(self) -> acquisition.AcquisitionPlan:
        plan = acquisition.build_plan(self.manifest_path, self.acquisition_dir)
        self.assert_created(acquisition.apply_plan(plan, stream_opener=self.opener))
        return plan

    @staticmethod
    def assert_created(status: str) -> None:
        if status != "created":
            raise AssertionError(f"expected created acquisition, got {status}")

    def prepare(self) -> preparation.PreparedJob:
        if not self.acquisition_dir.exists():
            self.acquire()
        return preparation.build_job(self.manifest_path, self.acquisition_dir)


class ImportJobPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(pathlib.Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code: str, callback) -> preparation.ImportJobError:
        with self.assertRaises(preparation.ImportJobError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_happy_path_binds_source_and_emits_bounds(self) -> None:
        prepared = self.fixture.prepare()
        job = prepared.job
        self.assertEqual(job["schema"], preparation.JOB_SCHEMA)
        self.assertEqual(job["engine_contract"]["engine_version"], "5.3.2")
        self.assertEqual(job["engine_contract"]["destination_root"], preparation.DESTINATION_ROOT)
        asset = job["assets"][0]
        self.assertEqual(asset["destination_content_path"], preparation.DESTINATION_ROOT + "/fixture_asset")
        self.assertEqual(asset["expected_object_paths"], [])
        self.assertEqual(asset["source_bounds"]["dimensions"], [2.0, 2.0, 1.0])
        self.assertEqual(
            [entry["path"] for entry in asset["external_dependencies"]],
            ["fixture_asset/model.bin", "fixture_asset/textures/diff.jpg"],
        )
        self.assertFalse(job["execution_gate"]["content_imported"])

    def test_bad_acquisition_receipt_fails_closed(self) -> None:
        self.fixture.acquire()
        receipt_path = self.fixture.acquisition_dir / "acquisition-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["tree_sha256"] = "0" * 64
        receipt_path.write_bytes(preparation.canonical_json(receipt))
        receipt_path.chmod(0o600)
        self.assert_error(
            "UE_IMPORT_ACQUISITION_INVALID",
            lambda: preparation.build_job(self.fixture.manifest_path, self.fixture.acquisition_dir),
        )

    def test_bad_acquisition_tree_fails_closed(self) -> None:
        self.fixture.acquire()
        binary = self.fixture.acquisition_dir / "fixture_asset/model.bin"
        binary.write_bytes(b"X" * len(self.fixture.binary))
        binary.chmod(0o600)
        self.assert_error(
            "UE_IMPORT_ACQUISITION_INVALID",
            lambda: preparation.build_job(self.fixture.manifest_path, self.fixture.acquisition_dir),
        )

    def test_duplicate_gltf_json_key_is_rejected(self) -> None:
        self.fixture.raw_gltf = (
            b'{"asset":{"version":"2.0","version":"2.0"},'
            b'"buffers":[{"uri":"model.bin","byteLength":36}],'
            b'"images":[{"uri":"textures/diff.jpg"}],'
            b'"accessors":[{"count":3,"type":"VEC3","min":[0,0,0],"max":[1,1,1]}],'
            b'"meshes":[{"primitives":[{"attributes":{"POSITION":0}}]}]}'
        )
        self.fixture.refresh_manifest()
        self.assert_error("UE_IMPORT_GLTF_DUPLICATE_KEY", self.fixture.prepare)

    def test_traversal_remote_data_and_encoded_uris_are_rejected(self) -> None:
        for uri in ("../model.bin", "https://example.com/model.bin", "data:application/octet-stream;base64,AA==", "%2e%2e/model.bin", "folder\\model.bin"):
            with self.subTest(uri=uri):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = Fixture(pathlib.Path(temporary))
                    fixture.gltf["buffers"][0]["uri"] = uri
                    fixture.refresh_manifest()
                    self.assert_error("UE_IMPORT_GLTF_URI_INVALID", fixture.prepare)

    def test_unpinned_dependency_is_rejected_as_missing(self) -> None:
        self.fixture.gltf["images"][0]["uri"] = "textures/missing.jpg"
        self.fixture.refresh_manifest()
        self.assert_error("UE_IMPORT_DEPENDENCY_MISSING", self.fixture.prepare)

    def test_unreferenced_pinned_dependency_is_rejected_as_orphan(self) -> None:
        self.fixture.gltf["images"] = []
        self.fixture.refresh_manifest()
        self.assert_error("UE_IMPORT_DEPENDENCY_ORPHANED", self.fixture.prepare)

    def test_buffer_byte_length_must_match_pinned_file(self) -> None:
        self.fixture.gltf["buffers"][0]["byteLength"] += 1
        self.fixture.refresh_manifest()
        self.assert_error("UE_IMPORT_BUFFER_SIZE_MISMATCH", self.fixture.prepare)

    def test_missing_or_inverted_position_bounds_are_rejected(self) -> None:
        del self.fixture.gltf["accessors"][0]["min"]
        self.fixture.refresh_manifest()
        self.assert_error("UE_IMPORT_GLTF_BOUNDS_MISSING", self.fixture.prepare)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(pathlib.Path(temporary))
            fixture.gltf["accessors"][0]["min"] = [2, 0, 0]
            fixture.gltf["accessors"][0]["max"] = [1, 1, 1]
            fixture.refresh_manifest()
            self.assert_error("UE_IMPORT_GLTF_BOUNDS_INVALID", fixture.prepare)

    def test_job_bytes_are_deterministic(self) -> None:
        first = self.fixture.prepare()
        second = preparation.build_job(self.fixture.manifest_path, self.fixture.acquisition_dir)
        self.assertEqual(first.job_bytes, second.job_bytes)
        self.assertEqual(first.job_sha256, second.job_sha256)

    def test_dry_run_writes_nothing(self) -> None:
        self.fixture.acquire()
        before = sorted(path.relative_to(self.fixture.root) for path in self.fixture.root.rglob("*"))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = preparation.main(
                [
                    "--manifest",
                    str(self.fixture.manifest_path),
                    "--acquisition-dir",
                    str(self.fixture.acquisition_dir),
                    "--output-dir",
                    str(self.fixture.output_dir),
                ]
            )
        after = sorted(path.relative_to(self.fixture.root) for path in self.fixture.root.rglob("*"))
        self.assertEqual(code, 0)
        self.assertEqual(before, after)
        self.assertFalse(self.fixture.output_dir.exists())
        self.assertEqual(json.loads(stdout.getvalue())["status"], "dry_run")

    def test_apply_is_private_receipt_last_and_non_overwriting(self) -> None:
        prepared = self.fixture.prepare()
        receipt = preparation.publish_job(prepared, self.fixture.output_dir, "CHANGE-VISTA-IMPORT-001")
        self.assertEqual(stat.S_IMODE(self.fixture.output_dir.stat().st_mode), 0o700)
        self.assertEqual(
            sorted(path.name for path in self.fixture.output_dir.iterdir()),
            ["import-job.json", "preparation-receipt.json"],
        )
        for path in self.fixture.output_dir.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        receipt_on_disk = json.loads((self.fixture.output_dir / "preparation-receipt.json").read_text())
        self.assertEqual(receipt_on_disk, receipt)
        self.assertNotIn("CHANGE-VISTA-IMPORT-001", json.dumps(receipt_on_disk))
        self.assertFalse(receipt_on_disk["unreal_started"])
        self.assertFalse(receipt_on_disk["content_imported"])
        self.assert_error(
            "UE_IMPORT_OUTPUT_EXISTS",
            lambda: preparation.publish_job(prepared, self.fixture.output_dir, "CHANGE-VISTA-IMPORT-002"),
        )

    def test_publication_rejects_nonprivate_parent_and_secretish_approval(self) -> None:
        prepared = self.fixture.prepare()
        self.fixture.publication_parent.chmod(0o755)
        self.assert_error(
            "UE_IMPORT_OUTPUT_INVALID",
            lambda: preparation.publish_job(prepared, self.fixture.output_dir, "CHANGE-VISTA-IMPORT-003"),
        )
        self.fixture.publication_parent.chmod(0o700)
        self.assert_error(
            "UE_IMPORT_APPROVAL_INVALID",
            lambda: preparation.publish_job(prepared, self.fixture.output_dir, "token-secret-value"),
        )

    def test_symlinked_publication_parent_is_rejected(self) -> None:
        prepared = self.fixture.prepare()
        real = self.fixture.root / "real-parent"
        real.mkdir(mode=0o700)
        linked = self.fixture.root / "linked-parent"
        linked.symlink_to(real, target_is_directory=True)
        self.assert_error(
            "UE_IMPORT_OUTPUT_INVALID",
            lambda: preparation.publish_job(prepared, linked / "job-r1", "CHANGE-VISTA-IMPORT-004"),
        )

    def test_foreign_lock_is_preserved(self) -> None:
        prepared = self.fixture.prepare()
        lock = self.fixture.publication_parent / ".fixture-job-r1.lock"
        lock.write_text("owner", encoding="utf-8")
        lock.chmod(0o600)
        self.assert_error(
            "UE_IMPORT_OUTPUT_BUSY",
            lambda: preparation.publish_job(prepared, self.fixture.output_dir, "CHANGE-VISTA-IMPORT-005"),
        )
        self.assertEqual(lock.read_text(encoding="utf-8"), "owner")

    def test_atomic_no_replace_primitive_preserves_both_directories(self) -> None:
        source = self.fixture.publication_parent / "source"
        destination = self.fixture.publication_parent / "destination"
        source.mkdir(mode=0o700)
        destination.mkdir(mode=0o700)
        self.assert_error(
            "UE_IMPORT_OUTPUT_EXISTS",
            lambda: preparation._rename_directory_no_replace(source, destination),
        )
        self.assertTrue(source.is_dir())
        self.assertTrue(destination.is_dir())


if __name__ == "__main__":
    unittest.main()
