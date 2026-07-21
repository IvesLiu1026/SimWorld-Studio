#!/usr/bin/env python3
"""Inspect the seven revision-pinned mmg_040 scene candidates in disposable UE."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import socket
import stat
from pathlib import Path
from typing import Any, Mapping


CANDIDATE_SCHEMA = "vista-mmg040-candidate-sources/v1"
CANDIDATE_SHA256 = "35aaf9741650d028f8f11e303035ab10168ef78d5244411c64d9f37db7cf9f4e"
MANIFEST_SCHEMA = "simworld-ue-object-manifest/v2"
BOOTSTRAP_SCHEMA = "simworld-ue-asset-bootstrap-receipt/v1"
OBSERVATION_SCHEMA = "vista-mmg040-live-object-observation/v1"
RECEIPT_SCHEMA = "vista-mmg040-live-object-observation-receipt/v1"
PROJECT_NAME = "gym_citynav"
PROJECT_REVISION = "source-patch:51426e97354477dca1635217455e644e9ca98976"
CONTENT_REVISION = "sha256:806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f"
ENGINE_PREFIX = "5.3.2-"
RESULT_PREFIX = "VISTA_MMG040_OBJECT_OBSERVATION:"
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 7_680
EXPECTED_CANDIDATE_COUNT = 7


class InspectionError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise InspectionError("document is not canonical JSON data") from error


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InspectionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_symlink_components(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise InspectionError(f"{label} path must be absolute")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise InspectionError(f"{label} path must not contain symlinks")
    except InspectionError:
        raise
    except OSError as error:
        raise InspectionError(f"{label} is unavailable") from error


def read_json(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    reject_symlink_components(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InspectionError(f"{label} is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise InspectionError(f"{label} must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as opened:
            raw = opened.read(MAX_INPUT_BYTES + 1)
    finally:
        os.close(descriptor)
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise InspectionError(f"{label} size is invalid")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise InspectionError(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise InspectionError(f"{label} must be a JSON object")
    return raw, value


def object_path_from_locator(locator: Any) -> tuple[str, str]:
    if not isinstance(locator, str) or not locator.endswith(".uasset"):
        raise InspectionError("candidate filesystem locator is invalid")
    relative = locator.removesuffix(".uasset")
    parts = relative.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise InspectionError("candidate filesystem locator is not canonical")
    name = parts[-1]
    return f"/Game/{relative}.{name}", name


def require_source_binding(binding: Any) -> None:
    if not isinstance(binding, dict):
        raise InspectionError("object manifest source binding is missing")
    project = binding.get("project")
    content = binding.get("content")
    archive = binding.get("archive")
    if (
        not isinstance(project, dict)
        or project.get("name") != PROJECT_NAME
        or project.get("revision") != PROJECT_REVISION
        or not str(project.get("engine_version", "")).startswith(ENGINE_PREFIX)
        or not isinstance(content, dict)
        or content.get("revision") != CONTENT_REVISION
        or content.get("mount_point") != "/Game"
        or not isinstance(archive, dict)
        or archive.get("verified") is not True
        or f"sha256:{archive.get('sha256', '')}" != CONTENT_REVISION
    ):
        raise InspectionError("object manifest source binding is not the verified official archive")


def build_plan(
    candidate_raw: bytes,
    candidate: Mapping[str, Any],
    manifest_raw: bytes,
    manifest: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if sha256_bytes(candidate_raw) != CANDIDATE_SHA256:
        raise InspectionError("candidate source byte pin does not match")
    if candidate.get("schema") != CANDIDATE_SCHEMA or candidate.get("sample_id") != "mmg_040":
        raise InspectionError("candidate source identity is invalid")
    rows = candidate.get("scene_object_candidates")
    if not isinstance(rows, list) or len(rows) != EXPECTED_CANDIDATE_COUNT:
        raise InspectionError("candidate source must contain exactly seven scene objects")

    assets = manifest.get("assets")
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or not isinstance(assets, list)
        or len(assets) > 10_000
        or manifest.get("count") != len(assets)
    ):
        raise InspectionError("object manifest identity or count is invalid")
    require_source_binding(manifest.get("source_binding"))
    if (
        bootstrap.get("schema") != BOOTSTRAP_SCHEMA
        or bootstrap.get("bundle_complete") is not True
        or bootstrap.get("snapshot_complete") is not False
        or bootstrap.get("source_binding") != manifest.get("source_binding")
    ):
        raise InspectionError("bootstrap receipt is incomplete or mismatched")
    manifest_file = (bootstrap.get("files") or {}).get("object-manifest.json")
    if not isinstance(manifest_file, dict) or manifest_file.get("sha256") != sha256_bytes(manifest_raw):
        raise InspectionError("bootstrap receipt does not bind the object manifest bytes")

    by_path: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("ue_path"), str):
            raise InspectionError("object manifest contains an invalid asset row")
        if asset["ue_path"] in by_path:
            raise InspectionError("object manifest contains duplicate UE paths")
        by_path[asset["ue_path"]] = asset

    plan = []
    candidate_ids: set[str] = set()
    for ordinal, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InspectionError("candidate row is invalid")
        candidate_id = row.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id.startswith("scene_")
            or candidate_id in candidate_ids
            or row.get("source_scope_id") != "official_minimal_content"
            or row.get("locator_kind") != "content_relative_file"
        ):
            raise InspectionError("candidate row identity is invalid")
        candidate_ids.add(candidate_id)
        ue_path, ue_name = object_path_from_locator(row.get("filesystem_locator"))
        asset = by_path.get(ue_path)
        if (
            not isinstance(asset, dict)
            or asset.get("ue_name") != ue_name
            or asset.get("asset_type") not in {"Blueprint", "StaticMesh"}
            or not isinstance(asset.get("asset_id"), str)
        ):
            raise InspectionError(f"AssetRegistry did not resolve pinned candidate {candidate_id}")
        plan.append(
            {
                "ordinal": ordinal,
                "candidate_id": candidate_id,
                "asset_id": asset["asset_id"],
                "asset_type": asset["asset_type"],
                "ue_name": ue_name,
                "ue_path": ue_path,
            }
        )
    return plan


def build_ue_script(item: dict[str, Any]) -> str:
    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return f'''import json,unreal
I=json.loads({encoded!r}); S=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
if S is None: raise RuntimeError("editor actor subsystem unavailable")
K=lambda x:str(x.get_path_name()); A=lambda:{{K(x):x for x in S.get_all_level_actors()}}
B=A(); SEL=list(S.get_selected_level_actors()); SEK={{K(x) for x in SEL}}; R=dict(I); R.update({{"load_succeeded":False,"spawn_succeeded":False,"observed_asset_class":None,"generated_class_path":None,"actor_class_path":None,"actor_identity":None,"bounds_cm":None,"introduced_actors":[],"collision":None,"material_slots":[],"cleanup_verified":False,"error":None}}); root=None
try:
 a=unreal.load_asset(I["ue_path"])
 if a is None: raise RuntimeError("asset load returned none")
 R["load_succeeded"]=True; R["observed_asset_class"]=K(a.get_class())
 p=unreal.Vector(float(I["ordinal"]*1000),0,500); q=unreal.Rotator(pitch=0,yaw=0,roll=0)
 if I["asset_type"]=="Blueprint":
  g=a.generated_class() if hasattr(a,"generated_class") else None
  if g is None: raise RuntimeError("blueprint generated class unavailable")
  R["generated_class_path"]=K(g); root=S.spawn_actor_from_class(g,p,q,transient=True)
 else:
  if not isinstance(a,unreal.StaticMesh): raise RuntimeError("static mesh class mismatch")
  root=S.spawn_actor_from_object(a,p,q,transient=True)
 if root is None: raise RuntimeError("actor spawn returned none")
 root.set_actor_label("VISTA_MMG040_INSPECT_"+str(I["ordinal"])); R["spawn_succeeded"]=True; R["actor_identity"]=K(root); R["actor_class_path"]=K(root.get_class())
 N=[x for k,x in A().items() if k not in B]
 if all(K(x)!=K(root) for x in N):N.insert(0,root)
 if len(N)>32: raise RuntimeError("introduced actor bound exceeded")
 R["introduced_actors"]=[{{"path":K(x),"class_path":K(x.get_class())}} for x in N]
 def bd(c):
  o,e=root.get_actor_bounds(c,True); return {{"origin_xyz":[round(o.x,4),round(o.y,4),round(o.z,4)],"extent_xyz":[round(e.x,4),round(e.y,4),round(e.z,4)],"nonzero":bool(e.x>.01 and e.y>.01 and e.z>.01)}}
 R["bounds_cm"]={{"visual":bd(False),"colliding":bd(True)}}; C=[]; SM={{}}
 for x in N:
  for c in x.get_components_by_class(unreal.PrimitiveComponent):
   try: en=bool(c.is_collision_enabled())
   except Exception: en=None
   z={{"actor":K(x),"component":str(c.get_name()),"class_path":K(c.get_class()),"enabled":en}}
   for n,f in (("mode","get_collision_enabled"),("profile_name","get_collision_profile_name")):
    try:z[n]=str(getattr(c,f)())
    except Exception:z[n]=None
   try:z["mobility"]=str(c.get_editor_property("mobility"))
   except Exception:z["mobility"]=None
   try:z["simulate_physics"]=bool(c.is_simulating_physics())
   except Exception:z["simulate_physics"]=None
   try:z["generate_overlap_events"]=bool(c.get_editor_property("generate_overlap_events"))
   except Exception:z["generate_overlap_events"]=None
   C.append(z)
   try:m=c.get_editor_property("static_mesh")
   except Exception:m=None
   if m is not None:SM[K(m)]=m
 R["collision"]={{"components":C,"static_meshes":[]}}
 for mp,m in sorted(SM.items()):
  z={{"mesh_path":mp,"collision_trace_flag":None,"complex_collision_mesh":None,"simple_shape_counts":None}}
  try:
   b=m.get_editor_property("body_setup"); z["collision_trace_flag"]=str(b.get_editor_property("collision_trace_flag")); ag=b.get_editor_property("agg_geom"); d={{}}
   for f in ("box_elems","sphere_elems","sphyl_elems","convex_elems","tapered_capsule_elems","level_set_elems","skinned_level_set_elems"):
    try:d[f]=len(ag.get_editor_property(f))
    except Exception:d[f]=None
   z["simple_shape_counts"]=d
   try:z["complex_collision_mesh"]=K(m.get_editor_property("complex_collision_mesh")) if m.get_editor_property("complex_collision_mesh") else None
   except Exception:pass
  except Exception:pass
  R["collision"]["static_meshes"].append(z)
 for x in N:
  for c in x.get_components_by_class(unreal.MeshComponent):
   try:n=int(c.get_num_materials())
   except Exception:n=0
   if n<0 or n>64: raise RuntimeError("component material slot bound exceeded")
   for j in range(max(0,n)):
    if len(R["material_slots"])>=64: raise RuntimeError("asset material slot bound exceeded")
    m=c.get_material(j); mp=K(m) if m else None; base=m.get_base_material() if m else None; bt=[]; pv=[]; te=[]
    if base is not None:
     try:bt=sorted({{K(t) for t in unreal.MaterialEditingLibrary.get_used_textures(base) if t}})
     except Exception:te.append("base_used_textures_unavailable")
     if len(bt)>128: raise RuntimeError("base texture dependency bound exceeded")
    if m is not None:
     try:
      names=list(unreal.MaterialEditingLibrary.get_texture_parameter_names(m))
     except Exception:names=[]; te.append("texture_parameter_names_unavailable")
     if len(names)>128: raise RuntimeError("texture parameter bound exceeded")
     try:
      for pn in names:
       try:v=m.get_texture_parameter_value(pn) if hasattr(m,"get_texture_parameter_value") else unreal.MaterialEditingLibrary.get_material_default_texture_parameter_value(base,pn)
       except Exception:v=None; te.append("texture_parameter_value_unavailable")
       pv.append({{"name":str(pn),"texture_path":K(v) if v else None}})
     except Exception:pass
    R["material_slots"].append({{"actor":K(x),"component":str(c.get_name()),"slot_index":j,"material_path":mp,"material_class_path":K(m.get_class()) if m else None,"base_material_path":K(base) if base else None,"base_used_texture_paths":bt,"resolved_texture_parameters":pv,"texture_observation_complete":len(te)==0,"texture_observation_errors":sorted(set(te))}})
except Exception as e:R["error"]=str(e)[:512]
finally:
 for k,x in list(A().items())[::-1]:
  if k not in B and (root is None or k!=K(root)):
   try:S.destroy_actor(x)
   except Exception:pass
 if root is not None:
  try:S.destroy_actor(root)
  except Exception:pass
 for k,x in list(A().items())[::-1]:
  if k not in B:
   try:S.destroy_actor(x)
   except Exception:pass
 try:unreal.SystemLibrary.collect_garbage()
 except Exception:pass
 try:S.set_selected_level_actors(SEL)
 except Exception:pass
 R["cleanup_verified"]=set(A())==set(B) and {{K(x) for x in S.get_selected_level_actors()}}==SEK
O={{"project_name":str(unreal.Paths.get_project_file_path()).replace("\\\\","/").rsplit("/",1)[-1].rsplit(".",1)[0],"engine_version":str(unreal.SystemLibrary.get_engine_version()),"record":R}}
print({RESULT_PREFIX!r}+json.dumps(O,sort_keys=True,separators=(",",":"),allow_nan=False))
'''


def extract_logs(response: Any) -> list[str]:
    if not isinstance(response, dict) or response.get("status") not in {None, "success"}:
        raise InspectionError("UE bridge reported an unsuccessful request")
    candidates = [response.get("python_logs")]
    result = response.get("result")
    if isinstance(result, dict):
        candidates.append(result.get("python_logs"))
        nested = result.get("result")
        if isinstance(nested, dict):
            candidates.append(nested.get("python_logs"))
    for candidate in candidates:
        if isinstance(candidate, list) and all(isinstance(line, str) for line in candidate):
            return candidate
    return []


def query_bridge(host: str, port: int, timeout: int, script: str) -> dict[str, Any]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise InspectionError("bridge host must be a numeric loopback address") from error
    if not address.is_loopback or not 1 <= port <= 65535 or not 1 <= timeout <= 600:
        raise InspectionError("bridge endpoint or timeout is invalid")
    payload = {"type": "execute_python_script", "params": {"script": script}}
    frame = canonical_bytes(payload) + b"\n"
    if len(frame) >= MAX_REQUEST_BYTES:
        raise InspectionError("fixed UE request exceeds the legacy bridge framing bound")
    try:
        with socket.create_connection((host, port), timeout=min(timeout, 10)) as connection:
            connection.settimeout(timeout)
            connection.sendall(frame)
            response = bytearray()
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > MAX_RESPONSE_BYTES:
                    raise InspectionError("UE bridge response exceeds the fixed byte bound")
                try:
                    decoded = json.loads(response.decode("utf-8"), object_pairs_hook=reject_duplicate_pairs)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(decoded, dict):
                    raise InspectionError("UE bridge response must be an object")
                return decoded
    except InspectionError:
        raise
    except OSError as error:
        raise InspectionError(f"UE bridge request failed: {error}") from error
    raise InspectionError("UE bridge closed without a complete response")


def _valid_bounds(bounds: Any) -> bool:
    if not isinstance(bounds, dict) or set(bounds) != {"visual", "colliding"}:
        return False
    for name in ("visual", "colliding"):
        row = bounds.get(name)
        if not isinstance(row, dict) or not isinstance(row.get("nonzero"), bool):
            return False
        values = (row.get("origin_xyz") or []) + (row.get("extent_xyz") or [])
        if len(values) != 6 or any(
            not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
            for value in values
        ):
            return False
    return True


def extract_record(response: Any, expected: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    matches = []
    for line in extract_logs(response):
        if RESULT_PREFIX in line:
            matches.append(line.split(RESULT_PREFIX, 1)[1])
    if len(matches) != 1:
        raise InspectionError("UE response did not contain exactly one inspection marker")
    try:
        envelope = json.loads(matches[0], object_pairs_hook=reject_duplicate_pairs)
    except (json.JSONDecodeError, RecursionError) as error:
        raise InspectionError("UE inspection marker is malformed") from error
    if (
        not isinstance(envelope, dict)
        or envelope.get("project_name") != PROJECT_NAME
        or not str(envelope.get("engine_version", "")).startswith(ENGINE_PREFIX)
        or not isinstance(envelope.get("record"), dict)
    ):
        raise InspectionError("UE inspection envelope identity is invalid")
    actual = envelope["record"]
    if any(actual.get(key) != expected[key] for key in expected):
        raise InspectionError("UE inspection result does not match the pinned plan")
    if actual.get("cleanup_verified") is not True:
        raise InspectionError("UE inspection did not restore the disposable scene")
    expected_asset_class = f"/Script/Engine.{expected['asset_type']}"
    expected_actor_class = (
        f"{expected['ue_path']}_C"
        if expected["asset_type"] == "Blueprint"
        else "/Script/Engine.StaticMeshActor"
    )
    expected_generated_class = expected_actor_class if expected["asset_type"] == "Blueprint" else None
    material_slots = actual.get("material_slots")
    collision = actual.get("collision")
    class_matches = (
        actual.get("observed_asset_class") == expected_asset_class
        and actual.get("actor_class_path") == expected_actor_class
        and actual.get("generated_class_path") == expected_generated_class
    )
    bounds_complete = _valid_bounds(actual.get("bounds_cm"))
    collision_complete = (
        isinstance(collision, dict)
        and isinstance(collision.get("components"), list)
        and isinstance(collision.get("static_meshes"), list)
        and len(collision["components"]) > 0
        and all(
            isinstance(row, dict)
            and isinstance(row.get("enabled"), bool)
            and all(row.get(key) is not None for key in (
                "mode", "profile_name", "mobility", "simulate_physics", "generate_overlap_events"
            ))
            for row in collision["components"]
        )
        and all(
            isinstance(row, dict)
            and isinstance(row.get("collision_trace_flag"), str)
            and isinstance(row.get("simple_shape_counts"), dict)
            and set(row["simple_shape_counts"]) == {
                "box_elems", "sphere_elems", "sphyl_elems", "convex_elems",
                "tapered_capsule_elems", "level_set_elems", "skinned_level_set_elems",
            }
            and all(isinstance(value, int) and not isinstance(value, bool) for value in row["simple_shape_counts"].values())
            for row in collision["static_meshes"]
        )
    )
    materials_complete = (
        isinstance(material_slots, list)
        and 0 < len(material_slots) <= 64
        and all(
            isinstance(slot, dict)
            and isinstance(slot.get("material_path"), str)
            and slot["material_path"].startswith("/")
            and isinstance(slot.get("base_used_texture_paths"), list)
            and isinstance(slot.get("resolved_texture_parameters"), list)
            and slot.get("texture_observation_complete") is True
            and slot.get("texture_observation_errors") == []
            for slot in material_slots
        )
    )
    actual["inspection_complete"] = bool(
        actual.get("load_succeeded") is True
        and actual.get("spawn_succeeded") is True
        and actual.get("error") is None
        and class_matches
        and bounds_complete
        and actual["bounds_cm"]["visual"]["nonzero"] is True
        and collision_complete
        and materials_complete
    )
    texture_paths = set()
    project_materials = 0
    if isinstance(material_slots, list):
        for slot in material_slots:
            material_path = slot.get("material_path") if isinstance(slot, dict) else None
            if isinstance(material_path, str) and material_path.startswith("/Game/"):
                project_materials += 1
            if isinstance(slot, dict):
                texture_paths.update(
                    value for value in slot.get("base_used_texture_paths", [])
                    if isinstance(value, str) and value.startswith("/")
                )
                texture_paths.update(
                    value.get("texture_path")
                    for value in slot.get("resolved_texture_parameters", [])
                    if isinstance(value, dict)
                    and isinstance(value.get("texture_path"), str)
                    and value["texture_path"].startswith("/")
                )
    actual["suitability_observation"] = {
        "collision_enabled": bool(
            isinstance(collision, dict)
            and any(row.get("enabled") is True for row in collision.get("components", []))
        ),
        "project_material_slot_count": project_materials,
        "observed_texture_dependency_count": len(texture_paths),
        "observed_texture_paths_are_conservative_union": True,
    }
    return envelope["engine_version"], actual


def write_exclusive(path: Path, value: Any) -> dict[str, Any]:
    raw = canonical_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return {"bytes": len(raw), "sha256": sha256_bytes(raw)}


def publish(output_dir: Path, observation: dict[str, Any], bindings: dict[str, Any]) -> Path:
    if not output_dir.is_absolute():
        raise InspectionError("output directory must be a new absolute path")
    parent = output_dir.parent
    reject_symlink_components(parent, "output parent")
    try:
        os.lstat(output_dir)
    except FileNotFoundError:
        pass
    else:
        raise InspectionError("output directory must be a new absolute path")
    parent_stat = os.lstat(parent)
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.geteuid() or parent_stat.st_mode & 0o077:
        raise InspectionError("output parent must be private and owner-controlled")
    output_dir.mkdir(mode=0o700)
    observation_file = write_exclusive(output_dir / "observation.json", observation)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "bindings": bindings,
        "observation": observation_file,
        "record_count": len(observation["records"]),
        "passed": observation.get("passed") is True,
        "cleanup_verified": observation.get("cleanup_verified") is True,
    }
    write_exclusive(output_dir / "receipt.json", receipt)
    return output_dir / "receipt.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-source", type=Path, required=True)
    parser.add_argument("--object-manifest", type=Path, required=True)
    parser.add_argument("--bootstrap-receipt", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live-query", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candidate_raw, candidate = read_json(args.candidate_source, "candidate source")
    manifest_raw, manifest = read_json(args.object_manifest, "object manifest")
    bootstrap_raw, bootstrap = read_json(args.bootstrap_receipt, "bootstrap receipt")
    plan = build_plan(candidate_raw, candidate, manifest_raw, manifest, bootstrap)
    bindings = {
        "candidate_source_sha256": sha256_bytes(candidate_raw),
        "object_manifest_sha256": sha256_bytes(manifest_raw),
        "bootstrap_receipt_sha256": sha256_bytes(bootstrap_raw),
        "bootstrap_bundle_revision": bootstrap.get("bundle_revision"),
        "project_revision": PROJECT_REVISION,
        "content_revision": CONTENT_REVISION,
    }
    if args.dry_run:
        print(json.dumps({"bindings": bindings, "plan": plan}, indent=2, ensure_ascii=False))
        return
    if not args.host or args.port is None or args.output_dir is None:
        raise InspectionError("live query requires explicit host, port, and output directory")
    records = []
    engine_version = None
    for item in plan:
        response = query_bridge(args.host, args.port, args.timeout, build_ue_script(item))
        observed_engine, record = extract_record(response, item)
        if engine_version is None:
            engine_version = observed_engine
        elif engine_version != observed_engine:
            raise InspectionError("UE engine identity changed during inspection")
        records.append(record)
    observation = {
        "schema": OBSERVATION_SCHEMA,
        "project_name": PROJECT_NAME,
        "engine_version": engine_version,
        "content_revision": CONTENT_REVISION,
        "records": records,
        "cleanup_verified": all(row["cleanup_verified"] is True for row in records),
        "passed": all(row["inspection_complete"] is True for row in records),
    }
    receipt = publish(args.output_dir, observation, bindings)
    print(f"wrote mmg_040 live object observation: {receipt}")
    print(json.dumps({
        "passed": observation["passed"],
        "record_count": len(observation["records"]),
        "loaded": sum(row["load_succeeded"] is True for row in observation["records"]),
        "spawned": sum(row["spawn_succeeded"] is True for row in observation["records"]),
        "cleanup_verified": observation["cleanup_verified"],
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except InspectionError as error:
        raise SystemExit(f"inspection failed: {error}") from error
