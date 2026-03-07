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
import random
from enum import IntEnum
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, List

from .config import (
    NODE_ID,
    NODE_NAME,
    LOCAL_IP,
    TCP_PORT,
    CHUNK_SIZE,
    MESH_TTL_DEFAULT,
    CHAT_DB_MAX_MB,
    CHAT_DB_MAX_ROWS,
    MESSAGING_MAX_PARALLEL_GLOBAL,
    MESSAGING_MAX_PARALLEL_PER_PEER,
    MESSAGING_SEND_SLOT_TIMEOUT,
    MESSAGING_CONNECT_TIMEOUT,
    MESSAGING_OUTBOX_MAX_PENDING,
)
from . import storage

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

    def ensure_msg_id(self):
        """Ensure msg_id is set. Must be called before signing."""
        if not self.msg_id:
            self.msg_id = f"{self.sender_id}-{time.time_ns()}"

    def to_bytes(self) -> bytes:
        self.ensure_msg_id()
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


def persist_chat_entry(entry: dict):
    """Persist one chat entry in SQLite with dedup/upsert semantics."""
    e = dict(entry)
    if e.get("kind") == "status_update":
        storage.update_message_status(
            e.get("peer_id", ""),
            e.get("msg_id", ""),
            e.get("status", ""),
        )
        return

    storage.persist_chat_entry(e)
    storage.enforce_db_limits(CHAT_DB_MAX_MB, CHAT_DB_MAX_ROWS)


def load_persisted_chats() -> Dict[str, List[dict]]:
    """Load persisted chat history grouped by peer_id from SQLite."""
    return storage.load_persisted_chats()


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
        self._global_send_sem = threading.BoundedSemaphore(max(1, int(MESSAGING_MAX_PARALLEL_GLOBAL)))
        self._peer_send_sems: Dict[str, threading.BoundedSemaphore] = {}
        self._resend_running = False
        self._resend_thread: Optional[threading.Thread] = None

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
        if peer_id:
            storage.set_delivery_status(peer_id, msg_id, status)
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
            status = self._delivery_status.get(msg_id)
        if status:
            return status
        return storage.get_delivery_status(msg_id)

    def _peer_sem(self, peer_id: str) -> threading.BoundedSemaphore:
        key = peer_id or "_anon_"
        with self._lock:
            sem = self._peer_send_sems.get(key)
            if sem is None:
                sem = threading.BoundedSemaphore(max(1, int(MESSAGING_MAX_PARALLEL_PER_PEER)))
                self._peer_send_sems[key] = sem
            return sem

    def _try_acquire_send_slot(self, peer_id: str) -> bool:
        timeout = max(0.01, float(MESSAGING_SEND_SLOT_TIMEOUT))
        if not self._global_send_sem.acquire(timeout=timeout):
            return False
        psem = self._peer_sem(peer_id)
        if not psem.acquire(timeout=timeout):
            self._global_send_sem.release()
            return False
        return True

    def _release_send_slot(self, peer_id: str):
        try:
            self._peer_sem(peer_id).release()
        except Exception:
            pass
        try:
            self._global_send_sem.release()
        except Exception:
            pass

    def _build_outbox_payload(self, msg: Message) -> dict:
        return {
            "type": int(msg.msg_type),
            "sender_id": msg.sender_id,
            "sender_name": msg.sender_name,
            "payload": msg.payload,
            "timestamp": msg.timestamp,
            "msg_id": msg.msg_id,
            "ttl": msg.ttl,
            "relay_path": msg.relay_path,
            "signature": msg.signature,
        }

    @staticmethod
    def _message_from_dict(obj: dict) -> Message:
        return Message(
            msg_type=int(obj.get("type", MsgType.TEXT)),
            sender_id=str(obj.get("sender_id", "")),
            sender_name=str(obj.get("sender_name", "")),
            payload=dict(obj.get("payload", {}) or {}),
            timestamp=float(obj.get("timestamp", time.time())),
            msg_id=str(obj.get("msg_id", "")),
            ttl=int(obj.get("ttl", MESH_TTL_DEFAULT)),
            relay_path=list(obj.get("relay_path", []) or []),
            signature=str(obj.get("signature", "")),
        )

    def _resend_loop(self):
        while self._resend_running:
            try:
                due = storage.load_due_outbox(limit=200)
                for item in due:
                    msg_blob = item.get("msg", {})
                    if not msg_blob:
                        storage.mark_outbox_attempt(item.get("msg_id", ""), int(item.get("attempts", 0)) + 1, time.time() + 60.0, "failed")
                        continue
                    try:
                        msg = self._message_from_dict(msg_blob)
                    except Exception:
                        storage.mark_outbox_attempt(item.get("msg_id", ""), int(item.get("attempts", 0)) + 1, time.time() + 60.0, "failed")
                        continue
                    ok = self.send_to_peer(
                        item.get("ip", ""),
                        int(item.get("port", 0) or 0),
                        msg,
                        item.get("peer_id", ""),
                        _from_outbox=True,
                    )
                    if ok:
                        storage.mark_outbox_delivered(item.get("msg_id", ""))
                    else:
                        storage.incr_counter("metrics.delivery_retry_total", 1.0)
                        attempts = int(item.get("attempts", 0)) + 1
                        next_retry = time.time() + min(60.0, self.retry_backoff_base * (2 ** min(attempts, 8)))
                        status = "pending" if attempts < max(3, self.max_retries * 3) else "failed"
                        storage.mark_outbox_attempt(item.get("msg_id", ""), attempts, next_retry, status)
            except Exception as e:
                logger.debug(f"Resend loop error: {e}")
            time.sleep(0.25)

    def _restore_pending_inbox_once(self):
        pending = storage.load_pending_inbox(limit=500)
        for item in pending:
            msg_obj = item.get("msg", {})
            if not msg_obj:
                storage.mark_inbox_processed(item.get("msg_id", ""))
                continue
            try:
                msg = self._message_from_dict(msg_obj)
            except Exception:
                storage.mark_inbox_processed(item.get("msg_id", ""))
                continue
            self._emit(msg)
            storage.mark_inbox_processed(item.get("msg_id", ""))

    def _drop_peer_connection(self, peer_id: str, sock: socket.socket):
        if not peer_id:
            return
        with self._lock:
            current = self._connections.get(peer_id)
            if current is sock:
                self._connections.pop(peer_id, None)

    def start(self):
        self._running = True
        self._restore_pending_inbox_once()
        self._resend_running = True
        self._resend_thread = threading.Thread(target=self._resend_loop, daemon=True, name="msg-resend")
        self._resend_thread.start()
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.settimeout(1.0)
        self._server_sock.bind(("0.0.0.0", TCP_PORT))
        self._server_sock.listen(20)
        logger.info(f"TCP message server listening on port {TCP_PORT}")
        threading.Thread(target=self._accept_loop, daemon=True, name="msg-server").start()

    def stop(self):
        self._running = False
        self._resend_running = False
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
                        storage.enqueue_inbox(
                            msg.msg_id,
                            msg.sender_id,
                            self._build_outbox_payload(msg),
                            received_at=time.time(),
                        )
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
                    if msg.msg_type == MsgType.TEXT:
                        storage.mark_inbox_processed(msg.msg_id)
                except socket.timeout:
                    continue
                except (ConnectionError, ConnectionResetError):
                    break
        except Exception as e:
            logger.debug(f"Client handler error: {e}")
        finally:
            sock.close()

    def send_to_peer(self, ip: str, port: int, msg: Message, peer_id: str = "", _from_outbox: bool = False):
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
            if not _from_outbox:
                if storage.outbox_pending_count() >= max(100, int(MESSAGING_OUTBOX_MAX_PENDING)):
                    logger.warning("Outbox backpressure: too many pending messages")
                    self._set_delivery_status(peer_id, msg.msg_id, "failed")
                    with self._delivery_lock:
                        self._delivery_events.pop(msg.msg_id, None)
                    return False
                storage.enqueue_outbox(
                    msg.msg_id,
                    peer_id,
                    ip,
                    int(port),
                    self._build_outbox_payload(msg),
                    next_retry_at=time.time(),
                )

        if not self._try_acquire_send_slot(peer_id):
            logger.debug(f"Send backpressure: no free send slot for peer={peer_id}")
            if should_track_delivery:
                with self._delivery_lock:
                    self._delivery_events.pop(msg.msg_id, None)
                if not _from_outbox:
                    storage.mark_outbox_attempt(msg.msg_id, 1, time.time() + 0.5, "pending")
            return False

        last_error = None
        try:
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
                        sock.settimeout(MESSAGING_CONNECT_TIMEOUT)
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
                            latency = max(0.0, time.time() - float(msg.timestamp or time.time()))
                            storage.incr_counter("metrics.delivery_latency_sum_seconds", latency)
                            storage.incr_counter("metrics.delivery_latency_count", 1.0)
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
                            storage.mark_outbox_delivered(msg.msg_id)
                        else:
                            storage.incr_counter("metrics.delivery_retry_total", 1.0)
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
                            if not _from_outbox:
                                storage.mark_outbox_attempt(msg.msg_id, 1, time.time() + self.retry_backoff_base, "pending")
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

                    if should_track_delivery and not _from_outbox:
                        storage.incr_counter("metrics.delivery_retry_total", 1.0)
                        storage.mark_outbox_attempt(msg.msg_id, attempt, time.time() + self.retry_backoff_base * (2 ** (attempt - 1)), "pending")

                    if attempt < self.max_retries:
                        time.sleep(self.retry_backoff_base * (2 ** (attempt - 1)))
        finally:
            self._release_send_slot(peer_id)

        logger.error(f"Failed to send to {ip}:{port}: {last_error}")
        if should_track_delivery:
            storage.incr_counter("metrics.delivery_fail_total", 1.0)
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

    def get_delivery_diagnostics(self) -> dict:
        with self._delivery_lock:
            return {
                "pending_deliveries": len(self._delivery_events),
                "delivery_status_count": len(self._delivery_status),
            }

    def get_queue_diagnostics(self) -> dict:
        return {
            "outbox_pending": storage.outbox_pending_count(),
        }


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


def make_seed_pair_message(peer_id_target: str, seed: str) -> Message:
    """
    SEED_PAIR handshake — notifies a peer that we've activated seed-pairing.
    The actual seed is exchanged out-of-band (shown in UI).
    """
    return Message(
        msg_type=    MsgType.SEED_PAIR,
        sender_id=   NODE_ID,
        sender_name= NODE_NAME,
        payload=     {"target": peer_id_target, "status": "paired", "seed": seed},
    )


