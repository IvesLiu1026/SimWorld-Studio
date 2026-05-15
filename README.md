# SimWorld Studio

**Vibe code the physical world.** Chat with an AI coding agent to build, simulate, and control 3D environments in Unreal Engine 5 — with embodied agent support, real-time pixel streaming, and a full data visualization stack.

---

## Overview

SimWorld Studio is an AI-native 3D scene authoring and embodied agent testbed built on Unreal Engine 5.3. It connects Claude (via Claude Code CLI) to UE through a multi-protocol bridge, letting you:

- **Build scenes by chat** — describe a city block and watch it materialize in real time
- **Control embodied agents** — spawn, command, and observe AI agents navigating the scene
- **Run multi-agent experiments** — testbed with auto-PIE, trajectory recording, collision tracking
- **Stream the viewport** — Pixel Streaming delivers the live UE viewport to any browser

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Browser (React UI)                    │
│  Coding Agent │ Viewport │ Embodied Agent │ Statistics   │
└──────┬────────────┬──────────────┬────────────┬─────────┘
       │ SSE/HTTP   │ WebSocket    │ SSE/HTTP   │
       ▼            ▼              ▼            ▼
┌─────────────────────────────────────────────────────────┐
│              Web Server  (Node.js / Express)             │
│  index.js  │  AgentController  │  MetricsHub  │  SSE    │
└──────┬──────────────┬──────────────────────────────────-┘
       │              │
       ▼              ▼
┌────────────┐  ┌─────────────────────────────────────────┐
│ Claude CLI │  │            Unreal Engine 5.3             │
│ (MCP tools)│  │  UnrealMCP (TCP:55557)  │  UnrealCV      │
│            │──│  spawn / delete / move  │  vget / vset   │
└────────────┘  │  Python scripting       │  camera / obj  │
                └─────────────────────────────────────────-┘
                              │
                ┌─────────────┴──────────────┐
                │  Cirrus Signalling Server   │
                │  Pixel Streaming  :8685/86  │
                └────────────────────────────┘
```

### Component Map

| Component | Path | Description |
|---|---|---|
| **Launch Scripts** | `SimWorld-Studio.ps1 / .bat` | One-click launcher: Cirrus → UE → Web Server |
| **Web Server** | `web/server/index.js` | Express API, SSE push, asset catalog, metrics |
| **MCP Server** | `web/server/mcp-server.js` | Claude ↔ UE tool bridge via TCP |
| **Agent Controller** | `web/server/agent-controller.js` | Per-agent sessions, trajectory, collision tracking |
| **Metrics Hub** | `web/server/metrics-hub.js` | Time-series data for all agents (collision, speed, turns) |
| **Context Manager** | `web/server/context-manager.js` | Scene state (actors, environment, round) |
| **UnrealCV Bridge** | `web/server/unreal-bridge.js` | Persistent TCP socket to UCV with FIFO queue + retry |
| **Frontend** | `web/src/App.jsx` | React app: chat, viewport, agents, stats, asset browser |
| **UE Plugin** | `UE_Project/Plugins/unrealcv/` | Extended UnrealCV with hit tracking, actor camera commands |

---

## Features

### 🏗️ Coding Agent (Scene Generation)
- Natural language → 3D scene in real time
- **125 buildings** (BP_Building_01–127), 6 trees, vehicles, street furniture, static meshes
- **17 marketplace packs** discoverable via `list_assets()` (allow-AI licensed)
- Auto session-suffix on actor names prevents cross-map name collision crashes
- `verify_scene` tool: Claude evaluates screenshot and returns PASS/NEEDS_IMPROVEMENT/FAIL

### 👁️ Live Viewport
- Pixel Streaming via Cirrus signalling server — full UE viewport in browser
- Click to activate mouse/keyboard input
- 1920×1080 default, adaptive resolution via MatchViewportRes

### 🤖 Embodied Agent Panel
- Spawn and control `Base_Pedestrian` / `Base_User_Agent` / `Base_Demo` agents
- Per-agent session: position, rotation, heading (compass), speed, status
- **Real-time state**: background poller every 3s via UnrealCV
- **Trajectory view**: top-down SVG map with heading arrows, collision markers (red dots)
- **Camera tab**: 3-strategy agent POV (actor camera → camera/0 at eye-level → latest screenshot)
- **Activity log**: ReAct thought → tool actions → response per turn
- **Hit tracking**: `OnActorHit` plugin event → collision count, impulse, impact point
- Floating draggable detail window per agent

### 📊 Statistics Panels
- **Embodied Agent Statistics**: aggregate map (2D top-down, all agents), collision/speed time-series charts
- **Coding Agent Verifier**: rule-based (scene collision count) + VLM-based (Claude reads screenshot, scores 1–10)
- **MetricsHub**: server-side 5s sampling → real-time SVG `LineChart` / `MultiLineChart`

### 📁 Asset Content Drawer
- Built once at startup from live UE Python scan (`EditorAssetLibrary.list_assets`)
- UE Content Browser–style navigation: folder → shows subfolders + direct assets
- Left sidebar: global category filter (Buildings / Trees / Vehicles / etc.)
- Search: recursive subtree, category-aware
- Click any asset → inserts correct spawn command into chat

### 🧪 Multi-Agent Testbed
- Auto-start PIE (polls until confirmed active)
- Spawn N agents at configurable radius, broadcast goal
- Testbed log with per-step status

### 🔧 Developer Tools
- Mock mode: replay pre-recorded sessions without GPU
- Session management: slot-based with heartbeat and TTL
- Skills: pre-made prompts for city layout, weather, navigation
- Learned tools: Claude can save custom tool recipes

---

## Quick Start (Windows — Local Dev)

### Prerequisites
- Unreal Engine 5.3 (Epic Games Launcher)
- Node.js 18+
- Claude Code CLI: `npm install -g @anthropic-ai/claude-code`

### 1. Configure paths

Edit `SimWorld-Studio.ps1` lines 46–47:
```powershell
$UeRoot    = "C:\Program Files\Epic Games\UE_5.3"
$UeProject = "E:\UE\SimWorld-copy\SimWorld.uproject"
```

### 2. Launch (one command)

```powershell
.\SimWorld-Studio.ps1
```

The script will:
1. Kill stale port processes
2. Sync Cirrus `player.js` and build the React frontend
3. Start **Cirrus** signalling server (Pixel Streaming)
4. Launch **UE Editor** with MCP + Pixel Streaming flags
5. Wait for MCP port to be ready (up to 90s)
6. Start **Web Server** on port 3002
7. Stream all logs to terminal with `[cirrus]` / `[server]` prefixes

Open **http://localhost:3002** in Chrome.

### Options

```powershell
.\SimWorld-Studio.ps1 -Port 3003 -McpPort 55560 -CirrusHttpPort 8687 -Gpu 1 -NoBuild
```

---

## Quick Start (macOS — Web stack only, no UE)

Unreal Engine 5.3 has no native macOS build of this project, but the web stack (React UI, Node API, asset/skill/scene browsers, agent panel scaffolding) runs fine on Mac for frontend work and API exploration. UE-dependent features (viewport, scene spawn, agent control) will show `ueConnected: false` and are no-ops.

### Prerequisites
- Node.js 18+ (`brew install node`)

### Run

```bash
./SimWorld-Studio-Mac.sh              # installs deps, builds frontend, starts backend on :3002
./SimWorld-Studio-Mac.sh --no-build   # reuse existing dist/
./SimWorld-Studio-Mac.sh --dev        # vite dev server on :5173 with HMR + backend on :3002
```

Then open **http://localhost:3002** (or `:5173` in `--dev`).

The script sets `UNREAL_PORT=1` / `UCV_PORT=1` so the backend's UE probes fail fast and stay quiet — no Cirrus, no UE Editor, no MCP subprocess required.

---

## Quick Start (Linux — Shared Server)

See [`INTERNAL_SETUP.md`](INTERNAL_SETUP.md) for port assignment table and multi-user setup.

```bash
export UE_ROOT=/data/murray/ue/UE_5.3
export UE_PROJECT_PATH=/data/user/simworld_projects

simworld-studio start \
  --port 3004 --mcp-port 55561 \
  --cirrus-http-port 8687 --cirrus-ws-port 8688 --cirrus-sfu-port 8990 \
  --gpu 0
```

---

## UE Plugin Extensions

The UnrealCV plugin has been extended with:

| Command | Description |
|---|---|
| `vget /object/{name}/velocity` | Actor velocity [vx, vy, vz] cm/s |
| `vget /object/{name}/overlaps` | Currently overlapping actors (JSON) |
| `vget /object/{name}/nearby {r}` | Actors within radius r cm (JSON) |
| `vset /object/{name}/track_hits` | Bind `OnActorHit` — start recording physics collisions |
| `vget /object/{name}/hit_events` | Get & clear hit event queue (JSON) |
| `vget /camera/actor/{name}/lit` | Render from named actor's camera (FusionCamSensor or UCameraComponent) |

**Recompile required** after updating plugin files (`Ctrl+Alt+F7` in UE Editor).

---

## Module READMEs

| Module | README |
|---|---|
| Web Server (backend) | [`web/server/README.md`](simworld_studio_workspace/web/server/README.md) |
| Frontend (React) | [`web/src/README.md`](simworld_studio_workspace/web/src/README.md) |
| Web (full stack) | [`web/README.md`](simworld_studio_workspace/web/README.md) |
| Gym / Navigation | [`gym_env/README.md`](simworld_studio_workspace/gym_env/README.md) |
| Co-Evolve | [`co_evolve/README.md`](simworld_studio_workspace/co_evolve/README.md) |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `[cirrus] EADDRINUSE :8685` | `Get-NetTCPConnection -LocalPort 8685 \| Stop-Process -Force` |
| MCP port never ready | UE crashed — check `simworld_studio_workspace/logs/ue.log` |
| Pixel Streaming black screen | Wait 60s after UE launch; check `logs/cirrus.log` for `Streamer connected` |
| `Cannot generate unique name for X` | Fixed — actor names now include session suffix automatically |
| Agent not detected after PIE | Auto-discovery runs every 5s; or use **↻ Sync** button in Agent panel |
| VLM Scoring fails | Ensure Claude Code is authenticated: `claude` |
| Camera tab black | Recompile UE plugin for `vget /camera/actor/{name}/lit` |

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
