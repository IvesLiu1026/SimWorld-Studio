from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "blender" / "probe_vista_blender_mcp.py"
SPEC = importlib.util.spec_from_file_location("probe_vista_blender_mcp", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

BOOTSTRAP_PATH = MODULE_PATH.with_name("vista_blender_mcp_bootstrap.py")
BOOTSTRAP_SPEC = importlib.util.spec_from_file_location(
    "vista_blender_mcp_bootstrap",
    BOOTSTRAP_PATH,
)
assert BOOTSTRAP_SPEC and BOOTSTRAP_SPEC.loader
BOOTSTRAP = importlib.util.module_from_spec(BOOTSTRAP_SPEC)
BOOTSTRAP_SPEC.loader.exec_module(BOOTSTRAP)


def test_decode_sse_returns_first_data_record() -> None:
    payload = b'event: message\ndata: {"jsonrpc":"2.0","result":{"ok":true}}\n\n'
    assert MODULE._decode_sse(payload)["result"]["ok"] is True


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8400/mcp",
        "http://0.0.0.0:8400/mcp",
        "http://example.com/mcp",
        "http://127.0.0.1:8400/not-mcp",
    ],
)
def test_validate_loopback_url_rejects_nonlocal_or_wrong_transport(url: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        MODULE._validate_loopback_url(url)


def test_claude_template_contains_no_literal_secret() -> None:
    template = MODULE_PATH.with_name("claude-blender-mcp.template.json")
    config = json.loads(template.read_text(encoding="utf-8"))
    server = config["mcpServers"]["blender-mcp"]
    assert server["url"] == "http://127.0.0.1:8400/mcp"
    assert server["headers"]["Authorization"] == "Bearer ${BLENDER_MCP_TOKEN}"


def test_directory_manifest_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "package.py").write_text("VALUE = 1\n", encoding="utf-8")
    (locked / "nested").mkdir()
    (locked / "nested" / "data.json").write_text('{"ok": true}\n', encoding="utf-8")
    (locked / "package-link.py").symlink_to("package.py")
    manifest = tmp_path / "locked.manifest.json"

    BOOTSTRAP.write_manifest(locked, manifest)
    BOOTSTRAP.verify_manifest(locked, manifest)

    (locked / "package.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(BOOTSTRAP.IntegrityError):
        BOOTSTRAP.verify_manifest(locked, manifest)


def test_directory_manifest_rejects_unlisted_file(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "package.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest = tmp_path / "locked.manifest.json"
    BOOTSTRAP.write_manifest(locked, manifest)

    (locked / "credential-stealer.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(BOOTSTRAP.IntegrityError):
        BOOTSTRAP.verify_manifest(locked, manifest)


@pytest.mark.parametrize(
    "script_name",
    ["setup_vista_blender_mcp.sh", "run_vista_blender_mcp.sh"],
)
def test_mcp_scripts_use_allowlisted_sandbox_mounts(script_name: str) -> None:
    script = BOOTSTRAP_PATH.with_name(script_name).read_text(encoding="utf-8")
    assert "--ro-bind / /" not in script
    assert "--dev-bind /dev /dev" not in script
    assert "--dev /dev" in script
    assert "--clearenv" in script
    assert "--ro-bind /usr /usr" in script
    assert "--ro-bind /lib /lib" in script
    assert "/etc/ssl/certs" in script
    assert "  /etc/ssl \\\n" not in script
    assert "POSTGRES_URL" not in script


def test_setup_archives_verified_commit_before_execution() -> None:
    script = BOOTSTRAP_PATH.with_name("setup_vista_blender_mcp.sh").read_text(
        encoding="utf-8"
    )
    assert 'EXPECTED_TREE="3657b1223e0d98d3376b2b155fd862a58eadbe42"' in script
    assert 'diff --quiet "$EXPECTED_COMMIT"' in script
    assert "ls-files --others --exclude-standard" in script
    assert 'archive --format=tar "$EXPECTED_COMMIT"' in script
    assert '--ro-bind "$SOURCE_SNAPSHOT" /source' in script
    assert "/source/tests/unit" in script


def test_launch_requires_and_rechecks_dependency_manifest() -> None:
    script = BOOTSTRAP_PATH.with_name("run_vista_blender_mcp.sh").read_text(
        encoding="utf-8"
    )
    assert "site-packages-locked.manifest.json" in script
    assert "site-packages-locked.manifest.sha256" in script
    assert 'manifest_tool verify-manifest "$SITE_DIR" "$SITE_MANIFEST"' in script
    assert '--ro-bind "$SITE_DIR" /opt/vista-site' in script
    assert '--ro-bind "$SOURCE_SNAPSHOT" /opt/blender-mcp' in script


def test_bootstrap_verifies_vendor_trees_before_import() -> None:
    source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    source_verify = source.index("verify_manifest(source_dir, source_manifest)")
    site_verify = source.index("verify_manifest(site_packages, site_manifest)")
    vendor_import = source.index("from blender_addon import bridge")
    assert source_verify < vendor_import
    assert site_verify < vendor_import
    assert "allow_execute_python=False" in source
    assert "unrestricted=False" in source
