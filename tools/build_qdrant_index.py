#!/usr/bin/env python3
"""Build or update the Qdrant asset text index from Postgres using local FastEmbed models."""
import argparse
import hashlib
import json
import os
import pathlib
import sys

import psycopg2
import psycopg2.extras
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PayloadSchemaType,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)


POSTGRES_URL = os.environ.get("POSTGRES_URL")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "assets")
DENSE_MODEL = os.environ.get("EMBED_DENSE_MODEL", os.environ.get("EMBED_MODEL", "BAAI/bge-large-en-v1.5"))
SPARSE_MODEL = os.environ.get("EMBED_SPARSE_MODEL", "Qdrant/bm25")
DENSE_REVISION = os.environ.get("EMBED_DENSE_REVISION", "")
SPARSE_REVISION = os.environ.get("EMBED_SPARSE_REVISION", "")
DENSE_SIZE = int(os.environ.get("EMBED_DENSE_SIZE", "1024"))
EMBED_VER = os.environ.get("EMBED_VERSION", "bge-large-en-v1.5-bm25-v1")
BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "32"))
EMBED_CACHE_DIR = os.environ.get("FASTEMBED_CACHE_PATH") or None


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--postgres-url", default=POSTGRES_URL)
    p.add_argument("--qdrant-url", default=QDRANT_URL)
    p.add_argument("--collection", default=COLLECTION)
    p.add_argument("--dense-model", default=DENSE_MODEL)
    p.add_argument("--sparse-model", default=SPARSE_MODEL)
    p.add_argument("--dense-revision", default=DENSE_REVISION)
    p.add_argument("--sparse-revision", default=SPARSE_REVISION)
    p.add_argument("--dense-size", type=int, default=DENSE_SIZE)
    p.add_argument("--embed-version", default=EMBED_VER)
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--asset-ids", default="", help="Comma/newline-separated asset ids to upsert")
    p.add_argument("--asset-id-file", default="", help="Text/JSON file of asset ids to upsert")
    p.add_argument("--dry-run", action="store_true", help="Resolve pending rows but do not load models or upsert")
    p.add_argument("--force", action="store_true", help="Re-embed/upsert all selected rows even when hashes match")
    p.add_argument("--skip-point-check", action="store_true", help="Do not verify Qdrant point existence before skipping rows")
    return p.parse_args()


def read_asset_ids(raw: str, file_path: str) -> list[str]:
    ids: list[str] = []
    if raw:
        ids.extend(x.strip() for x in raw.replace("\n", ",").split(",") if x.strip())
    if file_path:
        text = pathlib.Path(file_path).read_text(encoding="utf-8").strip()
        if text.startswith("{") or text.startswith("["):
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("asset_ids"), list):
                ids.extend(str(x) for x in data["asset_ids"])
            elif isinstance(data, dict) and isinstance(data.get("assets"), list):
                ids.extend(str(x["asset_id"]) for x in data["assets"] if x.get("asset_id"))
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        ids.append(item)
                    elif isinstance(item, dict) and item.get("asset_id"):
                        ids.append(str(item["asset_id"]))
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                ids.append(line.split()[0].strip(","))
    seen = set()
    out = []
    for asset_id in ids:
        if asset_id not in seen:
            out.append(asset_id)
            seen.add(asset_id)
    return out


def build_embedding_text(row):
    parts = [
        f"name: {row['name'] or ''}",
        f"category: {row['category'] or ''}",
        f"subcategory: {row['subcategory'] or ''}",
        f"setting: {row['setting'] or ''}",
        f"style: {row['style'] or ''}",
        f"condition: {row['condition'] or ''}",
        f"scene types: {', '.join(row['scene_types'] or [])}",
        f"tags: {', '.join(row['tags'] or [])}",
        f"materials: {', '.join(row['materials'] or [])}",
        f"mood: {', '.join(row['mood'] or [])}",
        f"typical placement: {', '.join(row['typical_placement'] or [])}",
        f"function: {row['function'] or ''}",
        f"affordances: {', '.join(row['affordances'] or [])}",
        f"short description: {row['short_description'] or ''}",
        f"description: {row['description'] or ''}",
    ]
    return " | ".join(p for p in parts if not p.endswith(": "))


def payload_for_row(
    row,
    embed_version: str,
    dense_model: str,
    dense_revision: str,
    sparse_model: str,
    sparse_revision: str,
    dense_size: int,
):
    return {
        "asset_id": row["asset_id"],
        "name": row["name"],
        "category": row["category"],
        "subcategory": row["subcategory"] or "",
        "setting": row["setting"] or "generic",
        "style": row["style"] or "",
        "scene_types": row["scene_types"] or [],
        "tags": row["tags"] or [],
        "materials": row["materials"] or [],
        "asset_type": row["asset_type"] or "",
        "source_pack": row["source_pack"] or "",
        "unreal_asset_path": row["unreal_asset_path"] or "",
        "short_description": row["short_description"] or "",
        "width_m": float(row["width_m"] or 0),
        "depth_m": float(row["depth_m"] or 0),
        "height_m": float(row["height_m"] or 0),
        "triangle_count": int(row["triangle_count"] or 0),
        "embedding_version": embed_version,
        "dense_model": dense_model,
        "dense_revision": dense_revision,
        "sparse_model": sparse_model,
        "sparse_revision": sparse_revision,
        "dense_size": int(dense_size),
    }


def embedding_hash(
    text,
    payload,
    embed_version: str,
    dense_model: str,
    dense_revision: str,
    sparse_model: str,
    sparse_revision: str,
    dense_size: int,
):
    material = {
        "text": text,
        "payload": payload,
        "embed_version": embed_version,
        "dense_model": dense_model,
        "dense_revision": dense_revision,
        "sparse_model": sparse_model,
        "sparse_revision": sparse_revision,
        "dense_size": int(dense_size),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def existing_qdrant_ids(qd, collection: str, rows) -> set[str]:
    ids = [str(row["qdrant_point_id"]) for row in rows]
    found: set[str] = set()
    for i in range(0, len(ids), 256):
        batch = ids[i : i + 256]
        points = qd.retrieve(
            collection_name=collection,
            ids=batch,
            with_payload=False,
            with_vectors=False,
        )
        for point in points:
            found.add(str(point.id))
    return found


def ensure_collection(qd, collection: str, dense_size: int):
    existing = [c.name for c in qd.get_collections().collections]
    if collection not in existing:
        qd.create_collection(
            collection_name=collection,
            vectors_config={"text_dense": VectorParams(size=dense_size, distance=Distance.COSINE)},
            sparse_vectors_config={"text_sparse": SparseVectorParams()},
        )
        print(f"Created Qdrant collection: {collection}")

    indexes = [
        ("asset_id", PayloadSchemaType.KEYWORD),
        ("category", PayloadSchemaType.KEYWORD),
        ("setting", PayloadSchemaType.KEYWORD),
        ("asset_type", PayloadSchemaType.KEYWORD),
        ("source_pack", PayloadSchemaType.KEYWORD),
        ("scene_types", PayloadSchemaType.KEYWORD),
        ("tags", PayloadSchemaType.KEYWORD),
        ("width_m", PayloadSchemaType.FLOAT),
        ("depth_m", PayloadSchemaType.FLOAT),
        ("height_m", PayloadSchemaType.FLOAT),
        ("triangle_count", PayloadSchemaType.INTEGER),
        ("embedding_version", PayloadSchemaType.KEYWORD),
        ("dense_model", PayloadSchemaType.KEYWORD),
        ("dense_revision", PayloadSchemaType.KEYWORD),
        ("sparse_model", PayloadSchemaType.KEYWORD),
        ("sparse_revision", PayloadSchemaType.KEYWORD),
        ("dense_size", PayloadSchemaType.INTEGER),
    ]
    for field, schema in indexes:
        try:
            qd.create_payload_index(collection, field_name=field, field_schema=schema)
        except Exception:
            pass


def main():
    global EMBED_VER
    args = parse_args()
    if not args.postgres_url:
        print("ERROR: POSTGRES_URL is required", file=sys.stderr)
        sys.exit(2)
    if not args.dense_revision or args.dense_revision.casefold() in {"dev", "latest", "main", "master", "unknown", "unversioned"}:
        print("ERROR: EMBED_DENSE_REVISION must identify an immutable model artifact revision", file=sys.stderr)
        sys.exit(2)
    if not args.sparse_revision or args.sparse_revision.casefold() in {"dev", "latest", "main", "master", "unknown", "unversioned"}:
        print("ERROR: EMBED_SPARSE_REVISION must identify an immutable model artifact revision", file=sys.stderr)
        sys.exit(2)
    EMBED_VER = args.embed_version

    asset_ids = read_asset_ids(args.asset_ids, args.asset_id_file)
    qd = QdrantClient(url=args.qdrant_url)
    collections = [c.name for c in qd.get_collections().collections]
    collection_exists = args.collection in collections
    if args.dry_run:
        print(f"Dry run: Qdrant collection exists={collection_exists}; no collection/index mutation will be performed.")
    else:
        ensure_collection(qd, args.collection, args.dense_size)
        collection_exists = True

    conn = psycopg2.connect(args.postgres_url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if asset_ids:
        cur.execute("SELECT * FROM assets WHERE asset_id = ANY(%s) ORDER BY asset_id", (asset_ids,))
    else:
        cur.execute("SELECT * FROM assets ORDER BY asset_id")
    rows = cur.fetchall()
    print(f"Processing {len(rows)} assets...")
    if asset_ids and len(rows) != len(asset_ids):
        found = {row["asset_id"] for row in rows}
        missing = [asset_id for asset_id in asset_ids if asset_id not in found]
        print(f"ERROR: {len(missing)} requested asset ids are not present in Postgres", file=sys.stderr)
        for asset_id in missing[:20]:
            print(f"  missing: {asset_id}", file=sys.stderr)
        sys.exit(1)

    qdrant_ids = set()
    if collection_exists and not args.skip_point_check:
        qdrant_ids = existing_qdrant_ids(qd, args.collection, rows)
        print(f"Qdrant point check: found={len(qdrant_ids)}/{len(rows)} selected rows")
    elif not collection_exists:
        print("Qdrant point check: collection missing; all selected rows need upsert")
    else:
        print("Qdrant point check skipped")

    pending = []
    for row in rows:
        text = build_embedding_text(row)
        payload = payload_for_row(
            row,
            args.embed_version,
            args.dense_model,
            args.dense_revision,
            args.sparse_model,
            args.sparse_revision,
            args.dense_size,
        )
        h = embedding_hash(
            text,
            payload,
            args.embed_version,
            args.dense_model,
            args.dense_revision,
            args.sparse_model,
            args.sparse_revision,
            args.dense_size,
        )
        point_missing = (not collection_exists) or (
            not args.skip_point_check and str(row["qdrant_point_id"]) not in qdrant_ids
        )
        if not args.force and not point_missing and row["embedding_hash"] == h and row["embedding_version"] == args.embed_version:
            continue
        pending.append((row, text, h, payload))
    print(f"Need to embed/upsert: {len(pending)} assets; unchanged: {len(rows) - len(pending)}")
    if args.dry_run or not pending:
        return

    print(f"Loading dense embedding model {args.dense_model}...")
    from fastembed import SparseTextEmbedding, TextEmbedding

    dense_model = TextEmbedding(args.dense_model, cache_dir=EMBED_CACHE_DIR)
    observed_dense_size = int(dense_model.embedding_size)
    if observed_dense_size != args.dense_size:
        print(
            f"ERROR: configured dense size {args.dense_size} does not match model size {observed_dense_size}",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"Loading sparse embedding model {args.sparse_model}...")
    sparse_model = SparseTextEmbedding(args.sparse_model, cache_dir=EMBED_CACHE_DIR)
    print("Embedding models loaded.")

    batch_size = max(1, args.batch_size)
    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        texts = [t for _, t, _, _ in batch]
        dense_vecs = [v.tolist() for v in dense_model.embed(texts)]
        sparse_res = list(sparse_model.embed(texts))

        points = []
        for (row, _, h, payload), dvec, sres in zip(batch, dense_vecs, sparse_res):
            points.append(
                PointStruct(
                    id=str(row["qdrant_point_id"]),
                    vector={
                        "text_dense": dvec,
                        "text_sparse": SparseVector(
                            indices=sres.indices.tolist(),
                            values=sres.values.tolist(),
                        ),
                    },
                    payload=payload,
                )
            )

        qd.upsert(collection_name=args.collection, points=points, wait=True)
        upd = conn.cursor()
        for row, _, h, _ in batch:
            upd.execute(
                "UPDATE assets SET embedding_hash=%s, embedding_version=%s, updated_at=now() WHERE asset_id=%s",
                (h, args.embed_version, row["asset_id"]),
            )
        conn.commit()
        print(f"Upserted {min(i + len(batch), len(pending))}/{len(pending)}")

    print("Qdrant index build complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
