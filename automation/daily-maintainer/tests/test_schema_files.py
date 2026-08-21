from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
