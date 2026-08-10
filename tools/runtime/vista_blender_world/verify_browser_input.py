#!/usr/bin/env python3
"""Correlate browser evidence with grounded UE movement receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import stat
import zipfile
from datetime import datetime
from io import BufferedReader
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_blender_world.runtime import (  # type: ignore
        RuntimeSafetyError,
        atomic_write_json,
        utc_now,
    )
else:
    from .runtime import RuntimeSafetyError, atomic_write_json, utc_now


SCHEMA = "vista-blender-world-browser-input/v1"
LIVE_SCHEMA = "vista-blender-world-live-qa/v1"
EXPECTED_WORLD = "MMG040_Office_BlenderR1"
EXPECTED_START_XY_CM = (150.0, -150.0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stream_contains_secret(handle: BufferedReader, secret: bytes) -> bool:
    overlap = b""
    while chunk := handle.read(1024 * 1024):
        candidate = overlap + chunk
        if secret in candidate:
            return True
        overlap = candidate[-(len(secret) - 1) :] if len(secret) > 1 else b""
    return False


def verify_trace_secret_hygiene(workspace: Path, trace_path: Path) -> dict[str, bool]:
    """Reject active access tokens and secret-bearing network logs in evidence."""

    token_path = workspace / "access-token"
    try:
        metadata = token_path.lstat()
    except OSError as exc:
        raise RuntimeSafetyError("active access token is unavailable for trace scan") from exc
    if (
        token_path.is_symlink()
        or not token_path.is_file()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeSafetyError("active access token must be a regular 0600 file")
    secret = token_path.read_bytes().strip()
    if len(secret) < 32:
        raise RuntimeSafetyError("active access token is too short for trace scan")

    if zipfile.is_zipfile(trace_path):
        try:
            with zipfile.ZipFile(trace_path) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    if Path(member.filename).suffix == ".network":
                        raise RuntimeSafetyError(
                            "browser evidence must exclude Playwright network logs"
                        )
                    with archive.open(member) as handle:
                        if _stream_contains_secret(handle, secret):
                            raise RuntimeSafetyError(
                                "browser evidence contains the active access token"
                            )
        except zipfile.BadZipFile as exc:
            raise RuntimeSafetyError("browser trace archive is invalid") from exc
    else:
        with trace_path.open("rb") as handle:
            if _stream_contains_secret(handle, secret):
                raise RuntimeSafetyError(
                    "browser evidence contains the active access token"
                )
    return {"active_token_present": False, "network_log_included": False}


def _owned_file(workspace: Path, path: Path, parent: str) -> Path:
    try:
        root = workspace.expanduser().resolve(strict=True)
        allowed = (root / parent).resolve(strict=True)
        candidate = path.expanduser().resolve(strict=True)
        candidate.relative_to(allowed)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise RuntimeSafetyError(f"{parent} evidence must be an existing owned file") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise RuntimeSafetyError(f"{parent} evidence must be a regular non-symlink file")
    return candidate


def _load_state_receipt(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeSafetyError("live state receipt is unreadable") from exc
    observed = receipt.get("observed") if isinstance(receipt, dict) else None
    if not (
        receipt.get("schema") == LIVE_SCHEMA
        and receipt.get("action") == "state"
        and receipt.get("acceptance") == "observed"
        and receipt.get("error") is None
        and isinstance(observed, dict)
        and observed.get("action") == "state"
        and observed.get("pie") is True
        and observed.get("possessed") is True
        and observed.get("on_ground") is True
        and observed.get("world") == EXPECTED_WORLD
    ):
        raise RuntimeSafetyError("live state receipt is not a grounded owned observation")
    location = observed.get("pawn_location_cm")
    if not (
        isinstance(location, list)
        and len(location) == 3
        and all(isinstance(value, (int, float)) and math.isfinite(value) for value in location)
    ):
        raise RuntimeSafetyError("live state receipt has no finite pawn location")
    return receipt, observed


def build_acceptance(before_path: Path, after_path: Path, trace_path: Path) -> dict[str, Any]:
    before_receipt, before = _load_state_receipt(before_path)
    after_receipt, after = _load_state_receipt(after_path)
    if (
        before_receipt.get("runtime_created_at") != after_receipt.get("runtime_created_at")
        or before_receipt.get("runtime_ready_at") != after_receipt.get("runtime_ready_at")
        or before.get("pawn_class") != after.get("pawn_class")
    ):
        raise RuntimeSafetyError("input observations are not from one runtime/pawn")
    start = [float(value) for value in before["pawn_location_cm"]]
    end = [float(value) for value in after["pawn_location_cm"]]
    if math.hypot(start[0] - EXPECTED_START_XY_CM[0], start[1] - EXPECTED_START_XY_CM[1]) > 1.0:
        raise RuntimeSafetyError("before observation is not the fixed PlayerStart")
    displacement = math.dist(start, end)
    if not 5.0 <= displacement <= 150.0:
        raise RuntimeSafetyError("browser-correlated pawn displacement is outside the safe acceptance band")
    if abs(end[2] - start[2]) > 10.0:
        raise RuntimeSafetyError("pawn did not remain grounded during browser input")
    engine_delta = float(after["engine_time_s"]) - float(before["engine_time_s"])
    if not 0.0 < engine_delta <= 30.0:
        raise RuntimeSafetyError("input observations have an invalid engine-time interval")
    wall_delta = (
        datetime.fromisoformat(after_receipt["created_at"])
        - datetime.fromisoformat(before_receipt["created_at"])
    ).total_seconds()
    if not 0.0 < wall_delta <= 30.0:
        raise RuntimeSafetyError("input observations have an invalid wall-time interval")
    if trace_path.stat().st_size <= 0:
        raise RuntimeSafetyError("browser trace is empty")
    return {
        "schema": SCHEMA,
        "created_at": utc_now(),
        "acceptance": "passed",
        "transport": "Pixel Streaming browser keyboard (correlated)",
        "runtime_created_at": before_receipt.get("runtime_created_at"),
        "runtime_ready_at": before_receipt.get("runtime_ready_at"),
        "world": EXPECTED_WORLD,
        "before": {
            "receipt": str(before_path),
            "sha256": _sha256(before_path),
            "pawn_location_cm": start,
            "engine_time_s": before["engine_time_s"],
        },
        "after": {
            "receipt": str(after_path),
            "sha256": _sha256(after_path),
            "pawn_location_cm": end,
            "engine_time_s": after["engine_time_s"],
        },
        "displacement_cm": displacement,
        "engine_delta_s": engine_delta,
        "wall_delta_s": wall_delta,
        "browser_trace": {
            "path": str(trace_path),
            "bytes": trace_path.stat().st_size,
            "sha256": _sha256(trace_path),
        },
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Verify bounded browser-to-UE input evidence")
    result.add_argument("--workspace", required=True, type=Path)
    result.add_argument("--before-receipt", required=True, type=Path)
    result.add_argument("--after-receipt", required=True, type=Path)
    result.add_argument("--browser-trace", required=True, type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.workspace.expanduser().resolve(strict=True)
    before = _owned_file(root, args.before_receipt, "receipts")
    after = _owned_file(root, args.after_receipt, "receipts")
    trace = _owned_file(root, args.browser_trace, "evidence")
    secret_scan = verify_trace_secret_hygiene(root, trace)
    acceptance = build_acceptance(before, after, trace)
    acceptance["secret_scan"] = secret_scan
    timestamp = acceptance["created_at"].replace(":", "").replace("+", "_")
    output = root / "receipts" / f"browser-input-{timestamp}.json"
    atomic_write_json(output, acceptance)
    print(json.dumps({**acceptance, "receipt_path": str(output)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeSafetyError as error:
        print(f"browser input verification refused: {error}", file=__import__("sys").stderr)
        raise SystemExit(2)
