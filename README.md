# SimWorld Studio

Vibe code the physical world for embodied agents. Chat with coding agent to build and simulate virtual environments in Unreal Engine.

<video src="assets/compressed_SimWorld_Studio.mp4" controls width="100%"></video>

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
--gpu INDEX    GPU to use (auto-detected if omitted)
--port PORT    Web UI port (default: 3002)
--binary PATH  Path to SimWorld-Studio-Minimal directory
```

Try: *"Set up the environment with a sunny sky, then build a small neighborhood with 4 houses and trees"*

---

## What Can SimWorld Studio Do?

SimWorld Studio lets you build 3D physical scenes by chatting with a coding agent. Describe what you want in natural language and watch the scene come together in real time.

- **Spawn buildings** — place residential houses, commercial buildings, and more to lay out neighborhoods and city blocks
- **Place props and vegetation** — add trees, vehicles, street furniture, fences, and other objects to fill your scene
- **Multi-turn sessions** — iteratively refine scenes through conversation; add, move, remove, or rearrange objects across multiple turns
- **Built-in skills** — pre-made prompts for common tasks like city layout patterns, weather moods, and building placement guides

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
