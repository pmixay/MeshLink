"""
MeshLink — Microservices Package

Each sub-module is a self-contained service with a start/stop lifecycle.
Services communicate via callbacks (dependency injection) orchestrated by core/node.py.

Available services:
  base          — Abstract BaseService class
  discovery     — UDP peer discovery (broadcast + multicast)
  crypto        — E2E encryption + message signing
  storage       — Per-user SQLite persistence
  messaging     — TCP messaging protocol
  media         — Voice/video UDP streaming with live metrics
  file_transfer — Chunked file transfer with SHA-256 integrity
  mesh          — Mesh flooding / TTL / LRU deduplication
  firewall      — Firewall integration (ufw / iptables)
"""

from .base import BaseService

__all__ = ["BaseService"]
