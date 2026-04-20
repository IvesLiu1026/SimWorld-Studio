"""Configuration for the co-evolution loop.

All paths and URLs can be overridden via CLI args or environment variables.
No hardcoded machine-specific paths.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class CoEvolveConfig:
    # ── Mode ──
    mode: str = "live"                 # "live" (UE) or "sim" (2D simulation)

    # ── Loop ──
    generations: int = 30
    episodes_per_gen: int = 4          # default; coding agent can override
    max_steps: int = 40                # hard cap

    # ── Nav agent ──
    nav_model: str = "qwen"
    nav_model_id: str = os.environ.get("NAV_MODEL_ID", "Qwen3.5-9B")
    nav_base_url: str = os.environ.get("NAV_BASE_URL", "http://localhost:8002/v1")
    nav_api_key: str = os.environ.get("NAV_API_KEY", "EMPTY")
    nav_memory: str = "strategy"
    vision_depth: int = 3
    capture_rgb: bool = False

    # ── Coding agent LLM ──
    coding_model_id: str = os.environ.get("CODING_MODEL_ID", "Qwen3.5-9B")
    coding_base_url: str = os.environ.get("CODING_BASE_URL", "http://localhost:8002/v1")
    coding_api_key: str = os.environ.get("CODING_API_KEY", "EMPTY")

    # ── UE connection ──
    ucv_host: str = "127.0.0.1"
    ucv_port: int = int(os.environ.get("UCV_PORT", "9001"))
    mcp_host: str = "127.0.0.1"
    mcp_port: int = int(os.environ.get("MCP_PORT", "55557"))

    # ── Output ──
    output_dir: str = "runs/co_evolve"
    seed: int = 42
