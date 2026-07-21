# SimWorld Studio — AWS Deployment

Lab-scale multi-tenant deploy (≤5 users, 2–3 concurrent) on a single GPU EC2 instance.

## What gets `git clone`-d vs not

`git clone` of this repo gives you:

- Web server, frontend, scripts, configs (this whole tree)
- The empty workspace structure under `simworld_studio_workspace/`

It does **not** include (too big or licensed):

| Asset | Size | Where to get it |
|---|---|---|
| UE 5.3.2 engine | 58 GB | Epic launcher, or pre-stage on host / S3 / HF |
| UE project skeleton (`SimWorld.uproject`, `Plugins/`, `Source/`, `Config/`) | ~500 MB | Your dev box / HF dataset |
| UE Content (`Content/`) | 100+ GB | HuggingFace — `bootstrap.sh` pulls automatically |

`bootstrap.sh` handles everything except the first two — it'll print
clear rsync commands if it can't auto-fetch them.

## Architecture

```
        ┌──────────────────────────────────────────────┐
HTTPS   │  Nginx + Let's Encrypt + Basic Auth          │  :443
   │    │  /                → :3002 (web)              │
   │    │  /cirrus/{slot}/* → :85xx (Cirrus per slot)  │
   ▼    └──────────┬───────────────────────────────────┘
┌─────────────────┴──────────────────────────────────────┐
│  EC2 g5.4xlarge (1×A10G, 64GB RAM) — default            │
│  (scale up to g5.12xlarge if you need 4 isolated GPUs)  │
│                                                         │
│  ┌───────────────────────────────────────────────────┐ │
│  │ simworld-web.service (node web/server/index.js)   │ │
│  │   - acquires/heartbeats/releases sessions          │ │
│  │   - per-token MCP/UCV port routing                 │ │
│  └─────────────┬─────────────────────────────────────┘ │
│                │ controls                                │
│  ┌─────────────▼─────────────────────────────────────┐ │
│  │ SlotPool (slot-pool.js, in-process)               │ │
│  │   acquire(slotId) → spawn slot-launcher.sh        │ │
│  │   release(slotId) → SIGTERM child UE              │ │
│  └─────────────┬─────────────────────────────────────┘ │
│       slot 0      slot 1      slot 2                    │
│       ▼           ▼           ▼                          │
│   ┌────────┐ ┌────────┐ ┌────────┐                      │
│   │ UE     │ │ UE     │ │ UE     │   ← all 3 share      │
│   │ GPU 0  │ │ GPU 0  │ │ GPU 0  │     same A10G        │
│   │ MCP    │ │ MCP    │ │ MCP    │                      │
│   │ 55559  │ │ 55561  │ │ 55563  │                      │
│   │ Cirrus │ │ Cirrus │ │ Cirrus │                      │
│   │ 8585   │ │ 8587   │ │ 8589   │                      │
│   │ UCV    │ │ UCV    │ │ UCV    │                      │
│   │ 9017   │ │ 9018   │ │ 9019   │                      │
│   └────────┘ └────────┘ └────────┘                      │
│   /var/lib/simworld/slots/{0,1,2}/  (per-slot Saved)    │
│                                                         │
│   Shared read-only:                                     │
│   /opt/ue-engine/      (58 GB UE 5.3.2)                │
│   /opt/simworld-project/  (uproject + Plugins)         │
│   /opt/simworld-content/  (from HuggingFace)           │
│                                                         │
│   coturn :3478 UDP/TCP, :5349 TLS, UDP 49160-49200      │
└─────────────────────────────────────────────────────────┘
```

## Port plan (per slot, stride = 2)

| Resource | Formula | Slot 0 | Slot 1 | Slot 2 | Slot 3 |
|---|---|---|---|---|---|
| MCP (TCP) | 55559 + 2·slot | 55559 | 55561 | 55563 | 55565 |
| Cirrus HTTP | 8585 + 2·slot | 8585 | 8587 | 8589 | 8591 |
| Cirrus WS | 8586 + 2·slot | 8586 | 8588 | 8590 | 8592 |
| Cirrus SFU | 8989 + 2·slot | 8989 | 8991 | 8993 | 8995 |
| UnrealCV | 9017 + slot | 9017 | 9018 | 9019 | 9020 |
| **GPU index** | **slot % UE_GPU_COUNT** | 0 | 0 | 0 | 0 |

All these listen on `127.0.0.1` only; public access goes through Nginx.

## Instance sizing

Default config is for **g5.4xlarge** (1× A10G, 16 vCPU, 64 GB RAM) — three
UE slots share one GPU, fine for 2-3 concurrent users on a 15 FPS cap.

| Instance | GPU | vCPU | RAM | UE_POOL_SIZE | UE_GPU_COUNT | $/h | $/mo (12h) |
|---|---|---|---|---|---|---|---|
| g5.2xlarge | 1× A10G | 8 | 32 GB | 2 | 1 | $1.21 | ~$440 |
| **g5.4xlarge** ⭐ | 1× A10G | 16 | 64 GB | **3** | **1** | $1.62 | ~$585 |
| g5.8xlarge | 1× A10G | 32 | 128 GB | 4 | 1 | $2.45 | ~$885 |
| g5.12xlarge | 4× A10G | 48 | 192 GB | 4 | 4 | $5.67 | ~$2050 |

To change tier: edit `UE_POOL_SIZE` and `UE_GPU_COUNT` in
`/etc/default/simworld`, then `systemctl restart simworld-web`. No code
changes needed.

## Files

| Path | Purpose |
|---|---|
| `scripts/bootstrap.sh` | **One-command** post-clone provision (calls everything below) |
| `scripts/stage-project-to-aws.sh` | Run on local dev box — rsyncs UE project skeleton to EC2 |
| `scripts/slot-launcher.sh` | Launch one UE instance for a given slot |
| `scripts/slot-pool.js` | Node module: child-process lifecycle for N slots |
| `scripts/session-shim.js` | Wires SlotPool into session-manager via env var |
| `scripts/per-session-ports.js` | Routes internal UE calls from the HttpOnly Studio session cookie |
| `scripts/bake-ami.sh` | Provision OS deps, NVIDIA driver, users, systemd units |
| `scripts/download-content.sh` | Pull Content from HuggingFace once |
| `scripts/test-slot-pool.js` | Smoke-test the pool with a fake launcher (no UE needed) |
| `templates/nginx.conf` | Reverse proxy + auth + WebSocket upgrades |
| `templates/coturn.conf` | TURN server for Pixel Streaming |
| `templates/htpasswd.example` | Basic Auth password file template |
| `templates/simworld.env` | Tunables for `/etc/default/simworld` |
| `systemd/simworld-web.service` | Manages the Node web server |
| `systemd/simworld-content-init.service` | One-shot Content download on first boot |
| `systemd/coturn.service.d-override.conf` | Override for distro's coturn unit |
| `docker/Dockerfile.web` | Optional: containerize web stack (UE stays on host) |
| `docker/docker-compose.yml` | Optional: compose for web + nginx + coturn |

## Deploy

You have three paths, in increasing order of "more containerized".

### Path 1 — Clone + one command (recommended)

The repo is **private**, so the bootstrap can't be `curl | bash`-ed
anonymously. The cleanest workflow:

```bash
# 1. Launch EC2 (g5.12xlarge or g6.12xlarge, 1 TB gp3, Ubuntu 22.04 LTS)
#    Security Group: 22 (your IP), 80/443 TCP, 3478 UDP/TCP,
#                    5349 TCP, 49160-49200 UDP

# 2. SSH in
ssh -i your-key.pem ubuntu@<EC2_IP>

# 3. Add a GitHub deploy key on the EC2 box (one-time)
ssh-keygen -t ed25519 -C "ec2-simworld" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
# Copy that public key → GitHub repo Settings → Deploy keys → Add (read-only OK)

# 4. Clone + bootstrap
sudo git clone -b aws git@github.com:SimWorld-AI/SimWorld-Studio-Internal.git /opt/simworld-studio
sudo /opt/simworld-studio/deploy/aws/scripts/bootstrap.sh

# 5. Stage UE engine + project (if the bootstrap couldn't auto-fetch them)
#    From your local dev box (NOT the EC2):
./deploy/aws/scripts/stage-project-to-aws.sh ubuntu@<EC2_IP>
sudo rsync -av /data/koe/Linux_Unreal_Engine_5.3.2/ ubuntu@<EC2_IP>:/opt/ue-engine/

# 6. Reboot if NVIDIA driver was installed (the bootstrap will tell you)
sudo reboot

# 7. Re-run bootstrap to finish (idempotent — skips done steps)
sudo /opt/simworld-studio/deploy/aws/scripts/bootstrap.sh

# 8. Claude OAuth, htpasswd, certbot, systemctl enable — follow bootstrap's
#    printed "Next" section.
```

> **Alternative**: if you don't want a deploy key, use HTTPS with a fine-grained
> Personal Access Token: `git clone -b aws https://<TOKEN>@github.com/SimWorld-AI/SimWorld-Studio-Internal.git /opt/simworld-studio`

### Path 2 — Manual step-by-step

If you want to control each step (or the bootstrap fails partway):

```bash
sudo git clone -b aws git@github.com:SimWorld-AI/SimWorld-Studio-Internal.git /opt/simworld-studio
sudo /opt/simworld-studio/deploy/aws/scripts/bake-ami.sh
sudo reboot                                                     # NVIDIA driver
# After reboot:
sudo rsync -a /src/Linux_Unreal_Engine_5.3.2/   /opt/ue-engine/
# Or from your dev box (302 MB, ~1 min on a fast link):
./deploy/aws/scripts/stage-project-to-aws.sh ubuntu@<EC2_IP>
sudo ln -s /opt/simworld-content /opt/simworld-project/Content
sudo systemctl start simworld-content-init                       # HF download
sudo -u simworld HOME=/var/lib/simworld/claude-home claude       # OAuth
sudo htpasswd -c /etc/nginx/htpasswd alice
sudo certbot --nginx -d simworld.your-lab.edu
sudo systemctl enable --now coturn simworld-web nginx
```

### Path 3 — Docker (optional)

The web stack (Node server + nginx + coturn) can be containerized. **UE
itself stays on the host** — see "Why not full Docker" below.

```bash
# Prereq: docker + docker compose + nvidia-container-toolkit
# (bake-ami.sh does NOT install these by default — add them if you go this route)

# After steps 1-2 of bootstrap (host OS deps + UE staged + Content downloaded):
cd /opt/simworld-studio
docker compose -f deploy/aws/docker/docker-compose.yml build
docker compose -f deploy/aws/docker/docker-compose.yml up -d
```

#### Why not full Docker (UE included)

We considered putting UE in containers too. Trade-off:

| Aspect | Host UE (current) | UE in container |
|---|---|---|
| Image size | small (~500 MB web image) | 58 GB+ |
| GPU access | direct | needs NVIDIA Container Toolkit |
| Pixel Streaming UDP | works out of box | needs `--net=host`, defeats isolation |
| Per-slot isolation | per-slot dirs | per-container, cleaner |
| Spawn latency | ~60s (UE startup) | ~60s + ~5s container start |
| Operations debug | journalctl + log files | docker logs, extra layer |

For a 5-person lab the marginal isolation gain doesn't justify the
operational complexity. If you need stronger isolation later, the path is
to make `slot-pool.js` exec `docker run` instead of bash — its interface
is already abstracted enough to swap.

Open https://simworld.your-lab.edu in a browser, log in, get a slot.

## Operations

```bash
# Watch slot allocation
curl -u admin:pwd http://localhost:3002/api/session/status | jq

# Tail one slot
journalctl -u simworld-web -f
ls /var/lib/simworld/slots/0/Saved/Logs/

# Force-kill a slot (e.g. UE wedged)
sudo /opt/simworld-studio/deploy/aws/scripts/slot-launcher.sh --stop --slot 0

# Cost-saver: stop EC2 at night
#   put on EventBridge cron 0 22 * * * → Lambda → ec2 stop-instances
```

## Decisions and trade-offs

- **Single instance, not ECS fleet.** At 2–3 concurrent users, the orchestration tax of ECS isn't worth it; one box with 4 GPUs is simpler. Revisit if usage grows past 4 concurrent.
- **OAuth shared across all sessions.** All slots share `/var/lib/simworld/claude-home/.claude/`. One Claude Code OAuth login = whole lab. Trade-off: no per-user cost attribution; add per-userId cost cap in the backend instead.
- **Per-slot UE, not shared.** Each slot is its own UE process with its own Saved/Intermediate. Avoids actor-name collisions and viewport conflicts. Content directory is symlinked (read-only) so no disk multiplication.
- **Cirrus per slot, not multiplexed.** Simpler than one Cirrus handling N streamers; 4 node processes have negligible memory cost.
- **TURN required.** Public users hit EC2 from arbitrary NATs; without TURN, WebRTC fails for ~30% of network conditions.

## What's intentionally NOT here

- ECS / Fargate / autoscaling — overkill for this scale
- Multi-region / multi-AZ — single dev box
- Per-user GitHub OAuth gating — Basic Auth covers 5 accounts; oauth2-proxy notes included for upgrade path
- CI/CD — push-to-deploy is a follow-up; ship manually first
