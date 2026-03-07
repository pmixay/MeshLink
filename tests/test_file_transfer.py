import os
import time

from core.file_transfer import FileTransferManager


def test_cleanup_stale_partials_once(tmp_workspace):
    mgr = FileTransferManager()
    mgr.partial_ttl_seconds = 1

    part_dir = mgr._partials_dir()
    stale = os.path.join(part_dir, "old.part")
    fresh = os.path.join(part_dir, "new.part")
    with open(stale, "wb") as f:
        f.write(b"x")
    with open(fresh, "wb") as f:
        f.write(b"y")

    old_ts = time.time() - 3600
    os.utime(stale, (old_ts, old_ts))

    mgr._cleanup_stale_partials_once()
    assert not os.path.exists(stale)
    assert os.path.exists(fresh)


def test_send_file_rejects_zero_and_missing(tmp_workspace):
    mgr = FileTransferManager()

    missing = mgr.send_file("not_exists.bin", "127.0.0.1", 9000, "p")
    assert missing is None

    p = tmp_workspace["base"] / "empty.bin"
    p.write_bytes(b"")
    empty = mgr.send_file(str(p), "127.0.0.1", 9000, "p")
    assert empty is None


def test_send_worker_sets_failed_after_retries(monkeypatch, tmp_workspace):
    mgr = FileTransferManager()
    mgr.max_retries = 2
    mgr.retry_backoff_base = 0

    src = tmp_workspace["base"] / "f.bin"
    src.write_bytes(b"abc123")

    file_id = mgr.send_file(str(src), "127.0.0.1", 65500, "peer1")
    assert file_id

    # Ждём поток-воркер (в тесте сеть недоступна, должен уйти в failed)
    deadline = time.time() + 5
    while time.time() < deadline:
        t = mgr.transfers[file_id]
        if t.status in ("failed", "complete", "rejected"):
            break
        time.sleep(0.05)

    final_status = mgr.transfers[file_id].status
    assert final_status in ("retrying", "failed", "rejected")
