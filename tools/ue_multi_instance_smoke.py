#!/usr/bin/env python3
"""Short UE multi-instance render smoke test.

This intentionally does not touch the asset catalog, Postgres, or Qdrant. It launches
isolated UE project copies on one GPU, renders a small asset on each instance, verifies
the screenshots, records timings, and shuts everything down.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import pathlib
import signal
import shutil
import socket
import subprocess
import sys
import time
from typing import Any

import full_asset_index_runner as runner


DEFAULT_UE_EDITOR = pathlib.Path("/data/siddhant/ue/UE_5.3.2/Engine/Binaries/Linux/UnrealEditor")
DEFAULT_PROJECT_ROOT = pathlib.Path("/data/siddhant")
DEFAULT_OUT_ROOT = pathlib.Path("/data/siddhant/ue_parallel_smoke")
DEFAULT_MANIFEST = pathlib.Path("/data/siddhant/asset_db/manifest_full.json")
DEFAULT_UE58_SOURCE_PROJECT = pathlib.Path("/data/koe/SimWorld_SPEAR")
DEFAULT_UE58_CONTENT_ROOT = pathlib.Path("/data/koe/simworld-content-store/current/Content")
DEFAULT_UE58_PROJECT_ROOT = pathlib.Path("/data/siddhant/ue58_smoke_instances")
DEFAULT_UE58_DDC_ROOT = pathlib.Path("/data/siddhant/ue58_ddc")
DEFAULT_UE58_EDITOR = DEFAULT_UE58_SOURCE_PROJECT / "Binaries/Linux/SimWorldEditor"
DEFAULT_UE58_BRIDGE_SCRIPT = DEFAULT_UE58_SOURCE_PROJECT / "tools/studio_migration/official_mcp_tcp_bridge.py"
DEFAULT_ASSETS = [
    "anchientruins_sm_tiles_08",
    "asiantemple_sm_rock_02a",
    "asiantemple_sm_temple_pillar_01a",
    "bazaar_meshingun_sm_claypot_01a_1",
]
DEFAULT_UE58_ASSETS = [
    "citydatabase_sm_chair_b",
    "citydatabase_sm_road_cone",
    "building_sm_bldg_chb_l10_a_wall_01_n1",
    "bazaar_meshingun_sm_claypot_01a_1",
]


def utc_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def wait_for_mcp(port: int, proc: subprocess.Popen[str], timeout: int) -> float:
    started = time.time()
    while time.time() - started < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"UE process exited early with code {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return round(time.time() - started, 3)
        except OSError:
            time.sleep(2)
    raise TimeoutError(f"MCP port {port} did not open within {timeout}s")


def nvidia_smi(gpu: int) -> dict[str, Any]:
    cmd = [
        "nvidia-smi",
        "--query-gpu=memory.used,utilization.gpu",
        "--format=csv,noheader,nounits",
        "-i",
        str(gpu),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
    gpu_stats: dict[str, Any] = {"raw": out.stdout.strip(), "returncode": out.returncode}
    if out.returncode == 0 and out.stdout.strip():
        parts = [x.strip() for x in out.stdout.strip().split(",")]
        if len(parts) >= 2:
            gpu_stats["memory_used_mb"] = int(parts[0])
            gpu_stats["utilization_gpu_pct"] = int(parts[1])
    proc_cmd = [
        "nvidia-smi",
        "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
        "-i",
        str(gpu),
    ]
    proc_out = subprocess.run(proc_cmd, capture_output=True, text=True, check=False, timeout=10)
    gpu_stats["processes_raw"] = proc_out.stdout.strip()
    return gpu_stats


def ensure_symlink(path: pathlib.Path, target: pathlib.Path) -> None:
    target = target.resolve()
    if path.is_symlink():
        if path.resolve() == target:
            return
        path.unlink()
    elif path.exists():
        raise RuntimeError(f"refusing to replace non-symlink path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target, target_is_directory=target.is_dir())


def ensure_ini_setting(path: pathlib.Path, section: str, key: str, value: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    out: list[str] = []
    section_header = f"[{section}]"
    in_section = False
    saw_section = False
    wrote_key = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not wrote_key:
                out.append(f"{key}={value}")
                wrote_key = True
            in_section = stripped == section_header
            saw_section = saw_section or in_section
            out.append(line)
            continue
        if in_section and not stripped.startswith((";", "#")) and stripped.split("=", 1)[0].strip() == key:
            if not wrote_key:
                out.append(f"{key}={value}")
                wrote_key = True
            continue
        out.append(line)

    if in_section and not wrote_key:
        out.append(f"{key}={value}")
    elif not saw_section:
        if out and out[-1] != "":
            out.append("")
        out.extend([section_header, f"{key}={value}"])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def prepare_ue58_config(project_dir: pathlib.Path, source_project: pathlib.Path) -> None:
    src = source_project / "Config"
    dst = project_dir / "Config"
    if not src.exists():
        return
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists() and not dst.is_dir():
        raise RuntimeError(f"refusing to replace non-directory config path: {dst}")
    shutil.copytree(src, dst, dirs_exist_ok=True)
    ensure_ini_setting(dst / "DefaultEngine.ini", "ConsoleVariables", "AssetRegistry.DisableDirectoryWatcher", "1")
    # Defer the DDC cleanup pass far past any run: on a large seeded local DDC (~75GB) the cleanup
    # thread scans the whole cache (~7min, deletes 0) and its I/O makes the editor sluggish/unresponsive
    # to MCP calls right when the run loop starts. Push it out so it never runs during indexing.
    ensure_ini_setting(dst / "DefaultEngine.ini", "DDCCleanup", "TimeToWaitAfterInit", "100000000")
    ensure_ini_setting(dst / "DefaultEngine.ini", "DDCCleanup", "MaxFileChecksPerSec", "1")


def prepare_ue58_project(project_dir: pathlib.Path, source_project: pathlib.Path, content_root: pathlib.Path) -> None:
    source_project = source_project.resolve()
    content_root = content_root.resolve()
    project_dir.mkdir(parents=True, exist_ok=True)

    uproject_src = source_project / "SimWorld.uproject"
    uproject_dst = project_dir / "SimWorld.uproject"
    if not uproject_src.exists():
        raise FileNotFoundError(uproject_src)
    if not uproject_dst.exists() or uproject_dst.read_text(encoding="utf-8") != uproject_src.read_text(encoding="utf-8"):
        uproject_dst.write_text(uproject_src.read_text(encoding="utf-8"), encoding="utf-8")

    for name in ("Binaries", "Plugins", "Source", "tools"):
        src = source_project / name
        if src.exists():
            ensure_symlink(project_dir / name, src)
    prepare_ue58_config(project_dir, source_project)
    ensure_symlink(project_dir / "Content", content_root)
    (project_dir / "Saved" / "Screenshots" / "LinuxEditor").mkdir(parents=True, exist_ok=True)
    (project_dir / "Intermediate").mkdir(parents=True, exist_ok=True)
    (project_dir / "DerivedDataCache").mkdir(parents=True, exist_ok=True)

    for src in (source_project / "Intermediate").glob("CachedAssetRegistry_*.bin"):
        dst = project_dir / "Intermediate" / src.name
        if dst.exists() and dst.stat().st_size == src.stat().st_size and dst.stat().st_mtime >= src.stat().st_mtime:
            continue
        tmp = dst.with_suffix(dst.suffix + f".{os.getpid()}.tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)


def seed_ue58_ddc(source_project: pathlib.Path, ddc_root: pathlib.Path) -> None:
    src_root = source_project / "DerivedDataCache"
    if not src_root.exists():
        return
    marker = ddc_root / ".seeded_from_simworld_spear"
    if marker.exists():
        return
    ddc_root.mkdir(parents=True, exist_ok=True)
    for src in src_root.iterdir():
        dst = ddc_root / src.name
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    marker.write_text(f"seeded_at={runner.utc_now()}\nsource={src_root}\n", encoding="utf-8")


def image_stats(paths: list[pathlib.Path]) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        row = {"path": str(path), "exists": path.exists()}
        if path.exists():
            row["size"] = path.stat().st_size
            row["ready"] = runner.image_file_ready(path)
        rows.append(row)
    return rows


def launch_worker(
    *,
    worker_idx: int,
    batch_size: int,
    gpu: int,
    mcp_port: int,
    ucv_port: int,
    project_dir: pathlib.Path,
    ue_editor: pathlib.Path,
    batch_dir: pathlib.Path,
    map_path: str,
    backend: str,
    ddc_mode: str,
    ddc_root: pathlib.Path | None = None,
    official_mcp_port: int | None = None,
) -> dict[str, Any]:
    project_file = project_dir / "SimWorld.uproject"
    if not project_file.exists():
        raise FileNotFoundError(project_file)
    log_path = batch_dir / f"worker_{worker_idx}_ue.log"
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = str(gpu)
    env["SDL_VIDEODRIVER"] = env.get("SDL_VIDEODRIVER", "offscreen")
    local_ddc = ddc_root or (project_dir / "DerivedDataCache")
    if ddc_mode == "local":
        env["UE-LocalDataCachePath"] = str(local_ddc)
    nvidia_icd = "/usr/share/vulkan/icd.d/nvidia_icd.json"
    if pathlib.Path(nvidia_icd).exists():
        env["VK_ICD_FILENAMES"] = nvidia_icd
    if backend == "ue58-bridge":
        if official_mcp_port is None:
            raise ValueError("official_mcp_port is required for ue58-bridge")
        args = [
            str(ue_editor),
            str(project_file),
            map_path,
            "-StartModelContextProtocolServer",
            f"-ModelContextProtocolPort={official_mcp_port}",
            "-AssetRegistry.DisableDirectoryWatcher=1",
            "-SpServicesRole=None",
            "-Unattended",
            "-NoUba",
            "-NOSPLASH",
            "-NOSOUND",
            "-Messaging",
            "-ResX=1280",
            "-ResY=720",
            "-FPSMAX=15",
            "-graphicsadapter=" + str(gpu),
            "-RenderOffScreen",
            "-log",
        ]
        if ddc_mode == "local":
            args.extend(["-DDC=NoZenLocalFallback", f"-LocalDataCachePath={local_ddc}"])
    else:
        args = [
            str(ue_editor),
            str(project_file),
            map_path,
            f"-MCPPort={mcp_port}",
            f"-UnrealCVPort={ucv_port}",
            "-graphicsadapter=" + str(gpu),
            "-RenderOffScreen",
            "-Unattended",
            "-NOSPLASH",
            "-NOSOUND",
            "-Messaging",
            "-ResX=1280",
            "-ResY=720",
            "-FPSMAX=15",
            "-log",
        ]
    log_handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(args, stdout=log_handle, stderr=subprocess.STDOUT, text=True, env=env)
    return {
        "backend": backend,
        "worker_idx": worker_idx,
        "batch_size": batch_size,
        "project_dir": str(project_dir),
        "mcp_port": mcp_port,
        "official_mcp_port": official_mcp_port,
        "ucv_port": ucv_port,
        "pid": proc.pid,
        "proc": proc,
        "log_handle": log_handle,
        "log_path": str(log_path),
        "bridge_proc": None,
        "bridge_log_handle": None,
        "bridge_log_path": str(batch_dir / f"worker_{worker_idx}_bridge.log"),
        "cmd": args,
    }


def launch_bridge(worker: dict[str, Any], bridge_script: pathlib.Path) -> None:
    log_path = pathlib.Path(worker["bridge_log_path"])
    env = dict(os.environ)
    env["SIMWORLD_OFFICIAL_MCP_URL"] = f"http://127.0.0.1:{worker['official_mcp_port']}/mcp"
    env["SIMWORLD_SCREENSHOT_DIR"] = str(pathlib.Path(worker["project_dir"]) / "Saved" / "Screenshots" / "LinuxEditor")
    log_handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        ["python3", str(bridge_script), "--host", "127.0.0.1", "--port", str(worker["mcp_port"])],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    worker["bridge_pid"] = proc.pid
    worker["bridge_proc"] = proc
    worker["bridge_log_handle"] = log_handle


def stop_workers(workers: list[dict[str, Any]]) -> None:
    for worker in workers:
        for key in ("bridge_proc", "proc"):
            proc = worker.get(key)
            if proc and proc.poll() is None:
                proc.terminate()
    deadline = time.time() + 30
    for worker in workers:
        for key in ("bridge_proc", "proc"):
            proc = worker.get(key)
            if not proc:
                continue
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.5)
    for worker in workers:
        for key in ("bridge_proc", "proc"):
            proc = worker.get(key)
            if proc and proc.poll() is None:
                proc.kill()
    for worker in workers:
        for key in ("bridge_log_handle", "log_handle"):
            handle = worker.get(key)
            if handle:
                handle.close()


def render_on_worker(
    *,
    worker: dict[str, Any],
    asset: dict[str, Any],
    asset_db_dir: pathlib.Path,
    n_views: int,
    res: int,
) -> dict[str, Any]:
    started = time.time()
    mcp_port = int(worker["mcp_port"])
    shotdir = pathlib.Path(worker["project_dir"]) / "Saved" / "Screenshots" / "LinuxEditor"
    shotdir.mkdir(parents=True, exist_ok=True)
    runner.ue(runner.SETUP_STAGE, mcp_port, 90)
    facts = runner.spawn_and_measure(asset, mcp_port)
    if not facts:
        raise RuntimeError("spawn/measure failed")
    views = runner.render_views(
        asset_id=asset["asset_id"],
        facts=facts,
        asset_db_dir=asset_db_dir,
        ue_shotdir=shotdir,
        mcp_port=mcp_port,
        n_views=n_views,
        res=res,
    )
    stats = image_stats(views)
    valid = sum(1 for row in stats if row.get("ready"))
    return {
        "worker_idx": worker["worker_idx"],
        "asset_id": asset["asset_id"],
        "duration_sec": round(time.time() - started, 3),
        "view_count": len(views),
        "valid_views": valid,
        "dimensions_m": facts.get("dimensions_m"),
        "image_stats": stats,
        "ok": valid == n_views,
    }


def run_batch(args: argparse.Namespace, batch_size: int, asset_by_id: dict[str, dict[str, Any]], run_dir: pathlib.Path) -> dict[str, Any]:
    batch_dir = run_dir / f"batch_{batch_size}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    workers: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "batch_size": batch_size,
        "started_at": runner.utc_now(),
        "gpu_before": nvidia_smi(args.gpu),
        "workers": [],
        "renders": [],
        "ok": False,
    }
    try:
        for i in range(batch_size):
            project_dir = pathlib.Path(args.project_dirs[i])
            worker = launch_worker(
                worker_idx=i,
                batch_size=batch_size,
                gpu=args.gpu,
                mcp_port=args.base_mcp_port + batch_size * 10 + i,
                ucv_port=args.base_ucv_port + batch_size * 10 + i,
                project_dir=project_dir,
                ue_editor=pathlib.Path(args.ue_editor),
                batch_dir=batch_dir,
                map_path=args.map,
                backend=args.backend,
                ddc_mode=args.ddc_mode,
                ddc_root=pathlib.Path(args.ddc_root) if args.backend == "ue58-bridge" else None,
                official_mcp_port=args.base_official_mcp_port + batch_size * 10 + i if args.backend == "ue58-bridge" else None,
            )
            workers.append(worker)
            result["workers"].append({k: v for k, v in worker.items() if k not in {"proc", "log_handle"}})

        boot_rows = []
        for worker in workers:
            if args.backend == "ue58-bridge":
                official_sec = wait_for_mcp(int(worker["official_mcp_port"]), worker["proc"], args.boot_timeout)
                launch_bridge(worker, pathlib.Path(args.bridge_script))
                bridge_sec = wait_for_mcp(int(worker["mcp_port"]), worker["bridge_proc"], 60)
                boot_rows.append(
                    {
                        "worker_idx": worker["worker_idx"],
                        "pid": worker["pid"],
                        "bridge_pid": worker.get("bridge_pid"),
                        "official_mcp_port": worker["official_mcp_port"],
                        "legacy_tcp_port": worker["mcp_port"],
                        "boot_sec": round(official_sec + bridge_sec, 3),
                        "official_boot_sec": official_sec,
                        "bridge_boot_sec": bridge_sec,
                    }
                )
            else:
                boot_sec = wait_for_mcp(int(worker["mcp_port"]), worker["proc"], args.boot_timeout)
                boot_rows.append({"worker_idx": worker["worker_idx"], "pid": worker["pid"], "boot_sec": boot_sec})
        result["boot"] = boot_rows
        result["gpu_after_boot"] = nvidia_smi(args.gpu)

        asset_ids = (args.asset_ids * ((batch_size // len(args.asset_ids)) + 1))[:batch_size]
        futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as pool:
            for worker, asset_id in zip(workers, asset_ids):
                futures.append(
                    pool.submit(
                        render_on_worker,
                        worker=worker,
                        asset=asset_by_id[asset_id],
                        asset_db_dir=batch_dir / f"worker_{worker['worker_idx']}_asset_db",
                        n_views=args.n_views,
                        res=args.res,
                    )
                )
            for future in concurrent.futures.as_completed(futures, timeout=args.render_timeout):
                result["renders"].append(future.result())
        result["gpu_after_render"] = nvidia_smi(args.gpu)
        result["ok"] = len(result["renders"]) == batch_size and all(row.get("ok") for row in result["renders"])
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["gpu_on_error"] = nvidia_smi(args.gpu)
    finally:
        stop_workers(workers)
        time.sleep(5)
        result["gpu_after_stop"] = nvidia_smi(args.gpu)
        result["finished_at"] = runner.utc_now()
        runner.write_json(batch_dir / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["legacy", "ue58-bridge"], default="legacy")
    parser.add_argument("--gpu", type=int, default=3)
    parser.add_argument("--batches", default="2,3,4")
    parser.add_argument("--only-try-4-if-3-ok", action="store_true", default=True)
    parser.add_argument("--ue-editor", default="")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--project-dirs", nargs="+", default=None)
    parser.add_argument("--project-root", default=str(DEFAULT_UE58_PROJECT_ROOT))
    parser.add_argument("--source-project", default=str(DEFAULT_UE58_SOURCE_PROJECT))
    parser.add_argument("--content-root", default=str(DEFAULT_UE58_CONTENT_ROOT))
    parser.add_argument("--ddc-mode", choices=["default", "local"], default="default")
    parser.add_argument("--ddc-root", default=str(DEFAULT_UE58_DDC_ROOT))
    parser.add_argument("--bridge-script", default=str(DEFAULT_UE58_BRIDGE_SCRIPT))
    parser.add_argument("--asset-ids", nargs="+", default=None)
    parser.add_argument("--base-mcp-port", type=int, default=55620)
    parser.add_argument("--base-official-mcp-port", type=int, default=8010)
    parser.add_argument("--base-ucv-port", type=int, default=10020)
    parser.add_argument("--map", default="")
    parser.add_argument("--n-views", type=int, default=8)
    parser.add_argument("--res", type=int, default=1024)
    parser.add_argument("--boot-timeout", type=int, default=0)
    parser.add_argument("--render-timeout", type=int, default=300)
    args = parser.parse_args()

    if not args.ue_editor:
        args.ue_editor = str(DEFAULT_UE58_EDITOR if args.backend == "ue58-bridge" else DEFAULT_UE_EDITOR)
    if not args.map:
        args.map = "/Game/Maps/empty" if args.backend == "ue58-bridge" else "/Game/Maps/EmptyMap"
    if args.asset_ids is None:
        args.asset_ids = DEFAULT_UE58_ASSETS if args.backend == "ue58-bridge" else DEFAULT_ASSETS
    if args.boot_timeout <= 0:
        args.boot_timeout = 900 if args.backend == "ue58-bridge" else 240

    run_dir = pathlib.Path(args.out_dir) if args.out_dir else DEFAULT_OUT_ROOT / f"gpu{args.gpu}_{utc_stamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = runner.load_json(pathlib.Path(args.manifest))
    asset_by_id = {asset["asset_id"]: asset for asset in manifest.get("assets", [])}
    missing = [asset_id for asset_id in args.asset_ids if asset_id not in asset_by_id]
    if missing:
        raise SystemExit(f"missing asset ids in manifest: {missing}")

    batch_sizes = [int(x.strip()) for x in args.batches.split(",") if x.strip()]
    if args.project_dirs is None:
        if args.backend == "ue58-bridge":
            args.project_dirs = [str(pathlib.Path(args.project_root) / f"inst_{i}") for i in range(max(batch_sizes))]
        else:
            args.project_dirs = [
                str(DEFAULT_PROJECT_ROOT / "simworld_studio_inst_0"),
                str(DEFAULT_PROJECT_ROOT / "simworld_studio_inst_1"),
                str(DEFAULT_PROJECT_ROOT / "simworld_studio_inst_2"),
                str(DEFAULT_PROJECT_ROOT / "simworld_studio_inst_3"),
            ]
    if max(batch_sizes) > len(args.project_dirs):
        raise SystemExit(f"need at least {max(batch_sizes)} project dirs")
    if args.backend == "ue58-bridge":
        if args.ddc_mode == "local":
            seed_ue58_ddc(pathlib.Path(args.source_project), pathlib.Path(args.ddc_root))
        for project_dir in args.project_dirs[: max(batch_sizes)]:
            prepare_ue58_project(
                pathlib.Path(project_dir),
                pathlib.Path(args.source_project),
                pathlib.Path(args.content_root),
            )

    results = []
    print(f"run_dir={run_dir}", flush=True)
    for batch_size in batch_sizes:
        if batch_size == 4 and args.only_try_4_if_3_ok:
            prior_3 = next((row for row in results if row["batch_size"] == 3), None)
            if prior_3 and not prior_3.get("ok"):
                print("skip batch_4 because batch_3 was not stable", flush=True)
                break
        print(f"START batch={batch_size}", flush=True)
        result = run_batch(args, batch_size, asset_by_id, run_dir)
        results.append(result)
        boot = [row.get("boot_sec") for row in result.get("boot", [])]
        renders = [row.get("duration_sec") for row in result.get("renders", [])]
        print(
            f"DONE batch={batch_size} ok={result.get('ok')} "
            f"boot={boot} render={renders} error={result.get('error')}",
            flush=True,
        )

    summary = {
        "run_dir": str(run_dir),
        "gpu": args.gpu,
        "results": results,
        "finished_at": runner.utc_now(),
    }
    runner.write_json(run_dir / "summary.json", summary)
    lines = ["# UE Multi-Instance GPU Smoke", ""]
    for result in results:
        boot = [row.get("boot_sec") for row in result.get("boot", [])]
        renders = result.get("renders", [])
        render_secs = [row.get("duration_sec") for row in renders]
        valid = [row.get("valid_views") for row in renders]
        gpu_boot = result.get("gpu_after_boot") or {}
        gpu_render = result.get("gpu_after_render") or {}
        lines.append(
            f"- batch {result['batch_size']}: ok={result.get('ok')}, "
            f"boot_sec={boot}, render_sec={render_secs}, valid_views={valid}, "
            f"gpu_after_boot={gpu_boot.get('memory_used_mb')}MB/{gpu_boot.get('utilization_gpu_pct')}%, "
            f"gpu_after_render={gpu_render.get('memory_used_mb')}MB/{gpu_render.get('utilization_gpu_pct')}%, "
            f"error={result.get('error')}"
        )
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"summary={run_dir / 'summary.md'}", flush=True)
    return 0 if all(row.get("ok") for row in results) else 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    raise SystemExit(main())
