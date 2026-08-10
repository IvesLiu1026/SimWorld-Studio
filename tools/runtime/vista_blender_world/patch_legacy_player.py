#!/usr/bin/env python3
"""Patch only the reviewed disposable Studio player used by the loopback demo.

The packaged UE 5.3.2 build cannot expose the newer begin-PIE backend command,
so this compatibility lane sends one fixed toolbar click through Pixel
Streaming. It accepts one exact legacy revision and produces one exact
reviewed revision; arbitrary HTML is never rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_blender_world.runtime import (  # type: ignore
        RuntimeSafetyError,
        atomic_write_bytes,
    )
else:
    from .runtime import RuntimeSafetyError, atomic_write_bytes


INPUT_SHA256 = "287ea008219916c80f61223051e456b6c87cdb0b1f73f6802461a479b8909dd8"
OUTPUT_SHA256 = "5d2eab57ef9e91fd352cb53cd85882d58ec2aaef31baedbfc314bbb5b10a5a17"
PLAYER_PATHS = (Path("web/public/ue-player.html"), Path("web/dist/ue-player.html"))

OLD_BLOCK = b"""  var sourceWidth = Number(video.videoWidth) || target.clientWidth;
  var sourceHeight = Number(video.videoHeight) || target.clientHeight;
  // UE 5.3 right-aligns the Play group. The reviewed live click is 180
  // intrinsic pixels from the right edge and 78 pixels from the top
  // (486,78 at 666x728; protocol coordinate 47823,7021).
  var point = swPixelStreamingClientPoint(
    video,
    target,
    Math.max(0, sourceWidth - 180),
    Math.min(78, Math.max(0, sourceHeight - 1))
  );
"""

NEW_BLOCK = b"""  // Pixel Streaming consumes coordinates in rendered target CSS pixels. The
  // reviewed UE 5.3 layout keeps Play at local (486, 78) in this fixed view.
  var point = {
    x: Math.min(486, Math.max(0, target.clientWidth - 1)),
    y: Math.min(78, Math.max(0, target.clientHeight - 1))
  };
"""


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def checked_player(root: Path, relative: Path) -> tuple[Path, bytes, int]:
    lexical = root / relative
    try:
        info = lexical.lstat()
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise RuntimeSafetyError(
            f"Studio player escaped or is unavailable: {relative}"
        ) from exc
    if lexical.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise RuntimeSafetyError(
            f"Studio player must be a regular non-symlink file: {relative}"
        )
    if info.st_uid != os.getuid():
        raise RuntimeSafetyError(
            f"Studio player must be owned by the runtime user: {relative}"
        )
    return resolved, resolved.read_bytes(), stat.S_IMODE(info.st_mode)


def patch_studio_player(
    studio_workspace: Path, *, apply: bool
) -> dict[str, object]:
    try:
        lexical = studio_workspace.expanduser()
        if not lexical.is_absolute() or lexical.is_symlink():
            raise RuntimeSafetyError(
                "Studio workspace must be an absolute non-symlink directory"
            )
        root = lexical.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeSafetyError("Studio workspace is unavailable") from exc
    if not root.is_dir():
        raise RuntimeSafetyError("Studio workspace must be a directory")

    inspected = [checked_player(root, relative) for relative in PLAYER_PATHS]
    hashes = [digest(payload) for _path, payload, _mode in inspected]
    if len(set(hashes)) != 1 or hashes[0] not in {INPUT_SHA256, OUTPUT_SHA256}:
        raise RuntimeSafetyError(
            "Studio player copies are divergent or not the reviewed legacy revision"
        )

    before = hashes[0]
    status = "already_patched" if before == OUTPUT_SHA256 else "ready_to_patch"
    if before == INPUT_SHA256:
        patched_payloads: list[tuple[Path, bytes, int]] = []
        for path, payload, mode in inspected:
            if payload.count(OLD_BLOCK) != 1:
                raise RuntimeSafetyError(
                    "reviewed Studio player patch block is absent or duplicated"
                )
            patched = payload.replace(OLD_BLOCK, NEW_BLOCK)
            if digest(patched) != OUTPUT_SHA256:
                raise RuntimeSafetyError(
                    "reviewed Studio player patch produced unexpected bytes"
                )
            patched_payloads.append((path, patched, mode))
        if apply:
            for path, payload, mode in patched_payloads:
                atomic_write_bytes(path, payload, mode=mode)
            status = "patched"

    return {
        "schema": "vista-blender-world-studio-player-patch/v1",
        "status": status,
        "applied": bool(apply and before == INPUT_SHA256),
        "studio_workspace": str(root),
        "paths": [str(root / relative) for relative in PLAYER_PATHS],
        "sha256_before": before,
        "sha256_after": OUTPUT_SHA256
        if apply or before == OUTPUT_SHA256
        else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-workspace", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    print(
        json.dumps(
            patch_studio_player(args.studio_workspace, apply=args.apply),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeSafetyError as error:
        print(f"Studio player patch refused: {error}", file=sys.stderr)
        raise SystemExit(2)
