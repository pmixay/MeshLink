"""
MeshLink — End-to-End Encryption + Message Signing Layer

Provides:
  - X25519 key exchange → AES-256-GCM per-peer sessions (E2E encryption)
  - Ed25519 key pair for message signing and authentication
  - Seed-pairing: 6-char shared code → PBKDF2 key → trusted session
"""

import os
import base64
import hashlib
import secrets
import logging
from typing import Optional

from .config import SEED_ALPHABET, SEED_LENGTH, SEED_KDF_SALT, SEED_KDF_ITERATIONS

logger = logging.getLogger("meshlink.crypto")

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.exceptions import InvalidSignature
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    logger.warning("cryptography library not found — encryption disabled")


# ── Key Pair (X25519 for ECDH) ──────────────────────────────────────────────

class KeyPair:
    """Manages an X25519 key pair for Diffie-Hellman key exchange."""

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
        """Derive a 256-bit shared secret from peer's X25519 public key."""
        if not HAS_CRYPTO or not self.private_key:
            return b"\x00" * 32
        peer_pub = X25519PublicKey.from_public_bytes(peer_public_bytes)
        shared = self.private_key.exchange(peer_pub)
        # HKDF-like: hash to get uniform 256-bit key
        return hashlib.sha256(shared).digest()


# ── Signing Key (Ed25519) ────────────────────────────────────────────────────

class SigningKey:
    """Ed25519 key pair for message signing and verification."""

    def __init__(self):
        if not HAS_CRYPTO:
            self.private_key = None
            self.public_bytes = b""
            return
        self.private_key = Ed25519PrivateKey.generate()
        self.public_bytes = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def sign(self, data: bytes) -> bytes:
        """Sign data. Returns 64-byte Ed25519 signature, or empty bytes if unavailable."""
        if not HAS_CRYPTO or not self.private_key:
            return b""
        return self.private_key.sign(data)

    @staticmethod
    def verify(data: bytes, signature: bytes, public_key_bytes: bytes) -> bool:
        """Verify an Ed25519 signature. Returns True if valid."""
        if not HAS_CRYPTO:
            return True  # passthrough when no crypto available
        if not signature or not public_key_bytes:
            return False
        try:
            pub = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            pub.verify(signature, data)
            return True
        except (InvalidSignature, Exception):
            return False


# ── Session Cipher (AES-256-GCM) ────────────────────────────────────────────

class SessionCipher:
    """AES-256-GCM cipher bound to a 256-bit shared key."""

    def __init__(self, shared_key: bytes):
        self.shared_key = shared_key
        self._aesgcm = AESGCM(shared_key) if HAS_CRYPTO else None

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt data. Returns nonce (12 B) || ciphertext+tag."""
        if not self._aesgcm:
            return plaintext
        nonce = os.urandom(12)
        ct = self._aesgcm.encrypt(nonce, plaintext, None)
        return nonce + ct

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt nonce+ciphertext bundle. Raises on authentication failure."""
        if not self._aesgcm:
            return data
        if len(data) < 13:  # 12 nonce + at least 1 byte ct
            raise ValueError("Ciphertext too short")
        nonce, ct = data[:12], data[12:]
        return self._aesgcm.decrypt(nonce, ct, None)


# ── Crypto Manager ───────────────────────────────────────────────────────────

class CryptoManager:
    """
    Central crypto manager for MeshLink.

    Features:
      - Per-peer AES-256-GCM sessions via X25519 ECDH key exchange
      - Ed25519 message signing and peer-signature verification
      - Seed-pairing: shared 6-char code → PBKDF2 trusted session
    """

    def __init__(self):
        self.keypair     = KeyPair()        # X25519 for ECDH
        self.signing_key = SigningKey()     # Ed25519 for signatures

        # peer_id → AES-GCM cipher (from X25519 exchange)
        self._sessions:         dict[str, SessionCipher] = {}
        # peer_id → Ed25519 public key bytes (for verifying their signatures)
        self._peer_signing_keys: dict[str, bytes] = {}
        # peer_id → AES-GCM cipher (from seed-pairing — trusted channel)
        self._seed_sessions:    dict[str, SessionCipher] = {}
        # Set of peer_ids that completed seed-pairing
        self.trusted_peers:     set[str] = set()

    # ── Own public keys ────────────────────────────────────────────────────

    @property
    def public_key_b64(self) -> str:
        """X25519 DH public key, base64-encoded."""
        return base64.b64encode(self.keypair.public_bytes).decode()

    @property
    def signing_key_b64(self) -> str:
        """Ed25519 signing public key, base64-encoded."""
        return base64.b64encode(self.signing_key.public_bytes).decode()

    # ── Session management ─────────────────────────────────────────────────

    def establish_session(self, peer_id: str, peer_public_b64: str,
                          peer_signing_b64: str = ""):
        """
        Derive a shared AES-GCM key via X25519 and store the session.
        Optionally register the peer's Ed25519 signing public key.
        """
        try:
            peer_pub = base64.b64decode(peer_public_b64)
            shared   = self.keypair.derive_shared_key(peer_pub)
            self._sessions[peer_id] = SessionCipher(shared)
            logger.info(f"E2E session established with {peer_id}")
        except Exception as e:
            logger.error(f"Session establishment failed for {peer_id}: {e}")
            return

        if peer_signing_b64:
            self.register_peer_signing_key(peer_id, peer_signing_b64)

    def register_peer_signing_key(self, peer_id: str, signing_key_b64: str):
        """Store a peer's Ed25519 public key for future signature verification."""
        try:
            self._peer_signing_keys[peer_id] = base64.b64decode(signing_key_b64)
        except Exception as e:
            logger.warning(f"Failed to register signing key for {peer_id}: {e}")

    def has_session(self, peer_id: str) -> bool:
        return peer_id in self._sessions

    # ── E2E Encrypt / Decrypt ──────────────────────────────────────────────

    def encrypt_for(self, peer_id: str, plaintext: bytes) -> bytes:
        """Encrypt plaintext for a peer. Returns ciphertext (or plaintext if no session)."""
        cipher = self._sessions.get(peer_id)
        if cipher:
            return cipher.encrypt(plaintext)
        return plaintext

    def decrypt_from(self, peer_id: str, data: bytes) -> bytes:
        """Decrypt ciphertext from a peer. Raises on auth failure."""
        cipher = self._sessions.get(peer_id)
        if cipher:
            return cipher.decrypt(data)
        return data

    # ── Sign / Verify ──────────────────────────────────────────────────────

    def sign(self, data: bytes) -> str:
        """Sign data with our Ed25519 key. Returns base64-encoded signature."""
        sig = self.signing_key.sign(data)
        return base64.b64encode(sig).decode()

    def verify_from(self, peer_id: str, data: bytes, signature_b64: str) -> bool:
        """
        Verify that `data` was signed by `peer_id`.
        Returns False if peer signing key is unknown or signature is invalid.
        """
        pub_bytes = self._peer_signing_keys.get(peer_id)
        if not pub_bytes:
            return False
        try:
            sig = base64.b64decode(signature_b64)
            return SigningKey.verify(data, sig, pub_bytes)
        except Exception:
            return False

    # ── Seed Pairing ───────────────────────────────────────────────────────

    @staticmethod
    def generate_seed() -> str:
        """Generate a random 6-character pairing seed using the unambiguous alphabet."""
        return "".join(secrets.choice(SEED_ALPHABET) for _ in range(SEED_LENGTH))

    @staticmethod
    def _derive_key_from_seed(seed: str) -> bytes:
        """Derive a 256-bit AES key from the seed via PBKDF2-HMAC-SHA256."""
        if not HAS_CRYPTO:
            # Fallback: simple SHA-256 (no real KDF when library absent)
            return hashlib.sha256(seed.upper().encode()).digest()
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=SEED_KDF_SALT,
            iterations=SEED_KDF_ITERATIONS,
        )
        return kdf.derive(seed.upper().encode("utf-8"))

    def establish_seed_session(self, peer_id: str, seed: str):
        """
        Establish a trusted session using a shared 6-char seed.
        Both nodes must call this with the same seed to communicate.
        """
        key = self._derive_key_from_seed(seed)
        self._seed_sessions[peer_id] = SessionCipher(key)
        self.trusted_peers.add(peer_id)
        logger.info(f"Seed-paired trusted session established with {peer_id}")

    def is_trusted(self, peer_id: str) -> bool:
        """True if this peer completed seed-pairing."""
        return peer_id in self.trusted_peers

    def encrypt_trusted(self, peer_id: str, plaintext: bytes) -> bytes:
        """Encrypt using seed-derived key (trusted channel), fall back to E2E."""
        cipher = self._seed_sessions.get(peer_id) or self._sessions.get(peer_id)
        if cipher:
            return cipher.encrypt(plaintext)
        return plaintext

    def decrypt_trusted(self, peer_id: str, data: bytes) -> bytes:
        """Decrypt using seed-derived key (trusted channel), fall back to E2E."""
        cipher = self._seed_sessions.get(peer_id) or self._sessions.get(peer_id)
        if cipher:
            return cipher.decrypt(data)
        return data
