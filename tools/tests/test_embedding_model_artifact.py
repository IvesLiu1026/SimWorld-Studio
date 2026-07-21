from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from asset_stack_config import AssetStackConfigError, verify_embedding_model_artifact  # noqa: E402
from embedding_model_artifact import capture  # noqa: E402


class EmbeddingModelArtifactTests(unittest.TestCase):
    def test_capture_verify_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "model.onnx").write_bytes(b"verified-onnx")
            (root / "tokenizer.json").write_bytes(b'{"version":"1"}')
            _manifest, revision = capture(
                root,
                kind="dense",
                model_id="BAAI/bge-large-en-v1.5",
                dense_size=1024,
            )
            result = verify_embedding_model_artifact(
                root,
                model_id="BAAI/bge-large-en-v1.5",
                revision=revision,
                kind="dense",
                dense_size=1024,
            )
            self.assertEqual(result["revision"], revision)
            self.assertEqual(result["file_count"], 2)

            (root / "model.onnx").write_bytes(b"tampered")
            with self.assertRaises(AssetStackConfigError) as tampered:
                verify_embedding_model_artifact(
                    root,
                    model_id="BAAI/bge-large-en-v1.5",
                    revision=revision,
                    kind="dense",
                    dense_size=1024,
                )
            self.assertEqual(tampered.exception.code, "ASSET_MODEL_CHECKSUM_MISMATCH")

    def test_capture_refuses_symlinks_and_manifest_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            outside = root.parent / "outside-model-file"
            outside.write_bytes(b"outside")
            try:
                (root / "model.onnx").symlink_to(outside)
                with self.assertRaises(AssetStackConfigError) as symlink:
                    capture(root, kind="sparse", model_id="Qdrant/bm25", dense_size=None)
                self.assertEqual(symlink.exception.code, "ASSET_MODEL_SYMLINK_REJECTED")
            finally:
                outside.unlink(missing_ok=True)

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "model.bin").write_bytes(b"model")
            capture(root, kind="sparse", model_id="Qdrant/bm25", dense_size=None)
            with self.assertRaises(AssetStackConfigError) as immutable:
                capture(root, kind="sparse", model_id="Qdrant/bm25", dense_size=None)
            self.assertEqual(immutable.exception.code, "ASSET_MODEL_MANIFEST_EXISTS")


if __name__ == "__main__":
    unittest.main()
