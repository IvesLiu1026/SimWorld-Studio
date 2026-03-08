#!/usr/bin/env node
"use strict";const net=require("net"),fs=require("fs"),path=require("path"),UNREAL_HOST="127.0.0.1",UNREAL_PORT=55559,THUMB_DIR=path.resolve(__dirname,"../../tmp/thumbnails"),ASSETS=JSON.parse(fs.readFileSync(path.join(__dirname,"assets.json"),"utf-8"));fs.mkdirSync(THUMB_DIR,{recursive:!0});const args=process.argv.slice(2);let category=null,startIdx=0,endIdx=1/0;for(let t=0;t<args.length;t++)args[t]==="--category"&&args[t+1]&&(category=args[++t]),args[t]==="--start"&&args[t+1]&&(startIdx=parseInt(args[++t])),args[t]==="--end"&&args[t+1]&&(endIdx=parseInt(args[++t]));function ueCommandOnce(t,o,i=3e4){return new Promise((c,r)=>{const e=new net.Socket,n=setTimeout(()=>{e.destroy(),r(new Error("Timeout"))},i);let a="";e.connect(UNREAL_PORT,UNREAL_HOST,()=>{e.write(JSON.stringify({type:t,params:o})+`
`)}),e.on("data",s=>{if(a+=s.toString(),a.includes(`
`)){clearTimeout(n),e.destroy();try{c(JSON.parse(a.trim()))}catch(l){r(l)}}}),e.on("error",s=>{clearTimeout(n),r(s)})})}function sleep(t){return new Promise(o=>setTimeout(o,t))}async function ueCommand(t,o,i=3e4){for(let r=1;r<=3;r++)try{const e=await ueCommandOnce(t,o,i);return await sleep(200),e}catch(e){if(r<3&&(e.message==="Timeout"||e.code==="ECONNREFUSED"))console.log(`    (retry ${r}/3 after ${e.message}, waiting...)`),await sleep(3e3*r);else throw e}}async function captureAsset(t,o,i){const c=path.join(THUMB_DIR,`${t}.png`);if(fs.existsSync(c))return console.log(`  SKIP ${t} (already exists)`),!0;try{const r=o.replace(/\.[^/]+$/,""),n=(await ueCommand("execute_python_script",{script:`
import unreal
import time

subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

# Clean previous preview actors (keep lighting)
for a in subsys.get_all_level_actors():
    if a.get_actor_label().startswith("ThumbActor_"):
        subsys.destroy_actor(a)

# Spawn the asset \u2014 try load_blueprint_class first, then load_asset
bp_path = "${r}"
bp_class = unreal.EditorAssetLibrary.load_blueprint_class(bp_path)
if bp_class is None:
    # Try with full path including _C suffix
    bp_class = unreal.EditorAssetLibrary.load_blueprint_class("${o}")
if bp_class is None:
    print("SPAWN_FAIL")
else:
    actor = subsys.spawn_actor_from_class(bp_class, unreal.Vector(0, 0, 0))
    if actor is None:
        print("SPAWN_FAIL")
    else:
        actor.set_actor_label("ThumbActor_Preview")

        # Get bounds for camera positioning
        import math
        (origin, extent) = actor.get_actor_bounds(False)
        cx, cy, cz = origin.x, origin.y, origin.z
        ex, ey, ez = extent.x, extent.y, extent.z
        radius = math.sqrt(ex*ex + ey*ey + ez*ez)
        category = "${i}"
        mult = 2.0 if category == "buildings" else 1.5
        min_dist = 800 if category == "buildings" else 300
        dist = max(radius * mult, min_dist)

        # Position camera in front-right, elevated \u2014 looking AT center
        cam_x = cx - dist * 0.7
        cam_y = cy - dist * 0.7
        cam_z = cz + dist * 0.2

        # Compute look-at rotation
        dx = cx - cam_x
        dy = cy - cam_y
        dz = cz - cam_z
        horiz = math.sqrt(dx*dx + dy*dy)
        pitch = math.degrees(math.atan2(dz, horiz))
        yaw = math.degrees(math.atan2(dy, dx))

        ed_subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        ed_subsys.set_level_viewport_camera_info(
            unreal.Vector(cam_x, cam_y, cam_z),
            unreal.Rotator(pitch=pitch, yaw=yaw, roll=0)
        )

        print(f"READY dist={dist}")
`},12e4))?.result?.python_logs||[];return n.some(s=>s.includes("SPAWN_FAIL"))?(console.log(`  FAIL ${t}: spawn failed`),!1):n.some(s=>s.includes("READY"))?(await sleep(1500),(await ueCommand("take_screenshot",{filepath:c},12e4)).status!=="success"?(console.log(`  FAIL ${t}: screenshot failed`),!1):(console.log(`  OK   ${t}`),!0)):(console.log(`  FAIL ${t}: setup failed (${JSON.stringify(n)})`),!1)}catch(r){return console.log(`  ERR  ${t}: ${r.message}`),!1}}async function waitForUE(t=120){for(let o=0;o<t;o+=5)try{return await ueCommandOnce("execute_python_script",{script:'print("PING")'},5e3),!0}catch{process.stdout.write(`  Waiting for UE (${o+5}s)...\r`),await sleep(5e3)}return!1}async function main(){console.log("Asset Thumbnail Generator"),console.log(`Output: ${THUMB_DIR}`),console.log(),console.log("Setting up lighting...");try{await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
for a in subsys.get_all_level_actors():
    if a.get_actor_label().startswith("Arena_Env_") or a.get_actor_label().startswith("ThumbActor_"):
        subsys.destroy_actor(a)
print("Clean OK")
`}),await sleep(300),await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
atmo = subsys.spawn_actor_from_class(unreal.SkyAtmosphere.static_class(), unreal.Vector(0,0,0))
atmo.set_actor_label("Arena_Env_Atmosphere")
print("Atmosphere OK")
`}),await sleep(300),await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
sun = subsys.spawn_actor_from_class(unreal.DirectionalLight.static_class(), unreal.Vector(0,0,500))
sun.set_actor_label("Arena_Env_Sun")
sun.set_actor_rotation(unreal.Rotator(pitch=-45.0, yaw=30.0, roll=0.0), False)
comp = sun.get_component_by_class(unreal.DirectionalLightComponent)
comp.set_intensity(3.0)
comp.set_atmosphere_sun_light(True)
print("Sun OK")
`}),await sleep(300),await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
sky = subsys.spawn_actor_from_class(unreal.SkyLight.static_class(), unreal.Vector(0,0,500))
sky.set_actor_label("Arena_Env_SkyLight")
sc = sky.get_component_by_class(unreal.SkyLightComponent)
sc.set_editor_property("intensity", 1.0)
print("SkyLight OK")
`}),await sleep(300),await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
ground = subsys.spawn_actor_from_class(unreal.StaticMeshActor.static_class(), unreal.Vector(0, 0, -10))
ground.set_actor_label("Arena_Env_Ground")
ground.set_actor_scale3d(unreal.Vector(5000, 5000, 1))
mc = ground.get_component_by_class(unreal.StaticMeshComponent)
mc.set_static_mesh(unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Plane"))
mat = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/BasicShapeMaterial")
if mat:
    mc.set_material(0, mat)
print("Ground OK")
`}),await sleep(300),await ueCommand("execute_python_script",{script:`
import unreal
unreal.SystemLibrary.execute_console_command(None, "r.ViewDistanceScale 100")
unreal.SystemLibrary.execute_console_command(None, "r.ForceLOD 0")
print("ViewDist OK")
`}),console.log("Lighting setup complete.")}catch(a){console.error("Failed to setup lighting:",a.message),process.exit(1)}const t=[],o=category?[category]:Object.keys(ASSETS);for(const a of o){const s=ASSETS[a];if(a==="buildings"&&s.ids)for(const l of s.ids){const _=`BP_Building_${String(l).padStart(2,"0")}`,p=`/Game/CityDatabase/blueprints/${_}.${_}_C`;t.push({id:_,path:p,category:a})}else if(s.items)for(const l of s.items){const u=l.split("/"),p=u[u.length-1].split(".")[0];t.push({id:p,path:l,category:a})}}const i=t.slice(startIdx,endIdx);console.log(`
Capturing ${i.length} assets (of ${t.length} total)...
`);let c=0,r=0,e=0,n=0;for(let a=0;a<i.length;a++){const s=i[a];process.stdout.write(`[${a+1}/${i.length}] `);const l=fs.existsSync(path.join(THUMB_DIR,`${s.id}.png`)),u=await captureAsset(s.id,s.path,s.category);if(l)e++,n=0;else if(u)c++,n=0;else if(r++,n++,n>=2){if(console.log(`
  UE appears down. Waiting for recovery...`),!await waitForUE(120)){console.log("  UE did not recover after 120s. Stopping.");break}console.log("  UE is back! Continuing..."),n=0}await sleep(3e3)}console.log(`
Done: ${c} captured, ${e} skipped, ${r} failed`),await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
for a in subsys.get_all_level_actors():
    if a.get_actor_label().startswith("Arena_Env_") or a.get_actor_label().startswith("ThumbActor_"):
        subsys.destroy_actor(a)
`})}main().catch(t=>{console.error(t),process.exit(1)});
