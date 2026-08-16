"""CLI for sealed, local-only hero coverage diagnostics.

The input enumerates every root and candidate.  This command performs no
catalog discovery and accepts only explicit regular files as candidate source
paths.  It emits public, path-scrubbed JSON evidence and never renders assets.

An incomplete audit deliberately exits non-zero *after* retaining
``coverage.json`` and ``contact-sheet-plan.json``.  ``promotion-gate.json`` is
written only when every required hero is eligible for a later human visual
review; that gate does not claim that visual review has happened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .local_asset_catalog import (
    LOCAL_ONLY_PROVIDERS,
    HeroRequirement,
    LocalAssetCandidate,
    LocalAssetCatalogError,
    build_contact_sheet_plan,
    evaluate_local_hero_coverage,
)
from .source_resolver import (
    AllowedSourceRoot,
    AssetSourceSpec,
    canonical_json_bytes,
    content_digest,
)


AUDIT_INPUT_SCHEMA_VERSION = "simworld.vista.playable-home-local-asset-audit-input/v1"
COVERAGE_EVIDENCE_SCHEMA_VERSION = (
    "simworld.vista.playable-home-local-asset-coverage-evidence/v1"
)
PROMOTION_GATE_SCHEMA_VERSION = (
    "simworld.vista.playable-home-local-asset-contact-sheet-promotion/v1"
)

EXIT_COMPLETE = 0
EXIT_INPUT_OR_IO_ERROR = 2
EXIT_AUDIT_REJECTED = 3
EXIT_INCOMPLETE_COVERAGE = 4

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_ROOTS = 64
_MAX_REQUIREMENTS = 128
_MAX_CANDIDATES = 4096
_PRIVATE_PATH_MARKERS = ("/home/", "/root/", "/mnt/", "/nas/", "file://")

_INPUT_FIELDS = frozenset(
    {"schema_version", "use_context", "allowed_roots", "requirements", "candidates"}
)
_ROOT_FIELDS = frozenset({"root_id", "path", "providers"})
_REQUIREMENT_FIELDS = frozenset(
    {
        "hero_id",
        "room_id",
        "required_category",
        "required_style_tags",
        "minimum_dimensions_m",
        "maximum_dimensions_m",
        "minimum_texture_size_px",
        "required_texture_semantics",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id",
        "target_hero_id",
        "declared_category",
        "semantic_aliases",
        "style_tags",
        "source_spec",
    }
)
_SOURCE_SPEC_FIELDS = frozenset(
    {
        "receipt_id",
        "logical_asset_id",
        "provider",
        "source_path",
        "source_version",
        "catalog_identity",
        "metric_bounds_m",
        "license",
        "material_inventory",
        "import_policy",
    }
)


class AuditLocalAssetsError(ValueError):
    """The CLI input, output target, or evidence write failed closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> None:
    raise AuditLocalAssetsError(code, message)


@dataclass(frozen=True)
class ParsedAuditInput:
    input_sha256: str
    use_context: str
    allowed_roots: tuple[AllowedSourceRoot, ...]
    requirements: tuple[HeroRequirement, ...]
    candidates: tuple[LocalAssetCandidate, ...]


def _closed_object(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != fields:
        _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", f"{label} fields differ from the closed contract")
    return dict(value)


def _bounded_list(value: Any, label: str, maximum: int, *, minimum: int = 0) -> list[Any]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", f"{label} has an invalid item count")
    return value


def _plain_string(value: Any, label: str, *, maximum: int = 512) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum or value != value.strip():
        _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", f"{label} is not a bounded normalized string")
    return value


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", "input JSON contains a duplicate key")
        result[key] = value
    return result


def _lexical_absolute(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.fspath(path)))


def _absolute_regular_file(path: pathlib.Path, label: str) -> pathlib.Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        _fail("VISTA_LOCAL_AUDIT_PATH_INVALID", f"{label} must be an absolute regular file")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise AuditLocalAssetsError(
            "VISTA_LOCAL_AUDIT_PATH_INVALID",
            f"{label} is unavailable",
        ) from error
    if _lexical_absolute(path) != resolved:
        _fail("VISTA_LOCAL_AUDIT_PATH_INVALID", f"{label} may not traverse a symlink")
    return resolved


def _absolute_directory(path: pathlib.Path, label: str) -> pathlib.Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        _fail("VISTA_LOCAL_AUDIT_PATH_INVALID", f"{label} must be an absolute directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise AuditLocalAssetsError(
            "VISTA_LOCAL_AUDIT_PATH_INVALID",
            f"{label} is unavailable",
        ) from error
    if _lexical_absolute(path) != resolved:
        _fail("VISTA_LOCAL_AUDIT_PATH_INVALID", f"{label} may not traverse a symlink")
    return resolved


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_json_bytes(path: pathlib.Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    if type(expected_sha256) is not str or not _SHA256_RE.fullmatch(expected_sha256):
        _fail("VISTA_LOCAL_AUDIT_INPUT_DIGEST_INVALID", "--input-sha256 must be lowercase SHA-256")
    source = _absolute_regular_file(path, "audit input")
    if source.suffix.lower() != ".json":
        _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", "audit input must have a .json suffix")
    try:
        size = source.stat().st_size
        if not 1 <= size <= _MAX_INPUT_BYTES:
            _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", "audit input size is outside the accepted bound")
        payload = source.read_bytes()
    except OSError as error:
        raise AuditLocalAssetsError(
            "VISTA_LOCAL_AUDIT_INPUT_READ_FAILED",
            "audit input bytes could not be read",
        ) from error
    observed = _sha256_bytes(payload)
    if observed != expected_sha256:
        _fail("VISTA_LOCAL_AUDIT_INPUT_DIGEST_MISMATCH", "audit input SHA-256 does not match")
    try:
        decoded = json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda value: _fail(
                "VISTA_LOCAL_AUDIT_INPUT_INVALID",
                f"non-finite JSON constant {value} is prohibited",
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditLocalAssetsError(
            "VISTA_LOCAL_AUDIT_INPUT_INVALID",
            "audit input is not strict UTF-8 JSON",
        ) from error
    return _closed_object(decoded, _INPUT_FIELDS, "input"), observed


def _parse_roots(value: Any) -> tuple[AllowedSourceRoot, ...]:
    rows = _bounded_list(value, "allowed_roots", _MAX_ROOTS, minimum=1)
    roots: list[AllowedSourceRoot] = []
    seen_ids: set[str] = set()
    seen_paths: set[pathlib.Path] = set()
    for index, raw in enumerate(rows):
        row = _closed_object(raw, _ROOT_FIELDS, f"allowed_roots[{index}]")
        root_id = _plain_string(row["root_id"], f"allowed_roots[{index}].root_id", maximum=64)
        if not _ROOT_ID_RE.fullmatch(root_id):
            _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", "allowed root ID is invalid")
        providers = _bounded_list(
            row["providers"],
            f"allowed_roots[{index}].providers",
            len(LOCAL_ONLY_PROVIDERS),
            minimum=1,
        )
        if (
            any(type(provider) is not str or provider not in LOCAL_ONLY_PROVIDERS for provider in providers)
            or len(providers) != len(set(providers))
        ):
            _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", "allowed root providers are invalid")
        private_path = pathlib.Path(
            _plain_string(row["path"], f"allowed_roots[{index}].path", maximum=4096)
        )
        resolved = _absolute_directory(private_path, f"allowed_roots[{index}].path")
        if root_id in seen_ids or resolved in seen_paths:
            _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", "allowed roots contain duplicate identity")
        seen_ids.add(root_id)
        seen_paths.add(resolved)
        roots.append(
            AllowedSourceRoot(
                root_id=root_id,
                path=resolved,
                providers=tuple(providers),
            )
        )
    return tuple(roots)


def _tuple_strings(value: Any, label: str, *, minimum: int = 0) -> tuple[str, ...]:
    items = _bounded_list(value, label, 32, minimum=minimum)
    return tuple(_plain_string(item, f"{label}[{index}]", maximum=64) for index, item in enumerate(items))


def _tuple_numbers(value: Any, label: str) -> tuple[float, float, float]:
    items = _bounded_list(value, label, 3, minimum=3)
    if len(items) != 3:
        _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", f"{label} must contain three numbers")
    if any(type(item) not in {int, float} for item in items):
        _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", f"{label} contains a non-number")
    return (float(items[0]), float(items[1]), float(items[2]))


def _parse_requirements(value: Any) -> tuple[HeroRequirement, ...]:
    rows = _bounded_list(value, "requirements", _MAX_REQUIREMENTS, minimum=1)
    requirements: list[HeroRequirement] = []
    for index, raw in enumerate(rows):
        row = _closed_object(raw, _REQUIREMENT_FIELDS, f"requirements[{index}]")
        texture_size = row["minimum_texture_size_px"]
        if type(texture_size) is not int:
            _fail("VISTA_LOCAL_AUDIT_INPUT_INVALID", "minimum texture size must be an integer")
        requirements.append(
            HeroRequirement(
                hero_id=_plain_string(row["hero_id"], f"requirements[{index}].hero_id"),
                room_id=_plain_string(row["room_id"], f"requirements[{index}].room_id"),
                required_category=_plain_string(
                    row["required_category"],
                    f"requirements[{index}].required_category",
                    maximum=64,
                ),
                required_style_tags=_tuple_strings(
                    row["required_style_tags"],
                    f"requirements[{index}].required_style_tags",
                    minimum=1,
                ),
                minimum_dimensions_m=_tuple_numbers(
                    row["minimum_dimensions_m"],
                    f"requirements[{index}].minimum_dimensions_m",
                ),
                maximum_dimensions_m=_tuple_numbers(
                    row["maximum_dimensions_m"],
                    f"requirements[{index}].maximum_dimensions_m",
                ),
                minimum_texture_size_px=texture_size,
                required_texture_semantics=_tuple_strings(
                    row["required_texture_semantics"],
                    f"requirements[{index}].required_texture_semantics",
                    minimum=1,
                ),
            )
        )
    return tuple(requirements)


def _parse_candidate(
    raw: Any,
    index: int,
) -> LocalAssetCandidate:
    row = _closed_object(raw, _CANDIDATE_FIELDS, f"candidates[{index}]")
    raw_spec = _closed_object(
        row["source_spec"],
        _SOURCE_SPEC_FIELDS,
        f"candidates[{index}].source_spec",
    )
    source_path = pathlib.Path(
        _plain_string(
            raw_spec["source_path"],
            f"candidates[{index}].source_spec.source_path",
            maximum=4096,
        )
    )
    # File-only is deliberate: accepting a directory would cause the resolver
    # to enumerate a tree.  The T8 audit must remain an explicit-candidate job.
    resolved_source = _absolute_regular_file(
        source_path,
        f"candidates[{index}].source_spec.source_path",
    )
    source_spec = AssetSourceSpec(
        receipt_id=_plain_string(raw_spec["receipt_id"], f"candidates[{index}].source_spec.receipt_id"),
        logical_asset_id=_plain_string(
            raw_spec["logical_asset_id"],
            f"candidates[{index}].source_spec.logical_asset_id",
        ),
        provider=_plain_string(raw_spec["provider"], f"candidates[{index}].source_spec.provider"),
        source_path=resolved_source,
        source_version=_plain_string(
            raw_spec["source_version"],
            f"candidates[{index}].source_spec.source_version",
            maximum=128,
        ),
        catalog_identity=_plain_string(
            raw_spec["catalog_identity"],
            f"candidates[{index}].source_spec.catalog_identity",
        ),
        metric_bounds_m=raw_spec["metric_bounds_m"],
        license=raw_spec["license"],
        material_inventory=raw_spec["material_inventory"],
        import_policy=raw_spec["import_policy"],
    )
    return LocalAssetCandidate(
        candidate_id=_plain_string(row["candidate_id"], f"candidates[{index}].candidate_id"),
        target_hero_id=_plain_string(row["target_hero_id"], f"candidates[{index}].target_hero_id"),
        source_spec=source_spec,
        declared_category=_plain_string(
            row["declared_category"],
            f"candidates[{index}].declared_category",
            maximum=64,
        ),
        semantic_aliases=_tuple_strings(
            row["semantic_aliases"],
            f"candidates[{index}].semantic_aliases",
        ),
        style_tags=_tuple_strings(
            row["style_tags"],
            f"candidates[{index}].style_tags",
            minimum=1,
        ),
    )


def parse_audit_input(path: pathlib.Path, expected_sha256: str) -> ParsedAuditInput:
    document, observed_sha256 = _load_json_bytes(path, expected_sha256)
    if document["schema_version"] != AUDIT_INPUT_SCHEMA_VERSION:
        _fail("VISTA_LOCAL_AUDIT_INPUT_SCHEMA_UNSUPPORTED", "audit input schema is unsupported")
    candidates = _bounded_list(document["candidates"], "candidates", _MAX_CANDIDATES)
    return ParsedAuditInput(
        input_sha256=observed_sha256,
        use_context=_plain_string(document["use_context"], "use_context", maximum=64),
        allowed_roots=_parse_roots(document["allowed_roots"]),
        requirements=_parse_requirements(document["requirements"]),
        candidates=tuple(_parse_candidate(raw, index) for index, raw in enumerate(candidates)),
    )


def _validate_fresh_output_target(path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    if not path.is_absolute() or path == pathlib.Path("/"):
        _fail("VISTA_LOCAL_AUDIT_OUTPUT_INVALID", "output root must be an absolute child path")
    if path.exists() or path.is_symlink():
        _fail("VISTA_LOCAL_AUDIT_OUTPUT_NOT_FRESH", "output root already exists")
    parent = path.parent
    resolved_parent = _absolute_directory(parent, "output parent")
    if _lexical_absolute(path) != resolved_parent / path.name or path.name in {"", ".", ".."}:
        _fail("VISTA_LOCAL_AUDIT_OUTPUT_INVALID", "output root may not traverse a symlink")
    return path, resolved_parent


def _create_output_root(path: pathlib.Path, resolved_parent: pathlib.Path) -> pathlib.Path:
    target = resolved_parent / path.name
    try:
        os.mkdir(target, mode=0o750)
    except FileExistsError as error:
        raise AuditLocalAssetsError(
            "VISTA_LOCAL_AUDIT_OUTPUT_NOT_FRESH",
            "output root was claimed concurrently",
        ) from error
    except OSError as error:
        raise AuditLocalAssetsError(
            "VISTA_LOCAL_AUDIT_OUTPUT_CREATE_FAILED",
            "output root could not be created",
        ) from error
    return target


def _assert_public_evidence(document: Mapping[str, Any], label: str) -> None:
    serialized = canonical_json_bytes(document).decode("utf-8").lower()
    if any(marker in serialized for marker in _PRIVATE_PATH_MARKERS):
        _fail("VISTA_LOCAL_AUDIT_PRIVATE_PATH_LEAK", f"{label} contains a private path")

    def visit(value: Any) -> None:
        if type(value) is str and (value.startswith("/") or "\\" in value):
            _fail("VISTA_LOCAL_AUDIT_PRIVATE_PATH_LEAK", f"{label} contains path syntax")
        if type(value) is dict:
            for nested in value.values():
                visit(nested)
        elif type(value) is list:
            for nested in value:
                visit(nested)

    visit(document)


def _seal(document: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(document)
    sealed["content_digest"] = content_digest(sealed, "content_digest")
    return sealed


def _coverage_evidence(
    input_sha256: str,
    matrix: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = _seal(
        {
            "schema_version": COVERAGE_EVIDENCE_SCHEMA_VERSION,
            "audit_input_sha256": input_sha256,
            "audit_outcome": (
                "complete"
                if matrix["summary"]["automated_coverage_status"] == "complete"
                else "incomplete"
            ),
            "promotion_gate_eligible": (
                matrix["summary"]["automated_coverage_status"] == "complete"
            ),
            "coverage_matrix": dict(matrix),
        }
    )
    _assert_public_evidence(evidence, "coverage evidence")
    return evidence


def _promotion_gate(
    input_sha256: str,
    coverage_evidence: Mapping[str, Any],
    contact_sheet_plan: Mapping[str, Any],
) -> dict[str, Any]:
    gate = _seal(
        {
            "schema_version": PROMOTION_GATE_SCHEMA_VERSION,
            "audit_input_sha256": input_sha256,
            "coverage_evidence_digest": coverage_evidence["content_digest"],
            "coverage_matrix_digest": coverage_evidence["coverage_matrix"]["content_digest"],
            "contact_sheet_plan_digest": contact_sheet_plan["content_digest"],
            "promotion_scope": "contact_sheet_render_only",
            "status": "eligible_for_human_visual_review",
            "visual_review_status": "not_performed",
            "visual_accepted": False,
        }
    )
    _assert_public_evidence(gate, "promotion gate")
    return gate


def _write_exclusive_json(output_root: pathlib.Path, filename: str, document: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(document) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        root_fd = os.open(output_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            fd = os.open(filename, flags, 0o640, dir_fd=root_fd)
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(fd, payload[offset:])
                    if written <= 0:
                        _fail("VISTA_LOCAL_AUDIT_OUTPUT_WRITE_FAILED", "evidence write made no progress")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
    except OSError as error:
        raise AuditLocalAssetsError(
            "VISTA_LOCAL_AUDIT_OUTPUT_WRITE_FAILED",
            f"{filename} could not be written append-only",
        ) from error


def execute_audit(
    input_path: pathlib.Path,
    input_sha256: str,
    output_root: pathlib.Path,
) -> dict[str, Any]:
    """Execute one no-render audit into a freshly created evidence root."""

    target, resolved_parent = _validate_fresh_output_target(output_root)
    parsed = parse_audit_input(input_path, input_sha256)
    matrix = evaluate_local_hero_coverage(
        parsed.requirements,
        parsed.candidates,
        parsed.allowed_roots,
        use_context=parsed.use_context,
    )
    plan = build_contact_sheet_plan(matrix)
    evidence = _coverage_evidence(parsed.input_sha256, matrix)
    complete = matrix["summary"]["automated_coverage_status"] == "complete"
    gate = _promotion_gate(parsed.input_sha256, evidence, plan) if complete else None

    created_root = _create_output_root(target, resolved_parent)
    _write_exclusive_json(created_root, "coverage.json", evidence)
    _write_exclusive_json(created_root, "contact-sheet-plan.json", plan)
    if gate is not None:
        _write_exclusive_json(created_root, "promotion-gate.json", gate)
    return {
        "status": "complete" if complete else "incomplete",
        "exit_code": EXIT_COMPLETE if complete else EXIT_INCOMPLETE_COVERAGE,
        "audit_input_sha256": parsed.input_sha256,
        "coverage_evidence_digest": evidence["content_digest"],
        "coverage_matrix_digest": matrix["content_digest"],
        "contact_sheet_plan_digest": plan["content_digest"],
        "promotion_gate_digest": gate["content_digest"] if gate is not None else None,
        "visual_review_status": "not_performed",
        "visual_accepted": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit explicitly listed local hero assets without discovery or rendering."
    )
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--output-root", required=True, type=pathlib.Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = execute_audit(args.input, args.input_sha256, args.output_root)
    except AuditLocalAssetsError as error:
        print(
            json.dumps({"status": "rejected", "exit_code": EXIT_INPUT_OR_IO_ERROR, "error_code": error.code}),
            file=sys.stderr,
        )
        return EXIT_INPUT_OR_IO_ERROR
    except LocalAssetCatalogError as error:
        print(
            json.dumps({"status": "rejected", "exit_code": EXIT_AUDIT_REJECTED, "error_code": error.code}),
            file=sys.stderr,
        )
        return EXIT_AUDIT_REJECTED
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return int(result["exit_code"])


if __name__ == "__main__":  # pragma: no cover - exercised through main in focused tests.
    raise SystemExit(main())


__all__ = [
    "AUDIT_INPUT_SCHEMA_VERSION",
    "AuditLocalAssetsError",
    "COVERAGE_EVIDENCE_SCHEMA_VERSION",
    "EXIT_AUDIT_REJECTED",
    "EXIT_COMPLETE",
    "EXIT_INCOMPLETE_COVERAGE",
    "EXIT_INPUT_OR_IO_ERROR",
    "PROMOTION_GATE_SCHEMA_VERSION",
    "ParsedAuditInput",
    "build_parser",
    "execute_audit",
    "main",
    "parse_audit_input",
]
