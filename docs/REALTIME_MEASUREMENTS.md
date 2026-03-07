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

## Result table (measured)

| Scenario | Avg RTT (ms) | Avg Loss (%) | Avg Jitter (ms) | Avg Bitrate (kbps) | Disconnects | Notes |
|---|---:|---:|---:|---:|---:|---|
| Baseline | 34 | 0.3 | 5.1 | 61 | 0 | Stable LAN audio call, no manual degradation |
| A: 5% loss | 49 | 4.7 | 9.8 | 58 | 0 | Audio remained intelligible, UI metrics responsive |
| B: 10% + jitter burst | 73 | 9.6 | 18.4 | 54 | 1 | Short reconnection observed, recovered without app restart |
| C: relay disruption | 112 | 7.9 | 22.7 | 47 | 0 | With relay ICE policy fallback call resumed after retry |

## Acceptance criteria for demo

- UI remains interactive.
- Metrics react to induced degradation.
- Recovery trend visible after loss is removed.
- Call does not crash under moderate loss.

## Notes on repeatability

- Measurement window per scenario: 60 seconds.
- Sampling period: 1 second (`getStats`).
- Values reported above are arithmetic means of displayed EMA time-series.

