"""
MeshLink — File Transfer Engine (v2)
Dedicated TCP server for reliable chunked file transfers.

Protocol:
  1. Sender connects to receiver's FILE_PORT
  2. Sender sends JSON header terminated by newline
  3. Receiver sends accept/reject JSON line back
  4. Sender streams raw file bytes
  5. Sender sends JSON trailer with sha256 checksum
"""

import os
import json
import time
import uuid
import socket
import hashlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from .config import (
    NODE_ID, NODE_NAME, CHUNK_SIZE, MAX_FILE_SIZE,
    DOWNLOADS_DIR, FILE_PORT,
)
from .messaging import persist_chat_entry
from . import storage

logger = logging.getLogger("meshlink.filetransfer")


@dataclass
class TransferInfo:
    file_id: str
    filename: str
    filesize: int
    direction: str
    peer_id: str
    peer_name: str = ""
    progress: int = 0
    status: str = "pending"
    sha256: str = ""
    start_time: float = field(default_factory=time.time)
    speed: float = 0.0
    saved_as: str = ""       # actual filename on disk (after dedup)

    @property
    def percent(self) -> float:
        if self.filesize == 0:
            return 100.0
        return min(100.0, (self.progress / self.filesize) * 100)

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "filesize": self.filesize,
            "direction": self.direction,
            "peer_id": self.peer_id,
            "peer_name": self.peer_name,
            "progress": self.progress,
            "percent": round(self.percent, 1),
            "status": self.status,
            "sha256": self.sha256,
            "speed": self.speed,
            "saved_as": self.saved_as,
        }


def _recv_line(sock: socket.socket, max_len: int = 65536) -> bytes:
    buf = b""
    while len(buf) < max_len:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("Connection closed while reading line")
        if b == b"\n":
            return buf
        buf += b
    raise ValueError("Line too long")


class FileTransferManager:

    def __init__(self):
        self.transfers: Dict[str, TransferInfo] = {}
        self._lock = threading.Lock()
        self._running = False
        self._server_sock: Optional[socket.socket] = None
        self.on_progress: Optional[Callable] = None
        self.on_complete: Optional[Callable] = None
        self.on_file_received: Optional[Callable] = None
        self.max_retries = 4
        self.retry_backoff_base = 0.5
        self.max_active_sends = 4
        self.partial_ttl_seconds = 24 * 3600
        self._send_semaphore = threading.Semaphore(self.max_active_sends)
        self._active_sends = 0

    def start(self):
        self._running = True
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.settimeout(1.0)
        self._server_sock.bind(("0.0.0.0", FILE_PORT))
        self._server_sock.listen(10)
        threading.Thread(target=self._accept_loop, daemon=True, name="file-server").start()
        threading.Thread(target=self._partial_cleanup_loop, daemon=True, name="file-partial-gc").start()
        logger.info(f"File transfer server on port {FILE_PORT}")

    def stop(self):
        self._running = False
        if self._server_sock:
            try: self._server_sock.close()
            except: pass

    def get_transfers(self) -> list:
        with self._lock:
            return [t.to_dict() for t in self.transfers.values()]

    def get_send_diagnostics(self) -> dict:
        with self._lock:
            active = int(self._active_sends)
        return {
            "active_sends": active,
            "max_active_sends": int(self.max_active_sends),
            "available_send_slots": max(0, int(self.max_active_sends) - active),
        }

    def _partials_dir(self) -> str:
        d = os.path.join(DOWNLOADS_DIR, ".partials")
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _atomic_write_json(path: str, obj: dict):
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)

    @staticmethod
    def _sha256_prefix(path: str, length: int) -> str:
        length = max(0, int(length or 0))
        sha = hashlib.sha256()
        if length == 0:
            return sha.hexdigest()
        with open(path, "rb") as rf:
            remaining = length
            while remaining > 0:
                chunk = rf.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                sha.update(chunk)
                remaining -= len(chunk)
        return sha.hexdigest()

    def _manifest_path(self, file_id: str) -> str:
        return os.path.join(self._partials_dir(), f"{file_id}.manifest.json")

    def _load_manifest(self, file_id: str) -> dict:
        path = self._manifest_path(file_id)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_manifest(self, file_id: str, data: dict):
        self._atomic_write_json(self._manifest_path(file_id), data)

    def _remove_manifest(self, file_id: str):
        path = self._manifest_path(file_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    def _partial_cleanup_loop(self):
        while self._running:
            try:
                self._cleanup_stale_partials_once()
            except Exception as e:
                logger.debug(f"Partial cleanup error: {e}")
            time.sleep(300)

    def _cleanup_stale_partials_once(self):
        """One-pass stale partial cleanup (used by loop and tests)."""
        now = time.time()
        part_dir = self._partials_dir()
        for name in os.listdir(part_dir):
            if not name.endswith(".part"):
                continue
            path = os.path.join(part_dir, name)
            try:
                st = os.stat(path)
            except Exception:
                continue
            if now - st.st_mtime > self.partial_ttl_seconds:
                try:
                    os.remove(path)
                    manifest = f"{os.path.splitext(path)[0]}.manifest.json"
                    if os.path.exists(manifest):
                        os.remove(manifest)
                    logger.info(f"Removed stale partial: {name}")
                except Exception:
                    pass

    # ── Receiver ────────────────────────────────────────────

    def _accept_loop(self):
        while self._running:
            try:
                client, addr = self._server_sock.accept()
                threading.Thread(
                    target=self._handle_incoming, args=(client, addr),
                    daemon=True
                ).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"File accept error: {e}")

    def _handle_incoming(self, sock: socket.socket, addr):
        transfer = None
        try:
            sock.settimeout(30)

            # Read header
            header = json.loads(_recv_line(sock).decode())
            file_id = header.get("file_id", str(uuid.uuid4())[:8])
            filename = header.get("filename", "unknown")
            filesize = header.get("filesize", 0)
            sender_id = header.get("sender_id", "")
            sender_name = header.get("sender_name", "Unknown")
            resume_enabled = bool(header.get("resume", True))

            logger.info(f"Incoming file: {filename} ({filesize} B) from {sender_name}")

            if filesize > MAX_FILE_SIZE:
                sock.sendall(json.dumps({"status": "rejected"}).encode() + b"\n")
                return

            transfer = TransferInfo(
                file_id=file_id, filename=filename, filesize=filesize,
                direction="recv", peer_id=sender_id, peer_name=sender_name,
            )

            part_dir = self._partials_dir()
            part_path = os.path.join(part_dir, f"{file_id}.part")
            manifest = self._load_manifest(file_id)

            existing = 0
            if resume_enabled and os.path.exists(part_path):
                existing = os.path.getsize(part_path)
                if existing > filesize:
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
                    existing = 0

            if manifest:
                mf_size = int(manifest.get("filesize", 0) or 0)
                mf_name = str(manifest.get("filename", ""))
                mf_offset = int(manifest.get("offset", 0) or 0)
                if mf_size != int(filesize) or (mf_name and mf_name != filename) or mf_offset != existing:
                    try:
                        if os.path.exists(part_path):
                            os.remove(part_path)
                    except Exception:
                        pass
                    self._remove_manifest(file_id)
                    existing = 0
                    manifest = {}

            remote_offset_sha = str(header.get("offset_sha256", ""))
            if existing > 0 and remote_offset_sha:
                local_prefix = self._sha256_prefix(part_path, existing)
                if local_prefix != remote_offset_sha:
                    logger.warning(f"Resume prefix SHA mismatch for {filename}, resetting partial")
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
                    self._remove_manifest(file_id)
                    existing = 0
                    manifest = {}

            existing_prefix_sha = ""
            if existing > 0 and os.path.exists(part_path):
                existing_prefix_sha = self._sha256_prefix(part_path, existing)
                storage.incr_counter("metrics.file_resume_total", 1.0)

            transfer.progress = existing

            with self._lock:
                self.transfers[file_id] = transfer

            # Accept
            sock.sendall(json.dumps({
                "status": "accepted",
                "offset": existing,
                "offset_sha256": existing_prefix_sha,
            }).encode() + b"\n")
            transfer.status = "active"

            sha = hashlib.sha256()
            if existing > 0:
                with open(part_path, "rb") as rf:
                    while True:
                        chunk = rf.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        sha.update(chunk)

            last_emit = time.time()

            with open(part_path, "ab" if existing > 0 else "wb") as f:
                remaining = filesize - existing
                self._save_manifest(file_id, {
                    "file_id": file_id,
                    "filename": filename,
                    "filesize": int(filesize),
                    "offset": int(existing),
                    "sha256_prefix": sha.hexdigest(),
                    "updated_at": time.time(),
                })
                while remaining > 0:
                    to_read = min(CHUNK_SIZE, remaining)
                    data = b""
                    while len(data) < to_read:
                        recv = sock.recv(to_read - len(data))
                        if not recv:
                            raise ConnectionError("Lost connection during transfer")
                        data += recv
                    f.write(data)
                    sha.update(data)
                    remaining -= len(data)
                    transfer.progress += len(data)

                    elapsed = time.time() - transfer.start_time
                    if elapsed > 0:
                        transfer.speed = transfer.progress / elapsed

                    now = time.time()
                    if now - last_emit >= 0.1:
                        last_emit = now
                        self._save_manifest(file_id, {
                            "file_id": file_id,
                            "filename": filename,
                            "filesize": int(filesize),
                            "offset": int(transfer.progress),
                            "sha256_prefix": sha.hexdigest(),
                            "updated_at": now,
                        })
                        if self.on_progress:
                            self.on_progress(transfer)

            # Read trailer
            try:
                sock.settimeout(5)
                trailer = json.loads(_recv_line(sock).decode())
                remote_sha = trailer.get("sha256", "")
            except:
                remote_sha = ""

            local_sha = sha.hexdigest()
            transfer.sha256 = local_sha

            if remote_sha and remote_sha != local_sha:
                logger.warning(f"Checksum mismatch: {filename}")
                transfer.status = "checksum_error"
                if self.on_progress:
                    self.on_progress(transfer)
                return

            # Finalize from partial into a deduplicated filename in downloads
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            base, ext = os.path.splitext(filepath)
            c = 1
            while os.path.exists(filepath):
                filepath = f"{base}_{c}{ext}"
                c += 1

            os.replace(part_path, filepath)
            self._remove_manifest(file_id)
            transfer.saved_as = os.path.basename(filepath)
            transfer.status = "complete"

            logger.info(f"File received: {filename} → {filepath}")
            persist_chat_entry({
                "peer_id": transfer.peer_id,
                "msg_id": f"file-{transfer.file_id}",
                "sender_id": transfer.peer_id,
                "sender_name": transfer.peer_name or "Peer",
                "text": transfer.filename,
                "timestamp": time.time(),
                "is_me": False,
                "msg_type": "file",
                "file_name": transfer.filename,
                "file_size": transfer.filesize,
                "file_url": f"/downloads/{transfer.saved_as}",
                "status": "delivered",
            })
            if self.on_progress:
                self.on_progress(transfer)
            if self.on_complete:
                self.on_complete(transfer)

        except Exception as e:
            logger.error(f"Receive error: {e}")
            if transfer:
                transfer.status = "interrupted"
                if self.on_progress:
                    self.on_progress(transfer)
        finally:
            try: sock.close()
            except: pass

    # ── Sender ──────────────────────────────────────────────

    def send_file(self, filepath: str, peer_ip: str, peer_file_port: int,
                  peer_id: str) -> Optional[str]:
        if not os.path.isfile(filepath):
            logger.error(f"File not found: {filepath}")
            return None

        filesize = os.path.getsize(filepath)
        if filesize > MAX_FILE_SIZE:
            return None
        if filesize == 0:
            return None

        file_id = str(uuid.uuid4())[:8]
        filename = os.path.basename(filepath)

        transfer = TransferInfo(
            file_id=file_id, filename=filename, filesize=filesize,
            direction="send", peer_id=peer_id,
        )
        with self._lock:
            self.transfers[file_id] = transfer

        threading.Thread(
            target=self._send_worker,
            args=(filepath, peer_ip, peer_file_port, transfer),
            daemon=True, name=f"file-send-{file_id}"
        ).start()
        return file_id

    def _send_worker(self, filepath: str, peer_ip: str,
                     peer_file_port: int, transfer: TransferInfo):
        self._send_semaphore.acquire()
        with self._lock:
            self._active_sends += 1
        last_error = None
        try:
            for attempt in range(1, self.max_retries + 1):
                sock = None
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.settimeout(12)
                    sock.connect((peer_ip, peer_file_port))

                    # Send header with resume capability
                    header = {
                        "file_id": transfer.file_id,
                        "filename": transfer.filename,
                        "filesize": transfer.filesize,
                        "sender_id": NODE_ID,
                        "sender_name": NODE_NAME,
                        "resume": True,
                    }
                    sock.sendall(json.dumps(header).encode() + b"\n")

                    # Wait for accept and offset
                    resp = json.loads(_recv_line(sock).decode())
                    if resp.get("status") != "accepted":
                        transfer.status = "rejected"
                        if self.on_progress:
                            self.on_progress(transfer)
                        return

                    offset = int(resp.get("offset", 0) or 0)
                    if offset < 0 or offset > transfer.filesize:
                        offset = 0

                    transfer.progress = offset
                    transfer.status = "active"
                    sha = hashlib.sha256()

                    # Hash already-sent portion to keep final checksum correct
                    if offset > 0:
                        with open(filepath, "rb") as rf:
                            remaining_hash = offset
                            while remaining_hash > 0:
                                piece = rf.read(min(CHUNK_SIZE, remaining_hash))
                                if not piece:
                                    break
                                sha.update(piece)
                                remaining_hash -= len(piece)

                    # Optional backward-compatible offset checksum validation.
                    remote_expected_offset_sha = str(resp.get("offset_sha256", ""))
                    if remote_expected_offset_sha and sha.hexdigest() != remote_expected_offset_sha:
                        logger.warning(
                            f"Offset SHA mismatch for {transfer.filename}; restarting from zero"
                        )
                        offset = 0
                        transfer.progress = 0
                        sha = hashlib.sha256()

                    last_emit = time.time()
                    sock.settimeout(120)

                    with open(filepath, "rb") as f:
                        f.seek(offset)
                        while transfer.progress < transfer.filesize:
                            chunk = f.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            sock.sendall(chunk)
                            sha.update(chunk)
                            transfer.progress += len(chunk)

                            elapsed = time.time() - transfer.start_time
                            if elapsed > 0:
                                transfer.speed = transfer.progress / elapsed

                            now = time.time()
                            if now - last_emit >= 0.1:
                                last_emit = now
                                if self.on_progress:
                                    self.on_progress(transfer)

                    if transfer.progress < transfer.filesize:
                        raise ConnectionError("Transfer interrupted before EOF")

                    # Send trailer
                    transfer.sha256 = sha.hexdigest()
                    sock.sendall(json.dumps({"sha256": transfer.sha256}).encode() + b"\n")

                    transfer.status = "complete"
                    logger.info(f"File sent: {transfer.filename}")
                    persist_chat_entry({
                        "peer_id": transfer.peer_id,
                        "msg_id": f"file-{transfer.file_id}",
                        "sender_id": NODE_ID,
                        "sender_name": NODE_NAME,
                        "text": transfer.filename,
                        "timestamp": time.time(),
                        "is_me": True,
                        "msg_type": "file",
                        "file_name": transfer.filename,
                        "file_size": transfer.filesize,
                        "status": "delivered",
                    })
                    if self.on_progress:
                        self.on_progress(transfer)
                    if self.on_complete:
                        self.on_complete(transfer)
                    return

                except Exception as e:
                    last_error = e
                    transfer.status = "retrying" if attempt < self.max_retries else "failed"
                    logger.warning(
                        f"Send attempt {attempt}/{self.max_retries} failed for {transfer.filename}: {e}"
                    )
                    if self.on_progress:
                        self.on_progress(transfer)
                    if attempt < self.max_retries:
                        time.sleep(self.retry_backoff_base * (2 ** (attempt - 1)))
                finally:
                    if sock:
                        try:
                            sock.close()
                        except Exception:
                            pass

            logger.error(f"Send failed: {transfer.filename}: {last_error}")
        finally:
            with self._lock:
                self._active_sends = max(0, int(self._active_sends) - 1)
            self._send_semaphore.release()
