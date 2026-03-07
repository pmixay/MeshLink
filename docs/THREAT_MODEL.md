# MeshLink Threat Model (Short)

## Assets

- message confidentiality/integrity,
- peer identity and trust state,
- file integrity,
- call availability.

## Trust boundaries

1. Browser/UI boundary (untrusted input from network).
2. Signaling/messaging transport boundary.
3. Local storage boundary (SQLite, local files).
4. Peer trust boundary (paired vs unpaired nodes).

## Threats and controls

### 1) Message tampering / spoofing
- Threat: attacker injects modified payloads.
- Controls: Ed25519 signature verification, trusted-only policy.

### 2) Passive interception
- Threat: eavesdropping on traffic.
- Controls: X25519 session + AES-256-GCM encryption.

### 3) Replay / loop amplification
- Threat: replayed or looping relay packets.
- Controls: `msg_id` dedup, TTL decrement, relay path checks.

### 4) Spam / flood
- Threat: excessive message rate degrades node.
- Controls: rate limiting, temporary bans, blacklist, outbox and relay backpressure.

### 5) File corruption
- Threat: damaged or truncated payload.
- Controls: SHA-256 verification, resume with offset hash validation.

## Residual risks

- No full PKI chain of trust.
- TURN/NAT fallback requires careful credential lifecycle management.
- Compromised trusted peer remains a high-impact risk.

