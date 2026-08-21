from __future__ import annotations

import hashlib
import tomllib
import unittest
from pathlib import Path

from vista_daily_maintainer.patcher import (
    PATCHER_OUTPUT_SCHEMA_SHA256,
    PATCHER_PROMPT_SHA256,
)


ROOT = Path(__file__).resolve().parents[1]

PINNED_RESOURCES = {
    "patcher-output.schema.json": PATCHER_OUTPUT_SCHEMA_SHA256,
    "prompts/patcher.md": PATCHER_PROMPT_SHA256,
}

EXPECTED_FORCE_INCLUDE = {
    "sdist": {
        "src/vista_daily_maintainer/resources/patcher-output.schema.json": (
            "src/vista_daily_maintainer/resources/patcher-output.schema.json"
        ),
        "src/vista_daily_maintainer/resources/prompts/patcher.md": (
            "src/vista_daily_maintainer/resources/prompts/patcher.md"
        ),
    },
    "wheel": {
        "src/vista_daily_maintainer/resources/patcher-output.schema.json": (
            "vista_daily_maintainer/resources/patcher-output.schema.json"
        ),
        "src/vista_daily_maintainer/resources/prompts/patcher.md": (
            "vista_daily_maintainer/resources/prompts/patcher.md"
        ),
    },
}


class PackageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pyproject = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        cls.lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))

    def test_python_floor_matches_tomllib_runtime_requirement(self) -> None:
        self.assertEqual(self.pyproject["project"]["requires-python"], ">=3.11")
        self.assertEqual(self.lock["requires-python"], ">=3.11")

    def test_hatch_inclusion_contract_pins_resources_at_stable_paths(self) -> None:
        targets = self.pyproject["tool"]["hatch"]["build"]["targets"]
        for target, expected in EXPECTED_FORCE_INCLUDE.items():
            with self.subTest(target=target):
                force_include = targets[target]["force-include"]
                for source, destination in expected.items():
                    self.assertEqual(force_include.get(source), destination)

        for source, expected_sha256 in PINNED_RESOURCES.items():
            with self.subTest(source=source):
                payload = (ROOT / source).read_bytes()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_sha256)
                packaged = (
                    ROOT / "src" / "vista_daily_maintainer" / "resources" / source
                )
                self.assertEqual(packaged.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
