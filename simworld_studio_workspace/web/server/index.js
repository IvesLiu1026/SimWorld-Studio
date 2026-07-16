"use strict";const{spawn}=require("child_process"),express=require("express"),path=require("path"),fs=require("fs"),{SkillRegistry}=require("./skills"),{SceneManager}=require("./scenes"),{CheckpointManager}=require("./checkpoints"),{ArenaManager}=require("./arena"),{AgentManager}=require("./agents"),{ContextManager}=require("./context-manager"),{AgentController}=require("./agent-controller"),PORT=parseInt(process.env.PORT||"3002",10),CLAUDE_BIN=process.env.CLAUDE_BIN||"claude",MCP_CONFIG=path.resolve(__dirname,"../mcp.json"),ARENA_ROOT=path.resolve(__dirname,"../.."),SCREENSHOT_DIR=path.join(ARENA_ROOT,"tmp","screens"),LOG_DIR=path.join(ARENA_ROOT,"logs"),PIXEL_STREAMING_URL=process.env.PIXEL_STREAMING_URL||"http://127.0.0.1:8080",CIRRUS_WS_PORT=parseInt(process.env.CIRRUS_WS_PORT||"8586",10),CIRRUS_HTTP_PORT=parseInt(process.env.CIRRUS_HTTP_PORT||"8585",10),UNREAL_HOST=process.env.UNREAL_HOST||"127.0.0.1",UNREAL_PORT=process.env.UNREAL_PORT||(()=>{try{return JSON.parse(fs.readFileSync(MCP_CONFIG,"utf-8")).mcpServers.simworld.env.UNREAL_PORT||"55559"}catch(_){return"55559"}})(),MOCK_MODE=process.env.MOCK_MODE==="1"||process.env.MOCK_MODE==="true",MOCK_FILE=process.env.MOCK_FILE?(path.isAbsolute(process.env.MOCK_FILE)?process.env.MOCK_FILE:path.join(ARENA_ROOT,process.env.MOCK_FILE)):path.join(ARENA_ROOT,"mock_responses.txt");function normalizeUnrealVersion(v){const s=String(v||"").trim();if(!s)return null;const m=s.match(/(?:UE\s*)?(\d+(?:\.\d+){1,2})/i);return m?m[1]:s}function versionFromPath(v){const s=String(v||"");const m=s.match(/(?:UE|Unreal(?:[_-]?Engine)?|Linux[_-]?Unreal[_-]?Engine)[_-]?(\d+(?:\.\d+){1,2})/i)||s.match(/(\d+\.\d+(?:\.\d+)?)/);return m?m[1]:null}function versionFromLog(){try{const p=path.join(LOG_DIR,"ue.log");if(!fs.existsSync(p))return null;const text=fs.readFileSync(p,"utf8").slice(-250000);const m=text.match(/Engine Version:\s*(\d+(?:\.\d+){1,2})/i)||text.match(/engineversion="(\d+(?:\.\d+){1,2})/i);return m?m[1]:null}catch{return null}}function getUnrealEngineVersion(){return normalizeUnrealVersion(process.env.UE_VERSION||process.env.UNREAL_VERSION||process.env.UNREAL_ENGINE_VERSION)||versionFromPath(process.env.UE_ROOT)||versionFromPath(process.env.UNREAL_ENGINE_ROOT)||versionFromPath(process.env.UE_EDITOR)||versionFromPath(process.env.UNREAL_EDITOR)||versionFromLog()}function getUnrealEngineHealthMeta(){const engineVersion=getUnrealEngineVersion();return{engineVersion,engineLabel:engineVersion?`UE ${engineVersion}`:"Unreal Engine"}}let mockReplay=null;let mockExecutor=null;if(MOCK_MODE){try{const{MockReplay:MockReplayClass}=require("./mock-replay");mockReplay=new MockReplayClass(MOCK_FILE);console.log(`[mock-replay] Mock mode enabled, using file: ${MOCK_FILE}`);console.log(`[mock-replay] Loaded ${mockReplay.messages.length} mock messages`);if(mockReplay.messages.length===0){console.error(`[mock-replay] WARNING: No messages loaded from ${MOCK_FILE}`)};({mockExecutor}=require("./mock-executor"))}catch(e){console.error(`[mock-replay] Failed to load mock-replay: ${e.message}`);console.error(e.stack)}}const crypto=require("crypto");const log=require("./logger");const{LearnedToolStore}=require("./learned-tools-store");const{getBroker:_getUcvBroker,getUeBroker:_getUeBroker}=require("./unreal-bridge");const{createVistaRuntimeBroker}=require("./vista-runtime-broker");const ctxManager=new ContextManager;const agentCtrl=new AgentController;const toolStore=new LearnedToolStore();const ucvBroker=_getUcvBroker();const ueBroker=_getUeBroker();const vistaRuntimeBroker=createVistaRuntimeBroker({ueBroker});const{MetricsHub}=require("./metrics-hub");const metricsHub=new MetricsHub(5000);metricsHub.init(agentCtrl);agentCtrl.setMetricsHub(metricsHub);const {SessionSkillManager}=require("./session-skills");const {generateSkills}=require("./skill-maker");const sessionSkillManager=new SessionSkillManager();const {handleSceneLoop}=require("./scene-loop");const {handleVisualSceneLoop}=require("./scene-loop-visual");const {handleCodexChat}=require("./chat-codex");const intentStore=new Map();
const { codingAgentsEnabled, createAccessGuard, createLoopbackBrowserHeaders, createModelGate, requestLoopbackGuard, resolveAccessToken, resolveBindHost, resolveContainedFile, resolveModelMode, resolveVistaDemoFps } = require("./runtime-security");
const STUDIO_HOST = resolveBindHost(process.env);
const STUDIO_ACCESS_TOKEN = resolveAccessToken(process.env);
const STUDIO_MODEL_MODE = resolveModelMode(process.env);
const STUDIO_CODING_AGENTS_ENABLED = codingAgentsEnabled(process.env);
const VISTA_DEMO_ENABLED = process.env.VISTA_DEMO_ENABLED === "1";
const VISTA_DEMO_FPS = resolveVistaDemoFps(process.env);
if (STUDIO_MODEL_MODE === "mock" && (!MOCK_MODE || !mockReplay || !mockExecutor)) {
  throw new Error("STUDIO_MODEL_MODE=mock requires the reviewed mock replay modules; use off for first bring-up");
}
// Stable session token — persists across all Claude subprocess spawns
const{buildSceneAgentRuntimeAppendix}=require("./agent-runtime-policy");
const demoVideos=require("./demo-videos");
const ASSET_PREVIEW_DIR=path.join(ARENA_ROOT,"tmp","asset-previews");
const STUDIO_SESSION=crypto.randomUUID();
function _normalizeRunner(v){const s=String(v||"").toLowerCase();if(s==="codex"||s==="gpt-5.5"||s==="openai")return "codex";if(s==="claude"||s==="anthropic")return "claude";return null;}
function _normalizeMode(v){const s=String(v||"").toLowerCase();if(s==="vanilla"||s==="off"||s==="none")return "vanilla";if(s==="text_loop"||s==="loop"||s==="text")return "text_loop";if(s==="visual_loop"||s==="visual"||s==="multi_view")return "visual_loop";return null;}
const _DEFAULT_RUNNER=(process.env.LLM_PROVIDER==="codex"||process.env.LLM_PROVIDER==="gpt-5.5")?"codex":"claude";
let DYNAMIC_SKILLS=(process.env.DYNAMIC_SKILLS==="1");
let sceneLoopMode=_normalizeMode(process.env.SCENE_LOOP_MODE)||((process.env.SCENE_LOOP_ENABLED==="1")?"text_loop":"vanilla");
let sceneLoopEnabled=(sceneLoopMode!=="vanilla");
logToFile("init",`Studio session: ${STUDIO_SESSION}`);async function snapshotScene(sid){return new Promise(resolve=>{const sock=new(require("net").Socket)(),timer=setTimeout(()=>{sock.destroy();resolve(null)},5000);sock.connect(parseInt(UNREAL_PORT),UNREAL_HOST,()=>{sock.write(JSON.stringify({type:"get_actors_in_level",params:{}})+"\n")});let buf="";sock.on("data",d=>{buf+=d.toString();try{const res=JSON.parse(buf);clearTimeout(timer);sock.destroy();ctxManager.updateFromSnapshot(sid,res);resolve(res)}catch(_){}});sock.on("error",()=>{clearTimeout(timer);sock.destroy();resolve(null)})})}const{TaskSetManager}=require("./tasksets"),{selectSkillsWithClaude}=require("./skill-selector");let skillRegistry=new SkillRegistry,sceneManager=new SceneManager,checkpointManager=new CheckpointManager(),arenaManager=new ArenaManager,agentManager=new AgentManager,taskSetManager=new TaskSetManager(),SCREENSHOT_SEARCH_DIRS=[SCREENSHOT_DIR];fs.mkdirSync(SCREENSHOT_DIR,{recursive:!0}),fs.mkdirSync(LOG_DIR,{recursive:!0}),fs.mkdirSync(ASSET_PREVIEW_DIR,{recursive:!0});function getLogFilePath(){const e=new Date().toISOString().slice(0,10);return path.join(LOG_DIR,`chat_${e}.log`)}function logToFile(s,e){const n=`[${new Date().toISOString()}] [${s}] ${e}
`;try{fs.appendFileSync(getLogFilePath(),n)}catch{}console.log(`[${s}] ${e}`)}const ARENA_SYSTEM_PROMPT=`You are the SimWorld Studio scene-generation agent.
You build city scenes in Unreal Engine 5 using MCP tools. The user sees a live viewport on the right.

## CRITICAL: HOW TO SPAWN OBJECTS

SimWorld assets are Blueprint actors. You MUST use spawn_blueprint_actor (NOT spawn_actor) for buildings, trees, vehicles, and props.

### Buildings -- 125 varieties (BP_Building_01 through BP_Building_127, IDs 57 and 120 missing)
Full path: /Game/CityDatabase/blueprints/BP_Building_XX.BP_Building_XX_C
- Small (01-20), mid-rise (21-60), large/specialty (61-127) -- use VARIED IDs for diversity
- Example: spawn_blueprint_actor(actor_name="Bldg_1", blueprint_id="BP_Building_42", location=[0,0,0])

### Trees: BP_Tree1 through BP_Tree6
### Vehicles: BP_Scooter_01-04, BP_Cart, BP_Cart2
### Street furniture: BP_Table/2/3, BP_Hydrant, BP_Trash_bin_a/b, BP_Trash_can,
  BP_RoadBlocker, BP_RoadCone, BP_Couch, BP_Box/2/3, BP_Can/2, BP_Rabbish, BP_Soda1-4
### Static meshes (spawn_actor): SM_TrafficLight1, SM_hydrant_main, SM_road_cone, SM_chair_b ...
### More assets: use list_assets(path="/Game/CityDatabase/") to discover all.
  17 allow-AI marketplace packs: list_assets(path="/Game/<PackName>/")

## UNITS & SPACING
- UE uses centimeters: 1 m = 100 units
- Small buildings: 1000-3000 tall, space 3000-5000 apart
- Large buildings: 4000-8000 tall, space 6000-10000 apart

## WORKFLOW
1. delete_all_spawned()
2. setup_environment() -- scene is BLACK without this
3. Spawn with varied blueprint_ids, add trees/props
4. take_screenshot()

## PYTHON SCRIPT BATCHING
- Follow the shared runtime policy appended to this prompt.
- Prefer normal MCP tools for simple spawning, transforms, screenshots, verification, and single-actor edits.
- If execute_python_script is required, build the scene in small sequential batches of roughly 6-12 actors/operations per script.
- Use phases such as clear/setup, major layout, buildings, props, vegetation/vehicles/agents, then validation/save.
- After each batch, read log_path until [DONE] or [ERROR], inspect the result, then continue with the next batch.
- Never generate one giant Python script for an entire large scene.

## EXAMPLE: city block
1. delete_all_spawned()
2. setup_environment()
3. spawn_blueprint_actor(actor_name="Bldg_1", blueprint_id="BP_Building_12", location=[0,0,0])
   spawn_blueprint_actor(actor_name="Bldg_2", blueprint_id="BP_Building_35", location=[5000,0,0])
   spawn_blueprint_actor(actor_name="Bldg_3", blueprint_id="BP_Building_67", location=[0,6000,0])
4. take_screenshot()

## SPAWNING CONTROLLABLE AGENTS
When the user asks for "people", "pedestrians", "characters", "agents", or "someone walking":
- Use spawn_agent (NOT spawn_blueprint_actor) to create controllable agents
- agent_type="pedestrian" for human NPCs, agent_type="humanoid" for robot agents
- Set location Z=110 for ground level
- Name agents clearly: "Pedestrian_1", "Agent_Walker", etc.
- After spawning, agents appear in the Agent Panel where users can control them via chat

Example — spawn 2 pedestrians:
  spawn_agent(agent_name="Pedestrian_1", agent_type="pedestrian", location=[0, 500, 110])
  spawn_agent(agent_name="Pedestrian_2", agent_type="pedestrian", location=[0, -500, 110])

## IMPORTANT RULES
- ALWAYS use spawn_blueprint_actor for buildings/trees/props, NOT spawn_actor
- Use spawn_agent for controllable humanoid/pedestrian agents
- actor_names get a session suffix automatically (prevents cross-map crashes) — use returned name for subsequent ops
- Use varied blueprint_ids (don't use the same building for everything)
- After placing objects, ALWAYS take_screenshot so the user sees results
- DO NOT set or move the camera. DO NOT use execute_python_script to change camera position/rotation. The camera is controlled by the user via the viewport. Just call take_screenshot directly.
- Keep it simple: spawn objects, screenshot. Don't overthink it.
- To load a map use LevelEditorSubsystem (NOT deprecated EditorLevelLibrary):
  subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
  subsystem.load_level("/Game/PackName/Maps/MapName")`+"\n\n"+buildSceneAgentRuntimeAppendix(),app=express();app.use(express.json({limit:"10mb"})),app.use(requestLoopbackGuard),app.use(createAccessGuard(STUDIO_ACCESS_TOKEN)),app.use(createLoopbackBrowserHeaders({signalingPort:VISTA_DEMO_ENABLED?CIRRUS_HTTP_PORT:null})),app.use(createModelGate({mode:STUDIO_MODEL_MODE,allowCodingAgents:STUDIO_CODING_AGENTS_ENABLED,demoMode:VISTA_DEMO_ENABLED,isMockReady:()=>Boolean(MOCK_MODE&&mockReplay&&mockExecutor)})),app.use((req,res,next)=>{if(req.method==="POST"){const fixedVistaRoute=req.path==="/api/vista/setup_vista_play_mode"||req.path==="/api/vista/stop_vista_play_mode",bodyForLog=fixedVistaRoute?"<fixed-empty-contract>":JSON.stringify(req.body||{}).slice(0,200);logToFile("http",`${req.method} ${req.path} body=${bodyForLog}`)}res.set("Connection","close");next()}),app.use("/screenshots",express.static(SCREENSHOT_DIR)),app.use("/thumbnails",express.static(path.join(ARENA_ROOT,"tmp","thumbnails"))),app.use("/asset-previews",express.static(ASSET_PREVIEW_DIR,{maxAge:"7d",immutable:true})),app.get("/ue",(s,e)=>{e.setHeader("Content-Type","text/html"),e.send(`<!DOCTYPE html>
<html style="width:100%;height:100%;margin:0;background:#000">
<head><meta charset="utf-8"><title>UE Pixel Stream</title>
<style>
body{margin:0;width:100vw;height:100vh;background:#000;overflow:hidden}
/* Fill viewport with no black bars */
#playerUI,#videoElementParent{
  position:absolute!important;
  inset:0!important;
  width:100%!important;
  height:100%!important;
  min-width:0!important;
  min-height:0!important;
  overflow:hidden!important;
  contain:size layout paint;
  background:#000!important;
}
#streamingVideo{position:absolute!important;inset:0!important;object-fit:contain;width:100%!important;height:100%!important;max-width:none!important;max-height:none!important}
/* Compact controls menu — top-right corner */
#controls{
  position:absolute!important;
  top:8px!important;
  right:8px!important;
  left:auto!important;
  display:flex!important;
  flex-direction:row!important;
  gap:4px!important;
  background:rgba(0,0,0,0.45)!important;
  border-radius:8px!important;
  padding:4px!important;
  backdrop-filter:blur(6px)!important;
}
#controls>*{
  margin-bottom:0!important;
  width:1.6rem!important;
  height:1.6rem!important;
  padding:0.25rem!important;
  line-height:1.1rem!important;
  border-radius:6px!important;
  display:flex!important;
  align-items:center!important;
  justify-content:center!important;
}
/* Connection quality indicator — bottom-right, small */
#connection{
  position:absolute!important;
  bottom:8px!important;
  right:8px!important;
  left:auto!important;
  width:1.6rem!important;
  height:1.6rem!important;
}
/* Slide-out panels from right */
.panel-wrap{min-width:260px!important;max-width:320px!important}
</style>
<script>
(function(){var p=new URLSearchParams(location.search);
var target='ws://'+location.hostname+':${CIRRUS_HTTP_PORT}';
if(p.get('ss')!==target){p.set('ss',target);
location.replace(location.pathname+'?'+p.toString());}})();
</script>
<script defer src="/ue-assets/player.js"></script>
</head><body style="width:100vw;height:100vh"></body></html>`)}),app.get("/api/pixel-streaming-url",async(s,e)=>{  const host=s.headers.host?.split(":")[0]||"127.0.0.1";  const candidates=[CIRRUS_HTTP_PORT,8685,8585,8785,8885,8485].filter((v,i,a)=>a.indexOf(v)===i);  const net=require("net");  const probe=p=>new Promise(r=>{    const sock=new net.Socket();    sock.setTimeout(800);    sock.connect(p,"127.0.0.1",()=>{sock.destroy();r(p)});    sock.on("error",()=>r(null));    sock.on("timeout",()=>{sock.destroy();r(null)});  });  for(const port of candidates){    const found=await probe(port);    if(found)return e.json({url:"http://"+host+":"+found,detectedPort:found,webRtcFps:VISTA_DEMO_FPS});  }  e.json({url:"http://"+host+":"+CIRRUS_HTTP_PORT,detectedPort:null,webRtcFps:VISTA_DEMO_FPS});}),app.get("/api/health",(s,e)=>{const t=require("net");let n=!1;const o=new t.Socket,i=setTimeout(()=>{o.destroy(),a()},2e3);o.connect(parseInt(UNREAL_PORT),UNREAL_HOST,()=>{n=!0,o.destroy(),clearTimeout(i),a()}),o.on("error",()=>{clearTimeout(i),a()});function a(){e.json({status:"ok",ueConnected:n,mcpConnected:n,pixelStreamingUrl:PIXEL_STREAMING_URL,vistaDemoFps:VISTA_DEMO_FPS,...getUnrealEngineHealthMeta()})}}),app.get("/api/screenshot/latest",(s,e)=>{let t=null;for(const n of SCREENSHOT_SEARCH_DIRS)if(fs.existsSync(n))try{const o=fs.readdirSync(n).filter(i=>i.endsWith(".png")).map(i=>({filepath:path.join(n,i),time:fs.statSync(path.join(n,i)).mtimeMs})).filter(({time:i})=>Date.now()-i<18e5);for(const i of o)(!t||i.time>t.time)&&(t=i)}catch{}if(!t)return e.status(404).json({error:"No screenshots found"});e.setHeader("Cache-Control","no-store"),e.sendFile(t.filepath)}),app.get("/api/screenshot/file",(s,e)=>{const t=resolveContainedFile(s.query.path,SCREENSHOT_SEARCH_DIRS);if(!t)return e.status(404).json({error:"Not found"});e.setHeader("Cache-Control","no-store"),e.sendFile(t)}),app.post("/api/camera",(s,e)=>{const{cmd:t,args:n=[]}=s.body;if(!["set_camera","get_camera"].includes(t))return e.status(400).json({error:"Unknown camera command"});const i=require("net"),a=new i.Socket,c=setTimeout(()=>{a.destroy(),e.status(504).json({error:"Timeout"})},1e4);let m={};if(t==="set_camera"&&n.length>=6)m={script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
loc = unreal.Vector(${n[0]}, ${n[1]}, ${n[2]})
rot = unreal.Rotator(pitch=${n[3]}, yaw=${n[4]}, roll=${n[5]})
subsys.set_level_viewport_camera_info(loc, rot)
`},a.connect(parseInt(UNREAL_PORT),UNREAL_HOST,()=>{a.write(JSON.stringify({type:"execute_python_script",params:m})+`
`)});else return clearTimeout(c),e.json({ok:!0,result:"no-op"});let _="";a.on("data",h=>{_+=h.toString();try{const g=JSON.parse(_);clearTimeout(c),a.destroy(),e.json({ok:!0,result:g})}catch{}}),a.on("error",h=>{clearTimeout(c),e.status(500).json({error:h.message})})}),app.get("/api/skills",(s,e)=>{e.json(skillRegistry.list())}),app.post("/api/skills/select",async(s,e)=>{try{const r=await selectSkillsWithClaude({prompt:s.body&&s.body.prompt,model:s.body&&s.body.model,claudeBin:CLAUDE_BIN,skillRegistry});e.json(r)}catch(err){e.status(500).json({error:err.message,selectedSkillIds:[]})}}),app.get("/api/skills/:id",(s,e)=>{const t=skillRegistry.get(s.params.id);if(!t)return e.status(404).json({error:"Skill not found"});e.json(t)}),app.get("/api/skills/search/:query",(s,e)=>{e.json(skillRegistry.search(s.params.query))}),app.post("/api/skills/reload",(s,e)=>{skillRegistry.reload(),e.json({ok:!0,count:skillRegistry.list().length})}),app.post("/api/skills",(s,e)=>{const{id:t,name:n,description:o,tags:i,dependencies:a,content:c}=s.body;if(!t||!n||!c)return e.status(400).json({error:"id, name, and content are required"});const m=["---",`id: ${t}`,`name: ${n}`,"version: 1.0.0","author: custom",`tags: [${(i||[]).join(", ")}]`,`dependencies: [${(a||[]).join(", ")}]`,`description: ${o||n}`,"---","",c].join(`
`),_=path.resolve(__dirname,"../../skills"),h=require("fs");h.mkdirSync(_,{recursive:!0});const g=path.join(_,`${t}.md`);h.writeFileSync(g,m,"utf-8"),skillRegistry.reload();const f=skillRegistry.get(t);e.json(f||{id:t,name:n,description:o,tags:i,source:"custom"})}),app.delete("/api/skills/:id",(s,e)=>{const t=skillRegistry.get(s.params.id);if(!t)return e.status(404).json({error:"Skill not found"});if(t.source!=="custom")return e.status(400).json({error:"Cannot delete builtin skills"});const n=require("fs");n.existsSync(t.filePath)&&n.unlinkSync(t.filePath),skillRegistry.reload(),e.json({ok:!0})}),app.get("/api/scenes",(s,e)=>{e.json(sceneManager.list())}),app.get("/api/scenes/:id",(s,e)=>{const t=sceneManager.load(s.params.id);if(!t)return e.status(404).json({error:"Scene not found"});e.json(t)}),app.post("/api/scenes",async(s,e)=>{try{const t=await sceneManager.save(s.body);e.status(201).json(t)}catch(err){e.status(500).json({error:err.message})}}),app.delete("/api/scenes/:id",async(s,e)=>{const own=s.query.ownerId||null;try{const t=await sceneManager.delete(s.params.id,own);if(t==="forbidden")return e.status(403).json({error:"Forbidden"});e.json({ok:t})}catch(err){e.status(500).json({error:err.message})}}),app.get("/api/scenes/:id/thumbnail",(s,e)=>{const t=sceneManager.getThumbnailPath(s.params.id);if(!t)return e.status(404).json({error:"No thumbnail"});e.sendFile(t)}),app.post("/api/arena/battles",(s,e)=>{const{prompt:t,skills:n}=s.body,o=arenaManager.createBattle(t,n);e.json(o)}),app.get("/api/arena/battles",(s,e)=>{const{status:t,limit:n,offset:o}=s.query;e.json(arenaManager.listBattles({status:t,limit:Number(n)||50,offset:Number(o)||0}))}),app.get("/api/arena/battles/:id",(s,e)=>{const t=arenaManager.getBattle(s.params.id);if(!t)return e.status(404).json({error:"Battle not found"});e.json(t)}),app.post("/api/arena/battles/:id/submit",(s,e)=>{const{side:t,sceneData:n}=s.body,o=arenaManager.submitSceneForBattle(s.params.id,t,n);if(!o)return e.status(404).json({error:"Battle not found"});e.json(o)}),app.post("/api/arena/battles/:id/vote",(s,e)=>{const{winner:t}=s.body,n=arenaManager.vote(s.params.id,t);if(!n)return e.status(404).json({error:"Battle not found"});e.json(n)}),app.get("/api/arena/leaderboard",(s,e)=>{e.json(arenaManager.getLeaderboard())}),app.get("/api/arena/gallery",(s,e)=>{const{limit:t,offset:n,sort:o}=s.query;e.json(arenaManager.listGallery({limit:Number(t)||50,offset:Number(n)||0,sort:o}))}),app.post("/api/arena/gallery",(s,e)=>{const t=arenaManager.addToGallery(s.body);e.json(t)}),app.get("/api/arena/gallery/:id",(s,e)=>{const t=arenaManager.getGalleryScene(s.params.id);if(!t)return e.status(404).json({error:"Scene not found"});e.json(t)}),// ── Learned Tools API ────────────────────────────────────────────────────
app.get("/api/tools",(s,e)=>{e.json(toolStore.list())});
app.get("/api/tools/:id",(s,e)=>{const t=toolStore.get(s.params.id);if(!t)return e.status(404).json({error:"Tool not found"});e.json(t)});
app.patch("/api/tools/:id",(s,e)=>{try{const t=toolStore.patch(s.params.id,s.body);if(!t)return e.status(404).json({error:"Tool not found"});e.json(t)}catch(err){e.status(400).json({error:err.message})}});
app.delete("/api/tools/:id",(s,e)=>{try{toolStore.archive(s.params.id);e.json({ok:true})}catch(err){e.status(400).json({error:err.message})}});

// Stable session — doesn't change across Claude subprocess spawns
app.get("/api/session",(s,e)=>{e.json({sessionId:STUDIO_SESSION})});

// P0-1: per-session subprocess tracking
const _chatProcs=new Map();
app.post("/api/chat-stop",(req,res)=>{
  const sid=(req.body&&req.body.sessionId)||(req.query&&req.query.sessionId)||"_global";
  const p=_chatProcs.get(sid);
  if(p&&!p.killed){try{p.kill("SIGTERM")}catch{};_chatProcs.delete(sid);return res.json({stopped:true});}
  res.json({stopped:false});
});

// ── Internal UCV bridge (Phase 2) ────────────────────────────────────────
// Loopback-only RPC endpoint that forwards UnrealCV commands to the singleton
// UcvBroker. This is how mcp-server.js subprocesses (and any other helper
// process) share the main server's persistent UCV connection instead of each
// opening their own one-shot socket. Loopback check is enforced because the
// broker has no auth — anyone who can hit it can drive UE.
app.post("/api/internal/ucv",async(req,res)=>{
  const ip=(req.ip||"").replace(/^::ffff:/,"");
  if(ip!=="127.0.0.1"&&ip!=="::1"&&ip!=="localhost"){
    return res.status(403).json({ok:false,error:"loopback only"});
  }
  const{cmd,timeoutMs,retries,queueDeadlineMs}=req.body||{};
  if(typeof cmd!=="string"||!cmd){
    return res.status(400).json({ok:false,error:"cmd required"});
  }
  try{
    const result=await ucvBroker.send(cmd,{
      timeoutMs:typeof timeoutMs==="number"?timeoutMs:undefined,
      retries:typeof retries==="number"?retries:undefined,
      queueDeadlineMs:typeof queueDeadlineMs==="number"?queueDeadlineMs:undefined,
    });
    res.json({ok:true,result});
  }catch(err){
    res.status(502).json({ok:false,error:err.message});
  }
});

app.get("/api/internal/ucv/status",(req,res)=>{
  const ip=(req.ip||"").replace(/^::ffff:/,"");
  if(ip!=="127.0.0.1"&&ip!=="::1"&&ip!=="localhost"){
    return res.status(403).json({error:"loopback only"});
  }
  res.json(ucvBroker.status());
});

// ── UE MCP broker (55559) — global funnel so all sessions share one serial queue ──
app.post("/api/internal/ue",async(req,res)=>{
  const ip=(req.ip||"").replace(/^::ffff:/,"");
  if(ip!=="127.0.0.1"&&ip!=="::1"&&ip!=="localhost"){
    return res.status(403).json({ok:false,error:"loopback only"});
  }
  const{type,params,timeoutMs}=req.body||{};
  if(typeof type!=="string"||!type){
    return res.status(400).json({ok:false,error:"type required"});
  }
  try{
    const result=await ueBroker.send(type,params,{timeoutMs:typeof timeoutMs==="number"?timeoutMs:undefined});
    res.json({ok:true,result});
  }catch(err){
    if(err&&err.retryAfterMs){
      res.set("Retry-After",String(Math.ceil(err.retryAfterMs/1000)));
      return res.status(429).json({ok:false,error:err.message,retryAfterMs:err.retryAfterMs});
    }
    res.status(502).json({ok:false,error:err.message});
  }
});

app.get("/api/internal/ue/status",(req,res)=>{
  const ip=(req.ip||"").replace(/^::ffff:/,"");
  if(ip!=="127.0.0.1"&&ip!=="::1"&&ip!=="localhost"){
    return res.status(403).json({error:"loopback only"});
  }
  res.json(ueBroker.status());
});

app.post("/api/vista/setup_vista_play_mode",vistaRuntimeBroker.setupVistaPlayMode);
app.post("/api/vista/stop_vista_play_mode",vistaRuntimeBroker.stopVistaPlayMode);
app.get("/api/vista/get_vista_state",vistaRuntimeBroker.getVistaState);

app.get("/api/context",(s,e)=>{const st=ctxManager.getState(STUDIO_SESSION);e.set("Cache-Control","no-store");e.json(st||{agents:[],objects:[],environment:{ready:false},round:0,updatedAt:null})});

// ── SSE status stream — replaces HTTP polling ──────────────────────────
// ONE persistent SSE connection pushes ALL status data to the client.
// No more HTTP polling = no connection-pool saturation = agent-chat never blocked.
//
// Port checks run in background; results are cached and pushed via SSE.
// Health check: lightweight MCP command to verify UE is alive.
// Uses find_actors_by_name with a dummy pattern — fast, tiny response,
// won't block the MCP connection for scene agent commands.
let _cachedPie=false,_cachedUeConn=false,_portCheckRunning=false;
const _refreshPortCache=async()=>{
  if(_portCheckRunning)return;
  _portCheckRunning=true;
  try{
    const result=await new Promise((resolve)=>{
      const sock=new(require("net").Socket)();
      const timer=setTimeout(()=>{sock.destroy();resolve(null)},3000);
      sock.connect(parseInt(UNREAL_PORT),UNREAL_HOST,()=>{
        // Lightweight ping — find a non-existent actor, UE returns fast empty result
        // TCP probe: connect+close only. No MCP command, zero UE log output.
        clearTimeout(timer); sock.destroy(); resolve({ok:true});
      });
      sock.on("error",()=>{clearTimeout(timer);resolve(null);});
    });
    _cachedUeConn=!!result;_cachedPie=_cachedUeConn;
  }catch{_cachedUeConn=false;_cachedPie=false}
  _portCheckRunning=false;
};
_refreshPortCache();
setInterval(_refreshPortCache,60000); // 15s — minimal interference with scene agent

// ── Live asset scan — runs once after UE connects ────────────────────────────
let _liveAssetTree = null;   // built from UE Python scan
let _assetScanDone = false;

async function _scanUeAssets() {
  if (_assetScanDone) return;
  _assetScanDone = true; // prevent re-entry; reset on failure
  log.system('info', 'Starting UE asset scan via Python…');
  const py = `
import unreal, json
roots = [
  '/Game/CityDatabase/',
  '/Game/TrafficSystem/',
  '/Game/ChineseWaterTown/',
  '/Game/Lighthouse_Island/',
  '/Game/ModularGothicFantasyEnvironment/',
  '/Game/CastleRiver/',
  '/Game/Cave/',
  '/Game/ModularTemplePlaza/',
  '/Game/TrainStation/',
  '/Game/ContainerYard/',
  '/Game/ModularCourtyard/',
  '/Game/MiddleEast/',
  '/Game/Chinese_Landscape/',
  '/Game/Village/',
  '/Game/WinterTown/',
  '/Game/ModularSciFi/',
  '/Game/Dungeon/',
  '/Game/HwaseongHaenggung/',
]
result = []
for root in roots:
    try:
        paths = list(unreal.EditorAssetLibrary.list_assets(root, recursive=True, include_folder=True))
        result.extend(paths)
    except Exception as e:
        pass
# Use clear delimiters so we can extract JSON even if other output is present
print('SWASSET_BEGIN' + __import__('json').dumps(result) + 'SWASSET_END')
`.trim();

  try {
    const raw = await new Promise((resolve, reject) => {
      const sock = new (require('net').Socket)();
      const timer = setTimeout(() => { sock.destroy(); reject(new Error('timeout')); }, 45000);
      sock.connect(parseInt(UNREAL_PORT), UNREAL_HOST, () => {
        sock.write(JSON.stringify({ type:'execute_python_script', params:{ script:py } }) + '\n');
      });
      let buf = '';
      sock.on('data', d => {
        buf += d.toString();
        // Try parse only when delimiter is present (avoids partial JSON parse errors)
        if (buf.includes('SWASSET_END')) {
          try { const r = JSON.parse(buf); clearTimeout(timer); sock.destroy(); resolve(r); } catch {}
        }
      });
      sock.on('error', e => { clearTimeout(timer); reject(e); });
      sock.on('close', () => { clearTimeout(timer); try { resolve(JSON.parse(buf)); } catch {} });
    });

    // Search in python_logs directly — avoids JSON double-encoding bug where
    // JSON.stringify(raw) escapes quotes to \" making JSON.parse fail on the slice.
    const logText = (raw?.result?.python_logs || []).join('\n');
    const start = logText.indexOf('SWASSET_BEGIN');
    const end   = logText.indexOf('SWASSET_END');
    if (start === -1 || end === -1) throw new Error('Missing SWASSET delimiters. LogText: ' + logText.slice(0,300));
    const paths = JSON.parse(logText.slice(start + 'SWASSET_BEGIN'.length, end));
    log.system('info', `Received ${paths.length} paths from UE`);

    // Build nested tree from paths
    // Folders end with '/' or match /Game/PackName/SubDir pattern without a '.' in last segment
    const root = { name:'', path:'/', children:[], assets:[] };
    const dirs = new Map([['/', root]]);

    const getOrCreateDir = (dirPath) => {
      if (dirs.has(dirPath)) return dirs.get(dirPath);
      const parts = dirPath.replace(/\/$/, '').split('/').filter(Boolean);
      let node = root, built = '/';
      for (const seg of parts) {
        built += seg + '/';
        if (!dirs.has(built)) {
          const child = { name:seg, path:built, children:[], assets:[] };
          node.children.push(child);
          dirs.set(built, child);
        }
        node = dirs.get(built);
      }
      return node;
    };

    const isFolder = (p) => p.endsWith('/') || !p.split('/').pop().includes('.');
    // Map/Level paths go to Scenes panel, not Assets drawer
    const isMap = (p) => /\/(Maps|Levels|Map)\//i.test(p);

    let totalAssets = 0;
    for (const p of paths) {
      if (!p.startsWith('/Game/')) continue;
      if (isFolder(p)) {
        // Always register the folder node so the tree has full depth
        const fp = p.endsWith('/') ? p : p + '/';
        getOrCreateDir(fp);
      } else {
        // Skip map/level assets — they belong in Scenes
        if (isMap(p)) continue;
        totalAssets++;
        const parts = p.split('/');
        const filename = parts.pop();
        const dir = parts.join('/') + '/';
        const name = filename.split('.')[0];
        const dirNode = getOrCreateDir(dir);
        const isBP   = filename.includes('_C') || p.includes('/blueprints/') || p.includes('/Blueprints/');
        const isMesh = p.includes('/meshes/') || p.includes('/Meshes/') || name.startsWith('SM_');
        const type = isBP ? 'blueprint' : isMesh ? 'static_mesh' : 'asset';
        const spawnTool = isBP ? 'spawn_blueprint_actor' : isMesh ? 'spawn_actor' : null;
        dirNode.assets.push(_withAssetPreviewMeta({ name, fullPath:p, type, spawnTool,
          icon: isBP ? '🏗️' : isMesh ? '📦' : '🔷' }));
      }
    }

    const sortNode = (node) => {
      node.children.sort((a,b) => a.name.localeCompare(b.name));
      node.children.forEach(sortNode);
      node.assets.sort((a,b) => a.name.localeCompare(b.name));
    };
    sortNode(root);
    _liveAssetTree = { tree: root, totalAssets, scannedAt: Date.now(), source:'ue-python' };
    log.system('info', `Asset tree built: ${totalAssets} assets, ${dirs.size} dirs`);
  } catch(e) {
    _assetScanDone = false; // allow retry
    log.system('info', `Failed: ${e.message} — will retry on next UE connect`);
  }
}

// Live UE asset scan via Python is gated behind LIVE_ASSET_SCAN_ENABLED.
// On UE 5.8 (official MCP) auto-firing execute_python_script against the editor is
// unsafe — it can destabilize the MCP server — so it is OFF by default and the static
// asset catalog (_assetTree) is served instead. Set LIVE_ASSET_SCAN_ENABLED=1 to opt in.
const LIVE_ASSET_SCAN_ENABLED = /^(1|true|yes|on)$/i.test(String(process.env.LIVE_ASSET_SCAN_ENABLED || ''));
// Trigger scan once when UE is reachable — check every 5s until done
const _assetScanInterval = LIVE_ASSET_SCAN_ENABLED ? setInterval(() => {
  if (_cachedUeConn && !_assetScanDone) {
    clearInterval(_assetScanInterval);
    setTimeout(_scanUeAssets, 2000); // 2s grace for UE Python to be ready
  }
}, 5000) : null;
if (!LIVE_ASSET_SCAN_ENABLED) log.system('info', 'Live UE asset scan disabled (set LIVE_ASSET_SCAN_ENABLED=1 to enable); serving static asset catalog.');

// Auto-discover player-controlled agents via vget /objects
// Patterns: actual agent pawns only — excludes cameras, controllers, HUDs, spectators
// Match actual agent/pawn blueprints — covers Base_Demo, Base_Pedestrian, Base_User_Agent etc.
const AGENT_PATTERNS  = /agent|pedestrian|base_user|base_ped|base_demo|walker|npc|character|human|robot|pawn/i;
// Exclude helper/system actors that share keywords but are not agents
const AGENT_EXCLUDE   = /camera|controller|manager|hud|spectator|default__|landscape|sky|light|fog|floor|ground|wall|brush|terrain|navmesh|trigger|volume|blockingvolume|decal|postprocess|atmosphericfog|exponentialheight/i;

async function _autoDiscoverAgents() {
  if (!_cachedPie) return;
  try {
    const raw = await ucvBroker.send('vget /objects', { timeoutMs: 4000, retries: 1 });
    const all = (raw || '').trim().split(/\s+/).filter(Boolean);
    const names = all.filter(n => AGENT_PATTERNS.test(n) && !AGENT_EXCLUDE.test(n));
    let registered = 0;
    for (const name of names) {
      if (!agentCtrl.get(name)) {
        agentCtrl.getOrCreate(name, 'pedestrian', null);
        registered++;
      }
    }
    if (registered > 0) log.system('info', `[agent-discover] Registered ${registered}: ${names.join(', ')}`);
  } catch { /* silent — UCV not connected */ }
}

// DISABLED: polls UCV `vget /objects` every 5s. In editor-only mode (no PIE),
// UnrealCV's FAliasHandler::VExecWithOutput hits a null GetGameViewport() at
// AliasHandler.cpp:107 and segfaults UE. _cachedPie just mirrors UE-connected,
// not actual PIE. Manual discovery is still available via POST /api/agent-discover.
// let _prevPie = false;
// setInterval(async () => {
//   if (_cachedPie && !_prevPie) setTimeout(_autoDiscoverAgents, 1000);
//   _prevPie = _cachedPie;
//   if (_cachedPie) await _autoDiscoverAgents();
// }, 5000);

// Gather current status snapshot (shared by SSE push and legacy poll)
function _gatherStatus(since=0){
  const ctx=ctxManager.getState(STUDIO_SESSION);
  if(ctx)agentCtrl.syncWithContext(ctx);
  const sessions=agentCtrl.list();
  const activities={};
  for(const sess of sessions) activities[sess.agentName]=agentCtrl.getActivity(sess.agentName);
  const chatLog=agentCtrl.getPublicChat(since);
  return{
    context:ctx||{agents:[],objects:[],environment:{ready:false},round:0,updatedAt:null},
    sessions,activities,chatLog,
    pieActive:_cachedPie,
    health:{ueConnected:_cachedUeConn,mcpConnected:_cachedUeConn,pixelStreamingUrl:PIXEL_STREAMING_URL,...getUnrealEngineHealthMeta()},
    metrics:metricsHub.snapshot(),
  };
}

// SSE endpoint — client opens ONE persistent connection, server pushes every 3s
const _sseClients=new Map();let _sseSeq=0;const _SSE_IDLE_MS=90000;
app.get("/api/events",(req,res)=>{
  res.setHeader("Content-Type","text/event-stream");
  res.setHeader("Cache-Control","no-cache");
  res.setHeader("Connection","keep-alive");
  res.setHeader("X-Accel-Buffering","no");
  res.flushHeaders();
  // Send initial snapshot immediately
  const initial=_gatherStatus(0);
  res.write(`data: ${JSON.stringify(initial)}\n\n`);
  const cid="sse-"+(++_sseSeq);
  _sseClients.set(cid,{res,lastSeen:Date.now()});
  req.on("close",()=>_sseClients.delete(cid));
});
// Push status to all SSE clients every 3s
let _sseSince=0;
let _sseLastHash='';
const _crypto=require('crypto');
setInterval(()=>{
  if(_sseClients.size===0)return;
  const snapshot=_gatherStatus(_sseSince);
  if(snapshot.chatLog?.length>0) _sseSince=Math.max(...snapshot.chatLog.map(m=>m.timestamp));

  // Hash-based conditional push — skip if nothing changed
  // Include chatLog length so new messages always push
  const hashInput=JSON.stringify({
    sessionsSig: snapshot.sessions?.map(s=>`${s.agentName}:${s.status}:${s.collisionCount}:${s.positionUpdatedAt}`).join('|'),
    objectCount: snapshot.context?.objects?.length,
    agentCount:  snapshot.context?.agents?.length,
    pieActive:   snapshot.pieActive,
    ueConnected: snapshot.health?.ueConnected,
    chatLen:     snapshot.chatLog?.length,
    round:       snapshot.context?.round,
  });
  const newHash=_crypto.createHash('md5').update(hashInput).digest('hex').slice(0,8);
  const unchanged=(newHash===_sseLastHash);
  _sseLastHash=newHash;

  // Always push at most every 9s even if unchanged (keepalive); skip intermediate pushes
  const payload=`data: ${JSON.stringify(snapshot)}\n\n`;
  const now=Date.now();
  for(const [id,c] of _sseClients){
    if(now-c.lastSeen>_SSE_IDLE_MS){_sseClients.delete(id);try{c.res.end()}catch{};continue;}
    if(unchanged && now-c.lastSeen < 9000) continue; // skip unchanged within 9s window
    try{c.res.write(payload);c.lastSeen=now;}catch{_sseClients.delete(id);}
  }
},3000);

// Legacy poll endpoint — kept as fallback, but clients should use /api/events
app.get("/api/poll",(s,e)=>{
  const since=parseInt(s.query.since||"0",10);
  e.set("Cache-Control","no-store");
  e.json(_gatherStatus(since));
});

// ── Agent Controller API ──────────────────────────────────────────────────
app.get("/api/agent-sessions",(s,e)=>{
  // Sync with latest context before returning
  const sid=s.query.sessionId||null;
  const ctx=ctxManager.getState(sid);
  if(ctx)agentCtrl.syncWithContext(ctx);
  e.json(agentCtrl.list());
});

app.post("/api/agent-chat",(s,e)=>{
  const{agentName,message,sessionId}=s.body;
  logToFile("agent-chat",`REQUEST: agentName=${agentName} message=${(message||"").slice(0,100)} sessionId=${sessionId}`);
  if(!agentName||!message)return e.status(400).json({error:"agentName and message required"});
  const ctx=ctxManager.getState(sessionId);
  logToFile("agent-chat",`context lookup: sessionId=${sessionId} hasCtx=${!!ctx} agents=${ctx?.agents?.length||0}`);
  if(ctx)agentCtrl.syncWithContext(ctx);
  logToFile("agent-chat",`after sync: sessions=[${agentCtrl.list().map(a=>a.agentName).join(",")}]`);
  const agent=agentCtrl.get(agentName);
  if(!agent){logToFile("agent-chat",`AGENT NOT FOUND: "${agentName}"`);return e.status(404).json({error:`Agent "${agentName}" not found in scene`})}

  log.agent('info',`user→${agentName}: ${message.slice(0,100)}`);
  agentCtrl.sendMessage("user",agentName,message);

  e.setHeader("Content-Type","text/event-stream");
  e.setHeader("Cache-Control","no-cache");
  e.setHeader("Connection","keep-alive");
  e.setHeader("X-Accel-Buffering","no");
  e.flushHeaders();

  let clientGone=false;
  function send(type,data){
    if(clientGone||e.writableEnded)return;
    try{e.write(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`)}
    catch(err){clientGone=true;log.agent('warn',`${agentName} SSE write error: ${err.message}`)}
  }

  agent.run(message,(type,data)=>{
    send(type,data);
    // When agent finishes, log response + auto-trigger @mentioned agents
    if(type==="done"&&data.text){
      agentCtrl.sendMessage(agentName,null,data.text.slice(0,500));
      const mentioned=agentCtrl.parseAndForwardMentions(agentName,data.text);
      for(const targetName of mentioned){
        const target=agentCtrl.get(targetName);
        if(target&&target.status!=="running"){
          setTimeout(()=>_triggerAgent(target,`Message from @${agentName}: ${data.text.slice(0,300)}`),1000);
        }
      }
    }
  }).then(()=>{
    if(!e.writableEnded)e.end();
  }).catch(err=>{
    send("error",{message:err.message});
    if(!e.writableEnded)e.end();
  });

  e.on("close",()=>{
    clientGone=true;
    log.agent('info',`${agentName} SSE closed (agent status: ${agent.status})`);
    // Don't kill the agent — let it finish in background so UE actions complete
  });
});

app.post("/api/agent-stop",(s,e)=>{
  const{agentName}=s.body;
  if(!agentName)return e.status(400).json({error:"agentName required"});
  agentCtrl.stop(agentName);
  e.json({ok:true});
});

// NOTE: these endpoints are not called by the frontend (data flows via SSE).
// Kept for debugging / external tool access.
app.get("/api/agent-history/:name",(s,e)=>{
  const agent=agentCtrl.get(s.params.name);
  if(!agent)return e.status(404).json({error:"Agent not found"});
  e.json({agentName:agent.agentName,history:agent.history});
});
app.get("/api/agent-activity/:name",(s,e)=>{
  e.json(agentCtrl.getActivity(s.params.name));
});

// ── PIE status ────────────────────────────────────────────────────────────
app.get("/api/pie-status",(_,e)=>{
  e.json({active:_cachedPie});
});

// Start PIE via UE Python API
app.post("/api/pie-start",async(req,res)=>{
  const script=`
import unreal
subsystem = unreal.get_editor_subsystem(unreal.PlayWorldEditorSubsystem)
params = unreal.RequestPlaySessionParams()
params.world_type = unreal.PlaySessionWorldType.PLAY_IN_EDITOR
subsystem.request_play_session(params)
print("PIE started")
`.trim();
  try{
    const sock=new(require('net').Socket)();
    const timer=setTimeout(()=>{sock.destroy();res.status(503).json({error:'timeout'})},8000);
    sock.connect(parseInt(UNREAL_PORT),UNREAL_HOST,()=>{
      sock.write(JSON.stringify({type:'execute_python_script',params:{script}})+'\n');
    });
    let buf='';
    sock.on('data',d=>{buf+=d.toString();try{const r=JSON.parse(buf);clearTimeout(timer);sock.destroy();res.json({ok:true,result:r})}catch{}});
    sock.on('error',e=>{clearTimeout(timer);res.status(503).json({error:e.message})});
  }catch(e){res.status(503).json({error:e.message})}
});

// ── Inter-agent communication ────────────────────────────────────────────
app.post("/api/agent-message",(s,e)=>{
  const{from,to,text}=s.body;
  if(!from||!text)return e.status(400).json({error:"from and text required"});
  const msg=agentCtrl.sendMessage(from,to||null,text);
  e.json(msg);
});

// Force re-sync context from UE (re-snapshot scene and sync agents)
app.post('/api/context-snapshot', async(req,res) => {
  try {
    const snap = await snapshotScene(STUDIO_SESSION);
    if (snap) {
      ctxManager.updateFromSnapshot(STUDIO_SESSION, snap);
      const ctx = ctxManager.getState(STUDIO_SESSION);
      if (ctx) agentCtrl.syncWithContext(ctx);
    }
    res.json({ ok: true, agents: agentCtrl.list().length });
  } catch(e) { res.status(503).json({ error: e.message }); }
});

// Broadcast + auto-trigger: send message to agents and run their turns
// @all → one message in chat log, all agents triggered
// @AgentName → one message, that agent triggered
app.post("/api/agent-broadcast",(s,e)=>{
  const{text,target,sessionId}=s.body;
  if(!text)return e.status(400).json({error:"text required"});
  const ctx=ctxManager.getState(sessionId||STUDIO_SESSION);
  if(ctx)agentCtrl.syncWithContext(ctx);
  const sessions=agentCtrl.list();
  let targets;
  if(!target||target==="all"){
    targets=sessions.map(s=>s.agentName);
    // ONE @all message — not N individual messages
    agentCtrl.sendMessage("user",null,text);
  }else{
    targets=[target];
    agentCtrl.sendMessage("user",target,text);
  }
  // Respond immediately — agent turns run in background
  const triggered=[];
  for(const name of targets){
    const agent=agentCtrl.get(name);
    if(!agent||agent.status==="running")continue;
    triggered.push(name);
    _triggerAgent(agent,text);
  }
  e.json({ok:true,triggered,skipped:targets.filter(t=>!triggered.includes(t))});
});

// Shared helper: trigger an agent turn and auto-forward @mentions
function _triggerAgent(agent,message){
  agent.run(message,(type,data)=>{
    if(type==="done"&&data.text){
      agentCtrl.sendMessage(agent.agentName,null,data.text.slice(0,500));
      // Auto-trigger mentioned agents
      const mentioned=agentCtrl.parseAndForwardMentions(agent.agentName,data.text);
      for(const targetName of mentioned){
        const target=agentCtrl.get(targetName);
        if(target&&target.status!=="running"){
          // Small delay so the current agent's turn fully completes first
          setTimeout(()=>_triggerAgent(target,`Message from @${agent.agentName}: ${data.text.slice(0,300)}`),1000);
        }
      }
    }
  }).catch(err=>{log.agent('error',`trigger ${agent.agentName}: ${err.message}`)});
}

app.get("/api/agent-chat-log",(s,e)=>{
  const since=parseInt(s.query.since||"0",10);
  e.json(agentCtrl.getPublicChat(since));
});
app.get("/api/agents",(s,e)=>{e.json(agentManager.list())}),app.post("/api/agents",(s,e)=>{const t=agentManager.register(s.body);e.json(t)}),app.patch("/api/agents/:id",(s,e)=>{const{enabled:t}=s.body;if(typeof t=="boolean"){const o=agentManager.toggleEnabled(s.params.id,t);return o?e.json(o):e.status(404).json({error:"Agent not found"})}const n=agentManager.register({id:s.params.id,...s.body});e.json(n)}),app.post("/api/arena/battles/:id/run",async(s,e)=>{const t=arenaManager.getBattle(s.params.id);if(!t)return e.status(404).json({error:"Battle not found"});if(t.status==="voted")return e.status(400).json({error:"Battle already completed"});e.setHeader("Content-Type","text/event-stream"),e.setHeader("Cache-Control","no-cache"),e.setHeader("Connection","keep-alive"),e.flushHeaders();function n(o,i){e.writableEnded||e.write(`event: ${o}
data: ${JSON.stringify(i)}

`)}try{const o=await agentManager.runBattle(t.prompt,t.skills,ARENA_SYSTEM_PROMPT,(a,c)=>n("progress",{phase:a,...c}));arenaManager.submitSceneForBattle(t.id,"a",o.side_a),arenaManager.submitSceneForBattle(t.id,"b",o.side_b);const i=arenaManager.getBattle(t.id);n("complete",i)}catch(o){n("error",{message:o.message})}e.end()}),app.post("/api/arena/run",async(s,e)=>{const{prompt:t,skills:n}=s.body;if(!t)return e.status(400).json({error:"prompt required"});const o=arenaManager.createBattle(t,n||[]);e.setHeader("Content-Type","text/event-stream"),e.setHeader("Cache-Control","no-cache"),e.setHeader("Connection","keep-alive"),e.flushHeaders();function i(a,c){e.writableEnded||e.write(`event: ${a}
data: ${JSON.stringify(c)}

`)}i("battle_created",{battleId:o.id,prompt:t});try{const a=await agentManager.runBattle(t,n||[],ARENA_SYSTEM_PROMPT,(m,_)=>i("progress",{phase:m,..._}));arenaManager.submitSceneForBattle(o.id,"a",a.side_a),arenaManager.submitSceneForBattle(o.id,"b",a.side_b);const c=arenaManager.getBattle(o.id);i("complete",c)}catch(a){i("error",{message:a.message})}e.end()}),app.get("/api/assets",(s,e)=>{try{const t=JSON.parse(fs.readFileSync(path.join(__dirname,"assets.json"),"utf-8")),n={};for(const[o,i]of Object.entries(t)){const a={description:i.description||"",items:[]};o==="buildings"&&i.ids?(a.items=i.ids.map(c=>{const _=`BP_Building_${String(c).padStart(2,"0")}`;return{id:_,path:`/Game/CityDatabase/blueprints/${_}.${_}_C`}}),i.notes&&(a.description+=" "+i.notes)):i.items&&(a.items=i.items.map(c=>{if(typeof c=="string"){const m=c.split("/");return{id:m[m.length-1].split(".")[0],path:c}}return c})),n[o]=a}e.json(n)}catch(t){e.status(500).json({error:t.message})}}),app.get("/api/mock/next-input",(s,e)=>{if(!MOCK_MODE||!mockReplay)return e.json({input:null,hasMore:false});e.json({input:mockReplay.peekNextInput(),hasMore:mockReplay.hasMore(),index:mockReplay.currentIndex})});app.get("/api/coding-agents",(s,e)=>{try{const reg=JSON.parse(fs.readFileSync(path.join(__dirname,"coding-agents.json"),"utf-8")),agents=reg.agents||{},out={};for(const[id,a]of Object.entries(agents))out[id]={label:a.label||id,defaultModel:a.defaultModel||"",models:Array.isArray(a.models)?a.models:[]};e.set("Cache-Control","no-store");e.json({default:reg.default||"claude",agents:out})}catch(err){e.status(500).json({error:err.message})}});
app.get("/api/scene-loop",(req,res)=>{res.json({enabled:sceneLoopEnabled,mode:sceneLoopMode,intentSummary:intentStore.get(STUDIO_SESSION)||null});});
app.post("/api/scene-loop",(req,res)=>{const b=req.body||{};let md=_normalizeMode(b.mode);if(md===null&&typeof b.enabled==="boolean")md=b.enabled?"text_loop":"vanilla";if(md===null)return res.status(400).json({error:"mode required: vanilla|text_loop|visual_loop"});sceneLoopMode=md;sceneLoopEnabled=(md!=="vanilla");logToFile("scene-loop","mode set to "+md);res.json({enabled:sceneLoopEnabled,mode:sceneLoopMode});});
app.delete("/api/scene-loop",(req,res)=>{intentStore.delete(STUDIO_SESSION);res.json({ok:true});});
app.get("/api/dynamic-skills",(req,res)=>{res.json({enabled:DYNAMIC_SKILLS});});
app.post("/api/dynamic-skills",(req,res)=>{const b=req.body||{};if(typeof b.enabled==="boolean")DYNAMIC_SKILLS=b.enabled;res.json({enabled:DYNAMIC_SKILLS});});
app.delete("/api/dynamic-skills",(req,res)=>{try{sessionSkillManager.clearSession(STUDIO_SESSION)}catch(_){}res.json({ok:true});});
app.post("/api/chat",async(s,e)=>{const{message:t,sessionId:n,skills:o,feedback:i}=s.body;if(!t)return e.status(400).json({error:"message required"});const _loopMode=_normalizeMode(s.body&&s.body.loopMode)||((s.body&&s.body.useLoop===false)?"vanilla":((s.body&&s.body.useLoop===true)?"text_loop":sceneLoopMode));if(_loopMode==="visual_loop"){try{return void handleVisualSceneLoop(s,e,{STUDIO_SESSION,logToFile,intentStore})}catch(_err){logToFile("scene-loop","visual dispatch error: "+_err.message);if(!e.headersSent)e.status(500).json({error:_err.message});else{try{e.end()}catch(_){}}return;}}if(_loopMode==="text_loop"){try{return void handleSceneLoop(s,e,{STUDIO_SESSION,logToFile,intentStore})}catch(_err){logToFile("scene-loop","text dispatch error: "+_err.message);if(!e.headersSent)e.status(500).json({error:_err.message});else{try{e.end()}catch(_){}}return;}}e.setHeader("Content-Type","text/event-stream"),e.setHeader("Cache-Control","no-cache"),e.setHeader("Connection","keep-alive"),e.setHeader("X-Accel-Buffering","no"),e.flushHeaders();if(MOCK_MODE&&mockReplay){const m=mockReplay.getNextMessage();if(!m)return e.status(500).json({error:"No more mock messages"});logToFile("chat",`[MOCK] User: "${t.slice(0,200)}"`);function s(d,r){e.writableEnded||e.write(`event: ${d}\ndata: ${JSON.stringify(r)}\n\n`)}s("system",{sessionId:n||"mock-session",mcpServers:[{name:"simworld",status:"connected"}]});(async()=>{await new Promise(r=>setTimeout(r,300));let _tidx=0;for(const _step of(m.steps||[])){if(_step.type==="thinking"){for(const _c of _step.text){s("text",{delta:_c});await new Promise(_p=>setTimeout(_p,12))}s("text",{delta:"\n"})}else if(_step.type==="tool"){await new Promise(_p=>setTimeout(_p,600));const _tid=`mock-${_tidx++}-${Date.now()}`;const _dn=_step.name.replace(/^mcp__[a-zA-Z0-9_]+__/,"");s("tool_start",{id:_tid,name:_step.name,displayName:_dn});_step.input&&s("tool_input",{id:_tid,delta:typeof _step.input=="string"?_step.input:JSON.stringify(_step.input)});let _rr;try{_rr=await mockExecutor.execute(_step.name,_step.input)}catch(_e){_rr=null}if(!_rr||_rr.status==="error"){_rr=_step.result||{}}s("tool_result",{toolUseId:_tid,result:typeof _rr=="string"?_rr:JSON.stringify(_rr),isError:!1});if(_dn==="verify_scene"){let _mfb="",_mss="";try{const _mro=typeof _step.result=="string"?JSON.parse(_step.result):(_step.result||{});_mfb=_mro.feedback||""}catch(_me){}try{await mockExecutor.execute("take_screenshot",{});if(fs.existsSync(SCREENSHOT_DIR)){const _fs=fs.readdirSync(SCREENSHOT_DIR).filter(f=>f.endsWith(".png")).map(f=>({fp:path.join(SCREENSHOT_DIR,f),t:fs.statSync(path.join(SCREENSHOT_DIR,f)).mtimeMs})).sort((a,b)=>b.t-a.t);if(_fs.length)_mss=`/api/screenshot/file?path=${encodeURIComponent(_fs[0].fp)}`}}catch(_e){}s("verifier_start",{toolUseId:_tid,screenshot:_mss});await new Promise(_p=>setTimeout(_p,1800));s("verifier_result",{toolUseId:_tid,feedback:_mfb,screenshot:_mss})}await new Promise(_p=>setTimeout(_p,250))}else if(_step.type==="text"){for(const _c of _step.content){s("text",{delta:_c});await new Promise(_p=>setTimeout(_p,18))}s("text",{delta:"\n"})}}s("done",{sessionId:n||"mock-session",isError:!1,costUsd:0,latestScreenshot:null});e.end()})().catch(err=>{console.error("[mock] error:",err.message);e.writableEnded||e.end()});return}function a(d,r){e.writableEnded||e.write(`event: ${d}
data: ${JSON.stringify(r)}

`)}const c=setInterval(()=>{e.writableEnded||e.write(`: ping

`)},5e3);let _sk=o;const _proceed=async()=>{let m=ARENA_SYSTEM_PROMPT;if(_sk&&_sk.length>0){const d=skillRegistry.compose(_sk);d&&(m+=`

## ACTIVE SKILLS (reference documentation)
`+d)}{const _ctx=ctxManager.renderForPrompt(STUDIO_SESSION);if(_ctx)m+="\n\n"+_ctx;}i&&(m+=`

## USER FEEDBACK ON CURRENT SCENE
The user is providing feedback on the current scene. Modify the scene based on this feedback. Do NOT start from scratch \u2014 refine what exists.
Feedback: ${i}`);const _agentId=(s.body&&s.body.agent)||"claude";const _CHAT_RUNNERS={gemini:{mod:"./gemini-runner",fn:"runGeminiChat"},codex:{mod:"./codex-runner",fn:"runCodexChat"},opencode:{mod:"./opencode-runner",fn:"runOpenCodeChat"},cursor:{mod:"./cursor-runner",fn:"runCursorChat"},grok:{mod:"./grok-runner",fn:"runGrokChat"}};if(_CHAT_RUNNERS[_agentId]){clearInterval(c);try{const _R=_CHAT_RUNNERS[_agentId];require(_R.mod)[_R.fn]({req:s,res:e,body:s.body,systemPrompt:m,ctx:{ctxManager,snapshotScene,STUDIO_SESSION,MCP_CONFIG,ARENA_ROOT,UNREAL_PORT,LOG_DIR,SCREENSHOT_DIR,_chatProcs,logToFile,MOCK_MODE}});}catch(err){logToFile(_agentId,"dispatch error: "+err.message);a("text",{delta:`\n\n⚠️ ${_agentId} runner failed to start: ${err.message}\n`});a("done",{sessionId:STUDIO_SESSION,isError:true,latestScreenshot:null});e.end();}return;}const _amRaw=(s.body&&(s.body.assetRetrievalMode||s.body.assetMode))||process.env.ASSET_RETRIEVAL_MODE;if(!MOCK_MODE&&_amRaw){try{const _ar=require("./asset-retrieval");const _am=_ar.resolveAssetMode(_amRaw);if(_am&&_am!=="off"){const _b=await _ar.buildPromptBlock(t,_am,{model:(s.body&&s.body.model),log:x=>logToFile("retrieval",x)});if(_b){m+="\n\n"+_b;logToFile("retrieval","injected palette ("+_am+", "+_b.length+" chars)");}}}catch(_e){logToFile("retrieval","inject skipped: "+(_e&&_e.message));}}const _=["-p",t,"--output-format","stream-json","--include-partial-messages","--verbose","--dangerously-skip-permissions","--mcp-config",MCP_CONFIG,"--append-system-prompt",m];const CLAUDE_MODEL=((s.body&&s.body.model)||process.env.CLAUDE_MODEL||"");if(CLAUDE_MODEL)_.push("--model",CLAUDE_MODEL);/* SECURITY: scene-gen agent — block all file/shell/web tools so it can ONLY use the simworld MCP tools (cannot read or modify Studio source). --disallowedTools is variadic so it must come last. */(function(){try{var _proj=(process.env.UE_PROJECT_PATH||"").replace(/\/+$/,"");var _cs=new Set();if(_proj){_cs.add(_proj+"/Content");try{_cs.add(fs.realpathSync(_proj+"/Content"))}catch(_e){}}var _dy=[];_cs.forEach(function(c){var a="//"+c.replace(/^\/+/,"");_dy.push("Read("+a+"/**)","Glob("+a+"/**)","Grep("+a+"/**)")});_dy.push("Read(**/Content/**)","Glob(**/Content/**)","Grep(**/Content/**)");_.push("--settings",JSON.stringify({permissions:{deny:_dy}}))}catch(_e){}})();_.push("--disallowedTools","Bash","BashOutput","KillShell","Edit","MultiEdit","Write","NotebookEdit","Task","TodoWrite","WebFetch","WebSearch");const h=Object.assign({},process.env);Object.keys(h).forEach(k=>{if(k.startsWith("CLAUDE"))delete h[k]});if(/glm/i.test(CLAUDE_MODEL)||CLAUDE_MODEL.indexOf("z-ai/")===0||CLAUDE_MODEL.indexOf("openrouter/")===0){h.ANTHROPIC_BASE_URL="https://openrouter.ai/api";h.ANTHROPIC_AUTH_TOKEN=process.env.OPENROUTER_API_KEY||h.ANTHROPIC_AUTH_TOKEN||"";h.ANTHROPIC_API_KEY="";}logToFile("chat",`User: "${t.slice(0,200)}" sessionId=${n||"new"}`);try{fs.writeFileSync(path.join(LOG_DIR,"raw_latest.jsonl"),"")}catch{}const _sbc=require("./agent-sandbox").sandboxedSpawn(CLAUDE_BIN,_,path.resolve(__dirname,".."));const g=spawn(_sbc.cmd,_sbc.args,{cwd:path.resolve(__dirname,".."),env:h,stdio:["ignore","pipe","pipe"]});const _pp=_chatProcs.get(n||"_global");if(_pp&&!_pp.killed){try{_pp.kill("SIGTERM")}catch{}}
_chatProcs.set(n||"_global",g);
g.on("exit",()=>_chatProcs.delete(n||"_global"));let f="",w=new Set,v=new Set,S=n||null,b=null,emittedText="";const toolInputs=new Map;function emitTextDelta(delta,full=false){if(typeof delta!=="string"||!delta)return;if(full){if(emittedText&&delta.startsWith(emittedText)){const rest=delta.slice(emittedText.length);if(rest){emittedText+=rest,a("text",{delta:rest})}return}if(emittedText&&emittedText.includes(delta))return}emittedText+=delta,a("text",{delta})}function j(d){if(d=d.trim(),!d)return;try{fs.appendFileSync(path.join(LOG_DIR,"raw_latest.jsonl"),d+`
`)}catch{}let r;try{r=JSON.parse(d)}catch{return}const u=r.type;if(u==="system"&&r.subtype==="init"){r.session_id&&(S=r.session_id);ctxManager.resolveSession(STUDIO_SESSION);ctxManager.beginRound(STUDIO_SESSION);const p=(r.mcp_servers||[]).map(l=>`${l.name}:${l.status}`);a("system",{sessionId:r.session_id,mcpServers:r.mcp_servers||[]}),logToFile("claude",`Session ${r.session_id} | MCP: ${p.join(", ")}`)}else if(u==="stream_event"){const p=r.event||{};if(p.type==="content_block_delta"&&p.delta?.type==="text_delta"&&emitTextDelta(p.delta.text),p.type==="content_block_delta"&&p.delta?.type==="thinking_delta"&&a("text",{delta:p.delta.thinking}),p.type==="content_block_start"&&p.content_block?.type==="tool_use"){const l=p.content_block;if(!w.has(l.id)){w.add(l.id);const y=l.name.replace(/^mcp__\w+__/,"");a("tool_start",{id:l.id,name:l.name,displayName:y}),logToFile("tool",`Starting: ${l.name}`);if(y==="verify_scene"){v.add(l.id);a("verifier_start",{toolUseId:l.id})}}}p.type==="content_block_delta"&&p.delta?.type==="input_json_delta"&&a("tool_input",{delta:p.delta.partial_json})}else if(u==="assistant"){const p=r.message?.content||[];for(const l of p)if(l.type==="tool_use"){const y=l.name.replace(/^mcp__\w+__/,"");a("tool_details",{id:l.id,name:l.name,displayName:y,input:l.input});if(["spawn_blueprint_actor","spawn_actor","spawn_agent","delete_actor","delete_all_spawned","setup_environment"].includes(y)){logToFile("ctx","cached tool_use: "+y+" id="+l.id+" input="+JSON.stringify(l.input).slice(0,200));toolInputs.set(l.id,{name:y,input:l.input})}}else l.type==="text"&&l.text&&emitTextDelta(l.text,true)}else if(u==="user"){const p=r.message?.content||[];for(const l of p)if(l.type==="tool_result"){const y=Array.isArray(l.content)?l.content.map(P=>P.text||"").join(""):String(l.content||""),B=y.match(/([\/][\w\/\-._]+\.png)/);B&&fs.existsSync(B[1])&&(b=B[1],a("screenshot",{toolUseId:l.tool_use_id,filepath:`/api/screenshot/file?path=${encodeURIComponent(b)}`})),a("tool_result",{toolUseId:l.tool_use_id,result:y.slice(0,2e3),isError:l.is_error||!1}),logToFile("tool_result",`${l.tool_use_id?.slice(0,8)} \u2192 ${y.slice(0,300)}`);{const _st=toolInputs.get(l.tool_use_id);if(_st){logToFile("ctx","tool_result for "+_st.name+" toolUseId="+l.tool_use_id+" is_error="+l.is_error+" S="+S);if(!l.is_error&&S){try{const _tr=JSON.parse(y);logToFile("ctx",_st.name+" status="+_tr.status);if(_tr.status==="success"){if(_st.name==="spawn_blueprint_actor"||_st.name==="spawn_actor"||_st.name==="spawn_agent"){const _an=_st.input.actor_name||_st.input.agent_name||_st.input.name;const _cls=_st.input.blueprint_id||_st.input.static_mesh||_st.input.agent_type||"";const _cat=_st.name==="spawn_agent"?"agent":undefined;logToFile("ctx","addActor: "+_an+" cls="+_cls+" cat="+(_cat||"auto"));ctxManager.addActor(STUDIO_SESSION,{name:_an,cls:_cls,category:_cat,location:_st.input.location})}else if(_st.name==="delete_actor")ctxManager.removeActor(STUDIO_SESSION,_st.input.name);else if(_st.name==="delete_all_spawned")ctxManager.clearAllSpawned(STUDIO_SESSION);else if(_st.name==="setup_environment"){logToFile("ctx","setEnvironmentReady");ctxManager.setEnvironmentReady(STUDIO_SESSION)}const _state=ctxManager.getState(STUDIO_SESSION);logToFile("ctx","state after update: agents="+(_state?.agents?.length)+" objects="+(_state?.objects?.length)+" updatedAt="+_state?.updatedAt)}}catch(_e){logToFile("ctx","parse error: "+_e.message)}}toolInputs.delete(l.tool_use_id)}}if(v.has(l.tool_use_id)){let _fb="",_ss="";try{const _ro=JSON.parse(y);_fb=_ro.feedback||"";_ss=_ro.screenshot||""}catch(_e){}a("verifier_result",{toolUseId:l.tool_use_id,feedback:_fb,screenshot:_ss?`/api/screenshot/file?path=${encodeURIComponent(_ss)}`:""})}}}else if(u==="result"){gotResultEvent=true;clearInterval(idleTimer);S=r.session_id;const p=r.is_error||r.subtype==="error_during_turn";r.result&&typeof r.result==="string"&&emitTextDelta(r.result+"\n",true),logToFile("claude",`Result: subtype=${r.subtype} session=${S} cost=$${r.total_cost_usd||"?"}`),logToFile("result",JSON.stringify({subtype:r.subtype,cost:r.total_cost_usd,duration:r.duration_ms}).slice(0,500)),T(),clearInterval(c);const _finish=()=>{const _st=ctxManager.getState(STUDIO_SESSION);logToFile("ctx","DONE: session="+S+" agents="+(_st?.agents?.length)+" objects="+(_st?.objects?.length)+" updatedAt="+_st?.updatedAt);a("done",{sessionId:STUDIO_SESSION,isError:p,costUsd:r.total_cost_usd,latestScreenshot:b?`/api/screenshot/file?path=${encodeURIComponent(b)}`:k()});e.end()};if(!MOCK_MODE){logToFile("ctx","calling snapshotScene for "+S);snapshotScene(STUDIO_SESSION).then(r=>{logToFile("ctx","snapshotScene result: "+(r?"success":"null"));_finish()}).catch(err=>{logToFile("ctx","snapshotScene error: "+err.message);_finish()})}else _finish()}}function T(){let d=null;if(fs.existsSync(SCREENSHOT_DIR))try{const r=fs.readdirSync(SCREENSHOT_DIR).filter(u=>u.endsWith(".png")).map(u=>({fp:path.join(SCREENSHOT_DIR,u),time:fs.statSync(path.join(SCREENSHOT_DIR,u)).mtimeMs})).filter(({time:u})=>Date.now()-u<18e5);for(const u of r)(!d||u.time>d.time)&&(d=u)}catch{}d&&(b=d.fp)}function k(){return T(),b?`/api/screenshot/file?path=${encodeURIComponent(b)}`:null}const CLAUDE_IDLE_TIMEOUT_MS=parseInt(process.env.CLAUDE_IDLE_TIMEOUT_MS||'1800000',10);let lastOutputTime=Date.now();const idleTimer=setInterval(()=>{if(Date.now()-lastOutputTime>CLAUDE_IDLE_TIMEOUT_MS&&!gotResultEvent){logToFile("claude",`Idle timeout (${CLAUDE_IDLE_TIMEOUT_MS/1000}s no output), killing process`);clearInterval(idleTimer);g.kill("SIGTERM")}},10000);g.stdout.on("data",d=>{lastOutputTime=Date.now();f+=d.toString();const r=f.split(`
`);f=r.pop()??"";for(const u of r)j(u)});let stderrBuf="";g.stderr.on("data",d=>{lastOutputTime=Date.now();const r=d.toString().trim();if(r){stderrBuf+=r+"\n";logToFile("stderr",r.slice(0,300))}});let gotResultEvent=false;g.on("close",d=>{clearInterval(c),clearInterval(idleTimer);/* P0-3: cleaned up via g.on(exit) */f.trim()&&j(f),logToFile("claude",`Process exited with code ${d} gotResult=${gotResultEvent}`);if(gotResultEvent)return;if(!e.writableEnded){const errDetail=stderrBuf.slice(0,400).trim()||(d!==0?`exit code ${d}`:`no output received`);a("text",{delta:`\n\n⚠️ Agent exited unexpectedly: ${errDetail}\n`});a("done",{sessionId:STUDIO_SESSION,isError:true,latestScreenshot:k()});e.end()}})};const _ssMode=(s.body&&s.body.skillSelectionMode)||null;if(_ssMode==="auto"){a("skill_selection_start",{});selectSkillsWithClaude({prompt:t,model:(s.body&&s.body.model),claudeBin:CLAUDE_BIN,skillRegistry}).then(_r=>{_sk=Array.isArray(_r.selectedSkillIds)?_r.selectedSkillIds:[];a("skill_selection_done",{mode:"auto",selectedSkills:_sk,reasoning:_r.reasoning||""})}).catch(_er=>{a("skill_selection_error",{message:String((_er&&_er.message)||_er)})}).finally(()=>{try{_proceed()}catch(_pe){try{a("done",{sessionId:STUDIO_SESSION,isError:true,latestScreenshot:null}),e.end()}catch(_x){}}})}else{if(_ssMode==="manual")a("skill_selection_done",{mode:"manual",selectedSkills:_sk||[]});_proceed()}e.on("close",()=>{if(!e.writableEnded){clearInterval(c);try{e.end()}catch{}logToFile("claude","Browser closed SSE — agent continues in background (use /api/chat-stop to kill)")}})});// Catch-all 404 for unknown /api/ routes — prevents hanging connections

// ── Session Routes ──────────────────────────────────────────────────────────
// ── Scene Checkpoints ────────────────────────────────────────────────────────
// Per-session scene snapshots + branch tree (parentId). A checkpoint captures the
// user-built actors as a manifest (class + transform + mesh); restore replays them into
// the LIVE world (delete current + respawn). No map load: load_level crashes the headless
// Pixel-Streaming editor (a duplicated UWorld stays resident → UE "old world not cleaned
// up" fatal), whereas spawn/destroy are safe and never interrupt the stream. Checkpoints
// are pure filesystem state (no UE assets), so cleanup is a directory removal.
const CKPT_TTL_MS = parseInt(process.env.CKPT_TTL_MS || String(30 * 60 * 1000), 10);
// Hardening knobs: debounce rapid auto-snapshots + bound the UE scene scan so a capture
// can never iterate/serialize a pathological number of actors on the game thread.
const CKPT_MIN_INTERVAL_MS = parseInt(process.env.CKPT_MIN_INTERVAL_MS || "6000", 10); // reuse last ckpt if fresher
const CKPT_MAX_ACTORS = parseInt(process.env.CKPT_MAX_ACTORS || "1500", 10);            // cap manifest size
const CKPT_MAX_SCAN = parseInt(process.env.CKPT_MAX_SCAN || "20000", 10);               // cap actors examined
// Actor-label prefixes that are base-map infrastructure (not user content): skipped on
// capture and preserved on restore.
const CKPT_INFRA_PREFIXES = ["Floor","SkySphere","Sky","Light","Atmo","Fog","PostProcess",
  "SphereReflection","WorldSettings","Brush","Default","Player","GameMode","Nav","LevelBounds",
  "Landscape","Volume","Note","Camera","Directional","ExponentialHeight"];
const _PY_INFRA = "(" + CKPT_INFRA_PREFIXES.map(p => JSON.stringify(p)).join(",") + ",)";

// All web-initiated UE Python funnels through the shared UeMcpBroker (concurrency=1,
// dedicated execute_python_script slow-lane, bounded queue + 429/queue-deadline
// backpressure) so a heavy script — e.g. a checkpoint scene scan — can't stack with
// other web UE calls and wedge the game thread. Non-fatal contract preserved: resolves
// null on timeout / backpressure / error (callers treat null as "capture failed").
function ueExecScript(script, timeoutMs = 60000) {
  if (ueBroker && typeof ueBroker.send === "function") {
    return ueBroker.send("execute_python_script", { script }, { timeoutMs }).catch(() => null);
  }
  return new Promise((resolve) => {
    const sock = new (require("net").Socket)();
    const timer = setTimeout(() => { try { sock.destroy(); } catch (_e) {} resolve(null); }, timeoutMs);
    let buf = "";
    sock.connect(parseInt(UNREAL_PORT), UNREAL_HOST, () => {
      sock.write(JSON.stringify({ type: "execute_python_script", params: { script } }) + "\n");
    });
    sock.on("data", (d) => {
      buf += d.toString();
      try { const r = JSON.parse(buf); clearTimeout(timer); sock.destroy(); resolve(r); } catch (_e) {}
    });
    sock.on("error", () => { clearTimeout(timer); try { sock.destroy(); } catch (_e) {} resolve(null); });
  });
}
function _ueLogs(r) { try { return (r.result.python_logs || []).join("\n"); } catch (_e) { return ""; } }

let _ueProjectSavedDir = null;
let _ueProjectSavedDirCheckedAt = 0;
async function getDemoVideoRoots() {
  const roots = [path.join(ARENA_ROOT, "results", "demo_videos")];
  try {
    if (!_ueProjectSavedDir && Date.now() - _ueProjectSavedDirCheckedAt > 30000) {
      _ueProjectSavedDirCheckedAt = Date.now();
      const r = await ueExecScript("import unreal, os\nprint('PROJECT_SAVED_DIR='+os.path.abspath(unreal.SystemLibrary.get_project_saved_directory()))", 3000);
      const m = _ueLogs(r).match(/PROJECT_SAVED_DIR=(.*)$/m);
      if (m) _ueProjectSavedDir = m[1].trim();
    }
    if (_ueProjectSavedDir) roots.push(path.join(_ueProjectSavedDir, "SimWorldDemos"));
  } catch (_e) {}
  return roots;
}
demoVideos.registerDemoVideoRoutes(app, { getRoots: getDemoVideoRoots });

// Capture the current scene's user-built actors (read-only — never loads or saves a map).
let _ckptCaptureInFlight = null;
async function ckptCaptureManifest() {
  // Single-flight: coalesce concurrent capture requests onto ONE UE scene scan so
  // overlapping auto-snapshots can't stack heavy Python on the game thread.
  if (_ckptCaptureInFlight) return _ckptCaptureInFlight;
  const script = [
    "import unreal, json",
    "eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "INFRA=" + _PY_INFRA,
    "CAP=" + CKPT_MAX_ACTORS + "; MAXSCAN=" + CKPT_MAX_SCAN,
    "items=[]; scanned=0",
    "for a in eas.get_all_level_actors():",
    "    scanned+=1",
    "    if scanned>MAXSCAN or len(items)>=CAP: break",
    "    try:",
    "        lbl=a.get_actor_label()",
    "        if (not lbl) or lbl.startswith(INFRA): continue",
    "        loc=a.get_actor_location(); rot=a.get_actor_rotation(); scl=a.get_actor_scale3d()",
    "        mesh=''",
    "        smc=a.get_component_by_class(unreal.StaticMeshComponent)",
    "        if smc is not None:",
    "            sm=smc.get_editor_property('static_mesh')",
    "            if sm is not None: mesh=sm.get_path_name()",
    "        items.append({'label':lbl,'cls':a.get_class().get_path_name(),'loc':[round(loc.x,2),round(loc.y,2),round(loc.z,2)],'rot':[round(rot.pitch,3),round(rot.yaw,3),round(rot.roll,3)],'scl':[round(scl.x,4),round(scl.y,4),round(scl.z,4)],'mesh':mesh})",
    "    except Exception as _e:",
    "        pass",
    "print('CKPT_MANIFEST='+json.dumps(items))",
  ].join("\n");
  _ckptCaptureInFlight = (async () => {
    const r = await ueExecScript(script, 60000);
    const m = _ueLogs(r).match(/CKPT_MANIFEST=(.*)$/m);
    if (!m) return null;
    try { return JSON.parse(m[1]); } catch (_e) { return null; }
  })();
  try { return await _ckptCaptureInFlight; }
  finally { _ckptCaptureInFlight = null; }
}

// Restore: delete current user actors, then respawn the checkpoint's manifest. No map load.
async function ckptRestoreManifest(manifest) {
  const script = [
    "import unreal, json",
    "eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "INFRA=" + _PY_INFRA,
    "for a in list(eas.get_all_level_actors()):",
    "    try:",
    "        lbl=a.get_actor_label()",
    "        if lbl and not lbl.startswith(INFRA): eas.destroy_actor(a)",
    "    except Exception as _e:",
    "        pass",
    "unreal.SystemLibrary.collect_garbage()",
    "items=json.loads(" + JSON.stringify(JSON.stringify(manifest || [])) + ")",
    "n=0",
    "for it in items:",
    "    try:",
    "        cls=unreal.load_object(None, it['cls'])",
    "        if cls is None: continue",
    "        a=eas.spawn_actor_from_class(cls, unreal.Vector(it['loc'][0],it['loc'][1],it['loc'][2]), unreal.Rotator(it['rot'][0],it['rot'][1],it['rot'][2]))",
    "        if a is None: continue",
    "        a.set_actor_scale3d(unreal.Vector(it['scl'][0],it['scl'][1],it['scl'][2]))",
    "        try: a.set_actor_label(it['label'])",
    "        except Exception: pass",
    "        if it.get('mesh'):",
    "            smc=a.get_component_by_class(unreal.StaticMeshComponent)",
    "            m=unreal.load_asset(it['mesh'])",
    "            if smc is not None and m is not None: smc.set_static_mesh(m)",
    "        n+=1",
    "    except Exception as _e:",
    "        pass",
    "print('CKPT_RESTORE_OK spawned='+str(n))",
  ].join("\n");
  const r = await ueExecScript(script, 90000);
  return _ueLogs(r).includes("CKPT_RESTORE_OK");
}

// Clear all user-built actors → blank scene (used by the chat Reset button).
async function ckptClearScene() {
  const script = [
    "import unreal",
    "eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "INFRA=" + _PY_INFRA,
    "n=0",
    "for a in list(eas.get_all_level_actors()):",
    "    try:",
    "        lbl=a.get_actor_label()",
    "        if lbl and not lbl.startswith(INFRA): eas.destroy_actor(a); n+=1",
    "    except Exception as _e:",
    "        pass",
    "unreal.SystemLibrary.collect_garbage()",
    "print('CKPT_SCENE_CLEARED '+str(n))",
  ].join("\n");
  const r = await ueExecScript(script, 60000);
  return _ueLogs(r).includes("CKPT_SCENE_CLEARED");
}

// POST /api/checkpoints — snapshot current scene (frontend calls this after a scene-changing turn)
app.post("/api/checkpoints", async (req, res) => {
  const body = req.body || {};
  const sid = body.sessionId || STUDIO_SESSION;
  const owner = body.ownerId || sid;
  let thumb = null;
  if (body.thumbnailPath && typeof body.thumbnailPath === "string" && body.thumbnailPath.endsWith(".png") && fs.existsSync(body.thumbnailPath)) thumb = body.thumbnailPath;
  // Debounce: rapid scene-changing turns shouldn't each fire a UE scene scan. If the
  // session's latest checkpoint is fresher than CKPT_MIN_INTERVAL_MS, reuse it instead
  // of scanning UE again. (force=true bypasses, e.g. an explicit user snapshot.)
  if (!body.force) {
    try {
      const recs = checkpointManager.list(sid, owner);
      const last = recs && recs.length ? recs[recs.length - 1] : null;
      if (last && Date.now() - new Date(last.createdAt).getTime() < CKPT_MIN_INTERVAL_MS) {
        return res.status(200).json({ ...last, throttled: true });
      }
    } catch (_e) {}
  }
  try {
    const manifest = await ckptCaptureManifest();
    if (manifest === null) return res.status(502).json({ error: "UE scene capture failed" });
    const rec = await checkpointManager.create({
      sessionId: sid, ownerId: owner, parentId: body.parentCheckpointId || null,
      messageId: body.messageId || null,
      prompt: body.prompt || "", turnIndex: body.turnIndex || 0,
      manifest, chatHistory: body.chatHistory || null, thumbnailSrcPath: thumb,
    });
    await checkpointManager.touch(sid, owner);
    logToFile("ckpt", "created " + rec.id + " (turn " + rec.turnIndex + ", " + rec.actorCount + " actors, parent " + (rec.parentId || "root") + ") for " + sid);
    res.status(201).json(rec);
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// GET /api/checkpoints?sessionId=&ownerId= — list the branch tree
app.get("/api/checkpoints", (req, res) => {
  const sid = req.query.sessionId || STUDIO_SESSION;
  res.json({ sessionId: sid, checkpoints: checkpointManager.list(sid, req.query.ownerId || null) });
});

// GET /api/checkpoints/:sid/:id — full record incl chatHistory
app.get("/api/checkpoints/:sid/:id", (req, res) => {
  const rec = checkpointManager.get(req.params.sid, req.params.id, req.query.ownerId || null);
  if (!rec) return res.status(404).json({ error: "Checkpoint not found" });
  res.json(rec);
});

// GET /api/checkpoints/:sid/:id/thumbnail
app.get("/api/checkpoints/:sid/:id/thumbnail", (req, res) => {
  const p = checkpointManager.getThumbnailPath(req.params.sid, req.params.id);
  if (!p) return res.status(404).json({ error: "No thumbnail" });
  res.sendFile(p);
});

// POST /api/checkpoints/:sid/:id/restore — revert the live UE scene to this checkpoint
app.post("/api/checkpoints/:sid/:id/restore", async (req, res) => {
  const { sid, id } = req.params;
  const owner = (req.body && req.body.ownerId) || req.query.ownerId || null;
  const manifest = checkpointManager.getManifest(sid, id, owner);
  if (manifest === null) return res.status(404).json({ error: "Checkpoint not found" });
  const ok = await ckptRestoreManifest(manifest);
  if (!ok) return res.status(502).json({ error: "UE restore failed" });
  await checkpointManager.touch(sid, owner);
  logToFile("ckpt", "restored " + id + " (" + manifest.length + " actors) for " + sid);
  res.json({ ok: true, checkpoint: checkpointManager.get(sid, id, owner) });
});

// POST /api/scene/reset — wipe all user-built actors so the scene starts from scratch.
app.post("/api/scene/reset", async (req, res) => {
  const ok = await ckptClearScene();
  if (!ok) return res.status(502).json({ error: "UE scene reset failed" });
  res.json({ ok: true });
});

// POST /api/scene/save-as — save the current editor world to /Game/SavedScenes/<name>.umap
// (a new on-disk .umap asset). The original /Game/Main.umap on disk is NOT modified.
// By default the editor stays on the new map after save; pass revert_to_original=true to
// reload /Game/Main back into the editor afterwards.
app.post("/api/scene/save-as", async (req, res) => {
  const name = (req.body && req.body.name || "").trim();
  const revert = !!(req.body && req.body.revert_to_original);
  if (!/^[A-Za-z0-9_\-]+$/.test(name)) {
    return res.status(400).json({ error: "name must be non-empty [A-Za-z0-9_-]" });
  }
  const newPkg = "/Game/SavedScenes/" + name;
  const revertBlock = revert
    ? "    try:\n        unreal.EditorLevelLibrary.load_level('/Game/Main')\n        print('[scene-save-as] reverted editor to /Game/Main')\n    except Exception as _e:\n        print('[scene-save-as] revert failed: ' + str(_e))\n"
    : "";
  const py =
    "import unreal\n" +
    "new_pkg = '" + newPkg + "'\n" +
    "W = unreal.EditorLevelLibrary.get_editor_world()\n" +
    "if W is None:\n" +
    "    print('[ERROR] No editor world available')\n" +
    "else:\n" +
    "    ok = unreal.EditorLoadingAndSavingUtils.save_map(W, new_pkg)\n" +
    "    if ok:\n" +
    "        actors = unreal.EditorLevelLibrary.get_all_level_actors()\n" +
    "        print('[scene-save-as] OK saved ' + str(len(actors)) + ' actors to ' + new_pkg + '.umap')\n" +
    revertBlock +
    "    else:\n" +
    "        print('[ERROR] save_map returned False for ' + new_pkg)\n";
  try {
    const raw = await new Promise((resolve, reject) => {
      const sock = new (require("net").Socket)();
      const timer = setTimeout(() => { sock.destroy(); reject(new Error("UE MCP timeout (180s)")); }, 180000);
      sock.connect(parseInt(UNREAL_PORT), UNREAL_HOST, () => {
        sock.write(JSON.stringify({ type: "execute_python_script", params: { script: py } }) + "\n");
      });
      let buf = "";
      sock.on("data", d => {
        buf += d.toString();
        try { const r = JSON.parse(buf); clearTimeout(timer); sock.destroy(); resolve(r); } catch {}
      });
      sock.on("error", e => { clearTimeout(timer); reject(e); });
      sock.on("close", () => { clearTimeout(timer); try { resolve(JSON.parse(buf)); } catch {} });
    });
    const logs = ((raw && raw.result && raw.result.python_logs) || []).join("\n");
    if (!raw || raw.status !== "success" || logs.includes("[ERROR]")) {
      return res.status(502).json({ error: "save_map failed", details: logs || JSON.stringify(raw).slice(0, 400) });
    }
    res.json({
      ok: true,
      asset_path: newPkg,
      file_hint: newPkg + ".umap",
      reverted_to_main: revert,
      logs,
    });
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});

// GET /api/saved-maps — list persistent .umap scene versions under /Game/SavedScenes
// (these are what `Save As` writes; they survive restarts, unlike per-session checkpoints).
// Listed via UE AssetRegistry so we don't need the project's on-disk content path.
app.get("/api/saved-maps", async (req, res) => {
  const py = [
    "import unreal, json",
    "ar=unreal.AssetRegistryHelpers.get_asset_registry()",
    "ar.scan_paths_synchronous(['/Game/SavedScenes'], True)",  // ensure freshly-saved maps are indexed
    "out=[]",
    "for a in ar.get_assets_by_path('/Game/SavedScenes', recursive=False):",
    "    try: cls=str(a.asset_class_path.asset_name)",
    "    except Exception: cls=str(getattr(a,'asset_class',''))",
    "    if cls=='World':",
    "        out.append({'name':str(a.asset_name),'path':str(a.package_name)})",
    "print('SAVED_MAPS='+json.dumps(sorted(out, key=lambda m: m['name'])))",
  ].join("\n");
  const r = await ueExecScript(py, 30000);
  const m = _ueLogs(r).match(/SAVED_MAPS=(.*)$/m);
  if (!m) return res.status(502).json({ error: "UE saved-map listing failed" });
  try {
    const maps = JSON.parse(m[1]);
    const roots = await getDemoVideoRoots();
    res.json({ maps: demoVideos.attachDemoVideosToMaps(maps, roots) });
  }
  catch (_e) { res.status(502).json({ error: "parse failed" }); }
});

// GET /api/saved-maps/:name/download — stream the raw .umap file for download.
// The .umap lives in the UE project's Content/SavedScenes (outside this repo), so we ask
// UE for its content dir once and cache it. (The .umap references project assets by /Game
// path — this downloads the map file itself, not its dependencies.)
let _ueContentDir = null;
app.get("/api/saved-maps/:name/download", async (req, res) => {
  const name = String(req.params.name || "");
  if (!/^[A-Za-z0-9_\-]+$/.test(name)) return res.status(400).json({ error: "invalid name" });
  try {
    if (!_ueContentDir) {
      const r = await ueExecScript("import unreal, os\nprint('CONTENT_DIR='+os.path.abspath(unreal.Paths.project_content_dir()))", 15000);
      const m = _ueLogs(r).match(/CONTENT_DIR=(.*)$/m);
      if (m) _ueContentDir = m[1].trim();
    }
    if (!_ueContentDir) return res.status(502).json({ error: "could not resolve UE content dir" });
    const file = path.join(_ueContentDir, "SavedScenes", name + ".umap");
    if (!fs.existsSync(file)) return res.status(404).json({ error: "map not found" });
    res.download(file, name + ".umap");
  } catch (e) { res.status(502).json({ error: e.message }); }
});

// ── Task Generation (navmesh build + episode sampling) ──────────────────────
require("./task-gen").registerTaskRoutes(app, { ueExecScript, _ueLogs, taskSetManager });

// ── Agent Training (gym_env experiment engine, live-visualized) ─────────────
require("./training").registerTrainingRoutes(app, {
  taskSetManager,
  canonicalExport: require("./task-gen").canonicalExport,
  ueExecScript,
  UNREAL_PORT,
  logToFile,
});

// DELETE /api/checkpoints/:sid/:id — delete one checkpoint
app.delete("/api/checkpoints/:sid/:id", async (req, res) => {
  const r = await checkpointManager.delete(req.params.sid, req.params.id, req.query.ownerId || null);
  if (r === "forbidden") return res.status(403).json({ error: "Forbidden" });
  if (r === null) return res.status(404).json({ error: "Checkpoint not found" });
  res.json({ ok: true });
});

// DELETE /api/checkpoints/:sid — clear an entire session (new-session / clear chat)
app.delete("/api/checkpoints/:sid", async (req, res) => {
  const r = await checkpointManager.clearSession(req.params.sid, req.query.ownerId || null);
  if (r === "forbidden") return res.status(403).json({ error: "Forbidden" });
  res.json({ ok: true });
});

// Idle-TTL sweep — clear checkpoints of sessions idle beyond TTL
const _ckptSweep = setInterval(() => {
  try { checkpointManager.sweepIdle(CKPT_TTL_MS); } catch (_e) {}
}, 60000);
if (_ckptSweep.unref) _ckptSweep.unref();

// Startup purge — clear any leftovers from a previous run (server-stop / crash recovery)
try { checkpointManager.clearAll(); logToFile("ckpt", "startup purge done"); } catch (_e) {}

// Server-stop cleanup
function _ckptShutdown() { try { checkpointManager.clearAll(); } catch (_e) {} process.exit(0); }
process.on("SIGTERM", _ckptShutdown);
process.on("SIGINT", _ckptShutdown);

let _sessionMgr=null;
try{const{sessionManager}=require('./session-manager');_sessionMgr=sessionManager;}
catch(e){logToFile('session','session-manager not loaded: '+e.message);}

app.post('/api/session/acquire',async(req,res)=>{
  if(!_sessionMgr)return res.json({token:'_dev',slotId:0,totalSlots:1,freeSlots:1,sessionTtlMs:0,dev:true});
  const userId=req.ip||'anon';
  try{
    const rec=await _sessionMgr.acquire(userId);
    res.json({token:rec.token,slotId:rec.slotId,uePorts:rec.uePorts,totalSlots:_sessionMgr.totalSlots,freeSlots:_sessionMgr.freeSlots,sessionTtlMs:parseInt(process.env.SESSION_TTL_MS||'1800000',10)});
  }catch(e){res.status(503).json({error:e.message,code:e.code||'UNAVAILABLE',queueLength:_sessionMgr.queueLength});}
});

app.post('/api/session/heartbeat',(req,res)=>{
  if(!_sessionMgr)return res.json({ok:true,dev:true});
  const tok=(req.headers['x-session-token']||req.body&&req.body.token||'').trim();
  const rec=tok?_sessionMgr.touch(tok):null;
  if(!rec)return res.status(401).json({error:'Session expired or invalid'});
  res.json({ok:true,slotId:rec.slotId,idleMs:Date.now()-rec.lastActivity});
});

app.post('/api/session/release',(req,res)=>{
  if(_sessionMgr){const tok=(req.headers['x-session-token']||req.body&&req.body.token||'').trim();if(tok)_sessionMgr.release(tok);}
  res.json({ok:true});
});

app.get('/api/session/status',(req,res)=>{
  if(!_sessionMgr)return res.json({mode:'single-user'});
  res.json({totalSlots:_sessionMgr.totalSlots,freeSlots:_sessionMgr.freeSlots,activeSessions:_sessionMgr.activeSessions,queueLength:_sessionMgr.queueLength,sessions:_sessionMgr.snapshot()});
});

// ── Asset Catalog API ──────────────────────────────────────────────────────────
const ASSETS_FULL = JSON.parse(fs.readFileSync(path.resolve(__dirname,'assets_full.json'),'utf-8'));

function _flattenAssets(catalog) {
  const out = [];
  // Buildings (numbered IDs)
  if (catalog.buildings?.ids) {
    const pfx = '/Game/CityDatabase/blueprints/';
    for (const id of catalog.buildings.ids) {
      const n = `BP_Building_${String(id).padStart(2,'0')}`;
      out.push({ name:n, dir:pfx, fullPath:`${pfx}${n}.${n}_C`, type:'blueprint', category:'buildings', spawnTool:'spawn_blueprint_actor' });
    }
  }
  // Item-based categories
  const catMeta = {
    trees:           { type:'blueprint',    spawnTool:'spawn_blueprint_actor', icon:'🌳' },
    vehicles:        { type:'blueprint',    spawnTool:'spawn_blueprint_actor', icon:'🛵' },
    street_furniture:{ type:'blueprint',    spawnTool:'spawn_blueprint_actor', icon:'🪑' },
    static_meshes:   { type:'static_mesh', spawnTool:'spawn_actor',           icon:'📦' },
  };
  for (const [cat, meta] of Object.entries(catMeta)) {
    for (const item of (catalog[cat]?.items || [])) {
      const fp = typeof item === 'string' ? item : item.path;
      const segs = fp.split('/'); const filename = segs.pop();
      const dir = segs.join('/') + '/';
      const name = filename.split('.')[0];
      out.push({ name, dir, fullPath:fp, type:meta.type, category:cat, spawnTool:meta.spawnTool, icon:meta.icon });
    }
  }
  // Agents
  for (const ag of (catalog.agents?.items || [])) {
    const segs = ag.path.split('/'); segs.pop();
    const dir = segs.join('/') + '/';
    out.push({ name:ag.id, dir, fullPath:ag.path, type:'agent', category:'agents', spawnTool:'spawn_agent', agentType:ag.type, description:ag.description, icon:'🤖' });
  }
  // Map templates
  for (const m of (catalog.map_templates?.items || [])) {
    const segs = m.path.split('/'); segs.pop();
    const dir = segs.join('/') + '/';
    const name = m.path.split('/').pop();
    out.push({ name, dir, fullPath:m.path, type:'map', category:'maps', spawnTool:null, biome:m.biome, pack:m.pack, icon:'🗺️' });
  }
  return out;
}

const _allAssets = _flattenAssets(ASSETS_FULL);

const ASSET_PREVIEW_VERSION="editor-thumbnail-v2";
const _assetPreviewJobs=new Map();

function _assetPreviewId(assetPath){
  return crypto.createHash("sha1").update(`${ASSET_PREVIEW_VERSION}:${String(assetPath||"")}`).digest("hex").slice(0,20);
}

function _assetPreviewFile(assetPath){
  return path.join(ASSET_PREVIEW_DIR,`${_assetPreviewId(assetPath)}.png`);
}

function _assetPreviewUrl(assetPath){
  return `/asset-previews/${_assetPreviewId(assetPath)}.png`;
}

function _assetPreviewSupported(asset){
  const type=String(asset?.type||"").toLowerCase();
  const fullPath=String(asset?.fullPath||asset?.path||"");
  if(!fullPath.startsWith("/Game/")&&!fullPath.startsWith("/Engine/"))return false;
  return ["blueprint","static_mesh","agent","asset"].includes(type);
}

function _assetPreviewMeta(asset){
  const fullPath=asset?.fullPath||asset?.path;
  const supported=_assetPreviewSupported(asset);
  const filePath=fullPath?_assetPreviewFile(fullPath):null;
  const ready=!!(supported&&filePath&&fs.existsSync(filePath));
  return {
    previewSupported:supported,
    previewStatus:ready?"ready":supported?"missing":"unsupported",
    previewUrl:ready?_assetPreviewUrl(fullPath):null,
    method:ready?"editor_thumbnail":undefined,
  };
}

function _withAssetPreviewMeta(asset){
  return {...asset,..._assetPreviewMeta(asset)};
}

function _ueToolCommand(type,params={},timeoutMs=60000){
  return new Promise((resolve,reject)=>{
    const sock=new(require("net").Socket)();
    let done=false,buf="";
    const finish=(err,value)=>{
      if(done)return;
      done=true;
      clearTimeout(timer);
      try{sock.destroy()}catch(_e){}
      err?reject(err):resolve(value);
    };
    const timer=setTimeout(()=>finish(new Error("timeout")),timeoutMs);
    sock.connect(parseInt(UNREAL_PORT),UNREAL_HOST,()=>{
      sock.write(JSON.stringify({type,params})+"\n");
    });
    sock.on("data",d=>{
      buf+=d.toString();
      try{finish(null,JSON.parse(buf.trim()))}catch(_e){}
    });
    sock.on("error",err=>finish(err));
    sock.on("close",()=>{if(!done){try{finish(null,JSON.parse(buf.trim()))}catch{finish(new Error("empty UE response"))}}});
  });
}

function _waitForFile(filePath,timeoutMs=15000){
  const start=Date.now();
  return new Promise(resolve=>{
    const tick=()=>{
      try{if(fs.existsSync(filePath)&&fs.statSync(filePath).size>0)return resolve(true)}catch(_e){}
      if(Date.now()-start>=timeoutMs)return resolve(false);
      setTimeout(tick,250);
    };
    tick();
  });
}

async function _generateAssetPreview(asset,{force=false}={}){
  const fullPath=String(asset?.fullPath||asset?.path||"");
  if(!_assetPreviewSupported({...asset,fullPath})){
    return {..._assetPreviewMeta({...asset,fullPath}),fullPath,status:"unsupported"};
  }
  const filePath=_assetPreviewFile(fullPath);
  if(force&&fs.existsSync(filePath))try{fs.unlinkSync(filePath)}catch(_e){}
  if(fs.existsSync(filePath)){
    return {..._assetPreviewMeta({...asset,fullPath}),fullPath,status:"ready",cached:true,method:"editor_thumbnail"};
  }
  if(_assetPreviewJobs.has(fullPath))return _assetPreviewJobs.get(fullPath);
  const job=(async()=>{
    try{
      const raw=await _ueToolCommand("export_asset_thumbnail",{
        asset_path:fullPath,
        output_path:filePath,
        width:512,
        height:512,
      },90000);
      if(raw?.status==="error"||raw?.error)throw new Error(raw.error||raw.message||"asset thumbnail export failed");
      const status=String(raw?.status||raw?.result?.status||"").toLowerCase();
      if(status&&status!=="ready"&&status!=="success"&&status!=="ok")throw new Error(raw?.message||raw?.result?.message||`asset thumbnail export returned ${status}`);
      const ok=await _waitForFile(filePath,15000);
      if(!ok)throw new Error("asset thumbnail file was not written");
      return {..._assetPreviewMeta({...asset,fullPath}),fullPath,status:"ready",cached:false,method:"editor_thumbnail"};
    }finally{
      _assetPreviewJobs.delete(fullPath);
    }
  })().catch(err=>{
    _assetPreviewJobs.delete(fullPath);
    log.system("warn",`asset preview ${fullPath}: ${err.message}`);
    return {..._assetPreviewMeta({...asset,fullPath}),fullPath,status:"error",error:err.message};
  });
  _assetPreviewJobs.set(fullPath,job);
  return job;
}

app.post("/api/asset-previews",async(req,res)=>{
  if(!_cachedUeConn)return res.status(503).json({error:"UE not connected"});
  const assets=Array.isArray(req.body?.assets)?req.body.assets:[];
  const force=!!req.body?.force;
  const limit=Math.max(1,Math.min(parseInt(req.body?.limit||"4",10)||4,8));
  const results=[];
  let generated=0;
  for(const raw of assets.slice(0,80)){
    const fullPath=String(raw?.fullPath||raw?.path||"");
    const asset={...raw,fullPath};
    if(!fullPath){results.push({status:"error",error:"fullPath required"});continue}
    const meta=_assetPreviewMeta(asset);
    if((!force&&meta.previewStatus==="ready")||meta.previewStatus==="unsupported"){
      results.push({fullPath,status:meta.previewStatus,...meta});
      continue;
    }
    if(generated>=limit){
      results.push({fullPath,status:"missing",...meta});
      continue;
    }
    generated++;
    results.push(await _generateAssetPreview(asset,{force}));
  }
  res.json({results,generated,cacheDir:ASSET_PREVIEW_DIR});
});

// Build complete nested tree once at startup — clients load once and navigate locally
const _assetTree = (() => {
  // Build nested dir structure: { name, path, children: [], assets: [] }
  const root = { name:'', path:'/', children:[], assets:[] };
  const dirs = new Map(); dirs.set('/', root);

  const getOrCreateDir = (dirPath) => {
    if (dirs.has(dirPath)) return dirs.get(dirPath);
    const parts = dirPath.replace(/\/$/, '').split('/').filter(Boolean);
    let node = root;
    let built = '/';
    for (const seg of parts) {
      built += seg + '/';
      if (!dirs.has(built)) {
        const child = { name: seg, path: built, children: [], assets: [] };
        node.children.push(child);
        dirs.set(built, child);
      }
      node = dirs.get(built);
    }
    return node;
  };

  for (const asset of _allAssets) {
    const dir = getOrCreateDir(asset.dir);
    dir.assets.push(_withAssetPreviewMeta({
      name: asset.name, fullPath: asset.fullPath, type: asset.type,
      category: asset.category, spawnTool: asset.spawnTool, icon: asset.icon || '📦',
      agentType: asset.agentType, description: asset.description, biome: asset.biome,
    }));
  }

  // Sort children alphabetically
  const sortNode = (node) => {
    node.children.sort((a,b) => a.name.localeCompare(b.name));
    node.children.forEach(sortNode);
    node.assets.sort((a,b) => a.name.localeCompare(b.name));
  };
  sortNode(root);

  // Category counts for sidebar
  const counts = {};
  for (const a of _allAssets) counts[a.category] = (counts[a.category]||0)+1;

  return { tree: root, counts, totalAssets: _allAssets.length, builtAt: Date.now() };
})();

// Return live UE-scanned tree if ready, else fall back to static catalog
app.get('/api/asset-tree',(req,res) => res.json(_liveAssetTree || _assetTree));

// Manual refresh — call after importing new assets into UE
app.post('/api/asset-tree/refresh', async(req,res) => {
  if (!LIVE_ASSET_SCAN_ENABLED) return res.status(403).json({ error:'live asset scan disabled (set LIVE_ASSET_SCAN_ENABLED=1)' });
  _assetScanDone = false;
  try { await _scanUeAssets(); res.json({ ok:true, scannedAt:_liveAssetTree?.scannedAt }); }
  catch(e) { res.status(503).json({ error:e.message }); }
});

// ── Lazy per-directory asset listing (one level at a time) ────────────────────
const _dirListCache = new Map();  // path → { dirs, assets, cachedAt }
const DIR_CACHE_TTL_MS = 90_000;

// Top-level content roots (same as full scan)
const _TOP_LEVEL_DIRS = [
  'CityDatabase','TrafficSystem','ChineseWaterTown','Lighthouse_Island',
  'ModularGothicFantasyEnvironment','CastleRiver','Cave','ModularTemplePlaza',
  'TrainStation','ContainerYard','ModularCourtyard','MiddleEast',
  'Chinese_Landscape','Village','WinterTown','ModularSciFi','Dungeon','HwaseongHaenggung',
];

app.get('/api/asset-ls', async (req, res) => {
  let browsePath = (req.query.path || '/Game/');
  if (!browsePath.endsWith('/')) browsePath += '/';

  if (!_cachedUeConn) {
    return res.json({ source: 'unavailable', dirs: [], assets: [] });
  }

  const cached = _dirListCache.get(browsePath);
  if (cached && Date.now() - cached.cachedAt < DIR_CACHE_TTL_MS) {
    return res.json({ source: 'ue-python', dirs: cached.dirs, assets: cached.assets.map(_withAssetPreviewMeta) });
  }

  // Root: scan /Game/ non-recursively to discover what actually exists in this project.
  // Subdir: scan recursively so nested folders can be inferred from deeper asset paths.
  const py = browsePath === '/Game/'
    ? `import unreal, json
items = []
try:
    items = list(unreal.EditorAssetLibrary.list_assets('/Game/', recursive=False, include_folder=True))
except Exception:
    pass
print('SWASSET_BEGIN' + json.dumps(items) + 'SWASSET_END')`.trim()
    : `import unreal, json
items = []
try:
    items = list(unreal.EditorAssetLibrary.list_assets(${JSON.stringify(browsePath)}, recursive=True, include_folder=True))
except Exception:
    pass
print('SWASSET_BEGIN' + json.dumps(items) + 'SWASSET_END')`.trim();

  try {
    const raw = await new Promise((resolve, reject) => {
      const sock = new (require('net').Socket)();
      const timer = setTimeout(() => { sock.destroy(); reject(new Error('timeout')); }, 20_000);
      sock.connect(parseInt(UNREAL_PORT), UNREAL_HOST, () => {
        sock.write(JSON.stringify({ type: 'execute_python_script', params: { script: py } }) + '\n');
      });
      let buf = '';
      sock.on('data', d => {
        buf += d.toString();
        if (buf.includes('SWASSET_END')) {
          try { const r = JSON.parse(buf); clearTimeout(timer); sock.destroy(); resolve(r); } catch {}
        }
      });
      sock.on('error', e => { clearTimeout(timer); reject(e); });
      sock.on('close', () => { clearTimeout(timer); try { resolve(JSON.parse(buf)); } catch { resolve({}); } });
    });

    // Search python_logs directly — JSON.stringify double-escapes quotes, breaking JSON.parse
    const logText = (raw?.result?.python_logs || []).join('\n');
    const si = logText.indexOf('SWASSET_BEGIN');
    const ei = logText.indexOf('SWASSET_END');
    if (si === -1 || ei === -1) throw new Error('no SWASSET delimiters in logs');
    const paths = JSON.parse(logText.slice(si + 'SWASSET_BEGIN'.length, ei));

    const seenDirs = new Set();
    const dirs = [], assets = [];

    for (const p of paths) {
      if (!p.startsWith('/Game/')) continue;
      const isFolder = p.endsWith('/') || !p.split('/').pop().includes('.');
      if (isFolder) {
        const fp = p.endsWith('/') ? p : p + '/';
        const rel = fp.slice(browsePath.length).replace(/\/$/, '');
        const seg = rel.split('/')[0];
        if (seg && !seenDirs.has(seg)) { seenDirs.add(seg); dirs.push({ name: seg, path: browsePath + seg + '/' }); }
      } else {
        const parts = p.split('/');
        const filename = parts.pop();
        const fileDir = parts.join('/') + '/';
        if (fileDir !== browsePath) {
          const rel = fileDir.slice(browsePath.length).replace(/\/$/, '');
          const seg = rel.split('/')[0];
          if (seg && !seenDirs.has(seg)) { seenDirs.add(seg); dirs.push({ name: seg, path: browsePath + seg + '/' }); }
          continue;
        }
        const name = filename.split('.')[0];
        const isBP   = filename.includes('_C') || /\/[Bb]lueprints?\//.test(p);
        const isMesh = /\/[Mm]eshes?\//.test(p) || name.startsWith('SM_');
        const isMapAsset = /\/(Maps|Levels|Map)\//i.test(p);
        const type = isBP ? 'blueprint' : isMesh ? 'static_mesh' : isMapAsset ? 'map' : 'asset';
        assets.push(_withAssetPreviewMeta({ name, fullPath: p, type }));
      }
    }

    dirs.sort((a, b) => a.name.localeCompare(b.name));
    assets.sort((a, b) => a.name.localeCompare(b.name));
    _dirListCache.set(browsePath, { dirs, assets, cachedAt: Date.now() });
    res.json({ source: 'ue-python', dirs, assets });
  } catch (e) {
    log.system('warn', `asset-ls ${browsePath}: ${e.message}`);
    res.json({ source: 'error', dirs: [], assets: [] });
  }
});

// ── Load a UE map directly in the editor ──────────────────────────────────────
app.post('/api/load-map', async (req, res) => {
  const { path } = req.body || {};
  if (!path || typeof path !== 'string') return res.status(400).json({ error: 'path required' });
  if (!_cachedUeConn) return res.status(503).json({ error: 'UE not connected' });

  // Strip asset reference suffix: /Game/Pack/Maps/Level.Level → /Game/Pack/Maps/Level
  const cleanPath = path.includes('.') ? path.split('.')[0] : path;

  // Guard: reloading the CURRENTLY-open map duplicates the UWorld and fatals the
  // pixel-streaming editor. Switching to a DIFFERENT map is safe (verified). So skip
  // the load when the target is already loaded.
  const py = `import unreal
try:
    ls = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    cur = ues.get_editor_world().get_name()
    target = ${JSON.stringify(cleanPath)}
    if cur == target.split('/')[-1]:
        print("MAP_ALREADY_LOADED")
    else:
        ls.load_level(target)
        print("MAP_LOADED_OK")
except Exception as e:
    print("MAP_LOAD_ERR " + str(e))`.trim();

  try {
    const raw = await new Promise((resolve, reject) => {
      const sock = new (require('net').Socket)();
      // Heavy / cold maps (first-time shader+asset compile) can block the game thread
      // for tens of seconds. Allow a long wait rather than failing the switch.
      const timer = setTimeout(() => { sock.destroy(); reject(new Error('timeout')); }, 180_000);
      sock.connect(parseInt(UNREAL_PORT), UNREAL_HOST, () => {
        sock.write(JSON.stringify({ type: 'execute_python_script', params: { script: py } }) + '\n');
      });
      let buf = '';
      sock.on('data', d => {
        buf += d.toString();
        try { const r = JSON.parse(buf); clearTimeout(timer); sock.destroy(); resolve(r); } catch {}
      });
      sock.on('error', e => { clearTimeout(timer); reject(e); });
      sock.on('close', () => { clearTimeout(timer); try { resolve(JSON.parse(buf)); } catch { resolve({}); } });
    });
    const logs = (raw?.result?.python_logs || []).join('\n');
    if (logs.includes('MAP_LOAD_ERR')) {
      const msg = logs.split('MAP_LOAD_ERR ').pop()?.trim() || 'unknown error';
      return res.status(500).json({ error: msg });
    }
    res.json({ ok: true, path: cleanPath });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// POST /api/immersive — no-op kept for backward compatibility. VISTA demo
// immersive mode is a one-time launcher state; browser clients must never
// inject the shared F11 toggle on load or reconnect.
app.post("/api/immersive",(req,res)=>{ res.json({ok:true}); });

app.get('/api/assets',(req,res)=>{
  let { path:browsePath='/', q='', page=0, limit=30, category='' } = req.query;
  page = Number(page); limit = Number(limit);
  if (!browsePath.endsWith('/')) browsePath += '/';

  // All assets whose dir starts with browsePath
  const inScope = _allAssets.filter(a => a.dir.startsWith(browsePath));

  // Virtual sub-dirs of current path
  const subDirs = new Set();
  for (const a of inScope) {
    const rel = a.dir.slice(browsePath.length);
    const seg = rel.split('/')[0];
    if (seg) subDirs.add(seg);
  }

  // Assets directly in this dir
  let here = inScope.filter(a => a.dir === browsePath);
  if (category) here = here.filter(a => a.category === category);
  if (q) here = here.filter(a => a.name.toLowerCase().includes(q.toLowerCase()));

  const total = here.length;
  const assets = here.slice(page * limit, (page+1) * limit);

  // Category counts (for the sidebar shortcuts)
  const counts = {};
  for (const a of _allAssets) {
    counts[a.category] = (counts[a.category] || 0) + 1;
  }

  res.json({ path:browsePath, dirs:[...subDirs].sort(), assets, total, page, hasMore:(page+1)*limit < total, counts });
});

// ── Agent state (fresh) ────────────────────────────────────────────────────────
app.get('/api/agent-state/:name', async(req,res) => {
  const name = req.params.name;
  const session = agentCtrl.get(name);
  if (!session) return res.status(404).json({ error:'Agent not found' });
  const lastAct = session.activity.length > 0 ? session.activity[session.activity.length-1] : null;
  res.json({
    agentName:         session.agentName,
    agentClass:        session.agentClass,
    status:            session.status,
    currentAction:     session.currentAction,
    lastAction:        lastAct?.actions?.length > 0 ? lastAct.actions[lastAct.actions.length-1].tool : null,
    location:          session.location,
    rotation:          session.rotation,
    velocity:          session.velocity,
    speed:             session.speed,
    positionUpdatedAt: session.positionUpdatedAt,
    activity:          session.activity.slice(-5),
    historyLength:     session.history.length,
    collisionCount:    session.collisionCount,
    recentCollisions:  (session.recentCollisions||[]).slice(-10),
    envFeedback:       (session.envFeedback||[]).slice(-5),
    memory:            (session.memory||[]).slice(-10),
    totalTurns:        session.totalTurns||0,
    totalCostUsd:      session.totalCostUsd||0,
    trajectoryLength:  (session.trajectory||[]).length,
    trajectoryPreview: (session.trajectory||[]).slice(-20),
  });
});

app.get('/api/agent-trajectory/:name', (req,res) => {
  const session = agentCtrl.get(req.params.name);
  if (!session) return res.status(404).json({ error:'Agent not found' });
  res.json({ trajectory: session.trajectory||[], agentName: session.agentName });
});

// ── Agent camera snapshot — renders from agent's own POV ──────────────────────
const _agentSnapCache = new Map(); // name -> {dataUrl, ts}

// Helper: read a file path returned by UCV and convert to base64 dataUrl.
// Returns null if path is invalid or file doesn't exist (rejects UCV error strings).
function _ucvPathToDataUrl(raw) {
  if (!raw) return null;
  const p = raw.trim();
  if (!p || p.includes('{') || p.toLowerCase().startsWith('error') || !fs.existsSync(p)) return null;
  const buf = fs.readFileSync(p);
  return `data:image/png;base64,${buf.toString('base64')}`;
}

// Helper: get latest screenshot from SCREENSHOT_DIR (< 5 min old)
function _latestScreenshotDataUrl() {
  try {
    const files = fs.readdirSync(SCREENSHOT_DIR)
      .filter(f => f.endsWith('.png'))
      .map(f => ({ f, t: fs.statSync(path.join(SCREENSHOT_DIR, f)).mtimeMs }))
      .filter(({ t }) => Date.now() - t < 300000)
      .sort((a, b) => b.t - a.t);
    if (!files[0]) return null;
    const buf = fs.readFileSync(path.join(SCREENSHOT_DIR, files[0].f));
    return `data:image/png;base64,${buf.toString('base64')}`;
  } catch { return null; }
}

app.get('/api/agent-camera/:name', async(req,res) => {
  const name = req.params.name;
  const CACHE_MS = 3500;
  const cached = _agentSnapCache.get(name);
  if (cached && Date.now() - cached.ts < CACHE_MS) {
    return res.json({ dataUrl: cached.dataUrl, ts: cached.ts });
  }

  // Try strategies in order, use first that works
  // Strategy 1: vget /camera/actor/{name}/lit — plugin renders from actor's own camera
  //   Requires recompiled plugin with CameraHandler::GetActorCameraLit
  let dataUrl = null;
  try {
    const raw = await ucvBroker.send(`vget /camera/actor/${name}/lit`, { timeoutMs: 6000, retries: 1 });
    dataUrl = _ucvPathToDataUrl(raw);
  } catch { /* plugin not yet compiled */ }

  // Strategy 2: position camera/0 at agent's eye-level (tracked state) + capture
  if (!dataUrl) {
    const session = agentCtrl.get(name);
    const loc = session?.location;
    const rot = session?.rotation;
    if (loc && rot) {
      try {
        const eyeX = loc[0], eyeY = loc[1], eyeZ = loc[2] + 160;
        const yaw = rot[1];
        await ucvBroker.send(`vset /camera/0/location ${eyeX.toFixed(1)} ${eyeY.toFixed(1)} ${eyeZ.toFixed(1)}`, { timeoutMs: 3000 });
        await ucvBroker.send(`vset /camera/0/rotation 0 ${yaw.toFixed(1)} 0`, { timeoutMs: 3000 });
        const raw2 = await ucvBroker.send('vget /camera/0/lit', { timeoutMs: 5000 });
        dataUrl = _ucvPathToDataUrl(raw2);
      } catch { /* UCV not connected or agent not tracked */ }
    }
  }

  // Strategy 3: latest scene screenshot (always available after take_screenshot)
  if (!dataUrl) {
    dataUrl = _latestScreenshotDataUrl();
  }

  if (dataUrl) {
    _agentSnapCache.set(name, { dataUrl, ts: Date.now() });
    return res.json({ dataUrl, ts: Date.now() });
  }
  res.status(503).json({ error:'No camera image available — take a screenshot first' });
});


// ── UE command passthrough (for verifier collision check) ─────────────────────
app.post('/api/ue-command', async(req,res) => {
  const { command } = req.body;
  if (!command) return res.status(400).json({ error:'command required' });
  try {
    const result = await new Promise((resolve, reject) => {
      const sock = new (require('net').Socket)();
      const timer = setTimeout(() => { sock.destroy(); reject(new Error('timeout')); }, 8000);
      sock.connect(parseInt(UNREAL_PORT), UNREAL_HOST, () => {
        sock.write(JSON.stringify({ type:'execute_console_command', params:{ command } }) + '\n');
      });
      let buf = '';
      sock.on('data', d => {
        buf += d.toString();
        try { const r = JSON.parse(buf); clearTimeout(timer); sock.destroy(); resolve(r); } catch {}
      });
      sock.on('error', e => { clearTimeout(timer); reject(e); });
    });
    // Also try direct UCV for vget commands
    if (command.startsWith('vget ') || command.startsWith('vset ')) {
      const ucvResult = await ucvBroker.send(command, { timeoutMs: 6000 }).catch(()=>null);
      if (ucvResult !== null) return res.json({ result: ucvResult, source:'ucv' });
    }
    res.json({ result: JSON.stringify(result), source:'mcp' });
  } catch(e) {
    // Fallback to direct UCV
    try {
      const ucvResult = await ucvBroker.send(command, { timeoutMs: 6000 });
      res.json({ result: ucvResult, source:'ucv' });
    } catch(e2) {
      res.status(503).json({ error: e2.message });
    }
  }
});

// ── VLM scene scoring ─────────────────────────────────────────────────────────
// Uses Claude Code CLI (already authenticated, no API key needed).
// Image is saved to a temp file; Claude reads it via its built-in Read tool.
app.post('/api/vlm-score', async(req,res) => {
  const { imageDataUrl } = req.body;
  if (!imageDataUrl) return res.status(400).json({ error:'imageDataUrl required' });

  const os = require('os');
  const tmpImg = path.join(os.tmpdir(), `sw_vlm_${Date.now()}.png`);

  try {
    // Write image to disk so Claude Code can read it with its Read tool
    const imgBuf = Buffer.from(imageDataUrl.replace(/^data:image\/\w+;base64,/, ''), 'base64');
    fs.writeFileSync(tmpImg, imgBuf);

    const prompt =
      `You are a 3D scene quality evaluator. ` +
      `Read the screenshot at "${tmpImg}" using your Read tool, then rate the scene 1-10. ` +
      `Reply ONLY with a single JSON object on one line, nothing else: ` +
      `{"score":N,"label":"one-word","feedback":"one sentence about the scene quality"}`;

    const result = await new Promise((resolve, reject) => {
      const proc = spawn(CLAUDE_BIN, [
        '-p', prompt,
        '--output-format', 'json',
        '--dangerously-skip-permissions',
      ], { env: process.env, timeout: 60000 });

      let out = '', err = '';
      proc.stdout.on('data', d => { out += d.toString(); });
      proc.stderr.on('data', d => { err += d.toString(); });

      proc.on('close', code => {
        // Clean up temp file
        try { fs.unlinkSync(tmpImg); } catch {}

        if (code !== 0 && !out) {
          reject(new Error(`claude exit ${code}: ${err.slice(0,200)}`));
          return;
        }

        // Claude --output-format json wraps result in {"result":"...","session_id":"..."}
        let text = '';
        try {
          const parsed = JSON.parse(out);
          text = parsed.result || parsed.text || out;
        } catch {
          text = out;
        }

        // Extract score JSON from response text
        const m = text.match(/\{[^{}]*"score"\s*:\s*\d+[^{}]*\}/);
        if (m) { try { resolve(JSON.parse(m[0])); return; } catch {} }

        // Lenient: extract fields individually
        const ms = text.match(/"score"\s*:\s*(\d+)/);
        const ml = text.match(/"label"\s*:\s*"([^"]+)"/);
        const mf = text.match(/"feedback"\s*:\s*"([^"]+)"/);
        if (ms) { resolve({ score:parseInt(ms[1]), label:ml?.[1]||'ok', feedback:mf?.[1]||text.slice(0,100) }); return; }

        // Last resort: just return the raw text as feedback with neutral score
        resolve({ score:5, label:'ok', feedback: text.replace(/\n/g,' ').slice(0,150) });
      });

      proc.on('error', e => { try { fs.unlinkSync(tmpImg); } catch {} reject(e); });
    });

    res.json({ score: result.score||5, label: result.label||'ok', feedback: result.feedback||'' });
  } catch(e) {
    try { fs.unlinkSync(tmpImg); } catch {}
    res.json({ score:5, label:'error', feedback:`Scoring failed: ${e.message}` });
  }
});

// ── Agent stop-all ─────────────────────────────────────────────────────────────
app.post('/api/agent-stop-all', (req,res) => {
  agentCtrl.stopAll();
  res.json({ ok:true });
});

// Manually register an agent for state tracking (for player-controlled agents
// that exist in UE but weren't spawned via our spawn_agent MCP call).
app.post('/api/agent-track', async(req,res) => {
  const { name, agentClass='pedestrian' } = req.body;
  if (!name) return res.status(400).json({ error:'name required' });
  const session = agentCtrl.getOrCreate(name, agentClass, null);
  // Immediately fetch position so the card shows data right away
  try {
    const { getBroker } = require('./unreal-bridge');
    const br = getBroker();
    const [loc, rot] = await Promise.all([
      br.send(`vget /object/${name}/location`, { timeoutMs:4000 }),
      br.send(`vget /object/${name}/rotation`, { timeoutMs:4000 }),
    ]);
    if (loc) { session.location = loc.trim().split(/\s+/).map(Number); session.positionUpdatedAt = Date.now(); }
    if (rot) session.rotation = rot.trim().split(/\s+/).map(Number);
  } catch {}
  res.json({ ok:true, agent: session.toJSON() });
});

// Auto-discover agents: query UCV for all objects, register pawn-like ones
app.post('/api/agent-discover', async(req,res) => {
  try {
    const { getBroker } = require('./unreal-bridge');
    const br = getBroker();
    const raw = await br.send('vget /objects', { timeoutMs:5000 });
    const names = (raw||'').trim().split(/\s+/).filter(Boolean);
    const discovered = names.filter(n => AGENT_PATTERNS.test(n) && !AGENT_EXCLUDE.test(n));
    for (const n of discovered) {
      agentCtrl.getOrCreate(n, 'pedestrian', null);
    }
    res.json({ discovered, total: names.length });
  } catch(e) {
    res.status(503).json({ error: e.message });
  }
});

// Metrics REST endpoint (also included in SSE)
app.get('/api/metrics', (req,res) => res.json(metricsHub.snapshot()));

// Record scene collision count into metrics hub (called from CodingVerifierPanel)
app.post('/api/metrics/scene-collision', (req,res) => {
  const { count } = req.body;
  if (typeof count === 'number') metricsHub.recordSceneCollisions(count);
  res.json({ ok:true });
});

// ── Scene check: AABB collision + floating detection via UE Python ─────────────
// Only checks actors spawned in this session (from ctxManager).
// Uses prefix-matching to find the UE actor with session suffix.
// Does NOT require PIE — runs against the editor world directly.
app.post('/api/scene-check', async (req, res) => {
  const FLOAT_THRESHOLD = (req.body && req.body.threshold_cm) || 10;

  // Get session-spawned actor names from context manager (names WITHOUT session suffix)
  const sessionState  = ctxManager.getState(STUDIO_SESSION);
  const ctxObjects    = (sessionState?.objects || []);
  const ctxAgents     = (sessionState?.agents  || []);
  const ctxNames      = [...ctxObjects, ...ctxAgents].map(o => o.name).filter(Boolean);

  // Only check actors from the current session. If nothing was spawned yet, return empty.
  if (ctxNames.length === 0) {
    return res.json({
      collision_count: 0, collision_pairs: [], checked_actors_count: 0,
      total_overlaps: 0, floating_count: 0, floating_actors: [],
      threshold_cm: FLOAT_THRESHOLD,
      note: 'No session actors tracked yet — spawn objects first',
    });
  }

  const script = `
import unreal, json

world = unreal.EditorLevelLibrary.get_editor_world()
ctx_names      = ${JSON.stringify(ctxNames)}   # may be empty
FLOAT_THRESHOLD = ${FLOAT_THRESHOLD}
TOUCH_TOL       = 5.0

all_actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)

# Infrastructure classes to skip entirely (volumes, system actors, atmosphere, etc.)
INFRA_CLS = frozenset(['skyatmosphere','directionallight','skylightcomponent',
    'exponentialheightfog','worldsettings','defaultphysicsvolume',
    'navmeshboundingvolume','postprocessvolume','triggervolume','brushactor',
    'recastnavmesh','atmosphericfog','smartobjectsubsystem'])
INFRA_NM  = frozenset(['navmesh','recast','worldsettings','smartobject','subsystem',
    'gameplaydebugger','atmosphericfog','postprocess'])

INFRA_LABEL_PREFIX = frozenset(['sm_bg', 'bp_bg'])
def is_infra(actor):
    cls = actor.get_class().get_name().lower()
    if any(s in cls for s in INFRA_CLS): return True
    if any(s in actor.get_name().lower() for s in INFRA_NM): return True
    try:
        lbl = actor.get_actor_label().lower()
        return any(lbl.startswith(p) for p in INFRA_LABEL_PREFIX)
    except: return False

# Build label + name lookup tables (non-infra actors only)
by_label, by_name = {}, {}
for a in all_actors:
    if is_infra(a): continue
    try:
        lbl = a.get_actor_label()
        if lbl: by_label[lbl] = a
    except: pass
    by_name[a.get_name()] = a

# Session mode only: resolve by label-prefix matching
# ctx_names are the base names (without session suffix) from ctxManager
def find_actor(cn):
    prefix = cn + '_'
    if cn in by_label: return by_label[cn]
    for lbl, actor in by_label.items():
        if lbl.startswith(prefix): return actor
    if cn in by_name: return by_name[cn]
    for nm, actor in by_name.items():
        if nm.startswith(prefix): return actor
    return None
resolved = {cn: find_actor(cn) for cn in ctx_names if find_actor(cn)}

# Build per-actor bounds list for actors we are checking
scene = []
for key, actor in resolved.items():
    origin, ext = actor.get_actor_bounds(False)
    if ext.x < 1 and ext.y < 1 and ext.z < 1: continue
    loc = actor.get_actor_location()
    scene.append({'actor': actor, 'name': key,
        'ox': origin.x, 'oy': origin.y, 'oz': origin.z,
        'ex': max(ext.x,1), 'ey': max(ext.y,1), 'ez': max(ext.z,1),
        'bot_z': origin.z - ext.z, 'top_z': origin.z + ext.z,
        'lx': loc.x, 'ly': loc.y})

# All non-infra actors as potential support surfaces (includes scene actors)
all_bounds = []
for a in all_actors:
    if is_infra(a): continue
    origin, ext = a.get_actor_bounds(False)
    if ext.x < 1 and ext.y < 1 and ext.z < 1: continue
    all_bounds.append({'actor': a,
        'ox': origin.x, 'oy': origin.y, 'oz': origin.z,
        'ex': max(ext.x,1), 'ey': max(ext.y,1), 'ez': max(ext.z,1),
        'top_z': origin.z + ext.z})

# 1. FLOATING CHECK
# Per-actor: find highest top surface strictly below this actor within its footprint.
# Fallback = Z=0 (ground assumption) when nothing is found below.
# Note: other scene actors ARE valid supports (e.g. L2 wall sitting on L1 wall).
floating = []
for a in scene:
    best = None  # None = no surface found below
    for other in all_bounds:
        if other['actor'] is a['actor']: continue  # skip self
        if other['top_z'] >= a['bot_z'] - 1: continue  # not strictly below
        if abs(other['ox'] - a['lx']) > (a['ex']*0.5 + other['ex']): continue
        if abs(other['oy'] - a['ly']) > (a['ey']*0.5 + other['ey']): continue
        if best is None or other['top_z'] > best: best = other['top_z']
    # No surface found above Z=0 -> always floating (actor is unsupported above ground)
    # bot_z <= 0 means actor is at/below ground plane (embedded) -> skip
    # Surface found -> floating only if gap exceeds threshold
    if best is None and a['bot_z'] > 0:
        floating.append({'name': a['name'],
                         'gap_cm': None,
                         'bottom_z': round(a['bot_z'], 1),
                         'surface_z': None, 'no_surface': True})
    elif best is not None and a['bot_z'] - best > FLOAT_THRESHOLD:
        floating.append({'name': a['name'],
                         'gap_cm': round(a['bot_z'] - best, 1),
                         'bottom_z': round(a['bot_z'], 1),
                         'surface_z': round(best, 1)})

# 2. COLLISION CHECK: AABB overlap, ignoring surface contacts (<=5 cm)
collisions = []
for i in range(len(scene)):
    a = scene[i]
    for j in range(i+1, len(scene)):
        b = scene[j]
        dx = abs(a['ox']-b['ox']) - (a['ex']+b['ex'])
        dy = abs(a['oy']-b['oy']) - (a['ey']+b['ey'])
        dz = abs(a['oz']-b['oz']) - (a['ez']+b['ez'])
        if dx < -TOUCH_TOL and dy < -TOUCH_TOL and dz < -TOUCH_TOL:
            pen = min(abs(dx),abs(dy),abs(dz)) - TOUCH_TOL
            collisions.append({'actor1': a['name'], 'actor2': b['name'],
                               'penetration_depth': round(pen,1)})

result = {
    'collision_count': len(collisions),
    'collision_pairs': collisions[:10],
    'checked_actors_count': len(scene),
    'total_overlaps': len(collisions),
    'floating_count': len(floating),
    'floating_actors': sorted(floating, key=lambda x: (0 if x.get('no_surface') else 1, -(x.get('gap_cm') or 0)))[:20],
    'threshold_cm': FLOAT_THRESHOLD,
    'unresolved_count': len(ctx_names) - len(resolved) if ctx_names else 0,
    'mode': 'session' if ctx_names else 'full_scan',
}
print('SCENE_CHECK:' + json.dumps(result))
`;

  try {
    const raw = await new Promise((resolve, reject) => {
      const sock = new (require('net').Socket)();
      const timer = setTimeout(() => { sock.destroy(); reject(new Error('scene-check timeout')); }, 25000);
      sock.connect(parseInt(UNREAL_PORT), UNREAL_HOST, () => {
        sock.write(JSON.stringify({ type: 'execute_python_script', params: { script } }) + '\n');
      });
      let buf = '';
      sock.on('data', d => {
        buf += d.toString();
        try { const r = JSON.parse(buf); clearTimeout(timer); sock.destroy(); resolve(r); } catch {}
      });
      sock.on('error', e => { clearTimeout(timer); reject(e); });
      sock.on('close', () => { if (buf.trim()) { try { resolve(JSON.parse(buf)); } catch {} } });
    });

    const logs = (raw?.result?.python_logs || []);
    let data = null;
    for (const line of logs) {
      if (line.includes('SCENE_CHECK:')) {
        try { data = JSON.parse(line.split('SCENE_CHECK:')[1]); } catch {}
        break;
      }
    }
    if (!data) {
      logToFile('scene-check', `No result — logs: ${logs.slice(0,5).join(' | ')}`);
      return res.status(502).json({ error: 'No result from UE', logs: logs.slice(0, 5) });
    }
    if (typeof data.collision_count === 'number') metricsHub.recordSceneCollisions(data.collision_count);
    res.json(data);
  } catch (e) {
    logToFile('scene-check', `Error: ${e.message}`);
    res.status(503).json({ error: e.message });
  }
});


/* ===== siddhant: demo + ab-eval endpoints + helpers ===== */

async function _contentPyDir() {
  if (_contentPyCache !== null) return _contentPyCache;
  const up = process.env.UE_PROJECT_PATH;
  if (up) { try { const d = path.join(path.dirname(up), "Content", "Python"); if (fs.existsSync(d)) { _contentPyCache = d; return d; } } catch (_e) {} }
  try {
    const r = await ueExecScript("import unreal\nprint(unreal.SystemLibrary.get_project_directory())", 20000);
    const m = _ueLogs(r).match(/(\/[\w\/.\-]+)/);
    if (m) { const d = path.join(m[1].replace(/\/+$/, ""), "Content", "Python"); try { fs.mkdirSync(d, { recursive: true }); } catch (_e) {} _contentPyCache = d; return d; }
  } catch (_e) {}
  _contentPyCache = false; return false;
}


async function _deleteUeModules(mods) {
  if (!mods || !mods.length) return;
  const dir = await _contentPyDir(); if (!dir) return;
  for (const pm of mods) { try { fs.unlinkSync(path.join(dir, pm + ".py")); } catch (_e) {} }
}


async function abEvalClearToBlankScene() {
  const script = [
    "import unreal",
    "eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "KEEP_CLASSES={",
    "  'WorldSettings','LevelScriptActor','WorldDataLayers','ExternalDataLayerAsset',",
    "  'DataLayerManager','LevelBounds','Brush','PlayerStart','CameraActor',",
    "  'DirectionalLight','SkyLight','SkyAtmosphere','ExponentialHeightFog',",
    "  'PostProcessVolume','SphereReflectionCapture','NavMeshBoundsVolume',",
    "  'AbstractNavData','NavigationData','RecastNavMesh'",
    "}",
    // NOTE: keep by CLASS (above) for all engine lighting/sky/fog/post-process — those classes are
    // already in KEEP_CLASSES. Do NOT keep by the generic label prefixes 'Light'/'Fog'/'Sky'/'Camera'/'Atmo':
    // builders legitimately name scene props 'Lighthouse…', 'Foggy_Stone…', 'Fog_Sheet…', 'Skyscraper…',
    // and a label-prefix match would preserve them across every reset → they leak/accumulate into all later
    // scenes. Only keep harness/engine-system labels that builder props never use.
    "KEEP_LABEL_PREFIXES=('WorldSettings','Brush','Default','Player','Directional','PostProcess','SphereReflection','Nav','LevelBounds','Arena_Env_Sun','Arena_Env_SkyLight','Arena_Env_Atmosphere','Arena_Env_Fog')",
    "deleted=[]",
    "kept=[]",
    "for a in list(eas.get_all_level_actors()):",
    "    try:",
    "        lbl=a.get_actor_label() or a.get_name()",
    "        cls=a.get_class().get_name()",
    "        if cls in KEEP_CLASSES or lbl.startswith(KEEP_LABEL_PREFIXES):",
    "            kept.append(lbl)",
    "            continue",
    "        eas.destroy_actor(a)",
    "        deleted.append(lbl)",
    "    except Exception as e:",
    "        print('AB_EVAL_RESET_DELETE_ERR '+str(e))",
    "unreal.SystemLibrary.collect_garbage()",
    "print('AB_EVAL_RESET_OK deleted='+str(len(deleted))+' kept='+str(len(kept)))",
  ].join("\n");
  const r = await ueExecScript(script, 120000);
  const logs = _ueLogs(r);
  return { ok: logs.includes("AB_EVAL_RESET_OK"), ue: r, logs };
}



app.post("/api/ab-eval/reset-scene", async (req, res) => {
  const result = await abEvalClearToBlankScene();
  if (typeof ctxManager.resetSession === "function") ctxManager.resetSession(STUDIO_SESSION);
  else ctxManager.clearAllSpawned(STUDIO_SESSION);
  if (!result.ok) return res.status(502).json({ ok: false, error: "UE blank-scene reset failed", logs: result.logs || "" });
  logToFile("ab_eval", "blank-scene reset ok");
  res.json({ ok: true, logs: result.logs || "" });
});


app.post("/api/demo/reset",(s,e)=>{try{delete require.cache[require.resolve("./chat-replay")]}catch(_){};const __dr=require("./chat-replay");__dr.reset();e.json({ok:true,reloaded:true,...__dr.status()})});


app.get("/api/demo/status",(s,e)=>{const __dr=require("./chat-replay");e.json({demoMode:process.env.DEMO_MODE==="1",...__dr.status()})});


app.post("/api/demo/stop",(s,e)=>{const __dr=require("./chat-replay");__dr.requestStop();e.json({ok:true})});


app.post("/api/demo/360",(s,e)=>{const __dr=require("./chat-replay");return __dr.handle360(s,e,{UNREAL_HOST,UNREAL_PORT,logToFile})});


app.all("/api/*",(s,e)=>{e.status(404).json({error:`Unknown API endpoint: ${s.method} ${s.path}`})});
const FRONTEND_DIR=path.resolve(__dirname,"../dist");
if(fs.existsSync(FRONTEND_DIR)){
  app.get("/ue-player.html",(req,res)=>{
    if(VISTA_DEMO_ENABLED&&(
      String(req.query.cirrus||"")!==String(CIRRUS_HTTP_PORT)||
      req.query.ss!==undefined
    )){
      return res.status(400).type("text/plain").send("VISTA demo requires the configured signaling port");
    }
    return res.sendFile(path.join(FRONTEND_DIR,"ue-player.html"));
  });
  app.use(express.static(FRONTEND_DIR));
  app.get("*",(s,e)=>{e.sendFile(path.join(FRONTEND_DIR,"index.html"))});
  console.log("  Frontend served from:",FRONTEND_DIR);
}
app.listen(PORT,STUDIO_HOST,()=>{console.log(`
\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557`),console.log("\u2551       SimWorld Studio Backend                      \u2551"),console.log("\u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563"),console.log(`\u2551  Listening : http://${STUDIO_HOST}:${PORT}                  \u2551`),console.log(`\u2551  Claude    : ${CLAUDE_BIN}                            \u2551`),console.log("\u2551  MCP config: mcp.json (local stdio)               \u2551"),console.log(`\u2551  UE TCP    : ${UNREAL_HOST}:${UNREAL_PORT}                 \u2551`),console.log(`\u2551  Logs      : ${LOG_DIR}          \u2551`),console.log(`\u255A\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255D
`)});
