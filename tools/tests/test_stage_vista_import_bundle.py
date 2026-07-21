from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOLS_DIR.parent
FIXTURE_ROOT = pathlib.Path(__file__).resolve().parent / "fixtures" / "vista_staging"
sys.path.insert(0, str(TOOLS_DIR))

import stage_vista_import_bundle as staging  # noqa: E402


REVISION = "sanitized_round_r1"
SAMPLE_ID = "mmg_040"
PROVIDER = "sora2"
ATTEMPT = 7
RENDER_RELATIVE = "pipeline_v2/media/mmg_040/attempt_007/render_script.yaml"
DIALOGUE_RELATIVE = "verified/dialogue/mmg_040.attempt_007.no-oracle.json"
MEDIA_RELATIVE = "pipeline_v2/media/mmg_040/attempt_007/video.mp4"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def mp4_box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def make_test_mp4(*, duration_sec: int = 12, width: int = 1280, height: int = 720) -> bytes:
    ftyp = mp4_box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2mp41")
    mvhd_payload = (
        b"\x00\x00\x00\x00"
        + struct.pack(">II", 0, 0)
        + struct.pack(">II", 1000, duration_sec * 1000)
        + b"\x00" * 20
    )
    tkhd_payload = (
        b"\x00\x00\x00\x07"
        + b"\x00" * 68
        + struct.pack(">II", width << 16, height << 16)
    )
    moov = mp4_box(
        b"moov",
        mp4_box(b"mvhd", mvhd_payload)
        + mp4_box(b"trak", mp4_box(b"tkhd", tkhd_payload)),
    )
    return ftyp + moov + mp4_box(b"mdat", b"VISTA-SANITIZED-NO-FRAMES")


class StagingFixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.dataset = root / "dataset"
        self.output_parent = root / "outputs"
        self.output = self.output_parent / "mmg_040-attempt-007"
        self.dataset.mkdir(mode=0o700)
        self.output_parent.mkdir(mode=0o700)
        os.chmod(self.dataset, 0o700)
        os.chmod(self.output_parent, 0o700)

        self.render = self.dataset / RENDER_RELATIVE
        self.dialogue = self.dataset / DIALOGUE_RELATIVE
        self.media = self.dataset / MEDIA_RELATIVE
        self.render.parent.mkdir(parents=True, mode=0o700)
        self.dialogue.parent.mkdir(parents=True, mode=0o700)
        self.media.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        shutil.copyfile(FIXTURE_ROOT / "render_script.yaml", self.render)
        shutil.copyfile(FIXTURE_ROOT / "dialogue.no-oracle.json", self.dialogue)
        encoded_media = (FIXTURE_ROOT / "reference.mp4.b64").read_text(encoding="ascii")
        self.media.write_bytes(base64.b64decode(encoded_media))
        self._make_private_tree(self.dataset)

        self.verified_dir = self.dataset / "verified"
        self.verified_dir.mkdir(mode=0o700, exist_ok=True)
        self.manifest = self.verified_dir / "manifest.json"
        self.record = json.loads((FIXTURE_ROOT / "verified-record.json").read_text(encoding="utf-8"))
        self.write_manifest()
        self.jsonl = self.verified_dir / "records.jsonl"
        self.write_jsonl()
        self._make_private_tree(self.dataset)

    @staticmethod
    def _make_private_tree(root: pathlib.Path) -> None:
        for current, directories, files in os.walk(root):
            os.chmod(current, 0o700)
            for directory in directories:
                os.chmod(pathlib.Path(current) / directory, 0o700)
            for filename in files:
                os.chmod(pathlib.Path(current) / filename, 0o600)

    def write_manifest(self) -> None:
        value = {
            "schema": staging.VERIFIED_MANIFEST_SCHEMA,
            "dataset_revision": REVISION,
            "records": [self.record],
        }
        self.manifest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.manifest.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.manifest, 0o600)

    def write_jsonl(self) -> None:
        self.jsonl.write_text(json.dumps(self.record, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(self.jsonl, 0o600)

    def update_evidence(self, role: str, path: pathlib.Path) -> None:
        content = path.read_bytes()
        declaration = self.record["selected_attempt"][role]
        declaration["bytes"] = len(content)
        declaration["sha256"] = sha256(content)
        self.write_manifest()
        self.write_jsonl()

    def args(self, *, source_format: str = "manifest", apply: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            dataset_root=str(self.dataset),
            verified_source="verified/manifest.json" if source_format == "manifest" else "verified/records.jsonl",
            verified_format=source_format,
            dataset_revision=REVISION,
            sample_id=SAMPLE_ID,
            provider=PROVIDER,
            attempt=ATTEMPT,
            render_script=RENDER_RELATIVE,
            dialogue_no_oracle=DIALOGUE_RELATIVE,
            media=MEDIA_RELATIVE,
            output_dir=str(self.output),
            apply=apply,
        )


class VistaImportBundleStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.fixture = StagingFixture(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code: str, callback) -> staging.VistaStagingError:
        with self.assertRaises(staging.VistaStagingError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_dry_run_then_atomic_apply_is_private_and_idempotent(self) -> None:
        plan = staging.build_stage_plan(self.fixture.args())
        self.assertFalse(self.fixture.output.exists())
        self.assertEqual(plan.report["validation"]["scene_action_timestamps_sec"], [0, 2, 5, 9])
        self.assertEqual(plan.report["bundle"]["declared_importer_file_count"], 3)
        self.assertFalse(plan.report["bundle"]["media_sidecar_declared_to_importer"])

        self.assertEqual(staging.apply_stage_plan(plan), "created")
        self.assertEqual(staging.apply_stage_plan(staging.build_stage_plan(self.fixture.args())), "idempotent")
        expected_files = {
            "manifest.json",
            "render_script.yaml",
            "dialogue.no-oracle.json",
            "media.descriptor.json",
            "media/reference.mp4",
            "validation-report.json",
            "registry-snippet.json",
        }
        observed_files = {
            path.relative_to(self.fixture.output).as_posix()
            for path in self.fixture.output.rglob("*")
            if path.is_file()
        }
        self.assertEqual(observed_files, expected_files)
        self.assertEqual(stat.S_IMODE(self.fixture.output.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.fixture.output / "media").stat().st_mode), 0o700)
        for relative in expected_files:
            self.assertEqual(stat.S_IMODE((self.fixture.output / relative).stat().st_mode), 0o600)

        manifest = json.loads((self.fixture.output / "manifest.json").read_text())
        self.assertEqual(manifest["schema"], "vista-import-source/v1")
        self.assertEqual(len(manifest["files"]), 3)
        self.assertEqual(
            {entry["role"] for entry in manifest["files"]},
            {"render_script", "dialogue_no_oracle", "media_descriptor"},
        )
        descriptor = json.loads((self.fixture.output / "media.descriptor.json").read_text())
        self.assertFalse(descriptor["media"]["bundled"])
        self.assertEqual(descriptor["media"]["logical_ref"], "media/reference.mp4")
        sidecar = (self.fixture.output / "media/reference.mp4").read_bytes()
        self.assertEqual(sha256(sidecar), descriptor["media"]["sha256"])
        self.assertEqual(len(sidecar), descriptor["media"]["bytes"])

        report = json.loads((self.fixture.output / "validation-report.json").read_text())
        self.assertEqual(
            staging.bundle_tree_digest(report["bundle"]["entries"]),
            report["bundle"]["digest_sha256"],
        )
        registry = json.loads((self.fixture.output / "registry-snippet.json").read_text())
        self.assertEqual(registry[REVISION]["root"], str(self.fixture.output))

    def test_staged_bundle_is_accepted_by_existing_node_importer(self) -> None:
        importer_module = REPOSITORY_ROOT / "simworld_studio_workspace" / "web" / "server" / "vista-importer.js"
        dependency_probe = subprocess.run(
            ["node", "-e", "require.resolve('yaml')"],
            cwd=importer_module.parent,
            check=False,
            capture_output=True,
            text=True,
        )
        if dependency_probe.returncode != 0:
            self.skipTest("server node_modules is not installed in this isolated worktree")
        plan = staging.build_stage_plan(self.fixture.args())
        staging.apply_stage_plan(plan)
        script = """
const { createVistaImporter } = require(process.argv[1]);
const root = process.argv[2];
const revision = process.argv[3];
const importer = createVistaImporter({ registry: { [revision]: { root } } });
importer.validateBundle({
  datasetRevision: revision,
  sampleId: "mmg_040",
  attempt: 7,
  scenarioType: "multimodal_grounded",
}).then((value) => process.stdout.write(JSON.stringify(value))).catch((error) => {
  process.stderr.write(`${error.code || "ERROR"}: ${error.message}`);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node", "-e", script, str(importer_module), str(self.fixture.output), REVISION],
            cwd=importer_module.parent,
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertTrue(result["valid"])
        self.assertEqual(result["schema"], "vista-import-source/v1")
        self.assertEqual(result["sample_id"], SAMPLE_ID)

    def test_explicit_jsonl_projection_selects_the_same_record(self) -> None:
        manifest_plan = staging.build_stage_plan(self.fixture.args(source_format="manifest"))
        jsonl_plan = staging.build_stage_plan(self.fixture.args(source_format="jsonl"))
        self.assertEqual(
            manifest_plan.report["selection"]["selection_sha256"],
            jsonl_plan.report["selection"]["selection_sha256"],
        )
        self.assertEqual(
            manifest_plan.report["bundle"]["digest_sha256"],
            jsonl_plan.report["bundle"]["digest_sha256"],
        )

    def test_selected_file_checksum_tampering_fails_closed(self) -> None:
        self.fixture.render.write_text(
            self.fixture.render.read_text(encoding="utf-8") + "# changed\n",
            encoding="utf-8",
        )
        self.assert_error(
            "VISTA_STAGING_CHECKSUM_MISMATCH",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )

    def test_dialogue_join_identity_and_restricted_fields_fail_closed(self) -> None:
        dialogue = json.loads(self.fixture.dialogue.read_text(encoding="utf-8"))
        dialogue["source"]["case_scope"] = "different_case"
        self.fixture.dialogue.write_text(json.dumps(dialogue) + "\n", encoding="utf-8")
        self.fixture.update_evidence("dialogue_no_oracle", self.fixture.dialogue)
        self.assert_error(
            "VISTA_STAGING_IDENTITY_MISMATCH",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )

        dialogue["source"]["case_scope"] = "sanitized_case_040"
        dialogue["privilege"]["oracle_label"] = "do-not-copy"
        self.fixture.dialogue.write_text(json.dumps(dialogue) + "\n", encoding="utf-8")
        self.fixture.update_evidence("dialogue_no_oracle", self.fixture.dialogue)
        error = self.assert_error(
            "VISTA_STAGING_RESTRICTED_FIELD",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )
        self.assertNotIn("do-not-copy", json.dumps(error.public_dict()))

    def test_render_duration_and_timeline_must_match_verified_record(self) -> None:
        source = self.fixture.render.read_text(encoding="utf-8")
        self.fixture.render.write_text(source.replace("Duration_sec: 12", "Duration_sec: 11"), encoding="utf-8")
        self.fixture.update_evidence("render_script", self.fixture.render)
        self.assert_error(
            "VISTA_STAGING_DURATION_MISMATCH",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )

        self.fixture.render.write_text(source.replace("[00:02]", "[00:00]"), encoding="utf-8")
        self.fixture.update_evidence("render_script", self.fixture.render)
        self.assert_error(
            "VISTA_STAGING_TIMELINE_INVALID",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )

    def test_render_yaml_aliases_and_reconstruction_review_fields_are_rejected(self) -> None:
        source = self.fixture.render.read_text(encoding="utf-8")
        self.fixture.render.write_text(source + "Unsafe_Alias: &private copied\n", encoding="utf-8")
        self.fixture.update_evidence("render_script", self.fixture.render)
        self.assert_error(
            "VISTA_STAGING_YAML_UNSAFE",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )

        self.fixture.render.write_text(source + "Review_Note: private\n", encoding="utf-8")
        self.fixture.update_evidence("render_script", self.fixture.render)
        self.assert_error(
            "VISTA_STAGING_RESTRICTED_FIELD",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )

    def test_media_binary_duration_and_dimensions_are_verified(self) -> None:
        self.fixture.media.write_bytes(make_test_mp4(duration_sec=11))
        self.fixture.update_evidence("media", self.fixture.media)
        self.assert_error(
            "VISTA_STAGING_DURATION_MISMATCH",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )

        self.fixture.media.write_bytes(make_test_mp4(width=640, height=360))
        self.fixture.update_evidence("media", self.fixture.media)
        self.assert_error(
            "VISTA_STAGING_MEDIA_MISMATCH",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )

    def test_cli_artifact_path_must_match_authoritative_selected_attempt(self) -> None:
        args = self.fixture.args()
        args.render_script = DIALOGUE_RELATIVE
        self.assert_error(
            "VISTA_STAGING_PATH_INVALID",
            lambda: staging.build_stage_plan(args),
        )

        args = self.fixture.args()
        args.render_script = "pipeline_v2/media/mmg_040/other/render_script.yaml"
        other = self.fixture.dataset / args.render_script
        other.parent.mkdir(parents=True)
        shutil.copyfile(self.fixture.render, other)
        self.assert_error(
            "VISTA_STAGING_IDENTITY_MISMATCH",
            lambda: staging.build_stage_plan(args),
        )

    def test_symlinks_and_dataset_contained_output_are_rejected(self) -> None:
        outside = self.root / "outside.yaml"
        shutil.copyfile(self.fixture.render, outside)
        self.fixture.render.unlink()
        self.fixture.render.symlink_to(outside)
        self.assert_error(
            "VISTA_STAGING_SYMLINK_REJECTED",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )

        self.fixture.render.unlink()
        shutil.copyfile(FIXTURE_ROOT / "render_script.yaml", self.fixture.render)
        args = self.fixture.args()
        args.output_dir = str(self.fixture.dataset / "generated" / "bundle")
        self.assert_error(
            "VISTA_STAGING_OUTPUT_INVALID",
            lambda: staging.build_stage_plan(args),
        )

    def test_existing_different_output_is_never_overwritten(self) -> None:
        plan = staging.build_stage_plan(self.fixture.args())
        self.assertEqual(staging.apply_stage_plan(plan), "created")
        report_path = self.fixture.output / "validation-report.json"
        report_path.write_text("{}\n", encoding="utf-8")
        os.chmod(report_path, 0o600)
        before = report_path.read_bytes()
        self.assert_error(
            "VISTA_STAGING_OUTPUT_CONFLICT",
            lambda: staging.apply_stage_plan(staging.build_stage_plan(self.fixture.args())),
        )
        self.assertEqual(report_path.read_bytes(), before)

    def test_output_parent_must_be_private_and_output_name_safe(self) -> None:
        os.chmod(self.fixture.output_parent, 0o755)
        self.assert_error(
            "VISTA_STAGING_OUTPUT_INVALID",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )
        os.chmod(self.fixture.output_parent, 0o700)
        args = self.fixture.args()
        args.output_dir = str(self.fixture.output_parent / "unsafe bundle name")
        self.assert_error(
            "VISTA_STAGING_OUTPUT_INVALID",
            lambda: staging.build_stage_plan(args),
        )

    def test_verified_projection_rejects_unselected_and_restricted_rows(self) -> None:
        self.fixture.record["selected_attempt"]["selected"] = False
        self.fixture.write_manifest()
        self.assert_error(
            "VISTA_STAGING_ATTEMPT_INVALID",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )

        self.fixture.record["selected_attempt"]["selected"] = True
        self.fixture.record["review_note"] = "private-review-value"
        self.fixture.write_manifest()
        error = self.assert_error(
            "VISTA_STAGING_RESTRICTED_FIELD",
            lambda: staging.build_stage_plan(self.fixture.args()),
        )
        self.assertNotIn("private-review-value", json.dumps(error.public_dict()))

    def test_cli_failure_is_machine_readable_and_does_not_create_output(self) -> None:
        command = [
            sys.executable,
            str(TOOLS_DIR / "stage_vista_import_bundle.py"),
            "--dataset-root",
            str(self.fixture.dataset),
            "--verified-source",
            "verified/manifest.json",
            "--verified-format",
            "manifest",
            "--dataset-revision",
            REVISION,
            "--sample-id",
            SAMPLE_ID,
            "--provider",
            PROVIDER,
            "--attempt",
            "8",
            "--render-script",
            RENDER_RELATIVE,
            "--dialogue-no-oracle",
            DIALOGUE_RELATIVE,
            "--media",
            MEDIA_RELATIVE,
            "--output-dir",
            str(self.fixture.output),
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        failure = json.loads(completed.stderr)
        self.assertFalse(failure["valid"])
        self.assertEqual(failure["error"]["code"], "VISTA_STAGING_SELECTION_NOT_FOUND")
        self.assertFalse(self.fixture.output.exists())

        missing = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "stage_vista_import_bundle.py"), "--dataset-root", str(self.fixture.dataset)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing.returncode, 2)
        argument_failure = json.loads(missing.stderr)
        self.assertEqual(argument_failure["error"]["code"], "VISTA_STAGING_ARGUMENT_INVALID")


if __name__ == "__main__":
    unittest.main()
