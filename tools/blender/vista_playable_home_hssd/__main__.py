"""Command-line entry point for the pure-Python HSSD binding planner."""

from .planner import plan_main


if __name__ == "__main__":
    raise SystemExit(plan_main())
