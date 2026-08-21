from __future__ import annotations

import hashlib
import json
import tempfile
import tomllib
import unittest
from pathlib import Path

import yaml

from vista_daily_maintainer.candidate import (
    BacklogTrust,
    enforce_v1_candidate_policy,
    load_trusted_backlog,
)
from vista_daily_maintainer.receipt import parse_receipt


ROOT = Path(__file__).resolve().parents[1]
DESIGN_PATH: Path | None = (
    ROOT.parents[1] / "docs/specs/vista-world-daily-maintainer/design.md"
    if ROOT.parent.name == "automation"
    else None
)


def _fenced_example(document: str, heading: str, language: str) -> str:
    section = document.index(heading)
    fence = f"```{language}\n"
    start = document.index(fence, section) + len(fence)
    end = document.index("\n```", start)
    return document[start:end]


class PublishedSchemaTests(unittest.TestCase):
    def test_contract_schemas_are_strict_json(self) -> None:
        for filename in (
            "candidate.schema.json",
            "backlog.schema.json",
            "receipt.schema.json",
        ):
            with self.subTest(filename=filename):
                value = json.loads((ROOT / filename).read_text(encoding="utf-8"))
                self.assertEqual(
                    value["$schema"], "https://json-schema.org/draft/2020-12/schema"
                )
                self.assertFalse(value["additionalProperties"])

    def test_candidate_schema_has_no_executable_fields(self) -> None:
        schema = json.loads(
            (ROOT / "candidate.schema.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("command", schema["properties"])
        self.assertNotIn("argv", schema["properties"])
        self.assertEqual(
            schema["properties"]["expected_external_side_effects"],
            {"const": "none"},
        )
        self.assertEqual(schema["properties"]["risk_tier"]["maximum"], 1)
        profiles = set(schema["properties"]["validation_profiles"]["items"]["enum"])
        self.assertNotIn("unreal-content-contract", profiles)

    def test_receipt_schema_statuses_match_v1(self) -> None:
        schema = json.loads((ROOT / "receipt.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(schema["properties"]["status"]["enum"]),
            {
                "skipped",
                "no_change",
                "patch_rejected",
                "validation_failed",
                "pr_open",
                "merged",
                "infrastructure_failed",
                "halted",
            },
        )
        conditioned: set[str] = set()
        for condition in schema["allOf"]:
            status = condition["if"]["properties"]["status"]
            conditioned.update(status.get("enum", ()))
            if "const" in status:
                conditioned.add(status["const"])
        self.assertEqual(conditioned, set(schema["properties"]["status"]["enum"]))
        protected = schema["properties"]["protected_paths_touched"]
        self.assertTrue(protected["uniqueItems"])
        self.assertIn("pattern", protected["items"])
        self.assertIn("actors", schema["required"])
        self.assertEqual(
            set(schema["$defs"]["actors"]["required"]),
            {
                "commit_author",
                "git_committer",
                "pr_actor",
                "promotion_actor",
            },
        )

    def test_contract_schemas_ship_in_wheel_and_sdist_configuration(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        build = project["tool"]["hatch"]["build"]["targets"]
        force_include = build["wheel"]["force-include"]
        for filename in (
            "candidate.schema.json",
            "backlog.schema.json",
            "receipt.schema.json",
        ):
            with self.subTest(filename=filename):
                self.assertIn(filename, force_include)
                self.assertIn(f"/{filename}", build["sdist"]["include"])

    def test_design_candidate_example_matches_the_executable_contract(self) -> None:
        if DESIGN_PATH is None or not DESIGN_PATH.is_file():
            self.skipTest("repository design document is not part of the sdist")
        document = DESIGN_PATH.read_text(encoding="utf-8")
        candidate = yaml.safe_load(
            _fenced_example(document, "### Candidate manifest", "yaml")
        )
        backlog = {
            "schema_version": "vista.world.daily-maintainer.backlog.v1",
            "manifest_revision": candidate["source"]["manifest_revision"],
            "approved_by": candidate["source"]["approved_by"],
            "candidates": [candidate],
        }
        encoded = yaml.safe_dump(backlog, sort_keys=False).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "backlog.yaml"
            path.write_bytes(encoded)
            loaded = load_trusted_backlog(
                BacklogTrust(
                    path=path,
                    sha256=hashlib.sha256(encoded).hexdigest(),
                    manifest_revision=backlog["manifest_revision"],
                    approved_by=backlog["approved_by"],
                )
            )
        enforce_v1_candidate_policy(loaded.candidates[0])

    def test_design_receipt_example_matches_the_executable_contract(self) -> None:
        if DESIGN_PATH is None or not DESIGN_PATH.is_file():
            self.skipTest("repository design document is not part of the sdist")
        document = DESIGN_PATH.read_text(encoding="utf-8")
        example = _fenced_example(document, "### Run receipt", "json")
        receipt = parse_receipt(example)
        self.assertEqual(receipt.status.value, "merged")
        self.assertEqual(receipt.candidate_id, "VW-DM-0001")


if __name__ == "__main__":
    unittest.main()
