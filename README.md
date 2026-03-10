# SimWorld Studio

AI-powered 3D scene generation platform. Chat with Claude to build urban scenes in Unreal Engine — runs entirely in Google Colab.

## Quick Start (Users)

**Open the notebook in Google Colab and run all cells:**

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/SimWorld-AI/SimWorld-Studio/blob/main/SimWorld_Studio.ipynb)

The notebook will:
1. Check your GPU (free T4 is fine)
2. Install SimWorld from [HuggingFace](https://huggingface.co/datasets/SimWorld-AI/SimWorld)
3. Install the SimWorld Studio platform
4. Ask for your [Anthropic API key](https://console.anthropic.com) (stays local — never sent to us)
5. Launch everything and give you a browser URL
6. Run verification checks + smoke test

**Requirements:** Google Colab with GPU runtime, an Anthropic API key.

---

## For Developers (Building from Source)

### Prerequisites

- Node.js 18+
- Python 3.9+
- Access to the `simworld_arena` source repo (sibling directory)

### Build the Package

```bash
# From this directory
./build.sh
```

This will:
- Build the React frontend (`npm run build`)
- Minify all backend JS with esbuild
- Patch the server to serve the frontend statically
- Bundle skills, assets, and config
- Output `dist/simworld_studio-{VERSION}.tar.gz`

### Test Locally

```bash
pip install dist/simworld_studio-0.1.0.tar.gz
simworld-studio start --ue-port 9000 --port 3002
```

### Release

1. Run `./build.sh`
2. Upload `dist/*.tar.gz` to GitHub Releases
3. Update `version.json` with the new version and download URL
4. Update `STUDIO_PKG_URL` in `SimWorld_Studio.ipynb` Cell 3

### Auto-Update

Users get updates automatically. On each notebook run, it checks `version.json` and upgrades the pip package if a newer version is available.

---

## Architecture

```
User's Colab (T4 GPU)
├── SimWorld binary (HuggingFace)  ← headless UE renderer
├── SimWorld Studio (pip package)  ← backend + frontend + MCP tools
├── Claude Code CLI (npm)          ← uses user's own API key
└── cloudflared tunnel             ← public browser URL
```

All internal code ships as minified JS bundles — no source code is exposed.

---

## File Structure

```
SimWorld-Studio/
├── SimWorld_Studio.ipynb        # Colab notebook for users
├── build.sh                     # Build pipeline
├── version.json                 # Release manifest (auto-update)
├── patch_server.js              # Adds frontend serving to backend
├── packaging/                   # pip package source
│   ├── pyproject.toml
│   ├── simworld_arena/
│   │   ├── launcher.py          # CLI: simworld-studio start
│   │   ├── version.py           # Auto-update logic
│   │   ├── server/              # Minified JS (backend + MCP)
│   │   ├── skills/builtin/      # Skill definitions (.md)
│   │   └── config/              # Default config
└── dist/                        # Built package (after ./build.sh)
```
