# SimWorld Studio

Vibe code the physical world for embodied agents. Chat with coding agent to build and simulate virtual environments in Unreal Engine.

https://github.com/user-attachments/assets/36a43835-e1c5-4304-a506-bcae9cd4126a

## Quick Start

### Option A: Google Colab (no local GPU needed)

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/SimWorld-AI/SimWorld-Studio/blob/main/SimWorld_Studio.ipynb)

Run all cells in order. Setup takes ~5 minutes. Requires a free Colab GPU runtime and an Anthropic API key.

### Option B: Local / Remote GPU Server (Linux + NVIDIA GPU)

#### Prerequisites

- **OS**: Linux (Ubuntu 20.04+ recommended)
- **GPU**: NVIDIA GPU with 8GB+ VRAM (tested on L40S, T4, A100)
- **NVIDIA drivers**: 525+ with Vulkan support
- **Node.js**: 18+
- **Python**: 3.9+
- **Disk**: ~40 GB free (15 GB download + 21 GB extracted)

#### 1. Download the Minimal SimWorld Binary

```bash
# Download (~15 GB compressed, ~21 GB extracted)
wget -O SimWorld-Studio-Minimal.tar.gz \
    https://huggingface.co/datasets/SimWorld-AI/SimWorld-Studio/resolve/main/SimWorld-Studio-Minimal.tar.gz

tar xzf SimWorld-Studio-Minimal.tar.gz
```

#### 2. Install SimWorld Studio

```bash
pip install git+https://github.com/SimWorld-AI/SimWorld-Studio.git#subdirectory=packaging
npm install -g @anthropic-ai/claude-code
```

#### 3. Authenticate with Claude

**Option A — API Key:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```
Get your key at [console.anthropic.com](https://console.anthropic.com).

**Option B — Claude Code Login (no API key needed):**
```bash
claude
```
This opens a browser for OAuth login. If on a headless server, use the API key option instead.

#### 4. Launch (one command)

```bash
simworld-studio start
```

This will:
- Detect your GPU and authenticate with Claude
- Launch Unreal Engine (headless)
- Wait for the engine to be ready
- Start the Studio web server
- Print the URL to open in your browser

On multi-GPU systems, it will ask which GPU to use (or pass `--gpu INDEX`).

For remote servers, it auto-detects your IP and prints SSH tunnel instructions.

**Options:**
```
--gpu INDEX          GPU to use (auto-detected if omitted)
--port PORT          Web UI port (default: 3002)
--binary PATH        Path to SimWorld-Studio-Minimal directory
--ue-path PATH       Path to UnrealEditor binary (overrides auto-detection)
--project PATH       Path to .uproject file (overrides auto-detection)
--mock               Enable mock mode (replay pre-recorded responses, no GPU needed)
--mock-file PATH     Path to mock responses file (used with --mock)
```

Try: *"Set up the environment with a sunny sky, then build a small neighborhood with 4 houses and trees"*

---

## What Can SimWorld Studio Do?

SimWorld Studio lets you build 3D physical scenes by chatting with a coding agent. Describe what you want in natural language and watch the scene come together in real time.

- **Spawn buildings** — place residential houses, commercial buildings, and more to lay out neighborhoods and city blocks
- **Place props and vegetation** — add trees, vehicles, street furniture, fences, and other objects to fill your scene
- **Multi-turn sessions** — iteratively refine scenes through conversation; add, move, remove, or rearrange objects across multiple turns
- **Built-in skills** — pre-made prompts for common tasks like city layout patterns, weather moods, and building placement guides
- **Scene verification** — `verify_scene` tool takes a screenshot and asks Claude to evaluate placement quality, returning structured feedback (PASS / NEEDS_IMPROVEMENT / FAIL) with actionable suggestions
- **Mock mode** — record a real agent session and replay it as a demo without a GPU

---

## Mock Mode (Demo Recording & Playback)

Mock mode lets you record a real agent run and replay it as a demo — no GPU or Unreal Engine required.

### Record a session

Run normally; the agent's tool calls and responses are automatically logged to the workspace `logs/` directory.

### Replay as a demo

```bash
simworld-studio start \
  --mock \
  --mock-file /path/to/mock_responses.txt \
  --gpu 6   # ignored in mock mode, but accepted
```

In mock mode:
- The web UI loads and plays back the recorded agent trajectory in real time
- Screenshots captured during the original run are served from the logs
- Click **Play Demo** in the UI to start playback
- No Unreal Engine process is launched; the server replays MCP tool responses from the file

This is useful for creating reproducible demos, presentations, or CI smoke tests.

> **Note on demo assets:** The demo video above showcases scenes built with high-quality commercial 3D assets (buildings, vehicles, characters, etc.) that are **not included** in the open-source release due to licensing restrictions. The redistributable Minimal build ships with a different set of freely licensed assets, so the visual appearance will differ from the demo. The functionality and workflow remain the same.

---

## Architecture

```
Browser (React UI)
    |
    |-- Chat with Claude --> Claude Code CLI --> MCP Tools
    |                                              |
    +-- Pixel Streaming <-- Unreal Engine 5.3 <----+
                              (headless GPU)
```

- **Frontend**: React + TypeScript (pre-built, served by backend)
- **Backend**: Node.js + Express (port 3002)
- **MCP Server**: Bridges Claude <-> UE via TCP (port 55559)
- **UE**: Headless Unreal Editor with UnrealMCP plugin

### Custom UE / Project Paths

By default `simworld-studio start` looks for the engine and project inside the `--binary` directory. You can override either path:

```bash
# via CLI flags
simworld-studio start \
  --ue-path /opt/UnrealEngine/Engine/Binaries/Linux/UnrealEditor \
  --project /home/user/MyProject/MyProject.uproject

# or via environment variables
export SIMWORLD_UE_PATH=/opt/UnrealEngine/Engine/Binaries/Linux/UnrealEditor
export SIMWORLD_PROJECT_PATH=/home/user/MyProject/MyProject.uproject
simworld-studio start
```

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `Vulkan memory crash` | Use `--gpu 0` flag; install `vulkan-tools mesa-vulkan-drivers` |
| `MCP port not opening` | Wait 60s more; check GPU drivers with `nvidia-smi` |
| `game module not found` | Ensure you extracted the full archive; check `gym_citynav/Binaries/Linux/` |
| `CUDA context error` | Set `--gpu INDEX` to isolate a single GPU |
| `Claude errors` | Run `claude login` or verify `ANTHROPIC_API_KEY` is set |
| `Can't access UI remotely` | Use SSH tunnel: `ssh -L 3002:localhost:3002 -L 8585:localhost:8585 user@server` |
| `No GPU detected` | Install NVIDIA drivers 525+; verify with `nvidia-smi` |

### View Logs

```bash
# UE logs (in the SimWorld-Studio-Minimal directory)
tail -f gym_citynav/Saved/Logs/gym_citynav.log

# Studio backend logs
tail -f simworld_studio_workspace/logs/server.log
```

---

## Embodied Navigation Experiments

The `gym_env` harness runs reproducible LLM navigation experiments in UE
via UnrealCV + MCP. See [`gym_env/README.md`](simworld_studio_workspace/gym_env/README.md)
for the full reference.

### Pre-generated Task Sets

Generate a fixed, seeded batch of episodes offline (no UE required) and
split into train/test. All geodesic paths are baked into the file, so
runtime skips navmesh construction entirely.

```bash
cd simworld_studio_workspace

# Generate 30 PointNav episodes, split 22 train / 8 test
python -m nav_task \
    --map ../SimWorld/simworld/data/roads.json \
    --seed 42 --n-episodes 30 \
    --min-path-length 1000 --max-path-length 4000 \
    --split 22,8 \
    --train-out tasks/pointnav_train.json \
    --test-out tasks/pointnav_test.json
```

### Train / Test Workflow

```bash
# 1. Train — memory accumulates across episodes
python -m gym_env.batch_runner --mode batch \
    --episodes-file tasks/pointnav_train.json \
    --n-tasks 22 --eval-mode train \
    --memory strategy --model qwen \
    --base-url http://gpu-server:8000/v1 --api-key EMPTY

# 2. Test — frozen memory, no new writes
python -m gym_env.batch_runner --mode batch \
    --episodes-file tasks/pointnav_test.json \
    --n-tasks 8 --eval-mode test \
    --memory strategy --model qwen \
    --base-url http://gpu-server:8000/v1 --api-key EMPTY
```

| Flag | Description |
|------|-------------|
| `--episodes-file` | Load pre-generated episodes (skips navmesh + sampling) |
| `--eval-mode train` | Memory read-write (default) |
| `--eval-mode test` | Memory read-only — query training memories, write nothing |
| `--memory` | Backend: `none` / `text` / `mem0` / `strategy` / `hierarchical` |

---

## For Developers

### Build from Source

```bash
# Requires access to the simworld_arena source repo
./build.sh
```

### Release

1. `./build.sh` -> creates `dist/simworld_studio-{VERSION}.tar.gz`
2. Upload to GitHub Releases
3. Update `version.json`

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
