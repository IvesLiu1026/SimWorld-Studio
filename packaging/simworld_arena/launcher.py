"""
SimWorld Studio Launcher

Creates a runtime workspace that mirrors the original project layout,
copies bundled server files in, and starts the backend.

The workspace structure matches what the server code expects:
    workspace/
    ├── web/
    │   ├── server/     (bundled JS + assets)
    │   ├── dist/       (pre-built frontend)
    │   └── mcp.json    (generated)
    ├── arena/
    │   └── skills/
    │       └── builtin/ (skill .md files)
    ├── scenes/          (runtime data)
    ├── skills/          (user custom skills)
    ├── tmp/screens/     (screenshots)
    ├── logs/            (server logs)
    └── arena_data/      (battles, ratings)
"""
import argparse
import json
import os
import shutil
import signal
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
        print(f"[simworld-studio] Setting up workspace (v{__version__})...")

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
                print("[simworld-studio] Installing Node.js dependencies...")
                subprocess.run(
                    ["npm", "install", "--production", "--no-optional", "--no-audit", "--no-fund"],
                    cwd=str(server_dst),
                    capture_output=True,
                )

        # Write version marker
        version_file.write_text(__version__)
        print(f"[simworld-studio] Workspace ready at {workspace}")
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


def start_server(args):
    """Start the Studio backend server."""
    pkg_dir = get_package_dir()
    node = find_node()

    # Workspace directory
    workspace = Path(args.data_dir) if args.data_dir else Path.cwd() / "simworld_studio_workspace"

    # Set up workspace
    workspace = setup_workspace(workspace, pkg_dir)

    # Generate MCP config
    generate_mcp_config(workspace, args.ue_host, str(args.ue_port))

    # Environment for the backend
    env = os.environ.copy()
    env["PORT"] = str(args.port)
    env["UNREAL_HOST"] = args.ue_host
    env["UNREAL_PORT"] = str(args.ue_port)
    env["PIXEL_STREAMING_URL"] = args.pixel_streaming_url

    # The backend entry point (in workspace)
    entry = str(workspace / "web" / "server" / "index.js")

    print(f"\n{'='*50}")
    print(f"  SimWorld Studio v{__version__}")
    print(f"  Backend:    http://0.0.0.0:{args.port}")
    print(f"  UE TCP:     {args.ue_host}:{args.ue_port}")
    print(f"  Workspace:  {workspace}")
    print(f"{'='*50}\n")

    # Start the server
    proc = subprocess.Popen(
        [node, entry],
        cwd=str(workspace / "web"),
        env=env,
    )

    def handle_signal(sig, frame):
        print("\n[simworld-studio] Shutting down...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    parser = argparse.ArgumentParser(
        prog="simworld-studio",
        description="SimWorld Studio — AI-powered 3D scene generation",
    )
    subparsers = parser.add_subparsers(dest="command")

    sp_start = subparsers.add_parser("start", help="Start the Studio platform")
    sp_start.add_argument("--port", type=int, default=3002)
    sp_start.add_argument("--ue-host", default="127.0.0.1")
    sp_start.add_argument("--ue-port", type=int, default=9000)
    sp_start.add_argument("--data-dir", default=None, help="Workspace directory")
    sp_start.add_argument("--pixel-streaming-url", default="http://127.0.0.1:8080")

    subparsers.add_parser("version", help="Show version")
    subparsers.add_parser("update", help="Check for updates")

    args = parser.parse_args()

    if args.command == "start":
        start_server(args)
    elif args.command == "version":
        print(f"simworld-studio v{__version__}")
    elif args.command == "update":
        from .version import auto_update
        auto_update()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
