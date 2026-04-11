"""MCP TCP client for talking to UE's editor Python interpreter.

The SimWorld JS server uses the same channel via ``mcp-server.js``.  We
do NOT touch the JS server here — this client speaks the JSON-line
protocol (port 55557 by default) directly.

Lifecycle note
--------------
The UE editor exposes a Python interpreter via this TCP channel.  It
runs the script on the **editor world** game thread.  This is fully
available **before** PIE (Play-In-Editor) is started, and is in fact
the only way to start PIE programmatically (call
``LevelEditorSubsystem.editor_play_simulate()``).  Once PIE is
running, ``execute_python_script`` calls do still get *delivered*, but
they execute against the editor world (not the PIE game world), so
they cannot interact with PIE actors.

Use this client for:

  * Pre-experiment scene setup (spawn buildings via Studio API).
  * Starting / stopping PIE.
  * Editor-world introspection (e.g. ``get_actors_in_level``).

Use UnrealCV (``ucv_client.UCVClient``) for everything that happens
inside PIE — agent spawning, control, observations, camera capture.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class MCPError(RuntimeError):
    """Raised when an MCP command fails or times out."""


class MCPClient:
    """One-shot JSON-over-TCP client for SimWorld's MCP server.

    Each call opens a new socket and closes it.  Cheap and stateless,
    matches the design of the JS server's ``ueCommand`` helper.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 55557,
        timeout: float = 30.0,
        name: str = "mcp",
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.name = name

    # ------------------------------------------------------------------

    def call(self, cmd_type: str, params: Optional[Dict[str, Any]] = None,
             *, timeout: Optional[float] = None) -> Optional[dict]:
        """Send ``{type, params}`` and return the parsed JSON reply."""
        params = params or {}
        msg = json.dumps({"type": cmd_type, "params": params}) + "\n"
        log.debug("[%s] >> %s %s", self.name, cmd_type,
                  json.dumps(params)[:200])

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout if timeout is not None else self.timeout)
        try:
            sock.connect((self.host, self.port))
            sock.sendall(msg.encode("utf-8"))
            buf = ""
            while True:
                chunk = sock.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buf += chunk
                try:
                    result = json.loads(buf)
                    log.debug("[%s] << %s", self.name, str(result)[:200])
                    return result
                except json.JSONDecodeError:
                    continue
            if buf.strip():
                return json.loads(buf)
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise MCPError(
                f"[{self.name}] {cmd_type} failed: {exc}"
            ) from exc
        finally:
            sock.close()

    # ------------------------------------------------------------------
    # Convenience wrappers — only the ones the env actually needs
    # ------------------------------------------------------------------

    def execute_python(self, script: str, timeout: float = 30.0) -> dict:
        """Run a Python snippet inside UE's editor interpreter.

        Returns the parsed reply from UE which typically looks like
        ``{"status": "ok"|"error", "result": {"python_logs": [...]}}``.
        """
        return self.call("execute_python_script", {"script": script},
                         timeout=timeout) or {}

    def get_actors_in_level(self) -> dict:
        """Return ``{actors: [...]}``.  Editor-world actors only.

        In PIE mode this still returns the editor world's actors
        (buildings, props), not the PIE-spawned agents — those must be
        queried via UnrealCV's ``vget /objects``.
        """
        return self.call("get_actors_in_level", {}, timeout=10) or {}

    # ------------------------------------------------------------------
    # PIE lifecycle
    # ------------------------------------------------------------------

    def is_pie_active(self) -> bool:
        """Best-effort check whether PIE is currently running."""
        script = (
            "import unreal\n"
            "gw = unreal.EditorLevelLibrary.get_game_world()\n"
            "print('PIE_ACTIVE' if gw is not None else 'PIE_INACTIVE')\n"
        )
        try:
            resp = self.execute_python(script, timeout=10)
        except MCPError as exc:
            log.warning("[%s] is_pie_active probe failed: %s", self.name, exc)
            return False
        logs = _extract_python_logs(resp)
        for line in logs:
            if "PIE_ACTIVE" in line:
                return True
            if "PIE_INACTIVE" in line:
                return False
        return False

    def start_pie(self, *, wait_seconds: float = 5.0) -> None:
        """Start PIE if it isn't already running.

        Idempotent — safe to call repeatedly.  Sleeps ``wait_seconds``
        after issuing the start command so PIE has time to initialise
        UnrealCV inside the new game world.
        """
        if self.is_pie_active():
            log.info("[%s] PIE already active", self.name)
            return
        log.info("[%s] starting PIE...", self.name)
        script = (
            "import unreal\n"
            "le = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
            "if le is None:\n"
            "    print('NO_LEVEL_EDITOR_SUBSYSTEM')\n"
            "else:\n"
            "    try:\n"
            "        le.editor_play_simulate()\n"
            "        print('PIE_START_REQUESTED')\n"
            "    except Exception as e:\n"
            "        print('PIE_START_FAILED:' + repr(e))\n"
        )
        try:
            self.execute_python(script, timeout=15)
        except MCPError as exc:
            raise MCPError(f"[{self.name}] PIE start failed: {exc}") from exc
        time.sleep(wait_seconds)


def _extract_python_logs(resp: Optional[dict]) -> list:
    """Pull the ``python_logs`` array out of an execute_python_script reply.

    UE replies have shape ``{status, result: {python_logs: [...]}}`` but
    older builds wrap differently, so we tolerate both.
    """
    if not resp:
        return []
    result = resp.get("result")
    if isinstance(result, dict) and "python_logs" in result:
        return list(result["python_logs"])
    if isinstance(resp.get("python_logs"), list):
        return list(resp["python_logs"])
    return []
