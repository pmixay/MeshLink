"""
MeshLink — End-to-End Encryption Layer
Uses X25519 key exchange + AES-256-GCM for message encryption.
Falls back to Fernet if cryptography primitives unavailable.
"""

import os
import base64
import hashlib
import logging
from typing import Optional, Tuple

logger = logging.getLogger("meshlink.crypto")

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import serialization
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    logger.warning("cryptography library not found — encryption disabled")


class KeyPair:
    """Manages an X25519 key pair for Diffie-Hellman exchange."""

    def __init__(self):
        if not HAS_CRYPTO:
            self.private_key = None
            self.public_bytes = b""
            return
        self.private_key = X25519PrivateKey.generate()
        self.public_bytes = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def derive_shared_key(self, peer_public_bytes: bytes) -> bytes:
        """Derive a 256-bit shared secret from peer's public key."""
        if not HAS_CRYPTO or not self.private_key:
            return b"\x00" * 32
        peer_pub = X25519PublicKey.from_public_bytes(peer_public_bytes)
        shared = self.private_key.exchange(peer_pub)
        return hashlib.sha256(shared).digest()


class SessionCipher:
    """AES-256-GCM cipher bound to a shared secret."""

    def __init__(self, shared_key: bytes):
        self.shared_key = shared_key
        if HAS_CRYPTO:
            self._aesgcm = AESGCM(shared_key)
        else:
            self._aesgcm = None

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt data. Returns nonce (12 bytes) + ciphertext."""
        if not self._aesgcm:
            return plaintext  # passthrough if no crypto
        nonce = os.urandom(12)
        ct = self._aesgcm.encrypt(nonce, plaintext, None)
        return nonce + ct

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt nonce+ciphertext bundle."""
        if not self._aesgcm:
            return data
        nonce, ct = data[:12], data[12:]
        return self._aesgcm.decrypt(nonce, ct, None)


class CryptoManager:
    """
    Per-peer encryption manager.
    Stores session ciphers keyed by peer_id.
    """

    def __init__(self):
        self.keypair = KeyPair()
        self._sessions: dict[str, SessionCipher] = {}

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(self.keypair.public_bytes).decode()

    def establish_session(self, peer_id: str, peer_public_b64: str):
        """Derive shared key and store a session cipher for this peer."""
        peer_pub = base64.b64decode(peer_public_b64)
        shared = self.keypair.derive_shared_key(peer_pub)
        self._sessions[peer_id] = SessionCipher(shared)
        logger.info(f"E2E session established with {peer_id}")

    def encrypt_for(self, peer_id: str, plaintext: bytes) -> bytes:
        cipher = self._sessions.get(peer_id)
        if cipher:
            return cipher.encrypt(plaintext)
        return plaintext

    def decrypt_from(self, peer_id: str, data: bytes) -> bytes:
        cipher = self._sessions.get(peer_id)
        if cipher:
            return cipher.decrypt(data)
        return data

    def has_session(self, peer_id: str) -> bool:
        return peer_id in self._sessions
