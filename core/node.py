"""
MeshLink — Node Orchestrator (v2)
WebRTC signaling relay + dedicated file transfer integration.
"""

import os
import time
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable

from .config import NODE_ID, NODE_NAME, LOCAL_IP, TCP_PORT, MEDIA_PORT, FILE_PORT, DOWNLOADS_DIR
from .crypto import CryptoManager
from .discovery import DiscoveryService, PeerInfo
from .messaging import (
    MessageServer, Message, MsgType,
    make_text_message, make_call_invite, make_key_exchange,
)
from .file_transfer import FileTransferManager
from .media import MediaEngine, CallState

logger = logging.getLogger("meshlink.node")


@dataclass
class ChatMessage:
    msg_id: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: float
    is_me: bool = False
    msg_type: str = "text"   # text / file / system
    file_url: str = ""       # download URL for file messages
    file_name: str = ""      # original filename
    file_size: int = 0       # file size in bytes

    def to_dict(self):
        d = {
            "msg_id": self.msg_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "text": self.text,
            "timestamp": self.timestamp,
            "is_me": self.is_me,
            "msg_type": self.msg_type,
        }
        if self.msg_type == "file":
            d["file_url"] = self.file_url
            d["file_name"] = self.file_name
            d["file_size"] = self.file_size
        return d


class MeshNode:

    def __init__(self):
        self.crypto = CryptoManager()
        self.discovery = DiscoveryService(public_key_b64=self.crypto.public_key_b64)
        self.msg_server = MessageServer()
        self.file_mgr = FileTransferManager()
        self.media = MediaEngine()

        self.chats: Dict[str, List[ChatMessage]] = {}
        self._chat_lock = threading.Lock()
        self.current_call_peer: Optional[str] = None

        self._event_handlers: Dict[str, List[Callable]] = {}
        self._setup_handlers()

    def _setup_handlers(self):
        self.discovery.on_peer_joined = self._on_peer_joined
        self.discovery.on_peer_left = self._on_peer_left

        self.msg_server.on(MsgType.TEXT, self._on_text_message)
        self.msg_server.on(MsgType.KEY_EXCHANGE, self._on_key_exchange)
        self.msg_server.on(MsgType.CALL_INVITE, self._on_call_invite)
        self.msg_server.on(MsgType.CALL_ACCEPT, self._on_call_accept)
        self.msg_server.on(MsgType.CALL_REJECT, self._on_call_reject)
        self.msg_server.on(MsgType.CALL_END, self._on_call_end)
        self.msg_server.on(MsgType.TYPING, self._on_typing)

        # WebRTC signaling relay
        self.msg_server.on(MsgType.WEBRTC_OFFER, self._on_webrtc_signal)
        self.msg_server.on(MsgType.WEBRTC_ANSWER, self._on_webrtc_signal)
        self.msg_server.on(MsgType.WEBRTC_ICE, self._on_webrtc_signal)

        # File transfer events
        self.file_mgr.on_progress = self._on_file_progress
        self.file_mgr.on_complete = self._on_file_complete

    # ── Event system ────────────────────────────────────────

    def on(self, event: str, handler: Callable):
        self._event_handlers.setdefault(event, []).append(handler)

    def _emit(self, event: str, data=None):
        for h in self._event_handlers.get(event, []):
            try:
                h(data)
            except Exception as e:
                logger.error(f"Event handler error ({event}): {e}")

    # ── Lifecycle ───────────────────────────────────────────

    def start(self):
        logger.info(f"Starting MeshLink: {NODE_NAME} ({NODE_ID}) @ {LOCAL_IP}")
        self.discovery.start()
        self.msg_server.start()
        self.file_mgr.start()
        self.media.start()
        logger.info("All subsystems started.")

    def stop(self):
        self.media.stop()
        self.file_mgr.stop()
        self.msg_server.stop()
        self.discovery.stop()
        logger.info("MeshLink stopped.")

    # ── Discovery callbacks ─────────────────────────────────

    def _on_peer_joined(self, peer: PeerInfo):
        if peer.public_key:
            self.crypto.establish_session(peer.peer_id, peer.public_key)
        key_msg = make_key_exchange(self.crypto.public_key_b64)
        self.msg_server.send_to_peer(peer.ip, peer.tcp_port, key_msg, peer.peer_id)
        self._emit("peer_joined", peer.to_dict())

    def _on_peer_left(self, peer: PeerInfo):
        self._emit("peer_left", peer.to_dict())

    # ── Message callbacks ───────────────────────────────────

    def _on_text_message(self, msg: Message):
        chat_msg = ChatMessage(
            msg_id=msg.msg_id,
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            text=msg.payload.get("text", ""),
            timestamp=msg.timestamp,
            is_me=False,
        )
        with self._chat_lock:
            self.chats.setdefault(msg.sender_id, []).append(chat_msg)
        self._emit("message", chat_msg.to_dict())

    def _on_key_exchange(self, msg: Message):
        pub_key = msg.payload.get("public_key", "")
        if pub_key:
            self.crypto.establish_session(msg.sender_id, pub_key)

    def _on_call_invite(self, msg: Message):
        self.current_call_peer = msg.sender_id
        self._emit("call_incoming", {
            "peer_id": msg.sender_id,
            "peer_name": msg.sender_name,
            "call_type": msg.payload.get("call_type", "audio"),
        })

    def _on_call_accept(self, msg: Message):
        self._emit("call_accepted", {"peer_id": msg.sender_id})

    def _on_call_reject(self, msg: Message):
        self.current_call_peer = None
        self._emit("call_rejected", {"peer_id": msg.sender_id})

    def _on_call_end(self, msg: Message):
        self.current_call_peer = None
        self._emit("call_ended", {"peer_id": msg.sender_id})

    def _on_typing(self, msg: Message):
        self._emit("typing", {"peer_id": msg.sender_id, "peer_name": msg.sender_name})

    # ── WebRTC signaling relay (TCP peer → SocketIO browser) ─

    def _on_webrtc_signal(self, msg: Message):
        """Relay WebRTC signaling from TCP peer to local browser via events."""
        event_map = {
            MsgType.WEBRTC_OFFER: "webrtc_offer",
            MsgType.WEBRTC_ANSWER: "webrtc_answer",
            MsgType.WEBRTC_ICE: "webrtc_ice",
        }
        event = event_map.get(msg.msg_type)
        if event:
            self._emit(event, {
                "peer_id": msg.sender_id,
                "peer_name": msg.sender_name,
                **msg.payload,
            })

    def send_webrtc_signal(self, peer_id: str, signal_type: str, payload: dict):
        """Send WebRTC signaling data to a peer via TCP."""
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return False

        type_map = {
            "offer": MsgType.WEBRTC_OFFER,
            "answer": MsgType.WEBRTC_ANSWER,
            "ice": MsgType.WEBRTC_ICE,
        }
        msg_type = type_map.get(signal_type)
        if not msg_type:
            return False

        msg = Message(
            msg_type=msg_type,
            sender_id=NODE_ID,
            sender_name=NODE_NAME,
            payload=payload,
        )
        return self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)

    # ── File callbacks ──────────────────────────────────────

    def _on_file_progress(self, transfer):
        self._emit("file_progress", transfer.to_dict())

    def _on_file_complete(self, transfer):
        self._emit("file_complete", transfer.to_dict())
        # Add file message to chat
        if transfer.direction == "recv":
            saved = transfer.saved_as or transfer.filename
            chat_msg = ChatMessage(
                msg_id=f"file-{transfer.file_id}",
                sender_id=transfer.peer_id,
                sender_name=transfer.peer_name or "Peer",
                text=transfer.filename,
                timestamp=time.time(),
                is_me=False,
                msg_type="file",
                file_url=f"/downloads/{saved}",
                file_name=transfer.filename,
                file_size=transfer.filesize,
            )
            with self._chat_lock:
                self.chats.setdefault(transfer.peer_id, []).append(chat_msg)
            self._emit("message", chat_msg.to_dict())
        elif transfer.direction == "send":
            chat_msg = ChatMessage(
                msg_id=f"file-{transfer.file_id}",
                sender_id=NODE_ID,
                sender_name=NODE_NAME,
                text=transfer.filename,
                timestamp=time.time(),
                is_me=True,
                msg_type="file",
                file_name=transfer.filename,
                file_size=transfer.filesize,
            )
            with self._chat_lock:
                self.chats.setdefault(transfer.peer_id, []).append(chat_msg)
            self._emit("message", chat_msg.to_dict())

    # ── Public API ──────────────────────────────────────────

    def get_info(self) -> dict:
        return {
            "node_id": NODE_ID,
            "node_name": NODE_NAME,
            "local_ip": LOCAL_IP,
            "tcp_port": TCP_PORT,
            "media_port": MEDIA_PORT,
            "file_port": FILE_PORT,
            "downloads_dir": DOWNLOADS_DIR,
            "encryption": self.crypto.keypair.private_key is not None,
        }

    def get_peers(self) -> list:
        return self.discovery.get_peers()

    def get_chat(self, peer_id: str) -> list:
        with self._chat_lock:
            return [m.to_dict() for m in self.chats.get(peer_id, [])]

    def send_text(self, peer_id: str, text: str) -> Optional[dict]:
        peer = self.discovery.get_peer(peer_id)
        if not peer:
            return None
        msg = make_text_message(text)
        success = self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg, peer_id)
        if success:
            chat_msg = ChatMessage(
                msg_id=msg.msg_id or f"{NODE_ID}-{time.time_ns()}",
                sender_id=NODE_ID,
                sender_name=NODE_NAME,
                text=text,
                timestamp=time.time(),
                is_me=True,
            )
            with self._chat_lock:
                self.chats.setdefault(peer_id, []).append(chat_msg)
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
        return self.file_mgr.send_file(filepath, peer.ip, peer.file_port, peer_id)

    def start_call(self, peer_id: str, call_type: str = "audio") -> bool:
        peer = self.discovery.get_peer(peer_id)
        if not peer:
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
        if self.current_call_peer:
            peer = self.discovery.get_peer(self.current_call_peer)
            if peer:
                msg = Message(msg_type=MsgType.CALL_END, sender_id=NODE_ID,
                              sender_name=NODE_NAME, payload={})
                self.msg_server.send_to_peer(peer.ip, peer.tcp_port, msg,
                                             self.current_call_peer)
        self.current_call_peer = None

    def get_transfers(self) -> list:
        return self.file_mgr.get_transfers()

    def get_media_stats(self) -> dict:
        return self.media.get_stats()
