"""
MeshLink — Cryptography Service (microservice module)

Re-exports the CryptoManager from core/crypto.py.
The concrete implementation provides:

  - X25519 ECDH → AES-256-GCM per-peer E2E sessions
  - Ed25519 message signing and verification
  - Seed-pairing: 6-char code → PBKDF2-SHA256 → trusted AES-GCM session
  - Session TTL (24 h) and automatic hourly key rotation

This module is the canonical import path for all new code that needs
cryptographic operations.
"""

from core.crypto import (  # noqa: F401
    CryptoManager,
    KeyPair,
    SigningKey,
    SessionCipher,
)

__all__ = ["CryptoManager", "KeyPair", "SigningKey", "SessionCipher"]
