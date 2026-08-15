from __future__ import annotations

import json
import math
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.runtime.vista_playable_home import acceptance
from tools.runtime.vista_playable_home.runtime import process_start_ticks


MAP_PATH = "/Game/VISTA/PlayableHome/vista_playable_home_r1/Maps/VistaPlayableHome"


def _state(
    semantic_id: str,
    *,
    location: list[float] | None = None,
    portable: bool = False,
    values: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "semantic_id": semantic_id,
        "hidden": False,
        "portable": portable,
        "transform": {
            "location_cm": location or [0.0, 0.0, 0.0],
            "rotation_deg": [0.0, 0.0, 0.0],
            "scale": [1.0, 1.0, 1.0],
        },
        "values": values or {"visible": "true"},
    }


class FakeVistaRuntime:
    """A real loopback TCP peer implementing only the accepted typed surface."""

    def __init__(
        self,
        *,
        drift_at: int | None = None,
        bad_schema_at: int | None = None,
        hang_at: int | None = None,
        trickle_at: int | None = None,
        trickle_interval_s: float = 0.02,
    ) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.05)
        self.port = self.listener.getsockname()[1]
        self.drift_at = drift_at
        self.bad_schema_at = bad_schema_at
        self.hang_at = hang_at
        self.trickle_at = trickle_at
        self.trickle_interval_s = trickle_interval_s
        self.generation = 0
        self.active_event: str | None = None
        self.door_open = False
        self.keys_held = False
        self.npc_queued = False
        self.npc_polls = 0
        self.requests: list[dict[str, Any]] = []
        self.error: BaseException | None = None
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "FakeVistaRuntime":
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._stopping.set()
        self.listener.close()
        self._thread.join(timeout=2)
        if exc_type is None and self.error is not None:
            raise self.error

    def _serve(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    connection, _address = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with connection:
                    connection.settimeout(1.0)
                    frame = bytearray()
                    while not frame.endswith(b"\n") and len(frame) <= 64 * 1024:
                        block = connection.recv(4096)
                        if not block:
                            break
                        frame.extend(block)
                    request = json.loads(bytes(frame))
                    index = len(self.requests)
                    self.requests.append(request)
                    if index == self.hang_at:
                        time.sleep(0.2)
                        continue
                    response = self._respond(request)
                    if index == self.drift_at and "session_generation" in response:
                        response["session_generation"] += 1
                    if index == self.bad_schema_at:
                        response["unexpected"] = True
                    encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
                    if index == self.trickle_at:
                        for byte in encoded:
                            if self._stopping.is_set():
                                break
                            try:
                                connection.sendall(bytes((byte,)))
                            except (BrokenPipeError, ConnectionResetError):
                                break
                            time.sleep(self.trickle_interval_s)
                        continue
                    try:
                        connection.sendall(encoded)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
        except BaseException as exc:
            if not self._stopping.is_set():
                self.error = exc

    def _respond(self, request: dict[str, Any]) -> dict[str, Any]:
        if set(request) != {"type", "params"} or request["type"] != "vista_world_action":
            raise AssertionError("unexpected typed envelope")
        params = request["params"]
        command_id = params["command_id"]
        operation = params["operation"]
        if operation == "status":
            if set(params) != {"operation", "command_id"}:
                raise AssertionError("status request fields differ")
            return {
                "command_id": command_id,
                "status": "success",
                "code": "READY",
                "world_revision": acceptance.DEFAULT_WORLD_REVISION,
                "session_generation": self.generation,
                "event_status": "active" if self.active_event else "inactive",
                "active_event": self.active_event,
            }

        if params["expected_revision"] != acceptance.DEFAULT_WORLD_REVISION:
            raise AssertionError("revision pin differs")
        if params["session_generation"] != self.generation:
            raise AssertionError("request generation differs")
        self.generation += 1

        if operation == "interaction":
            expected = {
                "operation",
                "command_id",
                "expected_revision",
                "session_generation",
                "requester_semantic_id",
                "target_semantic_id",
                "affordance",
            }
            if params.get("affordance") == "place":
                expected.add("placement_anchor_semantic_id")
            if set(params) != expected or params["requester_semantic_id"] != acceptance.PLAYER_ID:
                raise AssertionError("interaction request fields differ")
            target = params["target_semantic_id"]
            affordance = params["affordance"]
            if target == acceptance.DOOR_ID:
                if affordance == "open":
                    self.door_open = True
                    code = "DOOR_OPENED"
                elif affordance == "close":
                    self.door_open = False
                    code = "DOOR_CLOSED"
                else:
                    code = "INSPECTED"
                state = _state(
                    target,
                    values={"visible": "true", "open": "true" if self.door_open else "false"},
                )
            elif target == acceptance.NPC_ID:
                code = "NPC_INSPECTED"
                if self.npc_queued:
                    self.npc_polls += 1
                    locations = (
                        [-140.0, -60.0, 96.0],
                        [-235.0, -205.0, 96.0],
                        [-475.0, -315.0, 96.0],
                    )
                    location = locations[min(self.npc_polls, len(locations)) - 1]
                else:
                    location = [260.0, 110.0, 96.0]
                state = _state(
                    target,
                    location=location,
                    values={"current_room_id": "home.r1/room.living_room"},
                )
            elif target == acceptance.KEYS_ID:
                if affordance == "pick_up":
                    self.keys_held = True
                    code = "ITEM_PICKED_UP"
                elif affordance == "place":
                    if params["placement_anchor_semantic_id"] != acceptance.TABLETOP_RIGHT_ID:
                        raise AssertionError("placement anchor differs")
                    self.keys_held = False
                    code = "ITEM_PLACED"
                else:
                    code = "INSPECTED"
                state = _state(
                    target,
                    portable=True,
                    values={
                        "visible": "true",
                        "held": "true" if self.keys_held else "false",
                        "held_by": acceptance.PLAYER_ID if self.keys_held else "",
                    },
                )
            else:
                raise AssertionError(f"unexpected interaction target: {target}")
            return {
                "command_id": command_id,
                "status": "success",
                "code": code,
                "session_generation": self.generation,
                "target_semantic_id": target,
                "state": state,
            }

        if operation == "npc_queue":
            if set(params) != {
                "operation",
                "command_id",
                "expected_revision",
                "session_generation",
                "npc_semantic_id",
                "replace",
                "actions",
            }:
                raise AssertionError("NPC queue request fields differ")
            self.npc_queued = True
            return {
                "command_id": command_id,
                "status": "success",
                "code": "QUEUE_REPLACED",
                "session_generation": self.generation,
                "target_semantic_id": params["npc_semantic_id"],
            }

        if operation == "event":
            if params["event_operation"] == "start_event":
                self.active_event = params["event_id"]
                code = "EVENT_STARTED"
            elif params["event_operation"] == "reset_event":
                if "event_id" in params:
                    raise AssertionError("reset event must not carry event_id")
                self.active_event = None
                code = "EVENT_RESET"
            else:
                raise AssertionError("unexpected event operation")
            return {
                "command_id": command_id,
                "status": "success",
                "code": code,
                "session_generation": self.generation,
            }
        raise AssertionError(f"unexpected operation: {operation}")


class RuntimeAcceptanceFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.workspace = root / "workspace"
        self.repo.mkdir()
        self.workspace.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "runtime-acceptance@example.invalid")
        self._git("config", "user.name", "Runtime Acceptance Test")
        (self.repo / "tracked.txt").write_text("pinned\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "fixture")
        self.commit = self._git("rev-parse", "HEAD").strip()

        project = self.workspace / "project" / "VistaPlayableHome.uproject"
        project.parent.mkdir()
        project.write_text("{}\n", encoding="utf-8")
        runtime_attempt = (
            self.workspace
            / "game-runtime"
            / f"attempt-20260815T120000.000000Z-{os.getpid()}"
        )
        runtime_attempt.mkdir(parents=True)
        ticks = process_start_ticks(os.getpid())
        if ticks is None:
            raise AssertionError("test process identity is unavailable")
        process_group = os.getpgid(os.getpid())
        identity = {
            "pid": os.getpid(),
            "start_ticks": ticks,
            "process_group": process_group,
        }
        state = {
            "schema": acceptance.RUNTIME_STATE_SCHEMA,
            "status": "running",
            "created_at": "2026-08-15T12:00:00+00:00",
            "updated_at": "2026-08-15T12:00:01+00:00",
            "map": MAP_PATH,
            "project": str(project),
            "display": ":117",
            "gpu": 0,
            "vista_world_port": acceptance.DEFAULT_VISTA_WORLD_PORT,
            "process": {"role": "unreal-game", **identity},
            "supervisor": {"role": "vista-world-supervisor", **identity},
            "readiness": {
                "command_id": "vwc-" + "a" * 24,
                "status": "success",
                "code": "READY",
                "world_revision": acceptance.DEFAULT_WORLD_REVISION,
                "session_generation": 0,
                "event_status": "inactive",
                "active_event": None,
            },
        }
        self.state_path = runtime_attempt / "runtime-state.json"
        self.state_path.write_bytes(acceptance._canonical_json_bytes(state))
        pointer = {
            "schema": acceptance.RUNTIME_POINTER_SCHEMA,
            "state": f"{runtime_attempt.name}/runtime-state.json",
        }
        (self.workspace / "game-runtime" / "current.json").write_bytes(
            acceptance._canonical_json_bytes(pointer)
        )

        build = {
            "schema_version": acceptance.BUILD_RESULT_SCHEMA,
            "status": "accepted_candidate",
            "timestamp_utc": "2026-08-15T11:59:00+00:00",
            "attempt_root": str(self.workspace),
            "revision": acceptance.DEFAULT_WORLD_REVISION,
            "map_path": MAP_PATH,
            "execution_sha256": "1" * 64,
            "import_receipt_sha256": "2" * 64,
            "scene_receipt_sha256": "3" * 64,
            "copy_methods": {"copy": 1},
            "runtime_play_proof": "pending",
        }
        build["content_digest"] = acceptance._content_digest(build)
        self.build_path = self.workspace / "result-receipt.json"
        self.build_path.write_bytes(acceptance._canonical_json_bytes(build))
        self.output = runtime_attempt / "runtime-acceptance-test.json"
        self.config = acceptance.AcceptanceConfig(
            workspace=self.workspace,
            repo_root=self.repo,
            output=self.output,
            runtime_state_sha256=acceptance.sha256_file(self.state_path),
            build_result_sha256=acceptance.sha256_file(self.build_path),
            source_commit=self.commit,
            socket_timeout_s=0.5,
            npc_timeout_s=2.0,
            npc_poll_interval_s=0.01,
        )

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return completed.stdout


class VistaPlayableHomeRuntimeAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _exchange(server: FakeVistaRuntime) -> acceptance.Exchange:
        return lambda request, timeout: acceptance.exchange_loopback(
            request, timeout, port=server.port
        )

    def test_full_tcp_sequence_writes_private_bound_acceptance(self) -> None:
        fixture = RuntimeAcceptanceFixture(self.root)
        with FakeVistaRuntime() as server:
            code, receipt = acceptance.execute_acceptance(
                fixture.config, exchange=self._exchange(server)
            )
        self.assertEqual(code, 0)
        self.assertEqual(receipt["schema"], acceptance.RECEIPT_SCHEMA)
        self.assertEqual(receipt["status"], "accepted")
        self.assertIsNone(receipt["error"])
        self.assertEqual(receipt["initial_generation"], 0)
        self.assertEqual(receipt["final_generation"], 17)
        self.assertEqual(len(receipt["checks"]), 24)
        self.assertEqual(len(server.requests), 24)
        self.assertEqual(stat.S_IMODE(fixture.output.stat().st_mode), 0o600)
        self.assertEqual(json.loads(fixture.output.read_text()), receipt)

        for check in receipt["checks"]:
            delta = check["generation_after"] - check["generation_before"]
            self.assertEqual(delta, 1 if check["mutation"] else 0, check["step"])
        preinspect = next(
            check for check in receipt["checks"] if check["step"] == "npc.preinspect"
        )
        final_poll = next(
            check
            for check in reversed(receipt["checks"])
            if check["step"].startswith("npc.inspect_poll.")
        )
        before_xy = preinspect["response"]["state"]["transform"]["location_cm"][:2]
        after_xy = final_poll["response"]["state"]["transform"]["location_cm"][:2]
        self.assertGreater(
            math.dist(before_xy, acceptance.LIVING_TARGET_XY),
            acceptance.LIVING_ACCEPTANCE_RADIUS_CM,
        )
        self.assertLessEqual(
            math.dist(after_xy, acceptance.LIVING_TARGET_XY),
            acceptance.LIVING_ACCEPTANCE_RADIUS_CM,
        )
        queue = next(
            check for check in receipt["checks"] if check["step"] == "npc.replace_queue"
        )
        self.assertEqual(
            queue["request"]["params"]["actions"],
            [
                {
                    "action_id": "acceptance.navigate.living",
                    "type": "navigate_to",
                    "target_semantic_id": acceptance.LIVING_ANCHOR_ID,
                    "timeout_sec": 20.0,
                },
                {
                    "action_id": "acceptance.navigate.living_clear",
                    "type": "navigate_to",
                    "target_location_cm": list(acceptance.LIVING_CLEAR_TARGET_CM),
                    "timeout_sec": 20.0,
                },
                {
                    "action_id": "acceptance.wait.living",
                    "type": "wait",
                    "duration_sec": 10.0,
                    "timeout_sec": 12.0,
                },
            ],
        )
        event_steps = [check["step"] for check in receipt["checks"] if check["step"].startswith("event.")]
        for event_id in acceptance.EVENT_IDS:
            self.assertIn(f"event.{event_id}.start", event_steps)
            self.assertIn(f"event.{event_id}.reset", event_steps)

    def test_generation_drift_fails_closed(self) -> None:
        with FakeVistaRuntime(drift_at=1) as server:
            with self.assertRaisesRegex(acceptance.AcceptanceError, "advance exactly one") as caught:
                acceptance.run_protocol(
                    server.port,
                    socket_timeout_s=0.5,
                    npc_timeout_s=1.0,
                    npc_poll_interval_s=0.01,
                )
        self.assertEqual(caught.exception.code, "GENERATION_DRIFT")

    def test_bad_response_schema_fails_and_leaves_failure_receipt(self) -> None:
        fixture = RuntimeAcceptanceFixture(self.root)
        with FakeVistaRuntime(bad_schema_at=0) as server:
            code, receipt = acceptance.execute_acceptance(
                fixture.config, exchange=self._exchange(server)
            )
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error"]["code"], "RESPONSE_SHAPE_INVALID")
        self.assertEqual(receipt["error"]["step"], "status.g0")
        self.assertEqual(stat.S_IMODE(fixture.output.stat().st_mode), 0o600)
        self.assertEqual(json.loads(fixture.output.read_text())["status"], "failed")

    def test_socket_timeout_is_bounded(self) -> None:
        with FakeVistaRuntime(hang_at=0) as server:
            with self.assertRaises(acceptance.AcceptanceError) as caught:
                acceptance.run_protocol(
                    server.port,
                    socket_timeout_s=0.05,
                    npc_timeout_s=0.2,
                    npc_poll_interval_s=0.01,
                )
        self.assertEqual(caught.exception.code, "RUNTIME_TIMEOUT")
        self.assertEqual(caught.exception.step, "status.g0")

    def test_socket_timeout_is_one_absolute_deadline_against_trickle_peer(self) -> None:
        started = time.monotonic()
        with FakeVistaRuntime(trickle_at=0, trickle_interval_s=0.02) as server:
            with self.assertRaises(acceptance.AcceptanceError) as caught:
                acceptance.run_protocol(
                    server.port,
                    socket_timeout_s=0.06,
                    npc_timeout_s=0.2,
                    npc_poll_interval_s=0.01,
                )
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "RUNTIME_TIMEOUT")
        self.assertEqual(caught.exception.step, "status.g0")
        # A per-recv timeout would be refreshed by every 20 ms byte and take
        # seconds to receive this response. The total exchange must stay bound.
        self.assertLess(elapsed, 0.3)

    def test_receipt_is_o_excl_and_existing_bytes_are_unchanged(self) -> None:
        fixture = RuntimeAcceptanceFixture(self.root)
        fixture.output.write_text("do-not-replace\n", encoding="utf-8")
        with self.assertRaises(acceptance.AcceptanceError) as caught:
            acceptance.ExclusiveReceipt.reserve(fixture.workspace, fixture.output)
        self.assertEqual(caught.exception.code, "RECEIPT_EXISTS")
        self.assertEqual(fixture.output.read_text(), "do-not-replace\n")

    def test_symlink_and_path_identity_are_refused(self) -> None:
        fixture = RuntimeAcceptanceFixture(self.root)
        linked_workspace = self.root / "linked-workspace"
        linked_workspace.symlink_to(fixture.workspace, target_is_directory=True)
        linked_config = acceptance.AcceptanceConfig(
            **{
                **fixture.config.__dict__,
                "workspace": linked_workspace,
                "output": fixture.output,
            }
        )
        with self.assertRaises(acceptance.AcceptanceError) as caught:
            acceptance.validate_binding(linked_config)
        self.assertIn(caught.exception.code, {"PATH_SYMLINK_REFUSED", "PATH_IDENTITY_INVALID"})

        outside_output = fixture.workspace / "runtime-acceptance-outside.json"
        with self.assertRaises(acceptance.AcceptanceError) as caught:
            acceptance.ExclusiveReceipt.reserve(fixture.workspace, outside_output)
        self.assertEqual(caught.exception.code, "RECEIPT_PATH_INVALID")

        original = fixture.build_path.read_bytes()
        outside = self.root / "outside-result.json"
        outside.write_bytes(original)
        fixture.build_path.unlink()
        fixture.build_path.symlink_to(outside)
        with self.assertRaises(acceptance.AcceptanceError) as caught:
            acceptance.validate_binding(fixture.config)
        self.assertEqual(caught.exception.code, "PATH_IDENTITY_INVALID")


if __name__ == "__main__":
    unittest.main()
