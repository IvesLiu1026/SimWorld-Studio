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
import shutil
import signal
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__


CIRRUS_LOOPBACK_PATCH_MARKER = "VISTA_LOOPBACK_PATCH_V1"
EXPECTED_ORIGINAL_CIRRUS_SHA256 = "92298e881c9240ebe76adfdf0fda39310cc28f5fd5934070194940cca7be29a4"
EXPECTED_PATCHED_CIRRUS_SHA256 = "133a12cf843c69914263a41c3ea3d7f09914ad9241125358850ea0318e55300e"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_prepared_workspace(workspace: Path, manifest_path: Optional[Path] = None) -> Path:
    """Require a staged workspace containing the reviewed source security patch."""
    workspace = Path(workspace).resolve()
    version_file = workspace / ".studio_version"
    index_file = workspace / "web" / "server" / "index.js"
    security_file = workspace / "web" / "server" / "runtime-security.js"
    if not version_file.is_file() or version_file.read_text().strip() != __version__:
        raise RuntimeError(f"Prepared workspace must contain .studio_version={__version__}: {workspace}")
    agent_sandbox_file = workspace / "web" / "server" / "agent-sandbox.js"
    dist_index = workspace / "web" / "dist" / "index.html"
    express_package = workspace / "web" / "server" / "node_modules" / "express" / "package.json"
    if not index_file.is_file() or not security_file.is_file() or not agent_sandbox_file.is_file():
        raise RuntimeError(f"Prepared workspace is missing the reviewed source server: {workspace}")
    if not dist_index.is_file() or not express_package.is_file():
        raise RuntimeError(f"Prepared workspace dependencies/frontend are incomplete: {workspace}")

    manifest_path = Path(manifest_path) if manifest_path else get_package_dir() / "security-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "vista-simworld-security-manifest/v1":
        raise RuntimeError("Installed Studio security manifest has an invalid schema")
    for relative, expected in manifest.get("workspace_sha256", {}).items():
        candidate = workspace / relative
        if not candidate.is_file() or sha256_file(candidate) != expected:
            raise RuntimeError(f"Prepared workspace security SHA-256 mismatch: {relative}")

    receipt_path = workspace.parent / "source-receipt.json"
    if not receipt_path.is_file():
        raise RuntimeError(f"Prepared workspace receipt is missing: {receipt_path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_files = receipt.get("security_files", {})
    receipt_expected = {
        "index.js": sha256_file(index_file),
        "runtime-security.js": sha256_file(security_file),
    }
    if receipt.get("schema") != "vista-simworld-staged-workspace/v1" or any(
        receipt_files.get(name) != digest for name, digest in receipt_expected.items()
    ):
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


def cleanup_managed_processes(processes, files) -> None:
    for process in reversed(processes):
        if process is not None and process.poll() is None:
            process.terminate()
    for process in reversed(processes):
        if process is None:
            continue
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    for handle in files:
        if handle is not None and not handle.closed:
            handle.close()


def get_package_dir():
    """Return the directory where this package is installed."""
    return Path(__file__).parent


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


def wait_for_port(port, host="127.0.0.1", timeout=120):
    """Wait for a TCP port to become available."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((host, port))
            s.close()
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(2)
    return False


def normalize_map_path(map_path: str) -> str:
    """Normalize UE map path to include .umap suffix when omitted."""
    if not map_path:
        return "/Game/Main.umap"
    return map_path if map_path.endswith(".umap") else f"{map_path}.umap"


def setup_workspace(workspace, pkg_dir):
    """
    Create/update workspace with bundled files.
    Only copies files if they're missing or package version changed.
    """
    workspace = Path(workspace)
    pkg_dir = Path(pkg_dir)

    # Version tracking
    version_file = workspace / ".studio_version"
    current_version = version_file.read_text().strip() if version_file.exists() else ""

    needs_update = current_version != __version__

    # Create directory structure
    for d in [
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
    ]:
        (workspace / d).mkdir(parents=True, exist_ok=True)

    if needs_update:
        print(f"  Setting up workspace (v{__version__})...")

        # Copy server JS files
        server_src = pkg_dir / "server"
        server_dst = workspace / "web" / "server"
        if server_src.exists():
            for f in server_src.iterdir():
                if f.is_file():
                    shutil.copy2(f, server_dst / f.name)

        # Copy frontend dist
        dist_src = pkg_dir / "server" / "dist"
        dist_dst = workspace / "web" / "dist"
        if dist_src.exists():
            shutil.rmtree(dist_dst, ignore_errors=True)
            shutil.copytree(dist_src, dist_dst, dirs_exist_ok=True)

        # Copy skills
        skills_src = pkg_dir / "skills" / "builtin"
        skills_dst = workspace / "arena" / "skills" / "builtin"
        if skills_src.exists():
            for f in skills_src.glob("*.md"):
                shutil.copy2(f, skills_dst / f.name)

        # Copy config
        config_src = pkg_dir / "config"
        config_dst = workspace / "arena" / "config"
        if config_src.exists():
            for f in config_src.iterdir():
                if f.is_file():
                    shutil.copy2(f, config_dst / f.name)

        # Install npm deps
        server_dst = workspace / "web" / "server"
        pkg_json = server_dst / "package.json"
        if pkg_json.exists():
            node_modules = server_dst / "node_modules"
            if not node_modules.exists():
                print("  Installing Node.js dependencies...")
                subprocess.run(
                    ["npm", "install", "--production", "--no-optional", "--no-audit", "--no-fund"],
                    cwd=str(server_dst),
                    capture_output=True,
                )

        # Write version marker
        version_file.write_text(__version__)
    else:
        # Still ensure npm deps exist
        server_dst = workspace / "web" / "server"
        node_modules = server_dst / "node_modules"
        if not node_modules.exists():
            pkg_json = server_dst / "package.json"
            if pkg_json.exists():
                subprocess.run(
                    ["npm", "install", "--production", "--no-optional", "--no-audit", "--no-fund"],
                    cwd=str(server_dst),
                    capture_output=True,
                )

    return workspace


def generate_mcp_config(workspace, ue_host, ue_port):
    """Generate mcp.json pointing to the workspace MCP server."""
    mcp_server_path = str(Path(workspace) / "web" / "server" / "mcp-server.js")
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
    config_path = Path(workspace) / "web" / "mcp.json"
    config_path.write_text(json.dumps(config, indent=2))
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
        "unrealcv": unrealcv_port,
    }
    try:
        require_ports_free(requested_ports)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"  [!!] Port preflight failed: {error}")
        sys.exit(1)

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
        if not args.skip_gpu_check:
            sys.exit(1)

    gpu_index = args.gpu
    if gpu_index is None and len(gpus) > 1:
        print()
        print(f"  Multiple GPUs detected. Which GPU to use? [0-{len(gpus)-1}]")
        try:
            gpu_index = int(input("  GPU index (default 0): ").strip() or "0")
        except (ValueError, EOFError):
            gpu_index = 0
    elif gpu_index is None:
        gpu_index = 0

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
    generate_mcp_config(workspace, "127.0.0.1", str(args.mcp_port))
    print(f"  [OK] Workspace: {workspace}")

    project_file = find_ue_project(binary_dir)
    if not project_file:
        print("  [!!] UE project file not found!")
        print("       Set UE_PROJECT_PATH or use a verified SimWorld Minimal runtime")
        sys.exit(1)
    print(f"  [OK] Project: {project_file}")
    sync_unrealcv_port_in_saved_ini(Path(project_file).parent, unrealcv_port)

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
        cirrus_env = os.environ.copy()
        cirrus_env["STUDIO_ACCESS_TOKEN"] = access_token
        cirrus_proc = subprocess.Popen(
            [node, str(cirrus_js), f"--configFile={cirrus_config_path}"],
            cwd=str(cirrus_dir),
            env=cirrus_env,
            stdout=cirrus_log_file,
            stderr=subprocess.STDOUT,
        )
        managed_processes.append(cirrus_proc)
        cirrus_ports = {
            "cirrus_http": args.cirrus_http_port,
            "cirrus_streamer": args.cirrus_ws_port,
            "cirrus_sfu": args.cirrus_sfu_port,
        }
        if (
            any(not wait_for_port(port, timeout=30) for port in cirrus_ports.values())
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
    
    ue_env = os.environ.copy()
    ue_env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
    nvidia_icd = "/usr/share/vulkan/icd.d/nvidia_icd.json"
    if os.path.isfile(nvidia_icd):
        ue_env["VK_ICD_FILENAMES"] = nvidia_icd

    ue_log = workspace / "logs" / "ue.log"

    ue_map = normalize_map_path(args.map)

    ue_cmd = [
        ue_editor, project_file,
        ue_map,
        f"-MCPPort={args.mcp_port}",
        "-Unattended", "-NOSPLASH", "-NOSOUND", "-Messaging",
        "-ResX=1280", "-ResY=720",
        "-FPSMAX=15",
        f"-graphicsadapter={gpu_index}",
        "-RenderOffScreen",
        # Pixel Streaming via Cirrus signaling server
        "-EditorPixelStreamingRes=1280x720",
        "-EditorPixelStreamingStartOnLaunch=true",
        "-EditorPixelStreamingUseRemoteSignallingServer=true",
        f"-PixelStreamingURL=ws://127.0.0.1:{args.cirrus_ws_port}",
        "-log",
    ]

    ue_log_file = open(ue_log, "w")
    managed_files.append(ue_log_file)
    ue_proc = subprocess.Popen(
        ue_cmd,
        env=ue_env,
        stdout=ue_log_file,
        stderr=subprocess.STDOUT,
    )
    managed_processes.append(ue_proc)
    print(f"  UE PID: {ue_proc.pid} (log: {ue_log})")
    print(f"  Map: {ue_map}")

    # ── Step 6: Wait for MCP port ──
    print(f"  Waiting for MCP port {args.mcp_port}...", end="", flush=True)
    if wait_for_port(args.mcp_port, timeout=120):
        print(" ready!")
    else:
        print(" TIMEOUT!")
        print(f"  UE may have crashed. Check log: {ue_log}")
        ue_proc.terminate()
        sys.exit(1)
    if not wait_for_port(unrealcv_port, timeout=120):
        print(f"  [!!] UnrealCV port {unrealcv_port} did not become ready")
        sys.exit(1)
    try:
        require_loopback_listeners({"mcp": args.mcp_port, "unrealcv": unrealcv_port})
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"  [!!] UE listener audit failed: {error}")
        sys.exit(1)

    # ── Step 7: Start web server ──
    env = os.environ.copy()
    env["PORT"] = str(args.port)
    env["UNREAL_HOST"] = "127.0.0.1"
    env["UNREAL_PORT"] = str(args.mcp_port)
    env["PIXEL_STREAMING_URL"] = f"http://127.0.0.1:{args.cirrus_http_port}"
    env["CIRRUS_HTTP_PORT"] = str(args.cirrus_http_port)
    env["CIRRUS_WS_PORT"] = str(args.cirrus_ws_port)
    env["UCV_PORT"] = str(unrealcv_port)
    env["STUDIO_HOST"] = "127.0.0.1"
    env["STUDIO_MODEL_MODE"] = args.model_mode
    env["STUDIO_CODING_AGENTS_ENABLED"] = "0"
    env["STUDIO_ACCESS_TOKEN"] = access_token

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

    server_proc = subprocess.Popen(
        [node, entry],
        cwd=str(workspace / "web"),
        env=env,
    )
    managed_processes.append(server_proc)
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
    print(f"  GPU: {gpu_index}  |  MCP: {args.mcp_port}  |  Web: {args.port}  |  Cirrus: HTTP:{args.cirrus_http_port} WS:{args.cirrus_ws_port} SFU:{args.cirrus_sfu_port}")
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
                server_proc.terminate()
                sys.exit(1)
            if server_proc.poll() is not None:
                print(f"\n  [!!] Web server exited with code {server_proc.returncode}")
                ue_proc.terminate()
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
    sp_start.add_argument("--map", default="/Game/Main", help="UE map path to open (default: /Game/Main)")
    sp_start.add_argument("--binary", default=None, help="Path to UE installation or SimWorld-Studio-Minimal directory (overrides UE_ROOT env var)")
    sp_start.add_argument("--data-dir", default=None, help="Prepared, versioned source workspace directory")
    sp_start.add_argument("--model-mode", choices=("off",), default="off", help="T2 secure bring-up disables all model execution")
    sp_start.add_argument("--skip-gpu-check", action="store_true", help="Skip GPU check")
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
