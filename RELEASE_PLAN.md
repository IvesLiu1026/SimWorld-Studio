# SimWorld Studio — Public Release Plan (Google Colab)

## Overview

Release SimWorld Studio as a **Google Colab notebook** that anyone can run for free. Users install SimWorld (official HuggingFace binary) + our Arena platform package, provide their own Claude API key, and get a full 3D scene generation playground in their browser. All internal Arena code (MCP tools, agent pipeline, skills) ships as **bundled/minified packages** — functional but not readable source.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│  GOOGLE COLAB (Free Tier — T4 GPU)               │
│                                                  │
│  1. SimWorld (official binary from HuggingFace)  │
│     ├── UE headless renderer (GPU, Vulkan/EGL)   │
│     ├── TCP plugin (port 9000)                   │
│     └── Pixel Streaming (port 8586)              │
│                                                  │
│  2. SimWorld Studio (pip package)           │
│     ├── Backend API (Express, bundled+minified)   │
│     ├── MCP Server (bundled+minified)            │
│     ├── Frontend (pre-built static files)        │
│     ├── Skills (bundled .md files)               │
│     └── Claude Code CLI (npm, user's API key)    │
│                                                  │
│  3. cloudflared tunnel → public browser URL      │
│                                                  │
│  USER PROVIDES: ANTHROPIC_API_KEY (stays local)  │
└──────────────────────────────────────────────────┘
```

**1:1 model** — one Colab instance = one user session.

---

## SimWorld Installation (Official)

Following the [official SimWorld docs](https://simworld.readthedocs.io/en/latest/getting_started/installation.html):

### System Requirements (Free Colab meets these)
- GPU: T4 (16GB VRAM) — exceeds 6GB minimum
- RAM: ~12GB (free Colab) — below 32GB recommended, but sufficient for headless
- Disk: ~100GB available
- Python: 3.10 (Colab default)

### Install Steps (in Colab)
```bash
# 1. Clone SimWorld Python client
!git clone https://github.com/SimWorld-AI/SimWorld.git /content/SimWorld
!cd /content/SimWorld && pip install -e .

# 2. Download SimWorld Linux binary from HuggingFace
!wget -q --show-progress -O /tmp/simworld_linux.zip \
    https://huggingface.co/datasets/SimWorld-AI/SimWorld/resolve/main/Base/Linux.zip
!mkdir -p /content/SimWorld-Binary
!unzip -q /tmp/simworld_linux.zip -d /content/SimWorld-Binary
!rm /tmp/simworld_linux.zip
!chmod +x /content/SimWorld-Binary/SimWorld.sh

# 3. (Optional) Download additional environment .pak files
# !wget -O /content/SimWorld-Binary/SimWorld/Content/Paks/Tokyo.pak \
#     https://huggingface.co/datasets/SimWorld-AI/SimWorld/resolve/main/AdditionEnvironmentPaks/Linux/<pak_file>
```

### Launch SimWorld Headless
```bash
# Install Vulkan/display dependencies for headless GPU rendering
!apt-get install -y -qq xvfb vulkan-utils mesa-vulkan-drivers libvulkan1 libegl1 libgles2

# Start virtual display
!Xvfb :99 -screen 0 1280x720x24 &
import os
os.environ['DISPLAY'] = ':99'

# Launch SimWorld headless
!cd /content/SimWorld-Binary && ./SimWorld.sh \
    /Game/Maps/empty.umap \
    -RenderOffScreen -Unattended -NOSPLASH -NOSOUND \
    -ResX=1280 -ResY=720 \
    -PixelStreamingIP=127.0.0.1 -PixelStreamingPort=8586 \
    &
```

---

## Arena Platform — What Ships to Users

### Distribution: Private pip Package

The Arena platform is distributed as a **single pip-installable package** hosted on a private GitHub Releases URL (or HuggingFace). No source code is exposed.

```
simworld-studio-{VERSION}.tar.gz
├── simworld_arena/
│   ├── __init__.py
│   ├── launcher.py              # CLI entry point: `simworld-studio start`
│   ├── version.py               # Version + auto-update check
│   ├── server/
│   │   ├── index.bundle.js      # Backend (esbuild bundled + minified)
│   │   ├── mcp-server.bundle.js # MCP server (esbuild bundled + minified)
│   │   ├── assets.json           # Asset catalog
│   │   ├── package.json          # Minimal runtime deps (express, cors)
│   │   └── dist/                 # Pre-built React frontend (static HTML/JS/CSS)
│   ├── skills/
│   │   └── builtin/             # Skill .md files
│   ├── config/
│   │   ├── arena_default.yaml
│   │   └── agent_profiles/
│   └── mcp.json                 # MCP config (generated at launch)
├── setup.py
└── pyproject.toml
```

### Build Pipeline (internal, run before release)

```bash
# 1. Build frontend
cd web && npm run build          # → web/dist/

# 2. Bundle + minify backend JS (no source maps)
npx esbuild web/server/index.js --bundle --platform=node --minify --outfile=dist/server/index.bundle.js
npx esbuild web/server/mcp-server.js --bundle --platform=node --minify --outfile=dist/server/mcp-server.bundle.js

# 3. Package into pip-installable tarball
python -m build                  # → dist/simworld_studio-{VERSION}.tar.gz

# 4. Upload to GitHub Releases
gh release create v{VERSION} dist/*.tar.gz
```

### Install in Colab
```bash
!pip install -q https://github.com/SimWorld-AI/SimWorld-Studio/releases/download/v0.1.0/simworld_studio-0.1.0.tar.gz
```

---

## Auto-Sync: Internal Changes → User Side

When we push code changes internally, users automatically get them on next Colab run:

```python
# Auto-update cell (runs first in notebook)
import subprocess, json, urllib.request

MANIFEST_URL = "https://raw.githubusercontent.com/SimWorld-AI/SimWorld-Studio/main/version.json"

manifest = json.loads(urllib.request.urlopen(MANIFEST_URL).read())
latest = manifest["latest"]
pkg_url = manifest["url"]

result = subprocess.run(["pip", "show", "simworld-studio"], capture_output=True, text=True)
current = None
for line in result.stdout.split('\n'):
    if line.startswith('Version:'):
        current = line.split(':')[1].strip()

if current != latest:
    print(f"Updating: {current} → {latest}")
    subprocess.run(["pip", "install", "-q", pkg_url])
    print("Updated! Restart runtime and re-run all cells.")
else:
    print(f"simworld-studio v{current} is up to date.")
```

**version.json** (hosted in public repo):
```json
{
  "latest": "0.1.0",
  "min_supported": "0.1.0",
  "url": "https://github.com/SimWorld-AI/SimWorld-Studio/releases/download/v0.1.0/simworld_studio-0.1.0.tar.gz"
}
```

---

## Claude Code + API Key

- User enters their `ANTHROPIC_API_KEY` via `getpass()` — never visible in notebook output
- Key is set as env var, read by Claude Code CLI locally in Colab
- Key **never** leaves the Colab runtime, never sent to any external server
- Backend spawns Claude Code CLI which reads the env var at runtime
- If key is missing or invalid, clear error message is shown

```python
from getpass import getpass
import os

api_key = getpass("Enter your Anthropic API key (sk-ant-...): ")
assert api_key.startswith("sk-ant-"), "Invalid key format. Get yours at console.anthropic.com"
os.environ["ANTHROPIC_API_KEY"] = api_key
print("API key set (stored only in this runtime's memory)")
```

---

## Complete Colab Notebook Cells

### Cell 1: GPU Check
```python
"""Check GPU availability — T4 or better required."""
!nvidia-smi
import subprocess
result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                       capture_output=True, text=True)
gpu_info = result.stdout.strip()
print(f"GPU: {gpu_info}")
assert "T4" in gpu_info or "A100" in gpu_info or "V100" in gpu_info or "L4" in gpu_info, \
    "No supported GPU found. Go to Runtime → Change runtime type → GPU."
print("GPU check passed!")
```

### Cell 2: Install SimWorld (Official)
```python
"""Install SimWorld Python client + download UE binary from HuggingFace."""
import os

# Python client
if not os.path.exists("/content/SimWorld"):
    !git clone https://github.com/SimWorld-AI/SimWorld.git /content/SimWorld
    !cd /content/SimWorld && pip install -q -e .
    print("SimWorld Python client installed.")
else:
    print("SimWorld Python client already installed.")

# UE binary
if not os.path.exists("/content/SimWorld-Binary/SimWorld.sh"):
    print("Downloading SimWorld binary (~3-5GB)...")
    !wget -q --show-progress -O /tmp/simworld_linux.zip \
        https://huggingface.co/datasets/SimWorld-AI/SimWorld/resolve/main/Base/Linux.zip
    !mkdir -p /content/SimWorld-Binary
    !unzip -q /tmp/simworld_linux.zip -d /content/SimWorld-Binary
    !rm /tmp/simworld_linux.zip
    !chmod +x /content/SimWorld-Binary/SimWorld.sh
    print("SimWorld binary installed.")
else:
    print("SimWorld binary already installed.")
```

### Cell 3: Install Studio Platform
```python
"""Install SimWorld Studio (bundled platform — no source code)."""
!pip install -q https://github.com/SimWorld-AI/SimWorld-Studio/releases/download/v0.1.0/simworld_studio-0.1.0.tar.gz
!npm install -g @anthropic-ai/claude-code 2>/dev/null
print("Studio + Claude Code CLI installed.")
```

### Cell 4: Enter API Key
```python
"""Your Anthropic API key — stays in this runtime, never sent anywhere."""
from getpass import getpass
import os

api_key = getpass("Enter your Anthropic API key (sk-ant-...): ")
assert api_key.startswith("sk-ant-"), "Invalid key format. Get yours at console.anthropic.com"
os.environ["ANTHROPIC_API_KEY"] = api_key
print("API key set (stored only in this runtime's memory)")
```

### Cell 5: Launch SimWorld + Arena
```python
"""Start SimWorld headless (GPU) and the Studio platform."""
import subprocess, time, os

# Install headless rendering dependencies
subprocess.run(["apt-get", "install", "-y", "-qq",
    "xvfb", "vulkan-utils", "mesa-vulkan-drivers", "libvulkan1", "libegl1", "libgles2"],
    capture_output=True)

# Start virtual display
subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x720x24"])
os.environ["DISPLAY"] = ":99"
time.sleep(2)

# Launch SimWorld headless
ue_proc = subprocess.Popen(
    ["/content/SimWorld-Binary/SimWorld.sh",
     "/Game/Maps/empty.umap",
     "-RenderOffScreen", "-Unattended", "-NOSPLASH", "-NOSOUND",
     "-ResX=1280", "-ResY=720",
     "-PixelStreamingIP=127.0.0.1", "-PixelStreamingPort=8586"],
    stdout=open("/content/ue.log", "w"), stderr=subprocess.STDOUT
)
print("Starting SimWorld (30-60 seconds)...")
time.sleep(40)

# Launch Arena platform
arena_proc = subprocess.Popen(
    ["simworld-studio", "start",
     "--ue-host", "127.0.0.1", "--ue-port", "9000",
     "--port", "3002"],
    stdout=open("/content/arena.log", "w"), stderr=subprocess.STDOUT
)
time.sleep(5)
print("Studio platform starting...")
```

### Cell 6: Create Public URL
```python
"""Create a tunnel so you can open the Arena in your browser."""
import subprocess, time, re

# Install cloudflared
subprocess.run(["wget", "-q",
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    "-O", "/usr/local/bin/cloudflared"], capture_output=True)
subprocess.run(["chmod", "+x", "/usr/local/bin/cloudflared"], capture_output=True)

# Start tunnel
tunnel = subprocess.Popen(
    ["cloudflared", "tunnel", "--url", "http://localhost:3002"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE
)
time.sleep(8)

# Extract public URL
output = tunnel.stderr.read(8192).decode()
url_match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', output)
if url_match:
    public_url = url_match.group(0)
    print(f"\n{'='*60}")
    print(f"  SimWorld Studio is live!")
    print(f"  Open in browser: {public_url}")
    print(f"{'='*60}\n")
else:
    print("Tunnel failed. Check /content/arena.log for errors.")
    print("Fallback: use Colab's built-in proxy:")
    print("  from google.colab.output import eval_js")
    print("  print(eval_js('google.colab.kernel.proxyPort(3002)'))")
```

### Cell 7: Verify Everything Works
```python
"""Automated verification — checks all services before you start."""
import requests, socket, subprocess, os, json

print("Running verification checks...\n")

results = {}

# 1. GPU
r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                   capture_output=True, text=True)
gpu_ok = r.returncode == 0
results["GPU"] = (gpu_ok, r.stdout.strip() if gpu_ok else "Not found")

# 2. SimWorld binary exists
sw_ok = os.path.exists("/content/SimWorld-Binary/SimWorld.sh")
results["SimWorld Binary"] = (sw_ok, "Installed" if sw_ok else "Missing")

# 3. SimWorld TCP connection (port 9000)
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(("127.0.0.1", 9000))
    s.close()
    results["SimWorld TCP (port 9000)"] = (True, "Connected")
except Exception as e:
    results["SimWorld TCP (port 9000)"] = (False, str(e))

# 4. Backend API health
try:
    r = requests.get("http://localhost:3002/api/health", timeout=10)
    results["Arena Backend API"] = (r.status_code == 200, f"HTTP {r.status_code}")
except Exception as e:
    results["Arena Backend API"] = (False, str(e))

# 5. Skills loaded
try:
    r = requests.get("http://localhost:3002/api/skills", timeout=10)
    data = r.json()
    count = len(data) if isinstance(data, list) else 0
    results["Skills Loaded"] = (count > 0, f"{count} skills")
except Exception as e:
    results["Skills Loaded"] = (False, str(e))

# 6. Assets catalog
try:
    r = requests.get("http://localhost:3002/api/assets", timeout=10)
    results["Asset Catalog"] = (r.status_code == 200, "Loaded")
except Exception as e:
    results["Asset Catalog"] = (False, str(e))

# 7. Claude Code CLI
r = subprocess.run(["claude", "--version"], capture_output=True, text=True)
results["Claude Code CLI"] = (r.returncode == 0, r.stdout.strip() if r.returncode == 0 else "Not found")

# 8. API key
key_set = os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-")
results["Anthropic API Key"] = (key_set, "Configured" if key_set else "NOT SET")

# 9. Tunnel URL
try:
    results["Public URL (tunnel)"] = (bool(public_url), public_url)
except NameError:
    results["Public URL (tunnel)"] = (False, "Not created — run Cell 6")

# Print results
all_ok = True
for name, (ok, detail) in results.items():
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_ok = False
    print(f"  [{status}] {name}: {detail}")

print()
if all_ok:
    print("ALL CHECKS PASSED — SimWorld Studio is ready!")
    print(f"Open {public_url} in your browser to start building 3D scenes.")
else:
    print("SOME CHECKS FAILED — see above. Common fixes:")
    print("  - SimWorld TCP: wait longer (60s+) for UE to boot, then re-run this cell")
    print("  - API key: run Cell 4")
    print("  - Tunnel: re-run Cell 6")
```

### Cell 8: Quick Smoke Test (End-to-End)
```python
"""Send a test prompt through the full pipeline: Chat → Claude → MCP → SimWorld."""
import requests, json

print("Running end-to-end smoke test...\n")

response = requests.post("http://localhost:3002/api/chat", json={
    "message": "Set up the environment with a sunny sky, then spawn one small residential building",
    "sessionId": "colab-verify",
    "skills": ["building_placement"]
}, stream=True, timeout=120)

tool_calls = 0
text_chunks = 0

for line in response.iter_lines():
    if line:
        decoded = line.decode('utf-8')
        if decoded.startswith('data: '):
            try:
                data = json.loads(decoded[6:])
                if data.get('type') == 'text':
                    text_chunks += 1
                    print(data.get('content', ''), end='')
                elif data.get('type') == 'tool_call':
                    tool_calls += 1
                    print(f"\n  [Tool Call] {data.get('name')}")
            except json.JSONDecodeError:
                pass

print(f"\n\nSmoke test complete: {text_chunks} text chunks, {tool_calls} tool calls")
if tool_calls > 0:
    print("SUCCESS — Full pipeline working (Chat → Claude → MCP Tools → SimWorld)")
else:
    print("WARNING — No tool calls detected. Check API key and logs.")
```

---

## Code Protection Strategy

| Layer | Method | Details |
|---|---|---|
| **Frontend** | Pre-built static files | `npm run build` → minified HTML/JS/CSS, no source |
| **Backend JS** | esbuild bundle + minify | Single-file bundles, no source maps, unreadable |
| **MCP Server** | esbuild bundle + minify | Same as backend |
| **Python launcher** | Minimal wrapper only | Only `launcher.py` and `version.py` — no agent logic |
| **Skills** | Bundled as package data | Functional but inside installed package |
| **Agent pipeline** | Runs via Claude Code CLI | User only sees tool names/results, not implementation |

The user interacts with the Arena through the browser UI. They see tool call names and results but never the MCP tool implementations, prompt engineering, or agent orchestration code.

---

## Key Design Decisions

| Decision | Choice | Reason |
|---|---|---|
| Colab tier | Free (T4, 12GB RAM) | Maximum accessibility |
| Hosting | GitHub Releases | Simple, free, no cloud vendor lock-in |
| User sessions | 1:1 (one Colab = one user) | Simplicity, isolation |
| API key handling | User's own key, `getpass()`, env var only | Privacy, no liability |
| Code protection | Bundle + minify (no obfuscation for now) | Sufficient for deterrence; revisit if needed |
| SimWorld install | Official HuggingFace binary | No maintenance burden, follows upstream |
| Auto-sync | version.json + pip upgrade | Simple, reliable |
| Pixel Streaming | Via cloudflared tunnel | Works; no fallback to screenshot-only |

---

## Deliverables (1-Day Sprint)

### Morning: Build Pipeline
- [ ] Write `build.sh` — bundles frontend, minifies backend JS, creates pip package
- [ ] Create `launcher.py` — CLI entry point that starts backend + MCP server
- [ ] Create `version.py` + `version.json` manifest
- [ ] Create `setup.py` / `pyproject.toml` for pip package

### Afternoon: Colab Notebook + Testing
- [ ] Write `SimWorld_Studio.ipynb` with all 8 cells above
- [ ] Test full pipeline in Colab: install → launch → chat → scene generation
- [ ] Verify GPU rendering works on T4
- [ ] Verify cloudflared tunnel works
- [ ] Verify auto-update mechanism works
- [ ] Fix any Colab-specific issues (paths, permissions, deps)

### End of Day: Release
- [ ] Upload package to GitHub Releases
- [ ] Publish notebook (Colab link in README)
- [ ] Verify a clean Colab run from scratch works end-to-end

---

## File Structure

```
SimWorld-Studio-Release/
├── RELEASE_PLAN.md                    # This document
├── build.sh                           # Build + package script
├── SimWorld_Studio.ipynb         # The Colab notebook
├── version.json                       # Version manifest
├── packaging/
│   ├── setup.py                       # pip package config
│   ├── pyproject.toml
│   ├── launcher.py                    # Entry point
│   └── version.py                     # Version check + auto-update
└── README.md                          # Public-facing quick start
```
