# SimWorld Studio

AI-powered 3D scene generation platform. Chat with Claude to build virtual urban scenes in Unreal Engine.

https://github.com/user-attachments/assets/placeholder-demo-video

## Quick Start

### Option A: Google Colab (no local GPU needed)

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/SimWorld-AI/SimWorld-Studio/blob/main/SimWorld_Studio.ipynb)

Run all cells in order. Setup takes ~5 minutes. Requires a free Colab GPU runtime and an Anthropic API key.

### Option B: Local Setup (Linux + NVIDIA GPU)

#### Prerequisites

- **OS**: Linux (Ubuntu 20.04+ recommended)
- **GPU**: NVIDIA GPU with 8GB+ VRAM (tested on L40S, T4, A100)
- **NVIDIA drivers**: 525+ with Vulkan support
- **Node.js**: 18+
- **Python**: 3.9+
- **Disk**: ~5 GB free (for the minimal SimWorld binary)

#### 1. Download the Minimal SimWorld Binary

```bash
# Download (~2.7 GB compressed, ~7.6 GB extracted)
wget -O SimWorld-Studio-Minimal.tar.gz \
    https://huggingface.co/datasets/SimWorld-AI/SimWorld-Studio/resolve/main/SimWorld-Studio-Minimal.tar.gz

tar xzf SimWorld-Studio-Minimal.tar.gz
```

#### 2. Install SimWorld Studio

```bash
pip install simworld-studio
npm install -g @anthropic-ai/claude-code
```

#### 3. Authenticate with Claude

You have two options:

**Option A — Claude Code Login (recommended, no API key needed):**
```bash
claude login
```
This opens a browser for OAuth login. Once authenticated, Claude Code (and SimWorld Studio) will use your Claude account automatically.

**Option B — API Key:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```
Get your key at [console.anthropic.com](https://console.anthropic.com).

#### 4. Launch SimWorld (headless)

```bash
cd SimWorld-Studio-Minimal
./SimWorld-Studio.sh --render-offscreen
```

Wait ~30-60 seconds for the MCP port (55559) to become available. You'll see log output in the terminal.

#### 5. Launch Studio (in a second terminal)

```bash
simworld-studio start --port 3002
```

#### 6. Open in Browser

Go to **http://localhost:3002** and start chatting!

Try: *"Set up the environment with a sunny sky, then build a small neighborhood with 4 houses and trees"*

---

## What Can SimWorld Studio Do?

- **Spawn buildings** — 127 building varieties (residential to skyscrapers)
- **Place props** — trees, vehicles, street furniture, and more
- **Control lighting** — sun position, fog, atmosphere, time of day
- **Take screenshots** — automated camera tours and captures
- **Multi-turn sessions** — iteratively refine scenes through conversation
- **Built-in skills** — city layout patterns, weather moods, building guides

---

## Architecture

```
Browser (React UI)
    │
    ├── Chat with Claude ──→ Claude Code CLI ──→ MCP Tools
    │                                              │
    └── Pixel Streaming ◄── Unreal Engine 5.3 ◄───┘
                              (headless GPU)
```

- **Frontend**: React + TypeScript (pre-built, served by backend)
- **Backend**: Node.js + Express (port 3002)
- **MCP Server**: Bridges Claude ↔ UE via TCP (port 55559)
- **UE**: Headless Unreal Editor with UnrealMCP plugin

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `MCP port not opening` | Wait 60s more; check GPU drivers with `nvidia-smi` |
| `game module not found` | Ensure you extracted the full archive; check `gym_citynav/Binaries/Linux/` |
| `Claude errors` | Run `claude login` or verify `ANTHROPIC_API_KEY` is set |
| `No GPU detected` | Install NVIDIA drivers 525+; verify with `nvidia-smi` |
| `Screenshot fails` | Ensure UE has finished loading (wait for MCP port) |

### View Logs

```bash
# UE logs (in the SimWorld-Studio-Minimal directory)
tail -f gym_citynav/Saved/Logs/gym_citynav.log

# Studio backend logs
tail -f simworld_studio_workspace/logs/server.log
```

---

## For Developers

### Build from Source

```bash
# Requires access to the simworld_arena source repo
./build.sh
```

### Release

1. `./build.sh` → creates `dist/simworld_studio-{VERSION}.tar.gz`
2. Upload to GitHub Releases
3. Update `version.json`

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
