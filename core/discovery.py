"""
MeshLink — Peer Discovery via UDP Broadcast / Multicast
Discovers peers on LAN without any central server.
Announces both the X25519 DH public key and Ed25519 signing public key.

Improvements:
  - Sends to subnet broadcast in addition to 255.255.255.255
  - Tries to auto-configure firewall (ufw) on Linux
  - Better socket binding for cross-platform compatibility
"""

import json
import time
import socket
import struct
import logging
import threading
import subprocess
import platform
from dataclasses import dataclass, field, asdict
from typing import Dict, Callable, Optional, List

from .config import (
    NODE_ID, NODE_NAME, LOCAL_IP,
    DISCOVERY_PORT, TCP_PORT, MEDIA_PORT, FILE_PORT, WEB_PORT,
    DISCOVERY_INTERVAL, PEER_TIMEOUT,
    BROADCAST_ADDR, MULTICAST_GROUPS, DISCOVERY_MAGIC, DISCOVERY_PEERS,
)

logger = logging.getLogger("meshlink.discovery")


def _try_auto_firewall():
    """Try to auto-add firewall rules for MeshLink ports on Linux and Windows."""
    logger.info("Attempting to configure firewall rules")
    ports = [DISCOVERY_PORT, TCP_PORT, MEDIA_PORT, FILE_PORT, WEB_PORT]
    if platform.system() == "Linux":
        # Try ufw
        try:
            result = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
            if "inactive" in result.stdout.lower():
                logger.info("UFW is inactive, no firewall rules needed")
                return
            if result.returncode == 0:
                for port in ports:
                    subprocess.run(
                        ["sudo", "-n", "ufw", "allow", str(port)],
                        capture_output=True, timeout=5,
                    )
                logger.info(f"Auto-added UFW allow rules for ports: {ports}")
                return
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            logger.debug(f"UFW auto-config skipped: {e}")

        # Try iptables directly
        try:
            for port in ports:
                for proto in ("tcp", "udp"):
                    subprocess.run(
                        ["sudo", "-n", "iptables", "-A", "INPUT", "-p", proto,
                         "--dport", str(port), "-j", "ACCEPT"],
                        capture_output=True, timeout=5,
                    )
            logger.info(f"Auto-added iptables rules for ports: {ports}")
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            logger.debug(f"iptables auto-config skipped: {e}")
    elif platform.system() == "Windows":
        # Try Windows Firewall
        try:
            # Avoid warning spam when app is not running elevated.
            try:
                import ctypes
                is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
            except Exception:
                is_admin = False

            if not is_admin:
                logger.info("Skipping Windows Firewall auto-config: administrator privileges required")
                return

            failed_rules = []
            for port in ports:
                for proto in ("TCP", "UDP"):
                    rule_name = f"MeshLink {proto} {port}"
                    cmd = [
                        "netsh", "advfirewall", "firewall", "add", "rule",
                        f"name={rule_name}", "dir=in", "action=allow",
                        f"protocol={proto}", f"localport={port}"
                    ]
                    result = subprocess.run(cmd, capture_output=True, timeout=5)
                    if result.returncode == 0:
                        logger.info(f"Added Windows Firewall rule: {rule_name}")
                    else:
                        error_text = (result.stderr or b"").decode(errors="ignore").strip()
                        failed_rules.append((rule_name, error_text))
                        logger.debug(f"Failed to add rule {rule_name}: {error_text}")

            if failed_rules:
                logger.warning(
                    f"Windows Firewall auto-config completed with {len(failed_rules)} failed rules. "
                    f"Enable debug logs for per-rule details."
                )
            else:
                logger.info(f"Auto-added Windows Firewall rules for ports: {ports}")
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            logger.debug(f"Windows Firewall auto-config skipped: {e}")
    else:
        logger.info("Firewall auto-config not supported on this platform")


def _get_subnet_broadcast(ip: str = LOCAL_IP) -> str:
    """Try to determine the subnet broadcast address for a given local IPv4."""
    try:
        import ipaddress
        if ip == "127.0.0.1":
            return BROADCAST_ADDR
        # Try common subnet masks
        for prefix in (24, 16, 20, 25, 26, 27, 28):
            try:
                net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
                broadcast = str(net.broadcast_address)
                # Check if broadcast is reasonable (not too broad)
                if broadcast != "255.255.255.255":
                    return broadcast
            except Exception:
                continue
    except Exception:
        pass
    return BROADCAST_ADDR


def _get_local_ipv4_candidates() -> List[str]:
    """Best-effort list of local interface IPv4 addresses.

    Important for discovery when default route is hijacked by VPN: we bind
    per-interface sockets to force broadcasts through LAN interfaces.
    """
    ips: set[str] = set()
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            ips.add(ip)
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            ips.add(ip)
    except Exception:
        pass

    # Always include computed LOCAL_IP
    try:
        ips.add(LOCAL_IP)
    except Exception:
        pass

    out = []
    for ip in sorted(ips):
        if not ip or ip == "0.0.0.0":
            continue
        if ip.startswith("127."):
            continue
        # Link-local (often useless for LAN discovery)
        if ip.startswith("169.254."):
            continue
        out.append(ip)
    return out


@dataclass
class PeerInfo:
    """Represents a discovered peer on the network."""
    peer_id:     str
    name:        str
    ip:          str
    tcp_port:    int
    media_port:  int
    file_port:   int   = 5153
    public_key:  str   = ""
    signing_key: str   = ""
    last_seen:   float = field(default_factory=time.time)
    status:      str   = "online"
    trusted:     bool  = False

    @property
    def is_alive(self) -> bool:
        return (time.time() - self.last_seen) < PEER_TIMEOUT

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_alive"] = self.is_alive
        # Don't expose IP in the dict for UI (metadata minimization)
        # Keep ip internally but UI won't show it
        return d


class DiscoveryService:
    """
    Broadcasts presence and listens for peers on the LAN.
    Uses UDP broadcast AND multicast for maximum compatibility.
    """

    def __init__(self, public_key_b64: str = "", signing_key_b64: str = ""):
        self.peers: Dict[str, PeerInfo] = {}
        self._lock      = threading.Lock()
        self._running   = False
        self._public_key  = public_key_b64
        self._signing_key = signing_key_b64
        self._subnet_broadcast = _get_subnet_broadcast()

        # Callbacks
        self.on_peer_joined: Optional[Callable[[PeerInfo], None]] = None
        self.on_peer_left:   Optional[Callable[[PeerInfo], None]] = None

    def _make_announcement(self) -> bytes:
        payload = {
            "id":          NODE_ID,
            "name":        NODE_NAME,
            "ip":          LOCAL_IP,
            "tcp_port":    TCP_PORT,
            "media_port":  MEDIA_PORT,
            "file_port":   FILE_PORT,
            "public_key":  self._public_key,
            "signing_key": self._signing_key,
            "status":      "online",
            "ts":          time.time(),
        }
        data = json.dumps(payload).encode("utf-8")
        return DISCOVERY_MAGIC + data

    def _broadcast_loop(self):
        # Default socket (uses OS routing).
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)

        # Per-interface sockets to avoid VPN/default-route issues.
        iface_socks: list[tuple[str, socket.socket]] = []
        for ip in _get_local_ipv4_candidates():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.settimeout(1.0)
                # Bind to interface IP so broadcast goes via that NIC.
                s.bind((ip, 0))
                iface_socks.append((ip, s))
            except Exception as e:
                logger.debug(f"Interface bind skipped for {ip}: {e}")

        ttl = struct.pack("b", 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)

        logger.info(f"Broadcasting on port {DISCOVERY_PORT} as '{NODE_NAME}' ({NODE_ID})")
        logger.info(f"Subnet broadcast: {self._subnet_broadcast}")
        if iface_socks:
            logger.info(f"Discovery TX interfaces: {[ip for ip,_ in iface_socks]}")

        while self._running:
            try:
                msg = self._make_announcement()
                # Send to global broadcast (default route)
                sock.sendto(msg, (BROADCAST_ADDR, DISCOVERY_PORT))
                logger.debug(f"Sent announcement to {BROADCAST_ADDR}:{DISCOVERY_PORT}")

                # Send per-interface to force LAN broadcast even when VPN hijacks routing
                for ip, s in iface_socks:
                    try:
                        s.sendto(msg, (BROADCAST_ADDR, DISCOVERY_PORT))
                        logger.debug(f"Sent announcement via {ip} to {BROADCAST_ADDR}:{DISCOVERY_PORT}")

                        bcast = _get_subnet_broadcast(ip)
                        if bcast != BROADCAST_ADDR:
                            s.sendto(msg, (bcast, DISCOVERY_PORT))
                            logger.debug(f"Sent announcement via {ip} to {bcast}:{DISCOVERY_PORT}")
                    except Exception as e:
                        logger.debug(f"Interface broadcast failed via {ip}: {e}")
                # Send to subnet broadcast (more reliable on many networks)
                if self._subnet_broadcast != BROADCAST_ADDR:
                    try:
                        sock.sendto(msg, (self._subnet_broadcast, DISCOVERY_PORT))
                        logger.debug(f"Sent announcement to {self._subnet_broadcast}:{DISCOVERY_PORT}")
                    except Exception as e:
                        logger.debug(f"Subnet broadcast failed: {e}")
                # Send to multicast groups
                for group in MULTICAST_GROUPS:
                    try:
                        sock.sendto(msg, (group, DISCOVERY_PORT))
                        logger.debug(f"Sent announcement to {group}:{DISCOVERY_PORT}")
                    except Exception as e:
                        logger.debug(f"Multicast send failed to {group}: {e}")
                # Send to static peers
                for peer_addr in DISCOVERY_PEERS:
                    try:
                        if ":" in peer_addr:
                            host, port_str = peer_addr.rsplit(":", 1)
                            port = int(port_str)
                        else:
                            host = peer_addr
                            port = DISCOVERY_PORT
                        sock.sendto(msg, (host, port))
                        logger.debug(f"Sent announcement to static peer {host}:{port}")
                    except Exception as e:
                        logger.debug(f"Static peer send failed to {peer_addr}: {e}")
                # Also send to loopback for same-machine testing
                try:
                    sock.sendto(msg, ("127.0.0.1", DISCOVERY_PORT))
                    logger.debug("Sent announcement to 127.0.0.1")
                except Exception as e:
                    logger.debug(f"Loopback send failed: {e}")
            except Exception as e:
                logger.debug(f"Broadcast error: {e}")
            time.sleep(DISCOVERY_INTERVAL)
        sock.close()
        for _, s in iface_socks:
            try:
                s.close()
            except Exception:
                pass

    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        # Enable broadcast receive
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        sock.bind(("0.0.0.0", DISCOVERY_PORT))

        # Join multicast groups
        for group in MULTICAST_GROUPS:
            try:
                group_bin = socket.inet_aton(group)
                mreq = struct.pack("4s4s", group_bin, socket.inet_aton("0.0.0.0"))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                logger.debug(f"Joined multicast group {group}")
            except Exception as e:
                logger.debug(f"Multicast join failed for {group}: {e}")

        sock.settimeout(1.0)
        logger.info(f"Listening for peers on port {DISCOVERY_PORT}")

        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
                self._handle_announcement(data, addr)
            except socket.timeout:
                pass
            except Exception as e:
                logger.debug(f"Listen error: {e}")

        sock.close()

    def _handle_announcement(self, data: bytes, addr):
        logger.debug(f"Received data from {addr[0]}:{addr[1]}, len={len(data)}")
        if not data.startswith(DISCOVERY_MAGIC):
            logger.debug("Invalid magic, ignoring")
            return
        try:
            payload = json.loads(data[len(DISCOVERY_MAGIC):].decode("utf-8"))
        except Exception as e:
            logger.debug(f"Failed to parse payload: {e}")
            return

        peer_id = payload.get("id", "")
        if not peer_id or peer_id == NODE_ID:
            logger.debug(f"Ignoring self or invalid peer_id: {peer_id}")
            return

        is_new = peer_id not in self.peers

        # Use the actual IP from the packet source, as announced IP might be wrong
        # due to NAT or misconfiguration
        announced_ip = payload.get("ip", addr[0])
        actual_ip = addr[0]
        # Always prefer actual_ip, unless it's loopback and announced is not
        if actual_ip == "127.0.0.1" and announced_ip != "127.0.0.1":
            use_ip = announced_ip
        else:
            use_ip = actual_ip

        peer = PeerInfo(
            peer_id=    peer_id,
            name=       payload.get("name", "Unknown"),
            ip=         use_ip,
            tcp_port=   payload.get("tcp_port",   TCP_PORT),
            media_port= payload.get("media_port", MEDIA_PORT),
            file_port=  payload.get("file_port",  FILE_PORT),
            public_key= payload.get("public_key",  ""),
            signing_key=payload.get("signing_key", ""),
            last_seen=  time.time(),
            status=     payload.get("status", "online"),
        )

        keys_changed = False
        with self._lock:
            if not is_new:
                old_peer = self.peers[peer_id]
                peer.trusted = old_peer.trusted
                if (peer.public_key and peer.public_key != old_peer.public_key) or \
                   (peer.signing_key and peer.signing_key != old_peer.signing_key):
                    keys_changed = True
            self.peers[peer_id] = peer

        if is_new:
            logger.info(f"New peer discovered: {peer.name} ({peer.ip}:{peer.tcp_port})")
            if self.on_peer_joined:
                self.on_peer_joined(peer)
        elif keys_changed:
            logger.info(f"Peer keys updated: {peer.name}")
            if self.on_peer_joined:
                self.on_peer_joined(peer)

    def _cleanup_loop(self):
        while self._running:
            time.sleep(PEER_TIMEOUT / 2)
            stale = []
            with self._lock:
                for pid, p in list(self.peers.items()):
                    if not p.is_alive:
                        stale.append(p)
                        del self.peers[pid]
            for p in stale:
                logger.info(f"Peer went offline: {p.name}")
                if self.on_peer_left:
                    self.on_peer_left(p)

    def start(self):
        self._running = True
        # Try auto-firewall configuration
        threading.Thread(target=_try_auto_firewall, daemon=True, name="firewall-config").start()
        # Add static peers
        for peer_addr in DISCOVERY_PEERS:
            try:
                if ":" in peer_addr:
                    host, port_str = peer_addr.rsplit(":", 1)
                    port = int(port_str)
                else:
                    host = peer_addr
                    port = DISCOVERY_PORT
                self.add_manual_peer(host, port, f"Static-{host}")
            except Exception as e:
                logger.debug(f"Failed to add static peer {peer_addr}: {e}")
        threading.Thread(target=self._broadcast_loop, daemon=True, name="discovery-tx").start()
        threading.Thread(target=self._listen_loop,    daemon=True, name="discovery-rx").start()
        threading.Thread(target=self._cleanup_loop,   daemon=True, name="discovery-gc").start()

    def stop(self):
        self._running = False

    def get_peers(self) -> list:
        with self._lock:
            return [p.to_dict() for p in self.peers.values() if p.is_alive]

    def get_peer(self, peer_id: str) -> Optional[PeerInfo]:
        with self._lock:
            return self.peers.get(peer_id)

    def add_manual_peer(self, ip: str, tcp_port: int, name: str = ""):
        """Manually add a peer for unicast discovery."""
        peer_id = f"manual-{ip}:{tcp_port}"
        peer = PeerInfo(
            peer_id=peer_id,
            name=name or f"Manual-{ip}",
            ip=ip,
            tcp_port=tcp_port,
            media_port=tcp_port + 1,  # Assume default offsets
            file_port=tcp_port + 2,
            last_seen=time.time(),
            status="online",
        )
        with self._lock:
            self.peers[peer_id] = peer
        logger.info(f"Added manual peer: {peer.name} ({peer.ip}:{peer.tcp_port})")
        if self.on_peer_joined:
            self.on_peer_joined(peer)
