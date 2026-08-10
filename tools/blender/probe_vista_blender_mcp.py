#!/usr/bin/env python3
"""Probe the authenticated Blender MCP endpoint without exposing its token."""

from __future__ import annotations

import argparse
import json
import pathlib
import urllib.parse
import urllib.request
from typing import Any


def _decode_sse(payload: bytes) -> dict[str, Any]:
    for line in payload.decode("utf-8").splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise RuntimeError("MCP response did not contain an SSE data record")


def _validate_loopback_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise argparse.ArgumentTypeError("MCP URL must be loopback HTTP")
    if parsed.path != "/mcp":
        raise argparse.ArgumentTypeError("MCP URL path must be /mcp")
    return value


class MCPClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.session_id: str | None = None

    def post(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.headers.get("mcp-session-id"):
                self.session_id = response.headers["mcp-session-id"]
            body = response.read()
            if not body:
                return None, response.status
            return _decode_sse(body), response.status


def probe(url: str, token_path: pathlib.Path) -> dict[str, Any]:
    token = token_path.read_text(encoding="ascii").strip()
    if len(token) < 32:
        raise RuntimeError("MCP token is missing or too short")
    client = MCPClient(url, token)
    initialized, init_status = client.post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "vista-blender-mcp-probe", "version": "1"},
            },
        }
    )
    if not initialized or "result" not in initialized:
        raise RuntimeError("MCP initialize failed")
    client.post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    tools_result, tools_status = client.post(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    if not tools_result or "result" not in tools_result:
        raise RuntimeError("MCP tools/list failed")
    names = [entry["name"] for entry in tools_result["result"]["tools"]]
    if "execute_python" in names:
        raise RuntimeError("unsafe execute_python tool is enabled")
    scene_result, scene_status = client.post(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_scene_info", "arguments": {}},
        }
    )
    if not scene_result or scene_result.get("result", {}).get("isError"):
        raise RuntimeError("MCP get_scene_info failed")
    content = scene_result["result"]["content"]
    scene = json.loads(content[0]["text"])
    return {
        "schema": "vista-blender-mcp-probe/v1",
        "url": url,
        "loopback_only": True,
        "initialize_http_status": init_status,
        "tools_http_status": tools_status,
        "scene_http_status": scene_status,
        "server_name": initialized["result"]["serverInfo"]["name"],
        "server_version": initialized["result"]["serverInfo"]["version"],
        "tool_count": len(names),
        "execute_python_enabled": False,
        "scene": scene,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", type=_validate_loopback_url, default="http://127.0.0.1:8400/mcp")
    parser.add_argument("--token-file", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    receipt = probe(args.url, args.token_file)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
