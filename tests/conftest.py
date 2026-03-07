import os
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_workspace(monkeypatch):
    base = tempfile.mkdtemp(prefix="meshlink_tests_")
    downloads = os.path.join(base, "downloads")
    data_dir = os.path.join(base, "data")
    os.makedirs(downloads, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    monkeypatch.setattr("core.config.DOWNLOADS_DIR", downloads, raising=False)
    monkeypatch.setattr("core.node.DOWNLOADS_DIR", downloads, raising=False)
    monkeypatch.setattr("core.file_transfer.DOWNLOADS_DIR", downloads, raising=False)
    monkeypatch.setattr("core.storage._base_dir", lambda: data_dir, raising=False)
    monkeypatch.setattr("core.storage._CONN", None, raising=False)

    def _history_path():
        return os.path.join(data_dir, "chat_history.jsonl")

    monkeypatch.setattr("core.messaging._history_store_path", _history_path, raising=False)

    yield {
        "base": Path(base),
        "downloads": Path(downloads),
        "data": Path(data_dir),
    }

    try:
        from core import storage
        if getattr(storage, "_CONN", None) is not None:
            storage._CONN.close()
            storage._CONN = None
    except Exception:
        pass

    shutil.rmtree(base, ignore_errors=True)

