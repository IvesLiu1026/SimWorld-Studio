from __future__ import annotations

import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import stage_vista_workspace as staging  # noqa: E402


class PrivateRuntimeDirectoryTests(unittest.TestCase):
    def test_workspace_root_is_rejected_before_chmod(self):
        with mock.patch.object(staging.os, "fchmod", wraps=os.fchmod) as fchmod:
            with self.assertRaisesRegex(RuntimeError, "filesystem root"):
                staging.provision_private_runtime_directories(pathlib.Path("/"))
        fchmod.assert_not_called()

    def test_resolve_to_open_ancestor_swap_writes_and_chmods_neither_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            ancestor = base / "staging-trusted-ancestor"
            original_workspace = ancestor / "parent" / "workspace"
            original_workspace.mkdir(parents=True, mode=0o755)
            original_workspace.chmod(0o755)
            displaced = base / "staging-ancestor-displaced"
            outside = base / "staging-outside"
            replacement_workspace = outside / "parent" / "workspace"
            replacement_workspace.mkdir(parents=True, mode=0o755)
            replacement_workspace.chmod(0o755)
            original_mode = stat.S_IMODE(original_workspace.stat().st_mode)
            replacement_mode = stat.S_IMODE(replacement_workspace.stat().st_mode)
            real_open = os.open
            swapped = False

            def swap_intermediate_before_open(target, *args, **kwargs):
                nonlocal swapped
                if (
                    not swapped
                    and str(target) == ancestor.name
                    and kwargs.get("dir_fd") is not None
                ):
                    swapped = True
                    ancestor.rename(displaced)
                    ancestor.symlink_to(outside, target_is_directory=True)
                return real_open(target, *args, **kwargs)

            with mock.patch.object(
                staging.os,
                "open",
                side_effect=swap_intermediate_before_open,
            ):
                with self.assertRaisesRegex(RuntimeError, "unavailable or unsafe"):
                    staging.provision_private_runtime_directories(original_workspace)

            displaced_workspace = displaced / "parent" / "workspace"
            self.assertTrue(swapped)
            self.assertEqual(stat.S_IMODE(displaced_workspace.stat().st_mode), original_mode)
            self.assertEqual(stat.S_IMODE(replacement_workspace.stat().st_mode), replacement_mode)
            self.assertEqual(list(displaced_workspace.iterdir()), [])
            self.assertEqual(list(replacement_workspace.iterdir()), [])

    def test_runtime_directories_are_private_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary) / "workspace"
            workspace.mkdir(mode=0o700)

            created = staging.provision_private_runtime_directories(workspace)
            self.assertEqual(
                created,
                tuple(workspace / relative for relative in staging.RUNTIME_DIRS),
            )
            for directory in created:
                self.assertTrue(directory.is_dir())
                self.assertFalse(directory.is_symlink())
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

            (workspace / "tmp" / "review-evidence" / "visual").chmod(0o755)
            staging.provision_private_runtime_directories(workspace)
            self.assertEqual(
                stat.S_IMODE(
                    (workspace / "tmp" / "review-evidence" / "visual").stat().st_mode
                ),
                0o700,
            )

    def test_preexisting_symlink_is_rejected_without_outside_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            workspace = base / "workspace"
            outside = base / "outside"
            workspace.mkdir(mode=0o700)
            outside.mkdir(mode=0o755)
            outside_mode = stat.S_IMODE(outside.stat().st_mode)
            (workspace / "tmp").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "unavailable or unsafe"):
                staging.provision_private_runtime_directories(workspace)

            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), outside_mode)

    def test_parent_swap_is_descriptor_anchored_and_writes_nothing_outside(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            workspace = base / "workspace"
            workspace.mkdir(mode=0o700)
            outside = base / "outside"
            outside.mkdir(mode=0o755)
            outside_mode = stat.S_IMODE(outside.stat().st_mode)
            displaced = base / "tmp-displaced"
            real_mkdir = os.mkdir
            swapped = False

            def swap_parent_before_leaf(target, *args, **kwargs):
                nonlocal swapped
                if not swapped and str(target) == "screens" and kwargs.get("dir_fd") is not None:
                    swapped = True
                    (workspace / "tmp").rename(displaced)
                    (workspace / "tmp").symlink_to(outside, target_is_directory=True)
                return real_mkdir(target, *args, **kwargs)

            with mock.patch.object(staging.os, "mkdir", side_effect=swap_parent_before_leaf):
                with self.assertRaisesRegex(RuntimeError, "changed|unavailable or unsafe"):
                    staging.provision_private_runtime_directories(workspace)

            self.assertTrue(swapped)
            self.assertTrue((displaced / "screens").is_dir())
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), outside_mode)

    def test_leaf_swap_is_descriptor_anchored_and_writes_nothing_outside(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            workspace = base / "workspace"
            workspace.mkdir(mode=0o700)
            outside = base / "outside"
            outside.mkdir(mode=0o755)
            outside_mode = stat.S_IMODE(outside.stat().st_mode)
            displaced = base / "screens-displaced"
            original_prepare = staging._prepare_private_directory_fd
            swapped = False

            def swap_leaf_after_open(descriptor, label):
                nonlocal swapped
                descriptor_target = pathlib.Path(os.readlink(f"/proc/self/fd/{descriptor}"))
                if not swapped and descriptor_target == workspace / "tmp" / "screens":
                    swapped = True
                    descriptor_target.rename(displaced)
                    descriptor_target.symlink_to(outside, target_is_directory=True)
                return original_prepare(descriptor, label)

            with mock.patch.object(
                staging,
                "_prepare_private_directory_fd",
                side_effect=swap_leaf_after_open,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed|unavailable or unsafe"):
                    staging.provision_private_runtime_directories(workspace)

            self.assertTrue(swapped)
            self.assertTrue(displaced.is_dir())
            self.assertEqual(stat.S_IMODE(displaced.stat().st_mode), 0o700)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), outside_mode)


if __name__ == "__main__":
    unittest.main()
