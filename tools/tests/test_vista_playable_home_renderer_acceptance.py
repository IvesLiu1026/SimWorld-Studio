from __future__ import annotations

import copy
import dataclasses
import json
import os
import pathlib
import stat
import types

import pytest

from tools.runtime.vista_playable_home import acceptance
from tools.runtime.vista_playable_home import renderer_acceptance as renderer
from tools.runtime.vista_playable_home import (
    packaged_entrypoint,
    packaged_profile,
    runtime,
)
from tools.tests.test_vista_playable_home_runtime_acceptance import (
    RuntimeAcceptanceFixture,
)
from tools.ue.vista_playable_home import build_home
from tools.ue.vista_playable_home import package_receipt as package


ROOT = pathlib.Path(__file__).resolve().parents[2]
PROFILE_SOURCE = (
    ROOT
    / "world_packs/vista_playable_home_r1/visual_profiles/realistic_interior_r2.json"
)


class RendererFixture(RuntimeAcceptanceFixture):
    def __init__(self, root: pathlib.Path) -> None:
        super().__init__(root, runtime_profile=acceptance.R2_RUNTIME_PROFILE)
        contracts = self.workspace / "contracts"
        contracts.mkdir()
        self.profile_path = contracts / build_home.VISUAL_PROFILE_ATTEMPT_FILE
        self.profile_path.write_bytes(PROFILE_SOURCE.read_bytes())
        self.profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        self.profile_sha = renderer.sha256_file(self.profile_path)
        self.compilation = build_home.compile_renderer_profile(
            self.profile["renderer_profile"]
        )

        self.project = self.workspace / "project" / "VistaPlayableHome.uproject"
        self.engine_ini = self.project.parent / "Config" / "DefaultEngine.ini"
        self.engine_ini.parent.mkdir()
        self.engine_ini.write_bytes(b"[ConsoleVariables]\nr.ScreenPercentage=100\n")
        self.request = build_home.build_renderer_request(
            self.profile,
            self.compilation,
            self.engine_ini.read_bytes(),
        )
        self.request_path = contracts / build_home.RENDERER_REQUEST_ATTEMPT_FILE
        self.request_path.write_bytes(build_home.canonical_json(self.request))
        self.request_sha = renderer.sha256_file(self.request_path)

        self.plugin = self.project.parent / "Plugins" / build_home.EXPECTED_PLUGIN_NAME
        self.plugin.mkdir(parents=True)
        (self.plugin / "VistaPlayableHome.uplugin").write_text(
            '{"FriendlyName":"VISTA Playable Home"}\n', encoding="utf-8"
        )
        (self.plugin / "Binaries").mkdir()
        (self.plugin / "Binaries" / "libVistaPlayableHome.so").write_bytes(
            b"fixture-plugin"
        )
        self.plugin_snapshot = build_home.snapshot_tree(self.plugin, "fixture plugin")

        execution = {
            "schema_version": "simworld.vista.playable-home-ue-execution/v1",
            "attempt_root": str(self.workspace),
            "project_file": str(self.project),
            "project_sha256": renderer.sha256_file(self.project),
            "visual_profile_path": str(self.profile_path),
            "visual_profile_sha256": self.profile_sha,
            "visual_profile_content_digest": self.profile["content_digest"],
            "renderer_profile_request": {
                "path": str(self.request_path),
                "sha256": self.request_sha,
                "content_digest": self.request["content_digest"],
                "status": "staged_runtime_observation_required",
                "runtime_proof": False,
            },
        }
        self.execution_path = self.workspace / "execution.json"
        self.execution_path.write_bytes(build_home.canonical_json(execution))
        self.execution_sha = renderer.sha256_file(self.execution_path)

        preparation = {
            "schema_version": "simworld.vista.playable-home-preparation/v1",
            "status": "prepared",
            "attempt_root": str(self.workspace),
            "execution_sha256": self.execution_sha,
            "project_sha256": renderer.sha256_file(self.project),
            "plugin_tree_sha256": self.plugin_snapshot.sha256,
            "visual_profile_sha256": self.profile_sha,
            "visual_profile_content_digest": self.profile["content_digest"],
            "renderer_profile_request_sha256": self.request_sha,
            "renderer_profile_request_content_digest": self.request["content_digest"],
            "renderer_runtime_observation": "pending",
        }
        self.preparation_path = self.workspace / "preparation-receipt.json"
        self.preparation_path.write_bytes(build_home.canonical_json(preparation))

        build = json.loads(self.build_path.read_text(encoding="utf-8"))
        build.update(
            {
                "execution_sha256": self.execution_sha,
                "visual_profile_sha256": self.profile_sha,
                "visual_profile_content_digest": self.profile["content_digest"],
                "renderer_profile_request_sha256": self.request_sha,
                "renderer_profile_request_content_digest": self.request[
                    "content_digest"
                ],
            }
        )
        build["content_digest"] = acceptance._content_digest(build)
        self.build_path.write_bytes(acceptance._canonical_json_bytes(build))
        self.build = build
        self.build_sha = renderer.sha256_file(self.build_path)

        package_attempt = root / "package-linux-development" / "attempt-renderer"
        package_attempt.mkdir(parents=True)
        self.package_path = package_attempt / "package-receipt.json"
        bindings = {
            "source_build_result": str(self.build_path),
            "source_build_result_sha256": self.build_sha,
            "source_commit": self.commit,
            "source_runtime_acceptance": str(
                self.workspace / "runtime-acceptance-r2.json"
            ),
            "source_runtime_acceptance_sha256": "c" * 64,
            "map_path": package.EXPECTED_MAP_PATH,
            "world_revision": package.EXPECTED_REVISION,
            "runtime_profile": package.R2_RUNTIME_PROFILE,
            "camera_profile": package.R2_CAMERA_PROFILE,
            "visual_profile_id": package.R2_RUNTIME_PROFILE,
            **{field: build[field] for field in package.R2_BUILD_DIGEST_FIELDS},
            "accepted_display": package.R2_DISPLAY,
            "accepted_gpu": package.R2_GPU,
            "accepted_vista_world_port": package.R2_VISTA_WORLD_PORT,
            "accepted_width": package.R2_WIDTH,
            "accepted_height": package.R2_HEIGHT,
            "accepted_fps": package.R2_FPS,
            "presentation_bundle_count": package.R2_PRESENTATION_BUNDLE_COUNT,
            "presentation_collision_policy": package.R2_PRESENTATION_COLLISION_POLICY,
        }
        launcher = package_attempt / package.LAUNCHER_RELATIVE
        executable = package_attempt / package.EXECUTABLE_RELATIVE
        pak = package_attempt / package.PAK_DIRECTORY_RELATIVE / "VistaPlayableHome.pak"
        launcher.parent.mkdir(parents=True)
        executable.parent.mkdir(parents=True)
        pak.parent.mkdir(parents=True)
        launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.write_bytes(b"fixture-elf\n")
        pak.write_bytes(b"fixture-pak\n")
        launcher.chmod(0o700)
        executable.chmod(0o700)
        pak.chmod(0o600)
        self.package_pak = pak
        self.mode_0644 = (
            package_attempt
            / "archive/Linux/VistaPlayableHome/Content/Fixtures/mode-0644.bin"
        )
        self.mode_0600 = self.mode_0644.with_name("mode-0600.bin")
        self.mode_0644.parent.mkdir(parents=True)
        self.mode_0644.write_bytes(b"mode fixture 0644\n")
        self.mode_0644.chmod(0o644)
        self.mode_0600.write_bytes(b"mode fixture 0600\n")
        self.mode_0600.chmod(0o600)
        unreal_pak = root / "UE" / "Engine" / "Binaries" / "Linux" / "UnrealPak"
        unreal_pak.parent.mkdir(parents=True, exist_ok=True)
        unreal_pak.write_bytes(b"#!/bin/sh\nexit 0\n")
        unreal_pak.chmod(0o700)
        self.unreal_pak = unreal_pak
        package_project = package_attempt / package.PROJECT_RELATIVE
        package_config = package_attempt / package.PROJECT_CONFIG_RELATIVE
        package_project.parent.mkdir(parents=True)
        package_config.parent.mkdir(parents=True)
        package_project.write_bytes(self.project.read_bytes())
        package_config.write_bytes(self.engine_ini.read_bytes())
        package_project.chmod(0o644)
        package_config.chmod(0o640)
        self.package_project = package_project
        self.package_config = package_config

        def artifact(path: pathlib.Path, *, executable_bit: bool) -> dict[str, object]:
            return {
                "relative_path": path.relative_to(package_attempt).as_posix(),
                "sha256": renderer.sha256_file(path),
                "bytes": path.stat().st_size,
                "executable": executable_bit,
                "mode": stat.S_IMODE(path.stat().st_mode),
            }

        archive_observation = package.inspect_archive(
            package_attempt / "archive/Linux",
            trusted_engine_root=root / "UE",
            exact_modes=True,
        )
        package_value = {
            "schema": package.R2_EXACT_MODE_RECEIPT_SCHEMA,
            "status": "accepted",
            "created_at": "2026-08-16T12:00:00+00:00",
            "attempt_root": str(package_attempt),
            "bindings": bindings,
            "artifacts": {
                "archive_root": str(package_attempt / "archive/Linux"),
                "launcher": artifact(launcher, executable_bit=True),
                "executable": artifact(executable, executable_bit=True),
                "pak": artifact(pak, executable_bit=False),
            },
            "uat": {"status": "fixture"},
            "project_policy": {
                "project_descriptor": str(package_project),
                "project_descriptor_sha256": renderer.sha256_file(package_project),
                "project_config": str(package_config),
                "project_config_sha256": renderer.sha256_file(package_config),
                "enabled_plugins": ["VistaPlayableHome"],
                "disabled_plugins": [
                    "AndroidFileServer",
                    "EditorScriptingUtilities",
                    "Interchange",
                    "PythonScriptPlugin",
                ],
                "host_module": "VistaPlayableHomeHost",
                "android_file_server_enabled": False,
                "mode_policy": packaged_profile.EXACT_MODE_POLICY,
                "project_descriptor_mode": stat.S_IMODE(package_project.stat().st_mode),
                "project_config_mode": stat.S_IMODE(package_config.stat().st_mode),
            },
            "tools": {"status": "fixture"},
            "trusted_upstream": {
                "policy": "engine-root-derived-from-pinned-unrealpak/v1",
                "engine_root": str(root / "UE"),
                "unreal_pak": str(unreal_pak),
                "unreal_pak_sha256": renderer.sha256_file(unreal_pak),
                "mode_policy": packaged_profile.EXACT_MODE_POLICY,
                "unreal_pak_mode": stat.S_IMODE(unreal_pak.stat().st_mode),
            },
            "archive": archive_observation,
            "output": str(self.package_path),
        }
        self.package_path.write_bytes(package.canonical_json(package_value))
        self.package_path.chmod(0o600)
        self.package_sha = renderer.sha256_file(self.package_path)
        package_binding = packaged_profile.validate_package_attempt(
            package_attempt, self.package_sha, verify_archive=False
        )
        nvidia_icd = root / "nvidia_icd.json"
        nvidia_icd.write_text('{"file_format_version":"1.0.0"}\n', encoding="utf-8")
        nvidia_icd.chmod(0o644)
        self.packaged_profile_path = (
            package_attempt / "sunshine-profile-packaged-renderer.json"
        )
        self.packaged_profile_path.write_bytes(
            packaged_profile.canonical_json(
                packaged_profile.profile_from_binding(
                    package_binding,
                    nvidia_icd,
                    runtime_profile=runtime.R2_RUNTIME_PROFILE,
                )
            )
        )
        self.packaged_profile_path.chmod(0o600)
        packaged_inputs = packaged_profile.load_profile(
            self.packaged_profile_path,
            renderer.sha256_file(self.packaged_profile_path),
            verify_archive=False,
        )
        self.workspace_state_path = self.state_path
        package_runtime_attempt = (
            package_attempt / "game-runtime" / self.state_path.parent.name
        )
        package_runtime_attempt.mkdir(parents=True)
        package_runtime_attempt.chmod(0o700)
        launch_plan = packaged_entrypoint.launch_plan(packaged_inputs)
        self.launch_plan_path = package_runtime_attempt / "launch-plan.json"
        self.launch_plan_path.write_bytes(renderer.canonical_json(launch_plan))
        self.launch_plan_path.chmod(0o600)
        original_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        process = {**original_state["process"], "role": "packaged-game"}
        supervisor = {
            **original_state["supervisor"],
            "role": "vista-world-packaged-supervisor",
        }
        packaged_state = {
            "schema": packaged_entrypoint.R2_STATE_SCHEMA,
            "status": "running",
            "created_at": "2026-08-16T12:01:00+00:00",
            "updated_at": "2026-08-16T12:02:00+00:00",
            "mode": packaged_profile.R2_PROFILE_MODE,
            "map": package.EXPECTED_MAP_PATH,
            "world_revision": package.EXPECTED_REVISION,
            "display": runtime.R2_DISPLAY,
            "gpu": runtime.R2_GPU,
            "vista_world_port": runtime.R2_VISTA_WORLD_PORT,
            "profile": str(self.packaged_profile_path),
            "profile_sha256": renderer.sha256_file(self.packaged_profile_path),
            "package_receipt": str(self.package_path),
            "package_receipt_sha256": self.package_sha,
            "archive_tree_sha256": package_value["archive"]["tree_sha256"],
            "executable": str(executable),
            "executable_sha256": renderer.sha256_file(executable),
            "trusted_engine_root": str(root / "UE"),
            "unreal_pak": str(unreal_pak),
            "unreal_pak_sha256": renderer.sha256_file(unreal_pak),
            "nvidia_icd": str(nvidia_icd),
            "nvidia_icd_sha256": renderer.sha256_file(nvidia_icd),
            "process": process,
            "supervisor": supervisor,
            "runtime_profile": runtime.R2_RUNTIME_PROFILE,
            "camera_profile": runtime.R2_CAMERA_PROFILE,
            "width": runtime.R2_WIDTH,
            "height": runtime.R2_HEIGHT,
            "fps": runtime.R2_FPS,
            "launch_plan_sha256": renderer.sha256_file(self.launch_plan_path),
            "readiness": {
                "typed": {
                    "command_id": "vwc-" + "b" * 24,
                    "status": "success",
                    "code": "READY",
                    "world_revision": acceptance.DEFAULT_WORLD_REVISION,
                    "session_generation": 0,
                    "event_status": "inactive",
                    "active_event": None,
                },
                "listener_ownership": {
                    "host": "127.0.0.1",
                    "port": runtime.R2_VISTA_WORLD_PORT,
                    "socket_inode": 12345,
                    "process_group": process["process_group"],
                    "owner_pids": [process["pid"]],
                },
            },
            "archive_reverified_after_readiness": True,
        }
        self.state_path = package_runtime_attempt / "runtime-state.json"
        self.state_path.write_bytes(renderer.canonical_json(packaged_state))
        self.state_path.chmod(0o600)
        self.runtime_log = package_runtime_attempt / renderer.PACKAGED_GAME_LOG_NAME
        self.runtime_log.write_bytes(
            b"LogInit: Display: packaged renderer warmup complete\n"
            b"LogRHI: Display: VULKAN_SM6\n"
        )
        self.runtime_log.chmod(0o600)
        runtime_root = package_attempt / "game-runtime"
        (runtime_root / "current.json").write_bytes(
            renderer.canonical_json(
                {
                    "schema": runtime.RUNTIME_POINTER_SCHEMA,
                    "state": (f"{package_runtime_attempt.name}/runtime-state.json"),
                }
            )
        )
        self.renderer_output = self.state_path.parent / "renderer-acceptance-test.json"
        self.renderer_config = renderer.RendererAcceptanceConfig(
            workspace=self.workspace,
            repo_root=self.repo,
            package_receipt=self.package_path,
            output=self.renderer_output,
            runtime_state_sha256=renderer.sha256_file(self.state_path),
            build_result_sha256=self.build_sha,
            package_receipt_sha256=self.package_sha,
            source_commit=self.commit,
            socket_timeout_s=0.5,
        )
        self.listener_proof_calls = 0

    def response(self, command_id: str) -> dict[str, object]:
        values = {
            item["name"]: item["expected"]
            for item in self.compilation.observation_contract[
                "required_runtime_observations"
            ]
        }
        return {
            "command_id": command_id,
            "status": "success",
            "code": "RENDERER_STATUS_OBSERVED",
            "schema_version": build_home.RENDERER_STATUS_SCHEMA,
            "unreal_engine_version": build_home.PINNED_UNREAL_ENGINE_RUNTIME_VERSION,
            "rhi": values.pop("rhi"),
            "feature_level": values.pop("feature_level"),
            "shader_platform": values.pop("shader_platform"),
            "cvars": values,
        }

    def exchange(self, request, timeout, port):
        assert timeout == 0.5
        assert port == acceptance.R2_VISTA_WORLD_PORT
        assert set(request) == {"type", "params"}
        assert request["type"] == "vista_world_action"
        assert set(request["params"]) == {"operation", "command_id"}
        assert request["params"]["operation"] == "renderer_status"
        response = self.response(request["params"]["command_id"])
        return renderer.canonical_json(response), response

    def listener_prover(self, port, process_group):
        self.listener_proof_calls += 1
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        proof = state["readiness"]["listener_ownership"]
        assert port == proof["port"]
        assert process_group == proof["process_group"]
        return copy.deepcopy(proof)


def test_full_renderer_observation_is_only_observed_acceptance(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)

    receipt, receipt_sha = renderer.execute_acceptance(
        fixture.renderer_config,
        exchange=fixture.exchange,
        listener_prover=fixture.listener_prover,
    )

    assert receipt["status"] == "accepted"
    assert receipt["schema"].endswith("/v3")
    assert receipt["renderer_runtime_observation"] == "observed_accepted"
    assert receipt["bindings"]["build"]["pending_renderer_observation"] is True
    assert receipt["bindings"]["renderer_request"]["runtime_proof_in_request"] is False
    assert receipt["bindings"]["plugin"]["tree_sha256"] == (
        fixture.plugin_snapshot.sha256
    )
    assert receipt["bindings"]["package"]["sha256"] == fixture.package_sha
    assert receipt["bindings"]["runtime"]["process"]["pid"] > 0
    assert receipt["bindings"]["runtime"]["listener_owner_closure_scope"] == (
        "single-loopback-inode+exact-managed-pid+visible-foreign-rejection/v1"
    )
    assert receipt["bindings"]["runtime"]["listener_expected_effective_uid"] >= 0
    assert receipt["bindings"]["runtime"]["listener_exact_packaged_game_identity"] == {
        key: receipt["bindings"]["runtime"]["process"][key]
        for key in ("pid", "start_ticks", "process_group")
    }
    assert receipt["bindings"]["runtime"]["profile_path"] == str(
        fixture.packaged_profile_path
    )
    assert receipt["bindings"]["runtime"]["executable_sha256"] == (
        renderer.sha256_file(fixture.package_path.parent / package.EXECUTABLE_RELATIVE)
    )
    assert (
        receipt["bindings"]["runtime"]["listener_before"]
        == (receipt["bindings"]["runtime"]["listener_after"])
    )
    assert fixture.listener_proof_calls == 2
    log_proof = receipt["bindings"]["runtime"]["packaged_game_log"]
    assert log_proof == {
        "path": str(fixture.runtime_log),
        "observed_prefix_sha256": renderer.sha256_file(fixture.runtime_log),
        "observed_prefix_bytes": fixture.runtime_log.stat().st_size,
        "mode": 0o600,
        "owner_uid": fixture.runtime_log.stat().st_uid,
        "owner_gid": fixture.runtime_log.stat().st_gid,
        "device": fixture.runtime_log.stat().st_dev,
        "inode": fixture.runtime_log.stat().st_ino,
        "nlink": 1,
        "gate_policy": renderer.RUNTIME_LOG_GATE_POLICY,
        "observed_after_renderer_status": True,
        "prohibited_patterns": list(renderer.PROHIBITED_RUNTIME_LOG_PATTERNS),
        "prohibited_pattern_matches": [],
    }
    filesystem_proof = receipt["bindings"]["runtime"][
        "attempt_filesystem_identity"
    ]
    assert filesystem_proof["policy"] == renderer.RUNTIME_ATTEMPT_IDENTITY_POLICY
    assert filesystem_proof["process_effective_uid_is_independent"] is True
    assert filesystem_proof["owner_uid_gid_consistent"] is True
    assert filesystem_proof["directory"]["mode"] == 0o700
    for name in ("state", "launch_plan", "packaged_game_log"):
        member = filesystem_proof[name]
        assert member["mode"] == 0o600
        assert member["nlink"] == 1
        assert member["owner_uid"] == filesystem_proof["directory"]["owner_uid"]
        assert member["owner_gid"] == filesystem_proof["directory"]["owner_gid"]
    byte_verification = receipt["bindings"]["package"]["byte_verification"]
    assert byte_verification["exact_match"] is True
    assert byte_verification["before_exchange"] == byte_verification["after_exchange"]
    package_proof = byte_verification["before_exchange"]
    package_receipt_value = json.loads(fixture.package_path.read_text(encoding="utf-8"))
    assert (
        package_proof["archive"]["tree_sha256"]
        == (package_receipt_value["archive"]["tree_sha256"])
    )
    assert package_proof["schema"].endswith("/v2")
    assert package_proof["archive"]["schema"] == (package.ARCHIVE_SCHEMA_EXACT_MODE_V2)
    assert package_proof["archive"]["algorithm"] == (
        package.ARCHIVE_ALGORITHM_EXACT_MODE_V2
    )
    assert package_proof["archive"]["entry_set_and_exact_modes_verified"] is True
    assert len(package_proof["archive"]["entry_set_sha256"]) == 64
    for artifact_name in (
        "package_receipt",
        "launcher",
        "executable",
        "pak",
        "unreal_pak",
        "project_descriptor",
        "project_config",
        "packaged_profile",
        "nvidia_icd",
    ):
        artifact = package_proof[artifact_name]
        assert len(artifact["sha256"]) == 64
        assert isinstance(artifact["mode"], int)
    assert receipt["protocol"]["one_request_one_eof_response"] is True
    assert receipt["evaluation"]["runtime_proof"] is True
    raw = fixture.renderer_output.read_bytes()
    assert renderer.sha256_bytes(raw) == receipt_sha
    assert renderer.strict_json_bytes(raw, label="receipt") == receipt
    with pytest.raises(
        renderer.RendererAcceptanceError, match="already exists"
    ) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=fixture.exchange,
            listener_prover=fixture.listener_prover,
        )
    assert caught.value.code == "RECEIPT_EXISTS"


@pytest.mark.parametrize("pattern", renderer.PROHIBITED_RUNTIME_LOG_PATTERNS)
def test_renderer_rejects_packaged_log_material_and_nanite_degradation(
    tmp_path: pathlib.Path, pattern: str
) -> None:
    fixture = RendererFixture(tmp_path)
    fixture.runtime_log.write_text(
        f"LogInit: warmup complete\nLogRenderer: Warning: {pattern}\n",
        encoding="utf-8",
    )
    fixture.runtime_log.chmod(0o600)

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=fixture.exchange,
            listener_prover=fixture.listener_prover,
        )

    assert caught.value.code == "RENDERER_LOG_REJECTED"
    assert pattern in str(caught.value)
    assert not fixture.renderer_output.exists()


def test_renderer_requires_private_attempt_local_packaged_log(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    fixture.runtime_log.chmod(0o644)

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=fixture.exchange,
            listener_prover=fixture.listener_prover,
        )

    assert caught.value.code == "RUNTIME_LOG_IDENTITY_INVALID"
    assert not fixture.renderer_output.exists()


def test_renderer_accepts_mapped_nas_owner_independent_of_process_euid(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = RendererFixture(tmp_path)
    filesystem_uid = fixture.runtime_log.stat().st_uid
    mapped_process_uid = filesystem_uid + 900_000_000
    monkeypatch.setattr(
        renderer.packaged_smoke,
        "process_effective_uid",
        lambda _pid: mapped_process_uid,
    )

    receipt, _receipt_sha = renderer.execute_acceptance(
        fixture.renderer_config,
        exchange=fixture.exchange,
        listener_prover=fixture.listener_prover,
    )

    runtime_proof = receipt["bindings"]["runtime"]
    assert runtime_proof["listener_expected_effective_uid"] == mapped_process_uid
    assert runtime_proof["packaged_game_log"]["owner_uid"] == filesystem_uid
    assert (
        runtime_proof["attempt_filesystem_identity"]["directory"]["owner_uid"]
        == filesystem_uid
    )
    assert mapped_process_uid != filesystem_uid


def test_renderer_rejects_inconsistent_attempt_local_owner(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = RendererFixture(tmp_path)
    original = renderer._seal_runtime_attempt_child

    def inconsistent_owner(directory_descriptor, directory, name, label):
        identity = original(directory_descriptor, directory, name, label)
        if name == renderer.PACKAGED_GAME_LOG_NAME:
            return dataclasses.replace(identity, owner_uid=identity.owner_uid + 1)
        return identity

    monkeypatch.setattr(
        renderer,
        "_seal_runtime_attempt_child",
        inconsistent_owner,
    )

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.validate_inputs(fixture.renderer_config)

    assert caught.value.code == "RUNTIME_ATTEMPT_OWNER_INVALID"
    assert not fixture.renderer_output.exists()


def test_renderer_rejects_non_private_runtime_attempt_parent(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    fixture.runtime_log.parent.chmod(0o750)

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.validate_inputs(fixture.renderer_config)

    assert caught.value.code == "RUNTIME_ATTEMPT_IDENTITY_INVALID"
    assert not fixture.renderer_output.exists()


def test_renderer_rejects_hard_linked_runtime_state(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    fixture.state_path.with_name("runtime-state-alias.json").hardlink_to(
        fixture.state_path
    )

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.validate_inputs(fixture.renderer_config)

    assert caught.value.code == "RUNTIME_ATTEMPT_IDENTITY_INVALID"
    assert not fixture.renderer_output.exists()


def test_renderer_receipt_mode_is_deterministic_under_restrictive_umask(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    inputs = renderer.validate_inputs(fixture.renderer_config)

    previous_umask = os.umask(0o777)
    try:
        descriptor = renderer._reserve_output(inputs)
    finally:
        os.umask(previous_umask)
    os.close(descriptor)

    assert stat.S_IMODE(fixture.renderer_output.stat().st_mode) == 0o600


def test_renderer_receipt_mode_failure_removes_reserved_output(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = RendererFixture(tmp_path)
    inputs = renderer.validate_inputs(fixture.renderer_config)

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError("fixture fchmod failure")

    monkeypatch.setattr(renderer.os, "fchmod", fail_fchmod)
    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer._reserve_output(inputs)

    assert caught.value.code == "RECEIPT_OPEN_FAILED"
    assert not fixture.renderer_output.exists()


def test_renderer_fdopen_close_then_raise_preserves_error_and_removes_output(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = RendererFixture(tmp_path)
    original_fdopen = os.fdopen

    def close_then_raise(descriptor, mode="r", *args, **kwargs):
        if mode == "wb":
            os.close(descriptor)
            raise RuntimeError("fixture fdopen failure")
        return original_fdopen(descriptor, mode, *args, **kwargs)

    monkeypatch.setattr(renderer.os, "fdopen", close_then_raise)
    with pytest.raises(RuntimeError, match="fixture fdopen failure"):
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=fixture.exchange,
            listener_prover=fixture.listener_prover,
        )

    assert not fixture.renderer_output.exists()


def test_renderer_response_rejects_spoof_missing_wrong_stale_and_unknown(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    inputs = renderer.validate_inputs(fixture.renderer_config)
    command_id = "vwc-" + "1" * 24
    valid = fixture.response(command_id)
    assert (
        renderer.validate_renderer_response(inputs, valid, command_id=command_id)[
            "runtime_proof"
        ]
        is True
    )

    mutations = []
    spoofed = copy.deepcopy(valid)
    spoofed["requested"] = fixture.request["renderer_profile"]
    mutations.append(spoofed)
    unknown_schema = copy.deepcopy(valid)
    unknown_schema["schema_version"] = "simworld.unknown/v9"
    mutations.append(unknown_schema)
    missing = copy.deepcopy(valid)
    del missing["cvars"]["r.ReflectionMethod"]
    mutations.append(missing)
    wrong = copy.deepcopy(valid)
    wrong["cvars"]["r.ReflectionMethod"] = 0
    mutations.append(wrong)
    stale = copy.deepcopy(valid)
    stale["command_id"] = "vwc-" + "2" * 24
    mutations.append(stale)
    boolean = copy.deepcopy(valid)
    boolean["cvars"]["r.Nanite"] = True
    mutations.append(boolean)

    for response in mutations:
        with pytest.raises(renderer.RendererAcceptanceError):
            renderer.validate_renderer_response(inputs, response, command_id=command_id)


@pytest.mark.parametrize(
    "version",
    (
        "5.7.2-50162420+++UE5+Release-5.7",
        "5.7.4-50162420+++UE5+Release-5.7",
    ),
)
def test_renderer_response_rejects_adjacent_engine_patch_versions(
    tmp_path: pathlib.Path, version: str
) -> None:
    fixture = RendererFixture(tmp_path)
    inputs = renderer.validate_inputs(fixture.renderer_config)
    command_id = "vwc-" + "3" * 24
    response = fixture.response(command_id)
    response["unreal_engine_version"] = version
    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.validate_renderer_response(inputs, response, command_id=command_id)
    assert caught.value.code == "RESPONSE_SCHEMA_INVALID"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1}\n{"x":2}',
    ],
)
def test_strict_wire_parser_rejects_duplicate_nan_and_multiple_responses(
    raw: bytes,
) -> None:
    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.strict_json_bytes(raw, label="renderer response")
    assert caught.value.code == "JSON_INVALID"


def test_stale_renderer_input_is_detected_after_response(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)

    def drifting_exchange(request, timeout, port):
        raw, response = fixture.exchange(request, timeout, port)
        fixture.request_path.write_bytes(raw)
        return raw, response

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=drifting_exchange,
            listener_prover=fixture.listener_prover,
        )
    assert caught.value.code == "EVIDENCE_CHANGED"
    assert not fixture.renderer_output.exists()


def test_same_length_packaged_elf_mutation_after_response_has_no_receipt(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    executable = fixture.package_path.parent / package.EXECUTABLE_RELATIVE
    original = executable.read_bytes()
    mutated = bytes([original[0] ^ 0x01]) + original[1:]
    assert len(mutated) == len(original)

    def mutating_exchange(request, timeout, port):
        raw, response = fixture.exchange(request, timeout, port)
        executable.write_bytes(mutated)
        executable.chmod(0o700)
        return raw, response

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=mutating_exchange,
            listener_prover=fixture.listener_prover,
        )
    assert caught.value.code == "PACKAGE_BYTES_CHANGED"
    assert not fixture.renderer_output.exists()


def test_packaged_executable_mode_mutation_after_response_has_no_receipt(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    executable = fixture.package_path.parent / package.EXECUTABLE_RELATIVE

    def mutating_exchange(request, timeout, port):
        raw, response = fixture.exchange(request, timeout, port)
        executable.chmod(0o711)
        return raw, response

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=mutating_exchange,
            listener_prover=fixture.listener_prover,
        )
    assert caught.value.code == "PACKAGE_BYTES_CHANGED"
    assert not fixture.renderer_output.exists()


@pytest.mark.parametrize(
    ("path_name", "changed_mode"),
    (
        ("mode_0644", 0o600),
        ("mode_0600", 0o640),
    ),
)
def test_non_named_archive_mode_mutation_after_response_has_no_receipt(
    tmp_path: pathlib.Path,
    path_name: str,
    changed_mode: int,
) -> None:
    fixture = RendererFixture(tmp_path)
    target = getattr(fixture, path_name)

    def mutating_exchange(request, timeout, port):
        raw, response = fixture.exchange(request, timeout, port)
        target.chmod(changed_mode)
        return raw, response

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=mutating_exchange,
            listener_prover=fixture.listener_prover,
        )
    assert caught.value.code == "PACKAGE_BYTES_CHANGED"
    assert not fixture.renderer_output.exists()


def test_package_config_mode_drift_before_exchange_has_no_receipt(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    fixture.package_config.chmod(0o600)

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=fixture.exchange,
            listener_prover=fixture.listener_prover,
        )
    assert caught.value.code == "PACKAGE_PROJECT_IDENTITY_INVALID"
    assert not fixture.renderer_output.exists()


def test_package_config_mode_mutation_after_response_has_no_receipt(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)

    def mutating_exchange(request, timeout, port):
        raw, response = fixture.exchange(request, timeout, port)
        fixture.package_config.chmod(0o600)
        return raw, response

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=mutating_exchange,
            listener_prover=fixture.listener_prover,
        )
    assert caught.value.code == "EVIDENCE_CHANGED"
    assert not fixture.renderer_output.exists()


def test_renderer_rejects_legacy_r2_v2_package_receipt(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    package_value = json.loads(fixture.package_path.read_text(encoding="utf-8"))
    package_value["schema"] = package.R2_RECEIPT_SCHEMA
    fixture.package_path.write_bytes(package.canonical_json(package_value))
    fixture.package_path.chmod(0o600)
    config = renderer.RendererAcceptanceConfig(
        **{
            **fixture.renderer_config.__dict__,
            "package_receipt_sha256": renderer.sha256_file(fixture.package_path),
        }
    )

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.validate_inputs(config)
    assert caught.value.code == "RENDERER_PACKAGE_INVALID"


def test_renderer_rejects_legacy_r2_v2_packaged_profile(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    profile = json.loads(fixture.packaged_profile_path.read_text(encoding="utf-8"))
    profile["schema"] = packaged_profile.R2_PROFILE_SCHEMA
    for name in (
        "archive_schema",
        "archive_algorithm",
        "mode_policy",
        "package_receipt_mode",
        "profile_file_mode",
        "nvidia_icd_mode",
    ):
        profile.pop(name)
    fixture.packaged_profile_path.write_bytes(packaged_profile.canonical_json(profile))
    fixture.packaged_profile_path.chmod(0o600)
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    state["profile_sha256"] = renderer.sha256_file(fixture.packaged_profile_path)
    fixture.state_path.write_bytes(renderer.canonical_json(state))
    config = renderer.RendererAcceptanceConfig(
        **{
            **fixture.renderer_config.__dict__,
            "runtime_state_sha256": renderer.sha256_file(fixture.state_path),
        }
    )

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.validate_inputs(config)
    assert caught.value.code == "PACKAGED_PROFILE_INVALID"


def test_renderer_exchange_rejects_spoof_listener_owner(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    exchange_called = False

    def forbidden_exchange(request, timeout, port):
        nonlocal exchange_called
        exchange_called = True
        return fixture.exchange(request, timeout, port)

    def spoof_listener(port, process_group):
        proof = fixture.listener_prover(port, process_group)
        proof["socket_inode"] += 1
        return proof

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=forbidden_exchange,
            listener_prover=spoof_listener,
        )
    assert caught.value.code == "LISTENER_OWNERSHIP_CHANGED"
    assert exchange_called is False
    assert not fixture.renderer_output.exists()


def test_renderer_exchange_binds_parsed_response_to_wire_bytes(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)

    def mismatched_exchange(request, timeout, port):
        raw, response = fixture.exchange(request, timeout, port)
        response["rhi"] = "OpenGL"
        return raw, response

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.execute_acceptance(
            fixture.renderer_config,
            exchange=mismatched_exchange,
            listener_prover=fixture.listener_prover,
        )
    assert caught.value.code == "RESPONSE_BYTES_MISMATCH"
    assert not fixture.renderer_output.exists()


def test_editor_workspace_runtime_state_pin_is_rejected(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    config = renderer.RendererAcceptanceConfig(
        **{
            **fixture.renderer_config.__dict__,
            "runtime_state_sha256": renderer.sha256_file(fixture.workspace_state_path),
        }
    )

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.validate_inputs(config)
    assert caught.value.code == "EVIDENCE_PIN_MISMATCH"


def test_packaged_runtime_state_cannot_claim_unverified_archive(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    state["archive_reverified_after_readiness"] = False
    fixture.state_path.write_bytes(renderer.canonical_json(state))
    config = renderer.RendererAcceptanceConfig(
        **{
            **fixture.renderer_config.__dict__,
            "runtime_state_sha256": renderer.sha256_file(fixture.state_path),
        }
    )

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.validate_inputs(config)
    assert caught.value.code == "RUNTIME_STATE_INVALID"


def test_package_projection_rejects_renderer_digest_drift(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RendererFixture(tmp_path)
    package_value = json.loads(fixture.package_path.read_text(encoding="utf-8"))
    package_value["bindings"]["renderer_profile_request_sha256"] = "f" * 64
    fixture.package_path.write_bytes(package.canonical_json(package_value))
    config = types.SimpleNamespace(**fixture.renderer_config.__dict__)
    config.package_receipt_sha256 = renderer.sha256_file(fixture.package_path)

    with pytest.raises(renderer.RendererAcceptanceError) as caught:
        renderer.validate_inputs(renderer.RendererAcceptanceConfig(**vars(config)))
    assert caught.value.code == "RENDERER_PACKAGE_INVALID"


def test_runtime_binding_additively_accepts_complete_external_pending_proof(
    tmp_path: pathlib.Path,
) -> None:
    fixture = RuntimeAcceptanceFixture(
        tmp_path, runtime_profile=acceptance.R2_RUNTIME_PROFILE
    )
    build = json.loads(fixture.build_path.read_text(encoding="utf-8"))
    build.update(
        {
            "presentation_external_content_verified": True,
            "presentation_external_nanite_policy": (
                acceptance.R2_EXTERNAL_NANITE_POLICY
            ),
            "presentation_external_nanite_disabled_verified": True,
        }
    )
    build["content_digest"] = acceptance._content_digest(build)
    fixture.build_path.write_bytes(acceptance._canonical_json_bytes(build))
    config = acceptance.AcceptanceConfig(
        **{
            **fixture.config.__dict__,
            "build_result_sha256": acceptance.sha256_file(fixture.build_path),
        }
    )

    binding = acceptance.validate_binding(config)

    assert binding.runtime_profile == acceptance.R2_RUNTIME_PROFILE


def test_unreal_adapter_exposes_read_only_observed_renderer_surface() -> None:
    header = (
        ROOT / "unreal_plugins/VistaPlayableHome/Source/VistaPlayableHome/Public/"
        "VistaPlayableHomeRuntimeSubsystem.h"
    ).read_text(encoding="utf-8")
    subsystem = (
        ROOT / "unreal_plugins/VistaPlayableHome/Source/VistaPlayableHome/Private/"
        "VistaPlayableHomeRuntimeSubsystem.cpp"
    ).read_text(encoding="utf-8")
    adapter = (
        ROOT / "unreal_plugins/VistaPlayableHome/Source/VistaPlayableHome/Private/"
        "VistaWorldTcpAdapter.cpp"
    ).read_text(encoding="utf-8")
    build_rules = (
        ROOT / "unreal_plugins/VistaPlayableHome/Source/VistaPlayableHome/"
        "VistaPlayableHome.Build.cs"
    ).read_text(encoding="utf-8")
    assert "GetRendererStatus" in header
    assert "renderer_status" in adapter
    assert "simworld.vista.playable-home-renderer-status/v1" in adapter
    for observed_source in (
        "FEngineVersion::Current()",
        "GDynamicRHI",
        "GMaxRHIFeatureLevel",
        "GMaxRHIShaderPlatform",
        "FindConsoleVariable",
    ):
        assert observed_source in subsystem
    profile = json.loads(PROFILE_SOURCE.read_text(encoding="utf-8"))
    compilation = build_home.compile_renderer_profile(profile["renderer_profile"])
    for requirement in compilation.observation_contract[
        "required_runtime_observations"
    ]:
        if requirement["source"] == "cvar":
            assert f'TEXT("{requirement["name"]}")' in subsystem
    assert 'TEXT("r.UsePreExposure")' not in subsystem
    assert '"RHI"' in build_rules
    renderer_branch = adapter.split('Operation == TEXT("renderer_status")', 1)[1]
    assert (
        "ExecuteInteraction"
        not in renderer_branch.split('Operation == TEXT("interaction")', 1)[0]
    )
