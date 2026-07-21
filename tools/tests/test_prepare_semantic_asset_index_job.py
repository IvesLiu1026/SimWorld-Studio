from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_AUDIT = TOOLS_DIR / "tests" / "fixtures" / "ue_asset_registry_audit_v1.json"
sys.path.insert(0, str(TOOLS_DIR))

import build_ue_asset_registry_bootstrap as bootstrap  # noqa: E402
import prepare_semantic_asset_index_job as preparation  # noqa: E402


PROJECT_REVISION = "source-patch:51426e97354477dca1635217455e644e9ca98976"
CONTENT_REVISION = "sha256:806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f"
SNAPSHOT_REVISION = "asset-snapshot-official-minimal-20260721-r1"


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
        "expected_sha256": CONTENT_REVISION.removeprefix("sha256:"),
        "actual_sha256": CONTENT_REVISION.removeprefix("sha256:"),
        "verified": True,
        "source_patch_commit": PROJECT_REVISION.removeprefix("source-patch:"),
        "notes": "synthetic receipt",
    }


def recipe() -> dict:
    return {
        "schema": preparation.RECIPE_SCHEMA,
        "caption": {
            "provider_id": "anthropic-claude-cli",
            "model_id": "claude-opus-4-8",
            "model_revision": "provider-snapshot:claude-opus-4-8-20260721",
            "prompt_revision": "sha256:" + "1" * 64,
            "output_schema_revision": "sha256:" + "2" * 64,
            "render_recipe_revision": "sha256:" + "3" * 64,
            "views_per_asset": 8,
            "image_width_px": 1024,
            "image_height_px": 1024,
            "max_output_tokens_per_asset": 1200,
            "temperature_milli": 0,
        },
        "embedding": {
            "recipe_revision": "bge-large-en-v1.5-bm25-v1-20260721",
            "dense_model_id": "BAAI/bge-large-en-v1.5",
            "dense_model_revision": "sha256:" + "4" * 64,
            "dense_size": 1024,
            "sparse_model_id": "Qdrant/bm25",
            "sparse_model_revision": "sha256:" + "5" * 64,
            "batch_size": 32,
        },
        "storage": {
            "postgres_schema_revision": 2,
            "qdrant_collection": "assets-v1-r20260721",
            "qdrant_dense_vector_name": "dense",
            "qdrant_sparse_vector_name": "sparse",
        },
        "limits": {
            "max_assets": 1000,
            "max_rendered_views": 8000,
            "max_render_pixels": 8_388_608_000,
            "max_total_caption_output_tokens": 1_200_000,
            "max_catalog_record_bytes": 65_536,
            "max_total_catalog_bytes": 65_536_000,
            "max_postgres_rows": 1000,
            "max_qdrant_points": 1000,
            "max_dense_vector_payload_bytes_estimate": 4_096_000,
        },
    }


class Fixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.chmod(0o700)
        bundle_parent = root / "bootstrap"
        bundle_parent.mkdir(mode=0o700)
        self.bundle_dir = bundle_parent / "official-minimal-r1"
        audit = bootstrap.validate_registry_audit(
            json.loads(FIXTURE_AUDIT.read_text(encoding="utf-8")),
            expected_project_name="gym_citynav",
        )
        source_binding = bootstrap.SourceBinding(
            project_name="gym_citynav",
            project_revision=PROJECT_REVISION,
            content_revision=CONTENT_REVISION,
            archive=bootstrap.validate_archive_receipt(archive_receipt()),
        )
        manifest = bootstrap.build_object_manifest(audit, source_binding)
        inventory = bootstrap.build_capability_inventory(audit, source_binding, limit_per_group=20)
        self.bootstrap_receipt = bootstrap.publish_bundle(
            self.bundle_dir,
            audit=audit,
            manifest=manifest,
            inventory=inventory,
        )
        self.receipt_path = self.bundle_dir / "bootstrap-receipt.json"
        self.manifest_path = self.bundle_dir / "object-manifest.json"
        self.recipe_path = root / "semantic-index-recipe.json"
        self.recipe = recipe()
        self.write_recipe()
        self.output_parent = root / "jobs"
        self.output_parent.mkdir(mode=0o700)
        self.output_dir = self.output_parent / "semantic-index-r1"

    def write_recipe(self) -> None:
        self.recipe_path.write_bytes(preparation.canonical_json(self.recipe))
        self.recipe_path.chmod(0o600)

    def kwargs(self) -> dict:
        return {
            "bootstrap_receipt_path": self.receipt_path,
            "object_manifest_path": self.manifest_path,
            "recipe_path": self.recipe_path,
            "expected_bootstrap_receipt_sha256": hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
            "expected_bundle_revision": self.bootstrap_receipt["bundle_revision"],
            "expected_object_manifest_sha256": hashlib.sha256(self.manifest_path.read_bytes()).hexdigest(),
            "expected_object_count": self.bootstrap_receipt["object_count"],
            "expected_project_revision": PROJECT_REVISION,
            "expected_content_revision": CONTENT_REVISION,
            "expected_recipe_sha256": hashlib.sha256(self.recipe_path.read_bytes()).hexdigest(),
            "asset_snapshot_revision": SNAPSHOT_REVISION,
        }

    def prepare(self) -> preparation.PreparedJob:
        return preparation.build_job(**self.kwargs())

    def bootstrap_json(self, filename: str) -> dict:
        return json.loads((self.bundle_dir / filename).read_text(encoding="utf-8"))

    def cli_args(self, *, output_dir: pathlib.Path | None = None) -> list[str]:
        values = self.kwargs()
        args = [
            "--bootstrap-receipt",
            str(values["bootstrap_receipt_path"]),
            "--object-manifest",
            str(values["object_manifest_path"]),
            "--recipe",
            str(values["recipe_path"]),
            "--expected-bootstrap-receipt-sha256",
            values["expected_bootstrap_receipt_sha256"],
            "--expected-bundle-revision",
            values["expected_bundle_revision"],
            "--expected-object-manifest-sha256",
            values["expected_object_manifest_sha256"],
            "--expected-object-count",
            str(values["expected_object_count"]),
            "--expected-project-revision",
            values["expected_project_revision"],
            "--expected-content-revision",
            values["expected_content_revision"],
            "--expected-recipe-sha256",
            values["expected_recipe_sha256"],
            "--asset-snapshot-revision",
            values["asset_snapshot_revision"],
        ]
        if output_dir is not None:
            args.extend(["--output-dir", str(output_dir)])
        return args


class SemanticAssetIndexJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(pathlib.Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code: str, callback) -> preparation.SemanticIndexJobError:
        with self.assertRaises(preparation.SemanticIndexJobError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_job_is_deterministic_and_keeps_candidate_snapshot_incomplete(self) -> None:
        first = self.fixture.prepare()
        second = self.fixture.prepare()
        self.assertEqual(first.job_bytes, second.job_bytes)
        self.assertEqual(first.job["job_revision"], second.job["job_revision"])
        self.assertEqual(first.job["pending_objects"]["count"], 3)
        self.assertEqual(
            [asset["status"] for asset in first.job["pending_objects"]["assets"]],
            ["pending_live_semantic_index"] * 3,
        )
        self.assertFalse(first.job["input_contract"]["candidate_manifest_reviewed"])
        self.assertFalse(first.job["input_contract"]["catalog_complete"])
        self.assertFalse(first.job["snapshot_complete"])
        revision_basis = {
            key: value for key, value in first.job.items() if key != "job_revision"
        }
        self.assertEqual(
            first.job["job_revision"],
            "sha256:" + preparation.json_sha256(revision_basis),
        )

    def test_recipe_snapshot_and_resource_upper_bounds_are_pinned(self) -> None:
        job = self.fixture.prepare().job
        self.assertEqual(job["snapshot_target"]["asset_snapshot_revision"], SNAPSHOT_REVISION)
        self.assertEqual(job["snapshot_target"]["postgres"]["planned_candidate_rows"], 3)
        self.assertEqual(job["snapshot_target"]["qdrant"]["planned_candidate_points"], 3)
        self.assertEqual(job["recipe_contract"]["recipe_sha256"], self.fixture.kwargs()["expected_recipe_sha256"])
        estimates = job["resource_contract"]["estimates"]
        self.assertEqual(estimates["rendered_views_upper_bound"], 24)
        self.assertEqual(estimates["render_pixels_upper_bound"], 24 * 1024 * 1024)
        self.assertEqual(estimates["caption_output_tokens_upper_bound"], 3600)
        self.assertEqual(estimates["embedding_batches_upper_bound"], 1)
        self.assertEqual(estimates["dense_vector_payload_bytes_estimate"], 3 * 1024 * 4)
        self.assertFalse(job["resource_contract"]["cost_authorized"])

    def test_every_external_source_pin_is_required_exactly(self) -> None:
        mutations = {
            "expected_bootstrap_receipt_sha256": "0" * 64,
            "expected_bundle_revision": "sha256:" + "0" * 64,
            "expected_object_manifest_sha256": "0" * 64,
            "expected_object_count": 4,
            "expected_project_revision": "source-patch:" + "0" * 40,
            "expected_content_revision": "sha256:" + "0" * 64,
            "expected_recipe_sha256": "0" * 64,
        }
        expected_codes = {
            "expected_bootstrap_receipt_sha256": "SEMANTIC_INDEX_BOOTSTRAP_RECEIPT_PIN_MISMATCH",
            "expected_bundle_revision": "SEMANTIC_INDEX_BUNDLE_PIN_MISMATCH",
            "expected_object_manifest_sha256": "SEMANTIC_INDEX_MANIFEST_PIN_MISMATCH",
            "expected_object_count": "SEMANTIC_INDEX_OBJECT_COUNT_PIN_MISMATCH",
            "expected_project_revision": "SEMANTIC_INDEX_PROJECT_REVISION_MISMATCH",
            "expected_content_revision": "SEMANTIC_INDEX_CONTENT_REVISION_MISMATCH",
            "expected_recipe_sha256": "SEMANTIC_INDEX_RECIPE_PIN_MISMATCH",
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                kwargs = self.fixture.kwargs()
                kwargs[key] = value
                self.assert_error(expected_codes[key], lambda kwargs=kwargs: preparation.build_job(**kwargs))

    def test_manifest_tamper_after_receipt_fails_closed(self) -> None:
        manifest = json.loads(self.fixture.manifest_path.read_text(encoding="utf-8"))
        manifest["assets"][0]["ue_name"] = "SM_Tampered"
        self.fixture.manifest_path.write_bytes(preparation.canonical_json(manifest))
        self.fixture.manifest_path.chmod(0o600)
        self.assert_error("SEMANTIC_INDEX_MANIFEST_RECEIPT_MISMATCH", self.fixture.prepare)

    def test_every_bootstrap_member_must_still_match_the_complete_receipt(self) -> None:
        capability_path = self.fixture.bundle_dir / "content-capabilities.json"
        capability_path.write_bytes(capability_path.read_bytes() + b"\n")
        capability_path.chmod(0o600)
        self.assert_error("SEMANTIC_INDEX_BOOTSTRAP_MEMBER_MISMATCH", self.fixture.prepare)

    def test_receipt_canonical_revision_is_rechecked(self) -> None:
        receipt = json.loads(self.fixture.receipt_path.read_text(encoding="utf-8"))
        receipt["object_count"] += 1
        self.fixture.receipt_path.write_bytes(preparation.canonical_json(receipt))
        self.fixture.receipt_path.chmod(0o600)
        self.assert_error("SEMANTIC_INDEX_BOOTSTRAP_REVISION_INVALID", self.fixture.prepare)

    def test_bootstrap_cross_references_rebuild_exact_producer_contracts(self) -> None:
        receipt = self.fixture.bootstrap_json("bootstrap-receipt.json")
        registry = self.fixture.bootstrap_json("registry-audit.json")
        manifest = self.fixture.bootstrap_json("object-manifest.json")
        inventory = self.fixture.bootstrap_json("content-capabilities.json")
        preparation.validate_bootstrap_cross_references(
            receipt=receipt,
            registry_audit=registry,
            object_manifest=manifest,
            capability_inventory=inventory,
        )

        invalid_registry = json.loads(json.dumps(registry))
        invalid_registry["selected_classes"] = invalid_registry["selected_classes"][:-1]
        self.assert_error(
            "SEMANTIC_INDEX_BOOTSTRAP_CONTRACT_INVALID",
            lambda: preparation.validate_bootstrap_cross_references(
                receipt=receipt,
                registry_audit=invalid_registry,
                object_manifest=manifest,
                capability_inventory=inventory,
            ),
        )

    def test_manifest_and_capabilities_must_be_registry_members_and_disjoint(self) -> None:
        receipt = self.fixture.bootstrap_json("bootstrap-receipt.json")
        registry = self.fixture.bootstrap_json("registry-audit.json")
        manifest = self.fixture.bootstrap_json("object-manifest.json")
        inventory = self.fixture.bootstrap_json("content-capabilities.json")

        forged_manifest = json.loads(json.dumps(manifest))
        forged_manifest["assets"][0]["ue_name"] = "SM_Forged"
        forged_manifest["assets"][0]["ue_path"] = "/Game/Forged/SM_Forged.SM_Forged"
        self.assert_error(
            "SEMANTIC_INDEX_MANIFEST_REGISTRY_MISMATCH",
            lambda: preparation.validate_bootstrap_cross_references(
                receipt=receipt,
                registry_audit=registry,
                object_manifest=forged_manifest,
                capability_inventory=inventory,
            ),
        )

        forged_inventory = json.loads(json.dumps(inventory))
        candidate = forged_inventory["groups"]["animation_clips"]["candidates"][0]
        candidate["ue_name"] = "AS_Forged"
        candidate["ue_path"] = "/Game/Forged/AS_Forged.AS_Forged"
        self.assert_error(
            "SEMANTIC_INDEX_CAPABILITY_REGISTRY_MISMATCH",
            lambda: preparation.validate_bootstrap_cross_references(
                receipt=receipt,
                registry_audit=registry,
                object_manifest=manifest,
                capability_inventory=forged_inventory,
            ),
        )

        overlapping_inventory = json.loads(json.dumps(inventory))
        candidate = overlapping_inventory["groups"]["character_blueprints"]["candidates"][0]
        candidate.update(
            {
                "ue_name": "BP_Box",
                "ue_path": "/Game/CityDatabase/blueprints/BP_Box.BP_Box",
                "asset_class": "Blueprint",
                "source_pack": "CityDatabase",
                "signals": [],
            }
        )
        self.assert_error(
            "SEMANTIC_INDEX_CAPABILITY_OBJECT_OVERLAP",
            lambda: preparation.validate_bootstrap_cross_references(
                receipt=receipt,
                registry_audit=registry,
                object_manifest=manifest,
                capability_inventory=overlapping_inventory,
            ),
        )

    def test_receipt_capability_counts_reconcile_with_canonical_inventory(self) -> None:
        receipt = self.fixture.bootstrap_json("bootstrap-receipt.json")
        registry = self.fixture.bootstrap_json("registry-audit.json")
        manifest = self.fixture.bootstrap_json("object-manifest.json")
        inventory = self.fixture.bootstrap_json("content-capabilities.json")
        receipt["capability_counts"]["animation_clips"] += 1
        self.assert_error(
            "SEMANTIC_INDEX_CAPABILITY_COUNT_MISMATCH",
            lambda: preparation.validate_bootstrap_cross_references(
                receipt=receipt,
                registry_audit=registry,
                object_manifest=manifest,
                capability_inventory=inventory,
            ),
        )

    def test_duplicate_json_keys_are_rejected(self) -> None:
        self.fixture.recipe_path.write_bytes(
            b'{"schema":"simworld-semantic-asset-index-recipe/v1","schema":"duplicate"}'
        )
        self.fixture.recipe_path.chmod(0o600)
        kwargs = self.fixture.kwargs()
        kwargs["expected_recipe_sha256"] = hashlib.sha256(self.fixture.recipe_path.read_bytes()).hexdigest()
        self.assert_error(
            "SEMANTIC_INDEX_JSON_DUPLICATE_KEY",
            lambda: preparation.build_job(**kwargs),
        )

    def test_unknown_json_keys_are_not_echoed_in_public_errors(self) -> None:
        sensitive_key = "bearer-secret-material-must-not-leak"
        self.fixture.recipe[sensitive_key] = True
        self.fixture.write_recipe()
        error = self.assert_error("SEMANTIC_INDEX_SCHEMA_INVALID", self.fixture.prepare)
        serialized = json.dumps(error.public_dict(), sort_keys=True)
        self.assertNotIn(sensitive_key, serialized)

    def test_private_regular_input_contract_rejects_weak_symlink_and_hardlink_files(self) -> None:
        self.fixture.recipe_path.chmod(0o644)
        self.assert_error("SEMANTIC_INDEX_INPUT_UNSAFE", self.fixture.prepare)
        self.fixture.recipe_path.chmod(0o600)

        link = self.fixture.root / "recipe-link.json"
        link.symlink_to(self.fixture.recipe_path)
        kwargs = self.fixture.kwargs()
        kwargs["recipe_path"] = link
        self.assert_error("SEMANTIC_INDEX_INPUT_UNSAFE", lambda: preparation.build_job(**kwargs))

        hardlink = self.fixture.root / "recipe-hardlink.json"
        os.link(self.fixture.recipe_path, hardlink)
        self.assert_error("SEMANTIC_INDEX_INPUT_UNSAFE", self.fixture.prepare)

    def test_secure_read_rejects_metadata_drift_during_read(self) -> None:
        real_fstat = os.fstat
        calls = 0

        def drifting_fstat(descriptor):
            nonlocal calls
            metadata = real_fstat(descriptor)
            calls += 1
            if calls != 2:
                return metadata
            fields = {
                name: getattr(metadata, name)
                for name in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_uid",
                    "st_gid",
                    "st_nlink",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            }
            fields["st_ctime_ns"] += 1
            return types.SimpleNamespace(**fields)

        with mock.patch.object(preparation.os, "fstat", side_effect=drifting_fstat):
            self.assert_error(
                "SEMANTIC_INDEX_INPUT_CHANGED",
                lambda: preparation._secure_read(
                    self.fixture.recipe_path,
                    max_bytes=preparation.MAX_RECIPE_BYTES,
                    pointer="test recipe",
                ),
            )

    def test_recipe_rejects_floating_revisions_generic_collection_and_insufficient_limits(self) -> None:
        cases = [
            ("caption", "model_revision", "provider-snapshot:latest"),
            ("caption", "model_revision", "provider-snapshot:"),
            ("embedding", "recipe_revision", "latest"),
            ("storage", "qdrant_collection", "assets"),
            ("storage", "qdrant_collection", "assets-v1-latest"),
            ("limits", "max_assets", 2),
        ]
        expected = [
            "SEMANTIC_INDEX_REVISION_FLOATING",
            "SEMANTIC_INDEX_CAPTION_REVISION_INVALID",
            "SEMANTIC_INDEX_REVISION_FLOATING",
            "SEMANTIC_INDEX_QDRANT_COLLECTION_UNPINNED",
            "SEMANTIC_INDEX_REVISION_FLOATING",
            "SEMANTIC_INDEX_RESOURCE_LIMIT_EXCEEDED",
        ]
        for (section, key, value), code in zip(cases, expected):
            with self.subTest(section=section, key=key):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = Fixture(pathlib.Path(temporary))
                    fixture.recipe[section][key] = value
                    fixture.write_recipe()
                    self.assert_error(code, fixture.prepare)

        kwargs = self.fixture.kwargs()
        kwargs["asset_snapshot_revision"] = "asset-snapshot-"
        self.assert_error(
            "SEMANTIC_INDEX_SNAPSHOT_REVISION_INVALID",
            lambda: preparation.build_job(**kwargs),
        )

    def test_dry_run_is_default_and_writes_nothing(self) -> None:
        output = self.fixture.output_parent / "dry-run-would-be-output"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = preparation.main(self.fixture.cli_args(output_dir=output))
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["network_used"])
        self.assertFalse(result["snapshot_complete"])
        self.assertFalse(output.exists())

    def test_apply_only_publishes_private_atomic_bundle_and_refuses_overwrite(self) -> None:
        prepared = self.fixture.prepare()
        approval = "CHANGE-SEMANTIC-INDEX-20260721"
        receipt = preparation.publish_job(prepared, self.fixture.output_dir, approval)
        self.assertTrue(receipt["bundle_complete"])
        self.assertFalse(receipt["execution_started"])
        self.assertFalse(receipt["network_used"])
        self.assertFalse(receipt["snapshot_complete"])
        self.assertEqual(stat.S_IMODE(self.fixture.output_dir.stat().st_mode), 0o700)
        for name in ("semantic-index-job.json", "preparation-receipt.json"):
            self.assertEqual(stat.S_IMODE((self.fixture.output_dir / name).stat().st_mode), 0o600)
        receipt_bytes = (self.fixture.output_dir / "preparation-receipt.json").read_bytes()
        self.assertNotIn(approval.encode("utf-8"), receipt_bytes)
        self.assert_error(
            "SEMANTIC_INDEX_OUTPUT_EXISTS",
            lambda: preparation.publish_job(prepared, self.fixture.output_dir, approval),
        )

    def test_publish_reprojects_from_raw_basis_instead_of_mutable_views(self) -> None:
        prepared = self.fixture.prepare()
        mutable_view = prepared.job
        mutable_view["pending_objects"]["count"] = 999
        receipt = preparation.publish_job(
            prepared,
            self.fixture.output_dir,
            "CHANGE-SEMANTIC-INDEX-20260721",
        )
        self.assertEqual(receipt["pending_object_count"], 3)
        published = json.loads(
            (self.fixture.output_dir / "semantic-index-job.json").read_text(encoding="utf-8")
        )
        self.assertEqual(published["pending_objects"]["count"], 3)

        forged_job = prepared.job
        forged_job["pending_objects"]["count"] = 999
        basis = {key: value for key, value in forged_job.items() if key != "job_revision"}
        forged_job["job_revision"] = "sha256:" + preparation.json_sha256(basis)
        with self.assertRaises(TypeError):
            dataclasses.replace(
                prepared,
                job_bytes=preparation.canonical_json(forged_job),
            )

    def test_same_count_pending_identity_forgery_cannot_replace_bound_manifest(self) -> None:
        prepared = self.fixture.prepare()
        forged_job = prepared.job
        forged_asset = forged_job["pending_objects"]["assets"][0]
        forged_asset.update(
            {
                "asset_id": "forged_asset_0000000000",
                "ue_name": "SM_Forged",
                "ue_path": "/Game/Forged/SM_Forged.SM_Forged",
            }
        )
        forged_job["pending_objects"]["assets_sha256"] = preparation.json_sha256(
            forged_job["pending_objects"]["assets"]
        )
        revision_basis = {
            key: value for key, value in forged_job.items() if key != "job_revision"
        }
        forged_job["job_revision"] = "sha256:" + preparation.json_sha256(revision_basis)
        with self.assertRaises(TypeError):
            dataclasses.replace(
                prepared,
                job_bytes=preparation.canonical_json(forged_job),
                bootstrap_receipt_sha256="a" * 64,
                object_manifest_sha256="b" * 64,
                recipe_sha256="c" * 64,
                pending_objects_sha256=forged_job["pending_objects"]["assets_sha256"],
            )

    def test_recipe_and_input_contract_forgery_cannot_replace_opaque_basis(self) -> None:
        prepared = self.fixture.prepare()
        self.assertEqual(
            [(item.name, item.init) for item in dataclasses.fields(prepared)],
            [("_trusted_basis", False)],
        )
        with self.assertRaises(TypeError):
            dataclasses.replace(prepared)

        forged_recipe_job = prepared.job
        forged_recipe_job["recipe_contract"]["caption"]["provider_id"] = "forged-provider"
        forged_recipe_job["recipe_contract"]["caption_recipe_sha256"] = (
            preparation.json_sha256(forged_recipe_job["recipe_contract"]["caption"])
        )
        forged_recipe_job["recipe_contract"]["recipe_sha256"] = "d" * 64
        recipe_basis = {
            key: value
            for key, value in forged_recipe_job.items()
            if key != "job_revision"
        }
        forged_recipe_job["job_revision"] = "sha256:" + preparation.json_sha256(
            recipe_basis
        )
        with self.assertRaises(TypeError):
            dataclasses.replace(
                prepared,
                job_bytes=preparation.canonical_json(forged_recipe_job),
                bootstrap_receipt_sha256=prepared.bootstrap_receipt_sha256,
                object_manifest_sha256=prepared.object_manifest_sha256,
                recipe_sha256="d" * 64,
                pending_objects_sha256=prepared.pending_objects_sha256,
            )

        forged_input_job = prepared.job
        forged_input = forged_input_job["input_contract"]
        forged_input.update(
            {
                "bootstrap_receipt_sha256": "1" * 64,
                "bootstrap_bundle_revision": "sha256:" + "2" * 64,
                "object_manifest_revision": "sha256:" + "3" * 64,
                "object_manifest_sha256": "4" * 64,
                "archive_receipt_sha256": "5" * 64,
                "registry_audit_sha256": "6" * 64,
            }
        )
        input_basis = {
            key: value for key, value in forged_input_job.items() if key != "job_revision"
        }
        forged_input_job["job_revision"] = "sha256:" + preparation.json_sha256(
            input_basis
        )
        with self.assertRaises(TypeError):
            dataclasses.replace(
                prepared,
                job_bytes=preparation.canonical_json(forged_input_job),
                bootstrap_receipt_sha256="1" * 64,
                object_manifest_sha256="4" * 64,
                recipe_sha256=prepared.recipe_sha256,
                pending_objects_sha256=prepared.pending_objects_sha256,
            )

        with self.assertRaises(ValueError):
            dataclasses.replace(
                prepared,
                _trusted_basis=object.__getattribute__(prepared, "_trusted_basis"),
            )

    def test_low_level_raw_recipe_tamper_is_revalidated_against_operator_pin(self) -> None:
        prepared = self.fixture.prepare()
        trusted_basis = object.__getattribute__(prepared, "_trusted_basis")
        forged_recipe = json.loads(trusted_basis.recipe_raw)
        forged_recipe["caption"]["provider_id"] = "forged-provider"
        forged_basis = dataclasses.replace(
            trusted_basis,
            recipe_raw=preparation.canonical_json(forged_recipe),
        )
        counterfeit = object.__new__(preparation.PreparedJob)
        object.__setattr__(counterfeit, "_trusted_basis", forged_basis)
        self.assert_error(
            "SEMANTIC_INDEX_RECIPE_PIN_MISMATCH",
            lambda: preparation.result(counterfeit, status="dry_run"),
        )

    def test_apply_requires_non_secret_approval(self) -> None:
        prepared = self.fixture.prepare()
        self.assert_error(
            "SEMANTIC_INDEX_APPROVAL_INVALID",
            lambda: preparation.publish_job(prepared, self.fixture.output_dir, "token=secret"),
        )
        self.assertFalse(self.fixture.output_dir.exists())

    def test_publication_rejects_symlink_parent_and_preserves_foreign_lock(self) -> None:
        prepared = self.fixture.prepare()
        symlink_parent = self.fixture.root / "jobs-link"
        symlink_parent.symlink_to(self.fixture.output_parent, target_is_directory=True)
        self.assert_error(
            "SEMANTIC_INDEX_OUTPUT_INVALID",
            lambda: preparation.publish_job(
                prepared,
                symlink_parent / "semantic-index-r1",
                "CHANGE-SEMANTIC-INDEX-20260721",
            ),
        )

        lock = self.fixture.output_parent / f".{self.fixture.output_dir.name}.lock"
        marker = b"foreign-lock\n"
        lock.write_bytes(marker)
        lock.chmod(0o600)
        self.assert_error(
            "SEMANTIC_INDEX_OUTPUT_BUSY",
            lambda: preparation.publish_job(
                prepared,
                self.fixture.output_dir,
                "CHANGE-SEMANTIC-INDEX-20260721",
            ),
        )
        self.assertEqual(lock.read_bytes(), marker)
        self.assertFalse(self.fixture.output_dir.exists())

    def test_lock_replacement_is_not_unlinked_by_previous_owner(self) -> None:
        prepared = self.fixture.prepare()
        lock = self.fixture.output_parent / f".{self.fixture.output_dir.name}.lock"
        marker = b"replacement-lock\n"
        rename = preparation._rename_directory_no_replace

        def replace_lock_then_publish(source, destination):
            lock.unlink()
            lock.write_bytes(marker)
            lock.chmod(0o600)
            rename(source, destination)

        with mock.patch.object(
            preparation,
            "_rename_directory_no_replace",
            side_effect=replace_lock_then_publish,
        ):
            preparation.publish_job(
                prepared,
                self.fixture.output_dir,
                "CHANGE-SEMANTIC-INDEX-20260721",
            )
        self.assertEqual(lock.read_bytes(), marker)
        self.assertTrue(self.fixture.output_dir.is_dir())

    def test_parent_fsync_failure_reports_committed_durability_uncertainty(self) -> None:
        prepared = self.fixture.prepare()
        with mock.patch.object(
            preparation,
            "_fsync_directory",
            side_effect=[None, OSError("synthetic parent fsync failure")],
        ):
            error = self.assert_error(
                "SEMANTIC_INDEX_OUTPUT_COMMITTED_NOT_DURABLE",
                lambda: preparation.publish_job(
                    prepared,
                    self.fixture.output_dir,
                    "CHANGE-SEMANTIC-INDEX-20260721",
                ),
            )
        self.assertTrue(error.committed)
        self.assertTrue(error.durability_uncertain)
        self.assertTrue(self.fixture.output_dir.is_dir())
        self.assertTrue((self.fixture.output_dir / "preparation-receipt.json").is_file())

    def test_job_and_recipe_schemas_track_emitted_contract(self) -> None:
        job_schema = json.loads((TOOLS_DIR / "semantic_asset_index_job_schema.json").read_text(encoding="utf-8"))
        recipe_schema = json.loads((TOOLS_DIR / "semantic_asset_index_recipe_schema.json").read_text(encoding="utf-8"))
        job = self.fixture.prepare().job
        self.assertEqual(job_schema["properties"]["schema"]["const"], preparation.JOB_SCHEMA)
        self.assertEqual(recipe_schema["properties"]["schema"]["const"], preparation.RECIPE_SCHEMA)
        self.assertEqual(set(job_schema["required"]), set(job))
        self.assertEqual(set(recipe_schema["required"]), set(self.fixture.recipe))
        self.assertFalse(job_schema["additionalProperties"])
        self.assertFalse(recipe_schema["additionalProperties"])
        embedded = {
            "caption": "captionRecipe",
            "embedding": "embeddingRecipe",
            "storage": "storageRecipe",
            "limits": "resourceLimits",
        }
        for recipe_key, job_definition in embedded.items():
            self.assertEqual(
                set(recipe_schema["properties"][recipe_key]["required"]),
                set(job_schema["$defs"][job_definition]["required"]),
            )
            self.assertEqual(
                set(recipe_schema["properties"][recipe_key]["properties"]),
                set(job_schema["$defs"][job_definition]["properties"]),
            )

        def refs(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "$ref":
                        yield child
                    yield from refs(child)
            elif isinstance(value, list):
                for child in value:
                    yield from refs(child)

        self.assertTrue(all(ref.startswith("#/") for ref in refs(job_schema)))
        self.assertTrue(all(ref.startswith("#/") for ref in refs(recipe_schema)))

        provider_patterns = [
            recipe_schema["properties"]["caption"]["properties"]["model_revision"]["anyOf"][1][
                "pattern"
            ],
            job_schema["$defs"]["captionRecipe"]["properties"]["model_revision"]["anyOf"][1][
                "pattern"
            ],
        ]
        for pattern in provider_patterns:
            self.assertIsNone(re.fullmatch(pattern, "provider-snapshot:"))
            self.assertIsNotNone(re.fullmatch(pattern, "provider-snapshot:r"))

        snapshot_pattern = job_schema["properties"]["snapshot_target"]["properties"][
            "asset_snapshot_revision"
        ]["pattern"]
        self.assertIsNone(re.fullmatch(snapshot_pattern, "asset-snapshot-"))
        self.assertIsNotNone(re.fullmatch(snapshot_pattern, "asset-snapshot-r"))

        collection_contracts = [
            recipe_schema["properties"]["storage"]["properties"]["qdrant_collection"],
            job_schema["$defs"]["storageRecipe"]["properties"]["qdrant_collection"],
            job_schema["properties"]["snapshot_target"]["properties"]["qdrant"][
                "properties"
            ]["collection"],
        ]
        for contract in collection_contracts:
            floating_pattern = contract["allOf"][1]["not"]["pattern"]
            self.assertIsNotNone(re.search(floating_pattern, "assets-v1-latest"))
            self.assertIsNone(re.search(floating_pattern, "assets-v1-r20260721"))

    def test_preparer_has_no_live_or_process_execution_imports(self) -> None:
        source = (TOOLS_DIR / "prepare_semantic_asset_index_job.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue({"socket", "subprocess", "urllib", "requests"}.isdisjoint(imports))
        self.assertNotIn("dangerously-bypass-approvals-and-sandbox\",\n        codex", source)

    def test_legacy_runner_is_not_an_authorized_execution_surface(self) -> None:
        contract = self.fixture.prepare().job["execution_contract"]
        self.assertFalse(contract["legacy_full_asset_index_runner_authorized"])
        self.assertTrue(contract["reviewed_adapter_required_for_legacy_runner"])
        self.assertEqual(
            contract["prohibited_production_default_args"],
            ["--dangerously-bypass-approvals-and-sandbox"],
        )
        self.assertTrue(contract["explicit_operator_execution_required"])


if __name__ == "__main__":
    unittest.main()
