"""Licensed HSSD visual bindings for the VISTA Playable Home.

The package is deliberately separate from :mod:`vista_playable_home`: the
procedural forge remains the collision/gameplay fallback and this pass only
produces visual replacement GLBs plus a closed attribution receipt.
"""

from .planner import (
    BINDING_PLAN_SCHEMA,
    BUILT_MANIFEST_SCHEMA,
    HssdBindingError,
    build_binding_plan,
    canonical_json_bytes,
    derive_target_assets,
    inspect_glb,
    validate_built_manifest,
    validate_binding_plan,
)

__all__ = [
    "BINDING_PLAN_SCHEMA",
    "BUILT_MANIFEST_SCHEMA",
    "HssdBindingError",
    "build_binding_plan",
    "canonical_json_bytes",
    "derive_target_assets",
    "inspect_glb",
    "validate_built_manifest",
    "validate_binding_plan",
]
