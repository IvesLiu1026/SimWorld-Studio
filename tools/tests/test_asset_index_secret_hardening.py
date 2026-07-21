from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "tools" / "full_asset_index_runner.py"
SPEC = importlib.util.spec_from_file_location("full_asset_index_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class AssetIndexSecretHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = "postgresql://asset_user:super-secret@db.internal:5432/assets"

    def test_run_config_records_source_but_not_dsn(self) -> None:
        args = argparse.Namespace(
            cmd="run",
            postgres_url=self.dsn,
            asset_db_dir=pathlib.Path("/tmp/assets"),
        )

        config = runner.serialize_run_config(args, pathlib.Path("schema.json"))
        encoded = json.dumps(config)

        self.assertNotIn(self.dsn, encoded)
        self.assertNotIn("super-secret", encoded)
        self.assertNotIn("postgres_url", config)
        self.assertTrue(config["postgres_url_configured"])
        self.assertEqual(config["postgres_url_source"], "POSTGRES_URL environment")

    def test_event_and_command_redaction_is_recursive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "events.ndjson"
            runner.append_jsonl(
                path,
                {
                    "event": "failed",
                    "postgres_url": self.dsn,
                    "error": f"connection failed for {self.dsn}",
                    "cmd": ["helper", "--postgres-url", self.dsn],
                },
            )
            persisted = path.read_text(encoding="utf-8")

        self.assertNotIn(self.dsn, persisted)
        self.assertNotIn("super-secret", persisted)
        event = json.loads(persisted)
        self.assertEqual(event["postgres_url"], "<redacted>")
        self.assertEqual(event["cmd"][-1], "<redacted-postgres-dsn>")
        self.assertIn("<redacted-postgres-dsn>", event["error"])

    def test_db_child_environment_carries_dsn_without_an_argv_option(self) -> None:
        args = argparse.Namespace(
            asset_db_dir=pathlib.Path("/tmp/assets"),
            postgres_url=self.dsn,
            qdrant_url="http://127.0.0.1:6333",
            qdrant_collection="assets",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            child_env = runner.build_env(args)

        self.assertEqual(child_env["POSTGRES_URL"], self.dsn)

        runner_source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertNotIn('p.add_argument("--postgres-url"', runner_source)
        self.assertNotIn('"--postgres-url",\n        args.postgres_url', runner_source)

    def test_command_log_masks_a_legacy_dsn_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = pathlib.Path(tmp) / "command.log"
            completed = runner.subprocess.CompletedProcess(["helper"], 0)
            with mock.patch.object(runner.subprocess, "run", return_value=completed):
                runner.run_command(
                    ["helper", "--postgres-url", self.dsn],
                    env={},
                    log_path=log_path,
                )
            persisted = log_path.read_text(encoding="utf-8")

        self.assertNotIn(self.dsn, persisted)
        self.assertNotIn("super-secret", persisted)
        self.assertIn("<redacted-postgres-dsn>", persisted)

    def test_common_parser_reads_postgres_only_from_environment(self) -> None:
        parser = argparse.ArgumentParser()
        with mock.patch.dict(os.environ, {"POSTGRES_URL": self.dsn}):
            runner.add_common_args(parser)
        args = parser.parse_args([])

        self.assertEqual(args.postgres_url, self.dsn)
        self.assertNotIn("--postgres-url", parser.format_help())

    def test_shell_launchers_never_render_the_dsn(self) -> None:
        launcher = (REPO_ROOT / "tools" / "launch_full_asset_index.sh").read_text(encoding="utf-8")
        parallel_launcher = (
            REPO_ROOT / "tools" / "launch_ue58_parallel_asset_index.sh"
        ).read_text(encoding="utf-8")
        parallel_runner = (
            REPO_ROOT / "tools" / "ue58_parallel_asset_index_runner.py"
        ).read_text(encoding="utf-8")
        studio_start = (
            REPO_ROOT / "simworld_studio_workspace" / "web" / "server" / "start.sh"
        ).read_text(encoding="utf-8")

        for source in (launcher, parallel_launcher):
            self.assertNotIn('--postgres-url "$POSTGRES_URL"', source)
            self.assertNotIn('echo "postgres_url=${POSTGRES_URL}"', source)
            self.assertNotIn("postgresql://USER:PASSWORD", source)
        self.assertNotIn('echo "  Postgres   : ${POSTGRES_URL}"', studio_start)
        self.assertNotIn("postgresql://USER:PASSWORD", studio_start)
        self.assertNotIn("DEFAULT_POSTGRES_URL", parallel_runner)
        self.assertIn("runner.serialize_run_config(args, schema)", parallel_runner)

    def test_admin_db_tools_have_no_dsn_argv_surface(self) -> None:
        for name in (
            "migrate_to_postgres.py",
            "build_qdrant_index.py",
            "ensure_postgres_database.py",
        ):
            with self.subTest(name=name):
                source = (REPO_ROOT / "tools" / name).read_text(encoding="utf-8")
                self.assertNotIn('add_argument("--postgres-url"', source)
                self.assertIn("POSTGRES_URL_FILE", source)

    def test_production_launchers_have_no_developer_specific_path_defaults(self) -> None:
        sources = [
            RUNNER_PATH.read_text(encoding="utf-8"),
            (REPO_ROOT / "tools" / "ue58_parallel_asset_index_runner.py").read_text(
                encoding="utf-8"
            ),
            (REPO_ROOT / "tools" / "launch_full_asset_index.sh").read_text(
                encoding="utf-8"
            ),
            (REPO_ROOT / "tools" / "launch_ue58_parallel_asset_index.sh").read_text(
                encoding="utf-8"
            ),
        ]
        for source in sources:
            self.assertNotIn("/data/siddhant", source)
        self.assertNotIn("smoke.DEFAULT_UE58", sources[1])
        self.assertIn("runner.configure_postgres_secret", sources[1])
        for launcher in sources[2:]:
            self.assertIn("uv run --project", launcher)
            self.assertNotIn('CMD=(\n  python3 ', launcher)

    def test_runner_loads_file_secret_but_never_serializes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = pathlib.Path(tmp) / "postgres_url"
            secret_path.write_text(self.dsn + "\n", encoding="utf-8")
            secret_path.chmod(0o600)
            args = argparse.Namespace(
                postgres_url="",
                postgres_url_file=str(secret_path),
                asset_db_dir=pathlib.Path("/tmp/assets"),
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                runner.configure_postgres_secret(args, required=True)
            config = runner.serialize_run_config(args, pathlib.Path("schema.json"))

        self.assertEqual(args.postgres_url, self.dsn)
        self.assertEqual(config["postgres_url_source"], "POSTGRES_URL_FILE")
        encoded = json.dumps(config)
        self.assertNotIn(self.dsn, encoded)
        self.assertNotIn(str(secret_path), encoded)


if __name__ == "__main__":
    unittest.main()
