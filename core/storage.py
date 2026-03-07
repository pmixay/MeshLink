"""
MeshLink — Local persistent storage (SQLite)

Per-user database: each NODE_NAME gets its own database file
so multiple users can run from the same directory.
"""

import json
import os
import re
import sqlite3
import threading
import time
from typing import Dict, List, Optional


_DB_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None
_CONN_LOCK = threading.Lock()


def _safe_name(name: str) -> str:
    """Sanitize node name for use in filesystem path."""
    s = re.sub(r'[^\w\-.]', '_', name.strip())
    return s[:64] if s else "default"


def _base_dir() -> str:
    from .config import NODE_NAME
    base = os.environ.get("MESHLINK_DATA_DIR", "").strip()
    if not base:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    # Per-user subdirectory
    user_dir = os.path.join(base, _safe_name(NODE_NAME))
    os.makedirs(user_dir, exist_ok=True)
    return user_dir


def db_path() -> str:
    return os.path.join(_base_dir(), "meshlink.db")


def _connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    with _CONN_LOCK:
        if _CONN is None:
            conn = sqlite3.connect(db_path(), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.execute("PRAGMA foreign_keys=ON;")
            _init_schema(conn)
            _CONN = conn
    return _CONN


def _init_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chat_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id TEXT NOT NULL,
            msg_id TEXT NOT NULL,
            sender_id TEXT,
            sender_name TEXT,
            text TEXT,
            timestamp REAL NOT NULL,
            is_me INTEGER NOT NULL DEFAULT 0,
            msg_type TEXT NOT NULL DEFAULT 'text',
            file_url TEXT,
            file_name TEXT,
            file_size INTEGER,
            signed INTEGER,
            encrypted INTEGER,
            status TEXT,
            UNIQUE(peer_id, msg_id)
        );
        CREATE INDEX IF NOT EXISTS idx_chat_peer_ts ON chat_entries(peer_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_chat_msg_id ON chat_entries(msg_id);
        CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_entries(timestamp);

        CREATE TABLE IF NOT EXISTS outbox_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id TEXT UNIQUE NOT NULL,
            peer_id TEXT NOT NULL,
            ip TEXT NOT NULL,
            port INTEGER NOT NULL,
            msg_blob TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_next_retry ON outbox_queue(next_retry_at, status);
        CREATE INDEX IF NOT EXISTS idx_outbox_peer ON outbox_queue(peer_id, status);
        CREATE INDEX IF NOT EXISTS idx_outbox_msg ON outbox_queue(msg_id);

        CREATE TABLE IF NOT EXISTS inbox_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id TEXT UNIQUE NOT NULL,
            peer_id TEXT NOT NULL,
            msg_blob TEXT NOT NULL,
            received_at REAL NOT NULL,
            processed_at REAL,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS idx_inbox_status_received ON inbox_queue(status, received_at);
        CREATE INDEX IF NOT EXISTS idx_inbox_peer ON inbox_queue(peer_id, status);
        CREATE INDEX IF NOT EXISTS idx_inbox_msg ON inbox_queue(msg_id);

        CREATE TABLE IF NOT EXISTS delivery_state (
            msg_id TEXT PRIMARY KEY,
            peer_id TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_peer_status ON delivery_state(peer_id, status);
        CREATE INDEX IF NOT EXISTS idx_delivery_updated ON delivery_state(updated_at);

        CREATE TABLE IF NOT EXISTS kv_counters (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()


def enforce_db_limits(max_db_mb: int = 128, max_chat_rows: int = 200000):
    conn = _connect()
    with _DB_LOCK:
        cur = conn.execute("SELECT COUNT(*) AS c FROM chat_entries")
        count = int(cur.fetchone()["c"])
        if count > max_chat_rows:
            drop = count - max_chat_rows
            conn.execute(
                "DELETE FROM chat_entries WHERE id IN (SELECT id FROM chat_entries ORDER BY id ASC LIMIT ?)",
                (drop,),
            )
            conn.commit()

        for table, max_rows in (("outbox_queue", 100000), ("inbox_queue", 200000), ("delivery_state", 500000)):
            cur = conn.execute(f"SELECT COUNT(*) AS c FROM {table}")
            t_count = int(cur.fetchone()["c"])
            if t_count > max_rows:
                drop = t_count - max_rows
                if table == "delivery_state":
                    conn.execute(
                        "DELETE FROM delivery_state WHERE msg_id IN (SELECT msg_id FROM delivery_state ORDER BY updated_at ASC LIMIT ?)",
                        (drop,),
                    )
                elif table == "outbox_queue":
                    conn.execute(
                        "DELETE FROM outbox_queue WHERE id IN (SELECT id FROM outbox_queue ORDER BY updated_at ASC LIMIT ?)",
                        (drop,),
                    )
                else:
                    conn.execute(
                        "DELETE FROM inbox_queue WHERE id IN (SELECT id FROM inbox_queue ORDER BY received_at ASC LIMIT ?)",
                        (drop,),
                    )
                conn.commit()

        try:
            p = db_path()
            if os.path.exists(p):
                max_bytes = max(8, int(max_db_mb)) * 1024 * 1024
                if os.path.getsize(p) > max_bytes:
                    conn.execute(
                        "DELETE FROM chat_entries WHERE id IN (SELECT id FROM chat_entries ORDER BY id ASC LIMIT (SELECT CAST(COUNT(*)*0.1 AS INT) FROM chat_entries))"
                    )
                    conn.execute(
                        "DELETE FROM inbox_queue WHERE id IN (SELECT id FROM inbox_queue ORDER BY id ASC LIMIT (SELECT CAST(COUNT(*)*0.1 AS INT) FROM inbox_queue))"
                    )
                    conn.execute(
                        "DELETE FROM delivery_state WHERE msg_id IN (SELECT msg_id FROM delivery_state ORDER BY updated_at ASC LIMIT (SELECT CAST(COUNT(*)*0.1 AS INT) FROM delivery_state))"
                    )
                    conn.commit()
                    conn.execute("VACUUM")
        except Exception:
            pass


def persist_chat_entry(entry: dict):
    e = dict(entry)
    e.setdefault("timestamp", time.time())
    peer_id = e.get("peer_id", "")
    msg_id = e.get("msg_id", "")
    if not peer_id or not msg_id:
        return

    conn = _connect()
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO chat_entries(
              peer_id,msg_id,sender_id,sender_name,text,timestamp,is_me,msg_type,
              file_url,file_name,file_size,signed,encrypted,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(peer_id,msg_id) DO UPDATE SET
              sender_id=excluded.sender_id,
              sender_name=excluded.sender_name,
              text=excluded.text,
              timestamp=excluded.timestamp,
              is_me=excluded.is_me,
              msg_type=excluded.msg_type,
              file_url=excluded.file_url,
              file_name=excluded.file_name,
              file_size=excluded.file_size,
              signed=excluded.signed,
              encrypted=excluded.encrypted,
              status=CASE
                WHEN excluded.status IS NULL OR excluded.status='' THEN chat_entries.status
                ELSE excluded.status
              END
            """,
            (
                peer_id,
                msg_id,
                e.get("sender_id", ""),
                e.get("sender_name", ""),
                e.get("text", ""),
                float(e.get("timestamp", time.time())),
                1 if e.get("is_me", False) else 0,
                e.get("msg_type", "text"),
                e.get("file_url", ""),
                e.get("file_name", ""),
                int(e.get("file_size", 0) or 0),
                1 if e.get("signed", False) else 0,
                1 if e.get("encrypted", False) else 0,
                e.get("status", ""),
            ),
        )
        conn.commit()


def update_message_status(peer_id: str, msg_id: str, status: str):
    if not peer_id or not msg_id:
        return
    conn = _connect()
    with _DB_LOCK:
        conn.execute(
            "UPDATE chat_entries SET status=? WHERE peer_id=? AND msg_id=?",
            (status, peer_id, msg_id),
        )
        conn.execute(
            "INSERT INTO delivery_state(msg_id,peer_id,status,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(msg_id) DO UPDATE SET peer_id=excluded.peer_id,status=excluded.status,updated_at=excluded.updated_at",
            (msg_id, peer_id, status, time.time()),
        )
        conn.commit()


def set_delivery_status(peer_id: str, msg_id: str, status: str):
    if not peer_id or not msg_id:
        return
    conn = _connect()
    now = time.time()
    with _DB_LOCK:
        conn.execute(
            "INSERT INTO delivery_state(msg_id,peer_id,status,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(msg_id) DO UPDATE SET peer_id=excluded.peer_id,status=excluded.status,updated_at=excluded.updated_at",
            (msg_id, peer_id, status, now),
        )
        conn.commit()


def get_delivery_status(msg_id: str) -> str:
    if not msg_id:
        return "unknown"
    conn = _connect()
    with _DB_LOCK:
        row = conn.execute("SELECT status FROM delivery_state WHERE msg_id=?", (msg_id,)).fetchone()
    return str(row["status"]) if row else "unknown"


def load_persisted_chats() -> Dict[str, List[dict]]:
    conn = _connect()
    by_peer: Dict[str, List[dict]] = {}
    with _DB_LOCK:
        rows = conn.execute(
            "SELECT * FROM chat_entries ORDER BY timestamp ASC, id ASC"
        ).fetchall()
    for r in rows:
        d = {
            "peer_id": r["peer_id"],
            "msg_id": r["msg_id"],
            "sender_id": r["sender_id"] or "",
            "sender_name": r["sender_name"] or "",
            "text": r["text"] or "",
            "timestamp": float(r["timestamp"] or 0.0),
            "is_me": bool(r["is_me"]),
            "msg_type": r["msg_type"] or "text",
            "file_url": r["file_url"] or "",
            "file_name": r["file_name"] or "",
            "file_size": int(r["file_size"] or 0),
            "signed": bool(r["signed"] or 0),
            "encrypted": bool(r["encrypted"] or 0),
            "status": r["status"] or "",
        }
        by_peer.setdefault(d["peer_id"], []).append(d)
    return by_peer


def enqueue_outbox(msg_id: str, peer_id: str, ip: str, port: int, msg_dict: dict, next_retry_at: float):
    conn = _connect()
    now = time.time()
    blob = json.dumps(msg_dict, ensure_ascii=False)
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO outbox_queue(msg_id,peer_id,ip,port,msg_blob,attempts,next_retry_at,created_at,updated_at,status)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(msg_id) DO UPDATE SET
              peer_id=excluded.peer_id,
              ip=excluded.ip,
              port=excluded.port,
              msg_blob=excluded.msg_blob,
              next_retry_at=excluded.next_retry_at,
              updated_at=excluded.updated_at,
              status='pending'
            """,
            (msg_id, peer_id, ip, int(port), blob, 0, float(next_retry_at), now, now, "pending"),
        )
        conn.commit()


def mark_outbox_attempt(msg_id: str, attempts: int, next_retry_at: float, status: str = "pending"):
    conn = _connect()
    with _DB_LOCK:
        conn.execute(
            "UPDATE outbox_queue SET attempts=?, next_retry_at=?, updated_at=?, status=? WHERE msg_id=?",
            (int(attempts), float(next_retry_at), time.time(), status, msg_id),
        )
        conn.commit()


def mark_outbox_delivered(msg_id: str):
    conn = _connect()
    with _DB_LOCK:
        conn.execute("DELETE FROM outbox_queue WHERE msg_id=?", (msg_id,))
        conn.commit()


def enqueue_inbox(msg_id: str, peer_id: str, msg_dict: dict, received_at: Optional[float] = None):
    if not msg_id or not peer_id:
        return
    conn = _connect()
    blob = json.dumps(msg_dict, ensure_ascii=False)
    ts = float(received_at or time.time())
    with _DB_LOCK:
        conn.execute(
            """
            INSERT INTO inbox_queue(msg_id,peer_id,msg_blob,received_at,status)
            VALUES(?,?,?,?,?)
            ON CONFLICT(msg_id) DO UPDATE SET
              peer_id=excluded.peer_id,
              msg_blob=excluded.msg_blob,
              status='pending'
            """,
            (msg_id, peer_id, blob, ts, "pending"),
        )
        conn.commit()


def mark_inbox_processed(msg_id: str):
    if not msg_id:
        return
    conn = _connect()
    with _DB_LOCK:
        conn.execute(
            "UPDATE inbox_queue SET status='processed', processed_at=? WHERE msg_id=?",
            (time.time(), msg_id),
        )
        conn.commit()


def load_pending_inbox(limit: int = 200) -> List[dict]:
    conn = _connect()
    with _DB_LOCK:
        rows = conn.execute(
            "SELECT msg_id,peer_id,msg_blob,received_at FROM inbox_queue WHERE status='pending' ORDER BY received_at ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
    out: List[dict] = []
    for r in rows:
        try:
            msg = json.loads(r["msg_blob"])
        except Exception:
            msg = {}
        out.append({
            "msg_id": str(r["msg_id"]),
            "peer_id": str(r["peer_id"]),
            "msg": msg,
            "received_at": float(r["received_at"] or 0.0),
        })
    return out


def load_due_outbox(limit: int = 200) -> List[dict]:
    conn = _connect()
    now = time.time()
    with _DB_LOCK:
        rows = conn.execute(
            "SELECT * FROM outbox_queue WHERE status='pending' AND next_retry_at<=? ORDER BY next_retry_at ASC LIMIT ?",
            (now, int(limit)),
        ).fetchall()
    out = []
    for r in rows:
        try:
            msg = json.loads(r["msg_blob"])
        except Exception:
            msg = {}
        out.append({
            "msg_id": r["msg_id"],
            "peer_id": r["peer_id"],
            "ip": r["ip"],
            "port": int(r["port"]),
            "attempts": int(r["attempts"]),
            "msg": msg,
        })
    return out


def outbox_pending_count() -> int:
    conn = _connect()
    with _DB_LOCK:
        row = conn.execute("SELECT COUNT(*) AS c FROM outbox_queue WHERE status='pending'").fetchone()
    return int(row["c"])


def incr_counter(key: str, delta: float = 1.0):
    conn = _connect()
    with _DB_LOCK:
        conn.execute(
            "INSERT INTO kv_counters(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
            (key, float(delta)),
        )
        conn.commit()


def get_counters(prefix: str = "") -> Dict[str, float]:
    conn = _connect()
    with _DB_LOCK:
        if prefix:
            rows = conn.execute("SELECT key,value FROM kv_counters WHERE key LIKE ?", (f"{prefix}%",)).fetchall()
        else:
            rows = conn.execute("SELECT key,value FROM kv_counters").fetchall()
    return {str(r["key"]): float(r["value"] or 0.0) for r in rows}
