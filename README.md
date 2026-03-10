# SimWorld Studio

AI-powered 3D scene generation platform. Chat with Claude to build virtual urban scenes in Unreal Engine.

https://github.com/user-attachments/assets/placeholder-demo-video

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
- **Disk**: ~10 GB free (2.7 GB download + 7.6 GB extracted)

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
./SimWorld-Studio.sh --gpu 0 --render-offscreen
```

> **Multi-GPU systems**: You **must** specify `--gpu INDEX` to select which GPU to use. Without it, Vulkan may crash trying to enumerate all GPUs.

Wait ~30-60 seconds for the MCP port (55559) to become available.

#### 5. Launch Studio (in a second terminal)

```bash
simworld-studio start --port 3002
```

#### 6. Open in Browser

**If running locally:** Go to **http://localhost:3002**

**If running on a remote GPU server:** Use SSH port forwarding:
```bash
# From your laptop (replace SERVER_IP with your GPU server's address)
ssh -L 3002:localhost:3002 user@SERVER_IP
```
Then open **http://localhost:3002** in your laptop browser.

Alternatively, access directly via **http://SERVER_IP:3002** if the port is open.

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

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `Vulkan memory crash` | Use `--gpu 0` flag; install `vulkan-tools mesa-vulkan-drivers` |
| `MCP port not opening` | Wait 60s more; check GPU drivers with `nvidia-smi` |
| `game module not found` | Ensure you extracted the full archive; check `gym_citynav/Binaries/Linux/` |
| `CUDA context error` | Set `--gpu INDEX` to isolate a single GPU |
| `Claude errors` | Run `claude login` or verify `ANTHROPIC_API_KEY` is set |
| `Can't access UI remotely` | Use SSH tunnel: `ssh -L 3002:localhost:3002 user@server` |
| `No GPU detected` | Install NVIDIA drivers 525+; verify with `nvidia-smi` |

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

1. `./build.sh` -> creates `dist/simworld_studio-{VERSION}.tar.gz`
2. Upload to GitHub Releases
3. Update `version.json`

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
