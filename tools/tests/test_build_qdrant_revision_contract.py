from __future__ import annotations

import pathlib
import sys
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))
import build_qdrant_index as builder  # noqa: E402


def asset_row():
    return {
        "asset_id": "chair",
        "qdrant_point_id": "9a511b8a-d781-5f51-8867-d90171d8e999",
        "asset_snapshot_revision": "asset-snapshot-20260721-r1",
        "name": "Office chair",
        "category": "office",
        "subcategory": "chair",
        "setting": "office",
        "style": "modern",
        "scene_types": ["workplace"],
        "tags": ["chair"],
        "materials": ["fabric"],
        "asset_type": "StaticMesh",
        "source_pack": "OfficePack",
        "unreal_asset_path": "/Game/Office/SM_Chair.SM_Chair",
        "short_description": "A wheeled chair",
        "width_m": 0.6,
        "depth_m": 0.6,
        "height_m": 1.0,
        "triangle_count": 1200,
    }


class QdrantRevisionContractTests(unittest.TestCase):
    def test_model_artifacts_are_verified_before_any_qdrant_mutation(self):
        source = (TOOLS_DIR / "build_qdrant_index.py").read_text(encoding="utf-8")
        main_start = source.index("def main()")
        verify_call = source.index("    verify_embedding_model_artifact(", main_start)
        client_call = source.index("    qd = QdrantClient", main_start)
        ensure_call = source.index("        ensure_collection", main_start)
        self.assertLess(verify_call, client_call)
        self.assertLess(verify_call, ensure_call)
        self.assertIn("specific_model_path=args.dense_model_path", source)
        self.assertIn("specific_model_path=args.sparse_model_path", source)
        self.assertGreaterEqual(source.count("local_files_only=True"), 2)

    def test_payload_records_both_immutable_model_revisions(self):
        payload = builder.payload_for_row(
            asset_row(),
            "asset-snapshot-20260721-r1",
            "embed-v1",
            "BAAI/bge-large-en-v1.5",
            "dense-sha256-abc",
            "Qdrant/bm25",
            "sparse-sha256-def",
            1024,
        )
        self.assertEqual(payload["embedding_version"], "embed-v1")
        self.assertEqual(
            payload["asset_snapshot_revision"], "asset-snapshot-20260721-r1"
        )
        self.assertEqual(payload["dense_revision"], "dense-sha256-abc")
        self.assertEqual(payload["sparse_revision"], "sparse-sha256-def")
        self.assertEqual(payload["dense_size"], 1024)

    def test_revision_change_invalidates_embedding_hash(self):
        row = asset_row()
        payload_a = builder.payload_for_row(
            row,
            "asset-snapshot-20260721-r1",
            "embed-v1",
            "dense",
            "revision-a",
            "sparse",
            "revision-a",
            1024,
        )
        payload_b = builder.payload_for_row(
            row,
            "asset-snapshot-20260721-r1",
            "embed-v1",
            "dense",
            "revision-b",
            "sparse",
            "revision-a",
            1024,
        )
        hash_a = builder.embedding_hash(
            "chair",
            payload_a,
            "asset-snapshot-20260721-r1",
            "embed-v1",
            "dense",
            "revision-a",
            "sparse",
            "revision-a",
            1024,
        )
        hash_b = builder.embedding_hash(
            "chair",
            payload_b,
            "asset-snapshot-20260721-r1",
            "embed-v1",
            "dense",
            "revision-b",
            "sparse",
            "revision-a",
            1024,
        )
        self.assertNotEqual(hash_a, hash_b)

    def test_snapshot_revision_change_invalidates_payload_and_hash(self):
        row_a = asset_row()
        payload_a = builder.payload_for_row(
            row_a,
            "asset-snapshot-20260721-r1",
            "embed-v1",
            "dense",
            "revision-a",
            "sparse",
            "revision-a",
            1024,
        )
        row_b = {**row_a, "asset_snapshot_revision": "asset-snapshot-20260721-r2"}
        payload_b = builder.payload_for_row(
            row_b,
            "asset-snapshot-20260721-r2",
            "embed-v1",
            "dense",
            "revision-a",
            "sparse",
            "revision-a",
            1024,
        )
        hash_a = builder.embedding_hash(
            "chair",
            payload_a,
            "asset-snapshot-20260721-r1",
            "embed-v1",
            "dense",
            "revision-a",
            "sparse",
            "revision-a",
            1024,
        )
        hash_b = builder.embedding_hash(
            "chair",
            payload_b,
            "asset-snapshot-20260721-r2",
            "embed-v1",
            "dense",
            "revision-a",
            "sparse",
            "revision-a",
            1024,
        )
        self.assertNotEqual(hash_a, hash_b)

    def test_postgres_or_qdrant_revision_mismatch_is_not_reused(self):
        row = asset_row()
        with self.assertRaisesRegex(ValueError, "does not match"):
            builder.payload_for_row(
                row,
                "asset-snapshot-20260721-r2",
                "embed-v1",
                "dense",
                "revision-a",
                "sparse",
                "revision-a",
                1024,
            )

        class Point:
            def __init__(self, payload):
                self.id = row["qdrant_point_id"]
                self.payload = payload

        class Client:
            def __init__(self, payload):
                self.payload = payload

            def retrieve(self, **_kwargs):
                return [Point(self.payload)]

        matching = builder.existing_qdrant_ids(
            Client(
                {
                    "asset_id": "chair",
                    "asset_snapshot_revision": "asset-snapshot-20260721-r1",
                }
            ),
            "assets",
            [row],
            "asset-snapshot-20260721-r1",
        )
        stale = builder.existing_qdrant_ids(
            Client(
                {
                    "asset_id": "chair",
                    "asset_snapshot_revision": "asset-snapshot-20260721-r0",
                }
            ),
            "assets",
            [row],
            "asset-snapshot-20260721-r1",
        )
        self.assertEqual(matching, {row["qdrant_point_id"]})
        self.assertEqual(stale, set())


if __name__ == "__main__":
    unittest.main()
