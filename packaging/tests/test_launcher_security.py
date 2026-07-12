import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from simworld_arena.launcher import (
    CIRRUS_LOOPBACK_PATCH_MARKER,
    EXPECTED_ORIGINAL_CIRRUS_SHA256,
    make_cirrus_config,
    require_loopback_listeners,
    require_ports_free,
    sha256_file,
    validate_cirrus_loopback_patch,
    validate_prepared_workspace,
)


class LauncherSecurityTests(unittest.TestCase):
    def test_checked_in_manifest_matches_source(self):
        repository = Path(__file__).resolve().parents[2]
        manifest = json.loads(
            (repository / "packaging" / "simworld_arena" / "security-manifest.json").read_text()
        )
        for relative, expected in manifest["workspace_sha256"].items():
            source = repository / "simworld_studio_workspace" / relative
            self.assertEqual(sha256_file(source), expected, relative)

    def test_prepared_workspace_requires_security_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            server = workspace / "web" / "server"
            server.mkdir(parents=True)
            (workspace / ".studio_version").write_text("0.2.0\n")
            (server / "runtime-security.js").write_text("module.exports = {};\n")
            (server / "agent-sandbox.js").write_text("module.exports = {};\n")
            (server / "index.js").write_text(
                "requestLoopbackGuard; createModelGate; app.listen(PORT,STUDIO_HOST,()=>{});\n"
            )
            dist = workspace / "web" / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<!doctype html>\n")
            express = server / "node_modules" / "express"
            express.mkdir(parents=True)
            (express / "package.json").write_text('{"name":"express"}\n')
            manifest = workspace / "security-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": "vista-simworld-security-manifest/v1",
                        "workspace_sha256": {
                            "web/server/index.js": sha256_file(server / "index.js"),
                            "web/server/runtime-security.js": sha256_file(
                                server / "runtime-security.js"
                            ),
                            "web/server/agent-sandbox.js": sha256_file(
                                server / "agent-sandbox.js"
                            ),
                        },
                    }
                )
            )
            (workspace.parent / "source-receipt.json").write_text(
                json.dumps(
                    {
                        "schema": "vista-simworld-staged-workspace/v1",
                        "security_files": {
                            "index.js": sha256_file(server / "index.js"),
                            "runtime-security.js": sha256_file(server / "runtime-security.js"),
                        },
                    }
                )
            )
            self.assertEqual(
                validate_prepared_workspace(workspace, manifest), workspace.resolve()
            )

            (server / "index.js").write_text('app.listen(PORT,"0.0.0.0",()=>{});\n')
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                validate_prepared_workspace(workspace, manifest)

    def test_stock_cirrus_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            cirrus = Path(temporary) / "cirrus.js"
            cirrus.write_text("http.listen(httpPort);\n")
            with self.assertRaisesRegex(RuntimeError, "loopback patch"):
                validate_cirrus_loopback_patch(cirrus)
            patched = (
                f"// {CIRRUS_LOOPBACK_PATCH_MARKER}\n"
                "BindAddress; http.listen(httpPort, bindAddress); "
                "https.listen(httpsPort, bindAddress); ({ host: bindAddress });\n"
            )
            cirrus.write_text(patched)
            patched_sha256 = sha256_file(cirrus)
            cirrus.with_name("cirrus.js.vista-receipt.json").write_text(
                json.dumps(
                    {
                        "schema": "vista-cirrus-loopback-patch/v1",
                        "patch_version": CIRRUS_LOOPBACK_PATCH_MARKER,
                        "original_sha256": EXPECTED_ORIGINAL_CIRRUS_SHA256,
                        "patched_sha256": patched_sha256,
                    }
                )
            )
            with mock.patch(
                "simworld_arena.launcher.EXPECTED_PATCHED_CIRRUS_SHA256", patched_sha256
            ):
                validate_cirrus_loopback_patch(cirrus)

    def test_cirrus_config_has_loopback_bind_address(self):
        args = SimpleNamespace(cirrus_http_port=8585, cirrus_ws_port=8586, cirrus_sfu_port=8889)
        config = make_cirrus_config(args)
        self.assertEqual(config["BindAddress"], "127.0.0.1")
        self.assertEqual(json.loads(json.dumps(config)), config)

    def test_port_preflight_rejects_duplicates_and_occupied_ports(self):
        with self.assertRaisesRegex(RuntimeError, "unique port"):
            require_ports_free({"web": 3002, "mcp": 3002})
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError, "already listening"):
                require_ports_free({"test": port})
            require_loopback_listeners({"test": port})
        finally:
            listener.close()

    def test_listener_audit_rejects_wildcard_bind(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError, "not IPv4-loopback-only"):
                require_loopback_listeners({"test": port})
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
