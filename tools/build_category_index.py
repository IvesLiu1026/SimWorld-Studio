#!/usr/bin/env python3
"""Rebuild category_index.json from the asset catalog.

Run this after indexing new assets with index_assets.py to regenerate the index
that asset-retrieval.js loads at startup.

Required env var:
  ASSET_DB_DIR — root of the asset DB (catalog/ must exist inside it)

Optional:
  CATEGORIES_JSON — path to a JSON file with category descriptions
                    (default: <repo_root>/tools/categories.json relative to this script,
                     then $ASSET_DB_DIR/categories.json as fallback)

Output: $ASSET_DB_DIR/category_index.json (overwrites existing)
"""
import json, os, sys, glob

ASSET_DB_DIR = os.environ.get("ASSET_DB_DIR", "").strip()
if not ASSET_DB_DIR:
    print("ERROR: $ASSET_DB_DIR is required but not set.", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    sys.exit(1)

CATALOG = os.path.join(ASSET_DB_DIR, "catalog")
OUTPUT  = os.path.join(ASSET_DB_DIR, "category_index.json")

# Load category descriptions from tools/categories.json (in this repo) or fallback
_here = os.path.dirname(os.path.abspath(__file__))
_candidates = [
    os.environ.get("CATEGORIES_JSON", ""),
    os.path.join(_here, "categories.json"),
    os.path.join(ASSET_DB_DIR, "categories.json"),
]
_desc_map = {}
for _path in _candidates:
    if _path and os.path.exists(_path):
        try:
            _data = json.load(open(_path))
            # supports both list format and {"categories": [...]} format
            _items = _data if isinstance(_data, list) else _data.get("categories", [])
            for item in _items:
                _desc_map[item["id"]] = item.get("description", "")
            break
        except Exception as e:
            print(f"Warning: could not load {_path}: {e}", file=sys.stderr)

def build():
    if not os.path.isdir(CATALOG):
        print(f"ERROR: catalog directory not found at {CATALOG}", file=sys.stderr)
        sys.exit(1)

    cat_dirs = sorted([
        d for d in os.listdir(CATALOG)
        if os.path.isdir(os.path.join(CATALOG, d))
    ])

    categories = []
    total = 0
    for cat_id in cat_dirs:
        cat_dir = os.path.join(CATALOG, cat_id)
        asset_files = sorted(glob.glob(os.path.join(cat_dir, "*.json")))
        assets = []
        for fp in asset_files:
            try:
                rec = json.load(open(fp))
                ident = rec.get("identity", {})
                sem   = rec.get("semantic", {})
                assets.append({
                    "asset_id":   ident.get("asset_id", os.path.splitext(os.path.basename(fp))[0]),
                    "name":       ident.get("name", ""),
                    "subcategory": ident.get("subcategory", ""),
                    "setting":    sem.get("setting", "generic"),
                })
            except Exception as e:
                print(f"  Warning: skipping {fp}: {e}", file=sys.stderr)
        categories.append({
            "id":          cat_id,
            "description": _desc_map.get(cat_id, ""),
            "count":       len(assets),
            "assets":      assets,
        })
        total += len(assets)
        print(f"  {cat_id}: {len(assets)} assets")

    index = {
        "schema_version": "1.0",
        "total_assets":   total,
        "categories":     categories,
    }
    json.dump(index, open(OUTPUT, "w"), indent=2)
    print(f"\nWrote {OUTPUT}  ({len(categories)} categories, {total} total assets)")

if __name__ == "__main__":
    build()
