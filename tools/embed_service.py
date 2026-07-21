#!/usr/bin/env python3
"""Local dense+sparse embedding service for asset retrieval query embeddings.

The health contract deliberately reports the configured model artifact revisions.
It becomes ready only after both models have loaded successfully; the production
Compose profile preloads them during application startup.
"""

from contextlib import asynccontextmanager
import math
import os
import pathlib
import secrets
import unicodedata

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastembed import SparseTextEmbedding, TextEmbedding
import uvicorn

try:
    from asset_stack_config import (
        load_secret,
        require_secret_minimum_bytes,
        verify_embedding_model_artifact,
    )
except ModuleNotFoundError:  # Imported as tools.embed_service in tests.
    from tools.asset_stack_config import (
        load_secret,
        require_secret_minimum_bytes,
        verify_embedding_model_artifact,
    )


PORT = int(os.environ.get("PORT", "7777"))
DENSE_MODEL = os.environ.get(
    "EMBED_DENSE_MODEL", os.environ.get("EMBED_MODEL", "BAAI/bge-large-en-v1.5")
)
SPARSE_MODEL = os.environ.get("EMBED_SPARSE_MODEL", "Qdrant/bm25")
DENSE_REVISION = os.environ.get("EMBED_DENSE_REVISION", "unversioned")
SPARSE_REVISION = os.environ.get("EMBED_SPARSE_REVISION", "unversioned")
DENSE_SIZE = int(os.environ.get("EMBED_DENSE_SIZE", "1024"))
EMBED_VERSION = os.environ.get("EMBED_VERSION", "bge-large-en-v1.5-bm25-v1")
PRELOAD = os.environ.get("EMBED_PRELOAD", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CACHE_DIR = os.environ.get("FASTEMBED_CACHE_PATH") or None
DENSE_MODEL_PATH = os.environ.get("EMBED_DENSE_MODEL_PATH", "")
SPARSE_MODEL_PATH = os.environ.get("EMBED_SPARSE_MODEL_PATH", "")
EMBED_SERVICE_TOKEN, EMBED_SERVICE_TOKEN_SOURCE = load_secret(
    os.environ,
    "EMBED_SERVICE_TOKEN",
    "EMBED_SERVICE_TOKEN_FILE",
    required=False,
)
if EMBED_SERVICE_TOKEN:
    require_secret_minimum_bytes(
        EMBED_SERVICE_TOKEN, EMBED_SERVICE_TOKEN_SOURCE or "EMBED_SERVICE_TOKEN"
    )

_dense = None
_sparse = None
_observed_dense_size = None
_dense_artifact = None
_sparse_artifact = None

MAX_EMBED_TEXTS = 4096
MAX_EMBED_TEXT_BYTES = 16 * 1024
MAX_EMBED_TOTAL_TEXT_BYTES = 4 * 1024 * 1024
MAX_SPARSE_ENTRIES_PER_TEXT = 1_000_000


def models():
    global _dense, _sparse, _observed_dense_size, _dense_artifact, _sparse_artifact
    if _dense is None:
        _dense_artifact = verify_embedding_model_artifact(
            pathlib.Path(DENSE_MODEL_PATH),
            model_id=DENSE_MODEL,
            revision=DENSE_REVISION,
            kind="dense",
            dense_size=DENSE_SIZE,
        )
        print(f"Loading dense embedding model {DENSE_MODEL}...")
        dense = TextEmbedding(
            DENSE_MODEL,
            cache_dir=CACHE_DIR,
            specific_model_path=DENSE_MODEL_PATH,
            local_files_only=True,
        )
        observed_size = int(dense.embedding_size)
        if observed_size != DENSE_SIZE:
            raise RuntimeError(
                f"Configured dense size {DENSE_SIZE} does not match model size {observed_size}"
            )
        _dense = dense
        _observed_dense_size = observed_size
    if _sparse is None:
        _sparse_artifact = verify_embedding_model_artifact(
            pathlib.Path(SPARSE_MODEL_PATH),
            model_id=SPARSE_MODEL,
            revision=SPARSE_REVISION,
            kind="sparse",
        )
        print(f"Loading sparse embedding model {SPARSE_MODEL}...")
        _sparse = SparseTextEmbedding(
            SPARSE_MODEL,
            cache_dir=CACHE_DIR,
            specific_model_path=SPARSE_MODEL_PATH,
            local_files_only=True,
        )
    return _dense, _sparse


@asynccontextmanager
async def lifespan(_app):
    if PRELOAD:
        models()
    yield


app = FastAPI(lifespan=lifespan)


class EmbedRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    texts: list[str] = Field(min_length=1, max_length=MAX_EMBED_TEXTS)

    @field_validator("texts")
    @classmethod
    def validate_text_bounds(cls, texts: list[str]) -> list[str]:
        total_bytes = 0
        for text in texts:
            if not text or any(
                unicodedata.category(character) == "Cc" for character in text
            ):
                raise ValueError("embedding text is empty or contains control characters")
            if not unicodedata.is_normalized("NFC", text):
                raise ValueError("embedding text must already be Unicode NFC")
            size = len(text.encode("utf-8"))
            if size > MAX_EMBED_TEXT_BYTES:
                raise ValueError("embedding text exceeds the per-item byte limit")
            total_bytes += size
            if total_bytes > MAX_EMBED_TOTAL_TEXT_BYTES:
                raise ValueError("embedding request exceeds the total byte limit")
        return texts


def _finite_vector(values, *, expected_size: int) -> list[float]:
    vector = values.tolist()
    if len(vector) != expected_size:
        raise RuntimeError("dense embedding output dimension is invalid")
    normalized: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("dense embedding output contains an invalid value")
        number = float(value)
        if not math.isfinite(number):
            raise RuntimeError("dense embedding output contains a non-finite value")
        normalized.append(number)
    return normalized


def _bounded_sparse_vector(result) -> dict[str, list[int] | list[float]]:
    indices = result.indices.tolist()
    values = result.values.tolist()
    if (
        len(indices) != len(values)
        or len(indices) > MAX_SPARSE_ENTRIES_PER_TEXT
    ):
        raise RuntimeError("sparse embedding output shape is invalid")
    normalized_indices: list[int] = []
    normalized_values: list[float] = []
    previous = -1
    for index, value in zip(indices, values, strict=True):
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise RuntimeError("sparse embedding output contains an invalid index")
        if index <= previous:
            raise RuntimeError("sparse embedding indices must be strictly increasing")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("sparse embedding output contains an invalid value")
        number = float(value)
        if not math.isfinite(number):
            raise RuntimeError("sparse embedding output contains a non-finite value")
        normalized_indices.append(index)
        normalized_values.append(number)
        previous = index
    return {"indices": normalized_indices, "values": normalized_values}


def embed(req: EmbedRequest):
    dense, sparse = models()
    expected_dense_size = _observed_dense_size or DENSE_SIZE
    dense_results = list(dense.embed(req.texts))
    sparse_results = list(sparse.embed(req.texts))
    if len(dense_results) != len(req.texts) or len(sparse_results) != len(req.texts):
        raise RuntimeError("embedding output count does not match the request")
    dense_vecs = [
        _finite_vector(result, expected_size=expected_dense_size)
        for result in dense_results
    ]
    sparse_vecs = [_bounded_sparse_vector(result) for result in sparse_results]
    return {
        "dense": dense_vecs,
        "sparse": sparse_vecs,
        "dense_model": DENSE_MODEL,
        "dense_revision": DENSE_REVISION,
        "dense_size": expected_dense_size,
        "sparse_model": SPARSE_MODEL,
        "sparse_revision": SPARSE_REVISION,
        "version": EMBED_VERSION,
    }


def require_embed_authorization(authorization: str) -> None:
    if not EMBED_SERVICE_TOKEN:
        return
    expected = f"Bearer {EMBED_SERVICE_TOKEN}"
    if not secrets.compare_digest(str(authorization or ""), expected):
        raise HTTPException(status_code=401, detail="embedding service authorization required")


@app.post("/embed")
def embed_endpoint(
    req: EmbedRequest,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_embed_authorization(authorization)
    return embed(req)


@app.get("/health")
def health():
    loaded = (
        _dense is not None
        and _sparse is not None
        and _dense_artifact is not None
        and _sparse_artifact is not None
    )
    return {
        "schema": "simworld-embedding-health/v1",
        "status": "ready" if loaded else "starting",
        "models_loaded": loaded,
        "authentication_required": bool(EMBED_SERVICE_TOKEN),
        "version": EMBED_VERSION,
        "dense_model": DENSE_MODEL,
        "dense_revision": DENSE_REVISION,
        "dense_size": _observed_dense_size or DENSE_SIZE,
        "dense_artifact_sha256": (
            _dense_artifact or {}
        ).get("manifest_sha256"),
        "sparse_model": SPARSE_MODEL,
        "sparse_revision": SPARSE_REVISION,
        "sparse_artifact_sha256": (
            _sparse_artifact or {}
        ).get("manifest_sha256"),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
