# MeshLink — Decentralized P2P Communication System

**No Internet. No Servers. Just the Mesh.**

MeshLink is a fully decentralized communication platform that works entirely over your local network (LAN). It provides instant messaging, file transfers, and voice/video call signaling — all without any internet connection or central server.

---

## Features

- **Zero-config P2P Discovery** — Automatic peer detection via UDP broadcast + multicast
- **Encrypted Messaging** — End-to-end encryption using X25519 key exchange + AES-256-GCM
- **File Transfer** — Chunked file streaming with SHA-256 integrity verification (up to 2 GB)
- **Voice & Video Call Signaling** — UDP-based media engine with packet fragmentation
- **Modern Web UI** — Beautiful dark-themed interface with real-time updates via WebSocket
- **Cross-Platform** — Runs on Windows, macOS, and Linux (Python 3.9+)

---

## Quick Start

### 1. Install dependencies

```bash
cd meshlink
pip install -r requirements.txt
```

### 2. Run MeshLink

```bash
python main.py
```

The web UI will automatically open at `http://localhost:8080`.

### 3. Run on another machine

On a second computer on the same LAN:

```bash
python main.py --name "Bob" --web-port 8081
```

Peers will discover each other automatically within seconds.

---

## CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `--name`, `-n` | Display name | System hostname |
| `--web-port`, `-w` | Web UI port | 8080 |
| `--tcp-port`, `-t` | TCP messaging port | 5151 |
| `--media-port`, `-m` | UDP media port | 5152 |
| `--discovery-port`, `-d` | Discovery broadcast port | 5150 |
| `--no-browser` | Don't auto-open browser | false |
| `--verbose`, `-v` | Debug logging | false |

### Environment Variables

All options can also be set via environment:

```bash
export MESHLINK_NODE_NAME="Alice"
export MESHLINK_WEB_PORT=9090
export MESHLINK_TCP_PORT=5151
export MESHLINK_MEDIA_PORT=5152
export MESHLINK_DISCOVERY_PORT=5150
export MESHLINK_DOWNLOADS="~/Downloads/MeshLink"
```

---

## Architecture

```
meshlink/
├── main.py                 # Entry point & CLI
├── requirements.txt        # Python dependencies
├── setup.py               # pip installable package
├── core/
│   ├── config.py          # Configuration & constants
│   ├── crypto.py          # E2E encryption (X25519 + AES-256-GCM)
│   ├── discovery.py       # UDP broadcast peer discovery
│   ├── messaging.py       # TCP messaging protocol
│   ├── file_transfer.py   # Chunked file transfer engine
│   ├── media.py           # UDP voice/video streaming engine
│   └── node.py            # Central orchestrator
├── web/
│   └── server.py          # Flask + SocketIO web server
└── templates/
    └── index.html         # Web UI (single-page app)
```

### Protocol Stack

```
┌──────────────────────────────────────────────┐
│              Web UI (Browser)                │
│          Flask + Socket.IO (WebSocket)       │
├──────────────────────────────────────────────┤
│              MeshNode Orchestrator           │
├───────────┬──────────────┬───────────────────┤
│ Discovery │  Messaging   │   Media Engine    │
│   (UDP    │   (TCP)      │    (UDP)          │
│ broadcast)│              │                   │
├───────────┼──────────────┼───────────────────┤
│           │  File Xfer   │  Audio / Video    │
│           │  (chunked)   │  (fragmented)     │
├───────────┴──────────────┴───────────────────┤
│         E2E Encryption (X25519 + AES)        │
├──────────────────────────────────────────────┤
│           LAN (WiFi / Ethernet)              │
└──────────────────────────────────────────────┘
```

---

## Network Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 5150 | UDP | Peer discovery (broadcast + multicast) |
| 5151 | TCP | Messaging & file transfer |
| 5152 | UDP | Voice/video media streaming |
| 8080 | TCP | Web UI (HTTP + WebSocket) |

> **Firewall**: If peers can't find each other, ensure these ports are open for LAN traffic.

---

## Security

- **Key Exchange**: X25519 Elliptic-Curve Diffie-Hellman
- **Message Encryption**: AES-256-GCM with per-message nonce
- **File Integrity**: SHA-256 checksums
- **No Data Leaves LAN**: All traffic stays on your local network

---

## Running Multiple Instances (Testing)

You can run multiple instances on the same machine for testing:

```bash
# Terminal 1
python main.py --name "Alice" --web-port 8080 --tcp-port 5151 --media-port 5152

# Terminal 2
python main.py --name "Bob" --web-port 8081 --tcp-port 5161 --media-port 5162

# Terminal 3
python main.py --name "Charlie" --web-port 8082 --tcp-port 5171 --media-port 5172
```

---

## License

MIT License — Use freely, modify freely, share freely.
