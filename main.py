#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   ███╗   ███╗███████╗███████╗██╗  ██╗██╗     ██╗███╗   ██╗  ║
║   ████╗ ████║██╔════╝██╔════╝██║  ██║██║     ██║████╗  ██║  ║
║   ██╔████╔██║█████╗  ███████╗███████║██║     ██║██╔██╗ ██║  ║
║   ██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║██║     ██║██║╚██╗██║  ║
║   ██║ ╚═╝ ██║███████╗███████║██║  ██║███████╗██║██║ ╚████║  ║
║   ╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝╚══════╝╚═╝╚═╝  ╚═══╝║
║                                                              ║
║   Decentralized P2P Communication System                     ║
║   No Internet · No Servers · Just the Mesh                   ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝

Usage:
    python main.py                          # Start with defaults
    python main.py --name "Alice"           # Custom display name
    python main.py --web-port 9090          # Custom web UI port
    python main.py --no-browser             # Don't auto-open browser

Environment variables:
    MESHLINK_NODE_NAME       Display name
    MESHLINK_TCP_PORT        TCP messaging port (default: 5151)
    MESHLINK_MEDIA_PORT      UDP media port (default: 5152)
    MESHLINK_WEB_PORT        Web UI port (default: 8080)
    MESHLINK_DISCOVERY_PORT  Discovery port (default: 5150)
    MESHLINK_DOWNLOADS       Downloads directory
"""

import os
import sys
import signal
import logging
import argparse
import webbrowser
import threading

# ── Fix imports: allow running from any location ────────
# Add the project directory itself to sys.path so that
# "from core.xxx" and "from web.xxx" work directly.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
# Also add parent so "from meshlink.xxx" works if run as package
PARENT_DIR = os.path.dirname(PROJECT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s │ %(name)-22s │ %(levelname)-5s │ %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)
    # Silence noisy libraries
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("engineio").setLevel(logging.WARNING)
    logging.getLogger("socketio").setLevel(logging.WARNING)


def parse_args():
    parser = argparse.ArgumentParser(
        description="MeshLink — Decentralized P2P Communication",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name", "-n", type=str, default=None,
                        help="Display name for this node")
    parser.add_argument("--web-port", "-w", type=int, default=None,
                        help="Web UI port (default: 8080)")
    parser.add_argument("--tcp-port", "-t", type=int, default=None,
                        help="TCP messaging port (default: 5151)")
    parser.add_argument("--media-port", "-m", type=int, default=None,
                        help="UDP media port (default: 5152)")
    parser.add_argument("--file-port", "-f", type=int, default=None,
                        help="File transfer port (default: 5153)")
    parser.add_argument("--discovery-port", "-d", type=int, default=None,
                        help="Discovery broadcast port (default: 5150)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)
    logger = logging.getLogger("meshlink")

    # Apply CLI overrides to config (before importing modules that use config)
    try:
        from meshlink.core import config
    except ImportError:
        from core import config

    if args.name:
        config.NODE_NAME = args.name
    if args.web_port:
        config.WEB_PORT = args.web_port
    if args.tcp_port:
        config.TCP_PORT = args.tcp_port
    if args.media_port:
        config.MEDIA_PORT = args.media_port
    if args.file_port:
        config.FILE_PORT = args.file_port
    if args.discovery_port:
        config.DISCOVERY_PORT = args.discovery_port

    # Import after config
    try:
        from meshlink.core.node import MeshNode
        from meshlink.web.server import init_app, run_server
    except ImportError:
        from core.node import MeshNode
        from web.server import init_app, run_server

    # ── Create and start the mesh node ──────────────────
    node = MeshNode()
    node.start()

    # ── Initialize web server ───────────────────────────
    init_app(node)

    # ── Auto-open browser ───────────────────────────────
    if not args.no_browser:
        url = f"http://localhost:{config.WEB_PORT}"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # ── Graceful shutdown ───────────────────────────────
    def shutdown(sig, frame):
        logger.info("\nShutting down MeshLink...")
        node.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Print startup banner ────────────────────────────
    print()
    print("  ╔═══════════════════════════════════════════════╗")
    print("  ║           MeshLink is running!                ║")
    print("  ╠═══════════════════════════════════════════════╣")
    print(f"  ║  Node:      {config.NODE_NAME:<33} ║")
    print(f"  ║  ID:        {config.NODE_ID:<33} ║")
    print(f"  ║  LAN IP:    {config.LOCAL_IP:<33} ║")
    print(f"  ║  Web UI:    http://localhost:{config.WEB_PORT:<19} ║")
    print(f"  ║  TCP Port:  {config.TCP_PORT:<33} ║")
    print(f"  ║  File Port: {config.FILE_PORT:<33} ║")
    print(f"  ║  UDP Media: {config.MEDIA_PORT:<33} ║")
    print(f"  ║  Discovery: {config.DISCOVERY_PORT:<33} ║")
    print(f"  ║  Downloads: {config.DOWNLOADS_DIR:<33} ║")
    print("  ╠═══════════════════════════════════════════════╣")
    print("  ║  Press Ctrl+C to stop                        ║")
    print("  ╚═══════════════════════════════════════════════╝")
    print()

    # ── Start web server (blocking) ─────────────────────
    run_server()


if __name__ == "__main__":
    main()
