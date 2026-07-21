"""
SimWorld Studio Launcher

Single-command launcher that:
1. Checks prerequisites (Node.js, Claude auth, GPU)
2. Launches UE binary (headless)
3. Waits for MCP port to become available
4. Starts the Studio web server
5. Prints access URL (auto-detects local vs remote)
"""
import argparse
import atexit
import hashlib
import http.client
import json
import os
import pwd
import shutil
import signal
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Optional

from . import __version__


CIRRUS_LOOPBACK_PATCH_MARKER = "VISTA_LOOPBACK_PATCH_V1"
EXPECTED_ORIGINAL_CIRRUS_SHA256 = "92298e881c9240ebe76adfdf0fda39310cc28f5fd5934070194940cca7be29a4"
EXPECTED_PATCHED_CIRRUS_SHA256 = "133a12cf843c69914263a41c3ea3d7f09914ad9241125358850ea0318e55300e"
SECURITY_MANIFEST_SCHEMA = "vista-simworld-security-manifest/v1"
SECURITY_MANIFEST_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
SECURITY_MANIFEST_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
PRIVATE_WORKSPACE_DIRECTORY_MODE = 0o700
PRIVATE_REVIEW_EVIDENCE_DIRS = (
    "tmp/review-evidence",
    "tmp/review-evidence/text",
    "tmp/review-evidence/visual",
)
REQUIRED_WORKSPACE_SECURITY_PATHS = frozenset(
    {
        "web/server/index.js",
        "web/server/runtime-security.js",
        "web/server/pixel-streaming-config.js",
        "web/server/pixel-streaming-endpoint-registry.js",
        "web/server/pixel-streaming-gateway.js",
        "web/server/pixel-streaming-telemetry.js",
        "web/server/review-loop-coordinator.js",
        "web/server/review-public-events.js",
        "web/server/session-manager.js",
        "web/server/vista-runtime-broker.js",
        "web/server/agent-sandbox.js",
        "web/server/builder-runtime-authority.js",
        "web/server/builder-process-policy.js",
        "web/server/agent-runtime-policy.js",
        "web/server/internal-run-capability.js",
        "web/server/production-execution-policy.js",
        "web/server/skill-selector.js",
        "web/server/chat-codex.js",
        "web/server/codex-runner.js",
        "web/server/cursor-runner.js",
        "web/server/gemini-runner.js",
        "web/server/grok-runner.js",
        "web/server/mcp-server.js",
        "web/server/opencode-runner.js",
        "web/server/artifact-revision-journal.js",
        "web/server/artifact-journal-runtime.js",
        "web/server/durable-artifact-io.js",
        "web/server/runtime-mutation-arbiter.js",
        "web/server/runtime-mutation-middleware.js",
        "web/server/internal-http.js",
        "web/server/review-provider.js",
        "web/server/review-budget.js",
        "web/server/review-run-registry.js",
        "web/server/review-scene-binding.js",
        "web/server/review-evidence-route.js",
        "web/server/scene-critic.js",
        "web/server/scene-loop.js",
        "web/server/scene-loop-visual.js",
        "web/server/studio-readiness.js",
        "web/server/vista-animation-timeline-runtime.js",
        "web/server/vista-animation-ue-readiness.js",
        "web/public/ue-player.html",
        "web/public/ue-assets/player.js",
        "web/src/PixelStreamPlayer.jsx",
        "web/src/features/viewport/ViewportPanel.jsx",
        "web/src/state/useSession.js",
        "web/src/state/pollContext.jsx",
        "web/src/api/studioApi.js",
        "web/src/api/appApi.js",
        "web/src/features/chat/chatRuntime.js",
        "web/src/features/chat/ChatPanel.jsx",
        "web/src/features/chat/ChatMessage.jsx",
        "web/src/features/library/staticMcpTools.js",
        "web/src/features/scene/CodingVerifierPanel.jsx",
        "web/src/index.css",
    }
)
VISTA_DEMO_GAME_MODE = (
    "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/"
    "BP_ThirdPersonGameMode.BP_ThirdPersonGameMode_C"
)
VISTA_DEMO_MAP = "/Game/Maps/Empty"
VISTA_DEMO_MAP_SHA256 = "432bc559e18c6d3814fb9e1f7ba21c3b83bde5536037208e765d079619b8d606"
GPU_IDLE_MAX_MEMORY_MIB = 1024
GPU_IDLE_MAX_UTILIZATION_PERCENT = 10
DEFAULT_UE_STARTUP_TIMEOUT_SECONDS = 120
VISTA_DEMO_COLD_UE_STARTUP_TIMEOUT_SECONDS = 300
REVIEWED_IDLE_GRAPHICS_PROCESSES = {
    ("/usr/lib/xorg/Xorg", "root", "G"): 64,
    ("/usr/bin/gnome-shell", "gdm", "G"): 64,
}
VISTA_DEMO_ASSETS = {
    "Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/"
    "BP_ThirdPersonCharacter.uasset": (
        "265abc5b63b32ea194358a0ecd6311c45231cb5022c867867c97fb6d14642db5"
    ),
    "Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/"
    "BP_ThirdPersonGameMode.uasset": (
        "1d7f5b2ef1da7e736cc84f20af892ee40afbb821362b89a72ce464acfd34dbef"
    ),
    "Human_Avatar/DefaultCharacter/ThirdPerson/Input/IMC_Default.uasset": (
        "f35782ed7f0a9e188e1d3ee5ebc36d2b5e76d18d8d8875f033e6c8f27e2bf0cd"
    ),
}
VISTA_DEMO_ASSET_TREES = {
    # Filled with deterministic SHA-256 tree digests for the complete template
    # input and mannequin dependency roots. A top-level Blueprint hash alone
    # cannot prove that its animation, mesh, material, or action dependencies
    # are unchanged.
    "Human_Avatar/DefaultCharacter/ThirdPerson": (
        "4560b359a9dd27e612b03a0f4ded1f4f17d0b72649929328a9b033f095aff195"
    ),
    "Human_Avatar/DefaultCharacter/Characters/Mannequins": (
        "280ec741fb4913875ec61ba5ef6411474e85bdc2248882ed1102a6610e26365d"
    ),
}
PROVIDER_CREDENTIAL_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "GH_CONFIG_DIR",
        "HF_HOME",
        "OPENAI_API_KEY",
        "OPENAI_CONFIG_FILE",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
        "CLOUDSDK_CONFIG",
        "AZURE_CONFIG_DIR",
        "XAI_API_KEY",
        "GROQ_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "CLOUDFLARE_API_TOKEN",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "SSH_AUTH_SOCK",
        "KRB5CCNAME",
        "NETRC",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "XAUTHORITY",
        "DBUS_SESSION_BUS_ADDRESS",
        "DOCKER_CONFIG",
        "KUBECONFIG",
    }
)
LOCAL_CHILD_SECRET_ENV_KEYS = frozenset({"STUDIO_ACCESS_TOKEN"})
SENSITIVE_ENV_KEY_FRAGMENTS = (
    "TOKEN",
    "API_KEY",
    "AUTH_TOKEN",
    "ACCESS_TOKEN",
    "OAUTH_TOKEN",
    "SECRET_KEY",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    """Hash every regular file under a tree by relative path and file digest."""

    root = Path(root).resolve()
    if not root.is_dir():
        raise RuntimeError(f"Required hash tree is missing: {root}")
    files = []
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise RuntimeError(f"Hash tree cannot contain symlinks: {candidate}")
        if candidate.is_file():
            files.append(candidate)
    digest = hashlib.sha256()
    for candidate in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = candidate.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(candidate)))
    return digest.hexdigest()


def sha256_server_source_tree(server_root: Path) -> str:
    """Hash the complete staged server source tree, excluding dependencies."""

    server_root = Path(server_root).resolve()
    if not server_root.is_dir():
        raise RuntimeError(f"Required server source tree is missing: {server_root}")
    files = []
    for candidate in server_root.rglob("*"):
        relative = candidate.relative_to(server_root)
        if relative.parts and relative.parts[0] == "node_modules":
            continue
        if candidate.is_symlink():
            raise RuntimeError(f"Server source tree cannot contain symlinks: {candidate}")
        if candidate.is_file():
            files.append(candidate)
    digest = hashlib.sha256()
    for candidate in sorted(files, key=lambda item: item.relative_to(server_root).as_posix()):
        relative = candidate.relative_to(server_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(candidate)))
    return digest.hexdigest()


def sha256_dependency_tree(root: Path) -> str:
    """Hash every production dependency file and safe in-tree symlink."""

    root = Path(root).resolve()
    if not root.is_dir():
        raise RuntimeError(f"Required dependency tree is missing: {root}")
    entries = []
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            target = os.readlink(candidate)
            if os.path.isabs(target):
                raise RuntimeError(f"Dependency tree has an absolute symlink: {candidate}")
            try:
                (candidate.parent / target).resolve(strict=True).relative_to(root)
            except (FileNotFoundError, RuntimeError, ValueError) as error:
                raise RuntimeError(f"Dependency symlink escapes or is broken: {candidate}") from error
            entries.append((relative, "L", target))
        elif candidate.is_file():
            entries.append((relative, "F", sha256_file(candidate)))
    digest = hashlib.sha256()
    for relative, entry_type, payload in sorted(entries):
        digest.update(entry_type.encode("ascii"))
        digest.update(b"\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def is_sensitive_environment_key(key: str) -> bool:
    normalized = str(key).upper()
    return normalized in PROVIDER_CREDENTIAL_ENV_KEYS or any(
        fragment in normalized for fragment in SENSITIVE_ENV_KEY_FRAGMENTS
    )


def validate_model_off_child_environment(environment: dict[str, str]) -> None:
    leaked = sorted(
        key
        for key in environment
        if key not in LOCAL_CHILD_SECRET_ENV_KEYS and is_sensitive_environment_key(key)
    )
    if leaked:
        raise RuntimeError(f"Model-off child environment still contains sensitive keys: {leaked}")


def make_model_off_child_environment(source=None) -> dict[str, str]:
    """Copy an environment without credentials unused by model-off children."""

    environment = dict(os.environ if source is None else source)
    for key in tuple(environment):
        if is_sensitive_environment_key(key):
            environment.pop(key)
    validate_model_off_child_environment(environment)
    return environment


def prepare_isolated_demo_environment(workspace: Path, source=None) -> dict[str, str]:
    """Build a credential-free HOME/XDG sandbox owned by the staged workspace."""

    workspace = Path(workspace).resolve()
    runtime_root = workspace / "runtime"
    sandbox_root = runtime_root / "vista-demo-sandbox"
    ue_user_dir = sandbox_root / "ue-user"
    paths = {
        "HOME": sandbox_root / "home",
        "XDG_CONFIG_HOME": sandbox_root / "xdg-config",
        "XDG_CACHE_HOME": sandbox_root / "xdg-cache",
        "XDG_DATA_HOME": sandbox_root / "xdg-data",
        "XDG_STATE_HOME": sandbox_root / "xdg-state",
        "XDG_RUNTIME_DIR": sandbox_root / "xdg-runtime",
        "TMPDIR": sandbox_root / "tmp",
    }
    for directory in (runtime_root, sandbox_root, ue_user_dir, *paths.values()):
        if directory.is_symlink():
            raise RuntimeError(f"Demo environment directory must not be a symlink: {directory}")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            directory.resolve().relative_to(workspace)
        except ValueError as error:
            raise RuntimeError(f"Demo environment directory escapes workspace: {directory}") from error
        directory.chmod(0o700)

    environment = make_model_off_child_environment(source)
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "SESSION_MANAGER"):
        environment.pop(key, None)
    environment.update({key: str(path.resolve()) for key, path in paths.items()})
    cache_paths = {
        "CUDA_CACHE_PATH": paths["XDG_CACHE_HOME"] / "cuda",
        "__GL_SHADER_DISK_CACHE_PATH": paths["XDG_CACHE_HOME"] / "nvidia-gl",
    }
    ddc_parent = paths["XDG_CACHE_HOME"] / "UnrealEngine"
    ddc_path = ddc_parent / "DDC"
    for directory in (*cache_paths.values(), ddc_parent, ddc_path):
        if directory.is_symlink():
            raise RuntimeError(f"Demo cache directory must not be a symlink: {directory}")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            directory.resolve().relative_to(workspace)
        except ValueError as error:
            raise RuntimeError(f"Demo cache directory escapes workspace: {directory}") from error
        directory.chmod(0o700)
    environment.update({key: str(path.resolve()) for key, path in cache_paths.items()})
    validate_model_off_child_environment(environment)
    return environment


def validate_prepared_workspace(workspace: Path, manifest_path: Optional[Path] = None) -> Path:
    """Require a staged workspace containing the reviewed source security patch."""
    workspace = Path(workspace).resolve()
    version_file = workspace / ".studio_version"
    index_file = workspace / "web" / "server" / "index.js"
    security_file = workspace / "web" / "server" / "runtime-security.js"
    vista_broker_file = workspace / "web" / "server" / "vista-runtime-broker.js"
    player_file = workspace / "web" / "public" / "ue-player.html"
    mcp_file = workspace / "web" / "mcp.json"
    if not version_file.is_file() or version_file.read_text().strip() != __version__:
        raise RuntimeError(f"Prepared workspace must contain .studio_version={__version__}: {workspace}")
    agent_sandbox_file = workspace / "web" / "server" / "agent-sandbox.js"
    dist_index = workspace / "web" / "dist" / "index.html"
    server_root = workspace / "web" / "server"
    node_modules = server_root / "node_modules"
    frontend_node_modules = workspace / "web" / "node_modules"
    express_package = node_modules / "express" / "package.json"
    if (
        not index_file.is_file()
        or not security_file.is_file()
        or not vista_broker_file.is_file()
        or not agent_sandbox_file.is_file()
        or not player_file.is_file()
        or not mcp_file.is_file()
    ):
        raise RuntimeError(f"Prepared workspace is missing the reviewed source server: {workspace}")
    if not dist_index.is_file() or not express_package.is_file():
        raise RuntimeError(f"Prepared workspace dependencies/frontend are incomplete: {workspace}")
    if frontend_node_modules.exists() or frontend_node_modules.is_symlink():
        raise RuntimeError("Prepared workspace must not retain frontend build dependencies")
    try:
        mcp_config = json.loads(mcp_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Prepared workspace model-off MCP config is invalid") from error
    if mcp_config != {"mcpServers": {}}:
        raise RuntimeError("Prepared workspace model-off MCP config must contain no servers")

    manifest_path = Path(manifest_path) if manifest_path else get_package_dir() / "security-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != SECURITY_MANIFEST_SCHEMA:
        raise RuntimeError("Installed Studio security manifest has an invalid schema")
    workspace_sha256 = manifest.get("workspace_sha256")
    if not isinstance(workspace_sha256, dict) or not workspace_sha256:
        raise RuntimeError("Installed Studio security manifest hashes must be a non-empty object")
    for relative, expected in workspace_sha256.items():
        parsed = PurePosixPath(relative) if isinstance(relative, str) else None
        if (
            parsed is None
            or not SECURITY_MANIFEST_PATH_RE.fullmatch(relative)
            or parsed.is_absolute()
            or any(part in {"", ".", ".."} for part in parsed.parts)
            or parsed.as_posix() != relative
        ):
            raise RuntimeError("Installed Studio security manifest contains an unsafe workspace path")
        if not isinstance(expected, str) or not SECURITY_MANIFEST_DIGEST_RE.fullmatch(expected):
            raise RuntimeError(
                f"Installed Studio security manifest has an invalid SHA-256 digest: {relative}"
            )
    actual_paths = frozenset(workspace_sha256)
    if actual_paths != REQUIRED_WORKSPACE_SECURITY_PATHS:
        missing = sorted(REQUIRED_WORKSPACE_SECURITY_PATHS - actual_paths)
        unexpected = sorted(actual_paths - REQUIRED_WORKSPACE_SECURITY_PATHS)
        raise RuntimeError(
            "Installed Studio security manifest has an invalid required path set "
            f"(missing={missing}, unexpected={unexpected})"
        )
    for relative in sorted(REQUIRED_WORKSPACE_SECURITY_PATHS):
        expected = workspace_sha256[relative]
        candidate = workspace / relative
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(workspace)
        except (OSError, RuntimeError, ValueError) as error:
            raise RuntimeError(
                f"Prepared workspace security path is unavailable or escapes workspace: {relative}"
            ) from error
        if candidate.is_symlink() or not resolved.is_file() or sha256_file(resolved) != expected:
            raise RuntimeError(f"Prepared workspace security SHA-256 mismatch: {relative}")

    receipt_path = workspace.parent / "source-receipt.json"
    if not receipt_path.is_file():
        raise RuntimeError(f"Prepared workspace receipt is missing: {receipt_path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_files = receipt.get("security_files", {})
    receipt_expected = {
        "index.js": sha256_file(index_file),
        "runtime-security.js": sha256_file(security_file),
        "vista-runtime-broker.js": sha256_file(vista_broker_file),
        "agent-sandbox.js": sha256_file(agent_sandbox_file),
        "web/mcp.json": sha256_file(mcp_file),
        "web/public/ue-player.html": sha256_file(player_file),
    }
    receipt_artifacts = receipt.get("artifacts", {})
    artifact_expected = {
        "web/dist/index.html": sha256_file(dist_index),
        "web/dist/tree_sha256": sha256_tree(dist_index.parent),
        "web/server/node_modules/express/package.json": sha256_file(express_package),
        "web/server/source_tree_sha256": sha256_server_source_tree(server_root),
        "web/server/node_modules/tree_sha256": sha256_dependency_tree(node_modules),
    }
    receipt_lockfiles = receipt.get("lockfiles", {})
    lockfile_expected = {
        "web/package-lock.json": sha256_file(workspace / "web" / "package-lock.json"),
        "web/server/package-lock.json": sha256_file(server_root / "package-lock.json"),
    }
    launcher_validation = receipt.get("launcher_validation", {})
    launcher_validation_expected = {
        "validator": "simworld_arena.launcher.validate_prepared_workspace",
        "launcher_sha256": sha256_file(Path(__file__).resolve()),
        "status": "passed",
    }
    source_copy = receipt.get("source_copy", {})
    source_copy_valid = (
        source_copy.get("policy") == "git-archive-head-allowlist/v1"
        and isinstance(source_copy.get("tracked_file_count"), int)
        and source_copy["tracked_file_count"] > 0
    )
    if receipt.get("schema") != "vista-simworld-staged-workspace/v1" or any(
        receipt_files.get(name) != digest for name, digest in receipt_expected.items()
    ) or any(
        receipt_artifacts.get(name) != digest for name, digest in artifact_expected.items()
    ) or any(
        receipt_lockfiles.get(name) != digest for name, digest in lockfile_expected.items()
    ) or launcher_validation != launcher_validation_expected or not source_copy_valid:
        raise RuntimeError("Prepared workspace source receipt is invalid")
    return workspace


def validate_cirrus_loopback_patch(cirrus_js: Path) -> None:
    """Refuse to start stock UE 5.3 Cirrus, which listens on every interface."""
    cirrus_js = Path(cirrus_js)
    receipt_path = cirrus_js.with_name("cirrus.js.vista-receipt.json")
    if not receipt_path.is_file():
        raise RuntimeError("Cirrus loopback patch receipt is missing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    actual_sha256 = sha256_file(cirrus_js)
    if (
        receipt.get("schema") != "vista-cirrus-loopback-patch/v1"
        or receipt.get("patch_version") != CIRRUS_LOOPBACK_PATCH_MARKER
        or receipt.get("original_sha256") != EXPECTED_ORIGINAL_CIRRUS_SHA256
        or receipt.get("patched_sha256") != EXPECTED_PATCHED_CIRRUS_SHA256
        or actual_sha256 != EXPECTED_PATCHED_CIRRUS_SHA256
    ):
        raise RuntimeError("Cirrus loopback patch receipt or SHA-256 is invalid")
    text = cirrus_js.read_text(encoding="utf-8", errors="strict")
    required = (
        CIRRUS_LOOPBACK_PATCH_MARKER,
        "BindAddress",
        "http.listen(httpPort, bindAddress",
        "https.listen(httpsPort, bindAddress",
        "host: bindAddress",
    )
    if any(marker not in text for marker in required):
        raise RuntimeError(
            "Cirrus does not contain the reviewed VISTA loopback patch; "
            "refusing to expose Pixel Streaming listeners"
        )


def make_cirrus_config(args) -> dict:
    return {
        "UseFrontend": True,
        "UseMatchmaker": False,
        "BindAddress": "127.0.0.1",
        "HttpPort": args.cirrus_http_port,
        "StreamerPort": args.cirrus_ws_port,
        "SFUPort": args.cirrus_sfu_port,
    }


def tcp_listener_addresses(port: int) -> list[str]:
    result = subprocess.run(
        ["ss", "-H", "-ltn", f"sport = :{port}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    addresses = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            addresses.append(fields[3].rsplit(":", 1)[0].strip("[]"))
    return addresses


def require_ports_free(ports: dict[str, int]) -> None:
    values = list(ports.values())
    invalid = {name: port for name, port in ports.items() if not 1024 <= port <= 65535}
    if invalid:
        raise RuntimeError(f"User-space ports must be between 1024 and 65535: {invalid}")
    if len(values) != len(set(values)):
        raise RuntimeError(f"Every service needs a unique port: {ports}")
    occupied = {name: port for name, port in ports.items() if tcp_listener_addresses(port)}
    if occupied:
        raise RuntimeError(f"Requested ports are already listening: {occupied}")


def require_loopback_listeners(ports: dict[str, int]) -> None:
    invalid = {}
    for name, port in ports.items():
        addresses = tcp_listener_addresses(port)
        if not addresses or any(address != "127.0.0.1" for address in addresses):
            invalid[name] = {"port": port, "addresses": addresses}
    if invalid:
        raise RuntimeError(f"Listeners are missing or not IPv4-loopback-only: {invalid}")


def wait_for_http_health(port: int, access_token: str, timeout: int = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        connection = None
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request(
                "GET",
                "/api/health",
                headers={
                    "Host": f"127.0.0.1:{port}",
                    "Authorization": f"Bearer {access_token}",
                },
            )
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return True
        except OSError:
            pass
        finally:
            if connection is not None:
                connection.close()
        time.sleep(1)
    return False


def http_status(port: int, access_token: Optional[str] = None) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        connection.request("GET", "/", headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def require_cirrus_http_auth(port: int, access_token: str) -> None:
    if http_status(port) != 401:
        raise RuntimeError("Cirrus accepted an unauthenticated HTTP request")
    if http_status(port, access_token) == 401:
        raise RuntimeError("Cirrus rejected the configured access token")


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise RuntimeError(
            f"Lost ownership of managed process group {process_group_id}"
        ) from error
    return True


def terminate_managed_process_group(process, term_timeout: float = 10, kill_timeout: float = 5) -> None:
    """Terminate and verify the independent process group created for one child."""

    if process is None:
        return
    process_group_id = int(process.pid)
    if process_group_id <= 1 or process_group_id == os.getpgrp():
        raise RuntimeError(f"Unsafe managed process group id: {process_group_id}")
    if not _process_group_exists(process_group_id):
        if process.poll() is None:
            raise RuntimeError(f"Managed child {process.pid} has no owned process group")
        return

    os.killpg(process_group_id, signal.SIGTERM)
    try:
        process.wait(timeout=term_timeout)
    except subprocess.TimeoutExpired:
        pass

    if _process_group_exists(process_group_id):
        os.killpg(process_group_id, signal.SIGKILL)
        if process.poll() is None:
            try:
                process.wait(timeout=kill_timeout)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(
                    f"Managed process group {process_group_id} ignored SIGKILL"
                ) from error
        deadline = time.monotonic() + kill_timeout
        while _process_group_exists(process_group_id) and time.monotonic() < deadline:
            time.sleep(0.05)
    if _process_group_exists(process_group_id):
        raise RuntimeError(f"Managed process group {process_group_id} survived cleanup")


def start_managed_process(command, processes, **kwargs):
    """Start one child in a new session and register its owned process group."""

    if "start_new_session" in kwargs:
        raise ValueError("start_new_session is controlled by the secure launcher")
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    processes.append(process)
    return process


def cleanup_managed_processes(processes, files) -> None:
    errors = []
    for process in reversed(processes):
        try:
            terminate_managed_process_group(process)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(error)
    for handle in files:
        if handle is not None and not handle.closed:
            handle.close()
    if errors:
        raise RuntimeError(
            "Managed process cleanup failed: " + "; ".join(str(error) for error in errors)
        )


def get_package_dir():
    """Return the directory where this package is installed."""
    return Path(__file__).parent


def get_nvidia_headless_icd() -> Path:
    """Return the reviewed EGL-backed NVIDIA ICD used for offscreen rendering."""
    manifest = get_package_dir() / "nvidia-headless-icd.json"
    if not manifest.is_file():
        raise RuntimeError(f"Packaged NVIDIA headless Vulkan ICD is missing: {manifest}")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    icd = payload.get("ICD", {})
    library = Path(str(icd.get("library_path", "")))
    if (
        payload.get("file_format_version") != "1.0.1"
        or library != Path("/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.0")
        or icd.get("api_version") != "1.4.325"
        or not library.is_file()
    ):
        raise RuntimeError("Packaged NVIDIA headless Vulkan ICD is invalid or unavailable")
    return manifest.resolve()


def prepare_nvidia_compat_libraries(workspace: Path) -> Path:
    """Provide unversioned NVIDIA DSOs expected by the pinned UE 5.3 build."""
    workspace = Path(workspace).resolve()
    runtime_dir = workspace / "runtime"
    if runtime_dir.is_symlink():
        raise RuntimeError("NVIDIA runtime directory must not be a symlink")
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    if runtime_dir.resolve().parent != workspace:
        raise RuntimeError("NVIDIA runtime directory escapes the prepared workspace")
    runtime_dir.chmod(0o700)
    compat_dir = runtime_dir / "nvidia-compat"
    if compat_dir.is_symlink():
        raise RuntimeError("NVIDIA compatibility directory must not be a symlink")
    compat_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if compat_dir.resolve().parent != runtime_dir.resolve():
        raise RuntimeError("NVIDIA compatibility directory escapes the prepared workspace")
    compat_dir.chmod(0o700)

    required = {
        "libcuda.so": Path("/usr/lib/x86_64-linux-gnu/libcuda.so.1"),
        "libnvcuvid.so": Path("/usr/lib/x86_64-linux-gnu/libnvcuvid.so.1"),
    }
    for link_name, versioned_path in required.items():
        try:
            target = versioned_path.resolve(strict=True)
        except FileNotFoundError as error:
            raise RuntimeError(f"Required NVIDIA runtime library is missing: {versioned_path}") from error
        target_stat = target.stat()
        if target_stat.st_uid != 0 or target_stat.st_mode & 0o022:
            raise RuntimeError(f"NVIDIA runtime library is not root-owned/read-only: {target}")
        link = compat_dir / link_name
        if link.exists() or link.is_symlink():
            if not link.is_symlink() or link.resolve(strict=True) != target:
                raise RuntimeError(f"Unexpected NVIDIA compatibility link: {link}")
        else:
            link.symlink_to(target)
    return compat_dir.resolve()


def sync_unrealcv_port_in_saved_ini(project_root: Path, port: int) -> None:
    """Set UnrealCV listen port in Saved/unrealcv.ini.

    The bundled UnrealCV plugin uses FParse::Value(..., TEXT("UnrealCVPort"), ...)
    without '='; passing -UnrealCVPort=NNN on the command line is parsed as port 0.
    Configuring the port via ini avoids that bug.
    """
    saved = project_root / "Saved" / "unrealcv.ini"
    saved.parent.mkdir(parents=True, exist_ok=True)
    default = (
        "[UnrealCV.Core]\n"
        f"Port={port}\n"
        "Width=640\n"
        "Height=480\n"
        "FOV=90\n"
        "EnableInput=True\n"
        "EnableRightEye=False\n\n"
    )
    if not saved.exists():
        saved.write_text(default)
        return
    text = saved.read_text(encoding="utf-8", errors="replace")
    section_match = re.search(
        r"(?ms)^(\[UnrealCV\.Core\][^\S\r\n]*\r?\n)(.*?)(?=^\[.*?\][^\S\r\n]*\r?\n|\Z)",
        text,
    )
    if section_match:
        section_header = section_match.group(1)
        section_body = section_match.group(2)
        if re.search(r"(?m)^Port=\d+$", section_body):
            section_body = re.sub(r"(?m)^Port=\d+$", f"Port={port}", section_body, count=1)
        else:
            section_body = f"Port={port}\n" + section_body
        updated = text[:section_match.start()] + section_header + section_body + text[section_match.end():]
        saved.write_text(updated)
    else:
        saved.write_text(text.rstrip() + f"\n\n[UnrealCV.Core]\nPort={port}\n")


def find_node():
    """Find node binary."""
    node = shutil.which("node")
    if not node:
        print("[ERROR] Node.js not found. Install with: apt-get install -y nodejs")
        sys.exit(1)
    return node


def check_claude_auth():
    """Check if Claude is authenticated (OAuth or API key)."""
    # Check API key first
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api_key"

    # Check Claude Code OAuth
    claude = shutil.which("claude")
    if claude:
        try:
            result = subprocess.run(
                [claude, "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and '"loggedIn": true' in result.stdout:
                return "oauth"
        except Exception:
            pass

    return None


def detect_gpu():
    """Detect available GPUs and return count."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            gpus = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
            return gpus
    except Exception:
        pass
    return []


def detected_gpu_indices(gpus: list[str]) -> list[int]:
    """Return the exact nvidia-smi indices from the detection rows."""

    indices = []
    for row in gpus:
        try:
            index = int(row.split(",", 1)[0].strip())
        except (AttributeError, ValueError) as error:
            raise RuntimeError(f"Malformed nvidia-smi GPU inventory row: {row!r}") from error
        if index < 0 or index in indices:
            raise RuntimeError(f"Invalid nvidia-smi GPU index inventory: {gpus!r}")
        indices.append(index)
    return indices


def validate_gpu_index(gpu_index: int, gpus: list[str]) -> int:
    """Require a non-negative GPU index present in the current inventory."""

    if isinstance(gpu_index, bool) or not isinstance(gpu_index, int) or gpu_index < 0:
        raise RuntimeError("GPU index must be a non-negative integer")
    available = detected_gpu_indices(gpus)
    if gpu_index not in available:
        raise RuntimeError(f"GPU index {gpu_index} is unavailable; detected indices: {available}")
    return gpu_index


def _run_nvidia_smi_query(arguments: list[str]) -> list[str]:
    try:
        result = subprocess.run(
            ["nvidia-smi", *arguments],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Unable to audit NVIDIA GPU state") from error
    if result.returncode != 0:
        raise RuntimeError("nvidia-smi GPU state audit failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _run_nvidia_smi_xml(gpu_index: int) -> ET.Element:
    try:
        result = subprocess.run(
            ["nvidia-smi", "-q", "-x", "-i", str(gpu_index)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Unable to audit NVIDIA GPU process state") from error
    if result.returncode != 0:
        raise RuntimeError("nvidia-smi GPU process audit failed")
    try:
        return ET.fromstring(result.stdout)
    except ET.ParseError as error:
        raise RuntimeError("nvidia-smi GPU process audit returned malformed XML") from error


def _parse_used_memory_mib(value: str) -> int:
    match = re.fullmatch(r"(\d+) MiB", str(value).strip())
    if not match:
        raise RuntimeError(f"Malformed nvidia-smi process memory value: {value!r}")
    return int(match.group(1))


def is_reviewed_idle_graphics_process(
    *, pid: int, process_type: str, process_name: str, used_memory_mib: int
) -> bool:
    """Allow only exact system display processes with bounded GPU memory."""

    try:
        process_uid = Path(f"/proc/{pid}").stat().st_uid
        process_user = pwd.getpwuid(process_uid).pw_name
        limit = REVIEWED_IDLE_GRAPHICS_PROCESSES.get(
            (process_name, process_user, process_type)
        )
        binary = Path(process_name)
        binary_stat = binary.stat()
    except (KeyError, OSError):
        return False
    return (
        limit is not None
        and used_memory_mib <= limit
        and binary.is_file()
        and binary_stat.st_uid == 0
        and not binary_stat.st_mode & 0o022
    )


def require_gpu_idle(gpu_index: int) -> dict[str, object]:
    """Fail closed unless the selected GPU is idle enough for rendered UE."""

    if isinstance(gpu_index, bool) or not isinstance(gpu_index, int) or gpu_index < 0:
        raise RuntimeError("GPU index must be a non-negative integer")
    rows = _run_nvidia_smi_query(
        [
            "--query-gpu=index,uuid,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
    )
    selected = None
    for row in rows:
        fields = [field.strip() for field in row.split(",")]
        if len(fields) != 4:
            raise RuntimeError(f"Malformed nvidia-smi GPU state row: {row!r}")
        try:
            index = int(fields[0])
            memory_used_mib = int(fields[2])
            utilization_percent = int(fields[3])
        except ValueError as error:
            raise RuntimeError(f"Malformed nvidia-smi GPU state row: {row!r}") from error
        if index == gpu_index:
            selected = {
                "index": index,
                "uuid": fields[1],
                "memory_used_mib": memory_used_mib,
                "utilization_percent": utilization_percent,
            }
    if selected is None:
        raise RuntimeError(f"GPU index {gpu_index} disappeared during the idle audit")

    xml_root = _run_nvidia_smi_xml(gpu_index)
    xml_gpus = xml_root.findall("gpu")
    if len(xml_gpus) != 1 or xml_gpus[0].findtext("uuid") != selected["uuid"]:
        raise RuntimeError("nvidia-smi GPU process audit did not match the selected GPU")
    processes = xml_gpus[0].find("processes")
    if processes is None:
        raise RuntimeError("nvidia-smi GPU process audit omitted process information")
    active_processes = []
    for process in processes.findall("process_info"):
        try:
            pid = int(process.findtext("pid", ""))
        except (TypeError, ValueError) as error:
            raise RuntimeError("Malformed nvidia-smi GPU process PID") from error
        process_type = (process.findtext("type", "") or "").strip()
        process_name = (process.findtext("process_name", "") or "").strip()
        used_memory_mib = _parse_used_memory_mib(process.findtext("used_memory", ""))
        if not process_type or not process_name:
            raise RuntimeError("Malformed nvidia-smi GPU process information")
        if not is_reviewed_idle_graphics_process(
            pid=pid,
            process_type=process_type,
            process_name=process_name,
            used_memory_mib=used_memory_mib,
        ):
            active_processes.append((pid, process_type))
    if active_processes:
        raise RuntimeError(
            f"GPU {gpu_index} has active process IDs/types {active_processes}; "
            "refusing to interrupt them"
        )
    if selected["memory_used_mib"] > GPU_IDLE_MAX_MEMORY_MIB:
        raise RuntimeError(
            f"GPU {gpu_index} is using {selected['memory_used_mib']} MiB; "
            f"idle limit is {GPU_IDLE_MAX_MEMORY_MIB} MiB"
        )
    if selected["utilization_percent"] > GPU_IDLE_MAX_UTILIZATION_PERCENT:
        raise RuntimeError(
            f"GPU {gpu_index} utilization is {selected['utilization_percent']}%; "
            f"idle limit is {GPU_IDLE_MAX_UTILIZATION_PERCENT}%"
        )
    return selected


def get_server_ip():
    """Get the server's external IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def is_local_machine():
    """Check if we're likely on a local machine (has DISPLAY or Wayland)."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def wait_for_port(port, host="127.0.0.1", timeout=120, process=None):
    """Wait for a TCP port, aborting early if its child process exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(min(0.25, max(0.01, deadline - time.monotonic())))
                s.connect((host, port))
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            if process is not None and process.poll() is not None:
                return False
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    return False


def ue_fps_log_confirms(text: str, fps: int) -> bool:
    """Require the console response emitted by querying t.MaxFPS after setting it."""

    if fps not in (30, 60):
        raise ValueError("VISTA demo FPS must be 30 or 60")
    timestamp = (
        r"\[\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d{3}\]"
        r"\[[ \t]*\d+\]"
    )
    category = r"LogConsoleResponse:[ \t]+(?:Display:[ \t]+)?"
    pattern = re.compile(
        rf"(?m)^(?:{timestamp}[ \t]*(?:{category})?|{category})"
        rf't\.MaxFPS[ \t]*=[ \t]*"{fps}(?:\.0+)?"'
        rf"[ \t]+LastSetBy:[ \t]*Console[ \t]*\r?$"
    )
    return bool(pattern.search(text))


def wait_for_ue_fps_confirmation(
    log_path: Path, fps: int, process, timeout: float = 15
) -> bool:
    """Wait for a non-command-line log proof that the engine accepted t.MaxFPS."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if ue_fps_log_confirms(text, fps):
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    return False


def resolve_project_map(project_file: Path, map_path: str) -> str:
    """Validate a /Game asset path against a real map in project Content."""
    requested = (map_path or "/Game/Maps/Empty").strip()
    if requested.endswith(".umap"):
        requested = requested[:-5]
    if not requested.startswith("/Game/"):
        raise RuntimeError("UE map must be a /Game asset path")
    relative = requested.removeprefix("/Game/")
    if (
        not relative
        or "\\" in relative
        or "//" in relative
        or any(part in ("", ".", "..") for part in relative.split("/"))
    ):
        raise RuntimeError(f"Unsafe UE map path: {map_path}")
    content_root = (Path(project_file).parent / "Content").resolve()
    candidate = (content_root / f"{relative}.umap").resolve()
    try:
        candidate.relative_to(content_root)
    except ValueError as error:
        raise RuntimeError(f"UE map escapes project Content: {map_path}") from error
    if not candidate.is_file():
        raise RuntimeError(f"UE map does not exist in the pinned runtime: {requested}")
    return f"/Game/{relative}.umap"


def validate_vista_demo_assets(project_file: Path) -> None:
    """Require the exact independent UE 5.3 third-person template assets."""

    content_root = (Path(project_file).parent / "Content").resolve()
    for relative, expected_sha256 in VISTA_DEMO_ASSETS.items():
        candidate = (content_root / relative).resolve()
        try:
            candidate.relative_to(content_root)
        except ValueError as error:
            raise RuntimeError(f"VISTA demo asset escapes project Content: {relative}") from error
        if not candidate.is_file():
            raise RuntimeError(f"VISTA demo asset is missing: /Game/{relative}")
        if sha256_file(candidate) != expected_sha256:
            raise RuntimeError(f"VISTA demo asset SHA-256 mismatch: /Game/{relative}")
    for relative, expected_sha256 in VISTA_DEMO_ASSET_TREES.items():
        candidate = (content_root / relative).resolve()
        try:
            candidate.relative_to(content_root)
        except ValueError as error:
            raise RuntimeError(f"VISTA demo asset tree escapes project Content: {relative}") from error
        if sha256_tree(candidate) != expected_sha256:
            raise RuntimeError(f"VISTA demo asset tree SHA-256 mismatch: /Game/{relative}")


def validate_vista_demo_map(project_file: Path, ue_map: str) -> None:
    """Require the exact hash-pinned Empty map used by the demo contract."""

    normalized = ue_map.removesuffix(".umap")
    if normalized != VISTA_DEMO_MAP:
        raise RuntimeError(f"VISTA demo requires the fixed map {VISTA_DEMO_MAP}")
    content_root = (Path(project_file).parent / "Content").resolve()
    candidate = (content_root / "Maps" / "Empty.umap").resolve()
    try:
        candidate.relative_to(content_root)
    except ValueError as error:
        raise RuntimeError("VISTA demo map escapes project Content") from error
    if not candidate.is_file() or sha256_file(candidate) != VISTA_DEMO_MAP_SHA256:
        raise RuntimeError(f"VISTA demo map SHA-256 mismatch: {VISTA_DEMO_MAP}")


def build_ue_map_url(ue_map: str, vista_demo: bool) -> str:
    """Build the editor map argument; the demo broker pins GameMode before PIE."""

    if not vista_demo:
        return ue_map
    map_asset = ue_map.removesuffix(".umap")
    if map_asset != VISTA_DEMO_MAP:
        raise ValueError(f"VISTA demo requires the fixed map {VISTA_DEMO_MAP}")
    # UnrealEditor treats a ?game= suffix as part of the package name during
    # editor startup, then silently falls back to EditorStartupMap. The VISTA
    # runtime broker applies and verifies the fixed GameMode before starting PIE.
    return f"{map_asset}.umap"


def ue_startup_timeout_seconds(vista_demo: bool) -> int:
    """Allow the isolated demo DDC one bounded first-start shader compile."""

    if vista_demo:
        return VISTA_DEMO_COLD_UE_STARTUP_TIMEOUT_SECONDS
    return DEFAULT_UE_STARTUP_TIMEOUT_SECONDS


def make_ue_command(
    *,
    ue_editor: str,
    project_file: str,
    ue_map: str,
    mcp_port: int,
    gpu_index: int,
    cirrus_ws_port: int,
    fps: int,
    vista_demo: bool,
    local_data_cache_path: Optional[str] = None,
    user_dir: Optional[str] = None,
) -> list[str]:
    """Return the reviewed off-screen Pixel Streaming editor command."""

    if fps not in (30, 60):
        raise ValueError("Pixel Streaming FPS must be 30 or 60")
    if isinstance(gpu_index, bool) or not isinstance(gpu_index, int) or gpu_index < 0:
        raise ValueError("GPU index must be a non-negative integer")
    if vista_demo and not local_data_cache_path:
        raise ValueError("VISTA demo requires an isolated local data cache path")
    if vista_demo and not user_dir:
        raise ValueError("VISTA demo requires an isolated Unreal user directory")
    command = [
        ue_editor,
        project_file,
        build_ue_map_url(ue_map, vista_demo),
        f"-MCPPort={mcp_port}",
        "-Unattended",
        "-NOSPLASH",
        "-NOSOUND",
        "-NoAnalytics",
        "-ini:EditorSettings:[/Script/UnrealEd.AnalyticsPrivacySettings]:"
        "bSendUsageData=False",
        # Studio does not use Unreal's message bus. Keep its UDP multicast and
        # TCP transport disabled so an editor launch cannot join LAN interfaces.
        "-UDPMESSAGING_TRANSPORT_ENABLE=0",
        "-ini:Engine:[/Script/TcpMessaging.TcpMessagingSettings]:"
        "EnableTransport=False",
        "-ResX=1280",
        "-ResY=720",
        # Query the CVar after setting it so startup can fail closed on the
        # LogConsoleResponse proof instead of trusting an unsupported flag.
        f"-ExecCmds=t.MaxFPS {fps},t.MaxFPS",
        f"-graphicsadapter={gpu_index}",
        "-RenderOffScreen",
        "-EditorPixelStreamingRes=1280x720",
        "-EditorPixelStreamingStartOnLaunch=true",
        "-EditorPixelStreamingUseRemoteSignallingServer=true",
        f"-PixelStreamingWebRTCFps={fps}",
        f"-PixelStreamingURL=ws://127.0.0.1:{cirrus_ws_port}",
        "-log",
    ]
    if vista_demo:
        command.extend(
            [
                # These are process-local config overrides. NOWRITE prevents the
                # editor from persisting the value into the user's project config.
                # The published project carries stale generated platform INIs.
                # UE must refresh them in memory so BaseEngine's Linux Vulkan
                # TargetedRHIs reaches RHI startup. NOAUTOINIUPDATE rejects that
                # refresh and exits before Vulkan initializes. NOWRITE keeps the
                # refreshed config process-local instead of persisting it.
                "-NOWRITE",
                # The staged project enables Virtual Shadow Maps, but this
                # pinned Linux UE 5.3 build targets Vulkan SM5. Disable the
                # unsupported renderer feature process-locally so its startup
                # warning cannot capture the streamed editor controls.
                "-ini:Engine:[/Script/Engine.RendererSettings]:"
                "r.Shadow.Virtual.Enable=0",
                # Pixel Streaming UI actions can persist editor user settings
                # even with NOWRITE. Redirect ProjectSavedDir and generated
                # config into this release's private sandbox instead of the
                # shared pinned runtime project.
                "-SaveToUserDir",
                f"-UserDir={Path(user_dir).resolve()}",
                "-ini:EditorPerProjectUserSettings:"
                "[/Script/UnrealEd.EditorLoadingSavingSettings]:bAutoSaveEnable=False",
                f"-LocalDataCachePath={Path(local_data_cache_path).resolve()}",
            ]
        )
    return command


def configure_vista_demo_server_environment(
    environment: dict[str, str], *, vista_demo: bool, fps: int
) -> dict[str, str]:
    """Publish only the validated demo frame-rate contract to the web server."""

    if fps not in (30, 60):
        raise ValueError("VISTA demo FPS must be 30 or 60")
    environment["VISTA_DEMO_ENABLED"] = "1" if vista_demo else "0"
    if vista_demo:
        environment["VISTA_DEMO_FPS"] = str(fps)
    else:
        environment.pop("VISTA_DEMO_FPS", None)
    return environment


def has_unrealcv_plugin(ue_root: Path, project_file: Path) -> bool:
    """Detect a loadable UnrealCV plugin without scanning the full runtime."""
    project_root = Path(project_file).parent
    candidates = (
        project_root / "Plugins" / "UnrealCV" / "UnrealCV.uplugin",
        Path(ue_root) / "Engine" / "Plugins" / "UnrealCV" / "UnrealCV.uplugin",
        Path(ue_root) / "Engine" / "Plugins" / "Marketplace" / "UnrealCV" / "UnrealCV.uplugin",
        Path(ue_root) / "Engine" / "Plugins" / "Runtime" / "UnrealCV" / "UnrealCV.uplugin",
    )
    return any(
        descriptor.is_file()
        and any((descriptor.parent / "Binaries" / "Linux").glob("*UnrealCV*.so"))
        for descriptor in candidates
    )


def _private_directory_flags() -> int:
    if any(not hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")):
        raise RuntimeError("Private workspace directories require no-follow directory descriptors")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _parse_private_relative(relative: str) -> PurePosixPath:
    if not isinstance(relative, str) or not relative:
        raise RuntimeError(f"Private workspace directory is invalid: {relative}")
    parsed = PurePosixPath(relative)
    if (
        relative in {".", ".."}
        or parsed.as_posix() != relative
        or parsed.is_absolute()
        or not parsed.parts
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise RuntimeError(f"Private workspace directory is invalid: {relative}")
    return parsed


def _close_descriptors(descriptors: list[int], primary_error: BaseException | None) -> None:
    """Close every tracked descriptor without replacing an in-flight failure."""

    cleanup_error = None
    while descriptors:
        descriptor = descriptors.pop()
        try:
            os.close(descriptor)
        except OSError as error:
            if cleanup_error is None:
                cleanup_error = error
    if cleanup_error is not None and primary_error is None:
        raise cleanup_error


def _prepare_private_directory_fd(descriptor: int, label: str) -> tuple[int, int]:
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid():
        raise RuntimeError(f"Private workspace directory is not owner-controlled: {label}")
    os.fchmod(descriptor, PRIVATE_WORKSPACE_DIRECTORY_MODE)
    os.fsync(descriptor)
    checked = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(checked.st_mode)
        or checked.st_uid != os.geteuid()
        or stat.S_IMODE(checked.st_mode) != PRIVATE_WORKSPACE_DIRECTORY_MODE
        or (checked.st_dev, checked.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise RuntimeError(f"Private workspace directory mode or identity is invalid: {label}")
    return opened.st_dev, opened.st_ino


def _canonical_private_absolute_path(value: Path | str, label: str) -> tuple[Path, tuple[str, ...]]:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\0" in raw or raw.startswith("//"):
        raise RuntimeError(f"{label} is invalid")
    if os.path.isabs(raw):
        if os.path.normpath(raw) != raw:
            raise RuntimeError(f"{label} is not canonical: {raw}")
        checked = Path(raw)
    else:
        parsed = _parse_private_relative(raw)
        checked = Path(os.getcwd()).joinpath(*parsed.parts)
    if checked == Path(checked.anchor) or not checked.is_absolute():
        raise RuntimeError(f"{label} must not be the filesystem root")
    parsed_absolute = PurePosixPath(os.fspath(checked))
    if parsed_absolute.as_posix() != os.fspath(checked):
        raise RuntimeError(f"{label} is not canonical: {checked}")
    return checked, tuple(parsed_absolute.parts[1:])


def _open_absolute_directory_authority(
    value: Path | str,
    *,
    label: str,
    require_owner: bool,
    private: bool,
) -> dict:
    """Open an absolute directory one no-follow component at a time from `/`."""

    checked, parts = _canonical_private_absolute_path(value, label)
    flags = _private_directory_flags()
    descriptors: list[int] = []
    primary_error = None
    try:
        anchor_fd = os.open("/", flags)
        descriptors.append(anchor_fd)
        anchor_status = os.fstat(anchor_fd)
        if not stat.S_ISDIR(anchor_status.st_mode):
            raise RuntimeError(f"{label} trusted root is unavailable")
        anchor_identity = (anchor_status.st_dev, anchor_status.st_ino)
        current_fd = os.dup(anchor_fd)
        descriptors.append(current_fd)
        identities = []
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as error:
                raise RuntimeError(f"{label} is unavailable or unsafe: {checked}") from error
            descriptors.append(next_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                raise RuntimeError(f"{label} is unavailable or unsafe: {checked}")
            identities.append((opened.st_dev, opened.st_ino))
            os.close(current_fd)
            descriptors.remove(current_fd)
            current_fd = next_fd
        target_status = os.fstat(current_fd)
        if require_owner and target_status.st_uid != os.geteuid():
            raise RuntimeError(f"{label} is not owner-controlled: {checked}")
        if private:
            os.fchmod(current_fd, PRIVATE_WORKSPACE_DIRECTORY_MODE)
            os.fsync(current_fd)
            target_status = os.fstat(current_fd)
            if (
                target_status.st_uid != os.geteuid()
                or stat.S_IMODE(target_status.st_mode) != PRIVATE_WORKSPACE_DIRECTORY_MODE
            ):
                raise RuntimeError(f"{label} is not private: {checked}")
        target_identity = (target_status.st_dev, target_status.st_ino)
        identities[-1] = target_identity
        descriptors.remove(anchor_fd)
        descriptors.remove(current_fd)
        return {
            "path": checked,
            "parts": parts,
            "anchor_fd": anchor_fd,
            "anchor_identity": anchor_identity,
            "directory_fd": current_fd,
            "identities": tuple(identities),
            "target_identity": target_identity,
            "require_owner": require_owner,
            "private": private,
            "label": label,
        }
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(descriptors, primary_error)


def _revalidate_absolute_directory_authority(authority: dict) -> None:
    """Rewalk an authority from its held `/` descriptor and compare every inode."""

    flags = _private_directory_flags()
    descriptors: list[int] = []
    primary_error = None
    try:
        anchor_status = os.fstat(authority["anchor_fd"])
        if (
            not stat.S_ISDIR(anchor_status.st_mode)
            or (anchor_status.st_dev, anchor_status.st_ino) != authority["anchor_identity"]
        ):
            raise RuntimeError(f'{authority["label"]} trusted root changed')
        current_fd = os.dup(authority["anchor_fd"])
        descriptors.append(current_fd)
        for index, part in enumerate(authority["parts"]):
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as error:
                raise RuntimeError(f'{authority["label"]} changed during use') from error
            descriptors.append(next_fd)
            opened = os.fstat(next_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != authority["identities"][index]
            ):
                raise RuntimeError(f'{authority["label"]} changed during use')
            os.close(current_fd)
            descriptors.remove(current_fd)
            current_fd = next_fd
        target_status = os.fstat(authority["directory_fd"])
        walked_status = os.fstat(current_fd)
        for checked_status in (target_status, walked_status):
            if (
                not stat.S_ISDIR(checked_status.st_mode)
                or (checked_status.st_dev, checked_status.st_ino) != authority["target_identity"]
                or (authority["require_owner"] and checked_status.st_uid != os.geteuid())
                or (
                    authority["private"]
                    and stat.S_IMODE(checked_status.st_mode) != PRIVATE_WORKSPACE_DIRECTORY_MODE
                )
            ):
                raise RuntimeError(f'{authority["label"]} changed during use')
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(descriptors, primary_error)


def _open_private_directory_chain(
    root_fd: int,
    relative: str,
    expected_identities: dict[tuple[str, ...], tuple[int, int]] | None = None,
) -> tuple[int, dict[tuple[str, ...], tuple[int, int]]]:
    """Create and open a relative directory chain from an already trusted root."""

    parsed = _parse_private_relative(relative)
    flags = _private_directory_flags()
    descriptors: list[int] = []
    identities: dict[tuple[str, ...], tuple[int, int]] = {}
    primary_error = None
    try:
        current_fd = os.dup(root_fd)
        descriptors.append(current_fd)
        prefix: list[str] = []
        for part in parsed.parts:
            created = False
            try:
                os.mkdir(part, mode=PRIVATE_WORKSPACE_DIRECTORY_MODE, dir_fd=current_fd)
                created = True
            except FileExistsError:
                pass
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as error:
                raise RuntimeError(
                    f"Private workspace directory is unavailable or unsafe: {relative}"
                ) from error
            descriptors.append(next_fd)
            prefix.append(part)
            opened = os.fstat(next_fd)
            expected = (expected_identities or {}).get(tuple(prefix))
            if expected is not None and (opened.st_dev, opened.st_ino) != expected:
                raise RuntimeError(f"Private workspace directory changed: {relative}")
            identity = _prepare_private_directory_fd(next_fd, relative)
            identities[tuple(prefix)] = identity
            if created:
                os.fsync(current_fd)
            os.close(current_fd)
            descriptors.remove(current_fd)
            current_fd = next_fd
        descriptors.remove(current_fd)
        return current_fd, identities
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(descriptors, primary_error)


def _revalidate_directory_identities(
    root_fd: int,
    identities: dict[tuple[str, ...], tuple[int, int]],
    label: str,
) -> None:
    """Re-open every path component no-follow and compare its original inode."""

    flags = _private_directory_flags()
    for parts, expected in sorted(identities.items(), key=lambda item: item[0]):
        descriptors: list[int] = []
        primary_error = None
        try:
            current_fd = os.dup(root_fd)
            descriptors.append(current_fd)
            for part in parts:
                next_fd = os.open(part, flags, dir_fd=current_fd)
                descriptors.append(next_fd)
                os.close(current_fd)
                descriptors.remove(current_fd)
                current_fd = next_fd
            checked = os.fstat(current_fd)
            if (
                not stat.S_ISDIR(checked.st_mode)
                or checked.st_uid != os.geteuid()
                or (checked.st_dev, checked.st_ino) != expected
            ):
                raise RuntimeError(f"Private workspace directory changed: {label}")
        except BaseException as error:
            primary_error = error
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"Private workspace directory changed: {label}") from error
        finally:
            _close_descriptors(descriptors, primary_error)


def ensure_private_workspace_directory(workspace: Path, relative: str) -> Path:
    """Create one owner-only workspace directory without following symlinks."""

    parsed = _parse_private_relative(relative)
    descriptors: list[int] = []
    authority = None
    primary_error = None
    try:
        authority = _open_absolute_directory_authority(
            workspace,
            label="Private workspace root",
            require_owner=True,
            private=True,
        )
        descriptors.extend([authority["anchor_fd"], authority["directory_fd"]])
        leaf_fd, identities = _open_private_directory_chain(
            authority["directory_fd"],
            relative,
        )
        descriptors.append(leaf_fd)
        _revalidate_directory_identities(authority["directory_fd"], identities, relative)
        _revalidate_absolute_directory_authority(authority)
        return authority["path"].joinpath(*parsed.parts)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(descriptors, primary_error)


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Private workspace write made no progress")
        remaining = remaining[written:]


def _atomic_write_at(
    directory_fd: int,
    name: str,
    content: bytes,
    *,
    mode: int,
    timestamps_ns: tuple[int, int] | None = None,
) -> None:
    """Replace one leaf through its parent descriptor without following it."""

    if not name or name in {".", ".."} or "/" in name:
        raise RuntimeError(f"Private workspace file name is invalid: {name}")
    temporary = f".{name}.tmp-{secrets.token_hex(8)}"
    descriptors: list[int] = []
    primary_error = None
    installed = False
    try:
        temporary_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=directory_fd,
        )
        descriptors.append(temporary_fd)
        _write_all(temporary_fd, content)
        os.fchmod(temporary_fd, mode)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        descriptors.remove(temporary_fd)
        if timestamps_ns is not None:
            os.utime(
                temporary,
                ns=timestamps_ns,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        installed = True
        os.fsync(directory_fd)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(descriptors, primary_error)
        if not installed:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                if primary_error is None:
                    raise


def _copy_file_at(source: Path, directory_fd: int, name: str) -> None:
    descriptors: list[int] = []
    primary_error = None
    try:
        source_fd = os.open(Path(source), os.O_RDONLY | os.O_NOFOLLOW)
        descriptors.append(source_fd)
        source_status = os.fstat(source_fd)
        if not stat.S_ISREG(source_status.st_mode):
            raise RuntimeError(f"Package source is not a regular file: {source}")
        chunks = []
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        _atomic_write_at(
            directory_fd,
            name,
            b"".join(chunks),
            mode=stat.S_IMODE(source_status.st_mode),
            timestamps_ns=(source_status.st_atime_ns, source_status.st_mtime_ns),
        )
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(descriptors, primary_error)


def _remove_directory_contents_at(directory_fd: int) -> None:
    """Remove a directory's contents without traversing a symlink entry."""

    flags = _private_directory_flags()
    for name in os.listdir(directory_fd):
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode) and not stat.S_ISLNK(entry.st_mode):
            descriptors: list[int] = []
            primary_error = None
            try:
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                descriptors.append(child_fd)
                _remove_directory_contents_at(child_fd)
            except BaseException as error:
                primary_error = error
                raise
            finally:
                _close_descriptors(descriptors, primary_error)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _copy_tree_at(source: Path, directory_fd: int) -> None:
    """Copy a trusted package tree into a descriptor-anchored destination."""

    flags = _private_directory_flags()
    with os.scandir(source) as entries:
        for entry in entries:
            source_path = Path(entry.path)
            if entry.is_symlink():
                raise RuntimeError(f"Package tree cannot contain symlinks: {source_path}")
            if entry.is_dir(follow_symlinks=False):
                try:
                    os.mkdir(
                        entry.name,
                        mode=PRIVATE_WORKSPACE_DIRECTORY_MODE,
                        dir_fd=directory_fd,
                    )
                    os.fsync(directory_fd)
                except FileExistsError:
                    pass
                descriptors: list[int] = []
                primary_error = None
                try:
                    child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                    descriptors.append(child_fd)
                    _prepare_private_directory_fd(child_fd, entry.name)
                    _copy_tree_at(source_path, child_fd)
                except BaseException as error:
                    primary_error = error
                    raise
                finally:
                    _close_descriptors(descriptors, primary_error)
            elif entry.is_file(follow_symlinks=False):
                _copy_file_at(source_path, directory_fd, entry.name)
            else:
                raise RuntimeError(f"Package tree has an unsupported entry: {source_path}")
    os.fsync(directory_fd)


def _read_version_at(workspace_fd: int) -> str:
    descriptors: list[int] = []
    primary_error = None
    try:
        try:
            version_fd = os.open(
                ".studio_version",
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=workspace_fd,
            )
        except FileNotFoundError:
            return ""
        except OSError as error:
            raise RuntimeError("Workspace version file is unavailable or unsafe") from error
        descriptors.append(version_fd)
        version_status = os.fstat(version_fd)
        if not stat.S_ISREG(version_status.st_mode):
            raise RuntimeError("Workspace version file is unavailable or unsafe")
        content = os.read(version_fd, 4097)
        if len(content) > 4096:
            raise RuntimeError("Workspace version file is invalid")
        return content.decode("utf-8").strip()
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(descriptors, primary_error)


def _entry_is_regular_at(directory_fd: int, name: str) -> bool:
    try:
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(entry.st_mode):
        raise RuntimeError(f"Private workspace file is unavailable or unsafe: {name}")
    return True


def _entry_is_directory_at(directory_fd: int, name: str) -> bool:
    try:
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
        raise RuntimeError(f"Private workspace directory is unavailable or unsafe: {name}")
    return True


def setup_workspace(workspace, pkg_dir):
    """
    Create/update workspace with bundled files.
    Only copies files if they're missing or package version changed.
    """
    workspace_path, workspace_parts = _canonical_private_absolute_path(
        workspace,
        "Workspace",
    )
    if len(workspace_parts) < 2:
        raise RuntimeError("Workspace parent must be an owner-controlled non-root directory")
    _parse_private_relative(workspace_path.name)
    workspace_parent = workspace_path.parent
    workspace = workspace_parent / workspace_path.name
    pkg_dir = Path(pkg_dir)
    flags = _private_directory_flags()
    descriptors: list[int] = []
    identities: dict[tuple[str, ...], tuple[int, int]] = {}
    parent_authority = None
    primary_error = None
    try:
        parent_authority = _open_absolute_directory_authority(
            workspace_parent,
            label="Workspace parent",
            require_owner=True,
            private=False,
        )
        parent_fd = parent_authority["directory_fd"]
        descriptors.extend([parent_authority["anchor_fd"], parent_fd])
        created_workspace = False
        try:
            os.mkdir(
                workspace_path.name,
                mode=PRIVATE_WORKSPACE_DIRECTORY_MODE,
                dir_fd=parent_fd,
            )
            created_workspace = True
        except FileExistsError:
            pass
        try:
            workspace_fd = os.open(workspace_path.name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise RuntimeError(f"Workspace is unavailable or unsafe: {workspace}") from error
        descriptors.append(workspace_fd)
        workspace_identity = _prepare_private_directory_fd(workspace_fd, str(workspace))
        if created_workspace:
            os.fsync(parent_fd)

        target_fds: dict[str, int] = {}
        target_directories = {
            "web/server",
            "web/dist",
            "arena/skills/builtin",
            "arena/config",
        }
        for relative in [
            "web/server",
            "web/dist",
            "arena/skills/builtin",
            "arena/config",
            "scenes",
            "skills",
            "tmp/screens",
            "tmp/thumbnails",
            "logs",
            "arena_data",
            *PRIVATE_REVIEW_EVIDENCE_DIRS,
        ]:
            directory_fd, opened_identities = _open_private_directory_chain(
                workspace_fd,
                relative,
                identities,
            )
            descriptors.append(directory_fd)
            for parts, identity in opened_identities.items():
                expected = identities.get(parts)
                if expected is not None and expected != identity:
                    raise RuntimeError(f"Private workspace directory changed: {relative}")
                identities[parts] = identity
            if relative in target_directories:
                target_fds[relative] = directory_fd
            else:
                os.close(directory_fd)
                descriptors.remove(directory_fd)

        current_version = _read_version_at(workspace_fd)
        needs_update = current_version != __version__
        server_fd = target_fds["web/server"]

        if needs_update:
            print(f"  Setting up workspace (v{__version__})...")

            server_src = pkg_dir / "server"
            if server_src.exists():
                if server_src.is_symlink() or not server_src.is_dir():
                    raise RuntimeError(f"Package server source is unsafe: {server_src}")
                for source in server_src.iterdir():
                    if source.is_file():
                        _copy_file_at(source, server_fd, source.name)

            dist_src = pkg_dir / "server" / "dist"
            if dist_src.exists():
                if dist_src.is_symlink() or not dist_src.is_dir():
                    raise RuntimeError(f"Package frontend source is unsafe: {dist_src}")
                dist_fd = target_fds["web/dist"]
                _remove_directory_contents_at(dist_fd)
                _copy_tree_at(dist_src, dist_fd)

            skills_src = pkg_dir / "skills" / "builtin"
            if skills_src.exists():
                if skills_src.is_symlink() or not skills_src.is_dir():
                    raise RuntimeError(f"Package skills source is unsafe: {skills_src}")
                skills_fd = target_fds["arena/skills/builtin"]
                for source in skills_src.glob("*.md"):
                    _copy_file_at(source, skills_fd, source.name)

            config_src = pkg_dir / "config"
            if config_src.exists():
                if config_src.is_symlink() or not config_src.is_dir():
                    raise RuntimeError(f"Package config source is unsafe: {config_src}")
                config_fd = target_fds["arena/config"]
                for source in config_src.iterdir():
                    if source.is_file():
                        _copy_file_at(source, config_fd, source.name)

        package_json_exists = _entry_is_regular_at(server_fd, "package.json")
        node_modules_exists = _entry_is_directory_at(server_fd, "node_modules")
        if package_json_exists and not node_modules_exists:
            if needs_update:
                print("  Installing Node.js dependencies...")
            subprocess.run(
                ["npm", "install", "--production", "--no-optional", "--no-audit", "--no-fund"],
                cwd=f"/proc/self/fd/{server_fd}",
                pass_fds=(server_fd,),
                capture_output=True,
            )

        if needs_update:
            _atomic_write_at(
                workspace_fd,
                ".studio_version",
                __version__.encode("utf-8"),
                mode=0o600,
            )

        _revalidate_absolute_directory_authority(parent_authority)
        try:
            lexical_workspace = os.stat(
                workspace_path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            opened_workspace = os.fstat(workspace_fd)
        except OSError as error:
            raise RuntimeError(f"Workspace changed during setup: {workspace}") from error
        if (
            not stat.S_ISDIR(lexical_workspace.st_mode)
            or stat.S_ISLNK(lexical_workspace.st_mode)
            or (lexical_workspace.st_dev, lexical_workspace.st_ino) != workspace_identity
            or not stat.S_ISDIR(opened_workspace.st_mode)
            or (opened_workspace.st_dev, opened_workspace.st_ino) != workspace_identity
            or opened_workspace.st_uid != os.geteuid()
            or stat.S_IMODE(opened_workspace.st_mode) != PRIVATE_WORKSPACE_DIRECTORY_MODE
        ):
            raise RuntimeError(f"Workspace changed during setup: {workspace}")
        _revalidate_directory_identities(workspace_fd, identities, str(workspace))
        return workspace
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(descriptors, primary_error)


def generate_mcp_config(workspace, ue_host, ue_port, *, enabled=False):
    """Generate the agent MCP config; model-off uses an empty server list."""
    mcp_server_path = str(Path(workspace) / "web" / "server" / "mcp-server.js")
    if enabled:
        config = {
            "mcpServers": {
                "simworld": {
                    "command": "node",
                    "args": [mcp_server_path],
                    "env": {
                        "UNREAL_HOST": ue_host,
                        "UNREAL_PORT": str(ue_port),
                    },
                }
            }
        }
    else:
        config = {"mcpServers": {}}
    config_path = Path(workspace) / "web" / "mcp.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return str(config_path)


def find_simworld_binary(binary_path=None):
    """Find the SimWorld binary directory or UE installation.
    
    Supports:
    1. Command line argument --binary
    2. Environment variable UE_ROOT (for local UE installation)
    3. SimWorld-Studio-Minimal in common locations
    """
    search_paths = []
    
    # Priority 1: Explicit command line argument
    if binary_path:
        explicit = Path(binary_path)
        if (explicit / "Engine" / "Binaries" / "Linux" / "UnrealEditor").exists():
            return explicit
        return None

    # Priority 2: Environment variable UE_ROOT (for local UE installation)
    ue_root = os.environ.get("UE_ROOT")
    if ue_root:
        ue_root_path = Path(ue_root)
        if (ue_root_path / "Engine" / "Binaries" / "Linux" / "UnrealEditor").exists():
            return ue_root_path

    # Priority 3: Common locations for SimWorld-Studio-Minimal
    search_paths.extend([
        Path.cwd() / "SimWorld-Studio-Minimal",
        Path.cwd(),
        Path.home() / "SimWorld-Studio-Minimal",
    ])

    for p in search_paths:
        if (p / "Engine" / "Binaries" / "Linux" / "UnrealEditor").exists():
            return p
    return None


def find_ue_project(ue_root_path):
    """Find the UE project file.
    
    Supports:
    1. Environment variable UE_PROJECT_PATH (for local project)
    2. Default gym_citynav project in SimWorld-Studio-Minimal
    """
    # Priority 1: Environment variable UE_PROJECT_PATH
    ue_project_path = os.environ.get("UE_PROJECT_PATH")
    if ue_project_path:
        project_path = Path(ue_project_path)
        # If it's a directory, look for .uproject file
        if project_path.is_dir():
            uproject_files = list(project_path.glob("*.uproject"))
            if uproject_files:
                return str(uproject_files[0])
        # If it's already a .uproject file
        elif project_path.is_file() and project_path.suffix == ".uproject":
            return str(project_path)
        # If it's a path to a project directory
        elif project_path.exists():
            uproject_files = list(project_path.glob("*.uproject"))
            if uproject_files:
                return str(uproject_files[0])
    
    # Priority 2: Default gym_citynav project (for SimWorld-Studio-Minimal)
    default_project = ue_root_path / "gym_citynav" / "gym_citynav.uproject"
    if default_project.exists():
        return str(default_project)
    
    return None


def start_server(args):
    """Start everything: UE binary + Studio web server."""
    node = find_node()

    print()
    print("=" * 55)
    print("  SimWorld Studio v" + __version__)
    print("=" * 55)

    if args.model_mode != "off" or args.mock:
        print("  [!!] This T2 secure launcher currently permits only --model-mode off")
        print("       Mock replay is incomplete upstream; live agents remain confinement-gated")
        sys.exit(1)

    unrealcv_port = int(os.environ.get("UNREALCV_PORT", os.environ.get("UCV_PORT", "9000")))
    access_token = secrets.token_urlsafe(32)
    requested_ports = {
        "web": args.port,
        "mcp": args.mcp_port,
        "cirrus_http": args.cirrus_http_port,
        "cirrus_streamer": args.cirrus_ws_port,
        "cirrus_sfu": args.cirrus_sfu_port,
    }

    # ── Step 1: Check model mode and authentication ──
    print()
    if args.model_mode == "off":
        print("  [OK] Model mode: off (no external agent/model execution)")
    else:
        auth = check_claude_auth()
        if auth == "api_key":
            print("  [OK] Claude auth: API key")
        elif auth == "oauth":
            print("  [OK] Claude auth: OAuth (claude login)")
        else:
            print("  [!!] Claude not authenticated!")
            print("       Set ANTHROPIC_API_KEY or run 'claude login'")
            if not args.skip_auth_check:
                sys.exit(1)

    # ── Step 2: Detect GPU ──
    gpus = detect_gpu()
    if gpus:
        print(f"  [OK] GPU: {len(gpus)} detected")
        for g in gpus:
            print(f"       - {g}")
    else:
        print("  [!!] No NVIDIA GPU detected. UE requires a GPU.")
        print("       Rendered launches cannot bypass the fail-closed GPU audit")
        sys.exit(1)

    available_gpu_indices = detected_gpu_indices(gpus)
    gpu_index = args.gpu
    if gpu_index is None and len(gpus) > 1:
        print()
        print(f"  Multiple GPUs detected. Choose one of: {available_gpu_indices}")
        try:
            default_gpu = available_gpu_indices[0]
            gpu_index = int(
                input(f"  GPU index (default {default_gpu}): ").strip() or str(default_gpu)
            )
        except (ValueError, EOFError):
            gpu_index = available_gpu_indices[0]
    elif gpu_index is None:
        gpu_index = available_gpu_indices[0]
    try:
        validate_gpu_index(gpu_index, gpus)
    except RuntimeError as error:
        print(f"  [!!] GPU selection failed: {error}")
        sys.exit(1)

    # ── Step 3: Find SimWorld binary or UE installation ──
    binary_dir = find_simworld_binary(args.binary)
    if not binary_dir:
        print("  [!!] UE binary not found!")
        print()
        print("       Option 1: Use local UE installation (recommended)")
        print("       Set environment variables:")
        print("         export UE_ROOT=/path/to/UE_5.3.2")
        print("         export UE_PROJECT_PATH=/path/to/your/project")
        print()
        print("       Option 2: Download SimWorld-Studio-Minimal")
        print("       Use the T2 pinned archive revision 26bdd2ca18f06ab455023b0a602ede60b3afb243")
        print("       and verify SHA-256 806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f")
        sys.exit(1)
    print(f"  [OK] UE Root: {binary_dir}")

    # ── Step 4: Validate the separately staged source workspace ──
    if not args.data_dir:
        print("  [!!] Secure bring-up requires --data-dir pointing to a prepared source workspace")
        sys.exit(1)
    try:
        workspace = validate_prepared_workspace(Path(args.data_dir))
    except RuntimeError as error:
        print(f"  [!!] {error}")
        sys.exit(1)
    generate_mcp_config(workspace, "127.0.0.1", str(args.mcp_port), enabled=False)
    print(f"  [OK] Workspace: {workspace}")

    project_file = find_ue_project(binary_dir)
    if not project_file:
        print("  [!!] UE project file not found!")
        print("       Set UE_PROJECT_PATH or use a verified SimWorld Minimal runtime")
        sys.exit(1)
    print(f"  [OK] Project: {project_file}")
    try:
        ue_map = resolve_project_map(Path(project_file), args.map)
        if args.vista_demo:
            validate_vista_demo_map(Path(project_file), ue_map)
            validate_vista_demo_assets(Path(project_file))
    except RuntimeError as error:
        print(f"  [!!] {error}")
        sys.exit(1)
    print(f"  [OK] Map: {ue_map}")
    if args.vista_demo:
        print(f"  [OK] VISTA demo GameMode: {VISTA_DEMO_GAME_MODE}")

    unrealcv_available = has_unrealcv_plugin(binary_dir, Path(project_file))
    if args.unrealcv_mode == "required" and not unrealcv_available:
        print("  [!!] --unrealcv-mode required but the pinned Minimal runtime has no UnrealCV plugin binary")
        sys.exit(1)
    unrealcv_enabled = unrealcv_available
    if unrealcv_enabled:
        requested_ports["unrealcv"] = unrealcv_port
        sync_unrealcv_port_in_saved_ini(Path(project_file).parent, unrealcv_port)
        print(f"  [OK] UnrealCV plugin: enabled on loopback port {unrealcv_port}")
    else:
        print("  [OK] UnrealCV plugin: unavailable/disabled; Studio broker will stay off")

    try:
        require_ports_free(requested_ports)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"  [!!] Port preflight failed: {error}")
        sys.exit(1)
    try:
        gpu_state = require_gpu_idle(gpu_index)
    except RuntimeError as error:
        print(f"  [!!] GPU idle preflight failed: {error}")
        sys.exit(1)
    print(
        f"  [OK] GPU {gpu_index} idle: {gpu_state['memory_used_mib']} MiB, "
        f"{gpu_state['utilization_percent']}% utilization"
    )

    try:
        if args.vista_demo:
            base_child_environment = prepare_isolated_demo_environment(workspace)
        else:
            base_child_environment = make_model_off_child_environment()
    except RuntimeError as error:
        print(f"  [!!] Child environment isolation failed: {error}")
        sys.exit(1)

    managed_processes = []
    managed_files = []
    atexit.register(cleanup_managed_processes, managed_processes, managed_files)

    # ── Step 5: Start Cirrus signaling server ──
    print()
    cirrus_dir = binary_dir / "Engine" / "Plugins" / "Media" / "PixelStreaming" / "Resources" / "WebServers" / "SignallingWebServer"
    cirrus_js = cirrus_dir / "cirrus.js"
    cirrus_proc = None

    if cirrus_js.exists():
        try:
            validate_cirrus_loopback_patch(cirrus_js)
        except RuntimeError as error:
            print(f"  [!!] {error}")
            sys.exit(1)
        cirrus_config = make_cirrus_config(args)
        cirrus_config_path = workspace / "cirrus-config.json"
        cirrus_config_path.write_text(json.dumps(cirrus_config, indent=2))

        cirrus_log = workspace / "logs" / "cirrus.log"
        cirrus_log_file = open(cirrus_log, "w")
        managed_files.append(cirrus_log_file)
        cirrus_env = dict(base_child_environment)
        cirrus_env["STUDIO_ACCESS_TOKEN"] = access_token
        validate_model_off_child_environment(cirrus_env)
        cirrus_proc = start_managed_process(
            [node, str(cirrus_js), f"--configFile={cirrus_config_path}"],
            managed_processes,
            cwd=str(cirrus_dir),
            env=cirrus_env,
            stdout=cirrus_log_file,
            stderr=subprocess.STDOUT,
        )
        cirrus_ports = {
            "cirrus_http": args.cirrus_http_port,
            "cirrus_streamer": args.cirrus_ws_port,
            "cirrus_sfu": args.cirrus_sfu_port,
        }
        if (
            any(
                not wait_for_port(port, timeout=30, process=cirrus_proc)
                for port in cirrus_ports.values()
            )
            or cirrus_proc.poll() is not None
        ):
            print("  [!!] Cirrus failed readiness checks")
            sys.exit(1)
        try:
            require_loopback_listeners(cirrus_ports)
            require_cirrus_http_auth(args.cirrus_http_port, access_token)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"  [!!] Cirrus listener audit failed: {error}")
            sys.exit(1)
        print(f"  [OK] Cirrus signaling server (HTTP :{args.cirrus_http_port}, WS :{args.cirrus_ws_port})")
    else:
        print("  [!!] Cirrus not found; secure Pixel Streaming is required")
        sys.exit(1)

    # ── Step 6: Launch UE ──
    print("  Launching Unreal Engine (headless)...")
    ue_editor = str(binary_dir / "Engine" / "Binaries" / "Linux" / "UnrealEditor")
    
    ue_env = dict(base_child_environment)
    ue_env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
    try:
        nvidia_icd = get_nvidia_headless_icd()
    except RuntimeError as error:
        print(f"  [!!] {error}")
        sys.exit(1)
    ue_env["VK_ICD_FILENAMES"] = str(nvidia_icd)
    try:
        nvidia_compat_dir = prepare_nvidia_compat_libraries(workspace)
    except RuntimeError as error:
        print(f"  [!!] {error}")
        sys.exit(1)
    inherited_library_path = ue_env.get("LD_LIBRARY_PATH")
    ue_env["LD_LIBRARY_PATH"] = str(nvidia_compat_dir)
    if inherited_library_path:
        ue_env["LD_LIBRARY_PATH"] += os.pathsep + inherited_library_path
    validate_model_off_child_environment(ue_env)

    try:
        require_gpu_idle(gpu_index)
    except RuntimeError as error:
        print(f"  [!!] GPU became busy before UE launch: {error}")
        sys.exit(1)

    ue_log = workspace / "logs" / "ue.log"

    ue_cmd = make_ue_command(
        ue_editor=ue_editor,
        project_file=project_file,
        ue_map=ue_map,
        mcp_port=args.mcp_port,
        gpu_index=gpu_index,
        cirrus_ws_port=args.cirrus_ws_port,
        fps=args.fps,
        vista_demo=args.vista_demo,
        local_data_cache_path=(
            str(Path(base_child_environment["XDG_CACHE_HOME"]) / "UnrealEngine" / "DDC")
            if args.vista_demo
            else None
        ),
        user_dir=(
            str(Path(base_child_environment["HOME"]).parent / "ue-user")
            if args.vista_demo
            else None
        ),
    )

    ue_log_file = open(ue_log, "w")
    managed_files.append(ue_log_file)
    ue_proc = start_managed_process(
        ue_cmd,
        managed_processes,
        env=ue_env,
        stdout=ue_log_file,
        stderr=subprocess.STDOUT,
    )
    print(f"  UE PID: {ue_proc.pid} (log: {ue_log})")
    print(f"  Map: {build_ue_map_url(ue_map, args.vista_demo)}")
    print(f"  Render profile: 1280x720 @ {args.fps} fps")

    # ── Step 6: Wait for MCP port ──
    print(f"  Waiting for MCP port {args.mcp_port}...", end="", flush=True)
    if wait_for_port(
        args.mcp_port,
        timeout=ue_startup_timeout_seconds(args.vista_demo),
        process=ue_proc,
    ):
        print(" ready!")
    else:
        returncode = ue_proc.poll()
        if returncode is not None:
            print(f" FAILED! UE exited with code {returncode}.")
        else:
            print(" TIMEOUT!")
            terminate_managed_process_group(ue_proc)
        print(f"  Check UE log: {ue_log}")
        sys.exit(1)
    if args.vista_demo:
        if not wait_for_ue_fps_confirmation(ue_log, args.fps, ue_proc):
            print(
                f"  [!!] UE log did not confirm t.MaxFPS={args.fps}; "
                "refusing an unverified render profile"
            )
            sys.exit(1)
        print(f"  [OK] UE console confirmed t.MaxFPS={args.fps}")
    if unrealcv_enabled and not wait_for_port(unrealcv_port, timeout=120, process=ue_proc):
        returncode = ue_proc.poll()
        if returncode is not None:
            print(f"  [!!] UE exited with code {returncode} before UnrealCV became ready")
        else:
            print(f"  [!!] UnrealCV port {unrealcv_port} did not become ready")
        sys.exit(1)
    try:
        ue_ports = {"mcp": args.mcp_port}
        if unrealcv_enabled:
            ue_ports["unrealcv"] = unrealcv_port
        require_loopback_listeners(ue_ports)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"  [!!] UE listener audit failed: {error}")
        sys.exit(1)

    # ── Step 7: Start web server ──
    env = dict(base_child_environment)
    env["PORT"] = str(args.port)
    env["UNREAL_HOST"] = "127.0.0.1"
    env["UNREAL_PORT"] = str(args.mcp_port)
    env["PIXEL_STREAMING_URL"] = f"http://127.0.0.1:{args.cirrus_http_port}"
    env["CIRRUS_HTTP_PORT"] = str(args.cirrus_http_port)
    env["CIRRUS_WS_PORT"] = str(args.cirrus_ws_port)
    env["UCV_PORT"] = str(unrealcv_port)
    if not unrealcv_enabled:
        env["DISABLE_UCV_BROKER"] = "1"
    env["STUDIO_HOST"] = "127.0.0.1"
    env["STUDIO_MODEL_MODE"] = args.model_mode
    env["STUDIO_CODING_AGENTS_ENABLED"] = "0"
    env["STUDIO_ACCESS_TOKEN"] = access_token
    configure_vista_demo_server_environment(
        env,
        vista_demo=args.vista_demo,
        fps=args.fps,
    )
    validate_model_off_child_environment(env)

    # Mock mode
    if args.mock:
        env["MOCK_MODE"] = "1"
        if args.mock_file:
            # If provided, use it (could be relative or absolute)
            mock_file = args.mock_file if os.path.isabs(args.mock_file) else str(workspace / args.mock_file)
        else:
            # Default to workspace/mock_responses.txt
            mock_file = str(workspace / "mock_responses.txt")
        # Always use absolute path
        env["MOCK_FILE"] = os.path.abspath(mock_file)
        print(f"  [MOCK] Mock mode enabled, using file: {env['MOCK_FILE']}")

    entry = str(workspace / "web" / "server" / "index.js")
    
    # Patch index.js for mock mode if enabled
    if args.mock:
        patch_script = str(workspace / "web" / "server" / "patch-mock-mode.js")
        if os.path.exists(patch_script):
            print("  [MOCK] Patching index.js for mock mode...")
            subprocess.run([node, patch_script], cwd=str(workspace / "web" / "server"), check=False)

    server_proc = start_managed_process(
        [node, entry],
        managed_processes,
        cwd=str(workspace / "web"),
        env=env,
    )
    if not wait_for_http_health(args.port, access_token, timeout=30) or server_proc.poll() is not None:
        print("  [!!] Studio web server failed its loopback health check")
        sys.exit(1)
    try:
        require_loopback_listeners({"web": args.port})
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"  [!!] Studio listener audit failed: {error}")
        sys.exit(1)

    # ── Step 8: Print access info ──
    is_local = is_local_machine()

    print()
    print("=" * 55)
    print("  SimWorld Studio is running!")
    print()
    if is_local:
        print(f"  Open: http://localhost:{args.port}/?token={access_token}")
    else:
        print(f"  Local access:  http://localhost:{args.port}")
        print()
        print("  Use an SSH tunnel from your laptop:")
        print(f"    ssh -L {args.port}:127.0.0.1:{args.port} -L {args.cirrus_http_port}:127.0.0.1:{args.cirrus_http_port} user@server")
        print(f"    Then open: http://localhost:{args.port}/?token={access_token}")
    print()
    ucv_status = str(unrealcv_port) if unrealcv_enabled else "disabled"
    print(f"  GPU: {gpu_index}  |  MCP: {args.mcp_port}  |  UCV: {ucv_status}  |  Web: {args.port}  |  Cirrus: HTTP:{args.cirrus_http_port} WS:{args.cirrus_ws_port} SFU:{args.cirrus_sfu_port}")
    print("=" * 55)
    print()
    print("  Model calls are disabled; use the Pixel Streaming viewport to move and inspect.")
    print()
    print("  Press Ctrl+C to stop.")
    print()

    # ── Handle shutdown ──
    def shutdown(sig=None, frame=None):
        print("\n  Shutting down...")
        cleanup_managed_processes(managed_processes, managed_files)
        print("  Done.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Wait for either process to exit
    try:
        while True:
            # Check if either process died
            if ue_proc.poll() is not None:
                print(f"\n  [!!] UE exited with code {ue_proc.returncode}")
                print(f"       Check log: {ue_log}")
                sys.exit(1)
            if server_proc.poll() is not None:
                print(f"\n  [!!] Web server exited with code {server_proc.returncode}")
                sys.exit(1)
            if cirrus_proc.poll() is not None:
                print(f"\n  [!!] Cirrus exited with code {cirrus_proc.returncode}")
                sys.exit(1)
            time.sleep(2)
    except KeyboardInterrupt:
        shutdown()


def main():
    parser = argparse.ArgumentParser(
        prog="simworld-studio",
        description="SimWorld Studio — AI-powered 3D scene generation",
    )
    subparsers = parser.add_subparsers(dest="command")

    sp_start = subparsers.add_parser("start", help="Launch SimWorld Studio (UE + web server)")
    sp_start.add_argument("--port", type=int, default=3002, help="Web UI port (default: 3002)")
    sp_start.add_argument("--gpu", type=int, default=None, help="GPU index (auto-detected if omitted)")
    sp_start.add_argument("--mcp-port", type=int, default=55560, help="UE MCP port (default: 55560)")
    sp_start.add_argument("--cirrus-http-port", type=int, default=8585, help="Cirrus HTTP port for Pixel Streaming (default: 8585)")
    sp_start.add_argument("--cirrus-ws-port", type=int, default=8586, help="Cirrus WebSocket port for Pixel Streaming (default: 8586)")
    sp_start.add_argument("--cirrus-sfu-port", type=int, default=8889, help="Cirrus SFU port for Pixel Streaming (default: 8889)")
    sp_start.add_argument("--map", default="/Game/Maps/Empty", help="Existing /Game map asset path (default: /Game/Maps/Empty)")
    sp_start.add_argument("--vista-demo", action="store_true", help="Use the hash-pinned VISTA third-person demo GameMode")
    sp_start.add_argument(
        "--fps",
        type=int,
        choices=(30, 60),
        default=60,
        help="Matched engine and Pixel Streaming FPS (default: 60; use 30 only as a measured fallback)",
    )
    sp_start.add_argument("--unrealcv-mode", choices=("auto", "required"), default="auto", help="Use UnrealCV when a loadable plugin is present, or require it (default: auto)")
    sp_start.add_argument("--binary", default=None, help="Path to UE installation or SimWorld-Studio-Minimal directory (overrides UE_ROOT env var)")
    sp_start.add_argument("--data-dir", default=None, help="Prepared, versioned source workspace directory")
    sp_start.add_argument("--model-mode", choices=("off",), default="off", help="T2 secure bring-up disables all model execution")
    sp_start.add_argument(
        "--skip-gpu-check",
        action="store_true",
        help="Deprecated compatibility flag; rendered launches still require the idle GPU audit",
    )
    sp_start.set_defaults(mock=False, mock_file=None, skip_auth_check=False)

    subparsers.add_parser("version", help="Show version")

    args = parser.parse_args()

    if args.command == "start":
        start_server(args)
    elif args.command == "version":
        print(f"simworld-studio v{__version__}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
