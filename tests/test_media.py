from core.media import MediaEngine


def test_seq_gap_wraparound():
    assert MediaEngine._seq_gap(10, 15) == 5
    assert MediaEngine._seq_gap(0xFFFFFFFE, 1) == 3


def test_stats_defaults_and_copy():
    m = MediaEngine()
    stats = m.get_stats()
    assert "latency_ms" in stats
    assert "loss_percent" in stats
    assert "jitter_ms" in stats
    assert "bitrate_kbps" in stats
    assert "uplink_latency_p50_ms" in stats
    assert "uplink_latency_p95_ms" in stats
    assert "downlink_jitter_p50_ms" in stats
    assert "downlink_jitter_p95_ms" in stats

