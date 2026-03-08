"use strict";const{spawn}=require("child_process"),express=require("express"),cors=require("cors"),path=require("path"),fs=require("fs"),{SkillRegistry}=require("./skills"),{SceneManager}=require("./scenes"),{ArenaManager}=require("./arena"),{AgentManager}=require("./agents"),PORT=parseInt(process.env.PORT||"3002",10),CLAUDE_BIN=process.env.CLAUDE_BIN||"claude",MCP_CONFIG=path.resolve(__dirname,"../mcp.json"),ARENA_ROOT=path.resolve(__dirname,"../.."),SCREENSHOT_DIR=path.join(ARENA_ROOT,"tmp","screens"),LOG_DIR=path.join(ARENA_ROOT,"logs"),PIXEL_STREAMING_URL=process.env.PIXEL_STREAMING_URL||"http://127.0.0.1:8080",UNREAL_HOST=process.env.UNREAL_HOST||"127.0.0.1",UNREAL_PORT=process.env.UNREAL_PORT||"55559",skillRegistry=new SkillRegistry,sceneManager=new SceneManager,arenaManager=new ArenaManager,agentManager=new AgentManager,SCREENSHOT_SEARCH_DIRS=[SCREENSHOT_DIR];fs.mkdirSync(SCREENSHOT_DIR,{recursive:!0}),fs.mkdirSync(LOG_DIR,{recursive:!0});function getLogFilePath(){const e=new Date().toISOString().slice(0,10);return path.join(LOG_DIR,`chat_${e}.log`)}function logToFile(s,e){const n=`[${new Date().toISOString()}] [${s}] ${e}
`;try{fs.appendFileSync(getLogFilePath(),n)}catch{}console.log(`[${s}] ${e}`)}const ARENA_SYSTEM_PROMPT=`You are the SimWorld Arena scene-generation agent.
You build city scenes in Unreal Engine 5 using MCP tools. The user sees a live viewport on the right.

## CRITICAL: HOW TO SPAWN OBJECTS

SimWorld assets are Blueprint actors. You MUST use spawn_blueprint_actor (NOT spawn_actor) for buildings, trees, vehicles, and props.

### Buildings (127 varieties)
spawn_blueprint_actor with blueprint_id like "BP_Building_01" through "BP_Building_127".
Full path format: /Game/CityDatabase/blueprints/BP_Building_XX.BP_Building_XX_C

BUILDING SIZES VARY ENORMOUSLY \u2014 choose carefully:
- Small/residential (height ~1000-3000 units): BP_Building_01 through BP_Building_09
- Medium (height ~3000-9000 units): BP_Building_10 through BP_Building_30
- Tall skyscrapers (height 10000-40000 units!): BP_Building_31+
For residential neighborhoods, ONLY use BP_Building_01 through BP_Building_09.

Example \u2014 spawn a house:
  spawn_blueprint_actor(actor_name="House_1", blueprint_id="BP_Building_05", location=[0, 0, 0])

### Trees (6 varieties)
  spawn_blueprint_actor(actor_name="Tree_1", blueprint_id="BP_Tree1", location=[500, 200, 0])
  BP_Tree1 through BP_Tree6

### Street furniture
  BP_Hydrant, BP_Trash_bin_a, BP_Trash_bin_b, BP_Trash_can, BP_Table, BP_Table2, BP_Table3
  BP_RoadBlocker, BP_RoadCone, BP_Box, BP_Box2, BP_Box3, BP_Couch

### Vehicles
  BP_Scooter_01 through BP_Scooter_04, BP_Cart, BP_Cart2

### Roads (static mesh \u2014 use spawn_actor)
  spawn_actor(name="Road_1", static_mesh="/Game/CityDatabase/meshes/SM_Road.SM_Road", location=[0,0,0], scale=[10,10,1])

## UNITS & SPACING
- UE uses centimeters: 1 meter = 100 units
- Small buildings (01-09): ~1000-3000 units tall, ~1000-2000 wide. Space 3000-5000 apart.
- Medium buildings (10-30): ~3000-9000 units tall. Space 5000-8000 apart.
- Trees: 1000-2000 units apart
- A small residential block: roughly 15000x10000 units

## WORKFLOW \u2014 FOLLOW THIS EXACTLY
1. Call delete_all_spawned() FIRST to clear previous session objects
2. Call setup_environment() to create sun, sky, fog, ground. Without it the scene is BLACK.
3. Plan the layout: calculate positions for all objects before spawning
4. Spawn buildings using spawn_blueprint_actor with varied blueprint_ids
5. Add trees along streets
6. Add street furniture (hydrants, trash bins, etc.)
7. Move camera to see the scene with set_camera
8. Take a screenshot with take_screenshot()
9. Tell the user what you built

## CAMERA \u2014 CRITICAL FOR GOOD SCREENSHOTS
Use set_camera to position the viewport. The rotation format is [pitch, yaw, roll]:
- pitch: negative = look down (-90 = straight down, -20 = slight downward)
- yaw: horizontal rotation (0 = look along +X, 90 = look along +Y)

Camera distance depends on building sizes:
- For small buildings (01-09, ~2000 units tall): camera at 3000-8000 altitude works well
- For medium buildings (10-30): camera at 8000-15000 altitude
- For tall skyscrapers: camera at 20000+ altitude

CAMERA PRESETS for small residential scene centered at (cx, cy):
- Isometric overview: set_camera(location=[cx-5000, cy-5000, 5000], rotation=[-25, 40, 0])
- Street view: set_camera(location=[cx-3000, cy, 1500], rotation=[-10, 15, 0])
- Bird's eye: set_camera(location=[cx, cy, 8000], rotation=[-75, 0, 0])

ALWAYS take a screenshot IMMEDIATELY after set_camera so the user sees results.

## EXAMPLE: "Build 6 houses with trees"
1. delete_all_spawned()
2. setup_environment()
3. Spawn 6 SMALL buildings (01-09 only!) in a 2x3 grid, 4000 units apart:
   spawn_blueprint_actor(actor_name="House_1", blueprint_id="BP_Building_01", location=[0, 0, 0])
   spawn_blueprint_actor(actor_name="House_2", blueprint_id="BP_Building_03", location=[4000, 0, 0])
   spawn_blueprint_actor(actor_name="House_3", blueprint_id="BP_Building_05", location=[8000, 0, 0])
   spawn_blueprint_actor(actor_name="House_4", blueprint_id="BP_Building_02", location=[0, 5000, 0])
   spawn_blueprint_actor(actor_name="House_5", blueprint_id="BP_Building_06", location=[4000, 5000, 0])
   spawn_blueprint_actor(actor_name="House_6", blueprint_id="BP_Building_08", location=[8000, 5000, 0])
4. Add trees between houses:
   spawn_blueprint_actor(actor_name="Tree_1", blueprint_id="BP_Tree1", location=[2000, -800, 0])
   spawn_blueprint_actor(actor_name="Tree_2", blueprint_id="BP_Tree3", location=[6000, -800, 0])
   ... (more trees along the streets)
5. set_camera(location=[-3000, -3000, 5000], rotation=[-25, 40, 0])
6. take_screenshot()

## IMPORTANT RULES
- ALWAYS use spawn_blueprint_actor for buildings/trees/props, NOT spawn_actor
- Each actor_name must be unique
- Use varied blueprint_ids (don't use the same building for everything)
- After placing objects, ALWAYS set_camera + take_screenshot so the user sees results
- Keep it simple: spawn objects, move camera, screenshot. Don't overthink it.`,app=express();app.use(cors()),app.use(express.json({limit:"10mb"})),app.use("/screenshots",express.static(SCREENSHOT_DIR)),app.use("/thumbnails",express.static(path.join(ARENA_ROOT,"tmp","thumbnails"))),app.get("/ue",(s,e)=>{e.setHeader("Content-Type","text/html"),e.send(`<!DOCTYPE html>
<html style="width:100%;height:100%;margin:0;background:#000">
<head><meta charset="utf-8"><title>UE Pixel Stream</title>
<style>body{margin:0;width:100vw;height:100vh;background:#000;overflow:hidden}</style>
<script>
(function(){var p=new URLSearchParams(location.search);
if(!p.has('ss')){p.set('ss','ws://'+location.hostname+':8080');
location.replace(location.pathname+'?'+p.toString());}})();
</script>
<script defer src="/ue-assets/player.js"></script>
</head><body style="width:100vw;height:100vh"></body></html>`)}),app.get("/api/pixel-streaming-url",(s,e)=>{const t=s.headers.host?.split(":")[0]||"127.0.0.1";e.json({url:`http://${t}:8080`})}),app.get("/api/health",(s,e)=>{const t=require("net");let n=!1;const o=new t.Socket,a=setTimeout(()=>{o.destroy(),i()},2e3);o.connect(parseInt(UNREAL_PORT),UNREAL_HOST,()=>{n=!0,o.destroy(),clearTimeout(a),i()}),o.on("error",()=>{clearTimeout(a),i()});function i(){e.json({status:"ok",ueConnected:n,mcpConnected:n,pixelStreamingUrl:PIXEL_STREAMING_URL})}}),app.get("/api/screenshot/latest",(s,e)=>{let t=null;for(const n of SCREENSHOT_SEARCH_DIRS)if(fs.existsSync(n))try{const o=fs.readdirSync(n).filter(a=>a.endsWith(".png")).map(a=>({filepath:path.join(n,a),time:fs.statSync(path.join(n,a)).mtimeMs})).filter(({time:a})=>Date.now()-a<18e5);for(const a of o)(!t||a.time>t.time)&&(t=a)}catch{}if(!t)return e.status(404).json({error:"No screenshots found"});e.setHeader("Cache-Control","no-store"),e.sendFile(t.filepath)}),app.get("/api/screenshot/file",(s,e)=>{const t=s.query.path;if(!t||!fs.existsSync(t))return e.status(404).json({error:"Not found"});e.setHeader("Cache-Control","no-store"),e.sendFile(path.resolve(t))}),app.post("/api/camera",(s,e)=>{const{cmd:t,args:n=[]}=s.body;if(!["set_camera","get_camera"].includes(t))return e.status(400).json({error:"Unknown camera command"});const a=require("net"),i=new a.Socket,c=setTimeout(()=>{i.destroy(),e.status(504).json({error:"Timeout"})},1e4);let m={};if(t==="set_camera"&&n.length>=6)m={script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
loc = unreal.Vector(${n[0]}, ${n[1]}, ${n[2]})
rot = unreal.Rotator(${n[3]}, ${n[4]}, ${n[5]})
subsys.set_level_viewport_camera_info(loc, rot)
`},i.connect(parseInt(UNREAL_PORT),UNREAL_HOST,()=>{i.write(JSON.stringify({type:"execute_python_script",params:m})+`
`)});else return clearTimeout(c),e.json({ok:!0,result:"no-op"});let _="";i.on("data",h=>{_+=h.toString();try{const g=JSON.parse(_);clearTimeout(c),i.destroy(),e.json({ok:!0,result:g})}catch{}}),i.on("error",h=>{clearTimeout(c),e.status(500).json({error:h.message})})}),app.get("/api/skills",(s,e)=>{e.json(skillRegistry.list())}),app.get("/api/skills/:id",(s,e)=>{const t=skillRegistry.get(s.params.id);if(!t)return e.status(404).json({error:"Skill not found"});e.json(t)}),app.get("/api/skills/search/:query",(s,e)=>{e.json(skillRegistry.search(s.params.query))}),app.post("/api/skills/reload",(s,e)=>{skillRegistry.reload(),e.json({ok:!0,count:skillRegistry.list().length})}),app.post("/api/skills",(s,e)=>{const{id:t,name:n,description:o,tags:a,dependencies:i,content:c}=s.body;if(!t||!n||!c)return e.status(400).json({error:"id, name, and content are required"});const m=["---",`id: ${t}`,`name: ${n}`,"version: 1.0.0","author: custom",`tags: [${(a||[]).join(", ")}]`,`dependencies: [${(i||[]).join(", ")}]`,`description: ${o||n}`,"---","",c].join(`
`),_=path.resolve(__dirname,"../../skills"),h=require("fs");h.mkdirSync(_,{recursive:!0});const g=path.join(_,`${t}.md`);h.writeFileSync(g,m,"utf-8"),skillRegistry.reload();const f=skillRegistry.get(t);e.json(f||{id:t,name:n,description:o,tags:a,source:"custom"})}),app.delete("/api/skills/:id",(s,e)=>{const t=skillRegistry.get(s.params.id);if(!t)return e.status(404).json({error:"Skill not found"});if(t.source!=="custom")return e.status(400).json({error:"Cannot delete builtin skills"});const n=require("fs");n.existsSync(t.filePath)&&n.unlinkSync(t.filePath),skillRegistry.reload(),e.json({ok:!0})}),app.get("/api/scenes",(s,e)=>{e.json(sceneManager.list())}),app.get("/api/scenes/:id",(s,e)=>{const t=sceneManager.load(s.params.id);if(!t)return e.status(404).json({error:"Scene not found"});e.json(t)}),app.post("/api/scenes",(s,e)=>{const t=sceneManager.save(s.body);e.json(t)}),app.delete("/api/scenes/:id",(s,e)=>{const t=sceneManager.delete(s.params.id);e.json({ok:t})}),app.get("/api/scenes/:id/thumbnail",(s,e)=>{const t=sceneManager.getThumbnailPath(s.params.id);if(!t)return e.status(404).json({error:"No thumbnail"});e.sendFile(t)}),app.post("/api/arena/battles",(s,e)=>{const{prompt:t,skills:n}=s.body,o=arenaManager.createBattle(t,n);e.json(o)}),app.get("/api/arena/battles",(s,e)=>{const{status:t,limit:n,offset:o}=s.query;e.json(arenaManager.listBattles({status:t,limit:Number(n)||50,offset:Number(o)||0}))}),app.get("/api/arena/battles/:id",(s,e)=>{const t=arenaManager.getBattle(s.params.id);if(!t)return e.status(404).json({error:"Battle not found"});e.json(t)}),app.post("/api/arena/battles/:id/submit",(s,e)=>{const{side:t,sceneData:n}=s.body,o=arenaManager.submitSceneForBattle(s.params.id,t,n);if(!o)return e.status(404).json({error:"Battle not found"});e.json(o)}),app.post("/api/arena/battles/:id/vote",(s,e)=>{const{winner:t}=s.body,n=arenaManager.vote(s.params.id,t);if(!n)return e.status(404).json({error:"Battle not found"});e.json(n)}),app.get("/api/arena/leaderboard",(s,e)=>{e.json(arenaManager.getLeaderboard())}),app.get("/api/arena/gallery",(s,e)=>{const{limit:t,offset:n,sort:o}=s.query;e.json(arenaManager.listGallery({limit:Number(t)||50,offset:Number(n)||0,sort:o}))}),app.post("/api/arena/gallery",(s,e)=>{const t=arenaManager.addToGallery(s.body);e.json(t)}),app.get("/api/arena/gallery/:id",(s,e)=>{const t=arenaManager.getGalleryScene(s.params.id);if(!t)return e.status(404).json({error:"Scene not found"});e.json(t)}),app.get("/api/agents",(s,e)=>{e.json(agentManager.list())}),app.post("/api/agents",(s,e)=>{const t=agentManager.register(s.body);e.json(t)}),app.patch("/api/agents/:id",(s,e)=>{const{enabled:t}=s.body;if(typeof t=="boolean"){const o=agentManager.toggleEnabled(s.params.id,t);return o?e.json(o):e.status(404).json({error:"Agent not found"})}const n=agentManager.register({id:s.params.id,...s.body});e.json(n)}),app.post("/api/arena/battles/:id/run",async(s,e)=>{const t=arenaManager.getBattle(s.params.id);if(!t)return e.status(404).json({error:"Battle not found"});if(t.status==="voted")return e.status(400).json({error:"Battle already completed"});e.setHeader("Content-Type","text/event-stream"),e.setHeader("Cache-Control","no-cache"),e.setHeader("Connection","keep-alive"),e.flushHeaders();function n(o,a){e.writableEnded||e.write(`event: ${o}
data: ${JSON.stringify(a)}

`)}try{const o=await agentManager.runBattle(t.prompt,t.skills,ARENA_SYSTEM_PROMPT,(i,c)=>n("progress",{phase:i,...c}));arenaManager.submitSceneForBattle(t.id,"a",o.side_a),arenaManager.submitSceneForBattle(t.id,"b",o.side_b);const a=arenaManager.getBattle(t.id);n("complete",a)}catch(o){n("error",{message:o.message})}e.end()}),app.post("/api/arena/run",async(s,e)=>{const{prompt:t,skills:n}=s.body;if(!t)return e.status(400).json({error:"prompt required"});const o=arenaManager.createBattle(t,n||[]);e.setHeader("Content-Type","text/event-stream"),e.setHeader("Cache-Control","no-cache"),e.setHeader("Connection","keep-alive"),e.flushHeaders();function a(i,c){e.writableEnded||e.write(`event: ${i}
data: ${JSON.stringify(c)}

`)}a("battle_created",{battleId:o.id,prompt:t});try{const i=await agentManager.runBattle(t,n||[],ARENA_SYSTEM_PROMPT,(m,_)=>a("progress",{phase:m,..._}));arenaManager.submitSceneForBattle(o.id,"a",i.side_a),arenaManager.submitSceneForBattle(o.id,"b",i.side_b);const c=arenaManager.getBattle(o.id);a("complete",c)}catch(i){a("error",{message:i.message})}e.end()}),app.get("/api/assets",(s,e)=>{try{const t=JSON.parse(fs.readFileSync(path.join(__dirname,"assets.json"),"utf-8")),n={};for(const[o,a]of Object.entries(t)){const i={description:a.description||"",items:[]};o==="buildings"&&a.ids?(i.items=a.ids.map(c=>{const _=`BP_Building_${String(c).padStart(2,"0")}`;return{id:_,path:`/Game/CityDatabase/blueprints/${_}.${_}_C`}}),a.notes&&(i.description+=" "+a.notes)):a.items&&(i.items=a.items.map(c=>{if(typeof c=="string"){const m=c.split("/");return{id:m[m.length-1].split(".")[0],path:c}}return c})),n[o]=i}e.json(n)}catch(t){e.status(500).json({error:t.message})}}),app.post("/api/chat",(s,e)=>{const{message:t,sessionId:n,skills:o,feedback:a}=s.body;if(!t)return e.status(400).json({error:"message required"});e.setHeader("Content-Type","text/event-stream"),e.setHeader("Cache-Control","no-cache"),e.setHeader("Connection","keep-alive"),e.setHeader("X-Accel-Buffering","no"),e.flushHeaders();function i(d,r){e.writableEnded||e.write(`event: ${d}
data: ${JSON.stringify(r)}

`)}const c=setInterval(()=>{e.writableEnded||e.write(`: ping

`)},15e3);let m=ARENA_SYSTEM_PROMPT;if(o&&o.length>0){const d=skillRegistry.compose(o);d&&(m+=`

## ACTIVE SKILLS (reference documentation)
`+d)}a&&(m+=`

## USER FEEDBACK ON CURRENT SCENE
The user is providing feedback on the current scene. Modify the scene based on this feedback. Do NOT start from scratch \u2014 refine what exists.
Feedback: ${a}`);const _=["-p",t,"--output-format","stream-json","--include-partial-messages","--verbose","--dangerously-skip-permissions","--mcp-config",MCP_CONFIG,"--append-system-prompt",m];n&&_.push("--resume",n);const h=Object.assign({},process.env);delete h.CLAUDECODE,delete h.CLAUDE_SESSION_ID,delete h.CLAUDE_CODE_ENTRYPOINT,logToFile("chat",`User: "${t.slice(0,200)}" sessionId=${n||"new"}`);try{fs.writeFileSync(path.join(LOG_DIR,"raw_latest.jsonl"),"")}catch{}const g=spawn(CLAUDE_BIN,_,{cwd:path.resolve(__dirname,".."),env:h,stdio:["ignore","pipe","pipe"]});let f="",B=new Set,S=n||null,b=null;function j(d){if(d=d.trim(),!d)return;try{fs.appendFileSync(path.join(LOG_DIR,"raw_latest.jsonl"),d+`
`)}catch{}let r;try{r=JSON.parse(d)}catch{return}const u=r.type;if(u==="system"&&r.subtype==="init"){r.session_id&&(S=r.session_id);const p=(r.mcp_servers||[]).map(l=>`${l.name}:${l.status}`);i("system",{sessionId:r.session_id,mcpServers:r.mcp_servers||[]}),logToFile("claude",`Session ${r.session_id} | MCP: ${p.join(", ")}`)}else if(u==="stream_event"){const p=r.event||{};if(p.type==="content_block_delta"&&p.delta?.type==="text_delta"&&i("text",{delta:p.delta.text}),p.type==="content_block_start"&&p.content_block?.type==="tool_use"){const l=p.content_block;if(!B.has(l.id)){B.add(l.id);const y=l.name.replace(/^mcp__\w+__/,"");i("tool_start",{id:l.id,name:l.name,displayName:y}),logToFile("tool",`Starting: ${l.name}`)}}p.type==="content_block_delta"&&p.delta?.type==="input_json_delta"&&i("tool_input",{delta:p.delta.partial_json})}else if(u==="assistant"){const p=r.message?.content||[];for(const l of p)if(l.type==="tool_use"){const y=l.name.replace(/^mcp__\w+__/,"");i("tool_details",{id:l.id,name:l.name,displayName:y,input:l.input})}}else if(u==="user"){const p=r.message?.content||[];for(const l of p)if(l.type==="tool_result"){const y=Array.isArray(l.content)?l.content.map(v=>v.text||"").join(""):String(l.content||""),w=y.match(/([\/][\w\/\-._]+\.png)/);w&&fs.existsSync(w[1])&&(b=w[1],i("screenshot",{toolUseId:l.tool_use_id,filepath:`/api/screenshot/file?path=${encodeURIComponent(b)}`})),i("tool_result",{toolUseId:l.tool_use_id,result:y.slice(0,2e3),isError:l.is_error||!1}),logToFile("tool_result",`${l.tool_use_id?.slice(0,8)} \u2192 ${y.slice(0,300)}`)}}else if(u==="result"){S=r.session_id;const p=r.is_error||r.subtype==="error_during_turn";logToFile("claude",`Result: subtype=${r.subtype} session=${S} cost=$${r.total_cost_usd||"?"}`),logToFile("result",JSON.stringify({subtype:r.subtype,cost:r.total_cost_usd,duration:r.duration_ms}).slice(0,500)),T(),clearInterval(c),i("done",{sessionId:S,isError:p,costUsd:r.total_cost_usd,latestScreenshot:b?`/api/screenshot/file?path=${encodeURIComponent(b)}`:k()}),e.end()}}function T(){let d=null;if(fs.existsSync(SCREENSHOT_DIR))try{const r=fs.readdirSync(SCREENSHOT_DIR).filter(u=>u.endsWith(".png")).map(u=>({fp:path.join(SCREENSHOT_DIR,u),time:fs.statSync(path.join(SCREENSHOT_DIR,u)).mtimeMs})).filter(({time:u})=>Date.now()-u<18e5);for(const u of r)(!d||u.time>d.time)&&(d=u)}catch{}d&&(b=d.fp)}function k(){return T(),b?`/api/screenshot/file?path=${encodeURIComponent(b)}`:null}g.stdout.on("data",d=>{f+=d.toString();const r=f.split(`
`);f=r.pop()??"";for(const u of r)j(u)}),g.stderr.on("data",d=>{const r=d.toString().trim();r&&logToFile("stderr",r.slice(0,300))}),g.on("close",d=>{clearInterval(c),f.trim()&&j(f),logToFile("claude",`Process exited with code ${d}`),e.writableEnded||(i("done",{sessionId:S,isError:d!==0,latestScreenshot:k()}),e.end())}),e.on("close",()=>{e.writableEnded||(clearInterval(c),g.killed||(g.kill("SIGTERM"),logToFile("claude","Browser closed connection, killed process")))})});const FRONTEND_DIR=path.resolve(__dirname,"../dist");fs.existsSync(FRONTEND_DIR)&&(app.use(express.static(FRONTEND_DIR)),app.get("*",(s,e)=>{!s.path.startsWith("/api/")&&!s.path.startsWith("/screenshots")&&!s.path.startsWith("/thumbnails")&&!s.path.startsWith("/ue")&&e.sendFile(path.join(FRONTEND_DIR,"index.html"))}),console.log("  Frontend served from:",FRONTEND_DIR)),app.listen(PORT,"0.0.0.0",()=>{console.log(`
\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557`),console.log("\u2551       SimWorld Arena Backend                      \u2551"),console.log("\u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563"),console.log(`\u2551  Listening : http://0.0.0.0:${PORT}                  \u2551`),console.log(`\u2551  Claude    : ${CLAUDE_BIN}                            \u2551`),console.log("\u2551  MCP config: mcp.json (local stdio)               \u2551"),console.log(`\u2551  UE TCP    : ${UNREAL_HOST}:${UNREAL_PORT}                 \u2551`),console.log(`\u2551  Logs      : ${LOG_DIR}          \u2551`),console.log(`\u255A\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255D
`)});
