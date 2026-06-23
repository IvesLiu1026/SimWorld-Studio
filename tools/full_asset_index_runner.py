#!/usr/bin/env python3
"""Robust full-catalog asset index runner.

This is the production wrapper for the long one-time indexing run:
- renders each missing manifest asset through the UE MCP socket
- calls Codex/GPT VLM with schema-constrained JSON output
- atomically writes asset_db/catalog/<category>/<asset_id>.json
- tracks per-asset status, failures, quality warnings, and token usage
- periodically imports completed JSONs into Postgres and upserts Qdrant vectors

The script is resumable. It skips catalog JSONs that already exist unless
--force is passed, and pending DB sync ids are retained in the run directory.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import glob
import json
import math
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_ASSET_DB_DIR = pathlib.Path("/data/siddhant/asset_db")
DEFAULT_POSTGRES_URL = "postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
DEFAULT_COLLECTION = "assets"
DEFAULT_QWEN_BASE_URL = "http://137.110.161.132:8005/v1"
DEFAULT_QWEN_MODEL = "Qwen3.6-35B-A3B"
_LOCK_HANDLE = None


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


CATEGORY_ENUM = {
    "buildings",
    "building_pieces",
    "vegetation",
    "nature_terrain",
    "seating",
    "furniture_indoor",
    "lighting",
    "signage",
    "vehicles",
    "carts_and_vendors",
    "market_goods",
    "industrial_goods",
    "pipes_tanks_infra",
    "tools_equipment",
    "barriers_and_fencing",
    "waste_and_bins",
    "litter_and_debris",
    "ground_and_road",
    "decor_and_landmarks",
    "religious_ritual",
    "medieval_fantasy_props",
    "sci_fi_props",
    "winter_snow_props",
    "agricultural_props",
    "camping_outdoor",
    "indoor_clutter",
}

SETTING_ENUM = {
    "modern_urban",
    "industrial",
    "suburban_residential",
    "commercial_retail",
    "nature_rural",
    "coastal_harbor",
    "medieval",
    "fantasy_gothic",
    "ancient_temple",
    "middle_eastern",
    "east_asian",
    "winter",
    "sci_fi",
    "indoor",
    "generic",
}

SEMANTIC_LIST_FIELDS = {
    "tags",
    "materials",
    "color_palette",
    "mood",
    "typical_placement",
    "affordances",
    "scene_types",
}


def clean_list_text(value: Any) -> str:
    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n\"'")


def normalize_semantic_list(value: Any, *, lowercase: bool = False) -> list[str]:
    """Coerce model list fields into arrays without dropping useful scalar text."""
    if value is None:
        raw_items: list[Any] = []
    elif isinstance(value, list):
        raw_items = value
    elif isinstance(value, tuple):
        raw_items = list(value)
    else:
        raw_items = [value]

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if item is None:
            continue
        if isinstance(item, (list, tuple)):
            parts = item
        elif isinstance(item, str):
            parts = re.split(r"[,;]", item)
        else:
            parts = [item]
        for part in parts:
            text = clean_list_text(part)
            if not text:
                continue
            if lowercase:
                text = text.lower()
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
    return out


def normalize_vlm_metadata(vlm: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(vlm)
    for field in SEMANTIC_LIST_FIELDS:
        normalized[field] = normalize_semantic_list(
            normalized.get(field),
            lowercase=field == "tags",
        )
    return normalized

INFRA = ("DB_Sun", "DB_Sky", "DB_Atmo", "DB_Ground", "DB_PPV")


class VlmCallError(RuntimeError):
    def __init__(self, message: str, trace: dict[str, Any] | None = None):
        super().__init__(message)
        self.trace = trace or {}

SETUP_STAGE = r'''
import unreal
eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
def has(lbl):
    return any(a.get_actor_label()==lbl for a in eas.get_all_level_actors())
if not has("DB_Sun"):
    dl=eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0,0,600), unreal.Rotator(pitch=-42.0,yaw=45.0,roll=0.0))
    dl.set_actor_label("DB_Sun")
    c=dl.get_component_by_class(unreal.DirectionalLightComponent)
    if c:
        c.set_intensity(8.0)
        try: c.set_editor_property("atmosphere_sun_light", True)
        except Exception: pass
if not has("DB_Sky"):
    sl=eas.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0,0,600))
    sl.set_actor_label("DB_Sky")
    c=sl.get_component_by_class(unreal.SkyLightComponent)
    if c:
        try: c.set_intensity(3.0)
        except Exception: pass
        try: c.set_editor_property("real_time_capture", True)
        except Exception: pass
if not has("DB_Atmo"):
    a=eas.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0,0,0))
    a.set_actor_label("DB_Atmo")
if not has("DB_Ground"):
    pm=unreal.load_asset("/Engine/BasicShapes/Plane.Plane")
    g=eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(0,0,0))
    g.set_actor_label("DB_Ground")
    comp=g.get_component_by_class(unreal.StaticMeshComponent)
    if comp and pm: comp.set_static_mesh(pm)
    g.set_actor_scale3d(unreal.Vector(80,80,1))
if not has("DB_PPV"):
    ppv=eas.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0,0,0))
    ppv.set_actor_label("DB_PPV")
    try:
        ppv.set_editor_property("unbound",True); ppv.set_editor_property("priority",100.0)
        s=ppv.settings
        s.override_auto_exposure_method=True; s.auto_exposure_method=unreal.AutoExposureMethod.AEM_HISTOGRAM
        s.override_auto_exposure_bias=True; s.auto_exposure_bias=1.0
        s.override_auto_exposure_min_brightness=True; s.auto_exposure_min_brightness=1.0
        s.override_auto_exposure_max_brightness=True; s.auto_exposure_max_brightness=3.0
        ppv.settings=s
    except Exception: pass
print("STAGE_READY")
'''


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def write_text_atomic(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def fsync_dir(path: pathlib.Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def jsonable(value: Any) -> Any:
    if isinstance(value, pathlib.Path):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value


def append_jsonl(path: pathlib.Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def safe_component(value: str) -> str:
    value = value.strip() or "uncategorized"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    proc_path = pathlib.Path(f"/proc/{pid}")
    if proc_path.exists():
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        pass
    except PermissionError:
        return True
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid="],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def acquire_lock(run_dir: pathlib.Path) -> None:
    global _LOCK_HANDLE
    lock_path = run_dir / "runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        existing = handle.read().strip()
        handle.close()
        raise RuntimeError(f"run directory is locked by another active process: {run_dir} {existing}")
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "created_at": utc_now(),
        "token": str(uuid.uuid4()),
    }, ensure_ascii=False) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    _LOCK_HANDLE = handle


def release_lock(run_dir: pathlib.Path) -> None:
    global _LOCK_HANDLE
    lock_path = run_dir / "runner.lock"
    if _LOCK_HANDLE:
        try:
            fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
            _LOCK_HANDLE.close()
        finally:
            _LOCK_HANDLE = None
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def catalog_records(asset_db_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    return {path.stem: path for path in asset_db_dir.glob("catalog/*/*.json")}


def find_catalog_record(asset_db_dir: pathlib.Path, asset_id: str) -> pathlib.Path | None:
    matches = sorted(asset_db_dir.glob(f"catalog/*/{asset_id}.json"))
    return matches[0] if matches else None


def read_asset_ids(raw: str | None, file_path: str | None) -> list[str]:
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


def select_assets(manifest: dict[str, Any], asset_ids: list[str], limit: int | None) -> list[dict[str, Any]]:
    assets = manifest.get("assets") or []
    if asset_ids:
        wanted = set(asset_ids)
        assets = [asset for asset in assets if asset.get("asset_id") in wanted]
        found = {asset.get("asset_id") for asset in assets}
        missing = [asset_id for asset_id in asset_ids if asset_id not in found]
        if missing:
            print(f"Warning: {len(missing)} requested asset ids are not in the manifest", file=sys.stderr)
            for asset_id in missing[:20]:
                print(f"  missing: {asset_id}", file=sys.stderr)
    if limit is not None:
        assets = assets[:limit]
    return assets


def resolve_schema(asset_db_dir: pathlib.Path, schema_arg: str | None) -> pathlib.Path:
    candidates = []
    if schema_arg:
        candidates.append(pathlib.Path(schema_arg))
    candidates.append(asset_db_dir / "schema" / "vlm_output_schema.json")
    candidates.append(REPO_ROOT / "tools" / "vlm_output_schema.json")
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("could not find vlm_output_schema.json")


def resolve_ue_shotdir(args: argparse.Namespace) -> pathlib.Path | None:
    shotdir = args.ue_shotdir or os.environ.get("UE_SHOTDIR", "")
    if shotdir:
        return pathlib.Path(shotdir)
    project = args.ue_project or os.environ.get("UE_PROJECT", "")
    if project:
        return pathlib.Path(project) / "Saved" / "Screenshots" / "LinuxEditor"
    return None


def ue(script: str, port: int, timeout: int = 120) -> dict[str, Any]:
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        payload = {"type": "execute_python_script", "params": {"script": script}}
        sock.sendall((json.dumps(payload) + "\n").encode())
        sock.settimeout(timeout)
        buf = b""
        result: dict[str, Any] = {}
        while True:
            data = sock.recv(65536)
            if not data:
                break
            buf += data
            try:
                result = json.loads(buf.decode())
                break
            except Exception:
                pass
        sock.close()
        return result
    except Exception as e:
        return {"error": str(e)}


def logs(resp: dict[str, Any] | None) -> list[str]:
    return (resp or {}).get("result", {}).get("python_logs", []) or []


def grab(resp: dict[str, Any] | None, tag: str) -> Any:
    for line in logs(resp):
        if tag in line:
            raw = line.split(tag, 1)[1]
            try:
                return json.loads(raw)
            except Exception:
                return raw
    return None


def spawn_and_measure(asset: dict[str, Any], mcp_port: int) -> dict[str, Any] | None:
    script = r'''
import unreal, json, math
eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
INFRA=%r
for a in list(eas.get_all_level_actors()):
    if a.get_actor_label() not in INFRA:
        try: eas.destroy_actor(a)
        except Exception: pass
unreal.SystemLibrary.collect_garbage()
path=%r; atype=%r
actor=None
try:
    if atype=="Blueprint":
        cp = path if path.endswith("_C") else path+"_C"
        cls=unreal.load_object(None, cp)
        if cls is None:
            bp=unreal.load_object(None, path)
            cls=bp.generated_class() if (bp and hasattr(bp,"generated_class")) else None
        actor=eas.spawn_actor_from_class(cls, unreal.Vector(0,0,0), unreal.Rotator(0,0,0)) if cls else None
    else:
        mesh=unreal.load_object(None, path)
        actor=eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))
        comp=actor.get_component_by_class(unreal.StaticMeshComponent)
        if comp and mesh: comp.set_static_mesh(mesh)
except Exception as e:
    print("SPAWNERR:"+str(e))
if actor is None:
    print("SPAWN_FAIL")
else:
    actor.set_actor_label("DB_Target")
    o0,e0=actor.get_actor_bounds(False)
    has_vis=False
    for comp in actor.get_components_by_class(unreal.StaticMeshComponent):
        if comp.get_editor_property("static_mesh"): has_vis=True; break
    if not has_vis:
        for comp in actor.get_components_by_class(unreal.SkeletalMeshComponent):
            try:
                if comp.get_editor_property("skeletal_mesh") or comp.get_editor_property("skeletal_mesh_asset"): has_vis=True; break
            except Exception: pass
    if (not has_vis) or (e0.x<1 and e0.y<1 and e0.z<1):
        print("NO_GEOMETRY")
    else:
        actor.set_actor_location(unreal.Vector(0,0, e0.z - o0.z), False, False)
        o,e=actor.get_actor_bounds(False)
        facts={}
        facts["dimensions_m"]={"width":round(2*e.x/100,3),"depth":round(2*e.y/100,3),"height":round(2*e.z/100,3)}
        facts["footprint_m"]={"width":round(2*e.x/100,3),"depth":round(2*e.y/100,3)}
        facts["bounding_radius_m"]=round(math.sqrt(e.x*e.x+e.y*e.y+e.z*e.z)/100,3)
        facts["center"]=[round(o.x,1),round(o.y,1),round(o.z,1)]
        facts["extent"]=[round(e.x,1),round(e.y,1),round(e.z,1)]
        facts["up_axis"]="Z"
        comp=actor.get_component_by_class(unreal.StaticMeshComponent)
        mats=[]
        if comp:
            try:
                for m in (comp.get_materials() or []):
                    mats.append(m.get_name() if m else "None")
            except Exception: pass
            try: facts["mobility"]=str(comp.get_editor_property("mobility")).split(".")[-1].split(":")[0].strip(" <>")
            except Exception: pass
        facts["material_slots"]=mats
        try:
            sm=comp.get_editor_property("static_mesh") if comp else None
            if sm:
                facts["lod_count"]=sm.get_num_lods() if hasattr(sm,"get_num_lods") else None
                try: facts["triangle_count"]=sm.get_num_triangles(0)
                except Exception: pass
        except Exception: pass
        facts["has_collision"]=bool(comp.get_collision_enabled()!=unreal.CollisionEnabled.NO_COLLISION) if comp else None
        print("FACTS:"+json.dumps(facts))
''' % (INFRA, asset["ue_path"], asset["asset_type"])
    resp = ue(script, mcp_port, 90)
    if any("SPAWN_FAIL" in line or "SPAWNERR" in line for line in logs(resp)) and not any(
        "FACTS:" in line for line in logs(resp)
    ):
        return None
    return grab(resp, "FACTS:")


def image_file_ready(path: pathlib.Path) -> bool:
    try:
        stat1 = path.stat()
        if stat1.st_size < 1024:
            return False
        with path.open("rb") as handle:
            header = handle.read(16)
        if not (header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff")):
            return False
        time.sleep(0.2)
        stat2 = path.stat()
        return stat2.st_size == stat1.st_size and stat2.st_mtime == stat1.st_mtime
    except OSError:
        return False


def game_view_console_setup() -> str:
    commands = [
        "viewmode lit",
        "showflag.game 1",
        "showflag.selectionoutline 0",
        "showflag.modewidgets 0",
        "showflag.bounds 0",
        "showflag.collision 0",
        "showflag.navigation 0",
        "DisableAllScreenMessages",
    ]
    return "\n".join(
        [
            "try:",
            "    unreal.EditorLevelLibrary.editor_set_game_view(True)",
            "except Exception as e:",
            "    print('GAME_VIEW_API_WARN:'+str(e))",
            f"for _cmd in {commands!r}:",
            "    try:",
            "        unreal.SystemLibrary.execute_console_command(None, _cmd)",
            "    except Exception as e:",
            "        print('GAME_VIEW_CMD_WARN:'+_cmd+':'+str(e))",
        ]
    )


def render_views(
    *,
    asset_id: str,
    facts: dict[str, Any],
    asset_db_dir: pathlib.Path,
    ue_shotdir: pathlib.Path,
    mcp_port: int,
    n_views: int,
    res: int,
) -> list[pathlib.Path]:
    outdir = asset_db_dir / "renders" / asset_id
    outdir.mkdir(parents=True, exist_ok=True)
    cx, cy, cz = facts["center"]
    ex, ey, ez = facts["extent"]
    radius = max((ex * ex + ey * ey + ez * ez) ** 0.5, 50.0)
    dist = radius * 2.6
    elev_deg = 20.0
    paths: list[pathlib.Path] = []
    for i in range(n_views):
        az = math.radians(i * (360.0 / n_views))
        camx = cx + dist * math.cos(az)
        camy = cy + dist * math.sin(az)
        camz = cz + dist * math.sin(math.radians(elev_deg))
        dx, dy, dz2 = cx - camx, cy - camy, cz - camz
        yaw = math.degrees(math.atan2(dy, dx))
        pitch = math.degrees(math.atan2(dz2, math.sqrt(dx * dx + dy * dy)))
        before = time.time() - 1
        script = (
            "import unreal\n"
            "sub=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\n"
            f"sub.set_level_viewport_camera_info(unreal.Vector({camx:.1f},{camy:.1f},{camz:.1f}),unreal.Rotator(pitch={pitch:.2f},yaw={yaw:.2f},roll=0.0))\n"
            f"{game_view_console_setup()}\n"
            f"unreal.SystemLibrary.execute_console_command(None,'HighResShot {res}x{res}')\n"
            "print('SHOT')"
        )
        ue(script, mcp_port, 40)
        got = None
        for _ in range(20):
            time.sleep(1.0)
            pngs = sorted(glob.glob(str(ue_shotdir / "*.png")), key=os.path.getmtime)
            for candidate in reversed(pngs[-5:]):
                candidate_path = pathlib.Path(candidate)
                if os.path.getmtime(candidate_path) > before and image_file_ready(candidate_path):
                    got = candidate_path
                    break
            if got:
                break
        if got:
            dest = outdir / f"view_{i:02d}.png"
            tmp = dest.with_suffix(dest.suffix + f".{os.getpid()}.tmp")
            shutil.copy(got, tmp)
            os.replace(tmp, dest)
            fsync_dir(outdir)
            paths.append(dest)
    return paths


def vlm_prompt(asset: dict[str, Any], facts: dict[str, Any], view_count: int) -> str:
    dims = facts.get("dimensions_m", {})
    return (
        "You are cataloging a single 3D asset for a general, multi-genre scene-generation asset database "
        "(scenes span many settings: modern city/street, industrial/logistics, suburban/residential, retail, "
        "rural/nature, harbor, medieval, fantasy/gothic, ancient temple, middle-eastern, east-asian, winter, sci-fi, indoor). "
        f"You are shown {view_count} rendered views of ONE object from different angles (orbit). "
        "Describe ONLY this object (ignore the gray ground plane and sky). "
        f"Measured size in meters (from the engine): width {dims.get('width')}, depth {dims.get('depth')}, height {dims.get('height')}. "
        f"Asset name hint: '{asset['ue_name']}'. Source pack: '{asset['source_pack']}'. "
        f"Unreal asset path hint: '{asset.get('ue_path')}'. "
        "Fill every field. Pick the single best-fit category from the allowed enum. "
        "For 'setting': choose the single primary thematic setting/genre this asset most reads as, from the allowed enum "
        "(modern_urban, industrial, suburban_residential, commercial_retail, nature_rural, coastal_harbor, medieval, "
        "fantasy_gothic, ancient_temple, middle_eastern, east_asian, winter, sci_fi, indoor, generic). "
        "Use 'generic' ONLY for plain, style-neutral props that fit many settings equally."
    )


def parse_codex_jsonl(stdout: str) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "thread_id": None,
        "usage": None,
        "last_agent_text": None,
        "raw_event_count": 0,
    }
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        trace["raw_event_count"] += 1
        if event.get("type") == "thread.started":
            trace["thread_id"] = event.get("thread_id")
        elif event.get("type") == "turn.completed":
            trace["usage"] = event.get("usage")
        elif event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                trace["last_agent_text"] = item.get("text")
    return trace


def run_vlm(
    *,
    asset: dict[str, Any],
    facts: dict[str, Any],
    view_paths: list[pathlib.Path],
    schema: pathlib.Path,
    model: str,
    reasoning_effort: str,
    codex_bin: str,
    run_dir: pathlib.Path,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    aid = asset["asset_id"]
    tmp_output = run_dir / "tmp" / f"{safe_component(aid)}_vlm.json"
    tmp_output.parent.mkdir(parents=True, exist_ok=True)
    if tmp_output.exists():
        tmp_output.unlink()
    args = [
        codex_bin,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        model,
    ]
    if reasoning_effort != "default":
        args += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
    args += [
        "--output-schema",
        str(schema),
        "-o",
        str(tmp_output),
        "-C",
        "/tmp",
    ]
    for path in view_paths:
        args += ["-i", str(path)]

    env = dict(os.environ, NO_COLOR="1")
    (run_dir / "codex_jsonl").mkdir(parents=True, exist_ok=True)
    (run_dir / "codex_stderr").mkdir(parents=True, exist_ok=True)
    attempt_id = f"{safe_component(aid)}_{int(time.time() * 1000)}_{os.getpid()}"
    stdout_path = run_dir / "codex_jsonl" / f"{attempt_id}.jsonl"
    stderr_path = run_dir / "codex_stderr" / f"{attempt_id}.log"
    try:
        completed = subprocess.run(
            args,
            input=vlm_prompt(asset, facts, len(view_paths)),
            text=True,
            env=env,
            timeout=timeout,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or ""
        stderr = e.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        trace = parse_codex_jsonl(stdout)
        trace["returncode"] = None
        trace["stderr_tail"] = stderr[-4000:]
        trace["model"] = model
        trace["reasoning_effort"] = reasoning_effort
        trace["stdout_path"] = str(stdout_path)
        trace["stderr_path"] = str(stderr_path)
        trace["attempt_id"] = attempt_id
        raise VlmCallError(f"Codex VLM call timed out after {timeout}s", trace)

    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    trace = parse_codex_jsonl(completed.stdout)
    trace["returncode"] = completed.returncode
    trace["stderr_tail"] = completed.stderr[-4000:]
    trace["model"] = model
    trace["reasoning_effort"] = reasoning_effort
    trace["stdout_path"] = str(stdout_path)
    trace["stderr_path"] = str(stderr_path)
    trace["attempt_id"] = attempt_id

    if tmp_output.exists():
        return load_json(tmp_output), trace
    if trace.get("last_agent_text"):
        try:
            return json.loads(trace["last_agent_text"]), trace
        except json.JSONDecodeError:
            pass
    raise VlmCallError(
        "Codex VLM call produced no output "
        f"(returncode={completed.returncode}). stderr tail:\n{trace['stderr_tail']}",
        trace,
    )


def qwen_data_url(path: pathlib.Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def qwen_prompt(asset: dict[str, Any], facts: dict[str, Any], view_count: int) -> str:
    dims = facts.get("dimensions_m", {})
    material_slots = facts.get("material_slots") or []
    categories = ", ".join(sorted(CATEGORY_ENUM))
    settings = ", ".join(sorted(SETTING_ENUM))
    return (
        "You are cataloging a single 3D asset for a general, multi-genre scene-generation asset database. "
        f"You are shown {view_count} rendered orbit views of ONE object from different angles. "
        "Describe ONLY this object; ignore the gray ground plane and sky. "
        f"Measured size in meters from the engine: width {dims.get('width')}, depth {dims.get('depth')}, height {dims.get('height')}. "
        f"Asset name hint: '{asset['ue_name']}'. Source pack: '{asset['source_pack']}'. "
        f"Unreal asset path hint: '{asset.get('ue_path')}'. "
        f"Engine material slot hints, if useful: {json.dumps(material_slots, ensure_ascii=False)[:1200]}. "
        "Use material slot names only as weak hints because game-material names may be generic; prefer visible physical material. "
        "Do not output unrelated materials such as plastic or metal unless they are visible or strongly indicated by the asset/path. "
        "Return ONLY a valid JSON object with exactly these keys: display_name, category, subcategory, "
        "short_description, description, tags, style, materials, color_palette, mood, typical_placement, "
        "function, affordances, scene_types, is_symmetric, condition, setting. "
        f"category must be one of: {categories}. setting must be one of: {settings}. "
        "Use 8-15 lowercase keyword tags. description should be 2-4 sentences. "
        "For setting, choose the single primary thematic setting/genre this asset reads as; use generic only for plain cross-setting props. "
        "Do not include markdown, comments, code fences, or extra text."
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
    raise ValueError(f"could not parse JSON object from VLM response: {raw[:500]}")


def run_qwen_vlm(
    *,
    asset: dict[str, Any],
    facts: dict[str, Any],
    view_paths: list[pathlib.Path],
    model: str,
    base_url: str,
    enable_thinking: bool,
    run_dir: pathlib.Path,
    timeout: int,
    max_tokens: int,
    temperature: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    aid = asset["asset_id"]
    attempt_id = f"{safe_component(aid)}_{int(time.time() * 1000)}_{os.getpid()}"
    raw_dir = run_dir / "qwen_raw"
    vlm_dir = run_dir / "qwen_vlm"
    raw_dir.mkdir(parents=True, exist_ok=True)
    vlm_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{attempt_id}.json"
    vlm_path = vlm_dir / f"{attempt_id}.json"

    content: list[dict[str, Any]] = [{"type": "text", "text": qwen_prompt(asset, facts, len(view_paths))}]
    for path in view_paths:
        content.append({"type": "image_url", "image_url": {"url": qwen_data_url(path)}})
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
    trace: dict[str, Any] = {
        "provider": "qwen",
        "model": model,
        "qwen_base_url": base_url,
        "qwen_enable_thinking": enable_thinking,
        "attempt_id": attempt_id,
        "raw_response_path": str(raw_path),
        "vlm_output_path": str(vlm_path),
        "returncode": None,
        "raw_event_count": None,
    }
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            raw = json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raw_path.write_text(body, encoding="utf-8")
        trace["duration_sec"] = round(time.time() - started, 3)
        trace["stderr_tail"] = body[-4000:]
        raise VlmCallError(f"Qwen VLM HTTP {e.code}: {body[:1000]}", trace) from e
    except Exception as e:
        trace["duration_sec"] = round(time.time() - started, 3)
        trace["stderr_tail"] = str(e)
        raise VlmCallError(f"Qwen VLM call failed: {e}", trace) from e

    duration_sec = round(time.time() - started, 3)
    write_json(raw_path, raw)
    choice = (raw.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""
    try:
        vlm = extract_json_object(text)
    except Exception as e:
        trace["duration_sec"] = duration_sec
        trace["usage"] = raw.get("usage") or {}
        trace["finish_reason"] = choice.get("finish_reason")
        trace["stderr_tail"] = text[-4000:]
        raise VlmCallError(f"Qwen VLM response was not valid JSON: {e}", trace) from e
    write_json(vlm_path, vlm)
    trace.update({
        "duration_sec": duration_sec,
        "usage": raw.get("usage") or {},
        "finish_reason": choice.get("finish_reason"),
        "returncode": 0,
    })
    return vlm, trace


def assemble_record(
    asset: dict[str, Any],
    facts: dict[str, Any],
    vlm: dict[str, Any],
    view_paths: list[pathlib.Path],
    asset_db_dir: pathlib.Path,
    model: str,
    run_id: str,
    run_dir: pathlib.Path,
) -> dict[str, Any]:
    vlm = normalize_vlm_metadata(vlm)
    rel_views = [os.path.relpath(path, asset_db_dir) for path in view_paths]
    return {
        "identity": {
            "asset_id": asset["asset_id"],
            "name": vlm.get("display_name") or asset["ue_name"],
            "category": vlm.get("category"),
            "subcategory": vlm.get("subcategory"),
            "source_pack": asset["source_pack"],
        },
        "semantic": {
            "short_description": vlm.get("short_description"),
            "description": vlm.get("description"),
            "tags": vlm.get("tags", []),
            "style": vlm.get("style"),
            "materials": vlm.get("materials", []),
            "color_palette": vlm.get("color_palette", []),
            "mood": vlm.get("mood", []),
            "typical_placement": vlm.get("typical_placement", []),
            "function": vlm.get("function"),
            "affordances": vlm.get("affordances", []),
            "scene_types": vlm.get("scene_types", []),
            "condition": vlm.get("condition"),
            "setting": vlm.get("setting"),
        },
        "geometry": {
            "dimensions_m": facts.get("dimensions_m"),
            "footprint_m": facts.get("footprint_m"),
            "bounding_radius_m": facts.get("bounding_radius_m"),
            "up_axis": facts.get("up_axis", "Z"),
            "pivot": "base_center",
            "is_symmetric": vlm.get("is_symmetric"),
            "default_scale": 1.0,
        },
        "technical": {
            "unreal_asset_path": asset["ue_path"],
            "asset_type": asset["asset_type"],
            "mobility": facts.get("mobility"),
            "has_collision": facts.get("has_collision"),
            "material_slots": facts.get("material_slots", []),
            "lod_count": facts.get("lod_count"),
            "triangle_count": facts.get("triangle_count"),
        },
        "indexing": {
            "render_views": rel_views,
            "view_count": len(rel_views),
            "capture_mode": "editor_game_view_highres",
            "caption_model": model,
            "schema_version": "1.0",
            "indexed_at_utc": utc_now(),
            "run_id": run_id,
            "run_dir": str(run_dir),
        },
    }


def validate_record(rec: dict[str, Any], min_views: int) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    ident = rec.get("identity", {})
    sem = rec.get("semantic", {})
    geo = rec.get("geometry", {})
    idx = rec.get("indexing", {})
    tech = rec.get("technical", {})

    for field in ["asset_id", "name", "category", "source_pack"]:
        if not ident.get(field):
            errors.append(f"missing identity.{field}")
    if ident.get("category") not in CATEGORY_ENUM:
        errors.append(f"invalid category {ident.get('category')!r}")
    if sem.get("setting") not in SETTING_ENUM:
        errors.append(f"invalid setting {sem.get('setting')!r}")
    for field in ["short_description", "description", "function"]:
        if not sem.get(field):
            warnings.append(f"missing semantic.{field}")
    if not isinstance(sem.get("tags"), list) or len(sem.get("tags") or []) < 6:
        warnings.append("tags list has fewer than 6 entries")
    if not isinstance(sem.get("materials"), list) or not sem.get("materials"):
        warnings.append("materials list is empty")
    dims = geo.get("dimensions_m") or {}
    if not dimensions_are_positive(dims):
        errors.append("nonpositive dimensions")
    if int(idx.get("view_count") or 0) < min_views:
        errors.append(f"view_count below minimum {min_views}")
    if not tech.get("unreal_asset_path"):
        errors.append("missing technical.unreal_asset_path")
    return errors, warnings


def dimensions_are_positive(dims: Any) -> bool:
    if not isinstance(dims, dict):
        return False
    for key in ["width", "depth", "height"]:
        try:
            if float(dims.get(key) or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def write_catalog_record(
    *,
    asset_db_dir: pathlib.Path,
    rec: dict[str, Any],
    old_path: pathlib.Path | None,
    force: bool,
) -> pathlib.Path:
    ident = rec["identity"]
    category = safe_component(ident.get("category") or "uncategorized")
    out_path = asset_db_dir / "catalog" / category / f"{safe_component(ident['asset_id'])}.json"
    write_json(out_path, rec)
    if force and old_path and old_path != out_path and old_path.exists():
        old_path.unlink()
    return out_path


def run_command(
    cmd: list[str],
    *,
    env: dict[str, str],
    log_path: pathlib.Path,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n$ {' '.join(cmd)}\n")
        log.flush()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            log.write(f"[timeout after {timeout}s]\n")
            proc = subprocess.CompletedProcess(cmd, 124)
        except Exception as e:
            log.write(f"[exception {type(e).__name__}: {e}]\n")
            proc = subprocess.CompletedProcess(cmd, 125)
        log.write(f"[exit {proc.returncode}]\n")
    return proc


def load_pending_ids(run_dir: pathlib.Path) -> set[str]:
    path = run_dir / "pending_db_asset_ids.txt"
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def save_pending_ids(run_dir: pathlib.Path, ids: set[str]) -> None:
    path = run_dir / "pending_db_asset_ids.txt"
    text = "".join(f"{asset_id}\n" for asset_id in sorted(ids))
    write_text_atomic(path, text)


def qdrant_point_id(asset_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simworld-asset:{asset_id}"))


def append_asset_result(
    run_dir: pathlib.Path,
    *,
    status: str,
    asset: dict[str, Any],
    idx: int | None = None,
    total: int | None = None,
    **fields: Any,
) -> None:
    rec = {
        "ts": utc_now(),
        "status": status,
        "idx": idx,
        "total": total,
        "asset_id": asset.get("asset_id"),
        "ue_name": asset.get("ue_name"),
        "source_pack": asset.get("source_pack"),
        "ue_path": asset.get("ue_path"),
        "asset_type": asset.get("asset_type"),
        **fields,
    }
    append_jsonl(run_dir / "asset_results.ndjson", rec)
    if status == "failed":
        append_jsonl(run_dir / "failed_assets.ndjson", rec)
    elif status.startswith("skip"):
        append_jsonl(run_dir / "skipped_assets.ndjson", rec)


def append_metering(
    run_dir: pathlib.Path,
    *,
    asset: dict[str, Any],
    status: str,
    phase: str,
    duration_sec: float | None = None,
    usage: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    reason: str | None = None,
    **fields: Any,
) -> None:
    trace = trace or {}
    rec = {
        "ts": utc_now(),
        "asset_id": asset.get("asset_id"),
        "ue_name": asset.get("ue_name"),
        "source_pack": asset.get("source_pack"),
        "asset_type": asset.get("asset_type"),
        "status": status,
        "phase": phase,
        "duration_sec": duration_sec,
        "reason": reason,
        "usage": usage if usage is not None else trace.get("usage"),
        "thread_id": trace.get("thread_id"),
        "returncode": trace.get("returncode"),
        "raw_event_count": trace.get("raw_event_count"),
        **fields,
    }
    append_jsonl(run_dir / "metering.ndjson", rec)
    append_jsonl(run_dir / "usage.ndjson", rec)


def write_id_list(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    seen = set()
    ids: list[str] = []
    for row in rows:
        asset_id = row.get("asset_id")
        if asset_id and asset_id not in seen:
            ids.append(str(asset_id))
            seen.add(asset_id)
    path.write_text("".join(f"{asset_id}\n" for asset_id in ids), encoding="utf-8")


def build_env(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    env["ASSET_DB_DIR"] = str(args.asset_db_dir)
    env["POSTGRES_URL"] = args.postgres_url
    env["QDRANT_URL"] = args.qdrant_url
    env["QDRANT_COLLECTION"] = args.qdrant_collection
    return env


def verify_postgres_asset_ids(postgres_url: str, asset_ids: list[str]) -> list[str]:
    if not asset_ids:
        return []
    import psycopg2

    conn = psycopg2.connect(postgres_url, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT asset_id FROM assets WHERE asset_id = ANY(%s)", (asset_ids,))
            found = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
    return [asset_id for asset_id in asset_ids if asset_id not in found]


def verify_qdrant_asset_ids(qdrant_url: str, collection: str, asset_ids: list[str]) -> list[str]:
    if not asset_ids:
        return []
    found: set[str] = set()
    url = qdrant_url.rstrip("/") + f"/collections/{collection}/points"
    for i in range(0, len(asset_ids), 128):
        chunk = asset_ids[i : i + 128]
        body = {
            "ids": [qdrant_point_id(asset_id) for asset_id in chunk],
            "with_payload": True,
            "with_vector": False,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for point in data.get("result") or []:
            payload = point.get("payload") or {}
            asset_id = payload.get("asset_id")
            if asset_id:
                found.add(asset_id)
    return [asset_id for asset_id in asset_ids if asset_id not in found]


def reconcile_current_run_pending(
    *,
    run_dir: pathlib.Path,
    asset_db_dir: pathlib.Path,
    assets: list[dict[str, Any]],
    pending_ids: set[str],
) -> list[str]:
    added: list[str] = []
    for asset in assets:
        asset_id = asset.get("asset_id")
        if not asset_id or asset_id in pending_ids:
            continue
        fp = find_catalog_record(asset_db_dir, asset_id)
        if not fp:
            continue
        try:
            rec = load_json(fp)
        except Exception:
            continue
        idx = rec.get("indexing") or {}
        if idx.get("run_id") == run_dir.name or idx.get("run_dir") == str(run_dir):
            pending_ids.add(asset_id)
            added.append(asset_id)
    if added:
        save_pending_ids(run_dir, pending_ids)
        append_jsonl(run_dir / "events.ndjson", {
            "ts": utc_now(),
            "event": "reconciled_pending_from_catalog",
            "count": len(added),
            "sample": added[:20],
        })
    return added


def sync_database(
    *,
    args: argparse.Namespace,
    run_dir: pathlib.Path,
    state: dict[str, Any],
    pending_ids: set[str],
    initial: bool = False,
    final: bool = False,
) -> bool:
    if args.no_db_sync or args.dry_run:
        return True
    if not initial and not pending_ids:
        return True

    sync_id = f"{'initial' if initial else 'final' if final else 'periodic'}_{stamp()}"
    sync_dir = run_dir / "db_sync" / sync_id
    sync_dir.mkdir(parents=True, exist_ok=True)
    env = build_env(args)
    asset_id_file = sync_dir / "asset_ids.txt"
    ids_for_sync: list[str] = []
    if not initial:
        ids_for_sync = sorted(pending_ids)
        asset_id_file.write_text("".join(f"{asset_id}\n" for asset_id in ids_for_sync), encoding="utf-8")

    append_jsonl(run_dir / "events.ndjson", {
        "ts": utc_now(),
        "event": "db_sync_start",
        "sync_id": sync_id,
        "initial": initial,
        "final": final,
        "asset_ids": len(ids_for_sync) if ids_for_sync else "all",
    })

    commands = []
    if initial:
        commands.append(([sys.executable, "tools/apply_schema.py"], sync_dir / "apply_schema.log"))
    migrate_cmd = [
        sys.executable,
        "tools/migrate_to_postgres.py",
        "--asset-db-dir",
        str(args.asset_db_dir),
        "--postgres-url",
        args.postgres_url,
        "--commit-every",
        str(args.db_commit_every),
    ]
    qdrant_cmd = [
        sys.executable,
        "tools/build_qdrant_index.py",
        "--postgres-url",
        args.postgres_url,
        "--qdrant-url",
        args.qdrant_url,
        "--collection",
        args.qdrant_collection,
        "--batch-size",
        str(args.embed_batch_size),
    ]
    if ids_for_sync:
        migrate_cmd += ["--asset-id-file", str(asset_id_file)]
        qdrant_cmd += ["--asset-id-file", str(asset_id_file)]
    commands.append((migrate_cmd, sync_dir / "migrate_to_postgres.log"))
    commands.append((qdrant_cmd, sync_dir / "build_qdrant_index.log"))

    ok = True
    for cmd, log_path in commands:
        proc = run_command(cmd, env=env, log_path=log_path, timeout=args.db_sync_timeout)
        if proc.returncode != 0:
            ok = False
            append_jsonl(run_dir / "events.ndjson", {
                "ts": utc_now(),
                "event": "db_sync_command_failed",
                "sync_id": sync_id,
                "cmd": cmd,
                "returncode": proc.returncode,
                "log": str(log_path),
            })
            break

    if ok and ids_for_sync:
        try:
            missing_pg = verify_postgres_asset_ids(args.postgres_url, ids_for_sync)
            missing_qd = verify_qdrant_asset_ids(args.qdrant_url, args.qdrant_collection, ids_for_sync)
            if missing_pg or missing_qd:
                ok = False
                append_jsonl(run_dir / "events.ndjson", {
                    "ts": utc_now(),
                    "event": "db_sync_verify_failed",
                    "sync_id": sync_id,
                    "missing_postgres": missing_pg[:50],
                    "missing_qdrant": missing_qd[:50],
                    "missing_postgres_count": len(missing_pg),
                    "missing_qdrant_count": len(missing_qd),
                })
        except Exception as e:
            ok = False
            append_jsonl(run_dir / "events.ndjson", {
                "ts": utc_now(),
                "event": "db_sync_verify_error",
                "sync_id": sync_id,
                "error": str(e),
            })

    if ok:
        category_proc = run_command(
            [sys.executable, "tools/build_category_index.py"],
            env=env,
            log_path=sync_dir / "build_category_index.log",
            timeout=args.category_index_timeout,
        )
        if category_proc.returncode != 0:
            ok = False

    if ok:
        if initial:
            pending_ids.clear()
            save_pending_ids(run_dir, pending_ids)
        elif ids_for_sync:
            pending_ids.difference_update(ids_for_sync)
            save_pending_ids(run_dir, pending_ids)
        if initial:
            state["initial_sync_done"] = True
        state["last_db_sync_at"] = utc_now()
        state["last_db_sync_id"] = sync_id
        state["pending_db_sync"] = len(pending_ids)
        append_jsonl(run_dir / "events.ndjson", {
            "ts": utc_now(),
            "event": "db_sync_ok",
            "sync_id": sync_id,
            "remaining_pending": len(pending_ids),
        })
    else:
        state["db_sync_failures"] = int(state.get("db_sync_failures") or 0) + 1
        append_jsonl(run_dir / "events.ndjson", {
            "ts": utc_now(),
            "event": "db_sync_failed",
            "sync_id": sync_id,
            "remaining_pending": len(pending_ids),
        })
    write_json(run_dir / "state.json", state)
    write_quality_snapshot(run_dir, state, args.asset_db_dir, pending_ids)
    return ok


def write_quality_snapshot(
    run_dir: pathlib.Path,
    state: dict[str, Any],
    asset_db_dir: pathlib.Path,
    pending_ids: set[str],
) -> None:
    catalog_counts: dict[str, int] = {}
    for path in asset_db_dir.glob("catalog/*/*.json"):
        catalog_counts[path.parent.name] = catalog_counts.get(path.parent.name, 0) + 1
    failures = tail_jsonl(run_dir / "failures.ndjson", 20)
    warnings = tail_jsonl(run_dir / "quality_warnings.ndjson", 20)
    snapshot = {
        "ts": utc_now(),
        "state": state,
        "catalog_total": sum(catalog_counts.values()),
        "catalog_counts": dict(sorted(catalog_counts.items())),
        "pending_db_sync": len(pending_ids),
        "recent_failures": failures,
        "recent_quality_warnings": warnings,
    }
    write_json(run_dir / "quality_snapshot.json", snapshot)
    lines = [
        "# Full Asset Index Quality Snapshot",
        "",
        f"Updated: {snapshot['ts']}",
        f"Catalog JSONs: {snapshot['catalog_total']}",
        f"Pending DB sync: {len(pending_ids)}",
        f"Indexed this run: {state.get('indexed', 0)}",
        f"Skipped existing: {state.get('skipped_existing', 0)}",
        f"Skipped configured: {state.get('skipped_configured', 0)}",
        f"Asset failures: {state.get('asset_failures', 0)}",
        f"Quality warnings: {state.get('quality_warnings', 0)}",
        "",
        "## Recent Failures",
        "",
    ]
    if failures:
        for item in failures:
            lines.append(f"- {item.get('asset_id')}: {item.get('reason')}")
    else:
        lines.append("None.")
    lines.extend(["", "## Recent Quality Warnings", ""])
    if warnings:
        for item in warnings:
            lines.append(f"- {item.get('asset_id')}: {', '.join(item.get('warnings') or [])}")
    else:
        lines.append("None.")
    (run_dir / "quality_snapshot.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def tail_jsonl(path: pathlib.Path, n: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def summarize_usage(usage_rows: list[dict[str, Any]]) -> dict[str, int]:
    keys = ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"]
    totals = {key: 0 for key in keys}
    totals["provider_total_tokens"] = 0
    records_with_usage = 0
    for row in usage_rows:
        usage = row.get("usage") or {}
        if usage:
            records_with_usage += 1
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        cached_input_tokens = int(usage.get("cached_input_tokens") or 0)
        reasoning_output_tokens = int(usage.get("reasoning_output_tokens") or 0)
        provider_total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        totals["input_tokens"] += input_tokens
        totals["cached_input_tokens"] += cached_input_tokens
        totals["output_tokens"] += output_tokens
        totals["reasoning_output_tokens"] += reasoning_output_tokens
        totals["provider_total_tokens"] += provider_total_tokens
    totals["records_with_usage"] = records_with_usage
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    totals["uncached_input_tokens"] = totals["input_tokens"] - totals["cached_input_tokens"]
    return totals


def write_run_summary(
    *,
    run_dir: pathlib.Path,
    state: dict[str, Any],
    asset_db_dir: pathlib.Path,
    pending_ids: set[str],
) -> None:
    failures = read_jsonl(run_dir / "failed_assets.ndjson")
    skipped = read_jsonl(run_dir / "skipped_assets.ndjson")
    results = read_jsonl(run_dir / "asset_results.ndjson")
    usage_rows = read_jsonl(run_dir / "usage.ndjson")
    status_rank = {
        "skip_existing": 1,
        "failed": 2,
        "skip_configured": 3,
        "ok": 4,
    }
    latest_by_asset: dict[str, dict[str, Any]] = {}
    for row in results:
        asset_id = row.get("asset_id")
        if asset_id:
            key = str(asset_id)
            old = latest_by_asset.get(key)
            status = str(row.get("status") or "")
            old_status = str(old.get("status") or "") if old else ""
            if old is None or status_rank.get(status, 0) >= status_rank.get(old_status, 0):
                latest_by_asset[key] = row
    latest_rows = list(latest_by_asset.values())
    latest_failures = [row for row in latest_rows if row.get("status") == "failed"]
    latest_skipped = [row for row in latest_rows if str(row.get("status") or "").startswith("skip")]
    write_id_list(run_dir / "failed_asset_ids.txt", latest_failures)
    write_id_list(run_dir / "skipped_asset_ids.txt", latest_skipped)

    status_counts: dict[str, int] = {}
    for row in latest_rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    catalog_counts: dict[str, int] = {}
    for path in asset_db_dir.glob("catalog/*/*.json"):
        catalog_counts[path.parent.name] = catalog_counts.get(path.parent.name, 0) + 1

    summary = {
        "ts": utc_now(),
        "run_dir": str(run_dir),
        "state": state,
        "status_counts": dict(sorted(status_counts.items())),
        "catalog_total": sum(catalog_counts.values()),
        "catalog_counts": dict(sorted(catalog_counts.items())),
        "pending_db_sync": len(pending_ids),
        "failed_assets": len(latest_failures),
        "skipped_assets": len(latest_skipped),
        "usage_totals": summarize_usage(usage_rows),
        "artifacts": {
            "asset_results": str(run_dir / "asset_results.ndjson"),
            "events": str(run_dir / "events.ndjson"),
            "failures": str(run_dir / "failed_assets.ndjson"),
            "failed_asset_ids": str(run_dir / "failed_asset_ids.txt"),
            "skipped_assets": str(run_dir / "skipped_assets.ndjson"),
            "skipped_asset_ids": str(run_dir / "skipped_asset_ids.txt"),
            "usage": str(run_dir / "usage.ndjson"),
            "metering": str(run_dir / "metering.ndjson"),
            "quality_warnings": str(run_dir / "quality_warnings.ndjson"),
            "pending_db_asset_ids": str(run_dir / "pending_db_asset_ids.txt"),
        },
    }
    write_json(run_dir / "run_summary.json", summary)

    usage = summary["usage_totals"]
    provider = state.get("caption_provider") or "codex"
    model_label = (
        f"{state.get('qwen_model')}:thinking_{'on' if state.get('qwen_enable_thinking') else 'off'}"
        if provider == "qwen"
        else state.get("model")
    )
    lines = [
        "# Full Asset Index Run Summary",
        "",
        f"Updated: {summary['ts']}",
        f"Run dir: `{run_dir}`",
        f"Provider: `{provider}`",
        f"Model: `{model_label}`",
        f"Reasoning effort: `{state.get('reasoning_effort')}`",
        f"Manifest: `{state.get('manifest')}`",
        "",
        "## Counts",
        "",
        f"- Selected assets: {state.get('total_selected')}",
        f"- Indexed this run: {state.get('indexed', 0)}",
        f"- Skipped existing: {state.get('skipped_existing', 0)}",
        f"- Skipped configured: {state.get('skipped_configured', 0)}",
        f"- Configured skip list size: {state.get('configured_skip_ids', 0)}",
        f"- Failed assets: {summary['failed_assets']}",
        f"- Quality warnings: {state.get('quality_warnings', 0)}",
        f"- Catalog JSONs now present: {summary['catalog_total']}",
        f"- Pending DB/Qdrant sync: {summary['pending_db_sync']}",
        f"- DB sync failures: {state.get('db_sync_failures', 0)}",
        "",
        "## Token Usage",
        "",
        f"- Records with usage: {usage['records_with_usage']}",
        f"- Input tokens: {usage['input_tokens']}",
        f"- Cached input tokens: {usage['cached_input_tokens']}",
        f"- Uncached input tokens: {usage['uncached_input_tokens']}",
        f"- Output tokens: {usage['output_tokens']}",
        f"- Reasoning output tokens: {usage['reasoning_output_tokens']}",
        f"- Total input+output tokens: {usage['total_tokens']}",
        f"- Provider reported total tokens: {usage['provider_total_tokens']}",
        "",
        "## Status Files",
        "",
        f"- Per-asset results: `{run_dir / 'asset_results.ndjson'}`",
        f"- Failed asset ids for retry: `{run_dir / 'failed_asset_ids.txt'}`",
        f"- Skipped asset ids: `{run_dir / 'skipped_asset_ids.txt'}`",
        f"- Raw events: `{run_dir / 'events.ndjson'}`",
        f"- Token usage rows: `{run_dir / 'usage.ndjson'}`",
        f"- Per-attempt metering rows: `{run_dir / 'metering.ndjson'}`",
        f"- Quality warnings: `{run_dir / 'quality_warnings.ndjson'}`",
        f"- Latest quality snapshot: `{run_dir / 'quality_snapshot.md'}`",
        "",
        "## Recent Failures",
        "",
    ]
    recent_failures = failures[-20:]
    if recent_failures:
        for row in recent_failures:
            lines.append(f"- `{row.get('asset_id')}`: {row.get('reason')}")
    else:
        lines.append("None.")
    lines.extend(["", "## Category Counts", ""])
    for category, count in sorted(catalog_counts.items()):
        lines.append(f"- `{category}`: {count}")
    (run_dir / "run_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_postgres(url: str) -> None:
    import psycopg2

    conn = psycopg2.connect(url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    finally:
        conn.close()


def check_qdrant(url: str) -> None:
    req = urllib.request.Request(url.rstrip("/") + "/collections")
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"Qdrant health returned HTTP {resp.status}")


def check_codex(codex_bin: str) -> str:
    proc = subprocess.run([codex_bin, "--version"], capture_output=True, text=True, check=False, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "codex --version failed")
    return (proc.stdout or proc.stderr).strip()


def check_qwen(base_url: str, model: str) -> str:
    req = urllib.request.Request(base_url.rstrip("/") + "/models")
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"Qwen models endpoint returned HTTP {resp.status}")
        data = json.loads(resp.read().decode("utf-8"))
    models = [item.get("id") for item in data.get("data") or [] if item.get("id")]
    if model and models and model not in models:
        raise RuntimeError(f"Qwen model {model!r} not listed by endpoint; available: {', '.join(models[:10])}")
    return ", ".join(models) if models else "models endpoint OK"


def check_ue(mcp_port: int) -> None:
    resp = ue("import unreal; print('UEOK')", mcp_port, 20)
    if "error" in resp or not any("UEOK" in line for line in logs(resp)):
        raise RuntimeError(f"UE MCP check failed: {resp}")


def run_preflight(args: argparse.Namespace, schema: pathlib.Path) -> None:
    if args.caption_provider == "codex":
        check_codex(args.codex_bin)
    elif args.caption_provider == "qwen":
        check_qwen(args.qwen_base_url, args.qwen_model)
    else:
        raise RuntimeError(f"unsupported caption provider: {args.caption_provider}")
    if not args.dry_run:
        if not args.skip_service_checks:
            shotdir = resolve_ue_shotdir(args)
            if not shotdir:
                raise RuntimeError("set UE_SHOTDIR or UE_PROJECT before running non-dry indexing")
            shotdir.mkdir(parents=True, exist_ok=True)
            check_ue(args.mcp_port)
            if not args.no_db_sync:
                try:
                    check_postgres(args.postgres_url)
                    check_qdrant(args.qdrant_url)
                except Exception:
                    if args.allow_db_offline:
                        print("Warning: DB service check failed; continuing because --allow-db-offline is set.", file=sys.stderr)
                    else:
                        raise
    if not schema.exists():
        raise RuntimeError(f"schema not found: {schema}")
    args.asset_db_dir.mkdir(parents=True, exist_ok=True)
    (args.asset_db_dir / "catalog").mkdir(parents=True, exist_ok=True)
    (args.asset_db_dir / "renders").mkdir(parents=True, exist_ok=True)


def asset_failure(
    *,
    run_dir: pathlib.Path,
    state: dict[str, Any],
    asset: dict[str, Any],
    reason: str,
    detail: Any = None,
) -> None:
    state["asset_failures"] = int(state.get("asset_failures") or 0) + 1
    event = {
        "ts": utc_now(),
        "event": "asset_failed",
        "asset_id": asset.get("asset_id"),
        "ue_name": asset.get("ue_name"),
        "reason": reason,
        "detail": detail,
    }
    append_jsonl(run_dir / "events.ndjson", event)
    append_jsonl(run_dir / "failures.ndjson", event)


def maybe_sync(
    *,
    args: argparse.Namespace,
    run_dir: pathlib.Path,
    state: dict[str, Any],
    pending_ids: set[str],
    force: bool = False,
    final: bool = False,
) -> bool:
    if args.no_db_sync or args.dry_run:
        return True
    if not pending_ids:
        return True
    last_attempt_ts = state.get("_last_sync_attempt_monotonic") or time.monotonic()
    last_attempt_pending = int(state.get("_last_sync_attempt_pending_count") or 0)
    enough_assets = len(pending_ids) - last_attempt_pending >= args.sync_every_assets
    enough_time = (time.monotonic() - float(last_attempt_ts)) >= args.sync_min_seconds
    if not force and not (enough_assets or enough_time):
        return True
    state["_last_sync_attempt_monotonic"] = time.monotonic()
    state["_last_sync_attempt_pending_count"] = len(pending_ids)
    ok = sync_database(args=args, run_dir=run_dir, state=state, pending_ids=pending_ids, final=final)
    if ok:
        state["_last_sync_monotonic"] = time.monotonic()
        state["_last_sync_attempt_pending_count"] = 0
    return ok


def finish_aborted_run(
    *,
    run_dir: pathlib.Path,
    state: dict[str, Any],
    args: argparse.Namespace,
    pending_ids: set[str],
    reason: str,
    code: int,
) -> int:
    state["aborted_at"] = utc_now()
    state["abort_reason"] = reason
    state["pending_db_sync"] = len(pending_ids)
    write_json(run_dir / "state.json", state)
    write_quality_snapshot(run_dir, state, args.asset_db_dir, pending_ids)
    write_run_summary(run_dir=run_dir, state=state, asset_db_dir=args.asset_db_dir, pending_ids=pending_ids)
    append_jsonl(run_dir / "events.ndjson", {
        "ts": utc_now(),
        "event": "run_aborted",
        "reason": reason,
        "code": code,
        "pending_db_sync": len(pending_ids),
    })
    return code


def run_index(args: argparse.Namespace) -> int:
    args.asset_db_dir = pathlib.Path(args.asset_db_dir)
    args.manifest = pathlib.Path(args.manifest)
    schema = resolve_schema(args.asset_db_dir, args.schema)
    if not args.run_dir:
        provider_label = "qwen36" if args.caption_provider == "qwen" else "gpt55"
        args.run_dir = args.asset_db_dir / "runs" / f"full_index_{provider_label}_{stamp()}"
    else:
        args.run_dir = pathlib.Path(args.run_dir)
    run_dir: pathlib.Path = args.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    acquire_lock(run_dir)
    try:
        pending_ids = load_pending_ids(run_dir)
        manifest = load_json(args.manifest)
        asset_ids = read_asset_ids(args.asset_ids, args.asset_id_file)
        skip_asset_ids = set(read_asset_ids(args.skip_asset_ids, args.skip_asset_id_file))
        assets = select_assets(manifest, asset_ids, args.limit)
        existing = catalog_records(args.asset_db_dir)
        state_path = run_dir / "state.json"
        if state_path.exists():
            state = load_json(state_path)
        else:
            state = {}
        if not args.dry_run:
            state.pop("finished_at", None)
            state.pop("dry_run_complete", None)
            state.pop("aborted_at", None)
            state.pop("abort_reason", None)
            state["resume_count"] = int(state.get("resume_count") or 0) + 1
        state.update(
            {
                "started_or_resumed_at": utc_now(),
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
            }
        )
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
        write_json(run_dir / "config.json", jsonable(vars(args) | {"schema": str(schema)}))
        write_text_atomic(
            run_dir / "selected_asset_ids.txt",
            "".join(f"{asset.get('asset_id')}\n" for asset in assets),
        )
        write_json(run_dir / "pid.json", {"pid": os.getpid(), "started_at": utc_now()})
        write_json(state_path, state)
        append_jsonl(run_dir / "events.ndjson", {
            "ts": utc_now(),
            "event": "run_start",
            "total_selected": len(assets),
            "dry_run": args.dry_run,
        })

        run_preflight(args, schema)
        if not args.dry_run:
            reconciled = reconcile_current_run_pending(
                run_dir=run_dir,
                asset_db_dir=args.asset_db_dir,
                assets=assets,
                pending_ids=pending_ids,
            )
            if reconciled:
                state["pending_db_sync"] = len(pending_ids)
                write_json(state_path, state)

        if args.dry_run:
            for idx, asset in enumerate(assets, 1):
                aid = asset["asset_id"]
                if aid in skip_asset_ids:
                    status = "skip_configured"
                    reason = "configured helper/non-spawnable skip"
                else:
                    status = "skip_existing" if aid in existing and not args.force else "would_index"
                    reason = None
                append_asset_result(
                    run_dir,
                    status=status,
                    asset=asset,
                    idx=idx,
                    total=len(assets),
                    existing_path=str(existing[aid]) if aid in existing else None,
                    skip_list=args.skip_asset_id_file or None,
                    reason=reason,
                    dry_run=True,
                )
                append_jsonl(run_dir / "events.ndjson", {
                    "ts": utc_now(),
                    "event": status,
                    "idx": idx,
                    "total": len(assets),
                    "asset_id": aid,
                    "ue_path": asset.get("ue_path"),
                })
            state["dry_run_complete"] = True
            state["finished_at"] = utc_now()
            write_json(state_path, state)
            write_quality_snapshot(run_dir, state, args.asset_db_dir, pending_ids)
            write_run_summary(run_dir=run_dir, state=state, asset_db_dir=args.asset_db_dir, pending_ids=pending_ids)
            print(f"Dry run complete. Run dir: {run_dir}")
            return 0

        if not args.no_initial_sync and not state.get("initial_sync_done"):
            ok = sync_database(args=args, run_dir=run_dir, state=state, pending_ids=pending_ids, initial=True)
            if not ok and not args.allow_db_offline:
                print("Initial DB sync failed; aborting before generation.", file=sys.stderr)
                return finish_aborted_run(
                    run_dir=run_dir,
                    state=state,
                    args=args,
                    pending_ids=pending_ids,
                    reason="initial DB sync failed before generation",
                    code=2,
                )
        elif pending_ids and not args.no_db_sync:
            ok = sync_database(args=args, run_dir=run_dir, state=state, pending_ids=pending_ids)
            if not ok and not args.allow_db_offline:
                print("Startup pending DB sync failed; aborting before generation.", file=sys.stderr)
                return finish_aborted_run(
                    run_dir=run_dir,
                    state=state,
                    args=args,
                    pending_ids=pending_ids,
                    reason="startup pending DB sync failed before generation",
                    code=2,
                )

        shotdir = resolve_ue_shotdir(args)
        if not shotdir:
            raise RuntimeError("UE_SHOTDIR/UE_PROJECT unexpectedly missing after preflight")
        resp = ue(SETUP_STAGE, args.mcp_port, 60)
        if "error" in resp:
            return finish_aborted_run(
                run_dir=run_dir,
                state=state,
                args=args,
                pending_ids=pending_ids,
                reason=f"UE stage setup failed: {resp}",
                code=3,
            )

        consecutive_failures = 0
        phase_failures: dict[str, int] = {}
        for idx, asset in enumerate(assets, 1):
            aid = asset["asset_id"]
            state["last_asset_id"] = aid
            state["last_asset_index"] = idx
            write_json(state_path, state)

            if aid in skip_asset_ids:
                consecutive_failures = 0
                phase_failures.clear()
                state["skipped_configured"] = int(state.get("skipped_configured") or 0) + 1
                append_asset_result(
                    run_dir,
                    status="skip_configured",
                    asset=asset,
                    idx=idx,
                    total=len(assets),
                    reason="configured helper/non-spawnable skip",
                    skip_list=args.skip_asset_id_file or None,
                )
                append_jsonl(run_dir / "events.ndjson", {
                    "ts": utc_now(),
                    "event": "skip_configured",
                    "idx": idx,
                    "total": len(assets),
                    "asset_id": aid,
                    "skip_list": args.skip_asset_id_file or None,
                    "reason": "configured helper/non-spawnable skip",
                })
                write_json(state_path, state)
                continue

            old_path = existing.get(aid)
            if old_path and not args.force:
                consecutive_failures = 0
                phase_failures.clear()
                state["skipped_existing"] = int(state.get("skipped_existing") or 0) + 1
                append_asset_result(
                    run_dir,
                    status="skip_existing",
                    asset=asset,
                    idx=idx,
                    total=len(assets),
                    existing_path=str(old_path),
                )
                append_jsonl(run_dir / "events.ndjson", {
                    "ts": utc_now(),
                    "event": "skip_existing",
                    "idx": idx,
                    "total": len(assets),
                    "asset_id": aid,
                    "path": str(old_path),
                })
                continue

            print(f"[{idx}/{len(assets)}] {aid} ({asset.get('source_pack')}/{asset.get('ue_name')})", flush=True)
            append_jsonl(run_dir / "events.ndjson", {
                "ts": utc_now(),
                "event": "asset_start",
                "idx": idx,
                "total": len(assets),
                "asset_id": aid,
            })
            asset_started = time.time()
            phase = "spawn_measure"
            trace: dict[str, Any] | None = None
            try:
                facts = spawn_and_measure(asset, args.mcp_port)
                if not facts:
                    raise RuntimeError("spawn/measure failed")
                phase = "geometry_validate"
                if not dimensions_are_positive(facts.get("dimensions_m")):
                    raise RuntimeError("nonpositive dimensions")
                phase = "render_views"
                views = render_views(
                    asset_id=aid,
                    facts=facts,
                    asset_db_dir=args.asset_db_dir,
                    ue_shotdir=shotdir,
                    mcp_port=args.mcp_port,
                    n_views=args.n_views,
                    res=args.res,
                )
                if len(views) < args.min_views:
                    raise RuntimeError(f"only {len(views)} views rendered")
                phase = f"{args.caption_provider}_vlm"
                if args.caption_provider == "qwen":
                    caption_model = f"{args.qwen_model}:thinking_{'on' if args.qwen_enable_thinking else 'off'}"
                    vlm, trace = run_qwen_vlm(
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
                else:
                    caption_model = args.model
                    vlm, trace = run_vlm(
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
                phase = "validate_write"
                rec = assemble_record(asset, facts, vlm, views, args.asset_db_dir, caption_model, run_dir.name, run_dir)
                errors, warnings = validate_record(rec, args.min_views)
                if errors:
                    raise RuntimeError("; ".join(errors))
                pending_ids.add(aid)
                save_pending_ids(run_dir, pending_ids)
                state["pending_db_sync"] = len(pending_ids)
                out_path = write_catalog_record(
                    asset_db_dir=args.asset_db_dir,
                    rec=rec,
                    old_path=old_path,
                    force=args.force,
                )
                existing[aid] = out_path
                state["indexed"] = int(state.get("indexed") or 0) + 1
                consecutive_failures = 0
                phase_failures.clear()
                duration_sec = round(time.time() - asset_started, 3)
                append_metering(
                    run_dir,
                    asset=asset,
                    status="ok",
                    phase="complete",
                    duration_sec=duration_sec,
                    trace=trace,
                    provider=args.caption_provider,
                    model=caption_model,
                    reasoning_effort=args.reasoning_effort,
                    vlm_duration_sec=trace.get("duration_sec"),
                    stdout_path=trace.get("stdout_path"),
                    stderr_path=trace.get("stderr_path"),
                    raw_response_path=trace.get("raw_response_path"),
                    vlm_output_path=trace.get("vlm_output_path"),
                )
                if warnings:
                    state["quality_warnings"] = int(state.get("quality_warnings") or 0) + 1
                    append_jsonl(run_dir / "quality_warnings.ndjson", {
                        "ts": utc_now(),
                        "asset_id": aid,
                        "warnings": warnings,
                    })
                append_asset_result(
                    run_dir,
                    status="ok",
                    asset=asset,
                    idx=idx,
                    total=len(assets),
                    category=rec["identity"]["category"],
                    setting=rec["semantic"]["setting"],
                    output_path=str(out_path),
                    views=len(views),
                    duration_sec=duration_sec,
                    warnings=warnings,
                    usage=trace.get("usage"),
                )
                append_jsonl(run_dir / "events.ndjson", {
                    "ts": utc_now(),
                    "event": "asset_ok",
                    "idx": idx,
                    "total": len(assets),
                    "asset_id": aid,
                    "category": rec["identity"]["category"],
                    "setting": rec["semantic"]["setting"],
                    "path": str(out_path),
                    "views": len(views),
                    "warnings": warnings,
                })
                print(f"   OK -> {out_path.parent.name}/{out_path.name} | views={len(views)}", flush=True)
                maybe_sync(args=args, run_dir=run_dir, state=state, pending_ids=pending_ids)
                write_json(state_path, state)
            except Exception as e:
                consecutive_failures += 1
                failure_trace = e.trace if isinstance(e, VlmCallError) else trace
                duration_sec = round(time.time() - asset_started, 3)
                phase_failures[phase] = int(phase_failures.get(phase) or 0) + 1
                asset_failure(run_dir=run_dir, state=state, asset=asset, reason=str(e))
                append_metering(
                    run_dir,
                    asset=asset,
                    status="failed",
                    phase=phase,
                    duration_sec=duration_sec,
                    trace=failure_trace,
                    reason=str(e),
                    provider=args.caption_provider,
                    model=(
                        f"{args.qwen_model}:thinking_{'on' if args.qwen_enable_thinking else 'off'}"
                        if args.caption_provider == "qwen" else args.model
                    ),
                    reasoning_effort=args.reasoning_effort,
                    vlm_duration_sec=(failure_trace or {}).get("duration_sec"),
                    stdout_path=(failure_trace or {}).get("stdout_path"),
                    stderr_path=(failure_trace or {}).get("stderr_path"),
                    raw_response_path=(failure_trace or {}).get("raw_response_path"),
                    vlm_output_path=(failure_trace or {}).get("vlm_output_path"),
                )
                append_asset_result(
                    run_dir,
                    status="failed",
                    asset=asset,
                    idx=idx,
                    total=len(assets),
                    reason=str(e),
                    phase=phase,
                    duration_sec=duration_sec,
                    usage=(failure_trace or {}).get("usage"),
                )
                write_json(state_path, state)
                print(f"   FAILED: {e}", flush=True)
                if (
                    args.max_consecutive_vlm_failures > 0
                    and phase.endswith("_vlm")
                    and phase_failures[phase] >= args.max_consecutive_vlm_failures
                ):
                    append_jsonl(run_dir / "events.ndjson", {
                        "ts": utc_now(),
                        "event": "abort_repeated_vlm_failures",
                        "provider": args.caption_provider,
                        "count": phase_failures[phase],
                        "last_error": str(e),
                    })
                    print("ABORT: repeated VLM failures. Fix provider/auth/quota/model/schema issue and rerun to resume.", file=sys.stderr)
                    return finish_aborted_run(
                        run_dir=run_dir,
                        state=state,
                        args=args,
                        pending_ids=pending_ids,
                        reason=f"repeated VLM failures: {e}",
                        code=5,
                    )
                if args.max_consecutive_failures > 0 and consecutive_failures >= args.max_consecutive_failures:
                    try:
                        check_ue(args.mcp_port)
                        if (
                            args.max_consecutive_phase_failures > 0
                            and phase_failures.get(phase, 0) >= args.max_consecutive_phase_failures
                        ):
                            append_jsonl(run_dir / "events.ndjson", {
                                "ts": utc_now(),
                                "event": "abort_repeated_phase_failures",
                                "phase": phase,
                                "count": phase_failures[phase],
                                "last_error": str(e),
                            })
                            print(f"ABORT: repeated {phase} failures while UE is responsive. Fix the phase-specific issue and rerun.", file=sys.stderr)
                            return finish_aborted_run(
                                run_dir=run_dir,
                                state=state,
                                args=args,
                                pending_ids=pending_ids,
                                reason=f"repeated {phase} failures: {e}",
                                code=6,
                            )
                        append_jsonl(run_dir / "events.ndjson", {
                            "ts": utc_now(),
                            "event": "consecutive_failures_but_ue_alive",
                            "count": consecutive_failures,
                        })
                        consecutive_failures = 0
                    except Exception as ue_err:
                        append_jsonl(run_dir / "events.ndjson", {
                            "ts": utc_now(),
                            "event": "abort_ue_unresponsive",
                            "count": consecutive_failures,
                            "error": str(ue_err),
                        })
                        print("ABORT: UE appears unresponsive. Relaunch UE and rerun to resume.", file=sys.stderr)
                        return finish_aborted_run(
                            run_dir=run_dir,
                            state=state,
                            args=args,
                            pending_ids=pending_ids,
                            reason=f"UE unresponsive after consecutive failures: {ue_err}",
                            code=3,
                        )

        final_sync_ok = maybe_sync(args=args, run_dir=run_dir, state=state, pending_ids=pending_ids, force=True, final=True)
        state["finished_at"] = utc_now()
        state["pending_db_sync"] = len(pending_ids)
        write_json(state_path, state)
        write_quality_snapshot(run_dir, state, args.asset_db_dir, pending_ids)
        write_run_summary(run_dir=run_dir, state=state, asset_db_dir=args.asset_db_dir, pending_ids=pending_ids)
        append_jsonl(run_dir / "events.ndjson", {
            "ts": utc_now(),
            "event": "run_finished",
            "indexed": state.get("indexed", 0),
            "skipped_existing": state.get("skipped_existing", 0),
            "asset_failures": state.get("asset_failures", 0),
            "pending_db_sync": len(pending_ids),
            "final_sync_ok": final_sync_ok,
        })
        if pending_ids and not final_sync_ok:
            print(f"Run finished but {len(pending_ids)} assets still need DB sync.", file=sys.stderr)
            return 4
        print(f"Run complete. Run dir: {run_dir}")
        return 0
    finally:
        release_lock(run_dir)


def check_command(args: argparse.Namespace) -> int:
    args.asset_db_dir = pathlib.Path(args.asset_db_dir)
    args.manifest = pathlib.Path(args.manifest)
    schema = resolve_schema(args.asset_db_dir, args.schema)
    manifest = load_json(args.manifest)
    asset_ids = read_asset_ids(args.asset_ids, args.asset_id_file)
    assets = select_assets(manifest, asset_ids, args.limit)
    existing = catalog_records(args.asset_db_dir)
    pending = [asset for asset in assets if asset.get("asset_id") not in existing]
    print(f"asset_db_dir: {args.asset_db_dir}")
    print(f"manifest: {args.manifest}")
    print(f"manifest assets selected: {len(assets)}")
    print(f"existing catalog JSONs: {len(existing)}")
    print(f"pending selected assets: {len(pending)}")
    print(f"schema: {schema}")
    print(f"caption provider: {args.caption_provider}")
    if args.caption_provider == "qwen":
        print(f"model: {args.qwen_model}")
        try:
            print(f"qwen: {check_qwen(args.qwen_base_url, args.qwen_model)}")
        except Exception as e:
            print(f"qwen: FAIL {e}")
            return 1
    else:
        print(f"model: {args.model}")
        try:
            print(f"codex: {check_codex(args.codex_bin)}")
        except Exception as e:
            print(f"codex: FAIL {e}")
            return 1
    if not args.skip_service_checks:
        try:
            check_ue(args.mcp_port)
            print("ue_mcp: OK")
        except Exception as e:
            print(f"ue_mcp: FAIL {e}")
            return 1
        if not args.no_db_sync:
            try:
                check_postgres(args.postgres_url)
                print("postgres: OK")
            except Exception as e:
                print(f"postgres: FAIL {e}")
                return 1
            try:
                check_qdrant(args.qdrant_url)
                print("qdrant: OK")
            except Exception as e:
                print(f"qdrant: FAIL {e}")
                return 1
    print("pending sample:")
    for asset in pending[:10]:
        print(f"  {asset.get('asset_id')} | {asset.get('source_pack')} | {asset.get('ue_name')}")
    return 0


def find_latest_run(asset_db_dir: pathlib.Path) -> pathlib.Path | None:
    run_root = asset_db_dir / "runs"
    if not run_root.exists():
        return None
    runs = [path for path in run_root.iterdir() if path.is_dir() and (path / "state.json").exists()]
    if not runs:
        return None
    return max(runs, key=lambda p: p.stat().st_mtime)


def format_progress_bar(done: int, total: int, width: int = 40) -> str:
    if total <= 0:
        return f"[{'-' * width}] 0/0 (0.0%)"
    done = max(0, min(done, total))
    pct = (done / total) * 100.0
    filled = int(round((done / total) * width))
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {done}/{total} ({pct:.1f}%)"


def parse_utc_timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def format_elapsed(start_value: Any, end_value: Any | None = None) -> str:
    start = parse_utc_timestamp(start_value)
    if not start:
        return "unknown"
    end = parse_utc_timestamp(end_value) if end_value else dt.datetime.now(dt.timezone.utc)
    if not end:
        end = dt.datetime.now(dt.timezone.utc)
    seconds = max(0, int((end - start).total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def event_stream_recent(events: list[dict[str, Any]], max_age_sec: int = 600) -> bool:
    if not events:
        return False
    ts = parse_utc_timestamp(events[-1].get("ts"))
    if not ts:
        return False
    age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
    return 0 <= age <= max_age_sec


def status_recent_activity(run_dir: pathlib.Path, limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in tail_jsonl(run_dir / "events.ndjson", limit):
        row = dict(event)
        row.setdefault("event", "event")
        rows.append(row)
    for result in tail_jsonl(run_dir / "asset_results.ndjson", limit):
        status = str(result.get("status") or "unknown")
        row = {
            "ts": result.get("ts"),
            "event": f"result_{status}",
            "asset_id": result.get("asset_id", ""),
            "reason": result.get("reason") or result.get("existing_path") or "",
        }
        rows.append(row)
    rows.sort(key=lambda row: parse_utc_timestamp(row.get("ts")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    return rows[-limit:]


def status_effective_counts(run_dir: pathlib.Path) -> tuple[dict[str, int], int]:
    rows = read_jsonl(run_dir / "asset_results.ndjson")
    if not rows:
        return {}, 0
    # Relaunches append skip_existing records for assets already written by this
    # run. Prefer the strongest effective terminal state so monitor counters do
    # not drift just because the runner was resumed.
    status_rank = {
        "skip_existing": 1,
        "failed": 2,
        "skip_configured": 3,
        "ok": 4,
    }
    effective_by_asset: dict[str, str] = {}
    for row in rows:
        asset_id = row.get("asset_id")
        status = row.get("status")
        status = str(status or "")
        if not asset_id or status not in status_rank:
            continue
        old_status = effective_by_asset.get(str(asset_id))
        if old_status is None or status_rank[status] >= status_rank[old_status]:
            effective_by_asset[str(asset_id)] = status
    counts: dict[str, int] = {}
    for status in effective_by_asset.values():
        counts[status] = counts.get(status, 0) + 1
    completed_statuses = {"ok", "failed", "skip_existing", "skip_configured"}
    completed = sum(1 for status in effective_by_asset.values() if status in completed_statuses)
    return counts, completed


def status_command(args: argparse.Namespace) -> int:
    asset_db_dir = pathlib.Path(args.asset_db_dir)
    run_dir = pathlib.Path(args.run_dir) if args.run_dir else find_latest_run(asset_db_dir)
    if not run_dir:
        print("No run directory found.")
        return 1
    state_path = run_dir / "state.json"
    if not state_path.exists():
        print(f"No state.json at {run_dir}")
        return 1
    state = load_json(state_path)
    pid = int(state.get("pid") or 0)
    events = status_recent_activity(run_dir, 50)
    if state.get("aborted_at"):
        pid_status = "aborted"
    elif state.get("finished_at") or state.get("dry_run_complete"):
        pid_status = "finished"
    else:
        pid_status = "alive" if pid_alive(pid) else "not running"
    pending = load_pending_ids(run_dir)
    print(f"run_dir: {run_dir}")
    print(f"pid: {pid} ({pid_status})")
    provider = state.get("caption_provider") or "codex"
    if provider == "qwen":
        print(f"provider: qwen model={state.get('qwen_model')} thinking={state.get('qwen_enable_thinking')}")
    else:
        print(f"model: {state.get('model')} reasoning={state.get('reasoning_effort')}")
    total_selected = int(state.get("total_selected") or 0)
    state_indexed = int(state.get("indexed") or 0)
    state_skipped_existing = int(state.get("skipped_existing") or 0)
    state_skipped_configured = int(state.get("skipped_configured") or 0)
    state_asset_failures = int(state.get("asset_failures") or 0)
    fallback_completed = state_indexed + state_skipped_existing + state_skipped_configured + state_asset_failures
    effective_counts, completed = status_effective_counts(run_dir)
    have_effective_counts = bool(effective_counts)
    if not completed:
        completed = fallback_completed
    indexed = effective_counts.get("ok", 0 if have_effective_counts else state_indexed)
    skipped_existing = effective_counts.get("skip_existing", 0 if have_effective_counts else state_skipped_existing)
    skipped_configured = effective_counts.get("skip_configured", 0 if have_effective_counts else state_skipped_configured)
    asset_failures = effective_counts.get("failed", 0 if have_effective_counts else state_asset_failures)
    print(f"selected: {total_selected}")
    print(f"progress: {format_progress_bar(completed, total_selected)}")
    print(f"elapsed: {format_elapsed(state.get('first_started_at') or state.get('started_or_resumed_at'), state.get('finished_at'))}")
    print(f"last asset: {state.get('last_asset_index')} {state.get('last_asset_id')}")
    print(f"indexed this run: {indexed}")
    print(f"skipped existing: {skipped_existing}")
    print(f"skipped configured: {skipped_configured}")
    print(f"asset failures: {asset_failures}")
    print(f"quality warnings: {state.get('quality_warnings', 0)}")
    print(f"pending DB sync: {len(pending)}")
    print(f"last DB sync: {state.get('last_db_sync_at')}")
    print(f"quality snapshot: {run_dir / 'quality_snapshot.md'}")
    print(f"run summary: {run_dir / 'run_summary.md'}")
    if events:
        print("recent events:")
        for event in events:
            print(f"  {event.get('ts')} {event.get('event')} {event.get('asset_id', '')} {event.get('reason', '')}")
    return 0


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--asset-db-dir", default=os.environ.get("ASSET_DB_DIR", str(DEFAULT_ASSET_DB_DIR)))
    p.add_argument("--manifest", default=os.environ.get("MANIFEST", str(DEFAULT_ASSET_DB_DIR / "manifest_full.json")))
    p.add_argument("--schema", default=os.environ.get("VLM_SCHEMA", ""))
    p.add_argument("--caption-provider", choices=["codex", "qwen"], default=os.environ.get("CAPTION_PROVIDER", "codex"))
    p.add_argument("--model", default=os.environ.get("CODEX_MODEL", "gpt-5.5"))
    p.add_argument("--reasoning-effort", default=os.environ.get("CODEX_REASONING_EFFORT", "default"))
    p.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    p.add_argument("--qwen-base-url", default=os.environ.get("QWEN_BASE_URL", DEFAULT_QWEN_BASE_URL))
    p.add_argument("--qwen-model", default=os.environ.get("QWEN_MODEL", DEFAULT_QWEN_MODEL))
    p.add_argument("--qwen-enable-thinking", action="store_true", default=env_bool("QWEN_ENABLE_THINKING", False))
    p.add_argument("--qwen-max-tokens", type=int, default=int(os.environ.get("QWEN_MAX_TOKENS", "1200")))
    p.add_argument("--qwen-temperature", type=float, default=float(os.environ.get("QWEN_TEMPERATURE", "0.0")))
    p.add_argument("--asset-ids", default="")
    p.add_argument("--asset-id-file", default="")
    p.add_argument("--skip-asset-ids", default=os.environ.get("SKIP_ASSET_IDS", ""))
    p.add_argument("--skip-asset-id-file", default=os.environ.get("SKIP_ASSET_ID_FILE", ""))
    p.add_argument("--limit", type=int)
    p.add_argument("--postgres-url", default=os.environ.get("POSTGRES_URL", DEFAULT_POSTGRES_URL))
    p.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", DEFAULT_QDRANT_URL))
    p.add_argument("--qdrant-collection", default=os.environ.get("QDRANT_COLLECTION", DEFAULT_COLLECTION))
    p.add_argument("--no-db-sync", action="store_true")
    p.add_argument("--skip-service-checks", action="store_true")
    p.add_argument("--mcp-port", type=int, default=int(os.environ.get("MCP_PORT", "55571")))
    p.add_argument("--ue-shotdir", default=os.environ.get("UE_SHOTDIR", ""))
    p.add_argument("--ue-project", default=os.environ.get("UE_PROJECT", ""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run or resume the full indexing workflow")
    add_common_args(run_p)
    run_p.add_argument("--run-dir", default="")
    run_p.add_argument("--force", action="store_true")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--no-initial-sync", action="store_true")
    run_p.add_argument("--allow-db-offline", action="store_true")
    run_p.add_argument("--n-views", type=int, default=int(os.environ.get("N_VIEWS", "8")))
    run_p.add_argument("--min-views", type=int, default=int(os.environ.get("MIN_VIEWS", "4")))
    run_p.add_argument("--res", type=int, default=int(os.environ.get("RES", "1024")))
    run_p.add_argument("--codex-timeout", type=int, default=int(os.environ.get("CODEX_TIMEOUT", "900")))
    run_p.add_argument("--qwen-timeout", type=int, default=int(os.environ.get("QWEN_TIMEOUT", "240")))
    run_p.add_argument("--db-sync-timeout", type=int, default=int(os.environ.get("DB_SYNC_TIMEOUT", "7200")))
    run_p.add_argument("--category-index-timeout", type=int, default=int(os.environ.get("CATEGORY_INDEX_TIMEOUT", "1200")))
    run_p.add_argument("--sync-every-assets", type=int, default=int(os.environ.get("SYNC_EVERY_ASSETS", "50")))
    run_p.add_argument("--sync-min-seconds", type=int, default=int(os.environ.get("SYNC_MIN_SECONDS", "1800")))
    run_p.add_argument("--db-commit-every", type=int, default=int(os.environ.get("DB_COMMIT_EVERY", "50")))
    run_p.add_argument("--embed-batch-size", type=int, default=int(os.environ.get("EMBED_BATCH_SIZE", "32")))
    run_p.add_argument("--max-consecutive-failures", type=int, default=int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "5")))
    run_p.add_argument("--max-consecutive-codex-failures", type=int, default=int(os.environ.get("MAX_CONSECUTIVE_CODEX_FAILURES", "3")))
    run_p.add_argument("--max-consecutive-vlm-failures", type=int, default=int(os.environ.get("MAX_CONSECUTIVE_VLM_FAILURES", os.environ.get("MAX_CONSECUTIVE_CODEX_FAILURES", "3"))))
    run_p.add_argument("--max-consecutive-phase-failures", type=int, default=int(os.environ.get("MAX_CONSECUTIVE_PHASE_FAILURES", "5")))

    check_p = sub.add_parser("check", help="Check config, counts, and optionally services")
    add_common_args(check_p)

    status_p = sub.add_parser("status", help="Show latest or specified run status")
    status_p.add_argument("--asset-db-dir", default=os.environ.get("ASSET_DB_DIR", str(DEFAULT_ASSET_DB_DIR)))
    status_p.add_argument("--run-dir", default="")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.cmd == "run":
        return run_index(args)
    if args.cmd == "check":
        return check_command(args)
    if args.cmd == "status":
        return status_command(args)
    raise RuntimeError(f"unknown command {args.cmd}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
