"""CLI entry point: python -m co_evolve"""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m co_evolve",
        description="Co-evolution: coding agent builds scenes + tasks, embodied agent executes.",
    )
    p.add_argument("--mode", choices=["live", "sim"], default="live")
    p.add_argument("--generations", type=int, default=30)
    p.add_argument("--episodes-per-gen", type=int, default=4)
    p.add_argument("--max-steps", type=int, default=40)

    p.add_argument("--nav-model", default="qwen")
    p.add_argument("--nav-model-id", default=None, help="Nav LLM model ID (default: from env NAV_MODEL_ID or Qwen3.5-9B)")
    p.add_argument("--nav-base-url", default=None, help="Nav LLM API URL (default: from env NAV_BASE_URL)")
    p.add_argument("--nav-api-key", default=None)
    p.add_argument("--nav-memory", default="strategy")
    p.add_argument("--no-rgb", action="store_true", default=False,
                   help="Disable RGB capture (default: RGB on)")

    p.add_argument("--coding-model-id", default=None, help="Coding LLM model ID (default: from env CODING_MODEL_ID)")
    p.add_argument("--coding-base-url", default=None, help="Coding LLM API URL (default: from env CODING_BASE_URL)")
    p.add_argument("--coding-api-key", default=None)

    p.add_argument("--ucv-port", type=int, default=9001)
    p.add_argument("--mcp-port", type=int, default=55557)

    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output-dir", default="runs/co_evolve")
    p.add_argument("--resume", default=None,
                   help="Path to experiment dir to resume from (e.g. runs/co_evolve/coevolve_XXX)")
    p.add_argument("--log-level", default="INFO")

    args = p.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    from .config import CoEvolveConfig
    from .loop import CoEvolutionRunner

    # Start with defaults (from env vars), override with CLI args if provided
    config = CoEvolveConfig(
        mode=args.mode,
        generations=args.generations,
        episodes_per_gen=args.episodes_per_gen,
        max_steps=args.max_steps,
        nav_model=args.nav_model,
        nav_memory=args.nav_memory,
        capture_rgb=not args.no_rgb,
        ucv_port=args.ucv_port,
        mcp_port=args.mcp_port,
        seed=args.seed,
        output_dir=args.output_dir,
    )
    # Only override LLM settings if explicitly provided via CLI
    if args.nav_model_id is not None:
        config.nav_model_id = args.nav_model_id
    if args.nav_base_url is not None:
        config.nav_base_url = args.nav_base_url
    if args.nav_api_key is not None:
        config.nav_api_key = args.nav_api_key
    if args.coding_model_id is not None:
        config.coding_model_id = args.coding_model_id
    if args.coding_base_url is not None:
        config.coding_base_url = args.coding_base_url
    if args.coding_api_key is not None:
        config.coding_api_key = args.coding_api_key

    runner = CoEvolutionRunner(config, resume_dir=args.resume)
    results = runner.run()

    print("\n" + "=" * 70)
    print("CO-EVOLUTION COMPLETE")
    print("=" * 70)
    for r in results:
        print(
            f"  Gen {r['generation']:>2}: SR={r['sr']:.0%} SPL={r['spl']:.3f} "
            f"scene={r.get('scene_id','?')} task={r.get('task_type','?')} "
            f"path=[{r['min_path_cm']:.0f},{r['max_path_cm']:.0f}]cm"
        )
    print("=" * 70)

    try:
        from .visualize import plot_coevolution, plot_sr_vs_difficulty
        plot_coevolution(results, runner.output_dir)
        plot_sr_vs_difficulty(results, runner.output_dir)
    except Exception as exc:
        print(f"Visualization failed: {exc}")


if __name__ == "__main__":
    main()
