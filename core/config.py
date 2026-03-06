"""
MeshLink — Configuration & Constants
"""

import os
import uuid
import socket
import platform

# ──────────────────────────────────────────────
# Identity
# ──────────────────────────────────────────────
NODE_ID = os.environ.get("MESHLINK_NODE_ID", str(uuid.uuid4())[:8])
NODE_NAME = os.environ.get("MESHLINK_NODE_NAME", f"{platform.node()}")

# ──────────────────────────────────────────────
# Network ports
# ──────────────────────────────────────────────
DISCOVERY_PORT = int(os.environ.get("MESHLINK_DISCOVERY_PORT", 5150))
TCP_PORT       = int(os.environ.get("MESHLINK_TCP_PORT",       5151))
MEDIA_PORT     = int(os.environ.get("MESHLINK_MEDIA_PORT",     5152))
FILE_PORT      = int(os.environ.get("MESHLINK_FILE_PORT",      5153))
WEB_PORT       = int(os.environ.get("MESHLINK_WEB_PORT",       8080))

# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────
DISCOVERY_INTERVAL = 3           # seconds between broadcasts
PEER_TIMEOUT       = 15          # seconds before peer considered offline
BROADCAST_ADDR     = "255.255.255.255"
MULTICAST_GROUP    = "239.77.69.83"  # "MESH" in ASCII-ish
DISCOVERY_MAGIC    = b"MESHLINK_V1"

# ──────────────────────────────────────────────
# File transfer
# ──────────────────────────────────────────────
CHUNK_SIZE   = 64 * 1024         # 64 KB chunks
MAX_FILE_SIZE = 2 * 1024 ** 3    # 2 GB max
DOWNLOADS_DIR = os.environ.get(
    "MESHLINK_DOWNLOADS",
    os.path.join(os.path.expanduser("~"), "MeshLink_Downloads")
)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ──────────────────────────────────────────────
# Media / Voice-Video
# ──────────────────────────────────────────────
AUDIO_SAMPLE_RATE        = 16000
AUDIO_CHANNELS           = 1
AUDIO_CHUNK_DURATION_MS  = 20
AUDIO_CHUNK_SAMPLES      = int(AUDIO_SAMPLE_RATE * AUDIO_CHUNK_DURATION_MS / 1000)
VIDEO_FPS                = 24
VIDEO_QUALITY            = 50    # JPEG quality 1-100

# ──────────────────────────────────────────────
# Encryption & Signing
# ──────────────────────────────────────────────
ENCRYPTION_ENABLED   = True
KEY_EXCHANGE_TIMEOUT = 10        # seconds

# ──────────────────────────────────────────────
# Seed Pairing
# ──────────────────────────────────────────────
SEED_LENGTH          = 6         # characters in pairing seed
SEED_ALPHABET        = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # unambiguous chars
SEED_KDF_SALT        = b"MeshLink-SeedPairing-v1"
SEED_KDF_ITERATIONS  = 100_000

# ──────────────────────────────────────────────
# Mesh Flooding / TTL / LRU deduplication
# ──────────────────────────────────────────────
MESH_TTL_DEFAULT     = 5         # max hops for a relayed message
MESH_LRU_MAX_SIZE    = 2000      # max entries in seen-message LRU cache

# ──────────────────────────────────────────────
# Rate limiting & Blacklist
# ──────────────────────────────────────────────
RATE_LIMIT_MAX_MSGS  = 60        # max messages per window per peer
RATE_LIMIT_WINDOW    = 10        # seconds
RATE_LIMIT_BAN_SECS  = 60        # auto-ban duration after limit exceeded

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def get_local_ip() -> str:
    """Get the LAN IP of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

LOCAL_IP = get_local_ip()
