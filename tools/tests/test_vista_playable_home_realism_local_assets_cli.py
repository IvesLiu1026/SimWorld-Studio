from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.blender.vista_playable_home_realism.audit_local_assets import (
    AUDIT_INPUT_SCHEMA_VERSION,
    AuditLocalAssetsError,
    COVERAGE_EVIDENCE_SCHEMA_VERSION,
    EXIT_AUDIT_REJECTED,
    EXIT_COMPLETE,
    EXIT_INCOMPLETE_COVERAGE,
    EXIT_INPUT_OR_IO_ERROR,
    PROMOTION_GATE_SCHEMA_VERSION,
    execute_audit,
    main,
)
from tools.blender.vista_playable_home_realism.local_asset_catalog import (
    CONTACT_SHEET_PLAN_SCHEMA_VERSION,
    PRIVATE_RESEARCH_DEMO,
    ROOM_KITCHEN,
    ROOM_LIVING,
)
from tools.blender.vista_playable_home_realism.source_resolver import content_digest


def _project_license() -> dict:
    return {
        "license_id": "Apache-2.0",
        "license_url": "https://spdx.org/licenses/Apache-2.0.html",
        "entitlement_status": "project_owned",
        "entitlement_record": "project-owned://simworld/cli-fixture",
        "attribution": "SimWorld focused CLI fixture author",
        "modification_notice": "Original project-authored fixture.",
        "commercial_use": "allowed",
        "redistribution_restriction": "project_policy",
    }


def _requirement(
    *,
    hero_id: str = "home.r1/room.living_room/entity.sofa.01",
    room_id: str = ROOM_LIVING,
    category: str = "sofa",
    minimum: tuple[float, float, float] = (1.8, 0.7, 0.7),
    maximum: tuple[float, float, float] = (2.6, 1.3, 1.3),
) -> dict:
    return {
        "hero_id": hero_id,
        "room_id": room_id,
        "required_category": category,
        "required_style_tags": ["residential", "contemporary"],
        "minimum_dimensions_m": list(minimum),
        "maximum_dimensions_m": list(maximum),
        "minimum_texture_size_px": 2048,
        "required_texture_semantics": ["base_color", "normal", "roughness"],
    }


def _candidate(root: Path, *, source: Path | None = None) -> dict:
    source = source or root / "assets" / "sofa.glb"
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        source.write_bytes(b"explicit CLI sofa candidate")
    return {
        "candidate_id": "candidate.project.sofa",
        "target_hero_id": "home.r1/room.living_room/entity.sofa.01",
        "declared_category": "sofa",
        "semantic_aliases": [],
        "style_tags": ["contemporary", "residential"],
        "source_spec": {
            "receipt_id": "source.candidate.project.sofa",
            "logical_asset_id": "visual.candidate.project.sofa",
            "provider": "project_authored",
            "source_path": str(source),
            "source_version": "fixture_v1",
            "catalog_identity": "project://catalog/candidate.project.sofa",
            "metric_bounds_m": {"min_m": [0, 0, 0], "max_m": [2.1, 0.95, 1.0]},
            "license": _project_license(),
            "material_inventory": {
                "slots": [
                    {
                        "slot_id": "hero_surface",
                        "shader_class": "pbr_metallic_roughness",
                        "blend_mode": "opaque",
                        "texture_semantics": ["base_color", "normal", "roughness"],
                        "minimum_texture_size_px": 2048,
                    }
                ],
                "texture_count": 3,
                "all_primitives_material_bound": True,
            },
            "import_policy": {
                "nanite": "eligible_static_opaque",
                "mobility": "static",
                "lod_policy": "nanite",
                "collision_policy": "hidden_r1_proxy",
            },
        },
    }


def _document(tmp_path: Path, *, complete: bool = True) -> dict:
    root = (tmp_path / "project_assets").resolve()
    root.mkdir()
    requirements = [_requirement()]
    if not complete:
        requirements.append(
            _requirement(
                hero_id="home.r1/room.kitchen_dining/entity.dining_table.01",
                room_id=ROOM_KITCHEN,
                category="dining_table",
                minimum=(1.3, 0.75, 0.65),
                maximum=(2.2, 1.3, 1.0),
            )
        )
    return {
        "schema_version": AUDIT_INPUT_SCHEMA_VERSION,
        "use_context": PRIVATE_RESEARCH_DEMO,
        "allowed_roots": [
            {
                "root_id": "project_assets",
                "path": str(root),
                "providers": ["project_authored"],
            }
        ],
        "requirements": requirements,
        "candidates": [_candidate(root)],
    }


def _write_input(tmp_path: Path, document: dict, *, name: str = "audit-input.json") -> tuple[Path, str]:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    path = (tmp_path / name).resolve()
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _serialized_outputs(root: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.iterdir()))


def test_complete_cli_writes_three_public_append_only_evidence_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = _document(tmp_path)
    input_path, input_sha = _write_input(tmp_path, document)
    output_root = (tmp_path / "attempt-complete").resolve()

    exit_code = main(
        [
            "--input",
            str(input_path),
            "--input-sha256",
            input_sha,
            "--output-root",
            str(output_root),
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == ""
    assert exit_code == EXIT_COMPLETE
    assert result["status"] == "complete"
    assert result["visual_review_status"] == "not_performed"
    assert result["visual_accepted"] is False
    assert {path.name for path in output_root.iterdir()} == {
        "coverage.json",
        "contact-sheet-plan.json",
        "promotion-gate.json",
    }

    coverage = _read(output_root / "coverage.json")
    plan = _read(output_root / "contact-sheet-plan.json")
    gate = _read(output_root / "promotion-gate.json")
    assert coverage["schema_version"] == COVERAGE_EVIDENCE_SCHEMA_VERSION
    assert coverage["audit_input_sha256"] == input_sha
    assert coverage["audit_outcome"] == "complete"
    assert coverage["content_digest"] == content_digest(coverage, "content_digest")
    assert coverage["coverage_matrix"]["summary"]["visual_accepted"] is False
    assert plan["schema_version"] == CONTACT_SHEET_PLAN_SCHEMA_VERSION
    assert plan["promotion_eligible"] is True
    assert plan["visual_accepted"] is False
    assert gate["schema_version"] == PROMOTION_GATE_SCHEMA_VERSION
    assert gate["promotion_scope"] == "contact_sheet_render_only"
    assert gate["status"] == "eligible_for_human_visual_review"
    assert gate["visual_review_status"] == "not_performed"
    assert gate["visual_accepted"] is False
    assert gate["content_digest"] == content_digest(gate, "content_digest")

    serialized = _serialized_outputs(output_root)
    assert str(tmp_path) not in serialized
    assert str(document["allowed_roots"][0]["path"]) not in serialized
    assert "/home/" not in serialized
    assert "/mnt/" not in serialized
    assert "file://" not in serialized


def test_incomplete_cli_retains_diagnostics_returns_nonzero_and_omits_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = _document(tmp_path, complete=False)
    input_path, input_sha = _write_input(tmp_path, document)
    output_root = (tmp_path / "attempt-incomplete").resolve()

    exit_code = main(
        [
            "--input",
            str(input_path),
            "--input-sha256",
            input_sha,
            "--output-root",
            str(output_root),
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_INCOMPLETE_COVERAGE
    assert result["status"] == "incomplete"
    assert result["promotion_gate_digest"] is None
    assert {path.name for path in output_root.iterdir()} == {
        "coverage.json",
        "contact-sheet-plan.json",
    }
    coverage = _read(output_root / "coverage.json")
    plan = _read(output_root / "contact-sheet-plan.json")
    missing = ["home.r1/room.kitchen_dining/entity.dining_table.01"]
    assert coverage["audit_outcome"] == "incomplete"
    assert coverage["promotion_gate_eligible"] is False
    assert coverage["coverage_matrix"]["summary"]["missing_hero_ids"] == missing
    assert plan["missing_hero_ids"] == missing
    assert plan["promotion_eligible"] is False
    assert plan["visual_review_status"] == "not_performed"


def test_evidence_bytes_are_deterministic_across_fresh_output_roots(tmp_path: Path) -> None:
    document = _document(tmp_path)
    input_path, input_sha = _write_input(tmp_path, document)
    first_root = (tmp_path / "attempt-a").resolve()
    second_root = (tmp_path / "attempt-b").resolve()

    first = execute_audit(input_path, input_sha, first_root)
    second = execute_audit(input_path, input_sha, second_root)

    assert first == second
    assert {
        path.name: path.read_bytes() for path in first_root.iterdir()
    } == {
        path.name: path.read_bytes() for path in second_root.iterdir()
    }


def test_input_digest_mismatch_fails_before_creating_output_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path, _input_sha = _write_input(tmp_path, _document(tmp_path))
    output_root = (tmp_path / "digest-rejected").resolve()

    exit_code = main(
        [
            "--input",
            str(input_path),
            "--input-sha256",
            "0" * 64,
            "--output-root",
            str(output_root),
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == EXIT_INPUT_OR_IO_ERROR
    assert error["error_code"] == "VISTA_LOCAL_AUDIT_INPUT_DIGEST_MISMATCH"
    assert not output_root.exists()


def test_closed_input_rejects_unknown_top_level_and_nested_fields(tmp_path: Path) -> None:
    for index, mutation in enumerate(("top", "candidate", "source")):
        case = tmp_path / f"case-{index}"
        case.mkdir()
        document = _document(case)
        if mutation == "top":
            document["download_if_missing"] = True
        elif mutation == "candidate":
            document["candidates"][0]["fallback_category"] = "sofa"
        else:
            document["candidates"][0]["source_spec"]["scan_root"] = True
        input_path, input_sha = _write_input(case, document)

        with pytest.raises(AuditLocalAssetsError, match="closed contract"):
            execute_audit(input_path, input_sha, (case / "output").resolve())


def test_wrong_input_schema_fails_before_creating_output_root(tmp_path: Path) -> None:
    document = _document(tmp_path)
    document["schema_version"] = "simworld.vista.playable-home-local-asset-audit-input/v999"
    input_path, input_sha = _write_input(tmp_path, document)
    output_root = (tmp_path / "wrong-schema").resolve()

    with pytest.raises(AuditLocalAssetsError, match="VISTA_LOCAL_AUDIT_INPUT_SCHEMA_UNSUPPORTED"):
        execute_audit(input_path, input_sha, output_root)

    assert not output_root.exists()


def test_existing_output_root_is_rejected_without_overwriting(tmp_path: Path) -> None:
    input_path, input_sha = _write_input(tmp_path, _document(tmp_path))
    output_root = (tmp_path / "already-used").resolve()
    output_root.mkdir()
    marker = output_root / "existing-evidence.json"
    marker.write_text("retain", encoding="utf-8")

    with pytest.raises(AuditLocalAssetsError, match="VISTA_LOCAL_AUDIT_OUTPUT_NOT_FRESH"):
        execute_audit(input_path, input_sha, output_root)

    assert marker.read_text(encoding="utf-8") == "retain"
    assert list(output_root.iterdir()) == [marker]


def test_relative_input_and_output_paths_are_rejected(tmp_path: Path) -> None:
    input_path, input_sha = _write_input(tmp_path, _document(tmp_path))

    with pytest.raises(AuditLocalAssetsError, match="output root must be an absolute"):
        execute_audit(input_path, input_sha, Path("relative-output"))
    with pytest.raises(AuditLocalAssetsError, match="audit input must be an absolute"):
        execute_audit(Path(input_path.name), input_sha, (tmp_path / "output").resolve())


def test_candidate_directory_is_rejected_so_cli_cannot_recursive_scan(tmp_path: Path) -> None:
    document = _document(tmp_path)
    candidate_dir = (tmp_path / "project_assets" / "assets" / "candidate-tree").resolve()
    candidate_dir.mkdir()
    (candidate_dir / "nested.glb").write_bytes(b"must not be enumerated")
    document["candidates"][0]["source_spec"]["source_path"] = str(candidate_dir)
    input_path, input_sha = _write_input(tmp_path, document)

    with pytest.raises(AuditLocalAssetsError, match="must be an absolute regular file"):
        execute_audit(input_path, input_sha, (tmp_path / "output-tree").resolve())


def test_explicit_file_path_never_uses_recursive_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path, input_sha = _write_input(tmp_path, _document(tmp_path))
    monkeypatch.setattr(
        Path,
        "rglob",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("recursive discovery")),
    )

    result = execute_audit(input_path, input_sha, (tmp_path / "no-scan").resolve())

    assert result["status"] == "complete"


def test_candidate_outside_allowlisted_root_is_audit_rejection_without_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = _document(tmp_path)
    outside = (tmp_path / "outside.glb").resolve()
    outside.write_bytes(b"explicit but outside allowlist")
    document["candidates"][0] = _candidate(
        Path(document["allowed_roots"][0]["path"]),
        source=outside,
    )
    input_path, input_sha = _write_input(tmp_path, document)
    output_root = (tmp_path / "outside-rejected").resolve()

    exit_code = main(
        [
            "--input",
            str(input_path),
            "--input-sha256",
            input_sha,
            "--output-root",
            str(output_root),
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == EXIT_AUDIT_REJECTED
    assert error["error_code"] == "VISTA_LOCAL_ASSET_SOURCE_REJECTED"
    assert not output_root.exists()


def test_input_document_is_not_mutated_by_execution(tmp_path: Path) -> None:
    document = _document(tmp_path)
    original = copy.deepcopy(document)
    input_path, input_sha = _write_input(tmp_path, document)

    execute_audit(input_path, input_sha, (tmp_path / "immutability").resolve())

    assert document == original
