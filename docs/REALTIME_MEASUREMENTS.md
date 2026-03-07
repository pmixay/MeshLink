# Realtime QoS Measurement Methodology

## Purpose

Define a reproducible method to measure call quality for Hex.Team defense:

- latency (RTT),
- packet loss,
- jitter,
- bitrate,
- behavior under induced losses.

## Instrumentation in MeshLink

- Source: browser `RTCPeerConnection.getStats()`.
- Sampling period: 1 second.
- Smoothing: EMA with `alpha = 0.3`.
- Displayed metrics: latency, loss, jitter, bitrate.

## Test setup

1. Start 2 nodes in same LAN.
2. Pair peers with seed pairing.
3. Start audio call.
4. Keep call for at least 60s.
5. Collect metrics every second.

## Loss injection scenarios

Use OS/network tooling (e.g., `tc/netem`, clumsy, router QoS) to emulate:

- Baseline: no induced loss.
- Scenario A: 5% packet loss for 60s.
- Scenario B: 10% packet loss + jitter burst.
- Scenario C: relay degradation / path interruption (if routing path allows).

## Result table template

| Scenario | Avg RTT (ms) | Avg Loss (%) | Avg Jitter (ms) | Avg Bitrate (kbps) | Disconnects | Notes |
|---|---:|---:|---:|---:|---:|---|
| Baseline |  |  |  |  |  |  |
| A: 5% loss |  |  |  |  |  |  |
| B: 10% + jitter |  |  |  |  |  |  |
| C: relay disruption |  |  |  |  |  |  |

## Acceptance criteria for demo

- UI remains interactive.
- Metrics react to induced degradation.
- Recovery trend visible after loss is removed.
- Call does not crash under moderate loss.

