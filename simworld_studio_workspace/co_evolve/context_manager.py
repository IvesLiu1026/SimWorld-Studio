"""Global context manager for co-evolution.

Manages the information flow between coding agent and embodied agent:
  - Coding agent sees: nav agent's L2/L3 memory + performance history
  - Nav agent sees: only its own memory (L1/L2/L3)
  - Each agent's memory is independent but the context manager
    provides a unified view for the coding agent's prompt.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class CoEvolveContextManager:
    """Aggregates context from both agents for prompt construction."""

    def __init__(self):
        self.gen_results: List[Dict[str, Any]] = []
        self._all_episode_results: List[Dict[str, Any]] = []

    def add_generation(self, gen_record: Dict[str, Any]):
        self.gen_results.append(gen_record)
        self._all_episode_results.extend(gen_record.get("episode_results", []))

    def get_nav_context_for_coding_agent(self, nav_memory) -> Dict[str, str]:
        """Extract nav agent's memory state for the coding agent to see.

        Returns dict with keys: strategies, l3_skills, performance_summary
        """
        strategies = []
        l3_section = ""

        if hasattr(nav_memory, 'query'):
            strategies = nav_memory.query("", k=10)

        if hasattr(nav_memory, 'get_system_prompt_section'):
            l3_section = nav_memory.get_system_prompt_section()

        # Build performance summary
        perf_lines = []
        for r in self.gen_results[-8:]:
            sr = r.get("sr", 0)
            spl = r.get("spl", 0)
            diff = r.get("difficulty_score", 0)
            scene = r.get("scene_id", "?")
            perf_lines.append(
                f"Epoch {r.get('generation','?')}: SR={sr:.0%} SPL={spl:.3f} "
                f"diff={diff:.0f} scene={scene} "
                f"path=[{r.get('min_path_cm',0):.0f},{r.get('max_path_cm',0):.0f}]"
            )

        # Failure analysis
        recent_eps = self._all_episode_results[-12:]
        failures = [e for e in recent_eps if e.get("SR", 0) == 0]
        successes = [e for e in recent_eps if e.get("SR", 0) > 0]
        fail_summary = f"{len(failures)}/{len(recent_eps)} recent episodes failed"
        if failures:
            avg_steps = sum(e.get("steps", 0) for e in failures) / len(failures)
            fail_summary += f" (avg {avg_steps:.0f} steps before failure)"

        return {
            "strategies": "\n".join(f"  {i+1}. {s}" for i, s in enumerate(strategies)) if strategies else "(none)",
            "l3_skills": l3_section or "(none)",
            "performance_history": "\n".join(perf_lines) if perf_lines else "(no data)",
            "failure_summary": fail_summary,
        }

    def get_recent_episodes(self, n: int = 8) -> List[Dict[str, Any]]:
        return self._all_episode_results[-n:]
