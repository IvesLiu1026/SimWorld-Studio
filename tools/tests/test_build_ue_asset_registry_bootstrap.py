from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = TOOLS_DIR / "tests" / "fixtures" / "ue_asset_registry_audit_v1.json"
sys.path.insert(0, str(TOOLS_DIR))
import build_ue_asset_registry_bootstrap as builder  # noqa: E402


def archive_receipt() -> dict:
    return {
        "schema": "vista-simworld-archive-receipt/v1",
        "verified_at": "2026-07-13T06:18:18+08:00",
        "repository": "SimWorld-AI/SimWorld-Studio",
        "repository_type": "dataset",
        "dataset_revision": "26bdd2ca18f06ab455023b0a602ede60b3afb243",
        "filename": "SimWorld-Studio-Minimal.tar.gz",
        "canonical_path": "/operator/archive/SimWorld-Studio-Minimal.tar.gz",
        "download_method": "fixture",
        "expected_size_bytes": 15170703068,
        "actual_size_bytes": 15170703068,
        "expected_sha256": "806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f",
        "actual_sha256": "806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f",
        "verified": True,
        "source_patch_commit": "51426e97354477dca1635217455e644e9ca98976",
        "notes": "synthetic receipt",
    }


def binding() -> builder.SourceBinding:
    return builder.SourceBinding(
        project_name="gym_citynav",
        project_revision="source-patch:51426e97354477dca1635217455e644e9ca98976",
        content_revision="sha256:806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f",
        archive=builder.validate_archive_receipt(archive_receipt()),
    )


class AssetRegistryBootstrapTests(unittest.TestCase):
    def load_audit(self):
        return builder.validate_registry_audit(
            json.loads(FIXTURE.read_text(encoding="utf-8")),
            expected_project_name="gym_citynav",
        )

    def test_deterministic_filter_separates_objects_from_character_content(self):
        audit = self.load_audit()
        manifest = builder.build_object_manifest(audit, binding())
        repeated = builder.build_object_manifest(copy.deepcopy(audit), binding())
        inventory = builder.build_capability_inventory(audit, binding(), limit_per_group=20)

        self.assertEqual(manifest, repeated)
        self.assertEqual(manifest["count"], 3)
        self.assertEqual(
            {asset["ue_name"] for asset in manifest["assets"]},
            {"SM_chair_b", "BP_Box", "SM_Cart"},
        )
        self.assertEqual(
            {asset["asset_type"] for asset in manifest["assets"]},
            {"StaticMesh", "Blueprint"},
        )
        self.assertNotIn("assets", inventory)
        self.assertEqual(inventory["purpose"], "runtime_capability_audit_only_not_semantic_object_index")
        self.assertEqual(inventory["groups"]["animation_clips"]["total_count"], 3)
        self.assertEqual(inventory["groups"]["character_blueprints"]["total_count"], 1)
        self.assertEqual(inventory["groups"]["skeletal_meshes"]["total_count"], 1)
        self.assertEqual(inventory["groups"]["animation_blueprints"]["total_count"], 1)
        self.assertEqual(inventory["groups"]["ik_control_rigs"]["total_count"], 4)
        self.assertEqual(inventory["groups"]["skeletons"]["total_count"], 1)
        capability_paths = {
            candidate["ue_path"]
            for group in inventory["groups"].values()
            for candidate in group["candidates"]
        }
        object_paths = {asset["ue_path"] for asset in manifest["assets"]}
        self.assertTrue(capability_paths.isdisjoint(object_paths))
        character_blueprint = inventory["groups"]["character_blueprints"]["candidates"][0]
        self.assertEqual(character_blueprint["ue_name"], "BP_HumanAvatar")
        self.assertIn("character", character_blueprint["signals"])
        fall = next(
            candidate
            for candidate in inventory["groups"]["animation_clips"]["candidates"]
            if candidate["ue_name"] == "MM_Fall_Loop"
        )
        self.assertIn("fall", fall["signals"])
        foot_ik = next(
            candidate
            for candidate in inventory["groups"]["ik_control_rigs"]["candidates"]
            if candidate["ue_name"] == "CR_Mannequin_BasicFootIK"
        )
        self.assertIn("foot_ik", foot_ik["signals"])
        self.assertEqual(
            manifest["object_filter_audit"]["reject_counts"],
            {
                "character_or_bodypart": 1,
                "helper_system": 1,
                "noise_path": 1,
                "non_object_class": 10,
                "surface_modular_shell": 1,
            },
        )

    def test_capability_inventory_is_bounded_and_reports_truncation(self):
        inventory = builder.build_capability_inventory(
            self.load_audit(), binding(), limit_per_group=1
        )
        animation = inventory["groups"]["animation_clips"]
        self.assertEqual(animation["total_count"], 3)
        self.assertEqual(animation["returned_count"], 1)
        self.assertTrue(animation["truncated"])
        for group in inventory["groups"].values():
            self.assertLessEqual(len(group["candidates"]), 1)
            self.assertEqual(group["selection_policy"], "signal_prioritized_then_class_path")

    def test_asset_ids_retain_stable_digest_with_long_names(self):
        row = {
            "package": "/Game/VeryLongPack/" + "A" * 220,
            "name": "SM_" + "Chair" * 40,
            "class": "StaticMesh",
        }
        asset_id = builder._safe_asset_id(row)
        digest = hashlib.sha256(f"{row['package']}.{row['name']}".encode()).hexdigest()[:10]
        self.assertLessEqual(len(asset_id), 240)
        self.assertTrue(asset_id.endswith("_" + digest))

    def test_checked_in_schema_class_allowlist_matches_code(self):
        schema = json.loads(
            (TOOLS_DIR / "ue_asset_registry_audit_schema.json").read_text(encoding="utf-8")
        )
        selected = set(schema["properties"]["selected_classes"]["items"]["enum"])
        row_classes = set(
            schema["properties"]["assets"]["items"]["properties"]["class"]["enum"]
        )
        self.assertEqual(selected, builder.QUERY_CLASSES)
        self.assertEqual(row_classes, builder.QUERY_CLASSES)

    def test_registry_audit_schema_fails_closed(self):
        base = json.loads(FIXTURE.read_text(encoding="utf-8"))
        mutations = []
        extra = copy.deepcopy(base)
        extra["content_root"] = "/operator/Content"
        mutations.append(extra)
        traversal = copy.deepcopy(base)
        traversal["assets"][0]["package"] = "/Game/CityDatabase/../Secret"
        mutations.append(traversal)
        duplicate = copy.deepcopy(base)
        duplicate["assets"].append(copy.deepcopy(duplicate["assets"][0]))
        duplicate["asset_count"] += 1
        mutations.append(duplicate)
        wrong_count = copy.deepcopy(base)
        wrong_count["asset_count"] -= 1
        mutations.append(wrong_count)
        foreign_project = copy.deepcopy(base)
        foreign_project["project_name"] = "other_project"
        mutations.append(foreign_project)
        unselected_class = copy.deepcopy(base)
        unselected_class["assets"][0]["class"] = "Texture2D"
        mutations.append(unselected_class)

        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(builder.BootstrapError):
                    builder.validate_registry_audit(
                        mutation,
                        expected_project_name="gym_citynav",
                    )

    def test_archive_receipt_must_bind_matching_verified_bytes(self):
        validated = builder.validate_archive_receipt(archive_receipt())
        self.assertEqual(validated["repository"], "SimWorld-AI/SimWorld-Studio")
        self.assertNotIn("canonical_path", validated)
        self.assertRegex(validated["receipt_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(validated["expected_project_revision"], binding().project_revision)
        self.assertEqual(validated["expected_content_revision"], binding().content_revision)

        for field, value in (
            ("actual_size_bytes", 1),
            ("actual_sha256", "0" * 64),
            ("verified", False),
            ("dataset_revision", "latest"),
            ("dataset_revision", "git:main"),
        ):
            invalid = archive_receipt()
            invalid[field] = value
            with self.subTest(field=field):
                with self.assertRaises(builder.BootstrapError):
                    builder.validate_archive_receipt(invalid)

        with self.assertRaisesRegex(builder.BootstrapError, "project revision"):
            builder.SourceBinding(
                project_name="gym_citynav",
                project_revision="source-patch:" + "0" * 40,
                content_revision=validated["expected_content_revision"],
                archive=validated,
            )
        with self.assertRaisesRegex(builder.BootstrapError, "content revision"):
            builder.SourceBinding(
                project_name="gym_citynav",
                project_revision=validated["expected_project_revision"],
                content_revision="sha256:" + "0" * 64,
                archive=validated,
            )

    def test_capability_signals_do_not_use_unsafe_substrings(self):
        pickup = {
            "package": "/Game/Human_Avatar/Animation/LiftSet",
            "name": "A_Lift_Light_PickUp_0cm",
            "class": "AnimSequence",
        }
        trigger = {
            "package": "/Game/Props",
            "name": "BP_OutputTrigger",
            "class": "Blueprint",
        }
        self.assertIn("lift", builder._capability_signals(pickup))
        self.assertNotIn("ik", builder._capability_signals(pickup))
        self.assertNotIn("rig", builder._capability_signals(trigger))
        self.assertNotIn("lift", builder._capability_signals(trigger))

    def test_ambiguous_unreal_package_or_object_names_are_rejected(self):
        audit = json.loads(FIXTURE.read_text(encoding="utf-8"))
        audit["assets"][0]["package"] = "/Game/Props/SM.Chair"
        audit["assets"][0]["name"] = "SM.Chair"
        with self.assertRaisesRegex(builder.BootstrapError, "not a /Game path"):
            builder.validate_registry_audit(audit, expected_project_name="gym_citynav")

    def test_atomic_bundle_refuses_overwrite_and_receipt_hashes_match(self):
        audit = self.load_audit()
        manifest = builder.build_object_manifest(audit, binding())
        inventory = builder.build_capability_inventory(audit, binding(), limit_per_group=20)
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = pathlib.Path(temporary) / "bundle"
            receipt = builder.publish_bundle(
                output_dir,
                audit=audit,
                manifest=manifest,
                inventory=inventory,
            )
            self.assertTrue(receipt["bundle_complete"])
            self.assertFalse(receipt["snapshot_complete"])
            self.assertTrue((output_dir / "bootstrap-receipt.json").is_file())
            for filename, descriptor in receipt["files"].items():
                payload = (output_dir / filename).read_bytes()
                self.assertEqual(len(payload), descriptor["bytes"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), descriptor["sha256"])
                self.assertEqual(stat.S_IMODE((output_dir / filename).stat().st_mode), 0o600)
            original_manifest = (output_dir / "object-manifest.json").read_bytes()
            with self.assertRaises(builder.BootstrapError):
                builder.publish_bundle(
                    output_dir,
                    audit=audit,
                    manifest=manifest,
                    inventory=inventory,
                )
            self.assertEqual((output_dir / "object-manifest.json").read_bytes(), original_manifest)

    def test_json_inputs_reject_duplicate_keys_and_symlink_components(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"schema":"first","schema":"second"}', encoding="utf-8")
            with self.assertRaisesRegex(builder.BootstrapError, "duplicate key"):
                builder._read_regular_json(duplicate, max_bytes=1024, label="fixture")

            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)
            nested = real / "audit.json"
            nested.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(builder.BootstrapError, "traverse symlinks"):
                builder._read_regular_json(
                    linked / "audit.json", max_bytes=1024, label="fixture"
                )

    def test_publication_requires_private_owned_parent(self):
        audit = self.load_audit()
        manifest = builder.build_object_manifest(audit, binding())
        inventory = builder.build_capability_inventory(audit, binding(), limit_per_group=20)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            public_parent = root / "public"
            public_parent.mkdir(mode=0o755)
            public_parent.chmod(0o755)
            with self.assertRaisesRegex(builder.BootstrapError, "private current-user-owned"):
                builder.publish_bundle(
                    public_parent / "bundle",
                    audit=audit,
                    manifest=manifest,
                    inventory=inventory,
                )
            self.assertFalse((public_parent / "bundle").exists())

    def test_offline_dry_run_never_calls_bridge_or_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            audit_path = root / "audit.json"
            receipt_path = root / "archive-receipt.json"
            audit_path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            receipt_path.write_text(json.dumps(archive_receipt()), encoding="utf-8")
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    builder,
                    "send_bridge_request",
                    side_effect=AssertionError("offline dry-run contacted a bridge"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                result = builder.main(
                    [
                        "--audit-input",
                        str(audit_path),
                        "--project-name",
                        "gym_citynav",
                        "--project-revision",
                        binding().project_revision,
                        "--content-revision",
                        binding().content_revision,
                        "--archive-receipt",
                        str(receipt_path),
                        "--dry-run",
                    ]
                )
            self.assertEqual(result, 0)
            summary = json.loads(stdout.getvalue())
            self.assertFalse(summary["snapshot_complete"])
            self.assertEqual(set(root.iterdir()), {audit_path, receipt_path})

    def test_live_query_uses_fixed_registry_only_script_and_explicit_endpoint(self):
        audit = self.load_audit()
        script = builder.build_ue_registry_script(max_rows=1000)
        lowered = script.lower()
        self.assertIn("get_assets_by_path", script)
        self.assertNotIn("load_asset", lowered)
        self.assertNotIn("editorassetlibrary", lowered)
        self.assertNotIn("with open", lowered)
        self.assertNotIn("content/", lowered)

        response = {
            "status": "success",
            "result": {
                "python_logs": [
                    "LogPython: "
                    + builder.LIVE_RESULT_TAG
                    + json.dumps(audit, separators=(",", ":"))
                ]
            },
        }
        with mock.patch.object(builder, "send_bridge_request", return_value=response) as sender:
            queried = builder.query_live_registry(
                transport="legacy-tcp",
                host="ue-operator.internal",
                port=55557,
                timeout=30,
                max_rows=1000,
                max_response_bytes=1024 * 1024,
                expected_project_name="gym_citynav",
            )
        self.assertEqual(queried, audit)
        call = sender.call_args.kwargs
        self.assertEqual(call["host"], "ue-operator.internal")
        self.assertEqual(call["port"], 55557)
        self.assertEqual(call["max_rows"], 1000)
        self.assertNotIn("script", call)

    def test_live_and_offline_modes_require_safe_explicit_arguments(self):
        common = [
            "--project-name",
            "gym_citynav",
            "--project-revision",
            binding().project_revision,
            "--content-revision",
            binding().content_revision,
            "--archive-receipt",
            "/tmp/receipt.json",
        ]
        invalid_argv = [
            ["--live-query", *common],
            ["--live-query", *common, "--dry-run"],
            ["--audit-input", "/tmp/audit.json", *common],
            [
                "--audit-input",
                "/tmp/audit.json",
                *common,
                "--dry-run",
                "--host",
                "127.0.0.1",
            ],
        ]
        for argv in invalid_argv:
            with (
                self.subTest(argv=argv),
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                builder.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
