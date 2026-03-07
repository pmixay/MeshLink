"""
MeshLink — Storage Service (microservice module)

Re-exports per-user SQLite storage from core/storage.py.
Each MeshLink instance stores its data under:

    data/<NODE_NAME>_<NODE_ID[:8]>/meshlink.db

This allows multiple instances to run from the same directory
(e.g. Alice and Bob on the same development machine) without
conflicting database files.
"""

from core.storage import (  # noqa: F401
    db_path,
    persist_chat_entry,
    update_message_status,
    set_delivery_status,
    get_delivery_status,
    load_persisted_chats,
    enqueue_outbox,
    mark_outbox_attempt,
    mark_outbox_delivered,
    load_due_outbox,
    outbox_pending_count,
    enforce_db_limits,
    incr_counter,
    get_counters,
)

__all__ = [
    "db_path",
    "persist_chat_entry",
    "update_message_status",
    "set_delivery_status",
    "get_delivery_status",
    "load_persisted_chats",
    "enqueue_outbox",
    "mark_outbox_attempt",
    "mark_outbox_delivered",
    "load_due_outbox",
    "outbox_pending_count",
    "enforce_db_limits",
    "incr_counter",
    "get_counters",
]
