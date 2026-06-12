#!/usr/bin/env python3
"""Local dense+sparse embedding service for asset retrieval query embeddings."""
import os

from fastapi import FastAPI
from pydantic import BaseModel
from fastembed import SparseTextEmbedding, TextEmbedding
import uvicorn


PORT = int(os.environ.get("PORT", "7777"))
DENSE_MODEL = os.environ.get("EMBED_DENSE_MODEL", os.environ.get("EMBED_MODEL", "BAAI/bge-large-en-v1.5"))
SPARSE_MODEL = os.environ.get("EMBED_SPARSE_MODEL", "Qdrant/bm25")

_dense = None
_sparse = None


def models():
    global _dense, _sparse
    if _dense is None:
        print(f"Loading dense embedding model {DENSE_MODEL}...")
        _dense = TextEmbedding(DENSE_MODEL)
    if _sparse is None:
        print(f"Loading sparse embedding model {SPARSE_MODEL}...")
        _sparse = SparseTextEmbedding(SPARSE_MODEL)
    return _dense, _sparse

app = FastAPI()


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
        "sparse_model": SPARSE_MODEL,
    }


@app.get("/health")
def health():
    return {"status": "ok", "dense_model": DENSE_MODEL, "sparse_model": SPARSE_MODEL}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
