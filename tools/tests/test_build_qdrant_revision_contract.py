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
    def test_payload_records_both_immutable_model_revisions(self):
        payload = builder.payload_for_row(
            asset_row(),
            "embed-v1",
            "BAAI/bge-large-en-v1.5",
            "dense-sha256-abc",
            "Qdrant/bm25",
            "sparse-sha256-def",
            1024,
        )
        self.assertEqual(payload["embedding_version"], "embed-v1")
        self.assertEqual(payload["dense_revision"], "dense-sha256-abc")
        self.assertEqual(payload["sparse_revision"], "sparse-sha256-def")
        self.assertEqual(payload["dense_size"], 1024)

    def test_revision_change_invalidates_embedding_hash(self):
        row = asset_row()
        payload_a = builder.payload_for_row(
            row, "embed-v1", "dense", "revision-a", "sparse", "revision-a", 1024
        )
        payload_b = builder.payload_for_row(
            row, "embed-v1", "dense", "revision-b", "sparse", "revision-a", 1024
        )
        hash_a = builder.embedding_hash(
            "chair",
            payload_a,
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
            "embed-v1",
            "dense",
            "revision-b",
            "sparse",
            "revision-a",
            1024,
        )
        self.assertNotEqual(hash_a, hash_b)


if __name__ == "__main__":
    unittest.main()
