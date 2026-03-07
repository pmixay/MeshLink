"""
MeshLink — Mesh Routing Service (microservice module)

Re-exports mesh routing logic. The concrete implementation lives in
core/node.py (_LRUSet, _flood_relay, _send_mesh_text) and is
orchestrated there.

This module provides type stubs and documentation for the mesh
sub-system so that it can be developed and tested independently.

Mesh protocol summary:
  - Every node forwards received TEXT messages to all known peers
    (flooding) unless the message ID is in the LRU dedup cache.
  - TTL starts at MESH_TTL_DEFAULT (5 hops) and decrements at each hop.
  - Messages with TTL <= 0 are dropped and not forwarded.
  - LRU cache (size MESH_LRU_MAX_SIZE) stores recently seen msg_ids to
    prevent forwarding loops.
  - Adaptive fanout: when the relay queue is under pressure, fewer peers
    are selected for forwarding (MESH_RELAY_FANOUT_MIN .. MESH_RELAY_FANOUT_MAX).
  - Back-pressure limit: if outbox_pending + relay_pending exceeds
    MESH_RELAY_BACKPRESSURE_MAX_PENDING, the relay message is dropped
    and a security event is recorded.
"""

from core.node import _LRUSet  # noqa: F401 — expose for external testing

__all__ = ["_LRUSet"]
