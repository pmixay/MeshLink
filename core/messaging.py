"""
MeshLink — TCP Messaging Protocol
Handles text messages, file transfer signaling, and call signaling.
"""

import json
import time
import struct
import socket
import logging
import threading
from enum import IntEnum
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, List

from .config import NODE_ID, NODE_NAME, LOCAL_IP, TCP_PORT, CHUNK_SIZE

logger = logging.getLogger("meshlink.messaging")


class MsgType(IntEnum):
    """Protocol message types."""
    TEXT = 1
    FILE_OFFER = 2
    FILE_ACCEPT = 3
    FILE_REJECT = 4
    FILE_CHUNK = 5
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
    # WebRTC signaling
    WEBRTC_OFFER = 50
    WEBRTC_ANSWER = 51
    WEBRTC_ICE = 52


@dataclass
class Message:
    """Represents a protocol message."""
    msg_type: int
    sender_id: str
    sender_name: str
    payload: dict
    timestamp: float = field(default_factory=time.time)
    msg_id: str = ""

    def to_bytes(self) -> bytes:
        data = json.dumps({
            "type": self.msg_type,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "msg_id": self.msg_id or f"{self.sender_id}-{time.time_ns()}",
        }).encode("utf-8")
        # Length-prefixed framing: 4-byte big-endian length + data
        return struct.pack("!I", len(data)) + data

    @staticmethod
    def from_bytes(data: bytes) -> "Message":
        obj = json.loads(data.decode("utf-8"))
        return Message(
            msg_type=obj["type"],
            sender_id=obj["sender_id"],
            sender_name=obj["sender_name"],
            payload=obj["payload"],
            timestamp=obj.get("timestamp", time.time()),
            msg_id=obj.get("msg_id", ""),
        )


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
    if length > 50 * 1024 * 1024:  # 50MB max message
        raise ValueError(f"Message too large: {length}")
    data = _recv_exact(sock, length)
    return Message.from_bytes(data)


def send_message(sock: socket.socket, msg: Message):
    """Send one length-prefixed message to a socket."""
    sock.sendall(msg.to_bytes())


class MessageServer:
    """
    TCP server that accepts incoming connections from peers.
    Routes messages to registered handlers.
    """

    def __init__(self):
        self._running = False
        self._server_sock: Optional[socket.socket] = None
        self._connections: Dict[str, socket.socket] = {}
        self._lock = threading.Lock()

        # Message handlers by MsgType
        self._handlers: Dict[int, List[Callable]] = {}

    def on(self, msg_type: int, handler: Callable):
        """Register a handler for a message type."""
        self._handlers.setdefault(msg_type, []).append(handler)

    def _emit(self, msg: Message):
        for handler in self._handlers.get(msg.msg_type, []):
            try:
                handler(msg)
            except Exception as e:
                logger.error(f"Handler error for type {msg.msg_type}: {e}")

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
        """Send a message to a peer, reusing or creating a connection."""
        sock = None
        with self._lock:
            if peer_id:
                sock = self._connections.get(peer_id)

        try:
            if sock:
                send_message(sock, msg)
                return True
        except Exception:
            sock = None

        # Create new connection
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((ip, port))
            send_message(sock, msg)
            if peer_id:
                with self._lock:
                    self._connections[peer_id] = sock
                # Start reader thread
                threading.Thread(
                    target=self._handle_client, args=(sock, (ip, port)),
                    daemon=True
                ).start()
            return True
        except Exception as e:
            logger.error(f"Failed to send to {ip}:{port}: {e}")
            if sock:
                sock.close()
            return False


def make_text_message(text: str) -> Message:
    return Message(
        msg_type=MsgType.TEXT,
        sender_id=NODE_ID,
        sender_name=NODE_NAME,
        payload={"text": text},
    )


def make_file_offer(filename: str, filesize: int, file_id: str) -> Message:
    return Message(
        msg_type=MsgType.FILE_OFFER,
        sender_id=NODE_ID,
        sender_name=NODE_NAME,
        payload={"filename": filename, "filesize": filesize, "file_id": file_id},
    )


def make_call_invite(call_type: str = "audio") -> Message:
    return Message(
        msg_type=MsgType.CALL_INVITE,
        sender_id=NODE_ID,
        sender_name=NODE_NAME,
        payload={"call_type": call_type},
    )


def make_key_exchange(public_key_b64: str) -> Message:
    return Message(
        msg_type=MsgType.KEY_EXCHANGE,
        sender_id=NODE_ID,
        sender_name=NODE_NAME,
        payload={"public_key": public_key_b64},
    )
