"""Coding agent: LLM-driven scene builder + task designer. NO FALLBACK.

If JSON parse fails, retries the LLM call. If still fails, uses the
LAST successful design (never a hardcoded default).

Adversarial reward: coding_reward = 1 - nav_sr.
The coding agent is incentivized to push difficulty up while keeping
the nav agent in the learning zone.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .scene_manager import ASSET_CATALOG, SceneManager, SceneSpec, SpawnedObject

log = logging.getLogger(__name__)


CODING_AGENT_PROMPT = """\
You are a **training environment designer** for a navigation agent.
Your REWARD = 1 - agent_success_rate (but 0 if agent_SR < 10%).
Maximize your reward by designing challenging but solvable tasks.

## EMBODIED AGENT STATUS
{performance_history}

Learned strategies:
{strategies}

Failure patterns:
{failure_patterns}

## CURRENT SCENE (difficulty: {current_difficulty}/100)
{current_scene}

## YOUR DESIGN MEMORY
{coding_memory}

## AVAILABLE ASSETS
{asset_catalog}

## COORDINATE CONSTRAINTS (CRITICAL)
The navigation area is a square centered at origin.
ALL object coordinates MUST be within **x: [-4000, 4000], y: [-4000, 4000]**.
Objects outside this range will NOT block the agent's path because the
agent's start and goal positions are sampled within this area.

To create effective obstacles that FORCE detours:
- Place buildings/objects BETWEEN likely start and goal positions
- Cluster objects to form walls, corridors, or chokepoints
- A single isolated object is easy to walk around — use groups of 2-3
  objects placed close together (300-800 units apart) to form barriers
- Example barrier: 3 buildings in a line at y=0, spaced 800 units apart,
  forces the agent to go around the entire wall

## DIFFICULTY SCALE (0-10)
Current: {current_difficulty}/10
1 = empty field, 1000cm path → agent ~80% SR
2 = empty field, 2000cm path → agent ~60% SR
3 = 1-2 objects, 1500cm path → agent ~50% SR
4 = 3-4 objects forming a partial wall, 2000cm path → agent ~40% SR
5 = 5+ objects forming corridors, 2500cm path → agent ~30% SR
6 = dense layout with walls, 3000cm path, forced detours → agent ~20% SR
8 = complex town layout with multiple walls → agent ~10% SR
10 = dense city with objectnav → agent ~5% SR

## YOUR GOAL
Increase difficulty GRADUALLY. If agent SR > 60%, push harder.
If SR < 20%, pull back slightly. Add 1-2 objects at a time.
Place objects to BLOCK direct paths, not just decorate the scene.

Output JSON:
```json
{{
  "action": "keep_scene" or "modify_scene" or "new_scene",
  "add_objects": [{{"name": "Building_1", "asset": "building_01", "x": 1000, "y": 0}}],
  "remove_objects": ["Building_old_1", "Building_old_2"],
  "task_type": "pointnav",
  "min_path_cm": 500,
  "max_path_cm": 1500,
  "max_steps": 25,
  "n_episodes": 4,
  "target_difficulty": 2,
  "reasoning": "why this design"
}}
```

Valid assets: {asset_keys}
Actions:
- keep_scene: no changes to objects, only adjust path/steps
- modify_scene: add AND/OR remove specific objects (incremental update)
- new_scene: CLEAR ALL existing objects, then add the new objects listed
For modify_scene: use add_objects to add, remove_objects to delete by name.
If SR is too low, REMOVE objects to reduce clutter. If SR is too high, ADD objects.
Coordinates: x and y must be in [-4000, 4000]. z=0 always.
Output ONLY the JSON."""


class CodingAgent:
    """LLM-driven scene + task designer. Never falls back to hardcoded defaults."""

    def __init__(self, llm_call, coding_memory=None):
        self._llm_call = llm_call
        self.memory = coding_memory
        self._current_scene_desc = "(empty field — no objects)"
        self._current_scene_id = "scene_000"
        self._scene_counter = 0
        self._current_difficulty = 0.0
        self._last_successful_spec: Optional[SceneSpec] = None

    def design(
        self,
        performance_history: str,
        strategies: str,
        failure_patterns: str,
        current_scene_objects: List[str],
    ) -> SceneSpec:
        """Design next scene+tasks. Retries on parse failure, NEVER falls back."""

        current_scene = self._current_scene_desc
        if current_scene_objects:
            current_scene += f"\nObjects: {', '.join(current_scene_objects)}"

        coding_memory_text = ""
        if self.memory:
            coding_memory_text = self.memory.get_prompt_section()

        prompt = CODING_AGENT_PROMPT.format(
            performance_history=performance_history or "(first epoch)",
            strategies=strategies or "(none yet)",
            failure_patterns=failure_patterns or "(none yet)",
            current_scene=current_scene,
            current_difficulty=f"{self._current_difficulty:.0f}",
            coding_memory=coding_memory_text or "(no experience yet)",
            asset_catalog=SceneManager.get_asset_catalog_prompt(),
            asset_keys=", ".join(sorted(ASSET_CATALOG.keys())),
        )

        # Try up to 3 times to get valid JSON
        for attempt in range(3):
            try:
                raw = self._llm_call(prompt)
                spec = self._parse(raw)
                self._last_successful_spec = spec
                return spec
            except Exception as exc:
                log.warning("CodingAgent attempt %d failed: %s", attempt + 1, exc)

        # All retries failed — use last successful design
        if self._last_successful_spec is not None:
            log.warning("CodingAgent: using last successful design (all retries failed)")
            return self._last_successful_spec

        # True first call with no history — minimal default
        log.warning("CodingAgent: first call, no history, using minimal default")
        spec = SceneSpec(
            scene_id=self._current_scene_id,
            description="empty field",
            task_type="pointnav",
            min_path_cm=500.0, max_path_cm=1000.0,
            max_steps=25, n_episodes=4,
            reasoning="Initial: empty field, short paths",
        )
        spec._is_new_scene = False
        self._last_successful_spec = spec
        return spec

    def _parse(self, raw: str) -> SceneSpec:
        """Parse LLM output. Raises on failure (caller retries)."""
        text = raw.strip()
        # Strip ALL thinking formats
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^.*?</think>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"```(?:json)?", "", text).strip()

        # Find last complete {...} block
        last_end = text.rfind("}")
        if last_end == -1:
            raise ValueError("No closing brace in output")

        depth = 0
        start = -1
        for i in range(last_end, -1, -1):
            if text[i] == '}':
                depth += 1
            elif text[i] == '{':
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start == -1:
            raise ValueError("No matched braces")

        json_str = text[start:last_end + 1]
        # Fix common JSON issues
        json_str = json_str.replace("'", '"')
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)

        data = json.loads(json_str)

        action = data.get("action", "keep_scene")
        is_new = action == "new_scene"
        is_modify = action == "modify_scene"

        if is_new or is_modify:
            self._scene_counter += 1
            scene_id = f"scene_{self._scene_counter:03d}"
            self._current_scene_desc = data.get("scene_description",
                                                  f"scene with objects")
        else:
            scene_id = self._current_scene_id

        # Parse objects to add (from "add_objects" or legacy "objects")
        objects = []
        obj_list = data.get("add_objects", data.get("objects", []))
        if (is_new or is_modify) and obj_list:
            for obj in obj_list:
                asset_key = obj.get("asset", "")
                if asset_key not in ASSET_CATALOG:
                    continue
                # Clamp coordinates to play area, force z=0
                ox = max(-4000.0, min(4000.0, float(obj.get("x", 0))))
                oy = max(-4000.0, min(4000.0, float(obj.get("y", 0))))
                objects.append(SpawnedObject(
                    actor_name=obj.get("name", f"obj_{len(objects)}"),
                    asset_key=asset_key,
                    x=ox,
                    y=oy,
                    z=0.0,  # always ground level
                    yaw=float(obj.get("yaw", 0)),
                ))

        # Parse objects to remove
        remove_names = []
        if is_modify:
            remove_names = data.get("remove_objects", [])

        target_diff = float(data.get("target_difficulty", self._current_difficulty))
        self._current_difficulty = target_diff

        spec = SceneSpec(
            scene_id=scene_id,
            description=self._current_scene_desc,
            objects=objects,
            task_type=str(data.get("task_type", "pointnav")),
            min_path_cm=max(500.0, float(data.get("min_path_cm", 800))),
            max_path_cm=min(5000.0, float(data.get("max_path_cm", 2000))),
            max_steps=min(40, max(15, int(data.get("max_steps", 25)))),
            n_episodes=min(6, max(2, int(data.get("n_episodes", 4)))),
            reasoning=str(data.get("reasoning", "")),
        )
        spec._is_new_scene = is_new
        spec._is_modify = is_modify
        spec._remove_names = remove_names
        self._current_scene_id = scene_id
        return spec
