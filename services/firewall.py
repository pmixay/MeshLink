"""
MeshLink — Firewall Integration Service

Automatically opens the required ports in the host firewall so that
peers can discover and connect to this node on a LAN.

Supported backends (tried in order):
  1. ufw  (Ubuntu / Debian)
  2. firewall-cmd  (RHEL / Fedora / CentOS)
  3. iptables  (universal fallback)

All operations are best-effort — failure never blocks startup.
"""

import logging
import shutil
import subprocess
from typing import List, Tuple

logger = logging.getLogger("meshlink.firewall")

# (port, proto, description)
_RULES: List[Tuple[int, str, str]] = []


def _run(*args) -> Tuple[bool, str]:
    """Run a command, return (success, combined_output)."""
    try:
        r = subprocess.run(
            list(args),
            capture_output=True, text=True, timeout=10
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def configure_ports(ports: List[Tuple[int, str]]):
    """
    Open *ports* in the system firewall.

    Args:
        ports: list of (port_number, protocol) tuples, e.g. [(5150, "udp"), (5151, "tcp")]
    """
    if not ports:
        return

    # Detect available firewall backend
    if shutil.which("ufw"):
        _apply_ufw(ports)
    elif shutil.which("firewall-cmd"):
        _apply_firewalld(ports)
    elif shutil.which("iptables"):
        _apply_iptables(ports)
    else:
        logger.info("No supported firewall tool found — skipping firewall configuration")


def _apply_ufw(ports: List[Tuple[int, str]]):
    """Add rules via ufw."""
    # Check if ufw is active
    ok, out = _run("ufw", "status")
    if not ok:
        logger.debug(f"ufw status failed: {out}")
        return
    if "inactive" in out.lower():
        logger.info("ufw is installed but inactive — skipping rule insertion")
        return

    results = []
    for port, proto in ports:
        ok, out = _run("ufw", "allow", f"{port}/{proto}", "comment", "MeshLink")
        results.append((port, proto, ok, out))
        if ok:
            logger.info(f"ufw: opened {port}/{proto}")
        else:
            logger.debug(f"ufw: failed to open {port}/{proto}: {out}")

    # Reload if any rule was added
    if any(ok for _, _, ok, _ in results):
        _run("ufw", "reload")


def _apply_firewalld(ports: List[Tuple[int, str]]):
    """Add rules via firewall-cmd."""
    for port, proto in ports:
        ok, out = _run(
            "firewall-cmd", "--permanent", "--add-port", f"{port}/{proto}"
        )
        if ok:
            logger.info(f"firewall-cmd: opened {port}/{proto}")
        else:
            logger.debug(f"firewall-cmd: failed {port}/{proto}: {out}")
    _run("firewall-cmd", "--reload")


def _apply_iptables(ports: List[Tuple[int, str]]):
    """Add rules via iptables."""
    for port, proto in ports:
        # Check if rule already exists
        ok, _ = _run("iptables", "-C", "INPUT",
                     "-p", proto, "--dport", str(port),
                     "-j", "ACCEPT")
        if ok:
            logger.debug(f"iptables: rule already exists for {port}/{proto}")
            continue
        ok, out = _run("iptables", "-A", "INPUT",
                       "-p", proto, "--dport", str(port),
                       "-j", "ACCEPT")
        if ok:
            logger.info(f"iptables: opened {port}/{proto}")
        else:
            logger.debug(f"iptables: failed {port}/{proto}: {out}")


def open_meshlink_ports(
    discovery_port: int,
    tcp_port: int,
    media_port: int,
    file_port: int,
    web_port: int,
):
    """
    Open all MeshLink ports in the system firewall.
    Called once on node startup — safe to call even without root privileges
    (the individual helpers will fail gracefully and log a debug message).
    """
    ports = [
        (discovery_port, "udp"),  # peer discovery
        (tcp_port,       "tcp"),  # messaging
        (media_port,     "udp"),  # voice/video
        (file_port,      "tcp"),  # file transfer
        (web_port,       "tcp"),  # web UI
    ]
    logger.info(
        f"Attempting firewall configuration for ports: "
        f"{discovery_port}/udp, {tcp_port}/tcp, {media_port}/udp, "
        f"{file_port}/tcp, {web_port}/tcp"
    )
    try:
        configure_ports(ports)
    except Exception as e:
        logger.debug(f"Firewall configuration failed (non-fatal): {e}")
