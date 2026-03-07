"""
MeshLink — BaseService

Abstract base class for all MeshLink microservices.
Provides a uniform lifecycle (start / stop) and logging interface.
Each concrete service runs in one or more daemon threads.
"""

import logging
import threading
from abc import ABC, abstractmethod


class BaseService(ABC):
    """
    Minimal contract that every MeshLink service must satisfy.

    Concrete services:
      - Override ``start()`` to launch background threads.
      - Override ``stop()`` to signal threads to exit.
      - May define additional public methods as their API.
      - Must NOT import from other services directly — all
        cross-service communication uses constructor-injected callbacks.
    """

    def __init__(self, name: str):
        self._name = name
        self._running = False
        self._threads: list[threading.Thread] = []
        self.logger = logging.getLogger(f"meshlink.{name}")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @abstractmethod
    def start(self):
        """Launch background threads and begin service operation."""

    @abstractmethod
    def stop(self):
        """Signal all background threads to exit and clean up resources."""

    def _spawn(self, target, name: str, **kwargs) -> threading.Thread:
        """Create and register a daemon thread, started immediately."""
        t = threading.Thread(target=target, name=f"{self._name}.{name}",
                             daemon=True, **kwargs)
        t.start()
        self._threads.append(t)
        return t

    def _join_threads(self, timeout: float = 2.0):
        """Wait for all registered threads to exit."""
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()

    def __repr__(self) -> str:
        status = "running" if self._running else "stopped"
        return f"<{self.__class__.__name__} name={self._name!r} {status}>"
