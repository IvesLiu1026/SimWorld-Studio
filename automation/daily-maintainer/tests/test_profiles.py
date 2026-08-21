from __future__ import annotations

import unittest

from vista_daily_maintainer.profiles import BUILTIN_VALIDATION_PROFILES


class BuiltinProfileTests(unittest.TestCase):
    def test_tools_profile_is_bound_to_the_standalone_locked_project(self) -> None:
        profile = BUILTIN_VALIDATION_PROFILES.resolve("tools-python-offline")
        self.assertEqual(profile.cwd, "tools")
        self.assertEqual(
            profile.argv,
            (
                "uv",
                "run",
                "--project",
                "..",
                "--locked",
                "python",
                "-m",
                "unittest",
                "tests/test_vista_playable_home_contracts.py",
                "tests/test_vista_playable_home_compiler.py",
            ),
        )


if __name__ == "__main__":
    unittest.main()
