#!/usr/bin/env python3
"""Asset indexer: for each asset in a manifest file, spawns it in Unreal Engine,
measures geometry (bounds, materials, LODs), renders 8 orbit views, then calls a
Codex/GPT VLM with the rendered images and schema-enforced JSON output to produce
a full semantic asset record. Writes catalog/<category>/<asset_id>.json + renders.

Resumable: skips assets whose catalog JSON already exists. Runs one asset at a time.

Required env vars:
  ASSET_DB_DIR   — root of the asset DB; catalog/, renders/, schema/ will be created here
  UE_SHOTDIR     — where UE writes HighResShots (or set UE_PROJECT, see below)

Optional env vars:
  UE_PROJECT     — UE project root; auto-derives UE_SHOTDIR as
                   <UE_PROJECT>/Saved/Screenshots/LinuxEditor if UE_SHOTDIR is not set
  MCP_PORT       — TCP port the UE MCP server listens on (default: 55571)
  CODEX_MODEL    — VLM model for captioning (default: gpt-5.5)
  MANIFEST       — path to the asset manifest JSON (default: $ASSET_DB_DIR/manifest.json)
  VLM_SCHEMA     — path to the VLM output schema JSON
                   (default: $ASSET_DB_DIR/schema/vlm_output_schema.json)
  N_VIEWS        — orbit views to render per asset (default: 8)
  RES            — render resolution in pixels, square (default: 1024)

Manifest format: {"schema_version":"1.0","assets":[
  {"asset_id":"<id>","ue_name":"<name>","ue_path":"/Game/...","asset_type":"Blueprint|StaticMesh","source_pack":"<pack>"}
]}

After indexing, run build_category_index.py to regenerate category_index.json.
"""
import socket, json, os, re, time, glob, shutil, subprocess, sys

# ── config from env ──────────────────────────────────────────────────────────
def _require(var):
    v = os.environ.get(var, "").strip()
    if not v:
        print(f"ERROR: ${var} is required but not set.", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    return v

ASSET_DB_DIR = _require("ASSET_DB_DIR")
MCP_PORT     = int(os.environ.get("MCP_PORT", "55571"))
CODEX_MODEL  = os.environ.get("CODEX_MODEL", "gpt-5.5")
MANIFEST     = os.environ.get("MANIFEST",   f"{ASSET_DB_DIR}/manifest.json")
VLM_SCHEMA   = os.environ.get("VLM_SCHEMA", f"{ASSET_DB_DIR}/schema/vlm_output_schema.json")
N_VIEWS      = int(os.environ.get("N_VIEWS", "8"))
RES          = int(os.environ.get("RES", "1024"))

# Derive UE_SHOTDIR from UE_PROJECT if not explicitly set
_ue_shotdir = os.environ.get("UE_SHOTDIR", "").strip()
if not _ue_shotdir:
    _ue_project = os.environ.get("UE_PROJECT", "").strip()
    if not _ue_project:
        print("ERROR: set either $UE_SHOTDIR or $UE_PROJECT so the indexer can find rendered screenshots.", file=sys.stderr)
        sys.exit(1)
    _ue_shotdir = os.path.join(_ue_project, "Saved", "Screenshots", "LinuxEditor")
UE_SHOTDIR = _ue_shotdir

RENDERS = f"{ASSET_DB_DIR}/renders"
CATALOG = f"{ASSET_DB_DIR}/catalog"

# ── MCP socket helper ─────────────────────────────────────────────────────────
def ue(script, t=120):
    try:
        s = socket.create_connection(("127.0.0.1", MCP_PORT), timeout=t)
        s.sendall((json.dumps({"type": "execute_python_script", "params": {"script": script}}) + "\n").encode())
        s.settimeout(t); b = b""
        while True:
            d = s.recv(65536)
            if not d: break
            b += d
            try: r = json.loads(b.decode()); break
            except: pass
        s.close(); return r
    except Exception as e:
        return {"error": str(e)}

def logs(r): return (r or {}).get("result", {}).get("python_logs", []) or []
def grab(r, tag):
    for l in logs(r):
        if tag in l:
            try: return json.loads(l.split(tag, 1)[1])
            except: return l.split(tag, 1)[1]
    return None

# ── one-time studio lighting + neutral ground (catalog backdrop) ──────────────
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

INFRA = ("DB_Sun", "DB_Sky", "DB_Atmo", "DB_Ground", "DB_PPV")

# ── spawn + measure geometry ──────────────────────────────────────────────────
def spawn_and_measure(asset):
    path = asset["ue_path"]; atype = asset["asset_type"]
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
    print("SPAWN_FAIL");
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
        print("NO_GEOMETRY");
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
''' % (INFRA, path, atype)
    r = ue(script, 90)
    if any("SPAWN_FAIL" in l or "SPAWNERR" in l for l in logs(r)) and not any("FACTS:" in l for l in logs(r)):
        return None
    return grab(r, "FACTS:")

# ── render N orbit views ──────────────────────────────────────────────────────
def render_views(asset_id, facts):
    outdir = f"{RENDERS}/{asset_id}"
    os.makedirs(outdir, exist_ok=True)
    cx, cy, cz = facts["center"]
    ex, ey, ez = facts["extent"]
    radius = max((ex*ex + ey*ey + ez*ez)**0.5, 50.0)
    dist = radius * 2.6
    elev_deg = 20.0
    import math
    paths = []
    for i in range(N_VIEWS):
        az = math.radians(i * (360.0 / N_VIEWS))
        camx = cx + dist * math.cos(az)
        camy = cy + dist * math.sin(az)
        camz = cz + dist * math.sin(math.radians(elev_deg))
        dx, dy, dz2 = cx - camx, cy - camy, cz - camz
        yaw = math.degrees(math.atan2(dy, dx))
        pitch = math.degrees(math.atan2(dz2, math.sqrt(dx*dx + dy*dy)))
        before = time.time() - 1
        sc = ("import unreal\n"
              f"sub=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\n"
              f"sub.set_level_viewport_camera_info(unreal.Vector({camx:.1f},{camy:.1f},{camz:.1f}),unreal.Rotator(pitch={pitch:.2f},yaw={yaw:.2f},roll=0.0))\n"
              f"unreal.SystemLibrary.execute_console_command(None,'HighResShot {RES}x{RES}')\nprint('SHOT')")
        ue(sc, 40)
        got = None
        for _ in range(20):
            time.sleep(1.0)
            pngs = sorted(glob.glob(UE_SHOTDIR + "/*.png"), key=os.path.getmtime)
            if pngs and os.path.getmtime(pngs[-1]) > before:
                got = pngs[-1]; break
        if got:
            dest = f"{outdir}/view_{i:02d}.png"
            shutil.copy(got, dest); paths.append(dest)
    return paths

# ── VLM captioning via Codex CLI ──────────────────────────────────────────────
def run_vlm(asset, facts, view_paths):
    dims = facts.get("dimensions_m", {})
    prompt = (
        "You are cataloging a single 3D asset for a general, multi-genre scene-generation asset database "
        "(scenes span many settings: modern city/street, industrial/logistics, suburban/residential, retail, "
        "rural/nature, harbor, medieval, fantasy/gothic, ancient temple, middle-eastern, east-asian, winter, sci-fi, indoor). "
        f"You are shown {len(view_paths)} rendered views of ONE object from different angles (orbit). "
        "Describe ONLY this object (ignore the gray ground plane and sky). "
        f"Measured size in meters (from the engine): width {dims.get('width')}, depth {dims.get('depth')}, height {dims.get('height')}. "
        f"Asset name hint: '{asset['ue_name']}'. Source pack: '{asset['source_pack']}'. "
        "Fill every field. Pick the single best-fit category from the allowed enum. "
        "For 'setting': choose the single primary thematic setting/genre this asset most reads as, from the allowed enum "
        "(modern_urban, industrial, suburban_residential, commercial_retail, nature_rural, coastal_harbor, medieval, "
        "fantasy_gothic, ancient_temple, middle_eastern, east_asian, winter, sci_fi, indoor, generic). "
        "Use 'generic' ONLY for plain, style-neutral props that fit many settings equally (e.g. a plain crate, a plain barrel, a generic rock)."
    )
    args = ["codex", "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
            "-m", CODEX_MODEL, "--output-schema", VLM_SCHEMA, "-o", "/tmp/vlm_out.json", "-C", "/tmp"]
    for p in view_paths: args += ["-i", p]
    args += ["-"]
    try:
        if os.path.exists("/tmp/vlm_out.json"): os.remove("/tmp/vlm_out.json")
        env = dict(os.environ, NO_COLOR="1")
        subprocess.run(args, input=prompt, text=True, env=env, timeout=300,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists("/tmp/vlm_out.json"):
            return json.loads(open("/tmp/vlm_out.json").read())
    except Exception as e:
        print("   VLM_ERR:", e, flush=True)
    return None

# ── assemble final asset record ───────────────────────────────────────────────
def assemble(asset, facts, vlm, view_paths):
    rel_views = [os.path.relpath(p, ASSET_DB_DIR) for p in view_paths]
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
            "caption_model": CODEX_MODEL,
            "schema_version": "1.0",
        },
    }

def already_done(asset_id):
    return bool(glob.glob(f"{CATALOG}/*/{asset_id}.json"))

def main():
    man = json.load(open(MANIFEST))
    assets = man["assets"]
    print(f"manifest: {len(assets)} assets  db={ASSET_DB_DIR}  port={MCP_PORT}  model={CODEX_MODEL}", flush=True)
    ue(SETUP_STAGE, 60)
    done = 0; failed = 0; consec_fail = 0
    for idx, asset in enumerate(assets):
        if consec_fail >= 5:
            if not any("UEOK" in str(l) for l in logs(ue("import unreal;print('UEOK')", 20))):
                print("ABORT: UE unresponsive after 5 fails — relaunch UE + rerun to resume.", flush=True); break
            else:
                print("   (5 fails but UE alive — continuing, likely bad assets)", flush=True); consec_fail = 0
        aid = asset["asset_id"]
        if already_done(aid):
            print(f"[{idx+1}/{len(assets)}] SKIP {aid} (done)", flush=True); continue
        print(f"[{idx+1}/{len(assets)}] {aid} ({asset['source_pack']}/{asset['ue_name']})", flush=True)
        facts = spawn_and_measure(asset)
        if not facts:
            print("   spawn/measure FAILED", flush=True); failed += 1; consec_fail += 1; continue
        views = render_views(aid, facts)
        if len(views) < 4:
            print(f"   only {len(views)} views rendered — skipping", flush=True); failed += 1; consec_fail += 1; continue
        vlm = run_vlm(asset, facts, views)
        if not vlm:
            print("   VLM FAILED", flush=True); failed += 1; continue
        rec = assemble(asset, facts, vlm, views)
        cat = rec["identity"]["category"] or "uncategorized"
        os.makedirs(f"{CATALOG}/{cat}", exist_ok=True)
        json.dump(rec, open(f"{CATALOG}/{cat}/{aid}.json", "w"), indent=2)
        done += 1; consec_fail = 0
        print(f"   OK -> {cat}/{aid}.json | {rec['identity']['name']} | dims={facts['dimensions_m']} | views={len(views)}", flush=True)
    print(f"\nDONE: indexed={done} failed={failed}", flush=True)
    print("Run build_category_index.py to regenerate category_index.json.", flush=True)

if __name__ == "__main__":
    main()
