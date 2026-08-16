"""Deterministic Blender forge for the VISTA Playable Home r2 presentation.

The package keeps its planning modules importable without Blender.  Only
``build`` and the material realization helpers import :mod:`bpy` at runtime.
"""

from .architecture import ExternalForgePlan, ForgePlan, build_external_forge_plan, build_forge_plan
from .config import EXPECTED_BLENDER_VERSION, FORGE_SCHEMA_VERSION, ForgeInputError

__all__ = [
    "EXPECTED_BLENDER_VERSION",
    "FORGE_SCHEMA_VERSION",
    "ForgeInputError",
    "ForgePlan",
    "ExternalForgePlan",
    "build_forge_plan",
    "build_external_forge_plan",
]
