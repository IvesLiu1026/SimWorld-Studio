# gym_env — SimWorld Nav Experiment Harness

A Gym-style Python wrapper around the SimWorld UE simulator for
running reproducible LLM navigation experiments. Built to bypass the
JS server / agent panel — talks UE directly via UnrealCV.

## Setup

### Linux (recommended for headless experiments)

```bash
cd simworld_studio_workspace/gym_env

# 1. One-time environment setup (creates venv, installs deps + task_gen)
#    Set TASK_GEN_DIR if your task_gen repo is not at ../../task_gen
source scripts/setup_env.sh

# 2. Set whichever model API key you plan to use
export ANTHROPIC_API_KEY=...    # --model claude
export OPENAI_API_KEY=...       # --model gpt
export GEMINI_API_KEY=...       # --model gemini
export DASHSCOPE_API_KEY=...    # --model qwen
```

### Windows

```powershell
# 1. Install the forked task_gen as an editable dependency
pip install -e ..\task_gen          # adjust path to your local task_gen clone

# 2. Install gym_env requirements
pip install -r simworld_studio_workspace\gym_env\requirements.txt

# 3. Set API keys (same as above, use $env:VAR = "..." in PowerShell)
```

### Common: API keys

| Model flag     | Env var              | Notes                                  |
|----------------|----------------------|----------------------------------------|
| `--model claude` | `ANTHROPIC_API_KEY`  | Anthropic SDK, supports vision         |
| `--model claude-sdk` | _(none)_        | Routes through local Claude Code CLI   |
| `--model gpt`  | `OPENAI_API_KEY`     | OpenAI SDK                             |
| `--model gemini`| `GEMINI_API_KEY`    | Via OpenAI-compat endpoint             |
| `--model qwen` | `DASHSCOPE_API_KEY`  | Via OpenAI-compat endpoint             |

## Pre-flight (UE side)

**Headless experiment mode** — do NOT run the Studio JS server
(`node server/index.js` / `SimWorld-Studio.bat`).

The minimum to launch:

1. Open the SimWorld UE editor with the project loaded.
2. Make sure UnrealCV is reachable on `127.0.0.1:9000`
   and UE editor MCP TCP server on `127.0.0.1:55557`.
3. Spawn the static scene you want. Leave the editor in **edit
   mode** — the runner will request PIE itself via MCP on first
   `env.reset()`.

Pass `--no-start-pie` if PIE is already running.

---

## Running Experiments

### Quick smoke test (no LLM needed)

```bash
# Linux
bash scripts/run_smoke_test.sh
bash scripts/run_smoke_test.sh --no-rgb --steps 10

# Windows / manual
cd simworld_studio_workspace
python -m gym_env.smoke_test --steps 8
```

### Single episode

```bash
# Linux
bash scripts/run_experiment.sh --model claude --task pointnav --target-distance 2000 --max-steps 30

# Windows / manual
cd simworld_studio_workspace
set PYTHONPATH=C:\path\to\task_gen;%CD%
python -m gym_env.runner --model claude --task pointnav --target-distance 2000 --max-steps 30
```

### Multi-episode (with memory)

Run N episodes back-to-back. The agent accumulates experience across episodes.

```bash
# 10 episodes, text memory enabled
bash scripts/run_experiment.sh \
    --model qwen \
    --n-episodes 10 \
    --memory text \
    --max-steps 40 \
    --target-distance 2000

# Same but without memory (baseline)
bash scripts/run_experiment.sh \
    --model qwen \
    --n-episodes 10 \
    --memory none \
    --max-steps 40
```

### Batch (multiple UE instances in parallel)

Start multiple UE instances on different ports, then:

```bash
# Linux
bash scripts/run_batch.sh \
    --models claude,qwen \
    --ucv-ports 9000,9001 \
    --parallel 2 \
    --max-steps 30

# Windows / manual
python -m gym_env.batch --models claude,gpt --ucv-ports 9000,9001 --parallel 2
```

### Custom LLM endpoint (e.g. local vLLM)

```bash
bash scripts/run_experiment.sh \
    --model qwen \
    --model-id "Qwen/Qwen3-VL-30B-A3B-Instruct" \
    --base-url "http://your-gpu-server:8000/v1" \
    --api-key "token-abc123" \
    --n-episodes 5
```

---

## Key CLI Flags

| Flag                | Default   | Description                                      |
|---------------------|-----------|--------------------------------------------------|
| `--model`           | `claude`  | LLM: claude / claude-sdk / gpt / gemini / qwen   |
| `--model-id`        | _(auto)_  | Override model ID for the endpoint                |
| `--base-url`        | _(auto)_  | Override LLM API base URL                         |
| `--n-episodes`      | `1`       | Episodes to run back-to-back                      |
| `--memory`          | `none`    | Memory backend: none / text / mem0                |
| `--task`            | `pointnav`| Task type: pointnav / objectnav                   |
| `--target-distance` | `2000`    | PointNav target distance in cm                    |
| `--max-steps`       | `40`      | Max steps per episode                             |
| `--vision-depth`    | `3`       | Recent frames kept in LLM context                 |
| `--record-trajectory` | off     | Save PNG frames for every step                    |
| `--no-rgb`          | off       | Text-only ablation (skip images)                  |
| `--seed`            | `42`      | Random seed (increments per episode)              |
| `--no-start-pie`    | off       | Skip auto PIE start via MCP                       |

---

## Output Structure

Each run produces a timestamped directory under `runs/`:

```
runs/<timestamp>_<run_name>/
  meta.json           # config snapshot, model name, episode ID, git SHA
  episode.jsonl       # one JSON line per env step (action, reward, obs, distance)
  llm_raw.jsonl       # full vendor API responses (reasoning, token usage)
  summary.json        # final metrics: SR, SPL, SoftSPL, cumulative reward
  run.log             # Python logging output
  frames/             # (if --record-trajectory) PNG screenshot per step
```

## Analyzing Results

```bash
# Print step-by-step report for all runs
bash scripts/analyze.sh

# Filter to specific runs
bash scripts/analyze.sh runs/20260410_*claude*

# Plot learning curves (memory vs no-memory comparison)
bash scripts/analyze.sh --plot
```

---

## Reproducing Published Experiments

### exp01 — Claude SDK baseline
```bash
bash scripts/run_experiment.sh --model claude-sdk --no-rgb --max-steps 30
```

### exp02 — Qwen no-memory (5 episodes)
```bash
bash scripts/run_experiment.sh \
    --model qwen --n-episodes 5 --memory none --max-steps 40
```

### exp04 — PointNav 20v20 (memory vs no-memory)
```bash
# Without memory
bash scripts/run_experiment.sh \
    --model qwen --n-episodes 20 --memory none \
    --max-steps 40 --run-name qwen_batch_no_mem

# With memory
bash scripts/run_experiment.sh \
    --model qwen --n-episodes 20 --memory text \
    --max-steps 40 --run-name qwen_with_memory
```

### exp05 — PointNav 30-episode RGB
```bash
# Without memory
bash scripts/run_experiment.sh \
    --model qwen --n-episodes 30 --memory none \
    --max-steps 40 --record-trajectory --run-name 30ep_no_mem

# With memory
bash scripts/run_experiment.sh \
    --model qwen --n-episodes 30 --memory text \
    --max-steps 40 --record-trajectory --run-name 30ep_with_mem
```

---

## Architecture

```
              +--------------------------------------+
              | gym_env (this package)               |
              |                                      |
   episode -->| episode_builder --> NavigationEpisode |
              |                                      |
              | SimWorldNavEnv <---- runner ---- LLMClient
              |      |                               |
              |      v                               |
              | UCVClient <---- observation           |
              |      |           builder             |
              +------|----- -------------------------+
                     |
                     v
              UnrealCV TCP :9000  (PIE)
                     |
                     v
                Unreal Engine
```

Reward / SR / SPL / SoftSPL come from `nav_task` (forked) backed by
`EuclideanNavigationInterface` — geodesic distance falls back to
straight-line because this repo's scenes have no road graph.
