#!/usr/bin/env python3
"""Local dense+sparse embedding service for asset retrieval query embeddings.

The health contract deliberately reports the configured model artifact revisions.
It becomes ready only after both models have loaded successfully; the production
Compose profile preloads them during application startup.
"""

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from pydantic import BaseModel
from fastembed import SparseTextEmbedding, TextEmbedding
import uvicorn


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

_dense = None
_sparse = None
_observed_dense_size = None


def models():
    global _dense, _sparse, _observed_dense_size
    if _dense is None:
        print(f"Loading dense embedding model {DENSE_MODEL}...")
        dense = TextEmbedding(DENSE_MODEL, cache_dir=CACHE_DIR)
        observed_size = int(dense.embedding_size)
        if observed_size != DENSE_SIZE:
            raise RuntimeError(
                f"Configured dense size {DENSE_SIZE} does not match model size {observed_size}"
            )
        _dense = dense
        _observed_dense_size = observed_size
    if _sparse is None:
        print(f"Loading sparse embedding model {SPARSE_MODEL}...")
        _sparse = SparseTextEmbedding(SPARSE_MODEL, cache_dir=CACHE_DIR)
    return _dense, _sparse


@asynccontextmanager
async def lifespan(_app):
    if PRELOAD:
        models()
    yield


app = FastAPI(lifespan=lifespan)


class EmbedRequest(BaseModel):
    texts: list[str]


@app.post("/embed")
def embed(req: EmbedRequest):
    dense, sparse = models()
    dense_vecs = [v.tolist() for v in dense.embed(req.texts)]
    sparse_results = list(sparse.embed(req.texts))
    sparse_vecs = [
        {"indices": r.indices.tolist(), "values": r.values.tolist()}
        for r in sparse_results
    ]
    return {
        "dense": dense_vecs,
        "sparse": sparse_vecs,
        "dense_model": DENSE_MODEL,
        "dense_revision": DENSE_REVISION,
        "dense_size": _observed_dense_size or DENSE_SIZE,
        "sparse_model": SPARSE_MODEL,
        "sparse_revision": SPARSE_REVISION,
        "version": EMBED_VERSION,
    }


@app.get("/health")
def health():
    loaded = _dense is not None and _sparse is not None
    return {
        "schema": "simworld-embedding-health/v1",
        "status": "ready" if loaded else "starting",
        "models_loaded": loaded,
        "version": EMBED_VERSION,
        "dense_model": DENSE_MODEL,
        "dense_revision": DENSE_REVISION,
        "dense_size": _observed_dense_size or DENSE_SIZE,
        "sparse_model": SPARSE_MODEL,
        "sparse_revision": SPARSE_REVISION,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
