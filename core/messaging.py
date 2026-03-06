"""
MeshLink — TCP Messaging Protocol
Handles text messages, file transfer signaling, call signaling,
mesh relay, and seed-pairing handshakes.
"""

import os
import json
import time
import struct
import socket
import logging
import threading
from enum import IntEnum
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, List

from .config import NODE_ID, NODE_NAME, LOCAL_IP, TCP_PORT, CHUNK_SIZE, MESH_TTL_DEFAULT

logger = logging.getLogger("meshlink.messaging")


class MsgType(IntEnum):
    """Protocol message types."""
    TEXT          = 1
    FILE_OFFER    = 2
    FILE_ACCEPT   = 3
    FILE_REJECT   = 4
    FILE_CHUNK    = 5
    FILE_COMPLETE = 6
    CALL_INVITE = 10
    CALL_ACCEPT = 11
    CALL_REJECT = 12
    CALL_END = 13
    KEY_EXCHANGE = 20
    PING = 30
    PONG = 31
    TYPING = 40
    READ_RECEIPT = 41
    DELIVERY_ACK = 42
    # WebRTC signaling
    WEBRTC_OFFER  = 50
    WEBRTC_ANSWER = 51
    WEBRTC_ICE    = 52
    # Mesh flooding relay
    MESH_RELAY    = 60
    # Seed-pairing handshake
    SEED_PAIR     = 70


@dataclass
class Message:
    """Represents a protocol message."""
    msg_type:    int
    sender_id:   str
    sender_name: str
    payload:     dict
    timestamp:   float = field(default_factory=time.time)
    msg_id:      str   = ""
    # Mesh flooding fields
    ttl:         int   = MESH_TTL_DEFAULT
    relay_path:  list  = field(default_factory=list)
    # Signing fields
    signature:   str   = ""   # base64-encoded Ed25519 signature of canonical payload

    def to_bytes(self) -> bytes:
        if not self.msg_id:
            self.msg_id = f"{self.sender_id}-{time.time_ns()}"
        data = json.dumps({
            "type":        self.msg_type,
            "sender_id":   self.sender_id,
            "sender_name": self.sender_name,
            "payload":     self.payload,
            "timestamp":   self.timestamp,
            "msg_id":      self.msg_id,
            "ttl":         self.ttl,
            "relay_path":  self.relay_path,
            "signature":   self.signature,
        }).encode("utf-8")
        # Length-prefixed framing: 4-byte big-endian length + data
        return struct.pack("!I", len(data)) + data

    @staticmethod
    def from_bytes(data: bytes) -> "Message":
        obj = json.loads(data.decode("utf-8"))
        return Message(
            msg_type=    obj["type"],
            sender_id=   obj["sender_id"],
            sender_name= obj["sender_name"],
            payload=     obj["payload"],
            timestamp=   obj.get("timestamp", time.time()),
            msg_id=      obj.get("msg_id", ""),
            ttl=         obj.get("ttl", MESH_TTL_DEFAULT),
            relay_path=  obj.get("relay_path", []),
            signature=   obj.get("signature", ""),
        )

    def canonical_bytes(self) -> bytes:
        """Deterministic byte representation used as the signing payload."""
        return json.dumps({
            "msg_id":    self.msg_id,
            "type":      self.msg_type,
            "sender_id": self.sender_id,
            "payload":   self.payload,
            "timestamp": self.timestamp,
        }, sort_keys=True).encode("utf-8")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes from a socket."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf += chunk
    return buf


def recv_message(sock: socket.socket) -> Message:
    """Read one length-prefixed message from a socket."""
    length_data = _recv_exact(sock, 4)
    length = struct.unpack("!I", length_data)[0]
    if length > 50 * 1024 * 1024:  # 50 MB max message size
        raise ValueError(f"Message too large: {length}")
    data = _recv_exact(sock, length)
    return Message.from_bytes(data)


def send_message(sock: socket.socket, msg: Message):
    """Send one length-prefixed message to a socket."""
    sock.sendall(msg.to_bytes())


def _history_store_path() -> str:
    base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "chat_history.jsonl")


_HISTORY_LOCK = threading.Lock()


def persist_chat_entry(entry: dict):
    """Append one chat entry to local persistent storage (JSONL)."""
    entry = dict(entry)
    entry.setdefault("timestamp", time.time())
    with _HISTORY_LOCK:
        with open(_history_store_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_persisted_chats() -> Dict[str, List[dict]]:
    """Load persisted chat history grouped by peer_id."""
    path = _history_store_path()
    if not os.path.exists(path):
        return {}

    by_peer: Dict[str, List[dict]] = {}
    by_msg_id: Dict[str, dict] = {}
    status_updates: Dict[str, str] = {}

    with _HISTORY_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue

                    if entry.get("kind") == "status_update":
                        msg_id = entry.get("msg_id")
                        if msg_id:
                            status_updates[msg_id] = entry.get("status", "")
                        continue

                    peer_id = entry.get("peer_id", "")
                    msg_id = entry.get("msg_id", "")
                    if not peer_id or not msg_id:
                        continue
                    if msg_id in by_msg_id:
                        continue

                    by_msg_id[msg_id] = entry
                    by_peer.setdefault(peer_id, []).append(entry)
        except Exception as e:
            logger.warning(f"Failed to load persisted chats: {e}")
            return {}

    for msg_id, status in status_updates.items():
        if msg_id in by_msg_id and status:
            by_msg_id[msg_id]["status"] = status

    for peer_id, items in by_peer.items():
        items.sort(key=lambda x: x.get("timestamp", 0.0))
        by_peer[peer_id] = items

    return by_peer


class MessageServer:
    """
    TCP server that accepts incoming connections from peers.
    Routes messages to registered handlers by MsgType.
    """

    def __init__(self):
        self._running      = False
        self._server_sock: Optional[socket.socket] = None
        self._connections: Dict[str, socket.socket] = {}
        self._lock         = threading.Lock()
        self._delivery_lock = threading.Lock()
        self._delivery_status: Dict[str, str] = {}
        self._delivery_events: Dict[str, threading.Event] = {}
        self.on_delivery_status: Optional[Callable[[dict], None]] = None

        self.max_retries = 3
        self.retry_backoff_base = 0.25
        self.delivery_wait_timeout = 1.2

        # Message handlers by MsgType value
        self._handlers: Dict[int, List[Callable]] = {}

    def on(self, msg_type: int, handler: Callable):
        """Register a handler for a message type."""
        self._handlers.setdefault(int(msg_type), []).append(handler)

    def _emit(self, msg: Message):
        for handler in self._handlers.get(msg.msg_type, []):
            try:
                handler(msg)
            except Exception as e:
                logger.error(f"Handler error for type {msg.msg_type}: {e}")

    def _set_delivery_status(self, peer_id: str, msg_id: str, status: str):
        if not msg_id:
            return
        with self._delivery_lock:
            self._delivery_status[msg_id] = status
            event = self._delivery_events.get(msg_id)
            if status == "delivered" and event:
                event.set()
        if self.on_delivery_status:
            try:
                self.on_delivery_status({
                    "peer_id": peer_id,
                    "msg_id": msg_id,
                    "status": status,
                    "timestamp": time.time(),
                })
            except Exception as e:
                logger.debug(f"Delivery callback error: {e}")

    def get_delivery_status(self, msg_id: str) -> str:
        with self._delivery_lock:
            return self._delivery_status.get(msg_id, "unknown")

    def _drop_peer_connection(self, peer_id: str, sock: socket.socket):
        if not peer_id:
            return
        with self._lock:
            current = self._connections.get(peer_id)
            if current is sock:
                self._connections.pop(peer_id, None)

    def start(self):
        self._running = True
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.settimeout(1.0)
        self._server_sock.bind(("0.0.0.0", TCP_PORT))
        self._server_sock.listen(20)
        logger.info(f"TCP message server listening on port {TCP_PORT}")
        threading.Thread(target=self._accept_loop, daemon=True, name="msg-server").start()

    def stop(self):
        self._running = False
        with self._lock:
            for sock in self._connections.values():
                try:
                    sock.close()
                except Exception:
                    pass
            self._connections.clear()
        if self._server_sock:
            self._server_sock.close()

    def _accept_loop(self):
        while self._running:
            try:
                client, addr = self._server_sock.accept()
                threading.Thread(
                    target=self._handle_client, args=(client, addr),
                    daemon=True, name=f"msg-client-{addr[0]}"
                ).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Accept error: {e}")

    def _handle_client(self, sock: socket.socket, addr):
        logger.debug(f"Incoming TCP connection from {addr[0]}")
        sock.settimeout(30)
        try:
            while self._running:
                try:
                    msg = recv_message(sock)
                    # Store connection for replies
                    with self._lock:
                        self._connections[msg.sender_id] = sock

                    if msg.msg_type == MsgType.DELIVERY_ACK:
                        acked_id = msg.payload.get("msg_id", "")
                        if acked_id:
                            self._set_delivery_status(msg.sender_id, acked_id, "delivered")
                            persist_chat_entry({
                                "kind": "status_update",
                                "peer_id": msg.sender_id,
                                "msg_id": acked_id,
                                "status": "delivered",
                                "timestamp": time.time(),
                            })
                        continue

                    if msg.msg_type == MsgType.TEXT:
                        persist_chat_entry({
                            "peer_id": msg.sender_id,
                            "msg_id": msg.msg_id,
                            "sender_id": msg.sender_id,
                            "sender_name": msg.sender_name,
                            "text": msg.payload.get("text", ""),
                            "timestamp": msg.timestamp,
                            "is_me": False,
                            "msg_type": "text",
                            "status": "delivered",
                        })
                        # Backward compatible delivery ACK.
                        try:
                            ack = Message(
                                msg_type=MsgType.DELIVERY_ACK,
                                sender_id=NODE_ID,
                                sender_name=NODE_NAME,
                                payload={"msg_id": msg.msg_id, "status": "delivered"},
                            )
                            send_message(sock, ack)
                        except Exception as e:
                            logger.debug(f"Failed to send ACK for {msg.msg_id}: {e}")

                    self._emit(msg)
                except socket.timeout:
                    continue
                except (ConnectionError, ConnectionResetError):
                    break
        except Exception as e:
            logger.debug(f"Client handler error: {e}")
        finally:
            sock.close()

    def send_to_peer(self, ip: str, port: int, msg: Message, peer_id: str = ""):
        """Send a message to a peer with retry and optional delivery tracking."""
        if not msg.msg_id:
            msg.msg_id = f"{msg.sender_id}-{time.time_ns()}"

        should_track_delivery = msg.msg_type == MsgType.TEXT and bool(peer_id)
        delivery_event = None
        if should_track_delivery:
            with self._delivery_lock:
                delivery_event = threading.Event()
                self._delivery_events[msg.msg_id] = delivery_event
            self._set_delivery_status(peer_id, msg.msg_id, "sent")

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            sock = None
            reused = False
            with self._lock:
                if peer_id:
                    sock = self._connections.get(peer_id)
                    reused = sock is not None

            try:
                if not sock:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.settimeout(5)
                    sock.connect((ip, port))
                    if peer_id:
                        with self._lock:
                            self._connections[peer_id] = sock
                        threading.Thread(
                            target=self._handle_client,
                            args=(sock, (ip, port)),
                            daemon=True,
                            name=f"msg-client-out-{ip}",
                        ).start()
                else:
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except Exception:
                        pass

                send_message(sock, msg)

                # Best-effort delivered status (backward compatible if remote has no ACK support).
                if should_track_delivery and delivery_event is not None:
                    if delivery_event.wait(timeout=self.delivery_wait_timeout):
                        persist_chat_entry({
                            "peer_id": peer_id,
                            "msg_id": msg.msg_id,
                            "sender_id": NODE_ID,
                            "sender_name": NODE_NAME,
                            "text": msg.payload.get("text", ""),
                            "timestamp": msg.timestamp,
                            "is_me": True,
                            "msg_type": "text",
                            "status": "delivered",
                        })
                    else:
                        self._set_delivery_status(peer_id, msg.msg_id, "sent")
                        persist_chat_entry({
                            "peer_id": peer_id,
                            "msg_id": msg.msg_id,
                            "sender_id": NODE_ID,
                            "sender_name": NODE_NAME,
                            "text": msg.payload.get("text", ""),
                            "timestamp": msg.timestamp,
                            "is_me": True,
                            "msg_type": "text",
                            "status": "sent",
                        })
                    with self._delivery_lock:
                        self._delivery_events.pop(msg.msg_id, None)
                return True
            except Exception as e:
                last_error = e
                if reused and sock is not None:
                    self._drop_peer_connection(peer_id, sock)
                if not reused and sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass

                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_base * (2 ** (attempt - 1)))

        logger.error(f"Failed to send to {ip}:{port}: {last_error}")
        if should_track_delivery:
            self._set_delivery_status(peer_id, msg.msg_id, "failed")
            persist_chat_entry({
                "peer_id": peer_id,
                "msg_id": msg.msg_id,
                "sender_id": NODE_ID,
                "sender_name": NODE_NAME,
                "text": msg.payload.get("text", ""),
                "timestamp": msg.timestamp,
                "is_me": True,
                "msg_type": "text",
                "status": "failed",
            })
        if should_track_delivery:
            with self._delivery_lock:
                self._delivery_events.pop(msg.msg_id, None)
        return False


# ── Message factories ────────────────────────────────────────────────────────

def make_text_message(text: str, encrypted: bool = False,
                      ciphertext_b64: str = "") -> Message:
    payload: dict = {"text": text}
    if encrypted:
        payload["encrypted"]  = True
        payload["ciphertext"] = ciphertext_b64
    return Message(
        msg_type=    MsgType.TEXT,
        sender_id=   NODE_ID,
        sender_name= NODE_NAME,
        payload=     payload,
    )


def make_file_offer(filename: str, filesize: int, file_id: str) -> Message:
    return Message(
        msg_type=    MsgType.FILE_OFFER,
        sender_id=   NODE_ID,
        sender_name= NODE_NAME,
        payload=     {"filename": filename, "filesize": filesize, "file_id": file_id},
    )


def make_call_invite(call_type: str = "audio") -> Message:
    return Message(
        msg_type=    MsgType.CALL_INVITE,
        sender_id=   NODE_ID,
        sender_name= NODE_NAME,
        payload=     {"call_type": call_type},
    )


def make_key_exchange(public_key_b64: str, signing_key_b64: str = "") -> Message:
    """KEY_EXCHANGE now carries both the X25519 DH key and the Ed25519 signing key."""
    return Message(
        msg_type=    MsgType.KEY_EXCHANGE,
        sender_id=   NODE_ID,
        sender_name= NODE_NAME,
        payload=     {
            "public_key":  public_key_b64,
            "signing_key": signing_key_b64,
        },
    )


def make_seed_pair_message(peer_id_target: str) -> Message:
    """
    SEED_PAIR handshake — notifies a peer that we've activated seed-pairing.
    The actual seed is exchanged out-of-band (shown in UI).
    """
    return Message(
        msg_type=    MsgType.SEED_PAIR,
        sender_id=   NODE_ID,
        sender_name= NODE_NAME,
        payload=     {"target": peer_id_target, "status": "paired"},
    )
