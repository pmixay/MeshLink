"""
MeshLink — Node Orchestrator
Integrates all subsystems: crypto, discovery, messaging, file transfer, media.

Security features (Developer 1):
  - E2E encryption for all text messages (AES-256-GCM via X25519 ECDH)
  - Ed25519 message signing + signature verification
  - Seed-pairing: 6-char shared code → trusted session
  - Mesh flooding with TTL decrement + LRU deduplication (A→B→C relay)
  - Per-peer rate limiting + auto-blacklist at backend level
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
    TRUSTED_ONLY_PRIVATE_CHATS,
    TRUSTED_ONLY_TEXT, TRUSTED_ONLY_FILE, TRUSTED_ONLY_CALL,
    SESSION_MAINTENANCE_INTERVAL_SECONDS,
    MESH_RELAY_FANOUT_MIN, MESH_RELAY_FANOUT_MAX, MESH_RELAY_BACKPRESSURE_MAX_PENDING,
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


# ── LRU cache for seen message IDs ──────────────────────────────────────────

class _LRUSet:
    """Thread-safe fixed-capacity set based on OrderedDict (LRU eviction)."""

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
                self._store.popitem(last=False)  # evict oldest


# ── Per-peer rate limiter ────────────────────────────────────────────────────

class _RateLimiter:
    """
    Sliding-window rate limiter per peer.
    Exceeding RATE_LIMIT_MAX_MSGS within RATE_LIMIT_WINDOW seconds
    causes a temporary ban for RATE_LIMIT_BAN_SECS.
    """

    def __init__(self):
        # peer_id → deque of timestamps
        self._windows:   Dict[str, collections.deque] = {}
        # peer_id → ban-expiry timestamp (0 = not banned)
        self._banned:    Dict[str, float] = {}
        # Permanent blacklist (manually added peer IDs)
        self._blacklist: set = set()
        self._lock = threading.Lock()

    def is_allowed(self, peer_id: str) -> bool:
        """Returns True if the peer is allowed to send a message right now."""
        with self._lock:
            now = time.time()

            # Permanent blacklist check
            if peer_id in self._blacklist:
                return False

            # Temporary ban check
            ban_expiry = self._banned.get(peer_id, 0)
            if ban_expiry > now:
                return False
            elif ban_expiry:
                # Ban expired — clear it
                del self._banned[peer_id]

            # Sliding-window rate check
            dq = self._windows.setdefault(peer_id, collections.deque())
            # Remove timestamps outside the window
            cutoff = now - RATE_LIMIT_WINDOW
            while dq and dq[0] < cutoff:
                dq.popleft()

            if len(dq) >= RATE_LIMIT_MAX_MSGS:
                # Rate exceeded → auto-ban
                self._banned[peer_id] = now + RATE_LIMIT_BAN_SECS
                logger.warning(
                    f"Rate limit exceeded by {peer_id} — auto-banned for "
                    f"{RATE_LIMIT_BAN_SECS}s"
                )
                return False

            dq.append(now)
            return True

    def blacklist_add(self, peer_id: str):
        with self._lock:
            self._blacklist.add(peer_id)
            logger.info(f"Peer {peer_id} added to permanent blacklist")

    def blacklist_remove(self, peer_id: str):
        with self._lock:
            self._blacklist.discard(peer_id)
            self._banned.pop(peer_id, None)
            logger.info(f"Peer {peer_id} removed from blacklist")

    def get_blacklist(self) -> list:
        with self._lock:
            return list(self._blacklist)

    def get_banned(self) -> dict:
        """Returns {peer_id: seconds_remaining} for all active temporary bans."""
        with self._lock:
            now = time.time()
            return {
                pid: round(exp - now, 1)
                for pid, exp in self._banned.items()
                if exp > now
            }


# ── Chat message dataclass ───────────────────────────────────────────────────

@dataclass
class ChatMessage:
    msg_id:      str
    sender_id:   str
    sender_name: str
    text:        str
    timestamp:   float
    is_me:       bool  = False
    msg_type:    str   = "text"   # text / file / system
    file_url:    str   = ""
    file_name:   str   = ""
    file_size:   int   = 0
    signed:      bool  = False    # True if signature was verified
    encrypted:   bool  = False    # True if message was E2E encrypted
    status:      str   = ""       # sent / delivered / failed (mostly for outgoing)

    def to_dict(self):
        d = {
            "msg_id":      self.msg_id,
            "sender_id":   self.sender_id,
            "sender_name": self.sender_name,
            "text":        self.text,
            "timestamp":   self.timestamp,
            "is_me":       self.is_me,
            "msg_type":    self.msg_type,
            "signed":      self.signed,
            "encrypted":   self.encrypted,
            "status":      self.status,
        }
        if self.msg_type == "file":
            d["file_url"]  = self.file_url
            d["file_name"] = self.file_name
            d["file_size"] = self.file_size
        return d


# ── MeshNode ─────────────────────────────────────────────────────────────────

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

        # Security subsystems
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
            "policy_drops_incoming_text_total": 0,
            "policy_drops_incoming_call_total": 0,
            "policy_drops_incoming_file_total": 0,
            "policy_blocks_outgoing_text_total": 0,
            "policy_blocks_outgoing_call_total": 0,
            "policy_blocks_outgoing_file_total": 0,
        }

        self._event_handlers: Dict[str, List[Callable]] = {}
        self._relay_pressure_lock = threading.Lock()
        self._relay_pending = 0
        self._setup_handlers()

    def _inc_metric(self, key: str, delta: int = 1):
        with self._metrics_lock:
            self._metrics[key] = int(self._metrics.get(key, 0)) + int(delta)

    def _trusted_policy_enabled(self, channel: str) -> bool:
        if channel == "text":
            return bool(TRUSTED_ONLY_TEXT or TRUSTED_ONLY_PRIVATE_CHATS)
        if channel == "file":
            return bool(TRUSTED_ONLY_FILE)
        if channel == "call":
            return bool(TRUSTED_ONLY_CALL)
        return False

    def _is_trusted_allowed(self, peer_id: str, channel: str, direction: str, msg_id: str = "") -> bool:
        if not self._trusted_policy_enabled(channel):
            return True
        if self.crypto.is_trusted(peer_id):
            return True

        event_name = f"{direction}_{channel}_blocked_untrusted"
        self._record_security_event(event_name, peer_id, {
            "msg_id": msg_id,
            "channel": channel,
            "direction": direction,
            "reason": f"trusted_only_{channel}",
        })
        if direction == "outgoing" and channel == "text":
            # Backward-compatible security event name.
            self._record_security_event("blocked_outgoing_untrusted", peer_id, {
                "msg_id": msg_id,
                "reason": "trusted_only_private_chats",
            })

        metric_map = {
            ("incoming", "text"): "policy_drops_incoming_text_total",
            ("incoming", "file"): "policy_drops_incoming_file_total",
            ("incoming", "call"): "policy_drops_incoming_call_total",
            ("outgoing", "text"): "policy_blocks_outgoing_text_total",
            ("outgoing", "file"): "policy_blocks_outgoing_file_total",
            ("outgoing", "call"): "policy_blocks_outgoing_call_total",
        }
        mk = metric_map.get((direction, channel))
        if mk:
            self._inc_metric(mk, 1)
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
        """Insert or update a chat message by msg_id. Returns True if inserted/newly updated path should emit."""
        if not peer_id or not chat_msg.msg_id:
            return False
        inserted = False
        with self._chat_lock:
            bucket = self.chats.setdefault(peer_id, [])
            for item in bucket:
                if item.msg_id == chat_msg.msg_id:
                    if chat_msg.status and (not item.status or item.status != chat_msg.status):
                        item.status = chat_msg.status
                    if chat_msg.text and not item.text:
                        item.text = chat_msg.text
                    return False
            bucket.append(chat_msg)
            inserted = True
        return inserted

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
        if event_type == "relay_backpressure_drop":
            self._inc_metric("relay_backpressure_drops_total", 1)
        self._emit("security_event", event)

    # ── Handler wiring ───────────────────────────────────────────────────────

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

        # WebRTC signaling relay
        self.msg_server.on(MsgType.WEBRTC_OFFER,  self._on_webrtc_signal)
        self.msg_server.on(MsgType.WEBRTC_ANSWER, self._on_webrtc_signal)
        self.msg_server.on(MsgType.WEBRTC_ICE,    self._on_webrtc_signal)

        # File transfer events
        self.file_mgr.on_progress = self._on_file_progress
        self.file_mgr.on_complete = self._on_file_complete

    def _on_delivery_status(self, data: dict):
        peer_id = data.get("peer_id", "")
        msg_id = data.get("msg_id", "")
        status = data.get("status", "")
        if not peer_id or not msg_id or not status:
            return

        updated = False
        with self._chat_lock:
            bucket = self.chats.get(peer_id, [])
            for item in reversed(bucket):
                if item.msg_id == msg_id and item.is_me:
                    item.status = status
                    updated = True
                    break

        persist_chat_entry({
            "kind": "status_update",
            "peer_id": peer_id,
            "msg_id": msg_id,
            "status": status,
            "timestamp": time.time(),
        })

        if updated:
            self._emit("message_status", {
                "peer_id": peer_id,
                "msg_id": msg_id,
                "status": status,
                "timestamp": data.get("timestamp", time.time()),
            })
        self._emit("network_diagnostics", self.get_network_diagnostics())

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
        logger.info(f"Starting MeshLink: {NODE_NAME} ({NODE_ID}) @ {LOCAL_IP}")
        self._running = True
        self.discovery.start()
        self.msg_server.start()
        self.file_mgr.start()
        self.media.start()
        threading.Thread(target=self._media_stats_loop, daemon=True, name="media-stats-loop").start()
        self._session_maintenance_thread = threading.Thread(
            target=self._session_maintenance_loop,
            daemon=True,
            name="session-maintenance-loop",
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

    # ── Security gate ────────────────────────────────────────────────────────

    def _security_check(self, msg: Message) -> bool:
        """
        Returns True if the message should be processed.
        Applies: rate limiting, blacklist check, deduplication.
        """
        # Rate limit / blacklist
        if not self._rate_limiter.is_allowed(msg.sender_id):
            logger.debug(f"Dropped message from rate-limited/blacklisted peer {msg.sender_id}")
            self._record_security_event("dropped_rate_limited_or_blacklisted", msg.sender_id, {
                "msg_id": msg.msg_id,
                "msg_type": int(msg.msg_type),
            })
            return False

        # LRU deduplication (also prevents relay loops)
        msg_id = msg.msg_id
        if msg_id and self._seen_msgs.contains(msg_id):
            logger.debug(f"Duplicate message dropped: {msg_id}")
            self._record_security_event("dropped_duplicate", msg.sender_id, {
                "msg_id": msg_id,
                "msg_type": int(msg.msg_type),
            })
            return False
        if msg_id:
            self._seen_msgs.add(msg_id)

        if msg.msg_type == MsgType.TEXT:
            if not self._is_trusted_allowed(msg.sender_id, "text", "incoming", msg_id=msg.msg_id):
                logger.info(f"Trusted-only mode: dropped untrusted text from {msg.sender_id}")
                return False

        return True

    # ── Discovery callbacks ──────────────────────────────────────────────────

    def _on_peer_joined(self, peer: PeerInfo):
        # Establish E2E session from DH keys announced in discovery
        if peer.public_key:
            self.crypto.establish_session(
                peer.peer_id,
                peer.public_key,
                peer.signing_key,
            )
        # Send our KEY_EXCHANGE message carrying both keys
        key_msg = make_key_exchange(
            self.crypto.public_key_b64,
            self.crypto.signing_key_b64,
        )
        self.msg_server.send_to_peer(peer.ip, peer.tcp_port, key_msg, peer.peer_id)
        self._emit("peer_joined", peer.to_dict())

    def _on_peer_left(self, peer: PeerInfo):
        self._emit("peer_left", peer.to_dict())

    # ── Message callbacks ────────────────────────────────────────────────────

    def _on_text_message(self, msg: Message):
        if not self._security_check(msg):
            return
        self._on_peer_activity(msg.sender_id)

        # Decrypt if encrypted
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

        # Verify signature
        signed = False
        if msg.signature:
            signed = self.crypto.verify_from(
                msg.sender_id,
                msg.canonical_bytes(),
                msg.signature,
            )
            if not signed:
                logger.warning(
                    f"Invalid signature on message {msg.msg_id} from {msg.sender_id}"
                )

        chat_msg = ChatMessage(
            msg_id=      msg.msg_id,
            sender_id=   msg.sender_id,
            sender_name= msg.sender_name,
            text=        text,
            timestamp=   msg.timestamp,
            is_me=       False,
            signed=      signed,
            encrypted=   was_enc,
            status=      "delivered",
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
            "peer_id":   msg.sender_id,
            "peer_name": msg.sender_name,
            "call_type": msg.payload.get("call_type", "audio"),
        })

    def _on_call_accept(self, msg: Message):
        """Remote peer accepted our call — start media engine on caller side."""
        peer = self.discovery.get_peer(msg.sender_id)
        if peer:
            self.media.start_call(peer.ip, peer.media_port)
        self._emit("call_accepted", {"peer_id": msg.sender_id})

    def _on_call_reject(self, msg: Message):
        """Remote peer rejected our call."""
        self.current_call_peer = None
        self.media.end_call()
        self._emit("call_rejected", {"peer_id": msg.sender_id})

    def _on_call_end(self, msg: Message):
        """Remote peer ended the call."""
        self.current_call_peer = None
        self.media.end_call()
        self._emit("call_ended", {"peer_id": msg.sender_id})

    def _on_typing(self, msg: Message):
        if not self._rate_limiter.is_allowed(msg.sender_id):
            return
        self._emit("typing", {"peer_id": msg.sender_id, "peer_name": msg.sender_name})

    def _on_seed_pair(self, msg: Message):
        """Peer notifies us they have completed seed-pairing on their side."""
        peer_id = msg.sender_id
        self.discovery.mark_trusted(peer_id)
        self._emit("seed_paired", {
            "peer_id":   peer_id,
            "peer_name": msg.sender_name,
        })
        self._record_security_event("seed_paired_confirmed", peer_id, {
            "peer_name": msg.sender_name,
        })
        logger.info(f"Seed-pair confirmation received from {peer_id}")

    # ── Mesh flooding / relay ────────────────────────────────────────────────

    def _on_mesh_relay(self, msg: Message):
        """
        Receive a relayed message:
          1. Security gate (rate-limit, dedup).
          2. Deliver the inner payload locally if we're the destination (or broadcast).
          3. Forward to all other peers if TTL > 0.
        """
        if not self._security_check(msg):
            return
        self._on_peer_activity(msg.sender_id)

        if msg.signature:
            relay_ok = self.crypto.verify_from(
                msg.sender_id,
                msg.canonical_bytes(),
                msg.signature,
            )
            if not relay_ok:
                logger.warning(
                    f"Dropped mesh relay with invalid signature: {msg.msg_id} from {msg.sender_id}"
                )
                self._record_security_event("dropped_invalid_relay_signature", msg.sender_id, {
                    "msg_id": msg.msg_id,
                })
                return

        inner_type = msg.payload.get("inner_type")
        inner_payload = msg.payload.get("inner_payload", {})

        if inner_payload.get("private") and not inner_payload.get("encrypted"):
            logger.warning(f"Dropped insecure private relay payload: {msg.msg_id}")
            self._record_security_event("dropped_insecure_private_relay", msg.sender_id, {
                "msg_id": msg.msg_id,
            })
            return

        # Deliver locally — use original sender from inner_payload if available
        if inner_type == MsgType.TEXT:
            origin_id = inner_payload.get("origin_id", msg.sender_id)
            origin_name = inner_payload.get("origin_name", msg.sender_name)
            if not self._is_trusted_allowed(origin_id, "text", "incoming", msg_id=msg.msg_id):
                return
            text = inner_payload.get("text", "")
            chat_msg = ChatMessage(
                msg_id=      msg.msg_id,
                sender_id=   origin_id,
                sender_name= origin_name,
                text=        text,
                timestamp=   msg.timestamp,
                is_me=       False,
                status=      "delivered",
            )
            is_new = self._upsert_chat_message(origin_id, chat_msg)
            if is_new:
                self._emit("message", chat_msg.to_dict())

        # Relay to other peers if TTL allows
        if msg.ttl > 1:
            self._flood_relay(msg)

    def _flood_relay(self, msg: Message):
        """
        Forward a relay message to all known peers except those already in relay_path.
        Decrements TTL by 1.
        """
        peers = self.discovery.get_peers()
        pending_outbox = storage.outbox_pending_count()
        with self._relay_pressure_lock:
            pressure = self._relay_pending
        if pending_outbox + pressure >= max(200, int(MESH_RELAY_BACKPRESSURE_MAX_PENDING)):
            self._record_security_event("relay_backpressure_drop", msg.sender_id, {
                "msg_id": msg.msg_id,
                "pending_outbox": pending_outbox,
                "relay_pending": pressure,
            })
            return
        already_visited = set(msg.relay_path)
        candidates = []

        for peer_dict in peers:
            pid = peer_dict["peer_id"]
            if pid in already_visited or pid == msg.sender_id:
                continue
            peer = self.discovery.get_peer(pid)
            if not peer:
                continue
            candidates.append((pid, peer))

        if not candidates:
            return

        # Adaptive fanout: less pressure -> larger fanout.
        cap_min = max(1, int(MESH_RELAY_FANOUT_MIN))
        cap_max = max(cap_min, int(MESH_RELAY_FANOUT_MAX))
        ratio = min(1.0, (pending_outbox + pressure) / float(max(1, int(MESH_RELAY_BACKPRESSURE_MAX_PENDING))))
        fanout = int(round(cap_max - (cap_max - cap_min) * ratio))
        fanout = max(cap_min, min(cap_max, fanout, len(candidates)))
        random.shuffle(candidates)
        selected = candidates[:fanout]

        for pid, peer in selected:

            relay_msg = Message(
                msg_type=    MsgType.MESH_RELAY,
                sender_id=   NODE_ID,
                sender_name= NODE_NAME,
                payload=     msg.payload,
                timestamp=   msg.timestamp,
                msg_id=      msg.msg_id,
                ttl=         msg.ttl - 1,
                relay_path=  msg.relay_path + [NODE_ID],
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

    def _send_mesh_text(self, text: str, origin_msg_id: str = ""):
        """
        Flood a text message to ALL peers via mesh relay (for broadcast/multi-hop).
        Used when direct delivery isn't enough.
        """
        import uuid as _uuid
        msg_id = origin_msg_id or f"{NODE_ID}-{_uuid.uuid4().hex[:8]}"
        self._seen_msgs.add(msg_id)  # mark as seen so we don't re-process our own relay

        peers = self.discovery.get_peers()
        for peer_dict in peers:
            pid  = peer_dict["peer_id"]
            peer = self.discovery.get_peer(pid)
            if not peer:
                continue
            relay_msg = Message(
                msg_type=    MsgType.MESH_RELAY,
                sender_id=   NODE_ID,
                sender_name= NODE_NAME,
                payload=     {"inner_type": MsgType.TEXT, "inner_payload": {
                    "text": text, "origin_id": NODE_ID, "origin_name": NODE_NAME,
                }},
                msg_id=      msg_id,
                ttl=         MESH_TTL_DEFAULT,
                relay_path=  [NODE_ID],
            )
            if not relay_msg.msg_id:
                relay_msg.msg_id = f"{NODE_ID}-{time.time_ns()}"
            relay_msg.signature = self.crypto.sign(relay_msg.canonical_bytes())
            self.msg_server.send_to_peer(peer.ip, peer.tcp_port, relay_msg, pid)

    def _media_stats_loop(self):
        while self._running:
            try:
                stats = self.media.get_stats()
                self._emit("media_stats", stats)
                self._emit("network_diagnostics", self.get_network_diagnostics())
            except Exception:
                pass
            time.sleep(1.0)

    # ── WebRTC signaling relay ────────────────────────────────────────────────

    def _on_webrtc_signal(self, msg: Message):
        event_map = {
            MsgType.WEBRTC_OFFER:  "webrtc_offer",
            MsgType.WEBRTC_ANSWER: "webrtc_answer",
            MsgType.WEBRTC_ICE:    "webrtc_ice",
        }
        event = event_map.get(msg.msg_type)
        if event:
            self._emit(event, {
                "peer_id":   msg.sender_id,
                "peer_name": msg.sender_name,
                **msg.payload,
            })

    def send_webrtc_signal(self, peer_id: str, signal_type: str, payload: dict):
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return False
        type_map = {
            "offer":  MsgType.WEBRTC_OFFER,
            "answer": MsgType.WEBRTC_ANSWER,
            "ice":    MsgType.WEBRTC_ICE,
        }
        msg_type = type_map.get(signal_type)
        if not msg_type:
            return False
        msg = Message(
            msg_type=    msg_type,
            sender_id=   NODE_ID,
            sender_name= NODE_NAME,
            payload=     payload,
        )
        return self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)

    # ── File callbacks ────────────────────────────────────────────────────────

    def _on_file_progress(self, transfer):
        self._emit("file_progress", transfer.to_dict())

    def _on_file_complete(self, transfer):
        self._emit("file_complete", transfer.to_dict())
        if transfer.direction == "recv":
            if not self._is_trusted_allowed(transfer.peer_id, "file", "incoming", msg_id=f"file-{transfer.file_id}"):
                return
            saved = transfer.saved_as or transfer.filename
            chat_msg = ChatMessage(
                msg_id=      f"file-{transfer.file_id}",
                sender_id=   transfer.peer_id,
                sender_name= transfer.peer_name or "Peer",
                text=        transfer.filename,
                timestamp=   time.time(),
                is_me=       False,
                msg_type=    "file",
                file_url=    f"/downloads/{saved}",
                file_name=   transfer.filename,
                file_size=   transfer.filesize,
            )
            is_new = self._upsert_chat_message(transfer.peer_id, chat_msg)
            if is_new:
                self._emit("message", chat_msg.to_dict())
        elif transfer.direction == "send":
            chat_msg = ChatMessage(
                msg_id=      f"file-{transfer.file_id}",
                sender_id=   NODE_ID,
                sender_name= NODE_NAME,
                text=        transfer.filename,
                timestamp=   time.time(),
                is_me=       True,
                msg_type=    "file",
                file_name=   transfer.filename,
                file_size=   transfer.filesize,
            )
            is_new = self._upsert_chat_message(transfer.peer_id, chat_msg)
            if is_new:
                self._emit("message", chat_msg.to_dict())

    # ── Public API ────────────────────────────────────────────────────────────

    def get_info(self) -> dict:
        return {
            "node_id":     NODE_ID,
            "node_name":   NODE_NAME,
            "local_ip":    LOCAL_IP,
            "tcp_port":    TCP_PORT,
            "media_port":  MEDIA_PORT,
            "file_port":   FILE_PORT,
            "downloads_dir": DOWNLOADS_DIR,
            "encryption":  self.crypto.keypair.private_key is not None,
            "signing_key": self.crypto.signing_key_b64,
            "trusted_only_private_chats": TRUSTED_ONLY_PRIVATE_CHATS,
            "trusted_only_text": bool(TRUSTED_ONLY_TEXT or TRUSTED_ONLY_PRIVATE_CHATS),
            "trusted_only_file": TRUSTED_ONLY_FILE,
            "trusted_only_call": TRUSTED_ONLY_CALL,
        }

    def get_peers(self) -> list:
        return self.discovery.get_peers()

    def get_chat(self, peer_id: str) -> list:
        with self._chat_lock:
            return [m.to_dict() for m in self.chats.get(peer_id, [])]

    def send_text(self, peer_id: str, text: str) -> Optional[dict]:
        """
        Send an encrypted + signed text message to a peer.
        Falls back to plaintext if no E2E session is established.
        """
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return None

        self._on_peer_activity(peer_id)
        if not self._is_trusted_allowed(peer_id, "text", "outgoing"):
            logger.info(f"Trusted-only mode: blocked outgoing text to untrusted peer {peer_id}")
            return None

        # Build message with E2E encryption
        encrypted   = False
        ciphertext_b64 = ""
        display_text = text
        if self.crypto.has_session(peer_id):
            try:
                ct = self.crypto.encrypt_for(peer_id, text.encode("utf-8"))
                ciphertext_b64 = base64.b64encode(ct).decode()
                encrypted = True
            except Exception as e:
                logger.warning(f"Encryption failed for {peer_id}: {e}")

        msg = make_text_message(text, encrypted=encrypted,
                                ciphertext_b64=ciphertext_b64)

        # Ensure msg_id is set BEFORE signing (canonical_bytes includes msg_id)
        if not msg.msg_id:
            msg.msg_id = f"{msg.sender_id}-{time.time_ns()}"

        # Sign the message
        canonical = msg.canonical_bytes()
        msg.signature = self.crypto.sign(canonical)

        success = self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)
        if success:
            status = self.msg_server.get_delivery_status(msg.msg_id)
            if status not in ("sent", "delivered", "failed"):
                status = "sent"
            chat_msg = ChatMessage(
                msg_id=      msg.msg_id or f"{NODE_ID}-{time.time_ns()}",
                sender_id=   NODE_ID,
                sender_name= NODE_NAME,
                text=        display_text,
                timestamp=   time.time(),
                is_me=       True,
                signed=      True,
                encrypted=   encrypted,
                status=      status,
            )
            with self._chat_lock:
                self.chats.setdefault(peer_id, []).append(chat_msg)
            persist_chat_entry({
                "peer_id": peer_id,
                "msg_id": chat_msg.msg_id,
                "sender_id": NODE_ID,
                "sender_name": NODE_NAME,
                "text": display_text,
                "timestamp": chat_msg.timestamp,
                "is_me": True,
                "msg_type": "text",
                "signed": True,
                "encrypted": encrypted,
                "status": status,
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

    def generate_pairing_seed(self) -> str:
        """Generate a fresh 6-char seed to share with a peer out-of-band."""
        return CryptoManager.generate_seed()

    def pair_with_seed(self, peer_id: str, seed: str) -> bool:
        """
        Activate seed-pairing with a peer using a shared 6-char seed.
        Both nodes must call this with the same seed.
        Returns True on success.
        """
        if not seed or len(seed) != 6:
            logger.warning("Seed must be exactly 6 characters")
            return False

        self.crypto.establish_seed_session(peer_id, seed)
        self.discovery.mark_trusted(peer_id)
        self._record_security_event("seed_paired_local", peer_id, {})

        # Notify peer that we've completed pairing on our side
        peer = self.discovery.get_peer(peer_id)
        if peer:
            notify = make_seed_pair_message(peer_id)
            self.msg_server.send_to_peer(peer.ip, peer.tcp_port, notify, peer_id)

        logger.info(f"Seed-pairing complete with {peer_id}")
        return True

    def is_peer_trusted(self, peer_id: str) -> bool:
        return self.crypto.is_trusted(peer_id)

    # ── Rate-limit / Blacklist API ────────────────────────────────────────────

    def blacklist_peer(self, peer_id: str):
        """Permanently blacklist a peer (backend-level, messages are dropped)."""
        self._rate_limiter.blacklist_add(peer_id)
        self._record_security_event("blacklist_added", peer_id, {})

    def unblacklist_peer(self, peer_id: str):
        self._rate_limiter.blacklist_remove(peer_id)
        self._record_security_event("blacklist_removed", peer_id, {})

    def get_blacklist(self) -> list:
        return self._rate_limiter.get_blacklist()

    def get_banned_peers(self) -> dict:
        """Returns peers under temporary rate-limit ban and remaining seconds."""
        return self._rate_limiter.get_banned()

    def get_security_events(self, limit: int = 200) -> list:
        lim = max(1, min(2000, int(limit or 200)))
        with self._security_lock:
            events = list(self._security_events)
        return events[-lim:]

    def get_security_snapshot(self) -> dict:
        with self._metrics_lock:
            m = dict(self._metrics)
        session_snapshot = self.crypto.get_session_snapshot()
        return {
            "trusted_only_private_chats": TRUSTED_ONLY_PRIVATE_CHATS,
            "trusted_only_text": bool(TRUSTED_ONLY_TEXT or TRUSTED_ONLY_PRIVATE_CHATS),
            "trusted_only_file": TRUSTED_ONLY_FILE,
            "trusted_only_call": TRUSTED_ONLY_CALL,
            "blacklist": self.get_blacklist(),
            "banned": self.get_banned_peers(),
            "session_keys": session_snapshot,
            "session_keys_active": sum(1 for _, x in session_snapshot.items() if x.get("active")),
            "session_rotations_total": m.get("session_rotations_total", 0),
            "session_expired_total": m.get("session_expired_total", 0),
            "events": self.get_security_events(100),
        }

    def is_ready(self) -> bool:
        if self.discovery is None or self.msg_server is None or self.file_mgr is None or self.media is None:
            return False
        return bool(self._running and self.msg_server._running and self.file_mgr._running and self.media._running)

    def get_health_snapshot(self) -> dict:
        return {
            "status": "ok",
            "node_id": NODE_ID,
            "uptime_seconds": max(0.0, time.time() - self._started_at),
            "started_at": self._started_at,
            "active_peers": len(self.discovery.get_peers()),
        }

    def get_metrics_snapshot(self) -> dict:
        with self._metrics_lock:
            base = dict(self._metrics)
        counters = storage.get_counters("metrics.")
        peers = self.discovery.get_peers()
        with self._relay_pressure_lock:
            relay_pending = self._relay_pending

        latency_sum = float(counters.get("metrics.delivery_latency_sum_seconds", 0.0))
        latency_count = float(counters.get("metrics.delivery_latency_count", 0.0))

        snap = {
            "active_peers": len(peers),
            "relay_pending": int(relay_pending),
            "relay_drops_total": int(base.get("relay_backpressure_drops_total", 0)),
            "outbox_pending": int(storage.outbox_pending_count()),
            "session_rotations_total": int(base.get("session_rotations_total", 0)),
            "session_expired_total": int(base.get("session_expired_total", 0)),
            "security_events_total": int(base.get("security_events_total", 0)),
            "trusted_policy_incoming_text_drops_total": int(base.get("policy_drops_incoming_text_total", 0)),
            "trusted_policy_incoming_file_drops_total": int(base.get("policy_drops_incoming_file_total", 0)),
            "trusted_policy_incoming_call_drops_total": int(base.get("policy_drops_incoming_call_total", 0)),
            "trusted_policy_outgoing_text_blocks_total": int(base.get("policy_blocks_outgoing_text_total", 0)),
            "trusted_policy_outgoing_file_blocks_total": int(base.get("policy_blocks_outgoing_file_total", 0)),
            "trusted_policy_outgoing_call_blocks_total": int(base.get("policy_blocks_outgoing_call_total", 0)),
            "delivery_retry_total": float(counters.get("metrics.delivery_retry_total", 0.0)),
            "delivery_fail_total": float(counters.get("metrics.delivery_fail_total", 0.0)),
            "delivery_latency_sum_seconds": latency_sum,
            "delivery_latency_count": latency_count,
            "delivery_latency_avg_seconds": (latency_sum / latency_count) if latency_count > 0 else 0.0,
            "file_resume_total": float(counters.get("metrics.file_resume_total", 0.0)),
        }
        return snap

    def get_network_diagnostics(self) -> dict:
        counters = storage.get_counters("metrics.")
        metrics = self.get_metrics_snapshot()
        media = self.media.get_stats()
        sends = self.file_mgr.get_send_diagnostics()
        retry_total = float(counters.get("metrics.delivery_retry_total", 0.0))
        fail_total = float(counters.get("metrics.delivery_fail_total", 0.0))
        latency_count = float(counters.get("metrics.delivery_latency_count", 0.0))
        fail_reason = "none"
        if fail_total > 0:
            fail_reason = "delivery_failed_or_unreachable"
        elif retry_total > 0:
            fail_reason = "retries_in_progress"

        return {
            "delivery": {
                "retry_total": retry_total,
                "fail_total": fail_total,
                "latency_count": latency_count,
                "failed_reason": fail_reason,
            },
            "queue": {
                "outbox_backlog": int(metrics.get("outbox_pending", 0)),
                "relay_pending": int(metrics.get("relay_pending", 0)),
                "relay_drops_total": int(metrics.get("relay_drops_total", 0)),
            },
            "file_transfer": sends,
            "media": {
                "uplink_bitrate_kbps": float(media.get("uplink_bitrate_kbps", 0.0)),
                "downlink_bitrate_kbps": float(media.get("downlink_bitrate_kbps", 0.0)),
                "uplink_jitter_p95_ms": float(media.get("uplink_jitter_p95_ms", 0.0)),
                "downlink_jitter_p95_ms": float(media.get("downlink_jitter_p95_ms", 0.0)),
                "downlink_loss_percent": float(media.get("loss_percent", 0.0)),
            },
            "active_peers": int(metrics.get("active_peers", 0)),
            "ts": time.time(),
        }

    # ── Call control ─────────────────────────────────────────────────────────

    def start_call(self, peer_id: str, call_type: str = "audio") -> bool:
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return False
        self._on_peer_activity(peer_id)
        if not self._is_trusted_allowed(peer_id, "call", "outgoing"):
            return False
        self.current_call_peer = peer_id
        msg     = make_call_invite(call_type)
        success = self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)
        if success:
            self._emit("call_outgoing", {
                "peer_id":   peer_id,
                "peer_name": peer.name,
                "call_type": call_type,
            })
        return success

    def accept_call(self, peer_id: str):
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return
        msg = Message(msg_type=MsgType.CALL_ACCEPT, sender_id=NODE_ID,
                      sender_name=NODE_NAME, payload={})
        self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)
        # Start media engine on callee side
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
        self._emit("call_rejected", {"peer_id": peer_id})

    def end_call(self):
        call_peer = self.current_call_peer
        if call_peer:
            peer = self.discovery.get_peer(call_peer)
            if peer:
                msg = Message(msg_type=MsgType.CALL_END, sender_id=NODE_ID,
                              sender_name=NODE_NAME, payload={})
                self.msg_server.send_to_peer(
                    peer.ip, peer.tcp_port, msg, call_peer
                )
        self.current_call_peer = None
        self.media.end_call()
        if call_peer:
            self._emit("call_ended", {"peer_id": call_peer})

    def get_transfers(self) -> list:
        return self.file_mgr.get_transfers()

    def get_media_stats(self) -> dict:
        return self.media.get_stats()
