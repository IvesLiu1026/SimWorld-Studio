from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile
import unittest


TOOLS_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_ROOT))

import ue58_parallel_asset_index_runner as parallel_runner  # noqa: E402


def runtime_args(**overrides: str) -> argparse.Namespace:
    values = {
        "ue_editor": "",
        "source_project": "",
        "content_root": "",
        "worker_project_root": "",
        "bridge_script": "",
        "ddc_mode": "default",
        "ddc_root": "",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class UE58ParallelRuntimeConfigTests(unittest.TestCase):
    def test_missing_editor_has_no_developer_fallback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "UE58_EDITOR.*required"):
            parallel_runner.validate_ue_runtime_paths(runtime_args())

    def test_relative_runtime_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "UE58_EDITOR must be an absolute path"):
            parallel_runner.validate_ue_runtime_paths(
                runtime_args(ue_editor="relative/SimWorldEditor")
            )

    def test_explicit_absolute_runtime_contract_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            editor = root / "SimWorldEditor"
            editor.write_text("binary placeholder", encoding="utf-8")
            source = root / "project"
            source.mkdir()
            content = root / "content"
            content.mkdir()
            bridge = root / "bridge.py"
            bridge.write_text("# bridge", encoding="utf-8")

            parallel_runner.validate_ue_runtime_paths(
                runtime_args(
                    ue_editor=str(editor),
                    source_project=str(source),
                    content_root=str(content),
                    worker_project_root=str(root / "workers"),
                    bridge_script=str(bridge),
                )
            )

    def test_local_ddc_requires_an_absolute_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            editor = root / "SimWorldEditor"
            editor.write_text("binary placeholder", encoding="utf-8")
            source = root / "project"
            source.mkdir()
            content = root / "content"
            content.mkdir()
            bridge = root / "bridge.py"
            bridge.write_text("# bridge", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "UE58_DDC_ROOT.*required"):
                parallel_runner.validate_ue_runtime_paths(
                    runtime_args(
                        ue_editor=str(editor),
                        source_project=str(source),
                        content_root=str(content),
                        worker_project_root=str(root / "workers"),
                        bridge_script=str(bridge),
                        ddc_mode="local",
                    )
                )


if __name__ == "__main__":
    unittest.main()
