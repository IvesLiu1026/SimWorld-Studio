#!/usr/bin/env python3
"""UE 5.8 parallel render runner for the asset indexing pipeline.

This runner launches multiple isolated UE 5.8 editor workers on one GPU, renders
assets in parallel, then serially runs the configured VLM and catalog/DB sync.
It writes the same run state/log artifacts as full_asset_index_runner.py so the
existing status and resume tooling can read the run directory.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import collections
import os
import pathlib
import random
import shutil
import socket
import subprocess
import sys
import time
from typing import Any

import full_asset_index_runner as runner
import ue_multi_instance_smoke as smoke


DEFAULT_ASSET_DB_DIR = pathlib.Path("/data/siddhant/asset_db_ue58_qwen")
DEFAULT_MANIFEST = pathlib.Path("/data/siddhant/asset_db/ue58_object_manifest.json")
DEFAULT_RUN_ROOT = DEFAULT_ASSET_DB_DIR / "runs"
DEFAULT_POSTGRES_URL = "postgresql://simworld:simworld@127.0.0.1:55432/asset_db_ue58_qwen"
DEFAULT_QDRANT_COLLECTION = "assets_ue58_qwen"
DEFAULT_MIN_INOTIFY_WATCHES = 524288


def port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def read_int_file(path: str) -> int | None:
    try:
        return int(pathlib.Path(path).read_text(encoding="utf-8").strip())
    except Exception:
        return None


def ensure_asset_db_layout(asset_db_dir: pathlib.Path, manifest: pathlib.Path) -> None:
    asset_db_dir.mkdir(parents=True, exist_ok=True)
    (asset_db_dir / "catalog").mkdir(parents=True, exist_ok=True)
    (asset_db_dir / "renders").mkdir(parents=True, exist_ok=True)
    (asset_db_dir / "schema").mkdir(parents=True, exist_ok=True)
    schema_src = runner.REPO_ROOT / "tools" / "vlm_output_schema.json"
    categories_src = runner.REPO_ROOT / "tools" / "categories.json"
    if schema_src.exists():
        shutil.copy2(schema_src, asset_db_dir / "schema" / "vlm_output_schema.json")
    if categories_src.exists():
        shutil.copy2(categories_src, asset_db_dir / "categories.json")
    if manifest.exists():
        shutil.copy2(manifest, asset_db_dir / "manifest_ue58_object.json")


def run_parallel_preflight(args: argparse.Namespace, schema: pathlib.Path) -> None:
    if args.caption_provider == "qwen":
        if not args.skip_provider_check:
            runner.check_qwen(args.qwen_base_url, args.qwen_model)
    elif args.caption_provider == "codex":
        if not args.skip_provider_check:
            runner.check_codex(args.codex_bin)
    else:
        raise RuntimeError(f"unsupported caption provider: {args.caption_provider}")
    if not schema.exists():
        raise RuntimeError(f"schema not found: {schema}")
    args.asset_db_dir.mkdir(parents=True, exist_ok=True)
    (args.asset_db_dir / "catalog").mkdir(parents=True, exist_ok=True)
    (args.asset_db_dir / "renders").mkdir(parents=True, exist_ok=True)
    if not args.dry_run and not args.skip_service_checks and not args.no_db_sync:
        try:
            runner.check_postgres(args.postgres_url)
            runner.check_qdrant(args.qdrant_url)
        except Exception:
            if args.allow_db_offline:
                print("Warning: DB service check failed; continuing because --allow-db-offline is set.", file=sys.stderr)
            else:
                raise


def select_assets_for_parallel(args: argparse.Namespace, run_dir: pathlib.Path) -> list[dict[str, Any]]:
    manifest = runner.load_json(args.manifest)
    asset_ids = runner.read_asset_ids(args.asset_ids, args.asset_id_file)
    selected_file = run_dir / "selected_asset_ids.txt"
    if selected_file.exists() and not args.force_reselect and not asset_ids:
        asset_ids = runner.read_asset_ids("", str(selected_file))
    assets = runner.select_assets(manifest, asset_ids, None)
    if args.sample_size and not selected_file.exists():
        rng = random.Random(args.sample_seed)
        sample_size = min(args.sample_size, len(assets))
        assets = rng.sample(assets, sample_size)
    elif args.limit is not None:
        assets = assets[: args.limit]
    return assets


def check_parallel_config(args: argparse.Namespace) -> int:
    args.asset_db_dir = pathlib.Path(args.asset_db_dir)
    args.manifest = pathlib.Path(args.manifest)
    ensure_asset_db_layout(args.asset_db_dir, args.manifest)
    run_dir = pathlib.Path(args.run_dir) if args.run_dir else DEFAULT_RUN_ROOT / "preflight"
    assets = select_assets_for_parallel(args, run_dir)
    existing = runner.catalog_records(args.asset_db_dir)
    pending = [asset for asset in assets if asset.get("asset_id") not in existing]
    print(f"asset_db_dir: {args.asset_db_dir}")
    print(f"manifest: {args.manifest}")
    print(f"manifest assets selected: {len(assets)}")
    print(f"existing catalog JSONs in this asset DB: {len(existing)}")
    print(f"pending selected assets: {len(pending)}")
    print(f"caption provider: {args.caption_provider}")
    if args.caption_provider == "qwen":
        print(f"qwen model: {args.qwen_model}")
        if not args.skip_provider_check:
            print(f"qwen: {runner.check_qwen(args.qwen_base_url, args.qwen_model)}")
    elif not args.skip_provider_check:
        print(f"codex: {runner.check_codex(args.codex_bin)}")
    current_watches = read_int_file("/proc/sys/fs/inotify/max_user_watches")
    print(f"inotify max_user_watches: {current_watches}")
    if current_watches is not None and current_watches < args.min_inotify_watches and not args.allow_low_inotify:
        print(
            f"inotify: FAIL current {current_watches} < required {args.min_inotify_watches}; "
            "ask admin to raise fs.inotify.max_user_watches",
            file=sys.stderr,
        )
        return 1
    if not args.skip_service_checks:
        runner.check_postgres(args.postgres_url)
        print("postgres: OK")
        runner.check_qdrant(args.qdrant_url)
        print("qdrant: OK")
    print("pending sample:")
    for asset in pending[:10]:
        print(f"  {asset.get('asset_id')} | {asset.get('source_pack')} | {asset.get('ue_name')}")
    return 0


def launch_workers(args: argparse.Namespace, run_dir: pathlib.Path) -> list[dict[str, Any]]:
    worker_root = pathlib.Path(args.worker_project_root)
    project_dirs = [worker_root / f"inst_{i}" for i in range(args.workers)]
    if args.ddc_mode == "local":
        smoke.seed_ue58_ddc(pathlib.Path(args.source_project), pathlib.Path(args.ddc_root))
    for project_dir in project_dirs:
        smoke.prepare_ue58_project(project_dir, pathlib.Path(args.source_project), pathlib.Path(args.content_root))

    occupied = []
    for i in range(args.workers):
        for port in (args.base_mcp_port + i, args.base_official_mcp_port + i, args.base_ucv_port + i):
            if port_is_open(port):
                occupied.append(port)
    if occupied:
        raise RuntimeError(f"refusing to launch because ports are already open: {occupied}")

    workers: list[dict[str, Any]] = []
    worker_dir = run_dir / "ue_workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    for i, project_dir in enumerate(project_dirs):
        worker = smoke.launch_worker(
            worker_idx=i,
            batch_size=args.workers,
            gpu=args.gpu,
            mcp_port=args.base_mcp_port + i,
            ucv_port=args.base_ucv_port + i,
            project_dir=project_dir,
            ue_editor=pathlib.Path(args.ue_editor),
            batch_dir=worker_dir,
            map_path=args.map,
            backend="ue58-bridge",
            ddc_mode=args.ddc_mode,
            ddc_root=pathlib.Path(args.ddc_root),
            official_mcp_port=args.base_official_mcp_port + i,
        )
        workers.append(worker)

    boot_rows = []
    for worker in workers:
        official_sec = smoke.wait_for_mcp(int(worker["official_mcp_port"]), worker["proc"], args.boot_timeout)
        smoke.launch_bridge(worker, pathlib.Path(args.bridge_script))
        bridge_sec = smoke.wait_for_mcp(int(worker["mcp_port"]), worker["bridge_proc"], 60)
        stage = runner.ue(runner.SETUP_STAGE, int(worker["mcp_port"]), 90)
        if "error" in stage or not any("STAGE_READY" in line for line in runner.logs(stage)):
            raise RuntimeError(f"stage setup failed on worker {worker['worker_idx']}: {stage}")
        boot_rows.append({
            "worker_idx": worker["worker_idx"],
            "pid": worker["pid"],
            "bridge_pid": worker.get("bridge_pid"),
            "official_mcp_port": worker["official_mcp_port"],
            "legacy_tcp_port": worker["mcp_port"],
            "project_dir": worker["project_dir"],
            "boot_sec": round(official_sec + bridge_sec, 3),
            "official_boot_sec": official_sec,
            "bridge_boot_sec": bridge_sec,
        })
    runner.write_json(run_dir / "ue_workers.json", {"workers": boot_rows, "gpu_after_boot": smoke.nvidia_smi(args.gpu)})
    return workers


def render_asset(worker: dict[str, Any], asset: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    mcp_port = int(worker["mcp_port"])
    shotdir = pathlib.Path(worker["project_dir"]) / "Saved" / "Screenshots" / "LinuxEditor"
    phase = "spawn_measure"
    facts = runner.spawn_and_measure(asset, mcp_port)
    if not facts:
        raise RuntimeError("spawn/measure failed")
    phase = "geometry_validate"
    if not runner.dimensions_are_positive(facts.get("dimensions_m")):
        raise RuntimeError("nonpositive dimensions")
    phase = "render_views"
    views = runner.render_views(
        asset_id=asset["asset_id"],
        facts=facts,
        asset_db_dir=args.asset_db_dir,
        ue_shotdir=shotdir,
        mcp_port=mcp_port,
        n_views=args.n_views,
        res=args.res,
    )
    valid_views = [path for path in views if runner.image_file_ready(path)]
    if len(valid_views) < args.min_views:
        raise RuntimeError(f"only {len(valid_views)} valid views rendered")
    return {
        "phase": phase,
        "worker_idx": worker["worker_idx"],
        "asset": asset,
        "facts": facts,
        "views": valid_views,
        "render_duration_sec": round(time.time() - started, 3),
    }


def call_vlm(args: argparse.Namespace, schema: pathlib.Path, run_dir: pathlib.Path, asset: dict[str, Any], facts: dict[str, Any], views: list[pathlib.Path]):
    if args.caption_provider == "qwen":
        caption_model = f"{args.qwen_model}:thinking_{'on' if args.qwen_enable_thinking else 'off'}"
        vlm, trace = runner.run_qwen_vlm(
            asset=asset,
            facts=facts,
            view_paths=views,
            model=args.qwen_model,
            base_url=args.qwen_base_url,
            enable_thinking=args.qwen_enable_thinking,
            run_dir=run_dir,
            timeout=args.qwen_timeout,
            max_tokens=args.qwen_max_tokens,
            temperature=args.qwen_temperature,
        )
        return caption_model, vlm, trace
    caption_model = args.model
    vlm, trace = runner.run_vlm(
        asset=asset,
        facts=facts,
        view_paths=views,
        schema=schema,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        codex_bin=args.codex_bin,
        run_dir=run_dir,
        timeout=args.codex_timeout,
    )
    return caption_model, vlm, trace


def write_success(args: argparse.Namespace, run_dir: pathlib.Path, state: dict[str, Any], pending_ids: set[str], existing: dict[str, pathlib.Path], idx: int, total: int, asset: dict[str, Any], result: dict[str, Any], caption_model: str, vlm: dict[str, Any], trace: dict[str, Any], started: float) -> None:
    rec = runner.assemble_record(
        asset,
        result["facts"],
        vlm,
        result["views"],
        args.asset_db_dir,
        caption_model,
        run_dir.name,
        run_dir,
    )
    errors, warnings = runner.validate_record(rec, args.min_views)
    if errors:
        raise RuntimeError("; ".join(errors))
    old_path = existing.get(asset["asset_id"])
    out_path = runner.write_catalog_record(asset_db_dir=args.asset_db_dir, rec=rec, old_path=old_path, force=args.force)
    existing[asset["asset_id"]] = out_path
    pending_ids.add(asset["asset_id"])
    runner.save_pending_ids(run_dir, pending_ids)
    state["indexed"] = int(state.get("indexed") or 0) + 1
    state["pending_db_sync"] = len(pending_ids)
    duration_sec = round(time.time() - started, 3)
    runner.append_metering(
        run_dir,
        asset=asset,
        status="ok",
        phase="complete",
        duration_sec=duration_sec,
        trace=trace,
        provider=args.caption_provider,
        model=caption_model,
        reasoning_effort=args.reasoning_effort,
        render_duration_sec=result.get("render_duration_sec"),
        vlm_duration_sec=trace.get("duration_sec"),
        worker_idx=result.get("worker_idx"),
        raw_response_path=trace.get("raw_response_path"),
        vlm_output_path=trace.get("vlm_output_path"),
        stdout_path=trace.get("stdout_path"),
        stderr_path=trace.get("stderr_path"),
    )
    if warnings:
        state["quality_warnings"] = int(state.get("quality_warnings") or 0) + 1
        runner.append_jsonl(run_dir / "quality_warnings.ndjson", {
            "ts": runner.utc_now(),
            "asset_id": asset["asset_id"],
            "warnings": warnings,
        })
    runner.append_asset_result(
        run_dir,
        status="ok",
        asset=asset,
        idx=idx,
        total=total,
        category=rec["identity"]["category"],
        setting=rec["semantic"]["setting"],
        output_path=str(out_path),
        views=len(result["views"]),
        duration_sec=duration_sec,
        render_duration_sec=result.get("render_duration_sec"),
        vlm_duration_sec=trace.get("duration_sec"),
        worker_idx=result.get("worker_idx"),
        warnings=warnings,
        usage=trace.get("usage"),
    )
    runner.append_jsonl(run_dir / "events.ndjson", {
        "ts": runner.utc_now(),
        "event": "asset_ok",
        "idx": idx,
        "total": total,
        "asset_id": asset["asset_id"],
        "category": rec["identity"]["category"],
        "setting": rec["semantic"]["setting"],
        "path": str(out_path),
        "views": len(result["views"]),
        "worker_idx": result.get("worker_idx"),
        "warnings": warnings,
    })


def write_failure(args: argparse.Namespace, run_dir: pathlib.Path, state: dict[str, Any], idx: int, total: int, asset: dict[str, Any], phase: str, started: float, error: Exception, trace: dict[str, Any] | None = None, worker_idx: int | None = None) -> None:
    duration_sec = round(time.time() - started, 3)
    failure_trace = error.trace if isinstance(error, runner.VlmCallError) else trace
    runner.asset_failure(run_dir=run_dir, state=state, asset=asset, reason=str(error))
    runner.append_metering(
        run_dir,
        asset=asset,
        status="failed",
        phase=phase,
        duration_sec=duration_sec,
        trace=failure_trace,
        reason=str(error),
        provider=args.caption_provider,
        model=(f"{args.qwen_model}:thinking_{'on' if args.qwen_enable_thinking else 'off'}" if args.caption_provider == "qwen" else args.model),
        reasoning_effort=args.reasoning_effort,
        worker_idx=worker_idx,
        vlm_duration_sec=(failure_trace or {}).get("duration_sec"),
        raw_response_path=(failure_trace or {}).get("raw_response_path"),
        vlm_output_path=(failure_trace or {}).get("vlm_output_path"),
        stdout_path=(failure_trace or {}).get("stdout_path"),
        stderr_path=(failure_trace or {}).get("stderr_path"),
    )
    runner.append_asset_result(
        run_dir,
        status="failed",
        asset=asset,
        idx=idx,
        total=total,
        reason=str(error),
        phase=phase,
        duration_sec=duration_sec,
        worker_idx=worker_idx,
        usage=(failure_trace or {}).get("usage"),
    )
    runner.append_jsonl(run_dir / "events.ndjson", {
        "ts": runner.utc_now(),
        "event": "asset_failed_parallel",
        "idx": idx,
        "total": total,
        "asset_id": asset.get("asset_id"),
        "phase": phase,
        "worker_idx": worker_idx,
        "reason": str(error),
    })


def run_parallel(args: argparse.Namespace) -> int:
    args.asset_db_dir = pathlib.Path(args.asset_db_dir)
    args.manifest = pathlib.Path(args.manifest)
    args.ue_editor = args.ue_editor or str(smoke.DEFAULT_UE58_EDITOR)
    args.map = args.map or "/Game/Maps/empty"
    schema = runner.resolve_schema(args.asset_db_dir, args.schema)
    if not args.run_dir:
        provider_label = "qwen36" if args.caption_provider == "qwen" else "gpt55"
        args.run_dir = pathlib.Path(args.run_root) / f"ue58_parallel_{provider_label}_{runner.stamp()}"
    else:
        args.run_dir = pathlib.Path(args.run_dir)
    run_dir: pathlib.Path = args.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    ensure_asset_db_layout(args.asset_db_dir, args.manifest)

    runner.acquire_lock(run_dir)
    workers: list[dict[str, Any]] = []
    executor: concurrent.futures.ThreadPoolExecutor | None = None
    try:
        pending_ids = runner.load_pending_ids(run_dir)
        skip_asset_ids = set(runner.read_asset_ids(args.skip_asset_ids, args.skip_asset_id_file))
        assets = select_assets_for_parallel(args, run_dir)
        existing = runner.catalog_records(args.asset_db_dir)
        state_path = run_dir / "state.json"
        state = runner.load_json(state_path) if state_path.exists() else {}
        if not args.dry_run:
            state.pop("finished_at", None)
            state.pop("dry_run_complete", None)
            state.pop("aborted_at", None)
            state.pop("abort_reason", None)
            state["resume_count"] = int(state.get("resume_count") or 0) + 1
        state.update({
            "started_or_resumed_at": runner.utc_now(),
            "pid": os.getpid(),
            "asset_db_dir": str(args.asset_db_dir),
            "manifest": str(args.manifest),
            "run_dir": str(run_dir),
            "caption_provider": args.caption_provider,
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "qwen_model": args.qwen_model,
            "qwen_enable_thinking": args.qwen_enable_thinking,
            "total_selected": len(assets),
            "configured_skip_ids": len(skip_asset_ids),
            "configured_skip_file": args.skip_asset_id_file or None,
            "pending_db_sync": len(pending_ids),
            "ue58_parallel_workers": args.workers,
            "gpu": args.gpu,
            "worker_project_root": args.worker_project_root,
        })
        state.setdefault("first_started_at", state["started_or_resumed_at"])
        state.setdefault("indexed", 0)
        state.setdefault("skipped_existing", 0)
        state.setdefault("skipped_configured", 0)
        state.setdefault("asset_failures", 0)
        state.setdefault("quality_warnings", 0)
        state.setdefault("db_sync_failures", 0)
        state.setdefault("initial_sync_done", False)
        state["_last_sync_monotonic"] = time.monotonic()
        state["_last_sync_attempt_monotonic"] = time.monotonic()
        state["_last_sync_attempt_pending_count"] = 0
        runner.write_json(run_dir / "config.json", runner.jsonable(vars(args) | {"schema": str(schema)}))
        runner.write_text_atomic(run_dir / "selected_asset_ids.txt", "".join(f"{asset.get('asset_id')}\n" for asset in assets))
        runner.write_json(run_dir / "pid.json", {"pid": os.getpid(), "started_at": runner.utc_now()})
        runner.write_json(state_path, state)
        runner.append_jsonl(run_dir / "events.ndjson", {
            "ts": runner.utc_now(),
            "event": "parallel_run_start",
            "total_selected": len(assets),
            "workers": args.workers,
            "dry_run": args.dry_run,
        })

        current_watches = read_int_file("/proc/sys/fs/inotify/max_user_watches")
        if current_watches is not None and current_watches < args.min_inotify_watches and not args.allow_low_inotify and not args.dry_run:
            return runner.finish_aborted_run(
                run_dir=run_dir,
                state=state,
                args=args,
                pending_ids=pending_ids,
                reason=f"inotify max_user_watches {current_watches} below required {args.min_inotify_watches}",
                code=7,
            )

        run_parallel_preflight(args, schema)
        if args.dry_run:
            for idx, asset in enumerate(assets, 1):
                aid = asset["asset_id"]
                status = "skip_configured" if aid in skip_asset_ids else "skip_existing" if aid in existing and not args.force else "would_index"
                runner.append_asset_result(run_dir, status=status, asset=asset, idx=idx, total=len(assets), dry_run=True)
            state["dry_run_complete"] = True
            state["finished_at"] = runner.utc_now()
            runner.write_json(state_path, state)
            runner.write_quality_snapshot(run_dir, state, args.asset_db_dir, pending_ids)
            runner.write_run_summary(run_dir=run_dir, state=state, asset_db_dir=args.asset_db_dir, pending_ids=pending_ids)
            print(f"Dry run complete. Run dir: {run_dir}")
            return 0

        if not args.no_initial_sync and not state.get("initial_sync_done"):
            ok = runner.sync_database(args=args, run_dir=run_dir, state=state, pending_ids=pending_ids, initial=True)
            if not ok and not args.allow_db_offline:
                return runner.finish_aborted_run(
                    run_dir=run_dir,
                    state=state,
                    args=args,
                    pending_ids=pending_ids,
                    reason="initial DB sync failed before generation",
                    code=2,
                )

        status_rank = {
            "skip_existing": 1,
            "failed": 2,
            "skip_configured": 3,
            "ok": 4,
        }
        result_status_by_asset: dict[str, str] = {}
        for row in runner.read_jsonl(run_dir / "asset_results.ndjson"):
            aid = row.get("asset_id")
            status = str(row.get("status") or "")
            if not aid or status not in status_rank:
                continue
            old_status = result_status_by_asset.get(str(aid))
            if old_status is None or status_rank[status] >= status_rank[old_status]:
                result_status_by_asset[str(aid)] = status

        work_items: list[tuple[int, dict[str, Any]]] = []
        skipped_existing_fast_forward = 0
        skipped_configured_fast_forward = 0
        skip_configured_records_added = 0
        for idx, asset in enumerate(assets, 1):
            aid = asset["asset_id"]
            if aid in skip_asset_ids:
                skipped_configured_fast_forward += 1
                if result_status_by_asset.get(aid) != "skip_configured":
                    runner.append_asset_result(
                        run_dir,
                        status="skip_configured",
                        asset=asset,
                        idx=idx,
                        total=len(assets),
                        reason="configured helper/non-spawnable skip",
                    )
                    result_status_by_asset[aid] = "skip_configured"
                    skip_configured_records_added += 1
                continue
            if existing.get(aid) and not args.force:
                skipped_existing_fast_forward += 1
                continue
            work_items.append((idx, asset))

        state["resume_fast_forward_existing"] = skipped_existing_fast_forward
        state["resume_fast_forward_configured"] = skipped_configured_fast_forward
        state["remaining_to_index"] = len(work_items)
        state["skipped_configured"] = max(
            int(state.get("skipped_configured") or 0),
            skipped_configured_fast_forward,
        )
        if work_items:
            state["next_asset_index"] = work_items[0][0]
            state["next_asset_id"] = work_items[0][1]["asset_id"]
        else:
            state.pop("next_asset_index", None)
            state.pop("next_asset_id", None)
        state["pending_db_sync"] = len(pending_ids)
        runner.write_json(state_path, state)
        runner.append_jsonl(run_dir / "events.ndjson", {
            "ts": runner.utc_now(),
            "event": "resume_fast_forward",
            "existing_catalog_assets": skipped_existing_fast_forward,
            "configured_skip_assets": skipped_configured_fast_forward,
            "skip_configured_records_added": skip_configured_records_added,
            "work_remaining": len(work_items),
            "first_work_index": work_items[0][0] if work_items else None,
            "first_work_asset_id": work_items[0][1]["asset_id"] if work_items else None,
        })

        if not work_items:
            final_sync_ok = runner.maybe_sync(args=args, run_dir=run_dir, state=state, pending_ids=pending_ids, force=True, final=True)
            state["finished_at"] = runner.utc_now()
            state["pending_db_sync"] = len(pending_ids)
            runner.write_json(state_path, state)
            runner.write_quality_snapshot(run_dir, state, args.asset_db_dir, pending_ids)
            runner.write_run_summary(run_dir=run_dir, state=state, asset_db_dir=args.asset_db_dir, pending_ids=pending_ids)
            runner.append_jsonl(run_dir / "events.ndjson", {
                "ts": runner.utc_now(),
                "event": "parallel_run_finished",
                "indexed": state.get("indexed", 0),
                "skipped_existing": state.get("skipped_existing", 0),
                "asset_failures": state.get("asset_failures", 0),
                "pending_db_sync": len(pending_ids),
                "final_sync_ok": final_sync_ok,
                "work_remaining": 0,
            })
            print(f"Parallel run complete. Run dir: {run_dir}")
            return 0

        workers = launch_workers(args, run_dir)
        worker_by_idx = {int(worker["worker_idx"]): worker for worker in workers}
        work_queue = collections.deque(work_items)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
        futures: dict[concurrent.futures.Future[dict[str, Any]], tuple[int, dict[str, Any], int, float]] = {}
        consecutive_failures = 0
        phase_failures: dict[str, int] = {}

        def submit_next(worker: dict[str, Any]) -> None:
            while work_queue:
                idx, asset = work_queue.popleft()
                aid = asset["asset_id"]
                state["last_asset_id"] = aid
                state["last_asset_index"] = idx
                state["remaining_to_index"] = len(work_queue) + len(futures) + 1
                runner.write_json(state_path, state)
                runner.append_jsonl(run_dir / "events.ndjson", {
                    "ts": runner.utc_now(),
                    "event": "asset_render_start",
                    "idx": idx,
                    "total": len(assets),
                    "asset_id": aid,
                    "worker_idx": worker["worker_idx"],
                })
                futures[executor.submit(render_asset, worker, asset, args)] = (idx, asset, int(worker["worker_idx"]), time.time())
                return

        for worker in workers:
            submit_next(worker)

        while futures:
            done, _ = concurrent.futures.wait(futures.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                idx, asset, worker_idx, started = futures.pop(future)
                worker = worker_by_idx[worker_idx]
                phase = "render_views"
                trace: dict[str, Any] | None = None
                try:
                    result = future.result()
                    phase = f"{args.caption_provider}_vlm"
                    caption_model, vlm, trace = call_vlm(args, schema, run_dir, asset, result["facts"], result["views"])
                    phase = "validate_write"
                    write_success(args, run_dir, state, pending_ids, existing, idx, len(assets), asset, result, caption_model, vlm, trace, started)
                    consecutive_failures = 0
                    phase_failures.clear()
                    runner.maybe_sync(args=args, run_dir=run_dir, state=state, pending_ids=pending_ids)
                    runner.write_json(state_path, state)
                    print(f"[{idx}/{len(assets)}] OK {asset['asset_id']} worker={worker_idx}", flush=True)
                except Exception as e:
                    consecutive_failures += 1
                    phase_failures[phase] = int(phase_failures.get(phase) or 0) + 1
                    write_failure(args, run_dir, state, idx, len(assets), asset, phase, started, e, trace, worker_idx)
                    runner.write_json(state_path, state)
                    print(f"[{idx}/{len(assets)}] FAILED {asset['asset_id']} worker={worker_idx}: {e}", flush=True)
                    if phase.endswith("_vlm") and phase_failures[phase] >= args.max_consecutive_vlm_failures:
                        return runner.finish_aborted_run(run_dir=run_dir, state=state, args=args, pending_ids=pending_ids, reason=f"repeated VLM failures: {e}", code=5)
                    if consecutive_failures >= args.max_consecutive_failures:
                        try:
                            runner.check_ue(int(worker["mcp_port"]))
                        except Exception as ue_err:
                            return runner.finish_aborted_run(run_dir=run_dir, state=state, args=args, pending_ids=pending_ids, reason=f"worker {worker_idx} UE unresponsive: {ue_err}", code=3)
                        if phase_failures.get(phase, 0) >= args.max_consecutive_phase_failures:
                            return runner.finish_aborted_run(run_dir=run_dir, state=state, args=args, pending_ids=pending_ids, reason=f"repeated {phase} failures: {e}", code=6)
                        consecutive_failures = 0
                submit_next(worker)

        final_sync_ok = runner.maybe_sync(args=args, run_dir=run_dir, state=state, pending_ids=pending_ids, force=True, final=True)
        state["finished_at"] = runner.utc_now()
        state["pending_db_sync"] = len(pending_ids)
        runner.write_json(state_path, state)
        runner.write_quality_snapshot(run_dir, state, args.asset_db_dir, pending_ids)
        runner.write_run_summary(run_dir=run_dir, state=state, asset_db_dir=args.asset_db_dir, pending_ids=pending_ids)
        runner.append_jsonl(run_dir / "events.ndjson", {
            "ts": runner.utc_now(),
            "event": "parallel_run_finished",
            "indexed": state.get("indexed", 0),
            "skipped_existing": state.get("skipped_existing", 0),
            "asset_failures": state.get("asset_failures", 0),
            "pending_db_sync": len(pending_ids),
            "final_sync_ok": final_sync_ok,
            "gpu_after_stop": smoke.nvidia_smi(args.gpu),
        })
        if pending_ids and not final_sync_ok:
            return 4
        print(f"Parallel run complete. Run dir: {run_dir}")
        return 0
    finally:
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
        if workers:
            smoke.stop_workers(workers)
        runner.release_lock(run_dir)


def add_args(parser: argparse.ArgumentParser) -> None:
    runner.add_common_args(parser)
    parser.set_defaults(
        asset_db_dir=str(DEFAULT_ASSET_DB_DIR),
        manifest=str(DEFAULT_MANIFEST),
        postgres_url=DEFAULT_POSTGRES_URL,
        qdrant_collection=DEFAULT_QDRANT_COLLECTION,
        caption_provider="qwen",
    )
    parser.add_argument("--run-root", default=os.environ.get("RUN_ROOT", str(DEFAULT_RUN_ROOT)))
    parser.add_argument("--run-dir", default=os.environ.get("RUN_DIR", ""))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("UE58_WORKERS", "6")))
    parser.add_argument("--gpu", type=int, default=int(os.environ.get("SIMWORLD_GPU", "3")))
    parser.add_argument("--ue-editor", default=os.environ.get("UE58_EDITOR", str(smoke.DEFAULT_UE58_EDITOR)))
    parser.add_argument("--source-project", default=os.environ.get("UE58_SOURCE_PROJECT", str(smoke.DEFAULT_UE58_SOURCE_PROJECT)))
    parser.add_argument("--content-root", default=os.environ.get("UE58_CONTENT_ROOT", str(smoke.DEFAULT_UE58_CONTENT_ROOT)))
    parser.add_argument("--worker-project-root", default=os.environ.get("UE58_WORKER_PROJECT_ROOT", str(smoke.DEFAULT_UE58_PROJECT_ROOT)))
    parser.add_argument("--bridge-script", default=os.environ.get("UE58_BRIDGE_SCRIPT", str(smoke.DEFAULT_UE58_BRIDGE_SCRIPT)))
    parser.add_argument("--ddc-mode", choices=["default", "local"], default=os.environ.get("UE58_DDC_MODE", "default"))
    parser.add_argument("--ddc-root", default=os.environ.get("UE58_DDC_ROOT", str(smoke.DEFAULT_UE58_DDC_ROOT)))
    parser.add_argument("--base-mcp-port", type=int, default=int(os.environ.get("UE58_BASE_MCP_PORT", "55680")))
    parser.add_argument("--base-official-mcp-port", type=int, default=int(os.environ.get("UE58_BASE_OFFICIAL_MCP_PORT", "8080")))
    parser.add_argument("--base-ucv-port", type=int, default=int(os.environ.get("UE58_BASE_UCV_PORT", "10080")))
    parser.add_argument("--map", default=os.environ.get("UE58_MAP", "/Game/Maps/empty"))
    parser.add_argument("--boot-timeout", type=int, default=int(os.environ.get("UE58_BOOT_TIMEOUT", "900")))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-reselect", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-initial-sync", action="store_true")
    parser.add_argument("--allow-db-offline", action="store_true")
    parser.add_argument("--skip-provider-check", action="store_true")
    parser.add_argument("--n-views", type=int, default=int(os.environ.get("N_VIEWS", "8")))
    parser.add_argument("--min-views", type=int, default=int(os.environ.get("MIN_VIEWS", "4")))
    parser.add_argument("--res", type=int, default=int(os.environ.get("RES", "1024")))
    parser.add_argument("--sample-size", type=int, default=int(os.environ.get("SAMPLE_SIZE", "0")))
    parser.add_argument("--sample-seed", type=int, default=int(os.environ.get("SAMPLE_SEED", "58")))
    parser.add_argument("--codex-timeout", type=int, default=int(os.environ.get("CODEX_TIMEOUT", "900")))
    parser.add_argument("--qwen-timeout", type=int, default=int(os.environ.get("QWEN_TIMEOUT", "240")))
    parser.add_argument("--db-sync-timeout", type=int, default=int(os.environ.get("DB_SYNC_TIMEOUT", "7200")))
    parser.add_argument("--category-index-timeout", type=int, default=int(os.environ.get("CATEGORY_INDEX_TIMEOUT", "1200")))
    parser.add_argument("--sync-every-assets", type=int, default=int(os.environ.get("SYNC_EVERY_ASSETS", "50")))
    parser.add_argument("--sync-min-seconds", type=int, default=int(os.environ.get("SYNC_MIN_SECONDS", "1800")))
    parser.add_argument("--db-commit-every", type=int, default=int(os.environ.get("DB_COMMIT_EVERY", "50")))
    parser.add_argument("--embed-batch-size", type=int, default=int(os.environ.get("EMBED_BATCH_SIZE", "32")))
    parser.add_argument("--max-consecutive-failures", type=int, default=int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "5")))
    parser.add_argument("--max-consecutive-vlm-failures", type=int, default=int(os.environ.get("MAX_CONSECUTIVE_VLM_FAILURES", "3")))
    parser.add_argument("--max-consecutive-phase-failures", type=int, default=int(os.environ.get("MAX_CONSECUTIVE_PHASE_FAILURES", "5")))
    parser.add_argument("--min-inotify-watches", type=int, default=int(os.environ.get("MIN_INOTIFY_WATCHES", str(DEFAULT_MIN_INOTIFY_WATCHES))))
    parser.add_argument("--allow-low-inotify", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="Run the UE 5.8 parallel indexing workflow")
    add_args(run_p)
    check_p = sub.add_parser("check", help="Check UE 5.8 parallel indexing config")
    add_args(check_p)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.cmd == "check":
        return check_parallel_config(args)
    if args.cmd == "run":
        return run_parallel(args)
    raise RuntimeError(f"unknown command {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
