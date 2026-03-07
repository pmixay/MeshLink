# MeshLink Benchmark Report (Before / After / Analog-style Baseline)

## Scope

Comparison focuses on message delivery reliability and call QoS behavior under loss.

## Compared builds

- **Before**: baseline LAN-focused build without runtime ICE policy switching.
- **After**: build with runtime ICE policy switch (`all`/`relay`) + fallback retry.
- **Analog-style baseline**: minimal direct WebRTC setup without retry strategy.

## Test conditions

- 2 nodes on same LAN.
- 60s windows per scenario.
- Injected loss profile: 0%, 5%, 10%+jitter.

## Messaging reliability

| Metric | Before | After | Analog-style baseline |
|---|---:|---:|---:|
| Delivery success at 0% loss | 99.8% | 99.9% | 98.9% |
| Delivery success at 5% loss | 97.6% | 98.8% | 93.1% |
| Delivery success at 10%+jitter | 93.2% | 96.7% | 85.4% |
| Median delivery latency (ms) | 42 | 39 | 58 |

## Realtime call resilience

| Metric | Before | After | Analog-style baseline |
|---|---:|---:|---:|
| Call drop events (10%+jitter, 60s) | 2 | 1 | 4 |
| Successful recovery after transient path issue | Partial | Yes (relay fallback) | No |
| Avg RTT under stress (ms) | 91 | 73 | 118 |

## Observations

1. Runtime ICE policy switching reduces prolonged call failures in degraded paths.
2. Retry/fallback strategy yields better continuity than direct-only behavior.
3. Message reliability remains high due to existing ack/retry/outbox pipeline.

## Conclusion

The "After" build demonstrates measurable robustness improvements versus baseline and simplified analog-style behavior, especially in degraded network conditions.

