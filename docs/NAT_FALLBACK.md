# NAT / STUN-TURN Fallback Design

## Scope

This document defines fallback architecture for environments where direct peer connectivity is unavailable.

## Baseline

- Current implementation targets LAN mesh.
- WebRTC uses public STUN server in browser config.

## Proposed fallback layers

1. **Direct P2P (preferred)**
   - LAN direct routing.
2. **STUN-assisted traversal**
   - Discover reflexive candidates.
3. **TURN relay fallback**
   - Force relay candidates when direct path fails.
4. **Application relay (future extension)**
   - Mesh relay for signaling and (optionally) media proxy in constrained setups.

## Config proposal

Environment variables (to be used by frontend config endpoint):

- `MESHLINK_WEBRTC_STUN` (comma-separated URLs)
- `MESHLINK_WEBRTC_TURN_URL`
- `MESHLINK_WEBRTC_TURN_USER`
- `MESHLINK_WEBRTC_TURN_PASS`
- `MESHLINK_WEBRTC_ICE_POLICY` (`all` or `relay`)

## Candidate selection policy

- Start with `iceTransportPolicy=all`.
- If repeated failures are detected, switch to `relay` and retry.

## Security notes

- TURN credentials must not be hardcoded.
- Rotate TURN credentials periodically.
- Apply rate-limits on relay resources.

## Verification checklist

- Call succeeds with symmetric NAT via TURN.
- Metrics remain visible under relay mode.
- UI indicates relay usage and quality impact.

