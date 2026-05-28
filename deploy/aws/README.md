# SimWorld Studio — AWS Deployment

Lab-scale multi-tenant deploy (≤5 users, 2–3 concurrent) on a single GPU EC2 instance.

## Architecture

```
        ┌──────────────────────────────────────────────┐
HTTPS   │  Nginx + Let's Encrypt + Basic Auth          │  :443
   │    │  /                → :3002 (web)              │
   │    │  /cirrus/{slot}/* → :85xx (Cirrus per slot)  │
   ▼    └──────────┬───────────────────────────────────┘
┌─────────────────┴──────────────────────────────────────┐
│  EC2 g5.12xlarge (4×A10G) or g6.12xlarge (4×L4)         │
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
│       slot 0   │   slot 1       slot 2      slot 3      │
│       ▼        ▼   ▼            ▼           ▼           │
│   ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐         │
│   │ UE     │ │ UE     │ │ UE     │ │ UE     │         │
│   │ GPU 0  │ │ GPU 1  │ │ GPU 2  │ │ GPU 3  │         │
│   │ MCP    │ │ MCP    │ │ MCP    │ │ MCP    │         │
│   │ 55559  │ │ 55561  │ │ 55563  │ │ 55565  │         │
│   │ Cirrus │ │ Cirrus │ │ Cirrus │ │ Cirrus │         │
│   │ 8585   │ │ 8587   │ │ 8589   │ │ 8591   │         │
│   │ UCV    │ │ UCV    │ │ UCV    │ │ UCV    │         │
│   │ 9017   │ │ 9018   │ │ 9019   │ │ 9020   │         │
│   └────────┘ └────────┘ └────────┘ └────────┘         │
│   /var/lib/simworld/slots/{0,1,2,3}/  (per-slot Saved) │
│                                                         │
│   Shared read-only:                                     │
│   /opt/ue-engine/      (58 GB UE 5.3.2)                │
│   /opt/simworld-project/  (uproject + Plugins)         │
│   /opt/simworld-content/  (from HuggingFace)           │
│                                                         │
│   coturn :3478 (UDP) — WebRTC NAT traversal             │
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

All these listen on `127.0.0.1` only; public access goes through Nginx.

## Files

| Path | Purpose |
|---|---|
| `scripts/slot-launcher.sh` | Launch one UE instance for a given slot |
| `scripts/slot-pool.js` | Node module: child-process lifecycle for N slots |
| `scripts/session-shim.js` | Wires SlotPool into session-manager via env var |
| `scripts/per-session-ports.js` | Helper for index.js to route by `x-session-token` |
| `scripts/bake-ami.sh` | Provision a fresh Ubuntu 22.04 box → ready to launch |
| `scripts/download-content.sh` | Pull Content from HuggingFace once |
| `scripts/init-slot-dirs.sh` | Create `/var/lib/simworld/slots/N` with symlinks |
| `templates/nginx.conf` | Reverse proxy + auth + WebSocket upgrades |
| `templates/coturn.conf` | TURN server for Pixel Streaming |
| `templates/htpasswd.example` | Basic Auth password file template |
| `systemd/simworld-web.service` | Manages the Node web server |
| `systemd/simworld-content-init.service` | One-shot Content download on first boot |
| `systemd/coturn.service` | TURN server unit (overrides distro default) |

## Deploy in 10 steps

```bash
# 1. Launch EC2 (g5.12xlarge or g6.12xlarge, 1 TB gp3, Ubuntu 22.04 LTS)
#    Security Group: 22 (your IP), 80, 443, 3478/udp, 49152-65535/udp (TURN relay)

# 2. SSH in
ssh -i your-key.pem ubuntu@<EC2_IP>

# 3. Clone repo to /opt/simworld-studio (aws branch)
sudo git clone -b aws https://github.com/SimWorld-AI/SimWorld-Studio.git /opt/simworld-studio

# 4. Run bake script (installs deps, NVIDIA driver, Claude Code, etc.)
sudo /opt/simworld-studio/deploy/aws/scripts/bake-ami.sh
sudo reboot  # for NVIDIA driver

# 5. After reboot, download Content from HF
sudo /opt/simworld-studio/deploy/aws/scripts/download-content.sh

# 6. Place UE 5.3.2 binary at /opt/ue-engine/
#    (Linux_Unreal_Engine_5.3.2 from your existing source)
sudo rsync -a /your/source/Linux_Unreal_Engine_5.3.2/ /opt/ue-engine/

# 7. Place project (uproject + Plugins + Config, NOT Content)
sudo rsync -a --exclude Content /your/source/SimWorld/ /opt/simworld-project/
sudo ln -s /opt/simworld-content /opt/simworld-project/Content

# 8. First-time Claude OAuth (run as the simworld user)
sudo -u simworld HOME=/var/lib/simworld/claude-home claude
# Follow device-code flow in browser

# 9. Configure auth — pick ONE
#    a) Basic Auth (5 fixed accounts):
sudo cp /opt/simworld-studio/deploy/aws/templates/htpasswd.example /etc/nginx/htpasswd
sudo htpasswd /etc/nginx/htpasswd alice
#    b) oauth2-proxy in front of nginx (GitHub org gate) — see docs/oauth2-proxy.md

# 10. Domain + TLS
sudo certbot --nginx -d simworld.your-lab.edu

# 11. Enable services
sudo systemctl enable --now coturn simworld-web
```

Open https://simworld.your-lab.edu in browser, log in, you get assigned a slot.

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
