from __future__ import annotations

import json
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

from tools.runtime.vista_playable_home import acceptance, runtime
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
        stale_placement: bool = False,
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
        self.stale_placement = stale_placement
        self.generation = 0
        self.active_event: str | None = None
        self.door_states = {
            acceptance.DOOR_ID: False,
            acceptance.OFFICE_DOOR_ID: True,
        }
        self.keys_held_by = ""
        self.keys_placed_at = (
            "home.r1/room.living_room/entity.coffee_table.01#tabletop_left"
        )
        self.keys_location = [-435.0, -170.0, 50.0]
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
            if target in self.door_states:
                if affordance == "open":
                    self.door_states[target] = True
                    code = "DOOR_OPENED"
                elif affordance == "close":
                    self.door_states[target] = False
                    code = "DOOR_CLOSED"
                else:
                    code = "INSPECTED"
                state = _state(
                    target,
                    values={
                        "visible": "true",
                        "open": "true" if self.door_states[target] else "false",
                    },
                )
            elif target == acceptance.NPC_ID:
                code = "NPC_INSPECTED"
                if self.npc_queued:
                    self.npc_polls += 1
                    locations = (
                        [-435.0, -170.0, 96.0],
                        [0.0, 0.0, 96.0],
                        [240.0, 200.0, 96.0],
                    )
                    location = locations[min(self.npc_polls, len(locations)) - 1]
                    if self.npc_polls < 3:
                        self.keys_held_by = acceptance.NPC_ID
                        self.keys_placed_at = ""
                        self.keys_location = list(location)
                    else:
                        self.keys_held_by = ""
                        self.keys_placed_at = acceptance.OFFICE_DESK_ANCHOR_ID
                        self.keys_location = list(acceptance.OFFICE_DESK_LOCATION_CM)
                else:
                    location = [0.0, 0.0, 96.0]
                state = _state(
                    target,
                    location=location,
                    values={
                        "current_room_id": (
                            "home.r1/room.office"
                            if acceptance.npc_is_in_office(location)
                            else "home.r1/room.entry_hall"
                        )
                    },
                )
            elif target == acceptance.KEYS_ID:
                if affordance == "pick_up":
                    self.keys_held_by = acceptance.PLAYER_ID
                    self.keys_placed_at = ""
                    code = "ITEM_PICKED_UP"
                elif affordance == "place":
                    if params["placement_anchor_semantic_id"] != acceptance.TABLETOP_RIGHT_ID:
                        raise AssertionError("placement anchor differs")
                    self.keys_held_by = ""
                    self.keys_placed_at = (
                        "home.r1/room.living_room/entity.coffee_table.01#tabletop_left"
                        if self.stale_placement
                        else acceptance.TABLETOP_RIGHT_ID
                    )
                    self.keys_location = list(acceptance.TABLETOP_RIGHT_LOCATION_CM)
                    code = "ITEM_PLACED"
                else:
                    code = "INSPECTED"
                state = _state(
                    target,
                    location=self.keys_location,
                    portable=True,
                    values={
                        "visible": "true",
                        "held": "true" if self.keys_held_by else "false",
                        "held_by": self.keys_held_by,
                        "placed_at": self.keys_placed_at,
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
    def __init__(
        self,
        root: Path,
        *,
        runtime_profile: str | None = None,
    ) -> None:
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
        if runtime_profile == runtime.R2_RUNTIME_PROFILE:
            editor = (
                self.root
                / "UE"
                / "Engine"
                / "Binaries"
                / "Linux"
                / "UnrealEditor"
            )
            editor.parent.mkdir(parents=True)
            editor.write_text("#!/bin/sh\n", encoding="utf-8")
            editor.chmod(0o700)
            r2_config = runtime.GameRuntimeConfig(
                workspace=self.workspace,
                project=project,
                ue_editor=editor,
                map_path=MAP_PATH,
                display=runtime.R2_DISPLAY,
                gpu=runtime.R2_GPU,
                vista_world_port=runtime.R2_VISTA_WORLD_PORT,
                width=runtime.R2_WIDTH,
                height=runtime.R2_HEIGHT,
                fps=runtime.R2_FPS,
                runtime_profile=runtime.R2_RUNTIME_PROFILE,
            )
            launch_plan = runtime.redacted_plan(r2_config)
            self.launch_plan_path = runtime_attempt / "launch-plan.json"
            self.launch_plan_path.write_bytes(
                acceptance._canonical_json_bytes(launch_plan)
            )
            state.update(
                {
                    "schema": acceptance.R2_RUNTIME_STATE_SCHEMA,
                    "display": runtime.R2_DISPLAY,
                    "gpu": runtime.R2_GPU,
                    "vista_world_port": runtime.R2_VISTA_WORLD_PORT,
                    "runtime_profile": runtime.R2_RUNTIME_PROFILE,
                    "camera_profile": runtime.R2_CAMERA_PROFILE,
                    "width": runtime.R2_WIDTH,
                    "height": runtime.R2_HEIGHT,
                    "fps": runtime.R2_FPS,
                    "launch_plan_sha256": acceptance.sha256_file(
                        self.launch_plan_path
                    ),
                }
            )
        elif runtime_profile is not None:
            raise AssertionError("fixture runtime profile is unsupported")
        else:
            self.launch_plan_path = None
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
        if runtime_profile == runtime.R2_RUNTIME_PROFILE:
            build.update(
                {
                    "visual_profile_id": runtime.R2_RUNTIME_PROFILE,
                    "visual_profile_sha256": "4" * 64,
                    "visual_profile_content_digest": "5" * 64,
                    "renderer_profile_request_sha256": "6" * 64,
                    "renderer_profile_request_content_digest": "7" * 64,
                    "renderer_runtime_observation": "pending",
                    "base_scene_receipt_sha256": "3" * 64,
                    "presentation_import_receipt_sha256": "8" * 64,
                    "presentation_scene_receipt_sha256": "9" * 64,
                    "presentation_manifest_sha256": "a" * 64,
                    "presentation_artifact_receipt_sha256": "b" * 64,
                    "presentation_bundle_count": 3,
                    "presentation_collision_policy": (
                        acceptance.R2_PRESENTATION_COLLISION_POLICY
                    ),
                    "presentation_ue_import_observation": (
                        "verified_by_commandlet"
                    ),
                    "presentation_runtime_play_proof": "pending",
                }
            )
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
            runtime_profile=runtime_profile,
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

    def test_door_clear_predicate_requires_safe_living_room_region(self) -> None:
        self.assertTrue(acceptance.npc_is_living_room_door_clear([-371.0, -270.0]))
        self.assertFalse(acceptance.npc_is_living_room_door_clear([-334.0, -252.0]))
        self.assertFalse(acceptance.npc_is_living_room_door_clear([-371.0, 100.0]))
        self.assertFalse(acceptance.npc_is_living_room_door_clear([-700.0, -270.0]))
        self.assertTrue(acceptance.npc_is_in_office([240.0, 200.0]))
        self.assertFalse(acceptance.npc_is_in_office([0.0, 0.0]))

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
        self.assertEqual(receipt["final_generation"], 22)
        self.assertEqual(len(receipt["checks"]), 29)
        self.assertEqual(len(server.requests), 29)
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
        self.assertFalse(acceptance.npc_is_living_room_door_clear(before_xy))
        self.assertTrue(acceptance.npc_is_in_office(after_xy))
        queue = next(
            check for check in receipt["checks"] if check["step"] == "npc.replace_queue"
        )
        self.assertEqual(
            queue["request"]["params"]["actions"],
            [
                {
                    "action_id": "acceptance.navigate.keys",
                    "type": "navigate_to",
                    "target_semantic_id": acceptance.KEYS_ID,
                    "timeout_sec": 20.0,
                },
                {
                    "action_id": "acceptance.pick_up.keys",
                    "type": "pick_up",
                    "target_semantic_id": acceptance.KEYS_ID,
                    "timeout_sec": 10.0,
                },
                {
                    "action_id": "acceptance.navigate.office",
                    "type": "navigate_to",
                    "target_semantic_id": acceptance.OFFICE_ANCHOR_ID,
                    "timeout_sec": 25.0,
                },
                {
                    "action_id": "acceptance.place.office_desk",
                    "type": "place",
                    "target_semantic_id": acceptance.OFFICE_DESK_ANCHOR_ID,
                    "timeout_sec": 10.0,
                },
                {
                    "action_id": "acceptance.wait.office",
                    "type": "wait",
                    "duration_sec": 5.0,
                    "timeout_sec": 7.0,
                },
            ],
        )
        held_poll = next(
            check
            for check in receipt["checks"]
            if check["step"].startswith("keys.inspect_cross_room_poll.")
            and check["response"]["state"]["values"]["held_by"] == acceptance.NPC_ID
        )
        self.assertEqual(held_poll["response"]["state"]["values"]["held"], "true")
        final_keys = next(
            check
            for check in reversed(receipt["checks"])
            if check["step"].startswith("keys.inspect_cross_room_poll.")
        )
        self.assertEqual(
            final_keys["response"]["state"]["values"]["placed_at"],
            acceptance.OFFICE_DESK_ANCHOR_ID,
        )
        self.assertEqual(
            final_keys["response"]["state"]["transform"]["location_cm"],
            list(acceptance.OFFICE_DESK_LOCATION_CM),
        )
        for step in (
            "door.open",
            "office_door.inspect_initial_open",
            "office_door.close_after_crossing",
            "office_door.inspect_closed_after_crossing",
        ):
            self.assertTrue(any(check["step"] == step for check in receipt["checks"]))
        event_steps = [check["step"] for check in receipt["checks"] if check["step"].startswith("event.")]
        for event_id in acceptance.EVENT_IDS:
            self.assertIn(f"event.{event_id}.start", event_steps)
            self.assertIn(f"event.{event_id}.reset", event_steps)

    def test_realistic_r2_acceptance_binds_profile_plan_build_and_port(self) -> None:
        fixture = RuntimeAcceptanceFixture(
            self.root,
            runtime_profile=runtime.R2_RUNTIME_PROFILE,
        )
        with FakeVistaRuntime() as server:
            code, receipt = acceptance.execute_acceptance(
                fixture.config,
                exchange=self._exchange(server),
            )
        self.assertEqual(code, 0)
        self.assertEqual(receipt["schema"], acceptance.R2_RECEIPT_SCHEMA)
        self.assertEqual(receipt["status"], "accepted")
        bindings = receipt["bindings"]
        self.assertEqual(bindings["runtime_profile"], runtime.R2_RUNTIME_PROFILE)
        self.assertEqual(bindings["camera_profile"], runtime.R2_CAMERA_PROFILE)
        self.assertEqual(bindings["display"], runtime.R2_DISPLAY)
        self.assertEqual(bindings["gpu"], runtime.R2_GPU)
        self.assertEqual(bindings["port"], runtime.R2_VISTA_WORLD_PORT)
        self.assertEqual(bindings["width"], runtime.R2_WIDTH)
        self.assertEqual(bindings["height"], runtime.R2_HEIGHT)
        self.assertEqual(bindings["fps"], runtime.R2_FPS)
        self.assertEqual(bindings["launch_plan"], str(fixture.launch_plan_path))
        self.assertEqual(
            bindings["launch_plan_sha256"],
            acceptance.sha256_file(fixture.launch_plan_path),
        )

    def test_realistic_r2_acceptance_rejects_state_plan_and_build_drift(self) -> None:
        cases = (
            ("state_port", "RUNTIME_STATE_INVALID"),
            ("camera_profile", "RUNTIME_STATE_INVALID"),
            ("launch_plan", "RUNTIME_LAUNCH_PLAN_INVALID"),
            ("build_renderer", "BUILD_RESULT_INVALID"),
        )
        for index, (case, expected_code) in enumerate(cases):
            with self.subTest(case=case):
                root = self.root / f"r2-{index}"
                root.mkdir()
                fixture = RuntimeAcceptanceFixture(
                    root,
                    runtime_profile=runtime.R2_RUNTIME_PROFILE,
                )
                config_values = dict(fixture.config.__dict__)
                if case in {"state_port", "camera_profile"}:
                    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
                    if case == "state_port":
                        state["vista_world_port"] = runtime.R2_VISTA_WORLD_PORT + 1
                    else:
                        state["camera_profile"] = "default"
                    fixture.state_path.write_bytes(
                        acceptance._canonical_json_bytes(state)
                    )
                    config_values["runtime_state_sha256"] = acceptance.sha256_file(
                        fixture.state_path
                    )
                elif case == "launch_plan":
                    fixture.launch_plan_path.write_bytes(
                        fixture.launch_plan_path.read_bytes() + b"\n"
                    )
                else:
                    build = json.loads(fixture.build_path.read_text(encoding="utf-8"))
                    build["renderer_runtime_observation"] = "observed"
                    build["content_digest"] = acceptance._content_digest(build)
                    fixture.build_path.write_bytes(
                        acceptance._canonical_json_bytes(build)
                    )
                    config_values["build_result_sha256"] = acceptance.sha256_file(
                        fixture.build_path
                    )
                with self.assertRaises(acceptance.AcceptanceError) as caught:
                    acceptance.validate_binding(
                        acceptance.AcceptanceConfig(**config_values)
                    )
                self.assertEqual(caught.exception.code, expected_code)

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

    def test_stale_placement_semantic_fails_closed(self) -> None:
        with FakeVistaRuntime(stale_placement=True) as server:
            with self.assertRaisesRegex(
                acceptance.AcceptanceError, "exact tabletop-right semantic anchor"
            ) as caught:
                acceptance.run_protocol(
                    server.port,
                    socket_timeout_s=0.5,
                    npc_timeout_s=1.0,
                    npc_poll_interval_s=0.01,
                )
        self.assertEqual(caught.exception.code, "KEYS_STATE_MISMATCH")
        self.assertEqual(caught.exception.step, "keys.place_tabletop_right")

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
