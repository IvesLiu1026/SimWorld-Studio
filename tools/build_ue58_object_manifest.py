#!/usr/bin/env python3
"""Compatibility entry point for the generic UE AssetRegistry bootstrap.

The former implementation embedded workstation-specific UE 5.8 ports and
developer-local output paths.  Production operators must now provide an
explicit source audit or live bridge endpoint, immutable revision metadata,
and output directory.  See ``build_ue_asset_registry_bootstrap.py --help``.
"""

from __future__ import annotations

import sys

from build_ue_asset_registry_bootstrap import BootstrapError, main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
