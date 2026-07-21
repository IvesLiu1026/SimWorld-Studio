from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))
import embed_service  # noqa: E402


class FakeDense:
    def __init__(self, size):
        self.embedding_size = size


class ListValue(list):
    def tolist(self):
        return list(self)


class FakeDenseRuntime:
    def embed(self, texts):
        return [ListValue([0.1, 0.2]) for _ in texts]


class FakeSparseRuntime:
    def embed(self, texts):
        return [
            SimpleNamespace(indices=ListValue([1]), values=ListValue([0.5]))
            for _ in texts
        ]


class EmbedServiceContractTests(unittest.TestCase):
    def setUp(self):
        embed_service._dense = None
        embed_service._sparse = None
        embed_service._observed_dense_size = None
        embed_service._dense_artifact = None
        embed_service._sparse_artifact = None

    def test_health_is_not_ready_until_both_models_are_loaded(self):
        starting = embed_service.health()
        self.assertEqual(starting["schema"], "simworld-embedding-health/v1")
        self.assertEqual(starting["status"], "starting")
        self.assertFalse(starting["models_loaded"])

        embed_service._dense = object()
        embed_service._sparse = object()
        embed_service._observed_dense_size = embed_service.DENSE_SIZE
        embed_service._dense_artifact = {"manifest_sha256": "a" * 64}
        embed_service._sparse_artifact = {"manifest_sha256": "b" * 64}
        ready = embed_service.health()
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["models_loaded"])
        self.assertEqual(ready["dense_size"], embed_service.DENSE_SIZE)

    def test_model_load_checks_actual_dense_dimension(self):
        with mock.patch.object(
            embed_service,
            "TextEmbedding",
            return_value=FakeDense(embed_service.DENSE_SIZE - 1),
        ), mock.patch.object(
            embed_service,
            "verify_embedding_model_artifact",
            return_value={"manifest_sha256": "a" * 64},
        ):
            with self.assertRaisesRegex(RuntimeError, "does not match model size"):
                embed_service.models()
        self.assertIsNone(embed_service._dense)
        self.assertIsNone(embed_service._sparse)

    def test_model_cache_path_is_passed_to_both_loaders(self):
        dense = FakeDense(embed_service.DENSE_SIZE)
        sparse = object()
        with (
            mock.patch.object(embed_service, "CACHE_DIR", "/model-cache"),
            mock.patch.object(
                embed_service, "TextEmbedding", return_value=dense
            ) as dense_loader,
            mock.patch.object(
                embed_service, "SparseTextEmbedding", return_value=sparse
            ) as sparse_loader,
            mock.patch.object(
                embed_service,
                "verify_embedding_model_artifact",
                side_effect=[
                    {"manifest_sha256": "a" * 64},
                    {"manifest_sha256": "b" * 64},
                ],
            ),
        ):
            self.assertEqual(embed_service.models(), (dense, sparse))

        dense_loader.assert_called_once_with(
            embed_service.DENSE_MODEL,
            cache_dir="/model-cache",
            specific_model_path=embed_service.DENSE_MODEL_PATH,
            local_files_only=True,
        )
        sparse_loader.assert_called_once_with(
            embed_service.SPARSE_MODEL,
            cache_dir="/model-cache",
            specific_model_path=embed_service.SPARSE_MODEL_PATH,
            local_files_only=True,
        )
        self.assertEqual(embed_service.health()["status"], "ready")

    def test_unverified_artifact_prevents_any_model_loader_call(self):
        error = RuntimeError("artifact mismatch")
        with (
            mock.patch.object(
                embed_service,
                "verify_embedding_model_artifact",
                side_effect=error,
            ),
            mock.patch.object(embed_service, "TextEmbedding") as dense_loader,
            mock.patch.object(embed_service, "SparseTextEmbedding") as sparse_loader,
            self.assertRaisesRegex(RuntimeError, "artifact mismatch"),
        ):
            embed_service.models()
        dense_loader.assert_not_called()
        sparse_loader.assert_not_called()

    def test_embed_response_carries_the_same_revision_contract(self):
        embed_service._dense = FakeDenseRuntime()
        embed_service._sparse = FakeSparseRuntime()
        embed_service._observed_dense_size = 2
        response = embed_service.embed(
            embed_service.EmbedRequest(texts=["office chair"])
        )

        self.assertEqual(response["dense"], [[0.1, 0.2]])
        self.assertEqual(response["sparse"], [{"indices": [1], "values": [0.5]}])
        self.assertEqual(response["version"], embed_service.EMBED_VERSION)
        self.assertEqual(response["dense_revision"], embed_service.DENSE_REVISION)
        self.assertEqual(response["sparse_revision"], embed_service.SPARSE_REVISION)
        self.assertEqual(response["dense_size"], 2)

    def test_configured_bearer_token_is_required_without_leaking_it(self):
        token = "embed-token-that-must-never-appear-in-errors"
        with mock.patch.object(embed_service, "EMBED_SERVICE_TOKEN", token):
            with self.assertRaises(HTTPException) as denied:
                embed_service.require_embed_authorization("Bearer wrong")
            self.assertEqual(denied.exception.status_code, 401)
            self.assertNotIn(token, str(denied.exception.detail))
            embed_service.require_embed_authorization(f"Bearer {token}")


if __name__ == "__main__":
    unittest.main()
