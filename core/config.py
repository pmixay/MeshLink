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
# Discovery settings
# ──────────────────────────────────────────────
DISCOVERY_INTERVAL = float(os.environ.get("MESHLINK_DISCOVERY_INTERVAL", 5.0))
PEER_TIMEOUT = int(os.environ.get("MESHLINK_PEER_TIMEOUT", 30))
BROADCAST_ADDR = os.environ.get("MESHLINK_BROADCAST_ADDR", "255.255.255.255")
MULTICAST_GROUP = os.environ.get("MESHLINK_MULTICAST_GROUP", "224.0.0.1")
MULTICAST_GROUPS = [MULTICAST_GROUP, "224.0.0.251", "239.255.255.250"]  # Add more common multicast groups
DISCOVERY_MAGIC = b"MeshLink-v1"

# ──────────────────────────────────────────────
# Discovery peers (static list for manual discovery)
# ──────────────────────────────────────────────
DISCOVERY_PEERS = [p.strip() for p in os.environ.get("MESHLINK_DISCOVERY_PEERS", "").split(",") if p.strip()]

# ──────────────────────────────────────────────
# File transfer
# ──────────────────────────────────────────────
CHUNK_SIZE   = 64 * 1024
MAX_FILE_SIZE = 2 * 1024 ** 3

# Downloads dir is per-user
def _make_downloads_dir() -> str:
    base = os.environ.get("MESHLINK_DOWNLOADS", "")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "MeshLink_Downloads")
    os.makedirs(base, exist_ok=True)
    return base

DOWNLOADS_DIR = _make_downloads_dir()

# ──────────────────────────────────────────────
# Media / Voice-Video
# ──────────────────────────────────────────────
AUDIO_SAMPLE_RATE        = 16000
AUDIO_CHANNELS           = 1
AUDIO_CHUNK_DURATION_MS  = 20
AUDIO_CHUNK_SAMPLES      = int(AUDIO_SAMPLE_RATE * AUDIO_CHUNK_DURATION_MS / 1000)
VIDEO_FPS                = 24
VIDEO_QUALITY            = 50

# ──────────────────────────────────────────────
# Encryption & Signing
# ──────────────────────────────────────────────
ENCRYPTION_ENABLED   = True
KEY_EXCHANGE_TIMEOUT = 10
SESSION_KEY_TTL_SECONDS = int(os.environ.get("MESHLINK_SESSION_TTL_SECONDS", 24 * 3600))
SESSION_KEY_ROTATE_SECONDS = int(os.environ.get("MESHLINK_SESSION_ROTATE_SECONDS", 3600))
SESSION_MAINTENANCE_INTERVAL_SECONDS = float(os.environ.get("MESHLINK_SESSION_MAINTENANCE_INTERVAL_SECONDS", 15.0))

# ──────────────────────────────────────────────
# Seed Pairing
# ──────────────────────────────────────────────
SEED_LENGTH          = 6
SEED_ALPHABET        = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SEED_KDF_SALT        = b"MeshLink-SeedPairing-v1"
SEED_KDF_ITERATIONS  = 100_000

# ──────────────────────────────────────────────
# Mesh Flooding / TTL / LRU deduplication
# ──────────────────────────────────────────────
MESH_TTL_DEFAULT     = 5
MESH_LRU_MAX_SIZE    = 2000
MESH_RELAY_FANOUT_MIN = int(os.environ.get("MESHLINK_RELAY_FANOUT_MIN", 2))
MESH_RELAY_FANOUT_MAX = int(os.environ.get("MESHLINK_RELAY_FANOUT_MAX", 6))
MESH_RELAY_BACKPRESSURE_MAX_PENDING = int(os.environ.get("MESHLINK_RELAY_BP_MAX_PENDING", 4000))

# ──────────────────────────────────────────────
# Rate limiting & Blacklist
# ──────────────────────────────────────────────
RATE_LIMIT_MAX_MSGS  = 60
RATE_LIMIT_WINDOW    = 10
RATE_LIMIT_BAN_SECS  = 60

# ──────────────────────────────────────────────
# Messaging reliability / backpressure
# ──────────────────────────────────────────────
CHAT_DB_MAX_MB = int(os.environ.get("MESHLINK_CHAT_DB_MAX_MB", 128))
CHAT_DB_MAX_ROWS = int(os.environ.get("MESHLINK_CHAT_DB_MAX_ROWS", 200000))
MESSAGING_MAX_PARALLEL_GLOBAL = int(os.environ.get("MESHLINK_MSG_MAX_PARALLEL_GLOBAL", 16))
MESSAGING_MAX_PARALLEL_PER_PEER = int(os.environ.get("MESHLINK_MSG_MAX_PARALLEL_PER_PEER", 4))
MESSAGING_SEND_SLOT_TIMEOUT = float(os.environ.get("MESHLINK_MSG_SEND_SLOT_TIMEOUT", 1.0))
MESSAGING_CONNECT_TIMEOUT = float(os.environ.get("MESHLINK_MSG_CONNECT_TIMEOUT", 10.0))
MESSAGING_OUTBOX_MAX_PENDING = int(os.environ.get("MESHLINK_MSG_OUTBOX_MAX_PENDING", 5000))

# ──────────────────────────────────────────────
# Trust policy — ALWAYS ON (trusted-only by default)
# Per specification: seed-pairing is required before communication.
# ──────────────────────────────────────────────
TRUSTED_ONLY_PRIVATE_CHATS = True
TRUSTED_ONLY_TEXT = True
TRUSTED_ONLY_FILE = True
TRUSTED_ONLY_CALL = True

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def get_local_ip() -> str:
    """Get the LAN IP of this machine, preferring private network IPs."""
    candidates = []
    try:
        # Get all IPs from hostname
        hostname = socket.gethostname()
        ips = socket.gethostbyname_ex(hostname)[2]
        candidates.extend(ips)
    except Exception:
        pass

    # Try connecting to a public DNS server to get the local IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 53))  # Google DNS
        ip = s.getsockname()[0]
        s.close()
        if ip not in candidates and ip != "127.0.0.1":
            candidates.append(ip)
    except Exception:
        pass

    # Prefer private IPs
    private_prefixes = ("192.168.", "10.", "172.")
    for ip in candidates:
        if ip.startswith(private_prefixes) and ip != "127.0.0.1":
            return ip
    # Fallback to any non-loopback
    for ip in candidates:
        if ip != "127.0.0.1":
            return ip
    return "127.0.0.1"

LOCAL_IP = get_local_ip()
