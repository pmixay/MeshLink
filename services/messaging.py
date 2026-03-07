"""
MeshLink — Messaging Service (microservice module)

Re-exports the TCP messaging layer from core/messaging.py.
Provides reliable, length-prefixed JSON messaging over TCP with:

  - Delivery ACK and retry queue
  - Per-peer send-slot concurrency control
  - Outbox persistence (SQLite) for restart-safe delivery
  - Mesh RELAY message routing
  - WebRTC signaling relay (OFFER / ANSWER / ICE)
  - Seed-pairing handshake
"""

from core.messaging import (  # noqa: F401
    MessageServer,
    Message,
    MsgType,
    make_text_message,
    make_call_invite,
    make_key_exchange,
    make_seed_pair_message,
    persist_chat_entry,
)

__all__ = [
    "MessageServer",
    "Message",
    "MsgType",
    "make_text_message",
    "make_call_invite",
    "make_key_exchange",
    "make_seed_pair_message",
    "persist_chat_entry",
]
