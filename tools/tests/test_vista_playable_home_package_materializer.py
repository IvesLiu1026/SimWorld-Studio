from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import pathlib
import stat

import pytest

from tools.ue.vista_playable_home import materialize_package_project as package


def _write(path: pathlib.Path, raw: bytes, mode: int = 0o640) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def _write_canonical(path: pathlib.Path, value: dict) -> pathlib.Path:
    return _write(path, package.canonical_json(value) + b"\n")


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _token_key() -> str:
    # Keep the credential-shaped fixture out of the committed source while
    # still constructing the exact UE-generated key in the temporary tree.
    return "Security" + "Token"


def _engine_config(*, include_nav: bool = True) -> bytes:
    lines = [
        "[/Script/EngineSettings.GameMapsSettings]",
        f"GameDefaultMap={package.EXPECTED_MAP_PATH}",
        "GlobalDefaultGameMode=/Script/VistaPlayableHome.VistaPlayableHomeGameMode",
        "",
        "[/Script/NavigationSystem.RecastNavMesh]",
    ]
    if include_nav:
        lines.append("RuntimeGeneration=Dynamic")
    lines.extend(
        [
            "",
            "[/Script/Engine.RendererSettings]",
            "r.AllowStaticLighting=False",
            "",
            "[/Script/AndroidFileServerEditor.AndroidFileServerRuntimeSettings]",
            f"{_token_key()}=source-fixture-value-that-must-not-escape",
            "bEnablePlugin=True",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


DEFAULT_INPUT = b"""[/Script/Engine.InputSettings]
+AxisMappings=(AxisName="MoveForward",Scale=1.0,Key=W)
+AxisMappings=(AxisName="MoveForward",Scale=-1.0,Key=S)
+AxisMappings=(AxisName="MoveRight",Scale=1.0,Key=D)
+AxisMappings=(AxisName="MoveRight",Scale=-1.0,Key=A)
+AxisMappings=(AxisName="Turn",Scale=1.0,Key=MouseX)
+AxisMappings=(AxisName="LookUp",Scale=-1.0,Key=MouseY)
+ActionMappings=(ActionName="Jump",Key=SpaceBar)
+ActionMappings=(ActionName="Sprint",Key=LeftShift)
+ActionMappings=(ActionName="Crouch",Key=C)
+ActionMappings=(ActionName="Interact",Key=E)
+ActionMappings=(ActionName="Drop",Key=Q)
"""


class Fixture:
    def __init__(
        self,
        root: pathlib.Path,
        *,
        include_required_nav: bool = True,
        historical_input_sha256: str = "a" * 64,
    ) -> None:
        self.root = root.resolve()
        self.source_attempt = self.root / "accepted-run" / "ue" / "attempt-source"
        self.project = self.source_attempt / "project"
        self.package_parent = self.root / package.EXPECTED_PARENT_NAME
        self.package_parent.mkdir(parents=True)

        descriptor = {
            "FileVersion": 3,
            "Plugins": [{"Enabled": True, "Name": package.EXPECTED_PLUGIN_NAME}],
        }
        self.project_descriptor = _write_canonical(
            self.project / package.EXPECTED_PROJECT_NAME,
            descriptor,
        )
        self.source_engine = _write(
            self.project / "Config/DefaultEngine.ini",
            _engine_config(include_nav=include_required_nav),
        )
        self.source_input = _write(
            self.project / "Config/DefaultInput.ini",
            DEFAULT_INPUT,
        )
        self.map_asset = _write(
            self.project / "Content/VISTA/PlayableHome/vista_playable_home_r1/Maps/"
            "VistaPlayableHome.umap",
            b"synthetic accepted map\n",
        )
        _write(
            self.project / "Content/VISTA/PlayableHome/Props/Chair.uasset",
            b"synthetic accepted chair\n",
        )

        plugin = self.project / "Plugins" / package.EXPECTED_PLUGIN_NAME
        plugin_descriptor = {
            "FileVersion": 3,
            "FriendlyName": "VISTA Playable Home fixture",
            "Modules": [
                {
                    "LoadingPhase": "Default",
                    "Name": package.EXPECTED_PLUGIN_NAME,
                    "Type": "Runtime",
                }
            ],
        }
        # A normal UE plugin descriptor is pretty-printed, not canonical.
        _write(
            plugin / "VistaPlayableHome.uplugin",
            (json.dumps(plugin_descriptor, indent=2) + "\n").encode("utf-8"),
        )
        _write(
            plugin / "Config/DefaultVistaPlayableHome.ini",
            b"[VistaPlayableHome]\nEnabled=True\n",
        )
        self.plugin_source = _write(
            plugin / "Source/VistaPlayableHome/Private/VistaPlayableHome.cpp",
            b'#include "Modules/ModuleManager.h"\n',
        )
        _write(
            plugin / "Source/VistaPlayableHome/Public/VistaPlayableHome.h",
            b"#pragma once\n",
        )
        _write(
            plugin / "Source/VistaPlayableHome/VistaPlayableHome.Build.cs",
            b"public class VistaPlayableHome {}\n",
        )

        # These trees are deliberately present and must never enter the target.
        self.plugin_binary = _write(
            plugin / "Binaries/Linux/libUnrealEditor-VistaPlayableHome.so",
            b"editor-only binary\n",
        )
        _write(plugin / "Intermediate/Build/cache.bin", b"plugin intermediate\n")
        _write(self.project / "Binaries/Linux/editor.bin", b"root binary\n")
        _write(self.project / "Intermediate/root.bin", b"root intermediate\n")
        _write(self.project / "Saved/Logs/editor.log", b"root saved\n")
        _write(self.project / "DerivedDataCache/cache.bin", b"root ddc\n")

        self.scene_path = self.source_attempt / "scene-receipt.json"
        self.execution_path = self.source_attempt / "execution.json"
        execution = {
            "attempt_root": str(self.source_attempt),
            "project_file": str(self.project_descriptor),
            "project_sha256": _sha256(self.project_descriptor),
            "scene_receipt": str(self.scene_path),
        }
        _write_canonical(self.execution_path, execution)
        execution_sha = _sha256(self.execution_path)

        scene = {
            "bindings": {
                "execution_manifest": str(self.execution_path),
                "execution_manifest_sha256": execution_sha,
                "input_config": str(self.source_input),
                "input_config_sha256": historical_input_sha256,
                "project": str(self.project_descriptor),
            },
            "gates": {
                "game_mode_configured": True,
                "input_mappings_verified": True,
                "map_reloaded": True,
                "map_saved": True,
                "navmesh_bounds_verified": True,
                "quarantined": False,
            },
            "schema_version": package.SOURCE_SCENE_SCHEMA,
            "status": "saved_reloaded_candidate",
        }
        _write_canonical(self.scene_path, scene)

        result = {
            "attempt_root": str(self.source_attempt),
            "execution_sha256": execution_sha,
            "map_path": package.EXPECTED_MAP_PATH,
            "revision": package.EXPECTED_REVISION,
            "scene_receipt_sha256": _sha256(self.scene_path),
            "schema_version": package.SOURCE_RESULT_SCHEMA,
            "status": "accepted_candidate",
        }
        result["content_digest"] = package._source_content_digest(result)
        self.result_path = _write_canonical(
            self.source_attempt / "result-receipt.json",
            result,
        )

        self.project_tree_sha256 = package.source_project_tree_sha256(self.project)
        self.result_sha256 = _sha256(self.result_path)

    def config(self, name: str = "attempt-test") -> package.MaterializationConfig:
        return package.MaterializationConfig(
            source_build_result=self.result_path,
            source_build_result_sha256=self.result_sha256,
            source_project=self.project,
            source_project_tree_sha256=self.project_tree_sha256,
            attempt_root=self.package_parent / name,
        )

    def source_files(self) -> dict[str, tuple[str, int]]:
        observed: dict[str, tuple[str, int]] = {}
        for path in sorted(self.source_attempt.rglob("*")):
            if path.is_file() and not path.is_symlink():
                observed[path.relative_to(self.source_attempt).as_posix()] = (
                    _sha256(path),
                    stat.S_IMODE(path.stat().st_mode),
                )
        return observed


def _read_receipt(attempt: pathlib.Path) -> tuple[dict, bytes]:
    raw = (attempt / package.MATERIALIZATION_RECEIPT).read_bytes()
    return json.loads(raw), raw


def _assert_private_tree(attempt: pathlib.Path) -> None:
    assert stat.S_IMODE(attempt.stat().st_mode) == package.PRIVATE_DIRECTORY_MODE
    for root, directories, files in os.walk(attempt):
        root_path = pathlib.Path(root)
        for name in directories:
            assert stat.S_IMODE((root_path / name).stat().st_mode) == 0o700
        for name in files:
            assert stat.S_IMODE((root_path / name).stat().st_mode) == 0o600


def test_dry_run_is_deterministic_zero_write_and_token_free(
    tmp_path: pathlib.Path,
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-dry-run")
    before = fixture.source_files()

    first = package.plan_materialization(config)
    second = package.plan_materialization(config)
    raw = package.canonical_json(first.report)

    assert first.report == second.report
    assert first.output.tree_sha256 == second.output.tree_sha256
    assert first.report["mode"] == "dry_run"
    assert not config.attempt_root.exists()
    assert fixture.source_files() == before
    assert _token_key().encode("utf-8") not in raw
    assert b"source-fixture-value-that-must-not-escape" not in raw
    assert first.report["source"]["source_default_engine"] == {
        "bytes": fixture.source_engine.stat().st_size,
        "sanitized_policy": package.SOURCE_SANITIZATION_POLICY,
        "sha256": _sha256(fixture.source_engine),
    }
    assert first.report["policy"]["default_engine"] == (
        "canonical_allowlisted_regeneration/v1"
    )


def test_cli_defaults_to_dry_run_without_creating_attempt(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-cli-dry-run")

    assert (
        package.main(
            [
                "--source-build-result",
                str(config.source_build_result),
                "--source-build-result-sha256",
                config.source_build_result_sha256,
                "--source-project",
                str(config.source_project),
                "--source-project-tree-sha256",
                config.source_project_tree_sha256,
                "--attempt-root",
                str(config.attempt_root),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry_run"
    assert not config.attempt_root.exists()


def test_apply_materializes_exact_private_project_and_append_only_receipt(
    tmp_path: pathlib.Path,
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-success")
    before = fixture.source_files()
    plan = package.plan_materialization(config, apply=True)

    receipt, receipt_sha = package.apply_materialization(plan)
    receipt_from_disk, receipt_raw = _read_receipt(config.attempt_root)
    project = config.attempt_root / "project"

    assert receipt == receipt_from_disk
    assert receipt_raw == package.canonical_json(receipt)
    assert hashlib.sha256(receipt_raw).hexdigest() == receipt_sha
    assert receipt["status"] == "accepted"
    assert receipt["content_digest"] == package._content_digest(receipt)
    assert receipt["project"]["tree_sha256"] == plan.output.tree_sha256
    assert receipt["project"] == plan.output.receipt_record()
    assert (
        receipt["source"]["source_default_engine"]
        == (plan.report["source"]["source_default_engine"])
    )
    assert fixture.source_files() == before

    assert (project / package.EXPECTED_PROJECT_NAME).read_bytes() == (
        package._canonical_project_descriptor()
    )
    assert (project / "Config/DefaultEngine.ini").read_bytes() == (
        package._canonical_engine_ini()
    )
    assert (project / "Config/DefaultInput.ini").read_bytes() == DEFAULT_INPUT
    assert (project / fixture.map_asset.relative_to(fixture.project)).read_bytes() == (
        fixture.map_asset.read_bytes()
    )
    assert (
        project / fixture.plugin_source.relative_to(fixture.project)
    ).read_bytes() == fixture.plugin_source.read_bytes()
    assert not (project / "Binaries").exists()
    assert not (project / "Intermediate").exists()
    assert not (project / "Saved").exists()
    assert not (project / "DerivedDataCache").exists()
    assert not (project / "Plugins/VistaPlayableHome/Binaries").exists()
    assert not (project / "Plugins/VistaPlayableHome/Intermediate").exists()
    assert _token_key().encode("utf-8") not in receipt_raw
    assert (
        _token_key().encode("utf-8")
        not in (project / "Config/DefaultEngine.ini").read_bytes()
    )
    _assert_private_tree(config.attempt_root)

    original_receipt = receipt_raw
    with pytest.raises(package.PackageProjectError) as caught:
        package.apply_materialization(plan)
    assert caught.value.code == "DESTINATION_EXISTS"
    assert (config.attempt_root / package.MATERIALIZATION_RECEIPT).read_bytes() == (
        original_receipt
    )


def test_apply_overrides_hostile_umask_with_private_modes(
    tmp_path: pathlib.Path,
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-umask")
    plan = package.plan_materialization(config, apply=True)

    previous = os.umask(0o777)
    try:
        package.apply_materialization(plan)
    finally:
        os.umask(previous)

    _assert_private_tree(config.attempt_root)


def test_apply_requires_an_apply_mode_plan(tmp_path: pathlib.Path) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-wrong-plan-mode")
    plan = package.plan_materialization(config)

    with pytest.raises(package.PackageProjectError) as caught:
        package.apply_materialization(plan)

    assert caught.value.code == "APPLY_PLAN_REQUIRED"
    assert not config.attempt_root.exists()


@pytest.mark.parametrize(
    ("result_pin", "tree_pin"),
    [("0" * 64, None), (None, "0" * 64)],
)
def test_source_pins_are_mandatory_and_fail_closed(
    tmp_path: pathlib.Path,
    result_pin: str | None,
    tree_pin: str | None,
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-bad-pin")
    config = dataclasses.replace(
        config,
        source_build_result_sha256=result_pin or config.source_build_result_sha256,
        source_project_tree_sha256=tree_pin or config.source_project_tree_sha256,
    )

    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(config, apply=True)

    assert caught.value.code == "SOURCE_PIN_MISMATCH"
    assert not config.attempt_root.exists()


@pytest.mark.parametrize("name", ["attempt-UPPER", "not-an-attempt", "attempt-"])
def test_destination_name_must_be_a_fresh_direct_attempt_child(
    tmp_path: pathlib.Path, name: str
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config(name)

    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(config, apply=True)

    assert caught.value.code == "DESTINATION_INVALID"
    assert not config.attempt_root.exists()


def test_destination_refuses_traversal_symlink_and_source_containment(
    tmp_path: pathlib.Path,
) -> None:
    fixture = Fixture(tmp_path)

    traversal = dataclasses.replace(
        fixture.config("attempt-traversal"),
        attempt_root=(
            fixture.package_parent
            / ".."
            / package.EXPECTED_PARENT_NAME
            / "attempt-traversal"
        ),
    )
    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(traversal, apply=True)
    assert caught.value.code == "PATH_INVALID"

    real_parent = fixture.root / "real-package-parent"
    real_parent.mkdir()
    linked_parent = fixture.root / "linked" / package.EXPECTED_PARENT_NAME
    linked_parent.parent.mkdir()
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    symlinked = dataclasses.replace(
        fixture.config("attempt-symlinked"),
        attempt_root=linked_parent / "attempt-symlinked",
    )
    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(symlinked, apply=True)
    assert caught.value.code == "SYMLINK_REFUSED"

    inside_parent = fixture.source_attempt / package.EXPECTED_PARENT_NAME
    inside_parent.mkdir()
    contained = dataclasses.replace(
        fixture.config("attempt-contained"),
        attempt_root=inside_parent / "attempt-contained",
    )
    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(contained, apply=True)
    assert caught.value.code == "DESTINATION_INVALID"


def test_existing_target_refused_before_and_after_planning(
    tmp_path: pathlib.Path,
) -> None:
    fixture = Fixture(tmp_path)
    before_config = fixture.config("attempt-already-there")
    before_config.attempt_root.mkdir()
    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(before_config, apply=True)
    assert caught.value.code == "DESTINATION_EXISTS"

    after_config = fixture.config("attempt-raced")
    plan = package.plan_materialization(after_config, apply=True)
    after_config.attempt_root.mkdir()
    with pytest.raises(package.PackageProjectError) as caught:
        package.apply_materialization(plan)
    assert caught.value.code == "DESTINATION_EXISTS"
    assert list(after_config.attempt_root.iterdir()) == []


def test_destination_parent_symlink_swap_after_plan_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-parent-swap")
    plan = package.plan_materialization(config, apply=True)
    fixture.package_parent.rmdir()
    redirected = fixture.root / "redirected-package-parent"
    redirected.mkdir()
    fixture.package_parent.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(package.PackageProjectError) as caught:
        package.apply_materialization(plan)

    assert caught.value.code == "SYMLINK_REFUSED"
    assert list(redirected.iterdir()) == []
    assert fixture.source_attempt.exists()


def test_source_mutation_before_apply_creates_no_attempt(
    tmp_path: pathlib.Path,
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-source-race-before")
    plan = package.plan_materialization(config, apply=True)
    fixture.map_asset.write_bytes(b"mutated after planning\n")

    with pytest.raises(package.PackageProjectError) as caught:
        package.apply_materialization(plan)

    assert caught.value.code == "SOURCE_CHANGED"
    assert not config.attempt_root.exists()
    assert fixture.source_attempt.exists()


def test_source_mutation_during_copy_is_quarantined_and_never_deleted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-source-race-during")
    plan = package.plan_materialization(config, apply=True)
    original = package._copy_source_file
    mutated = False

    def mutate_after_first_copy(
        output: package.OutputFile, destination: pathlib.Path
    ) -> str:
        nonlocal mutated
        method = original(output, destination)
        if not mutated:
            mutated = True
            fixture.plugin_source.write_bytes(b"source changed during copy\n")
        return method

    monkeypatch.setattr(package, "_copy_source_file", mutate_after_first_copy)
    with pytest.raises(package.PackageProjectError) as caught:
        package.apply_materialization(plan)

    assert caught.value.code == "SOURCE_CHANGED"
    assert fixture.source_attempt.exists()
    assert fixture.plugin_source.read_bytes() == b"source changed during copy\n"
    failure, raw = _read_receipt(config.attempt_root)
    assert failure["status"] == "failed_quarantined"
    assert failure["error"]["code"] == "SOURCE_CHANGED"
    assert failure["quarantine"]["attempt_retained"] is True
    assert raw == package.canonical_json(failure)
    _assert_private_tree(config.attempt_root)


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b"copy drift\n", "COPY_DRIFT"),
        ((_token_key() + "=injected-output-value\n").encode(), "SECRET_REFUSED"),
    ],
)
def test_target_drift_or_secret_injection_is_quarantined(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    expected_code: str,
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config(f"attempt-{expected_code.lower().replace('_', '-')}")
    plan = package.plan_materialization(config, apply=True)
    original = package._copy_source_file
    injected = False

    def inject_after_first_copy(
        output: package.OutputFile, destination: pathlib.Path
    ) -> str:
        nonlocal injected
        method = original(output, destination)
        if not injected:
            injected = True
            destination.write_bytes(payload)
            destination.chmod(package.PRIVATE_FILE_MODE)
        return method

    monkeypatch.setattr(package, "_copy_source_file", inject_after_first_copy)
    with pytest.raises(package.PackageProjectError) as caught:
        package.apply_materialization(plan)

    assert caught.value.code == expected_code
    failure, raw = _read_receipt(config.attempt_root)
    assert failure["status"] == "failed_quarantined"
    assert failure["error"]["code"] == expected_code
    assert b"injected-output-value" not in raw
    assert fixture.source_attempt.exists()


def test_unexpected_partial_copy_failure_is_retained_with_safe_receipt(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-partial-failure")
    plan = package.plan_materialization(config, apply=True)
    original = package._copy_source_file
    calls = 0

    def fail_after_one_copy(
        output: package.OutputFile, destination: pathlib.Path
    ) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected fixture failure")
        return original(output, destination)

    monkeypatch.setattr(package, "_copy_source_file", fail_after_one_copy)
    with pytest.raises(RuntimeError, match="injected fixture failure"):
        package.apply_materialization(plan)

    failure, raw = _read_receipt(config.attempt_root)
    assert failure["status"] == "failed_quarantined"
    assert failure["error"]["code"] == "MATERIALIZER_UNEXPECTED"
    assert "injected fixture failure" not in raw.decode("utf-8")
    assert any((config.attempt_root / "project").rglob("*"))
    assert fixture.source_attempt.exists()
    _assert_private_tree(config.attempt_root)


def test_reflink_unsupported_uses_verified_byte_copy(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-byte-copy")
    plan = package.plan_materialization(config, apply=True)

    def unsupported(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "fixture filesystem has no reflink")

    monkeypatch.setattr(package.fcntl, "ioctl", unsupported)
    receipt, _ = package.apply_materialization(plan)

    copied_file_count = sum(output.source is not None for output in plan.output.files)
    assert receipt["copy_methods"] == {"byte_copy": copied_file_count}
    assert receipt["project"] == plan.output.receipt_record()


@pytest.mark.parametrize(
    "secret_payload",
    [
        (_token_key() + "=copy-eligible-secret\n").encode(),
        b"postgresql://" + b"fixture-user:" + b"fixture-password@db.invalid/vista\n",
    ],
)
def test_copy_eligible_secret_refuses_before_target_creation(
    tmp_path: pathlib.Path, secret_payload: bytes
) -> None:
    fixture = Fixture(tmp_path)
    config = fixture.config("attempt-source-secret")
    secret_file = fixture.project / "Content/VISTA/secret.ini"
    _write(secret_file, secret_payload)

    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(config, apply=True)

    assert caught.value.code == "SECRET_REFUSED"
    assert "fixture-password" not in str(caught.value)
    assert "copy-eligible-secret" not in str(caught.value)
    assert not config.attempt_root.exists()


def test_credential_shaped_output_path_cannot_escape_in_plan(
    tmp_path: pathlib.Path,
) -> None:
    fixture = Fixture(tmp_path)
    credential_path = (
        fixture.project / "Content/VISTA" / f"{_token_key()}=path-value.uasset"
    )
    _write(credential_path, b"non-secret fixture bytes\n")
    config = dataclasses.replace(
        fixture.config("attempt-secret-path"),
        source_project_tree_sha256=package.source_project_tree_sha256(fixture.project),
    )

    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(config, apply=True)

    assert caught.value.code == "SECRET_REFUSED"
    assert "path-value" not in str(caught.value)
    assert not config.attempt_root.exists()


def test_missing_required_engine_semantics_refuses_without_secret_echo(
    tmp_path: pathlib.Path,
) -> None:
    fixture = Fixture(tmp_path, include_required_nav=False)
    config = fixture.config("attempt-missing-nav")

    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(config, apply=True)

    assert caught.value.code == "SOURCE_CONFIG_INVALID"
    assert "source-fixture-value-that-must-not-escape" not in str(caught.value)
    assert _token_key() not in str(caught.value)
    assert not config.attempt_root.exists()


@pytest.mark.parametrize(
    "invalid_input",
    [
        b"""[/Script/Engine.InputSettings]
; +AxisMappings=(AxisName="MoveForward",Scale=1.0,Key=W)
; +AxisMappings=(AxisName="MoveForward",Scale=-1.0,Key=S)
; +AxisMappings=(AxisName="MoveRight",Scale=1.0,Key=D)
; +AxisMappings=(AxisName="MoveRight",Scale=-1.0,Key=A)
; +AxisMappings=(AxisName="Turn",Scale=1.0,Key=MouseX)
; +AxisMappings=(AxisName="LookUp",Scale=-1.0,Key=MouseY)
; +ActionMappings=(ActionName="Jump",Key=SpaceBar)
; +ActionMappings=(ActionName="Sprint",Key=LeftShift)
; +ActionMappings=(ActionName="Crouch",Key=C)
; +ActionMappings=(ActionName="Interact",Key=E)
; +ActionMappings=(ActionName="Drop",Key=Q)
""",
        DEFAULT_INPUT.replace(b'ActionName="Drop",Key=Q', b'ActionName="Drop",Key=G'),
    ],
)
def test_default_input_requires_active_fixed_key_semantics(
    tmp_path: pathlib.Path, invalid_input: bytes
) -> None:
    fixture = Fixture(tmp_path)
    fixture.source_input.write_bytes(invalid_input)
    config = dataclasses.replace(
        fixture.config("attempt-invalid-input"),
        source_project_tree_sha256=package.source_project_tree_sha256(fixture.project),
    )

    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(config, apply=True)

    assert caught.value.code == "SOURCE_INPUT_INVALID"
    assert not config.attempt_root.exists()


def test_symlink_and_case_collision_in_copy_projection_are_refused(
    tmp_path: pathlib.Path,
) -> None:
    fixture = Fixture(tmp_path)
    external = fixture.root / "external-binary"
    _write(external, b"external\n")
    fixture.plugin_binary.unlink()
    fixture.plugin_binary.symlink_to(external)
    symlink_config = fixture.config("attempt-plugin-symlink")

    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(symlink_config, apply=True)
    assert caught.value.code == "SYMLINK_REFUSED"

    fixture.plugin_binary.unlink()
    _write(fixture.plugin_binary, b"restored excluded binary\n")
    first = fixture.project / "Content/VISTA/CaseAsset.uasset"
    second = fixture.project / "Content/VISTA/caseasset.uasset"
    _write(first, b"first\n")
    _write(second, b"second\n")
    collision_config = dataclasses.replace(
        fixture.config("attempt-case-collision"),
        source_project_tree_sha256=package.source_project_tree_sha256(fixture.project),
    )
    with pytest.raises(package.PackageProjectError) as caught:
        package.plan_materialization(collision_config, apply=True)
    assert caught.value.code == "OUTPUT_COLLISION"


def test_historical_scene_input_pin_can_differ_but_current_bytes_are_exact(
    tmp_path: pathlib.Path,
) -> None:
    historical = "b" * 64
    fixture = Fixture(tmp_path, historical_input_sha256=historical)
    config = fixture.config("attempt-historical-input")

    plan = package.plan_materialization(config, apply=True)
    receipt, _ = package.apply_materialization(plan)

    binding = receipt["source"]["verified_default_input"]
    assert binding["scene_receipt_declared_sha256"] == historical
    assert binding["sha256"] == _sha256(fixture.source_input)
    assert (
        config.attempt_root / "project/Config/DefaultInput.ini"
    ).read_bytes() == fixture.source_input.read_bytes()
