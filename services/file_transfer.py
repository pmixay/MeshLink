"""
MeshLink — File Transfer Service (microservice module)

Re-exports the file transfer engine from core/file_transfer.py.
Provides reliable chunked file transfer with:

  - 64 KB chunks over dedicated TCP connection (port 5153 by default)
  - SHA-256 integrity verification on receive
  - Transfer resumption on network interruption
  - Progress callbacks for UI updates
  - Up to 2 GB per file
  - Safe filename deduplication on receiver side
"""

from core.file_transfer import (  # noqa: F401
    FileTransferManager,
    TransferInfo,
)

__all__ = ["FileTransferManager", "TransferInfo"]
