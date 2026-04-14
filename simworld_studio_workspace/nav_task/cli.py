"""Command-line interface for the navigation task generator.

Usage
-----
PointNav (default):
    python -m nav_task --map roads.json --seed 42 --n-episodes 10 --output episodes.json

ObjectNav:
    python -m nav_task --map roads.json --task objectnav --category TRASH \\
        --elements elements.json --seed 42 --n-episodes 5 --output objnav.json

Output format: n == 1 → single JSON object; n > 1 → JSON array.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .interface import UnrealCVNavigationInterface
from .generator import NavigationTaskGenerator


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m nav_task",
        description="Generate navigation task episodes from a SimWorld roads.json map.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--map", required=True, metavar="PATH",
                   help="Path to roads.json")
    p.add_argument("--seed", type=int, default=42,
                   help="Master RNG seed")
    p.add_argument("--n-episodes", type=int, default=1, dest="n_episodes",
                   help="Number of episodes to generate")
    p.add_argument("--output", metavar="PATH", default="-",
                   help="Output file path (- = stdout)")
    p.add_argument("--min-path-length", type=float, default=1000.0,
                   dest="min_path_length",
                   help="Minimum path length in cm")
    p.add_argument("--max-retries", type=int, default=50, dest="max_retries",
                   help="Max resampling attempts per episode")
    p.add_argument("--sidewalk-offset", type=float, default=500.0,
                   dest="sidewalk_offset",
                   help="Sidewalk-offset in cm passed to Map")
    # ObjectNav flags
    p.add_argument("--task", choices=["pointnav", "objectnav"], default="pointnav",
                   help="Task type")
    p.add_argument("--category", metavar="NAME", default=None,
                   help="Object category for ObjectNav (e.g. TRASH, VEGETATION)")
    p.add_argument("--elements", metavar="PATH", default=None,
                   help="Path to elements.json (required for ObjectNav)")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.task == "objectnav":
        if args.category is None:
            build_parser().error("--category is required for --task objectnav")
        if args.elements is None:
            build_parser().error("--elements is required for --task objectnav")

    roads_file = str(Path(args.map).resolve())
    elements_file = str(Path(args.elements).resolve()) if args.elements else None

    interface = UnrealCVNavigationInterface(
        roads_file=roads_file,
        sidewalk_offset=args.sidewalk_offset,
        elements_file=elements_file,
    )
    generator = NavigationTaskGenerator(
        interface=interface,
        roads_file=roads_file,
        min_path_length_cm=args.min_path_length,
        max_retries=args.max_retries,
    )

    if args.task == "objectnav":
        episodes = generator.generate_objectnav(
            seed=args.seed,
            object_category=args.category,
            n_episodes=args.n_episodes,
        )
    else:
        episodes = generator.generate(seed=args.seed, n_episodes=args.n_episodes)

    payload = (
        episodes[0].to_dict()
        if args.n_episodes == 1
        else [ep.to_dict() for ep in episodes]
    )
    json_str = json.dumps(payload, indent=2)

    if args.output == "-":
        print(json_str)
    else:
        out = Path(args.output)
        out.write_text(json_str)
        print(
            f"Wrote {args.n_episodes} episode(s) to {out}",
            file=sys.stderr,
        )
