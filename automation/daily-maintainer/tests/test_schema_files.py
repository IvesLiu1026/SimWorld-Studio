from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
