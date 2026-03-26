# SimWorld Studio — Internal Server Setup Guide

This guide is for team members running SimWorld Studio on our shared server (`sn4622121915`).

## Shared Resources

| Resource | Path |
|---|---|
| UE Engine | `/data/murray/ue/UE_5.3.2` |
| UE Project | `/data/murray/simworld_projects/SimWorld.uproject` |

**Do NOT modify files under these paths.** They are shared by all users.

## Port Assignments

Each person **must** use unique ports to avoid conflicts. Pick an unused slot from the table below and add your name:

| User | GPU | Web UI | MCP | Cirrus HTTP | Cirrus WS | Cirrus SFU |
|------|-----|--------|-----|-------------|-----------|------------|
| murray | 0 | 3002 | 55560 | 8685 | 8686 | 8989 |
| james | 0 | 3003 | 55779 | 8585 | 8586 | 8889 |
| (your name) | 1 | 3004 | 55561 | 8687 | 8688 | 8990 |
| (your name) | 2 | 3005 | 55562 | 8689 | 8690 | 8991 |
| (your name) | 3 | 3006 | 55563 | 8691 | 8692 | 8992 |
| (your name) | 4 | 3007 | 55564 | 8693 | 8694 | 8993 |
| (your name) | 5 | 3008 | 55565 | 8695 | 8696 | 8994 |
| (your name) | 6 | 3009 | 55566 | 8697 | 8698 | 8995 |
| (your name) | 7 | 3010 | 55567 | 8699 | 8700 | 8996 |

**Rules:**
- Each GPU can only run **one** UE instance at a time
- Check GPU availability before launching: `nvidia-smi`
- If a GPU is occupied, pick a different one

## Setup (One-Time)

### 1. Install SimWorld Studio

```bash
pip install git+https://github.com/SimWorld-AI/SimWorld-Studio-Internal.git#subdirectory=packaging
npm install -g @anthropic-ai/claude-code
```

### 2. Authenticate with Claude

**Option A — API Key (recommended for server):**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

**Option B — Claude Code Login:**
```bash
claude
```

### 3. Set Up Your Workspace

Each user needs their own workspace directory. Clone the repo under your home directory:

```bash
cd ~
git clone git@github.com:SimWorld-AI/SimWorld-Studio-Internal.git SimWorld-Studio
```

Your workspace will be created at `~/SimWorld-Studio/simworld_studio_workspace/` on first launch.

### 4. Build the Frontend (first time only)

```bash
cd ~/SimWorld-Studio/simworld_studio_workspace/web
npm install
npm run build
```

## Launch

Replace the port numbers below with **your assigned ports** from the table above:

```bash
export UE_ROOT=/data/murray/ue/UE_5.3.2
export UE_PROJECT_PATH=/data/murray/simworld_projects

simworld-studio start \
  --data-dir ~/SimWorld-Studio/simworld_studio_workspace \
  --gpu <YOUR_GPU> \
  --port <YOUR_WEB_PORT> \
  --mcp-port <YOUR_MCP_PORT> \
  --cirrus-http-port <YOUR_CIRRUS_HTTP> \
  --cirrus-ws-port <YOUR_CIRRUS_WS> \
  --cirrus-sfu-port <YOUR_CIRRUS_SFU>
```

**Example (using slot 3, GPU 1):**

```bash
export UE_ROOT=/data/murray/ue/UE_5.3.2
export UE_PROJECT_PATH=/data/murray/simworld_projects

simworld-studio start \
  --data-dir ~/SimWorld-Studio/simworld_studio_workspace \
  --gpu 1 \
  --port 3004 \
  --mcp-port 55561 \
  --cirrus-http-port 8687 \
  --cirrus-ws-port 8688 \
  --cirrus-sfu-port 8990
```

## Access the UI

### From your local machine (SSH tunnel)

Replace ports with your assigned values:

```bash
ssh -L 3004:localhost:3004 -L 8687:localhost:8687 <your_user>@sn4622121915
```

Then open http://localhost:3004 in your browser.

### Pixel Streaming (live UE viewport)

The Pixel Streaming tab in the UI connects to the Cirrus HTTP port. The SSH tunnel above forwards both the web UI and Cirrus ports, so it should work automatically.

If you see **"WebSocket disconnected"** in the Pixel Streaming panel:
1. Make sure your SSH tunnel includes the Cirrus HTTP port (`-L <cirrus_http>:localhost:<cirrus_http>`)
2. Wait ~60 seconds after launch for UE to fully initialize and connect to Cirrus
3. Check that no one else is using your assigned ports: `ss -tlnp | grep <your_port>`

## Troubleshooting

### Check if your ports are free

```bash
ss -tlnp | grep -E '<your_web_port>|<your_mcp_port>|<your_cirrus_http>'
```

### Check GPU availability

```bash
nvidia-smi
```

### View logs

```bash
# UE log
tail -f ~/SimWorld-Studio/simworld_studio_workspace/logs/ue.log

# Cirrus (pixel streaming) log
tail -f ~/SimWorld-Studio/simworld_studio_workspace/logs/cirrus.log

# Server log (web backend)
# Check terminal output where simworld-studio is running
```

### Common issues

| Issue | Fix |
|---|---|
| `ENOENT: web/dist/index.html` | Run `cd ~/SimWorld-Studio/simworld_studio_workspace/web && npm install && npm run build` |
| WebSocket disconnected | Wait for UE to finish loading (~60s). Check Cirrus log for `streamer connected`. |
| Port already in use | Someone else is using your port. Check with `ss -tlnp \| grep <port>` and pick a free slot. |
| Cirrus says "already running" | Another user's Cirrus is on the default ports. Make sure you pass `--cirrus-http-port` etc. |
| No GPU available | Run `nvidia-smi` and pick a GPU with enough free VRAM (~4 GB minimum). |
| `PixelStreaming not working` | Ensure the UE project has `PixelStreaming` plugin enabled in `.uproject`. It's already enabled in the shared project. |

## Stopping

Press `Ctrl+C` in the terminal where `simworld-studio start` is running. This stops UE, Cirrus, and the web server.

If processes linger:

```bash
# Find your processes
ps aux | grep $USER | grep -E 'UnrealEditor|cirrus|node.*server'

# Kill them
kill <pid>
```
