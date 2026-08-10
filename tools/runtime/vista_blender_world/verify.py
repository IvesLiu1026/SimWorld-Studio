#!/usr/bin/env python3
"""Verify live loopback listeners, authentication, UE health, and process binding."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_blender_world.runtime import (  # type: ignore
        LOOPBACK_HOST,
        VERIFY_SCHEMA,
        Ports,
        RuntimeSafetyError,
        assert_owned_loopback_listener,
        atomic_write_json,
        ensure_access_token,
        identity_is_live,
        load_runtime_state,
        pid_entries,
        utc_now,
    )
else:
    from .runtime import (
        LOOPBACK_HOST,
        VERIFY_SCHEMA,
        Ports,
        RuntimeSafetyError,
        assert_owned_loopback_listener,
        atomic_write_json,
        ensure_access_token,
        identity_is_live,
        load_runtime_state,
        pid_entries,
        utc_now,
    )


def http_request(
    url: str, *, token: str | None = None
) -> tuple[int, bytes, dict[str, str]]:
    headers = {"Connection": "close"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return (
                response.status,
                response.read(2 * 1024 * 1024),
                dict(response.headers),
            )
    except urllib.error.HTTPError as error:
        return error.code, error.read(2 * 1024 * 1024), dict(error.headers)
    except urllib.error.URLError as error:
        raise RuntimeSafetyError(
            f"HTTP verification failed for loopback service: {error.reason}"
        ) from error


def verify(workspace: Path, *, receipt_path: Path | None = None) -> dict[str, Any]:
    _state_path, state = load_runtime_state(workspace)
    if state.get("status") != "ready":
        raise RuntimeSafetyError(
            f"runtime is not ready; state is {state.get('status')!r}"
        )
    raw_ports = state.get("ports")
    if not isinstance(raw_ports, dict):
        raise RuntimeSafetyError("runtime state has no port contract")
    try:
        ports = Ports(**raw_ports).validate()
    except TypeError as error:
        raise RuntimeSafetyError("runtime state port contract is malformed") from error
    identities = list(pid_entries(state))
    dead = [
        str(item.get("role", "unknown"))
        for item in identities
        if not identity_is_live(item)
    ]
    if dead:
        raise RuntimeSafetyError(
            f"runtime process identity is no longer live: {', '.join(dead)}"
        )
    identities_by_role = {str(item.get("role")): item for item in identities}
    if set(identities_by_role) != {"cirrus", "ue", "studio"}:
        raise RuntimeSafetyError("runtime process roles are missing or duplicated")
    listener_contract = (
        ("studio", ports.studio, "studio"),
        ("ue_mcp", ports.ue_mcp, "ue"),
        ("cirrus_http", ports.cirrus_http, "cirrus"),
        ("cirrus_streamer", ports.cirrus_streamer, "cirrus"),
        ("cirrus_sfu", ports.cirrus_sfu, "cirrus"),
    )
    listener_receipt = {
        name: assert_owned_loopback_listener(
            port, int(identities_by_role[role]["pid"])
        )
        for name, port, role in listener_contract
    }
    token_file = Path(str(state.get("token_file", "")))
    workspace_path = Path(state["workspace"]).resolve(strict=True)
    try:
        token_file.resolve(strict=False).relative_to(workspace_path)
    except ValueError as error:
        raise RuntimeSafetyError(
            "runtime token file escaped the runtime workspace"
        ) from error
    token = ensure_access_token(token_file)
    studio_base = f"http://{LOOPBACK_HOST}:{ports.studio}"
    cirrus_base = f"http://{LOOPBACK_HOST}:{ports.cirrus_http}"
    studio_unauthorized, _body, _headers = http_request(f"{studio_base}/api/health")
    if studio_unauthorized != 401:
        raise RuntimeSafetyError("Studio accepted an unauthenticated health request")
    cirrus_unauthorized, _body, _headers = http_request(f"{cirrus_base}/")
    if cirrus_unauthorized != 401:
        raise RuntimeSafetyError("Cirrus accepted an unauthenticated player request")
    studio_status, body, _headers = http_request(
        f"{studio_base}/api/health", token=token
    )
    if studio_status != 200:
        raise RuntimeSafetyError(
            f"authenticated Studio health returned HTTP {studio_status}"
        )
    try:
        health = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeSafetyError("Studio health response is not JSON") from error
    if (
        health.get("status") != "ok"
        or health.get("ueConnected") is not True
        or health.get("mcpConnected") is not True
    ):
        raise RuntimeSafetyError("Studio health does not prove connected UE and MCP")
    cirrus_status, body, _headers = http_request(f"{cirrus_base}/", token=token)
    if cirrus_status != 200 or not body.strip():
        raise RuntimeSafetyError(
            "authenticated Cirrus player page is unavailable or empty"
        )
    receipt: dict[str, Any] = {
        "schema": VERIFY_SCHEMA,
        "verified_at": utc_now(),
        "workspace": str(Path(state["workspace"])),
        "project": state.get("project"),
        "map": state.get("map"),
        "gpu": state.get("gpu"),
        "ports": raw_ports,
        "listeners": listener_receipt,
        "processes": identities,
        "authentication": {
            "studio_unauthenticated_status": studio_unauthorized,
            "studio_authenticated_status": studio_status,
            "cirrus_unauthenticated_status": cirrus_unauthorized,
            "cirrus_authenticated_status": cirrus_status,
        },
        "health": {
            "status": health.get("status"),
            "ueConnected": health.get("ueConnected"),
            "mcpConnected": health.get("mcpConnected"),
            "engineVersion": health.get("engineVersion"),
            "pixelStreamingProfile": health.get("pixelStreamingProfile"),
        },
    }
    target = receipt_path
    if target is None:
        stamp = utc_now().replace(":", "").replace("+00:00", "Z").replace("-", "")
        target = Path(state["workspace"]) / "receipts" / f"health-{stamp}.json"
    target = target.expanduser()
    if not target.is_absolute():
        raise RuntimeSafetyError("verification receipt path must be absolute")
    try:
        target.resolve(strict=False).relative_to(workspace_path)
    except ValueError as error:
        raise RuntimeSafetyError(
            "verification receipt must stay under the runtime workspace"
        ) from error
    if target.exists() or target.is_symlink():
        raise RuntimeSafetyError(
            "verification receipt already exists; append-only receipts are not overwritten"
        )
    atomic_write_json(target, receipt)
    receipt["receipt_path"] = str(target)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            verify(args.workspace, receipt_path=args.receipt), indent=2, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeSafetyError as error:
        print(f"runtime verification failed: {error}", file=sys.stderr)
        raise SystemExit(2)
