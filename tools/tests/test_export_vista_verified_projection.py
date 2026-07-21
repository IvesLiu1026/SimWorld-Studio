from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pathlib
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

import yaml


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import export_vista_verified_projection as exporter  # noqa: E402
import stage_vista_import_bundle as staging  # noqa: E402


REVISION = "synthetic_round_r1"
VISUAL_ID = "mmg_040"
CASE_SCOPE = "synthetic_safety_040"
SCENARIO_TYPE = "multimodal_grounded"
PROVIDER = "sora2"
ATTEMPT = 7
ROW_ID = f"{VISUAL_ID}__{SCENARIO_TYPE}::{CASE_SCOPE}::{PROVIDER}::attempt_{ATTEMPT:03d}"

NO_ORACLE_RELATIVE = "handoff/no-oracle.jsonl"
LEDGER_RELATIVE = "case/pipeline_v2/media/video_attempts.json"
RENDER_RELATIVE = "case/pipeline_v2/media/render_script.yaml"
SUMMARY_RELATIVE = "case/pipeline_v2/media/attempts/attempt_007/media_summary.json"
MEDIA_RELATIVE = "case/pipeline_v2/media/attempts/attempt_007/video.mp4"

OMITTED_URL_SECRET = "https://private.invalid/object?X-Amz-Signature=DO_NOT_EXPORT_URL"
OMITTED_REVIEW_SECRET = "DO_NOT_EXPORT_REVIEW"
OMITTED_INTERVENTION_SECRET = "DO_NOT_EXPORT_INTERVENTION"
OMITTED_GENERATION_SECRET = "DO_NOT_EXPORT_GENERATION"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def mp4_box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def make_test_mp4(*, duration_sec: int = 12, width: int = 1280, height: int = 720) -> bytes:
    ftyp = mp4_box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2mp41")
    mvhd = (
        b"\x00\x00\x00\x00"
        + struct.pack(">II", 0, 0)
        + struct.pack(">II", 1000, duration_sec * 1000)
        + b"\x00" * 20
    )
    tkhd = b"\x00\x00\x00\x07" + b"\x00" * 68 + struct.pack(">II", width << 16, height << 16)
    moov = mp4_box(b"moov", mp4_box(b"mvhd", mvhd) + mp4_box(b"trak", mp4_box(b"tkhd", tkhd)))
    return ftyp + moov + mp4_box(b"mdat", b"SYNTHETIC-NO-VIDEO-FRAMES")


class ProjectionFixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.dataset = root / "dataset"
        self.outputs = root / "outputs"
        self.output = self.outputs / "verified-mmg-040"
        self.stage_parent = root / "staged"
        self.stage_output = self.stage_parent / "import-mmg-040"
        for path in (self.dataset, self.outputs, self.stage_parent):
            path.mkdir(mode=0o700)
            os.chmod(path, 0o700)

        self.no_oracle_path = self.dataset / NO_ORACLE_RELATIVE
        self.ledger_path = self.dataset / LEDGER_RELATIVE
        self.render_path = self.dataset / RENDER_RELATIVE
        self.summary_path = self.dataset / SUMMARY_RELATIVE
        self.media_path = self.dataset / MEDIA_RELATIVE
        for path in (self.no_oracle_path, self.ledger_path, self.render_path, self.summary_path, self.media_path):
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        self.row = self.make_row()
        self.ledger = self.make_ledger()
        self.render = self.make_render()
        self.summary = self.make_summary()
        self.write_all()

    def absolute(self, relative: str) -> str:
        return str(self.dataset.joinpath(*pathlib.PurePosixPath(relative).parts))

    def make_row(self) -> dict:
        return {
            "row_id": ROW_ID,
            "dataset_source": REVISION,
            "handoff_split": "synthetic",
            "visual_id": VISUAL_ID,
            "pairing_id": f"{VISUAL_ID}__{SCENARIO_TYPE}",
            "case_scope": CASE_SCOPE,
            "scenario_type": SCENARIO_TYPE,
            "review_group": None,
            "video_model_key": PROVIDER,
            "video_attempt_index": ATTEMPT,
            "video_path": self.absolute(MEDIA_RELATIVE),
            "video_url": OMITTED_URL_SECRET,
            "video_r2_url": OMITTED_URL_SECRET,
            "video_r2_object_key": "private/object/key/DO_NOT_EXPORT",
            "video_r2_url_expires_in_seconds": 3600,
            "video_r2_url_for_model_api": OMITTED_URL_SECRET,
            "video_r2_object_key_for_model_api": "private/provider/key/DO_NOT_EXPORT",
            "video_r2_url_for_model_api_expires_in_seconds": 1800,
            "video_r2_url_for_model_api_is_provider_normalized": True,
            "reference_image_path": "/private/source/DO_NOT_EXPORT_REFERENCE.png",
            "reference_image_url": None,
            "duration_seconds": None,
            "dialogue_en": [
                {"turn_id": 1, "role": "context", "speaker": "other_person", "text": "Please get the box from the shelf."},
                {"turn_id": 2, "role": "context", "speaker": "user", "text": "I am choosing a stable access option."},
            ],
            "dialogue_time_sec": 0,
        }

    def attempt_entry(self, index: int, *, selected: bool) -> dict:
        suffix = f"attempt_{index:03d}"
        return {
            "attempt_index": index,
            "script_revision": "synthetic-v1",
            "script_origin": "approved_script",
            "trigger": OMITTED_GENERATION_SECRET,
            "status": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:12Z",
            "reference_image_path": "/private/reference.png",
            "video_path": self.absolute(MEDIA_RELATIVE if selected else f"case/pipeline_v2/media/attempts/{suffix}/video.mp4"),
            "media_summary_path": self.absolute(SUMMARY_RELATIVE if selected else f"case/pipeline_v2/media/attempts/{suffix}/media_summary.json"),
            "triggered_by": "private-operator",
            "review_decision": "accepted",
            "reviewed_by": "private-reviewer",
            "reviewed_at": "2026-01-01T01:00:00Z",
            "review_note": OMITTED_REVIEW_SECRET,
            "selected_for_export": selected,
            "usable_until_seconds": None,
            **({"usable_from_seconds": None} if selected else {}),
        }

    def make_ledger(self) -> dict:
        return {
            "schema_version": "1.0",
            "artifact_root": self.absolute("case/pipeline_v2/media"),
            "attempts": [self.attempt_entry(6, selected=False), self.attempt_entry(7, selected=True)],
        }

    @staticmethod
    def make_render() -> dict:
        return {
            "Global_Metadata": {
                "Title": "Synthetic shelf retrieval",
                "Perspective": "First-person egocentric",
                "Environment": "A compact office with a cabinet, rolling chair, and stable stool.",
                "Lighting": "Neutral overhead office lighting.",
                "Emotional_Tone": OMITTED_GENERATION_SECRET,
                "Duration_sec": 12,
                "Dialogue_Delivery": OMITTED_GENERATION_SECRET,
                "Speech_Policy": OMITTED_GENERATION_SECRET,
                "Audio_Policy": OMITTED_GENERATION_SECRET,
                "Viewpoint_Contract": {
                    "POV_Mode": "first_person",
                    "Camera_Rig": "head",
                    "Allowed_Body_Visibility": ["hands"],
                    "Face_Visibility": "forbidden",
                    "Forbidden_Views": ["third_person"],
                },
            },
            "Camera_Continuity": {
                "Position": "Head-mounted eye line",
                "Height": "Adult eye level",
                "Angle": "Natural forward view",
                "Motion": "One uninterrupted take",
                "Framing": "Hands only, never the face",
                "Head_Motion_Profile": OMITTED_GENERATION_SECRET,
                "Forbidden_Camera_Behaviors": [OMITTED_GENERATION_SECRET],
            },
            "Scene": {
                "Key_Visual_Elements": ["rolling chair", "stable stool", "box on a cabinet"],
                "Description": "The wearer considers how to reach the box.",
                "Actions": [
                    "[00:00] Look at the box and both access options.",
                    "[00:02] Pull the rolling chair toward the cabinet.",
                    "[00:05] Brace a hand and pause before stepping up.",
                    "[00:09] Hold at the unresolved decision point.",
                ],
                "Dialogue": [],
                "Emotion": OMITTED_GENERATION_SECRET,
            },
            "Exit_State": {"Action": "End before climbing.", "Audio": OMITTED_GENERATION_SECRET},
            "Intervention_Cues": {
                "Signal_State": OMITTED_INTERVENTION_SECRET,
                "Signal_Location": OMITTED_INTERVENTION_SECRET,
                "User_Awareness": OMITTED_INTERVENTION_SECRET,
                "Reasoning": OMITTED_INTERVENTION_SECRET,
                "Trigger_Condition": OMITTED_INTERVENTION_SECRET,
            },
        }

    def make_summary(self) -> dict:
        return {
            "status": "ok",
            "artifact_root": self.absolute("case/pipeline_v2/media"),
            "case_id": CASE_SCOPE,
            "model_key": PROVIDER,
            "attempt_index": ATTEMPT,
            "trigger": OMITTED_GENERATION_SECRET,
            "render_script_path": self.absolute(RENDER_RELATIVE),
            "reference_image_path": "/private/reference.png",
            "video_path": self.absolute(MEDIA_RELATIVE),
            "video_prompt": OMITTED_GENERATION_SECRET,
            "video_backend": PROVIDER,
            "target_size": "1280x720",
            "seconds": 12,
        }

    def write_json(self, path: pathlib.Path, value: object) -> None:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    def write_all(self) -> None:
        unrelated = copy.deepcopy(self.row)
        unrelated["row_id"] = "other__multimodal_grounded::other_case::sora2::attempt_001"
        self.no_oracle_path.write_text(
            json.dumps(unrelated, separators=(",", ":")) + "\n" + json.dumps(self.row, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(self.no_oracle_path, 0o600)
        self.write_json(self.ledger_path, self.ledger)
        self.render_path.write_text(yaml.safe_dump(self.render, sort_keys=False), encoding="utf-8")
        os.chmod(self.render_path, 0o600)
        self.write_json(self.summary_path, self.summary)
        self.media_path.write_bytes(make_test_mp4())
        os.chmod(self.media_path, 0o600)

    def args(self, *, apply: bool = False, approval: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            dataset_root=str(self.dataset),
            no_oracle_jsonl=NO_ORACLE_RELATIVE,
            attempt_ledger=LEDGER_RELATIVE,
            raw_render_script=RENDER_RELATIVE,
            media_summary=SUMMARY_RELATIVE,
            media=MEDIA_RELATIVE,
            dataset_revision=REVISION,
            row_id=ROW_ID,
            visual_id=VISUAL_ID,
            case_scope=CASE_SCOPE,
            scenario_type=SCENARIO_TYPE,
            provider=PROVIDER,
            attempt=ATTEMPT,
            output_dir=str(self.output),
            owner_approval_ref=approval,
            apply=apply,
        )


class VistaVerifiedProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.fixture = ProjectionFixture(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_projection_error(self, code: str, callback):
        with self.assertRaises((exporter.VistaProjectionError, staging.VistaStagingError)) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_default_dry_run_is_read_only_and_report_contains_no_raw_values(self) -> None:
        plan = exporter.build_projection_plan(self.fixture.args())
        self.assertFalse(self.fixture.output.exists())
        self.assertIsNone(plan.approval_reference_sha256)
        self.assertEqual(plan.report["schema"], exporter.REPORT_SCHEMA)
        serialized = json.dumps(plan.report, sort_keys=True)
        for forbidden in (
            OMITTED_URL_SECRET,
            OMITTED_REVIEW_SECRET,
            OMITTED_INTERVENTION_SECRET,
            OMITTED_GENERATION_SECRET,
            str(self.fixture.dataset),
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            {entry["path"] for entry in plan.report["projection"]["entries"]},
            {exporter.OUTPUT_MANIFEST, exporter.OUTPUT_RENDER, exporter.OUTPUT_DIALOGUE, exporter.OUTPUT_MEDIA},
        )

    def test_sanitized_outputs_use_strict_allowlists(self) -> None:
        plan = exporter.build_projection_plan(self.fixture.args())
        render = yaml.safe_load(plan.small_files[exporter.OUTPUT_RENDER])
        self.assertEqual(set(render), {"Global_Metadata", "Camera_Continuity", "Scene", "Exit_State"})
        self.assertEqual(
            set(render["Global_Metadata"]),
            {"Title", "Perspective", "Environment", "Lighting", "Duration_sec"},
        )
        self.assertEqual(render["Scene"]["Dialogue"], [])
        self.assertNotIn("Intervention_Cues", render)
        dialogue = json.loads(plan.small_files[exporter.OUTPUT_DIALOGUE])
        self.assertEqual(set(dialogue), {"schema", "profile", "privilege", "source", "turns"})
        self.assertTrue(dialogue["privilege"]["evaluation_input_allowed"])
        all_output = b"\n".join(plan.small_files.values()).decode("utf-8")
        for forbidden in (OMITTED_URL_SECRET, OMITTED_REVIEW_SECRET, OMITTED_INTERVENTION_SECRET, OMITTED_GENERATION_SECRET):
            self.assertNotIn(forbidden, all_output)

    def test_apply_requires_approval_then_is_private_and_idempotent(self) -> None:
        dry_plan = exporter.build_projection_plan(self.fixture.args())
        self.assert_projection_error("VISTA_PROJECTION_OWNER_APPROVAL_REQUIRED", lambda: exporter.apply_projection(dry_plan))

        args = self.fixture.args(apply=True, approval="VISTA-DATA-APPROVAL-001")
        plan = exporter.build_projection_plan(args)
        self.assertEqual(exporter.apply_projection(plan), "created")
        self.assertEqual(exporter.apply_projection(exporter.build_projection_plan(args)), "idempotent")
        self.assertEqual(stat.S_IMODE(self.fixture.output.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.fixture.output / "media").stat().st_mode), 0o700)
        observed = {path.relative_to(self.fixture.output).as_posix() for path in self.fixture.output.rglob("*") if path.is_file()}
        self.assertEqual(observed, exporter.EXPECTED_FILES)
        for relative in observed:
            target = self.fixture.output / relative
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.stat().st_nlink, 1)
        report = json.loads((self.fixture.output / exporter.OUTPUT_REPORT).read_text(encoding="utf-8"))
        self.assertEqual(report["owner_approval_reference_sha256"], sha256(b"VISTA-DATA-APPROVAL-001"))
        self.assertNotIn("VISTA-DATA-APPROVAL-001", json.dumps(report))

    def test_approval_reference_rejects_urls_and_email_addresses(self) -> None:
        for approval in ("https://tracker.invalid/VISTA-001", "owner@example.invalid"):
            with self.subTest(approval=approval):
                self.assert_projection_error(
                    "VISTA_PROJECTION_OWNER_APPROVAL_INVALID",
                    lambda approval=approval: exporter.build_projection_plan(
                        self.fixture.args(apply=True, approval=approval)
                    ),
                )

    def test_projection_is_directly_compatible_with_staging_adapter(self) -> None:
        projection_args = self.fixture.args(apply=True, approval="VISTA-DATA-APPROVAL-001")
        exporter.apply_projection(exporter.build_projection_plan(projection_args))
        stage_args = argparse.Namespace(
            dataset_root=str(self.fixture.output),
            verified_source=exporter.OUTPUT_MANIFEST,
            verified_format="manifest",
            dataset_revision=REVISION,
            sample_id=VISUAL_ID,
            provider=PROVIDER,
            attempt=ATTEMPT,
            render_script=exporter.OUTPUT_RENDER,
            dialogue_no_oracle=exporter.OUTPUT_DIALOGUE,
            media=exporter.OUTPUT_MEDIA,
            output_dir=str(self.fixture.stage_output),
            apply=False,
        )
        stage_plan = staging.build_stage_plan(stage_args)
        self.assertEqual(stage_plan.report["validation"]["scene_action_timestamps_sec"], [0, 2, 5, 9])
        self.assertEqual(staging.apply_stage_plan(stage_plan), "created")

    def test_schema_drift_fails_closed_for_each_structured_source(self) -> None:
        cases = (
            ("row", lambda: self.fixture.row.__setitem__("oracle_label", "secret")),
            ("ledger", lambda: self.fixture.ledger.__setitem__("new_private_field", "secret")),
            ("render", lambda: self.fixture.render.__setitem__("Generation_Metadata", {"secret": True})),
            ("summary", lambda: self.fixture.summary.__setitem__("new_generation_field", "secret")),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                fixture = ProjectionFixture(self.root / label)
                self.fixture = fixture
                mutate = {
                    "row": lambda: fixture.row.__setitem__("oracle_label", "secret"),
                    "ledger": lambda: fixture.ledger.__setitem__("new_private_field", "secret"),
                    "render": lambda: fixture.render.__setitem__("Generation_Metadata", {"secret": True}),
                    "summary": lambda: fixture.summary.__setitem__("new_generation_field", "secret"),
                }[label]
                mutate()
                fixture.write_all()
                self.assert_projection_error("VISTA_PROJECTION_SCHEMA_DRIFT", lambda: exporter.build_projection_plan(fixture.args()))

    def test_duplicate_json_keys_and_yaml_keys_are_rejected(self) -> None:
        source = self.fixture.summary_path.read_text(encoding="utf-8").rstrip()
        self.fixture.summary_path.write_text(source[:-1] + ',"seconds":12}\n', encoding="utf-8")
        self.assert_projection_error("VISTA_PROJECTION_JSON_INVALID", lambda: exporter.build_projection_plan(self.fixture.args()))

        fixture = ProjectionFixture(self.root / "yaml-duplicate")
        fixture.render_path.write_text(fixture.render_path.read_text(encoding="utf-8") + "Scene: {}\n", encoding="utf-8")
        self.assert_projection_error("VISTA_PROJECTION_YAML_INVALID", lambda: exporter.build_projection_plan(fixture.args()))

    def test_selected_attempt_must_be_unique_completed_and_exact(self) -> None:
        self.fixture.ledger["attempts"][0]["selected_for_export"] = True
        self.fixture.write_all()
        self.assert_projection_error("VISTA_PROJECTION_SELECTION_INVALID", lambda: exporter.build_projection_plan(self.fixture.args()))

        fixture = ProjectionFixture(self.root / "wrong-status")
        fixture.ledger["attempts"][1]["status"] = "failed"
        fixture.write_all()
        self.assert_projection_error("VISTA_PROJECTION_SELECTION_INVALID", lambda: exporter.build_projection_plan(fixture.args()))

    def test_identity_and_declared_paths_are_joined_exactly(self) -> None:
        self.fixture.row["case_scope"] = "different_case"
        self.fixture.write_all()
        self.assert_projection_error("VISTA_PROJECTION_IDENTITY_MISMATCH", lambda: exporter.build_projection_plan(self.fixture.args()))

        fixture = ProjectionFixture(self.root / "wrong-path")
        fixture.ledger["attempts"][1]["video_path"] = fixture.absolute(RENDER_RELATIVE)
        fixture.write_all()
        self.assert_projection_error("VISTA_PROJECTION_PATH_MISMATCH", lambda: exporter.build_projection_plan(fixture.args()))

    def test_symlink_source_and_output_inside_dataset_are_rejected(self) -> None:
        real = self.root / "outside-summary.json"
        real.write_bytes(self.fixture.summary_path.read_bytes())
        self.fixture.summary_path.unlink()
        self.fixture.summary_path.symlink_to(real)
        self.assert_projection_error("VISTA_STAGING_SYMLINK_REJECTED", lambda: exporter.build_projection_plan(self.fixture.args()))

        fixture = ProjectionFixture(self.root / "inside-output")
        args = fixture.args()
        parent = fixture.dataset / "generated"
        parent.mkdir(mode=0o700)
        args.output_dir = str(parent / "projection")
        self.assert_projection_error("VISTA_STAGING_OUTPUT_INVALID", lambda: exporter.build_projection_plan(args))

    def test_mp4_metadata_must_match_summary_and_render(self) -> None:
        self.fixture.media_path.write_bytes(make_test_mp4(duration_sec=11))
        self.assert_projection_error("VISTA_PROJECTION_MEDIA_MISMATCH", lambda: exporter.build_projection_plan(self.fixture.args()))

        fixture = ProjectionFixture(self.root / "dimensions")
        fixture.media_path.write_bytes(make_test_mp4(width=640, height=360))
        self.assert_projection_error("VISTA_PROJECTION_MEDIA_MISMATCH", lambda: exporter.build_projection_plan(fixture.args()))

    def test_emitted_dialogue_rejects_url_or_absolute_path_leakage(self) -> None:
        self.fixture.row["dialogue_en"][0]["text"] = "Read https://private.invalid/signed before continuing."
        self.fixture.write_all()
        self.assert_projection_error("VISTA_PROJECTION_LEAKAGE_REJECTED", lambda: exporter.build_projection_plan(self.fixture.args()))

        fixture = ProjectionFixture(self.root / "path-leak")
        fixture.render["Scene"]["Description"] = "Use /home/private/operator/source.json to reconstruct this."
        fixture.write_all()
        self.assert_projection_error("VISTA_PROJECTION_LEAKAGE_REJECTED", lambda: exporter.build_projection_plan(fixture.args()))

    def test_existing_different_output_is_never_overwritten(self) -> None:
        args = self.fixture.args(apply=True, approval="VISTA-DATA-APPROVAL-001")
        exporter.apply_projection(exporter.build_projection_plan(args))
        report = self.fixture.output / exporter.OUTPUT_REPORT
        report.write_text("{}\n", encoding="utf-8")
        os.chmod(report, 0o600)
        before = report.read_bytes()
        self.assert_projection_error("VISTA_PROJECTION_OUTPUT_CONFLICT", lambda: exporter.apply_projection(exporter.build_projection_plan(args)))
        self.assertEqual(report.read_bytes(), before)

    def test_source_change_between_plan_and_apply_fails(self) -> None:
        args = self.fixture.args(apply=True, approval="VISTA-DATA-APPROVAL-001")
        plan = exporter.build_projection_plan(args)
        self.fixture.summary["video_prompt"] = "changed-but-still-omitted"
        self.fixture.write_json(self.fixture.summary_path, self.fixture.summary)
        self.assert_projection_error("VISTA_PROJECTION_SOURCE_CHANGED", lambda: exporter.apply_projection(plan))
        self.assertFalse(self.fixture.output.exists())

    def test_cli_failure_is_machine_readable_and_never_echoes_sensitive_values(self) -> None:
        command = [
            sys.executable,
            str(TOOLS_DIR / "export_vista_verified_projection.py"),
            "--dataset-root",
            str(self.fixture.dataset),
            "--no-oracle-jsonl",
            NO_ORACLE_RELATIVE,
            "--attempt-ledger",
            LEDGER_RELATIVE,
            "--raw-render-script",
            RENDER_RELATIVE,
            "--media-summary",
            SUMMARY_RELATIVE,
            "--media",
            MEDIA_RELATIVE,
            "--dataset-revision",
            REVISION,
            "--row-id",
            ROW_ID,
            "--visual-id",
            VISUAL_ID,
            "--case-scope",
            CASE_SCOPE,
            "--scenario-type",
            SCENARIO_TYPE,
            "--provider",
            PROVIDER,
            "--attempt",
            str(ATTEMPT),
            "--output-dir",
            str(self.fixture.output),
            "--apply",
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        failure = json.loads(completed.stderr)
        self.assertEqual(failure["error"]["code"], "VISTA_PROJECTION_OWNER_APPROVAL_REQUIRED")
        self.assertNotIn(OMITTED_URL_SECRET, completed.stderr)
        self.assertNotIn(OMITTED_REVIEW_SECRET, completed.stderr)
        self.assertFalse(self.fixture.output.exists())


if __name__ == "__main__":
    unittest.main()
