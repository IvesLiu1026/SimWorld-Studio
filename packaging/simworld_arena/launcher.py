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
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import __version__


def get_package_dir():
    """Return the directory where this package is installed."""
    return Path(__file__).parent


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
    """Find the SimWorld binary directory."""
    search_paths = []
    if binary_path:
        search_paths.append(Path(binary_path))
    # Common locations relative to cwd
    search_paths.extend([
        Path.cwd() / "SimWorld-Studio-Minimal",
        Path.cwd(),
        Path.home() / "SimWorld-Studio-Minimal",
    ])

    for p in search_paths:
        if (p / "Engine" / "Binaries" / "Linux" / "UnrealEditor").exists():
            return p
    return None


def start_server(args):
    """Start everything: UE binary + Studio web server."""
    pkg_dir = get_package_dir()
    node = find_node()

    print()
    print("=" * 55)
    print("  SimWorld Studio v" + __version__)
    print("=" * 55)

    # ── Step 1: Check Claude auth ──
    print()
    auth = check_claude_auth()
    if auth == "api_key":
        print("  [OK] Claude auth: API key")
    elif auth == "oauth":
        print("  [OK] Claude auth: OAuth (claude login)")
    else:
        print("  [!!] Claude not authenticated!")
        print("       Set ANTHROPIC_API_KEY (get one at console.anthropic.com)")
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

    # ── Step 3: Find SimWorld binary ──
    binary_dir = find_simworld_binary(args.binary)
    if not binary_dir:
        print("  [!!] SimWorld binary not found!")
        print("       Download it first:")
        print("       wget -O SimWorld-Studio-Minimal.tar.gz \\")
        print("           https://huggingface.co/datasets/SimWorld-AI/SimWorld-Studio/resolve/main/SimWorld-Studio-Minimal.tar.gz")
        print("       tar xzf SimWorld-Studio-Minimal.tar.gz")
        sys.exit(1)
    print(f"  [OK] Binary: {binary_dir}")

    # ── Step 4: Setup workspace ──
    workspace = Path(args.data_dir) if args.data_dir else Path.cwd() / "simworld_studio_workspace"
    workspace = setup_workspace(workspace, pkg_dir)
    generate_mcp_config(workspace, "127.0.0.1", str(args.mcp_port))
    print(f"  [OK] Workspace: {workspace}")

    # ── Step 5: Launch UE ──
    print()
    print("  Launching Unreal Engine (headless)...")
    ue_editor = str(binary_dir / "Engine" / "Binaries" / "Linux" / "UnrealEditor")
    project_file = str(binary_dir / "gym_citynav" / "gym_citynav.uproject")

    ue_env = os.environ.copy()
    ue_env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
    nvidia_icd = "/usr/share/vulkan/icd.d/nvidia_icd.json"
    if os.path.isfile(nvidia_icd):
        ue_env["VK_ICD_FILENAMES"] = nvidia_icd

    ue_log = workspace / "logs" / "ue.log"

    ue_cmd = [
        ue_editor, project_file,
        "/Game/Maps/Empty.umap",
        f"-MCPPort={args.mcp_port}",
        "-Unattended", "-NOSPLASH", "-NOSOUND", "-Messaging",
        "-ResX=1280", "-ResY=720",
        "-FPSMAX=15",
        f"-graphicsadapter={gpu_index}",
        "-RenderOffScreen",
        "-log",
    ]

    ue_log_file = open(ue_log, "w")
    ue_proc = subprocess.Popen(
        ue_cmd,
        env=ue_env,
        stdout=ue_log_file,
        stderr=subprocess.STDOUT,
    )
    print(f"  UE PID: {ue_proc.pid} (log: {ue_log})")

    # ── Step 6: Wait for MCP port ──
    print(f"  Waiting for MCP port {args.mcp_port}...", end="", flush=True)
    if wait_for_port(args.mcp_port, timeout=120):
        print(" ready!")
    else:
        print(" TIMEOUT!")
        print(f"  UE may have crashed. Check log: {ue_log}")
        ue_proc.terminate()
        sys.exit(1)

    # ── Step 7: Start web server ──
    env = os.environ.copy()
    env["PORT"] = str(args.port)
    env["UNREAL_HOST"] = "127.0.0.1"
    env["UNREAL_PORT"] = str(args.mcp_port)

    entry = str(workspace / "web" / "server" / "index.js")

    server_proc = subprocess.Popen(
        [node, entry],
        cwd=str(workspace / "web"),
        env=env,
    )

    # ── Step 8: Print access info ──
    server_ip = get_server_ip()
    is_local = is_local_machine()

    print()
    print("=" * 55)
    print("  SimWorld Studio is running!")
    print()
    if is_local:
        print(f"  Open: http://localhost:{args.port}")
    else:
        print(f"  Local access:  http://localhost:{args.port}")
        print(f"  Remote access: http://{server_ip}:{args.port}")
        print()
        print(f"  Or use SSH tunnel from your laptop:")
        print(f"    ssh -L {args.port}:localhost:{args.port} user@{server_ip}")
        print(f"    Then open: http://localhost:{args.port}")
    print()
    print(f"  GPU: {gpu_index}  |  MCP: {args.mcp_port}  |  Web: {args.port}")
    print("=" * 55)
    print()
    print('  Try: "Set up a sunset scene with 4 houses and trees"')
    print()
    print("  Press Ctrl+C to stop.")
    print()

    # ── Handle shutdown ──
    def shutdown(sig=None, frame=None):
        print("\n  Shutting down...")
        server_proc.terminate()
        ue_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        try:
            ue_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ue_proc.kill()
        ue_log_file.close()
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
    sp_start.add_argument("--mcp-port", type=int, default=55559, help="UE MCP port (default: 55559)")
    sp_start.add_argument("--binary", default=None, help="Path to SimWorld-Studio-Minimal directory")
    sp_start.add_argument("--data-dir", default=None, help="Workspace directory")
    sp_start.add_argument("--skip-auth-check", action="store_true", help="Skip Claude auth check")
    sp_start.add_argument("--skip-gpu-check", action="store_true", help="Skip GPU check")

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
