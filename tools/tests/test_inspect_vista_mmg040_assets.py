from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
CANDIDATE_PATH = (
    TOOLS_DIR / "assets" / "vista_mmg_040_gym_citynav_candidate_sources_v1.json"
)
sys.path.insert(0, str(TOOLS_DIR))
import inspect_vista_mmg040_assets as inspector  # noqa: E402


EXPECTED_OBJECT_PATHS = [
    "/Game/CityDatabase/meshes/SM_chair_b.SM_chair_b",
    "/Game/CityDatabase/blueprints/BP_Box.BP_Box",
    "/Game/CityDatabase/blueprints/BP_Box2.BP_Box2",
    "/Game/CityDatabase/blueprints/BP_Box3.BP_Box3",
    "/Game/Camping_Pack/Props/Seat_Table_01/Meshes/SM_SeatTable_01a.SM_SeatTable_01a",
    "/Game/Industrial_Carts/Meshes/SM_Industrial_Carts_Static_Carts_1.SM_Industrial_Carts_Static_Carts_1",
    "/Game/Industrial_Carts/Meshes/SM_Industrial_Carts_Service_Carts_8.SM_Industrial_Carts_Service_Carts_8",
]


def source_binding() -> dict:
    return {
        "project": {
            "name": inspector.PROJECT_NAME,
            "revision": inspector.PROJECT_REVISION,
            "engine_version": "5.3.2-fixture",
        },
        "content": {
            "mount_point": "/Game",
            "revision": inspector.CONTENT_REVISION,
        },
        "archive": {
            "verified": True,
            "sha256": inspector.CONTENT_REVISION.removeprefix("sha256:"),
        },
    }


def input_documents() -> tuple[bytes, dict, bytes, dict, bytes, dict]:
    candidate_raw = CANDIDATE_PATH.read_bytes()
    candidate = json.loads(candidate_raw)
    assets = []
    for ordinal, row in enumerate(candidate["scene_object_candidates"]):
        ue_path, ue_name = inspector.object_path_from_locator(row["filesystem_locator"])
        assets.append(
            {
                "asset_id": f"fixture-{ordinal}",
                "asset_type": "Blueprint" if ue_name.startswith("BP_") else "StaticMesh",
                "ue_name": ue_name,
                "ue_path": ue_path,
            }
        )
    manifest = {
        "schema": inspector.MANIFEST_SCHEMA,
        "count": len(assets),
        "source_binding": source_binding(),
        # Deliberately reverse this list: plan order must come from the pinned source.
        "assets": list(reversed(assets)),
    }
    manifest_raw = inspector.canonical_bytes(manifest)
    bootstrap = {
        "schema": inspector.BOOTSTRAP_SCHEMA,
        "bundle_complete": True,
        "snapshot_complete": False,
        "bundle_revision": "sha256:" + "a" * 64,
        "source_binding": copy.deepcopy(manifest["source_binding"]),
        "files": {
            "object-manifest.json": {
                "bytes": len(manifest_raw),
                "sha256": inspector.sha256_bytes(manifest_raw),
            }
        },
    }
    bootstrap_raw = inspector.canonical_bytes(bootstrap)
    return candidate_raw, candidate, manifest_raw, manifest, bootstrap_raw, bootstrap


def write_inputs(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    candidate_raw, _candidate, manifest_raw, _manifest, bootstrap_raw, _bootstrap = (
        input_documents()
    )
    candidate_path = root / "candidate.json"
    manifest_path = root / "object-manifest.json"
    bootstrap_path = root / "bootstrap-receipt.json"
    candidate_path.write_bytes(candidate_raw)
    manifest_path.write_bytes(manifest_raw)
    bootstrap_path.write_bytes(bootstrap_raw)
    return candidate_path, manifest_path, bootstrap_path


class VistaMmg040InspectorTests(unittest.TestCase):
    def build_plan(self) -> list[dict]:
        candidate_raw, candidate, manifest_raw, manifest, _bootstrap_raw, bootstrap = (
            input_documents()
        )
        return inspector.build_plan(
            candidate_raw,
            candidate,
            manifest_raw,
            manifest,
            bootstrap,
        )

    def test_pinned_source_and_manifest_resolve_exactly_seven_object_paths(self) -> None:
        candidate_raw, candidate, manifest_raw, manifest, _bootstrap_raw, bootstrap = (
            input_documents()
        )
        self.assertEqual(inspector.sha256_bytes(candidate_raw), inspector.CANDIDATE_SHA256)

        plan = inspector.build_plan(
            candidate_raw,
            candidate,
            manifest_raw,
            manifest,
            bootstrap,
        )

        self.assertEqual(len(plan), inspector.EXPECTED_CANDIDATE_COUNT)
        self.assertEqual([row["ordinal"] for row in plan], list(range(7)))
        self.assertEqual([row["ue_path"] for row in plan], EXPECTED_OBJECT_PATHS)
        self.assertEqual(
            [row["asset_type"] for row in plan],
            ["StaticMesh", "Blueprint", "Blueprint", "Blueprint", "StaticMesh", "StaticMesh", "StaticMesh"],
        )

        with self.assertRaisesRegex(inspector.InspectionError, "byte pin"):
            inspector.build_plan(
                candidate_raw + b"\n",
                candidate,
                manifest_raw,
                manifest,
                bootstrap,
            )

        bad_receipt = copy.deepcopy(bootstrap)
        bad_receipt["files"]["object-manifest.json"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(inspector.InspectionError, "does not bind"):
            inspector.build_plan(
                candidate_raw,
                candidate,
                manifest_raw,
                manifest,
                bad_receipt,
            )

    def test_candidate_object_paths_fail_closed_on_unsafe_or_unresolved_locators(self) -> None:
        candidate_raw, candidate, manifest_raw, manifest, _bootstrap_raw, bootstrap = (
            input_documents()
        )
        for locator in ("../Secret.uasset", "/Game/Secret.uasset", "City/NoSuffix"):
            invalid = copy.deepcopy(candidate)
            invalid["scene_object_candidates"][0]["filesystem_locator"] = locator
            with (
                self.subTest(locator=locator),
                self.assertRaises(inspector.InspectionError),
            ):
                inspector.build_plan(
                    candidate_raw,
                    invalid,
                    manifest_raw,
                    manifest,
                    bootstrap,
                )

        unresolved_manifest = copy.deepcopy(manifest)
        unresolved_manifest["assets"] = unresolved_manifest["assets"][1:]
        unresolved_manifest["count"] -= 1
        unresolved_raw = inspector.canonical_bytes(unresolved_manifest)
        rebound = copy.deepcopy(bootstrap)
        rebound["files"]["object-manifest.json"]["sha256"] = inspector.sha256_bytes(
            unresolved_raw
        )
        with self.assertRaisesRegex(inspector.InspectionError, "did not resolve"):
            inspector.build_plan(
                candidate_raw,
                candidate,
                unresolved_raw,
                unresolved_manifest,
                rebound,
            )

    def test_fixed_per_asset_scripts_compile_and_fit_legacy_request_bound(self) -> None:
        plan = self.build_plan()
        for item in plan:
            with self.subTest(candidate_id=item["candidate_id"]):
                script = inspector.build_ue_script(item)
                compile(script, f"<{item['candidate_id']}>", "exec")
                frame = inspector.canonical_bytes(
                    {
                        "type": "execute_python_script",
                        "params": {"script": script},
                    }
                ) + b"\n"
                self.assertLess(len(frame), inspector.MAX_REQUEST_BYTES)
                self.assertIn("transient=True", script)

        with mock.patch.object(inspector.socket, "create_connection") as connector:
            with self.assertRaisesRegex(inspector.InspectionError, "framing bound"):
                inspector.query_bridge(
                    "127.0.0.1",
                    55560,
                    30,
                    "x" * (inspector.MAX_REQUEST_BYTES * 2),
                )
        connector.assert_not_called()

    def test_malformed_and_symlinked_bootstrap_receipts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            malformed = root / "malformed.json"
            malformed.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
            with self.assertRaisesRegex(inspector.InspectionError, "duplicate JSON key"):
                inspector.read_json(malformed.resolve(), "bootstrap receipt")

            _candidate, _manifest, receipt = write_inputs(root)
            direct_link = root / "receipt-link.json"
            direct_link.symlink_to(receipt.name)
            with self.assertRaisesRegex(inspector.InspectionError, "must not contain symlinks"):
                inspector.read_json(direct_link.absolute(), "bootstrap receipt")

            real_dir = root / "real"
            real_dir.mkdir()
            nested_receipt = real_dir / "receipt.json"
            nested_receipt.write_bytes(receipt.read_bytes())
            linked_dir = root / "linked"
            linked_dir.symlink_to(real_dir, target_is_directory=True)
            with self.assertRaisesRegex(inspector.InspectionError, "must not contain symlinks"):
                inspector.read_json(
                    (linked_dir / nested_receipt.name).absolute(),
                    "bootstrap receipt",
                )

    def test_main_aggregates_seven_records_and_binds_published_receipt(self) -> None:
        plan = self.build_plan()
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            root.chmod(0o700)
            candidate_path, manifest_path, bootstrap_path = write_inputs(root)
            output_dir = root / "observation-r1"
            args = argparse.Namespace(
                candidate_source=candidate_path.resolve(),
                object_manifest=manifest_path.resolve(),
                bootstrap_receipt=bootstrap_path.resolve(),
                dry_run=False,
                live_query=True,
                host="127.0.0.1",
                port=55560,
                timeout=30,
                output_dir=output_dir.resolve(),
            )

            def fake_query(host: str, port: int, timeout: int, script: str) -> dict:
                self.assertEqual((host, port, timeout), ("127.0.0.1", 55560, 30))
                frame = inspector.canonical_bytes(
                    {"type": "execute_python_script", "params": {"script": script}}
                ) + b"\n"
                self.assertLess(len(frame), inspector.MAX_REQUEST_BYTES)
                return {"status": "success"}

            observed = iter(plan)

            def fake_extract(_response: dict, expected: dict) -> tuple[str, dict]:
                self.assertEqual(expected, next(observed))
                record = dict(expected)
                record.update(
                    {
                        "cleanup_verified": True,
                        "inspection_complete": expected["candidate_id"] != "scene_bp_box",
                        "load_succeeded": True,
                        "spawn_succeeded": True,
                    }
                )
                return "5.3.2-fixture", record

            stdout = io.StringIO()
            with (
                mock.patch.object(inspector, "parse_args", return_value=args),
                mock.patch.object(inspector, "query_bridge", side_effect=fake_query) as query,
                mock.patch.object(inspector, "extract_record", side_effect=fake_extract),
                contextlib.redirect_stdout(stdout),
            ):
                result = inspector.main()

            self.assertIsNone(result)
            self.assertEqual(query.call_count, inspector.EXPECTED_CANDIDATE_COUNT)
            observation_raw = (output_dir / "observation.json").read_bytes()
            observation = json.loads(observation_raw)
            receipt = json.loads((output_dir / "receipt.json").read_bytes())
            self.assertEqual(len(observation["records"]), 7)
            self.assertFalse(observation["passed"])
            self.assertTrue(observation["cleanup_verified"])
            self.assertEqual(receipt["record_count"], 7)
            self.assertFalse(receipt["passed"])
            self.assertTrue(receipt["cleanup_verified"])
            self.assertEqual(receipt["observation"]["bytes"], len(observation_raw))
            self.assertEqual(
                receipt["observation"]["sha256"],
                inspector.sha256_bytes(observation_raw),
            )
            self.assertIn('"passed": false', stdout.getvalue())

    def test_offline_dry_run_cli_exit_statuses_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            candidate_path, manifest_path, bootstrap_path = write_inputs(root)
            base = [
                sys.executable,
                str(TOOLS_DIR / "inspect_vista_mmg040_assets.py"),
                "--candidate-source",
                str(candidate_path.resolve()),
                "--object-manifest",
                str(manifest_path.resolve()),
                "--bootstrap-receipt",
                str(bootstrap_path.resolve()),
                "--dry-run",
            ]
            success = subprocess.run(
                base,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(len(json.loads(success.stdout)["plan"]), 7)

            malformed = root / "malformed-receipt.json"
            malformed.write_text("{", encoding="utf-8")
            failure_argv = list(base)
            failure_argv[failure_argv.index(str(bootstrap_path.resolve()))] = str(
                malformed.resolve()
            )
            failure = subprocess.run(
                failure_argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(failure.returncode, 0)
            self.assertIn("inspection failed", failure.stderr)


if __name__ == "__main__":
    unittest.main()
