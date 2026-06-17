#!/usr/bin/env python3
"""Isolated Qwen VLM metadata comparison for existing asset renders.

This does not write to the production catalog, Postgres, or Qdrant. It reuses
stored render views and catalog geometry, calls an OpenAI-compatible chat
endpoint, saves all generated JSON outputs, and writes a compact visual report.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from regenerate_metadata_compare import (
    assemble_record,
    compare_records,
    existing_view_paths,
    find_catalog_records,
    load_json,
    parse_asset_ids,
    select_balanced,
    write_json,
)


DEFAULT_BASE_URL = "http://137.110.161.132:8005/v1"
DEFAULT_MODEL = "Qwen3.6-35B-A3B"


def data_url(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def qwen_prompt(rec: dict[str, Any], view_count: int) -> str:
    ident = rec.get("identity", {})
    geom = rec.get("geometry", {})
    dims = geom.get("dimensions_m", {})
    tech = rec.get("technical", {})
    categories = (
        "buildings, building_pieces, vegetation, nature_terrain, seating, furniture_indoor, "
        "lighting, signage, vehicles, carts_and_vendors, market_goods, industrial_goods, "
        "pipes_tanks_infra, tools_equipment, barriers_and_fencing, waste_and_bins, "
        "litter_and_debris, ground_and_road, decor_and_landmarks, religious_ritual, "
        "medieval_fantasy_props, sci_fi_props, winter_snow_props, agricultural_props, "
        "camping_outdoor, indoor_clutter"
    )
    settings = (
        "modern_urban, industrial, suburban_residential, commercial_retail, nature_rural, "
        "coastal_harbor, medieval, fantasy_gothic, ancient_temple, middle_eastern, "
        "east_asian, winter, sci_fi, indoor, generic"
    )
    return (
        "You are cataloging a single 3D asset for a multi-genre scene-generation asset database. "
        f"You are shown {view_count} rendered orbit views of ONE object. Ignore the gray ground plane and sky. "
        f"Measured size in meters: width {dims.get('width')}, depth {dims.get('depth')}, height {dims.get('height')}. "
        f"Asset name/path hints: name='{ident.get('name') or ident.get('asset_id')}', "
        f"source_pack='{ident.get('source_pack')}', unreal_path='{tech.get('unreal_asset_path')}'. "
        "Return ONLY a valid JSON object with exactly these keys: display_name, category, subcategory, "
        "short_description, description, tags, style, materials, color_palette, mood, typical_placement, "
        "function, affordances, scene_types, is_symmetric, condition, setting. "
        f"category must be one of: {categories}. setting must be one of: {settings}. "
        "Use 8-15 lowercase keyword tags. description should be 2-4 sentences. "
        "Do not include markdown, comments, or extra text."
    )


def extract_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return json.loads(raw[start : end + 1])
    raise ValueError(f"could not parse JSON object from response: {raw[:500]}")


def call_qwen(
    *,
    rec: dict[str, Any],
    view_paths: list[Path],
    base_url: str,
    model: str,
    enable_thinking: bool,
    timeout: int,
    max_tokens: int,
    temperature: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": qwen_prompt(rec, len(view_paths))}]
    for path in view_paths:
        content.append({"type": "image_url", "image_url": {"url": data_url(path)}})
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Qwen HTTP {exc.code}: {body[:2000]}") from exc
    duration = round(time.time() - started, 3)
    choice = (raw.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""
    return extract_json_object(text), {
        "duration_sec": duration,
        "usage": raw.get("usage") or {},
        "finish_reason": choice.get("finish_reason"),
        "raw_response": raw,
        "content": text,
    }


def copy_views(view_paths: list[Path], out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rels = []
    for src in view_paths:
        dest = out_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
        rels.append(str(dest))
    return rels


def esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def join_list(value: Any, limit: int = 12) -> str:
    if not isinstance(value, list):
        return esc(value)
    items = [str(x) for x in value]
    if len(items) > limit:
        items = items[:limit] + [f"+{len(value)-limit} more"]
    return esc(", ".join(items))


def image_row(asset_id: str, count: int) -> str:
    imgs = []
    for i in range(count):
        rel = f"views/{asset_id}/view_{i:02d}.png"
        imgs.append(f'<img src="{rel}" width="120">')
    return " ".join(imgs)


def summarize_usage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"count": len(rows)}
    for key in ["duration_sec", "prompt_tokens", "completion_tokens", "total_tokens"]:
        vals = []
        for row in rows:
            if key == "duration_sec":
                vals.append(float(row.get("duration_sec") or 0))
            else:
                vals.append(float((row.get("usage") or {}).get(key) or 0))
        out[key] = {
            "total": round(sum(vals), 3),
            "mean": round(sum(vals) / len(vals), 3) if vals else 0,
            "min": round(min(vals), 3) if vals else 0,
            "max": round(max(vals), 3) if vals else 0,
        }
    return out


def write_report(out_dir: Path, asset_ids: list[str], baseline: dict[str, dict[str, Any]], modes: dict[str, dict[str, dict[str, Any]]], usage_rows: dict[str, list[dict[str, Any]]]) -> None:
    lines = [
        "# Qwen Thinking Mode Asset Metadata Comparison",
        "",
        "Isolated comparison using existing render views. No production catalog, Postgres, or Qdrant writes were made.",
        "",
        "## Summary",
        "",
        "| mode | assets | avg sec | avg prompt tok | avg completion tok | avg total tok |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for mode, rows in usage_rows.items():
        s = summarize_usage(rows)
        lines.append(
            f"| {mode} | {s['count']} | {s['duration_sec']['mean']} | "
            f"{s['prompt_tokens']['mean']} | {s['completion_tokens']['mean']} | {s['total_tokens']['mean']} |"
        )
    lines.extend(["", "## Assets", ""])
    for asset_id in asset_ids:
        if asset_id not in baseline:
            continue
        old = baseline[asset_id]
        ident = old.get("identity", {})
        sem = old.get("semantic", {})
        view_count = len(list((out_dir / "views" / asset_id).glob("view_*.png")))
        lines.extend(
            [
                f"### `{asset_id}`",
                "",
                f"Baseline: **{esc(ident.get('name'))}** / `{esc(ident.get('category'))}` / `{esc(sem.get('setting'))}`",
                "",
                image_row(asset_id, view_count),
                "",
                "| mode | generated name | category | setting | short description | tags | materials | function | sec | prompt | completion | total |",
                "|---|---|---|---|---|---|---|---|---:|---:|---:|---:|",
            ]
        )
        for mode, by_asset in modes.items():
            if asset_id not in by_asset:
                continue
            rec = by_asset[asset_id]
            ident2 = rec.get("identity", {})
            sem2 = rec.get("semantic", {})
            usage = next((r for r in usage_rows[mode] if r.get("asset_id") == asset_id), {})
            u = usage.get("usage") or {}
            lines.append(
                f"| {mode} | {esc(ident2.get('name'))} | `{esc(ident2.get('category'))}` | `{esc(sem2.get('setting'))}` | "
                f"{esc(sem2.get('short_description'))} | {join_list(sem2.get('tags'))} | {join_list(sem2.get('materials'), 8)} | "
                f"{esc(sem2.get('function'))} | {usage.get('duration_sec')} | {u.get('prompt_tokens')} | {u.get('completion_tokens')} | {u.get('total_tokens')} |"
            )
        lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-db-dir", default="/data/siddhant/asset_db")
    parser.add_argument("--asset-ids")
    parser.add_argument("--asset-id-file")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260610)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--base-url", default=os.environ.get("QWEN_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("QWEN_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    asset_db_dir = Path(args.asset_db_dir)
    records = find_catalog_records(asset_db_dir)
    asset_ids = parse_asset_ids(args.asset_ids, args.asset_id_file)
    if not asset_ids:
        eligible = []
        for aid, rec_path in records.items():
            try:
                rec = load_json(rec_path)
                if len(existing_view_paths(asset_db_dir, rec)) >= 4:
                    eligible.append(aid)
            except Exception:
                pass
        random.Random(args.seed).shuffle(eligible)
        asset_ids = eligible[: args.limit]
    else:
        asset_ids = asset_ids[: args.limit]

    missing = [aid for aid in asset_ids if aid not in records]
    if missing:
        raise SystemExit(f"missing catalog records: {', '.join(missing[:10])}")

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir) if args.output_dir else asset_db_dir / "metadata_compare" / f"qwen36_10_thinking_compare_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "selected_assets.json", {"asset_ids": asset_ids, "seed": args.seed, "model": args.model, "base_url": args.base_url})

    modes_config = {"thinking_off": False, "thinking_on": True}
    baseline: dict[str, dict[str, Any]] = {}
    regenerated: dict[str, dict[str, dict[str, Any]]] = {mode: {} for mode in modes_config}
    usage_rows: dict[str, list[dict[str, Any]]] = {mode: [] for mode in modes_config}

    for idx, asset_id in enumerate(asset_ids, 1):
        old = load_json(records[asset_id])
        baseline[asset_id] = old
        views = existing_view_paths(asset_db_dir, old)[:8]
        if not views:
            raise RuntimeError(f"{asset_id}: no render views found")
        copy_views(views, out_dir / "views" / asset_id)
        for mode, enable_thinking in modes_config.items():
            rec_path = out_dir / "regenerated" / mode / f"{asset_id}.json"
            vlm_path = out_dir / "vlm_outputs" / mode / f"{asset_id}.json"
            usage_path = out_dir / "usage" / mode / f"{asset_id}.json"
            raw_path = out_dir / "raw_responses" / mode / f"{asset_id}.json"
            if rec_path.exists() and usage_path.exists() and not args.force:
                rec = load_json(rec_path)
                usage = load_json(usage_path)
                print(f"[{idx}/{len(asset_ids)} {mode}] reuse {asset_id}", flush=True)
            else:
                print(f"[{idx}/{len(asset_ids)} {mode}] qwen {asset_id} views={len(views)}", flush=True)
                vlm, trace = call_qwen(
                    rec=old,
                    view_paths=views,
                    base_url=args.base_url,
                    model=args.model,
                    enable_thinking=enable_thinking,
                    timeout=args.timeout,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                )
                rec = assemble_record(old, vlm, f"{args.model}:{mode}")
                usage = {
                    "asset_id": asset_id,
                    "mode": mode,
                    "enable_thinking": enable_thinking,
                    "model": args.model,
                    "duration_sec": trace["duration_sec"],
                    "usage": trace["usage"],
                    "finish_reason": trace["finish_reason"],
                }
                write_json(vlm_path, vlm)
                write_json(rec_path, rec)
                write_json(usage_path, usage)
                write_json(raw_path, trace["raw_response"])
                u = usage["usage"]
                print(
                    f"[{idx}/{len(asset_ids)} {mode}] sec={usage['duration_sec']} "
                    f"prompt={u.get('prompt_tokens')} completion={u.get('completion_tokens')} total={u.get('total_tokens')}",
                    flush=True,
                )
            regenerated[mode][asset_id] = rec
            usage_rows[mode].append(usage)
            write_json(out_dir / "usage_summary.json", {m: summarize_usage(rows) for m, rows in usage_rows.items()})
            write_json(out_dir / "comparisons.json", {
                mode: [compare_records(baseline[aid], regenerated[mode][aid]) for aid in regenerated[mode]]
                for mode in regenerated
            })
            write_report(out_dir, asset_ids, baseline, regenerated, usage_rows)
    print(f"wrote report: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
