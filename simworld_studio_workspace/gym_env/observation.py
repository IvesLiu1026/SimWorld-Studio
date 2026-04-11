"""Builds the observation dict the LLM (and any future RL agent) sees.

Habitat-shape: ``rgb`` (uint8 H×W×3), ``gps`` (float32[2]),
``compass`` (float32[1]), and one of ``pointgoal_with_gps_compass``
(float32[2] = distance, angle) or ``objectgoal`` (int32[1]) depending
on task type.

This module is intentionally pure: it does not own state.  The env
constructs an :class:`ObservationBuilder` once and calls
:meth:`ObservationBuilder.observe` from ``reset`` / ``step``.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

from nav_task.episode import NavigationEpisode
from nav_task.task_spec import compute_pointgoal, compute_objectgoal_id

from .ucv_client import UCVClient

log = logging.getLogger(__name__)


@dataclass
class ObservationBuilder:
    """Pulls a single observation from the live UE scene.

    Parameters
    ----------
    ucv : UCVClient
        Shared UnrealCV connection (also used by the env for actions).
    agent_name : str
        UE actor name to query.
    camera_id : int
        UnrealCV camera index for RGB capture.
    image_size : tuple
        ``(height, width)`` of the captured RGB frame in pixels.
    """

    ucv: UCVClient
    agent_name: str
    camera_id: int = 0
    image_size: Tuple[int, int] = (240, 320)
    capture_rgb: bool = True

    # ------------------------------------------------------------------

    def observe(
        self,
        episode: NavigationEpisode,
        start_xy: Tuple[float, float],
    ) -> Dict[str, Any]:
        """Build a single Habitat-style observation dict.

        ``start_xy`` is the agent's spawn position; ``gps`` is reported
        as a relative displacement from this point (Habitat convention).
        """
        loc = self.ucv.vget_location(self.agent_name)
        rot = self.ucv.vget_rotation(self.agent_name)
        x, y, _z = loc
        _pitch, yaw_deg, _roll = rot
        yaw_rad = math.radians(yaw_deg)

        gps = np.array(
            [x - start_xy[0], y - start_xy[1]],
            dtype=np.float32,
        )
        compass = np.array([yaw_rad], dtype=np.float32)

        obs: Dict[str, Any] = {
            "gps": gps,
            "compass": compass,
            "agent_xy": np.array([x, y], dtype=np.float32),
            "agent_yaw_deg": float(yaw_deg),
        }

        if episode.task_type == "pointnav":
            d, ang = compute_pointgoal(x, y, yaw_deg, episode.goal_position)
            obs["pointgoal_with_gps_compass"] = np.array([d, ang], dtype=np.float32)
        elif episode.task_type == "objectnav":
            cat = episode.object_category or ""
            try:
                cid = compute_objectgoal_id(cat) if cat else -1
            except Exception:
                cid = -1
            obs["objectgoal"] = np.array([cid], dtype=np.int32)

        if self.capture_rgb:
            obs["rgb"] = self._capture_rgb()

        return obs

    # ------------------------------------------------------------------

    def _capture_rgb(self) -> np.ndarray:
        try:
            png = self.ucv.vget_camera_png(camera_id=self.camera_id, mode="lit")
            if not png:
                raise RuntimeError("empty PNG payload")
            from PIL import Image
            img = Image.open(io.BytesIO(png)).convert("RGB")
            target_h, target_w = self.image_size
            if img.size != (target_w, target_h):
                img = img.resize((target_w, target_h), Image.BILINEAR)
            return np.array(img, dtype=np.uint8)
        except Exception as exc:
            log.warning("RGB capture failed: %s — returning zeros", exc)
            h, w = self.image_size
            return np.zeros((h, w, 3), dtype=np.uint8)
