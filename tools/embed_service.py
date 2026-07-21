#!/usr/bin/env python3
"""Local dense+sparse embedding service for asset retrieval query embeddings.

The health contract deliberately reports the configured model artifact revisions.
It becomes ready only after both models have loaded successfully; the production
Compose profile preloads them during application startup.
"""

from contextlib import asynccontextmanager
import os
import pathlib
import secrets

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
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
    texts: list[str]


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
