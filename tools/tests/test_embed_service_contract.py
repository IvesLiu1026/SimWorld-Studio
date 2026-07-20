from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


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

    def test_health_is_not_ready_until_both_models_are_loaded(self):
        starting = embed_service.health()
        self.assertEqual(starting["schema"], "simworld-embedding-health/v1")
        self.assertEqual(starting["status"], "starting")
        self.assertFalse(starting["models_loaded"])

        embed_service._dense = object()
        embed_service._sparse = object()
        embed_service._observed_dense_size = embed_service.DENSE_SIZE
        ready = embed_service.health()
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["models_loaded"])
        self.assertEqual(ready["dense_size"], embed_service.DENSE_SIZE)

    def test_model_load_checks_actual_dense_dimension(self):
        with mock.patch.object(
            embed_service,
            "TextEmbedding",
            return_value=FakeDense(embed_service.DENSE_SIZE - 1),
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
        ):
            self.assertEqual(embed_service.models(), (dense, sparse))

        dense_loader.assert_called_once_with(
            embed_service.DENSE_MODEL, cache_dir="/model-cache"
        )
        sparse_loader.assert_called_once_with(
            embed_service.SPARSE_MODEL, cache_dir="/model-cache"
        )
        self.assertEqual(embed_service.health()["status"], "ready")

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


if __name__ == "__main__":
    unittest.main()
