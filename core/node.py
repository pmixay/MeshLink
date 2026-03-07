"""
MeshLink — Node Orchestrator
Integrates all subsystems: crypto, discovery, messaging, file transfer, media.

Security (always-on trusted-only mode):
  - E2E encryption for all text messages (AES-256-GCM via X25519 ECDH)
  - Ed25519 message signing + signature verification
  - Seed-pairing: 6-char shared code → trusted session (REQUIRED before chat)
  - Mesh flooding with TTL + LRU deduplication
  - Per-peer rate limiting + auto-blacklist
"""

import base64
import collections
import time
import logging
import threading
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable

from .config import (
    NODE_ID, NODE_NAME, LOCAL_IP,
    TCP_PORT, MEDIA_PORT, FILE_PORT, DOWNLOADS_DIR,
    MESH_TTL_DEFAULT, MESH_LRU_MAX_SIZE,
    RATE_LIMIT_MAX_MSGS, RATE_LIMIT_WINDOW, RATE_LIMIT_BAN_SECS,
    SESSION_MAINTENANCE_INTERVAL_SECONDS,
    MESH_RELAY_FANOUT_MIN, MESH_RELAY_FANOUT_MAX, MESH_RELAY_BACKPRESSURE_MAX_PENDING,
    SEED_ALPHABET,
)
from .crypto import CryptoManager
from .discovery import DiscoveryService, PeerInfo
from .messaging import (
    MessageServer, Message, MsgType,
    make_text_message, make_call_invite, make_key_exchange,
    make_seed_pair_message,
    persist_chat_entry,
)
from .file_transfer import FileTransferManager
from .media import MediaEngine, CallState
from . import storage

logger = logging.getLogger("meshlink.node")


class _LRUSet:
    def __init__(self, max_size: int):
        self._max   = max_size
        self._store: collections.OrderedDict = collections.OrderedDict()
        self._lock  = threading.Lock()

    def contains(self, key: str) -> bool:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                return True
            return False

    def add(self, key: str):
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                return
            self._store[key] = True
            if len(self._store) > self._max:
                self._store.popitem(last=False)


class _RateLimiter:
    def __init__(self):
        self._windows:   Dict[str, collections.deque] = {}
        self._banned:    Dict[str, float] = {}
        self._blacklist: set = set()
        self._lock = threading.Lock()

    def is_allowed(self, peer_id: str) -> bool:
        with self._lock:
            now = time.time()
            if peer_id in self._blacklist:
                return False
            ban_expiry = self._banned.get(peer_id, 0)
            if ban_expiry > now:
                return False
            elif ban_expiry:
                del self._banned[peer_id]

            dq = self._windows.setdefault(peer_id, collections.deque())
            cutoff = now - RATE_LIMIT_WINDOW
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= RATE_LIMIT_MAX_MSGS:
                self._banned[peer_id] = now + RATE_LIMIT_BAN_SECS
                logger.warning(f"Rate limit exceeded by {peer_id}")
                return False
            dq.append(now)
            return True

    def blacklist_add(self, peer_id: str):
        with self._lock:
            self._blacklist.add(peer_id)

    def blacklist_remove(self, peer_id: str):
        with self._lock:
            self._blacklist.discard(peer_id)
            self._banned.pop(peer_id, None)

    def get_blacklist(self) -> list:
        with self._lock:
            return list(self._blacklist)

    def get_banned(self) -> dict:
        with self._lock:
            now = time.time()
            return {pid: round(exp - now, 1) for pid, exp in self._banned.items() if exp > now}


@dataclass
class ChatMessage:
    msg_id:      str
    sender_id:   str
    sender_name: str
    text:        str
    timestamp:   float
    is_me:       bool  = False
    msg_type:    str   = "text"
    file_url:    str   = ""
    file_name:   str   = ""
    file_size:   int   = 0
    signed:      bool  = False
    encrypted:   bool  = False
    status:      str   = ""
    # Security indicator: "secure" (both signed+encrypted), "partial", "none"
    security:    str   = "none"

    def to_dict(self):
        # Compute security level
        if self.signed and self.encrypted:
            sec = "secure"
        elif self.signed or self.encrypted:
            sec = "partial"
        else:
            sec = "none"

        d = {
            "msg_id":      self.msg_id,
            "sender_id":   self.sender_id,
            "sender_name": self.sender_name,
            "text":        self.text,
            "timestamp":   self.timestamp,
            "is_me":       self.is_me,
            "msg_type":    self.msg_type,
            "security":    sec,
            "status":      self.status,
        }
        if self.msg_type == "file":
            d["file_url"]  = self.file_url
            d["file_name"] = self.file_name
            d["file_size"] = self.file_size
        return d


class MeshNode:

    def __init__(self):
        self.crypto   = CryptoManager()
        self.discovery = DiscoveryService(
            public_key_b64=  self.crypto.public_key_b64,
            signing_key_b64= self.crypto.signing_key_b64,
        )
        self.msg_server = MessageServer()
        self.file_mgr   = FileTransferManager()
        self.media      = MediaEngine()

        self.chats:             Dict[str, List[ChatMessage]] = {}
        self._chat_lock         = threading.Lock()
        self.current_call_peer: Optional[str] = None

        self._seen_msgs  = _LRUSet(MESH_LRU_MAX_SIZE)
        self._rate_limiter = _RateLimiter()
        self._security_events = collections.deque(maxlen=1000)
        self._security_lock = threading.Lock()
        self._running = False
        self._started_at = time.time()
        self._session_maintenance_thread: Optional[threading.Thread] = None

        self._metrics_lock = threading.Lock()
        self._metrics = {
            "security_events_total": 0,
            "relay_backpressure_drops_total": 0,
            "session_rotations_total": 0,
            "session_expired_total": 0,
            "messages_sent_total": 0,
            "messages_received_total": 0,
            "messages_encrypted_total": 0,
            "policy_drops_total": 0,
        }

        self._event_handlers: Dict[str, List[Callable]] = {}
        self._relay_pressure_lock = threading.Lock()
        self._relay_pending = 0
        self.expected_seeds: Dict[str, str] = {}  # peer_id -> expected seed for pairing
        self._setup_handlers()

    def _inc_metric(self, key: str, delta: int = 1):
        with self._metrics_lock:
            self._metrics[key] = int(self._metrics.get(key, 0)) + int(delta)

    def _is_trusted_allowed(self, peer_id: str, channel: str, direction: str, msg_id: str = "") -> bool:
        """Always enforce trusted-only policy. Returns True only for trusted peers."""
        if self.crypto.is_trusted(peer_id):
            return True
        self._record_security_event(f"{direction}_{channel}_blocked_untrusted", peer_id, {
            "msg_id": msg_id, "channel": channel, "direction": direction,
        })
        self._inc_metric("policy_drops_total", 1)
        return False

    def _on_peer_activity(self, peer_id: str):
        if not peer_id:
            return
        if self.crypto.has_session(peer_id) and not self.crypto.check_session_ttl(peer_id):
            self._record_security_event("session_expired", peer_id, {})
            self._inc_metric("session_expired_total", 1)
            return
        if self.crypto.should_rotate_session(peer_id):
            if self.crypto.rotate_session(peer_id):
                self._record_security_event("session_rotated", peer_id, {"trigger": "peer_activity"})
                self._inc_metric("session_rotations_total", 1)
        self.crypto.touch_session(peer_id)

    def _session_maintenance_loop(self):
        interval = max(1.0, float(SESSION_MAINTENANCE_INTERVAL_SECONDS))
        while self._running:
            try:
                result = self.crypto.maintain_sessions()
                for pid in result.get("expired", []):
                    self._record_security_event("session_expired", pid, {"trigger": "maintenance"})
                    self._inc_metric("session_expired_total", 1)
                for pid in result.get("rotated", []):
                    self._record_security_event("session_rotated", pid, {"trigger": "maintenance"})
                    self._inc_metric("session_rotations_total", 1)
            except Exception as e:
                logger.debug(f"Session maintenance error: {e}")
            time.sleep(interval)

    def _upsert_chat_message(self, peer_id: str, chat_msg: ChatMessage) -> bool:
        if not peer_id or not chat_msg.msg_id:
            return False
        with self._chat_lock:
            bucket = self.chats.setdefault(peer_id, [])
            for item in bucket:
                if item.msg_id == chat_msg.msg_id:
                    if chat_msg.status and (not item.status or item.status != chat_msg.status):
                        item.status = chat_msg.status
                    return False
            bucket.append(chat_msg)
        return True

    def _record_security_event(self, event_type: str, peer_id: str = "", details: Optional[dict] = None):
        event = {
            "ts": time.time(),
            "event": event_type,
            "peer_id": peer_id,
            "details": details or {},
        }
        with self._security_lock:
            self._security_events.append(event)
        self._inc_metric("security_events_total", 1)
        self._emit("security_event", event)

    def _setup_handlers(self):
        self.discovery.on_peer_joined = self._on_peer_joined
        self.discovery.on_peer_left   = self._on_peer_left

        self.msg_server.on(MsgType.TEXT,         self._on_text_message)
        self.msg_server.on(MsgType.KEY_EXCHANGE, self._on_key_exchange)
        self.msg_server.on(MsgType.CALL_INVITE,  self._on_call_invite)
        self.msg_server.on(MsgType.CALL_ACCEPT,  self._on_call_accept)
        self.msg_server.on(MsgType.CALL_REJECT,  self._on_call_reject)
        self.msg_server.on(MsgType.CALL_END,     self._on_call_end)
        self.msg_server.on(MsgType.TYPING,       self._on_typing)
        self.msg_server.on(MsgType.MESH_RELAY,   self._on_mesh_relay)
        self.msg_server.on(MsgType.SEED_PAIR,    self._on_seed_pair)
        self.msg_server.on_delivery_status = self._on_delivery_status

        self.msg_server.on(MsgType.WEBRTC_OFFER,  self._on_webrtc_signal)
        self.msg_server.on(MsgType.WEBRTC_ANSWER, self._on_webrtc_signal)
        self.msg_server.on(MsgType.WEBRTC_ICE,    self._on_webrtc_signal)

        self.file_mgr.on_progress = self._on_file_progress
        self.file_mgr.on_complete = self._on_file_complete

    def _on_delivery_status(self, data: dict):
        peer_id = data.get("peer_id", "")
        msg_id = data.get("msg_id", "")
        status = data.get("status", "")
        if not peer_id or not msg_id or not status:
            return
        with self._chat_lock:
            bucket = self.chats.get(peer_id, [])
            for item in reversed(bucket):
                if item.msg_id == msg_id and item.is_me:
                    item.status = status
                    break
        persist_chat_entry({
            "kind": "status_update", "peer_id": peer_id,
            "msg_id": msg_id, "status": status, "timestamp": time.time(),
        })
        self._emit("message_status", {
            "peer_id": peer_id, "msg_id": msg_id, "status": status,
            "timestamp": data.get("timestamp", time.time()),
        })

    # ── Event system ─────────────────────────────────────────────────────────

    def on(self, event: str, handler: Callable):
        self._event_handlers.setdefault(event, []).append(handler)

    def _emit(self, event: str, data=None):
        for h in self._event_handlers.get(event, []):
            try:
                h(data)
            except Exception as e:
                logger.error(f"Event handler error ({event}): {e}")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        logger.info(f"Starting MeshLink: {NODE_NAME} ({NODE_ID})")
        self._running = True
        self.discovery.start()
        self.msg_server.start()
        self.file_mgr.start()
        self.media.start()
        threading.Thread(target=self._stats_loop, daemon=True, name="stats-loop").start()
        self._session_maintenance_thread = threading.Thread(
            target=self._session_maintenance_loop, daemon=True, name="session-maintenance",
        )
        self._session_maintenance_thread.start()
        logger.info("All subsystems started.")

    def stop(self):
        self._running = False
        self.media.stop()
        self.file_mgr.stop()
        self.msg_server.stop()
        self.discovery.stop()
        logger.info("MeshLink stopped.")

    def _stats_loop(self):
        while self._running:
            try:
                self._emit("statistics", self.get_statistics())
            except Exception:
                pass
            time.sleep(2.0)

    # ── Security gate ────────────────────────────────────────────────────────

    def _security_check(self, msg: Message) -> bool:
        if not self._rate_limiter.is_allowed(msg.sender_id):
            self._record_security_event("dropped_rate_limited", msg.sender_id, {"msg_id": msg.msg_id})
            return False
        msg_id = msg.msg_id
        if msg_id and self._seen_msgs.contains(msg_id):
            return False
        if msg_id:
            self._seen_msgs.add(msg_id)
        return True

    # ── Discovery callbacks ──────────────────────────────────────────────────

    def _on_peer_joined(self, peer: PeerInfo):
        if peer.public_key:
            self.crypto.establish_session(peer.peer_id, peer.public_key, peer.signing_key)
        key_msg = make_key_exchange(self.crypto.public_key_b64, self.crypto.signing_key_b64)
        self.msg_server.send_to_peer(peer.ip, peer.tcp_port, key_msg, peer.peer_id)
        self._emit("peer_joined", peer.to_dict())

    def _on_peer_left(self, peer: PeerInfo):
        self._emit("peer_left", peer.to_dict())

    # ── Message callbacks ────────────────────────────────────────────────────

    def _on_text_message(self, msg: Message):
        if not self._security_check(msg):
            return
        self._on_peer_activity(msg.sender_id)

        # Check trust policy
        if not self._is_trusted_allowed(msg.sender_id, "text", "incoming", msg_id=msg.msg_id):
            return

        text    = msg.payload.get("text", "")
        was_enc = False
        if msg.payload.get("encrypted") and self.crypto.has_session(msg.sender_id):
            try:
                ct   = base64.b64decode(msg.payload["ciphertext"])
                text = self.crypto.decrypt_from(msg.sender_id, ct).decode("utf-8")
                was_enc = True
            except Exception as e:
                logger.warning(f"Decryption failed from {msg.sender_id}: {e}")
                text = "[decryption failed]"

        signed = False
        if msg.signature:
            signed = self.crypto.verify_from(msg.sender_id, msg.canonical_bytes(), msg.signature)

        self._inc_metric("messages_received_total", 1)
        if was_enc:
            self._inc_metric("messages_encrypted_total", 1)

        chat_msg = ChatMessage(
            msg_id=msg.msg_id, sender_id=msg.sender_id, sender_name=msg.sender_name,
            text=text, timestamp=msg.timestamp, is_me=False,
            signed=signed, encrypted=was_enc, status="delivered",
        )
        is_new = self._upsert_chat_message(msg.sender_id, chat_msg)
        if is_new:
            self._emit("message", chat_msg.to_dict())

    def _on_key_exchange(self, msg: Message):
        if not self._security_check(msg):
            return
        self._on_peer_activity(msg.sender_id)
        pub_key  = msg.payload.get("public_key",  "")
        sign_key = msg.payload.get("signing_key", "")
        if pub_key:
            self.crypto.establish_session(msg.sender_id, pub_key, sign_key)
        elif sign_key:
            self.crypto.register_peer_signing_key(msg.sender_id, sign_key)

    def _on_call_invite(self, msg: Message):
        if not self._security_check(msg):
            return
        if not self._is_trusted_allowed(msg.sender_id, "call", "incoming", msg_id=msg.msg_id):
            return
        self._on_peer_activity(msg.sender_id)
        self.current_call_peer = msg.sender_id
        self._emit("call_incoming", {
            "peer_id": msg.sender_id, "peer_name": msg.sender_name,
            "call_type": msg.payload.get("call_type", "audio"),
        })

    def _on_call_accept(self, msg: Message):
        peer = self.discovery.get_peer(msg.sender_id)
        if peer:
            self.media.start_call(peer.ip, peer.media_port)
        self._emit("call_accepted", {"peer_id": msg.sender_id})

    def _on_call_reject(self, msg: Message):
        self.current_call_peer = None
        self.media.end_call()
        self._emit("call_rejected", {"peer_id": msg.sender_id})

    def _on_call_end(self, msg: Message):
        self.current_call_peer = None
        self.media.end_call()
        self._emit("call_ended", {"peer_id": msg.sender_id})

    def _on_typing(self, msg: Message):
        if not self._rate_limiter.is_allowed(msg.sender_id):
            return
        self._emit("typing", {"peer_id": msg.sender_id, "peer_name": msg.sender_name})

    def _on_seed_pair(self, msg: Message):
        peer_id = msg.sender_id
        seed = msg.payload.get("seed", "").upper()
        expected_seed = self.expected_seeds.get(peer_id, "").upper()
        if seed and expected_seed == seed:
            # Mutual pairing: establish session with the expected seed
            self.crypto.establish_seed_session(peer_id, seed)
            self._record_security_event("seed_paired_mutual", peer_id, {"peer_name": msg.sender_name})
            self.expected_seeds.pop(peer_id, None)
            self._emit("seed_pair_result", {"ok": True, "peer_id": peer_id})
        self._emit("seed_paired", {"peer_id": peer_id, "peer_name": msg.sender_name})

    # ── Mesh flooding / relay ────────────────────────────────────────────────

    def _on_mesh_relay(self, msg: Message):
        if not self._security_check(msg):
            return
        self._on_peer_activity(msg.sender_id)

        if msg.signature:
            if not self.crypto.verify_from(msg.sender_id, msg.canonical_bytes(), msg.signature):
                self._record_security_event("dropped_invalid_relay_signature", msg.sender_id, {"msg_id": msg.msg_id})
                return

        inner_type = msg.payload.get("inner_type")
        inner_payload = msg.payload.get("inner_payload", {})

        if inner_type == MsgType.TEXT:
            origin_id = inner_payload.get("origin_id", msg.sender_id)
            origin_name = inner_payload.get("origin_name", msg.sender_name)
            if not self._is_trusted_allowed(origin_id, "text", "incoming", msg_id=msg.msg_id):
                return
            text = inner_payload.get("text", "")
            chat_msg = ChatMessage(
                msg_id=msg.msg_id, sender_id=origin_id, sender_name=origin_name,
                text=text, timestamp=msg.timestamp, is_me=False, status="delivered",
            )
            is_new = self._upsert_chat_message(origin_id, chat_msg)
            if is_new:
                self._emit("message", chat_msg.to_dict())

        if msg.ttl > 1:
            self._flood_relay(msg)

    def _flood_relay(self, msg: Message):
        peers = self.discovery.get_peers()
        pending_outbox = storage.outbox_pending_count()
        with self._relay_pressure_lock:
            pressure = self._relay_pending
        if pending_outbox + pressure >= max(200, int(MESH_RELAY_BACKPRESSURE_MAX_PENDING)):
            self._record_security_event("relay_backpressure_drop", msg.sender_id, {"msg_id": msg.msg_id})
            return

        already_visited = set(msg.relay_path)
        candidates = []
        for peer_dict in peers:
            pid = peer_dict["peer_id"]
            if pid in already_visited or pid == msg.sender_id:
                continue
            peer = self.discovery.get_peer(pid)
            if peer:
                candidates.append((pid, peer))

        if not candidates:
            return

        cap_min = max(1, int(MESH_RELAY_FANOUT_MIN))
        cap_max = max(cap_min, int(MESH_RELAY_FANOUT_MAX))
        ratio = min(1.0, (pending_outbox + pressure) / float(max(1, int(MESH_RELAY_BACKPRESSURE_MAX_PENDING))))
        fanout = int(round(cap_max - (cap_max - cap_min) * ratio))
        fanout = max(cap_min, min(cap_max, fanout, len(candidates)))
        random.shuffle(candidates)

        for pid, peer in candidates[:fanout]:
            relay_msg = Message(
                msg_type=MsgType.MESH_RELAY, sender_id=NODE_ID, sender_name=NODE_NAME,
                payload=msg.payload, timestamp=msg.timestamp, msg_id=msg.msg_id,
                ttl=msg.ttl - 1, relay_path=msg.relay_path + [NODE_ID],
            )
            if not relay_msg.msg_id:
                relay_msg.msg_id = f"{NODE_ID}-{time.time_ns()}"
            relay_msg.signature = self.crypto.sign(relay_msg.canonical_bytes())
            with self._relay_pressure_lock:
                self._relay_pending += 1
            try:
                self.msg_server.send_to_peer(peer.ip, peer.tcp_port, relay_msg, pid)
            finally:
                with self._relay_pressure_lock:
                    self._relay_pending = max(0, self._relay_pending - 1)

    # ── WebRTC signaling relay ────────────────────────────────────────────────

    def _on_webrtc_signal(self, msg: Message):
        event_map = {
            MsgType.WEBRTC_OFFER: "webrtc_offer",
            MsgType.WEBRTC_ANSWER: "webrtc_answer",
            MsgType.WEBRTC_ICE: "webrtc_ice",
        }
        event = event_map.get(msg.msg_type)
        if event:
            self._emit(event, {"peer_id": msg.sender_id, "peer_name": msg.sender_name, **msg.payload})

    def send_webrtc_signal(self, peer_id: str, signal_type: str, payload: dict):
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return False
        type_map = {"offer": MsgType.WEBRTC_OFFER, "answer": MsgType.WEBRTC_ANSWER, "ice": MsgType.WEBRTC_ICE}
        msg_type = type_map.get(signal_type)
        if not msg_type:
            return False
        msg = Message(msg_type=msg_type, sender_id=NODE_ID, sender_name=NODE_NAME, payload=payload)
        return self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)

    # ── File callbacks ────────────────────────────────────────────────────────

    def _on_file_progress(self, transfer):
        self._emit("file_progress", transfer.to_dict())

    def _on_file_complete(self, transfer):
        self._emit("file_complete", transfer.to_dict())
        if transfer.direction == "recv":
            if not self._is_trusted_allowed(transfer.peer_id, "file", "incoming"):
                return
            saved = transfer.saved_as or transfer.filename
            chat_msg = ChatMessage(
                msg_id=f"file-{transfer.file_id}", sender_id=transfer.peer_id,
                sender_name=transfer.peer_name or "Peer", text=transfer.filename,
                timestamp=time.time(), is_me=False, msg_type="file",
                file_url=f"/downloads/{saved}", file_name=transfer.filename,
                file_size=transfer.filesize,
            )
            if self._upsert_chat_message(transfer.peer_id, chat_msg):
                d = chat_msg.to_dict()
                d["peer_id"] = transfer.peer_id
                self._emit("message", d)
        elif transfer.direction == "send":
            chat_msg = ChatMessage(
                msg_id=f"file-{transfer.file_id}", sender_id=NODE_ID,
                sender_name=NODE_NAME, text=transfer.filename,
                timestamp=time.time(), is_me=True, msg_type="file",
                file_name=transfer.filename, file_size=transfer.filesize,
            )
            if self._upsert_chat_message(transfer.peer_id, chat_msg):
                # UI groups chats by peer_id, not by sender_id. For outgoing file messages
                # sender_id is our NODE_ID, so include explicit peer_id for correct routing.
                d = chat_msg.to_dict()
                d["peer_id"] = transfer.peer_id
                self._emit("message", d)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_info(self) -> dict:
        return {
            "node_id":     NODE_ID,
            "node_name":   NODE_NAME,
            "encryption":  self.crypto.keypair.private_key is not None,
            "trusted_only": True,
        }

    def get_peers(self) -> list:
        peers = self.discovery.get_peers()
        # Strip IP addresses from peer info sent to UI (metadata minimization)
        for p in peers:
            p.pop("ip", None)
            # Derive trust from crypto state (seed-pairing) so UI can восстановиться
            # после перезагрузки страницы.
            try:
                pid = p.get("peer_id", "")
                if pid:
                    p["trusted"] = bool(self.crypto.is_trusted(pid))
            except Exception:
                # Best-effort: never break peers list rendering due to trust check
                pass
        return peers

    def get_peer_list_internal(self) -> list:
        """Internal use only — includes IPs for backend operations."""
        return self.discovery.get_peers()

    def add_manual_peer(self, ip: str, tcp_port: int, name: str = ""):
        """Manually add a peer."""
        self.discovery.add_manual_peer(ip, tcp_port, name)

    def send_text(self, peer_id: str, text: str) -> Optional[dict]:
        """
        Send an encrypted + signed text message to a peer.
        Returns the chat message dict on success, None on failure.
        """
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return None

        self._on_peer_activity(peer_id)
        if not self._is_trusted_allowed(peer_id, "text", "outgoing"):
            return None

        encrypted = False
        ciphertext_b64 = ""
        if self.crypto.has_session(peer_id):
            try:
                ct = self.crypto.encrypt_for(peer_id, text.encode("utf-8"))
                ciphertext_b64 = base64.b64encode(ct).decode()
                encrypted = True
            except Exception as e:
                logger.warning(f"Encryption failed for {peer_id}: {e}")

        msg = make_text_message(text, encrypted=encrypted, ciphertext_b64=ciphertext_b64)
        if not msg.msg_id:
            msg.msg_id = f"{msg.sender_id}-{time.time_ns()}"

        canonical = msg.canonical_bytes()
        msg.signature = self.crypto.sign(canonical)

        success = self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)
        if success:
            status = self.msg_server.get_delivery_status(msg.msg_id)
            if status not in ("sent", "delivered", "failed"):
                status = "sent"
            chat_msg = ChatMessage(
                msg_id=msg.msg_id, sender_id=NODE_ID, sender_name=NODE_NAME,
                text=text, timestamp=time.time(), is_me=True,
                signed=True, encrypted=encrypted, status=status,
            )
            # Use upsert to prevent duplicates
            self._upsert_chat_message(peer_id, chat_msg)
            self._inc_metric("messages_sent_total", 1)
            if encrypted:
                self._inc_metric("messages_encrypted_total", 1)
            persist_chat_entry({
                "peer_id": peer_id, "msg_id": chat_msg.msg_id,
                "sender_id": NODE_ID, "sender_name": NODE_NAME,
                "text": text, "timestamp": chat_msg.timestamp,
                "is_me": True, "msg_type": "text",
                "signed": True, "encrypted": encrypted, "status": status,
            })
            return chat_msg.to_dict()
        return None

    def send_typing(self, peer_id: str):
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return
        msg = Message(msg_type=MsgType.TYPING, sender_id=NODE_ID,
                      sender_name=NODE_NAME, payload={})
        self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)

    def send_file(self, peer_id: str, filepath: str) -> Optional[str]:
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return None
        self._on_peer_activity(peer_id)
        if not self._is_trusted_allowed(peer_id, "file", "outgoing"):
            return None
        return self.file_mgr.send_file(filepath, peer.ip, peer.file_port, peer_id)

    # ── Seed-pairing API ─────────────────────────────────────────────────────

    def generate_pairing_seed(self, peer_id: str) -> str:
        seed = CryptoManager.generate_seed()
        self.expected_seeds[peer_id] = seed
        return seed

    def pair_with_seed(self, peer_id: str, seed: str) -> dict:
        if not seed or len(seed) != 6 or not all(c in SEED_ALPHABET for c in seed.upper()):
            return {"ok": False, "reason": "invalid"}
        if seed == self.expected_seeds.get(peer_id):
            return {"ok": False, "reason": "own_seed"}
        self.expected_seeds[peer_id] = seed
        self.crypto.establish_seed_session(peer_id, seed)
        self._record_security_event("seed_paired_local", peer_id, {})
        peer = self.discovery.get_peer(peer_id)
        if peer:
            notify = make_seed_pair_message(peer_id, seed)
            self.msg_server.send_to_peer(peer.ip, peer.tcp_port, notify, peer_id)
        return {"ok": True}

    def is_peer_trusted(self, peer_id: str) -> bool:
        return self.crypto.is_trusted(peer_id)

    # ── Rate-limit / Blacklist API ────────────────────────────────────────────

    def blacklist_peer(self, peer_id: str):
        self._rate_limiter.blacklist_add(peer_id)
        self._record_security_event("blacklist_added", peer_id, {})

    def unblacklist_peer(self, peer_id: str):
        self._rate_limiter.blacklist_remove(peer_id)

    def get_blacklist(self) -> list:
        return self._rate_limiter.get_blacklist()

    def get_banned_peers(self) -> dict:
        return self._rate_limiter.get_banned()

    def get_security_events(self, limit: int = 200) -> list:
        with self._security_lock:
            events = list(self._security_events)
        return events[-min(2000, max(1, limit)):]

    def get_chat(self, peer_id: str) -> list:
        """Get chat history for a peer as list of dicts."""
        return [msg.__dict__ for msg in self.chats.get(peer_id, [])]

    # ── Unified Statistics API ────────────────────────────────────────────────

    def get_statistics(self) -> dict:
        """Single unified statistics snapshot for the UI."""
        with self._metrics_lock:
            m = dict(self._metrics)
        counters = storage.get_counters("metrics.")
        media = self.media.get_stats()
        sends = self.file_mgr.get_send_diagnostics()
        session_snap = self.crypto.get_session_snapshot()
        peers = self.discovery.get_peers()

        with self._relay_pressure_lock:
            relay_pending = self._relay_pending

        latency_sum = float(counters.get("metrics.delivery_latency_sum_seconds", 0.0))
        latency_count = float(counters.get("metrics.delivery_latency_count", 0.0))
        retry_total = float(counters.get("metrics.delivery_retry_total", 0.0))
        fail_total = float(counters.get("metrics.delivery_fail_total", 0.0))

        return {
            # Network
            "active_peers": len(peers),
            "outbox_pending": int(storage.outbox_pending_count()),
            "relay_pending": int(relay_pending),

            # Messaging
            "messages_sent": int(m.get("messages_sent_total", 0)),
            "messages_received": int(m.get("messages_received_total", 0)),
            "messages_encrypted": int(m.get("messages_encrypted_total", 0)),
            "delivery_retries": int(retry_total),
            "delivery_failures": int(fail_total),
            "avg_delivery_latency_ms": round((latency_sum / latency_count * 1000) if latency_count > 0 else 0, 1),

            # Security
            "security_events": int(m.get("security_events_total", 0)),
            "policy_drops": int(m.get("policy_drops_total", 0)),
            "session_rotations": int(m.get("session_rotations_total", 0)),
            "sessions_active": sum(1 for _, x in session_snap.items() if x.get("active")),
            "blacklist_count": len(self.get_blacklist()),
            "banned_count": len(self.get_banned_peers()),

            # Relay
            "relay_drops": int(m.get("relay_backpressure_drops_total", 0)),

            # File transfers
            "active_file_sends": int(sends.get("active_sends", 0)),
            "file_send_slots": int(sends.get("max_active_sends", 0)),

            # Media (from UDP engine)
            "media_uplink_kbps": round(float(media.get("uplink_bitrate_kbps", 0.0)), 1),
            "media_downlink_kbps": round(float(media.get("downlink_bitrate_kbps", 0.0)), 1),
            "media_latency_ms": round(float(media.get("latency_ms", 0.0)), 1),
            "media_loss_pct": round(float(media.get("loss_percent", 0.0)), 2),
            "media_jitter_ms": round(float(media.get("jitter_ms", 0.0)), 1),

            "uptime_seconds": round(time.time() - self._started_at, 0),
            "ts": time.time(),
        }

    def is_ready(self) -> bool:
        return bool(self._running and self.msg_server._running and self.file_mgr._running and self.media._running)

    def get_health_snapshot(self) -> dict:
        return {
            "status": "ok", "node_id": NODE_ID,
            "uptime_seconds": max(0.0, time.time() - self._started_at),
            "active_peers": len(self.discovery.get_peers()),
        }

    def get_security_snapshot(self) -> dict:
        from .config import TRUSTED_ONLY_PRIVATE_CHATS, TRUSTED_ONLY_CALL
        return {
            "trusted_only_private_chats": TRUSTED_ONLY_PRIVATE_CHATS,
            "trusted_only_call": TRUSTED_ONLY_CALL,
            "blacklist": self.get_blacklist(),
            "banned": self.get_banned_peers(),
            "events": self.get_security_events(20),
        }

    # ── Call control ─────────────────────────────────────────────────────────

    def start_call(self, peer_id: str, call_type: str = "audio") -> bool:
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return False
        if not self._is_trusted_allowed(peer_id, "call", "outgoing"):
            return False
        self.current_call_peer = peer_id
        msg = make_call_invite(call_type)
        success = self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)
        if success:
            self._emit("call_outgoing", {
                "peer_id": peer_id, "peer_name": peer.name, "call_type": call_type,
            })
        return success

    def accept_call(self, peer_id: str):
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return
        msg = Message(msg_type=MsgType.CALL_ACCEPT, sender_id=NODE_ID,
                      sender_name=NODE_NAME, payload={})
        self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)
        self.media.start_call(peer.ip, peer.media_port)
        self.current_call_peer = peer_id
        self._emit("call_accepted", {"peer_id": peer_id})

    def reject_call(self, peer_id: str):
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return
        msg = Message(msg_type=MsgType.CALL_REJECT, sender_id=NODE_ID,
                      sender_name=NODE_NAME, payload={})
        self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)
        self.current_call_peer = None

    def end_call(self):
        call_peer = self.current_call_peer
        if call_peer:
            peer = self.discovery.get_peer(call_peer)
            if peer:
                msg = Message(msg_type=MsgType.CALL_END, sender_id=NODE_ID,
                              sender_name=NODE_NAME, payload={})
                self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, call_peer)
        self.current_call_peer = None
        self.media.end_call()
        if call_peer:
            self._emit("call_ended", {"peer_id": call_peer})

    def get_transfers(self) -> list:
        return self.file_mgr.get_transfers()

    def get_media_stats(self) -> dict:
        return self.media.get_stats()
