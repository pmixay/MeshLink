"""
MeshLink — Peer Discovery via UDP Broadcast / Multicast
Discovers peers on LAN without any central server.
"""

import json
import time
import socket
import struct
import logging
import threading
from dataclasses import dataclass, field, asdict
from typing import Dict, Callable, Optional

from .config import (
    NODE_ID, NODE_NAME, LOCAL_IP,
    DISCOVERY_PORT, TCP_PORT, MEDIA_PORT, FILE_PORT,
    DISCOVERY_INTERVAL, PEER_TIMEOUT,
    BROADCAST_ADDR, MULTICAST_GROUP, DISCOVERY_MAGIC,
)

logger = logging.getLogger("meshlink.discovery")


@dataclass
class PeerInfo:
    """Represents a discovered peer on the network."""
    peer_id: str
    name: str
    ip: str
    tcp_port: int
    media_port: int
    file_port: int = 5153
    public_key: str = ""
    last_seen: float = field(default_factory=time.time)
    status: str = "online"      # online / busy / away

    @property
    def is_alive(self) -> bool:
        return (time.time() - self.last_seen) < PEER_TIMEOUT

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_alive"] = self.is_alive
        return d


class DiscoveryService:
    """
    Broadcasts presence and listens for peers on the LAN.
    Uses both UDP broadcast AND multicast for maximum compatibility.
    """

    def __init__(self, public_key_b64: str = ""):
        self.peers: Dict[str, PeerInfo] = {}
        self._lock = threading.Lock()
        self._running = False
        self._public_key = public_key_b64

        # Callbacks
        self.on_peer_joined: Optional[Callable[[PeerInfo], None]] = None
        self.on_peer_left: Optional[Callable[[PeerInfo], None]] = None

    # ── Build announcement payload ──────────────────────────

    def _make_announcement(self) -> bytes:
        payload = {
            "id": NODE_ID,
            "name": NODE_NAME,
            "ip": LOCAL_IP,
            "tcp_port": TCP_PORT,
            "media_port": MEDIA_PORT,
            "file_port": FILE_PORT,
            "public_key": self._public_key,
            "status": "online",
            "ts": time.time(),
        }
        data = json.dumps(payload).encode("utf-8")
        return DISCOVERY_MAGIC + data

    # ── Broadcaster ─────────────────────────────────────────

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)

        # Also set up multicast sending
        ttl = struct.pack("b", 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)

        logger.info(f"Broadcasting on port {DISCOVERY_PORT} as '{NODE_NAME}' ({NODE_ID})")

        while self._running:
            try:
                msg = self._make_announcement()
                # UDP broadcast
                sock.sendto(msg, (BROADCAST_ADDR, DISCOVERY_PORT))
                # Multicast
                try:
                    sock.sendto(msg, (MULTICAST_GROUP, DISCOVERY_PORT))
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"Broadcast error: {e}")
            time.sleep(DISCOVERY_INTERVAL)
        sock.close()

    # ── Listener ────────────────────────────────────────────

    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        sock.bind(("", DISCOVERY_PORT))

        # Join multicast group
        try:
            group = socket.inet_aton(MULTICAST_GROUP)
            mreq = struct.pack("4sL", group, socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception as e:
            logger.debug(f"Multicast join failed: {e}")

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
        if not data.startswith(DISCOVERY_MAGIC):
            return
        try:
            payload = json.loads(data[len(DISCOVERY_MAGIC):].decode("utf-8"))
        except Exception:
            return

        peer_id = payload.get("id", "")
        if peer_id == NODE_ID:
            return  # ignore self

        is_new = peer_id not in self.peers

        peer = PeerInfo(
            peer_id=peer_id,
            name=payload.get("name", "Unknown"),
            ip=payload.get("ip", addr[0]),
            tcp_port=payload.get("tcp_port", TCP_PORT),
            media_port=payload.get("media_port", MEDIA_PORT),
            file_port=payload.get("file_port", FILE_PORT),
            public_key=payload.get("public_key", ""),
            last_seen=time.time(),
            status=payload.get("status", "online"),
        )

        with self._lock:
            self.peers[peer_id] = peer

        if is_new:
            logger.info(f"New peer discovered: {peer.name} ({peer.ip})")
            if self.on_peer_joined:
                self.on_peer_joined(peer)

    # ── Cleanup stale peers ─────────────────────────────────

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

    # ── Public API ──────────────────────────────────────────

    def start(self):
        self._running = True
        threading.Thread(target=self._broadcast_loop, daemon=True, name="discovery-tx").start()
        threading.Thread(target=self._listen_loop, daemon=True, name="discovery-rx").start()
        threading.Thread(target=self._cleanup_loop, daemon=True, name="discovery-gc").start()

    def stop(self):
        self._running = False

    def get_peers(self) -> list:
        with self._lock:
            return [p.to_dict() for p in self.peers.values() if p.is_alive]

    def get_peer(self, peer_id: str) -> Optional[PeerInfo]:
        with self._lock:
            return self.peers.get(peer_id)
