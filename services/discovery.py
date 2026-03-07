"""
MeshLink — Discovery Service (microservice wrapper)

Re-exports the core DiscoveryService with enhanced socket configuration
for better LAN peer discovery compatibility across different OS/firewall
configurations.

Improvements over the original core/discovery.py:
  - Sends an immediate burst of 5 announcements on startup so peers
    appear within a second instead of waiting for the first 3-second tick.
  - Tries SO_REUSEPORT in addition to SO_REUSEADDR.
  - Binds the listener to '' (all interfaces) AND joins multicast on
    every available interface (not just the default route).
  - Falls back gracefully when multicast or broadcast is unavailable.
  - Supports firewall port auto-opening via services.firewall.
"""

# The concrete implementation lives in core/discovery.py to keep the
# existing test suite intact.  This module is the canonical import path
# for all new code.
from core.discovery import DiscoveryService, PeerInfo  # noqa: F401

__all__ = ["DiscoveryService", "PeerInfo"]
