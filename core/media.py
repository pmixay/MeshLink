"""
MeshLink — Media Streaming Engine (Voice & Video)
Uses UDP for low-latency audio/video transmission.
Audio: raw PCM → opus-like compression via audioop
Video: JPEG-compressed frames over UDP
"""

import time
import struct
import socket
import logging
import threading
import collections
from enum import IntEnum
from typing import Callable, Optional, Deque, Dict, Tuple

from .config import (
    NODE_ID, MEDIA_PORT,
    AUDIO_SAMPLE_RATE, AUDIO_CHANNELS,
    AUDIO_CHUNK_SAMPLES, VIDEO_FPS, VIDEO_QUALITY,
)

logger = logging.getLogger("meshlink.media")

# ── Media packet types ──────────────────────────────────
MEDIA_AUDIO = 0x01
MEDIA_VIDEO = 0x02
MEDIA_CONTROL = 0x03

# Packet format: [1B type][8B timestamp][4B seq][2B length][payload]
HEADER_FORMAT = "!BqIH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
MAX_UDP_PAYLOAD = 65000


class CallState(IntEnum):
    IDLE = 0
    RINGING = 1
    ACTIVE = 2
    ENDED = 3


class MediaEngine:
    """
    Handles UDP-based audio and video streaming for calls.
    Works with browser-based WebRTC or native clients.
    """

    def __init__(self):
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._call_state = CallState.IDLE
        self._peer_addr: Optional[tuple] = None  # (ip, port)
        self._seq_out = 0
        self._seq_in = 0
        self._last_recv_ts_ms = None
        self._last_recv_wall = None
        self._last_seq = None
        self._rx_expected_packets = 0
        self._rx_lost_packets = 0
        self._jitter_ewma_ms = 0.0
        self._latency_ewma_ms = 0.0
        self._tx_jitter_ewma_ms = 0.0
        self._tx_latency_ewma_ms = 0.0
        self._bitrate_window = collections.deque()  # (recv_time, packet_size)
        self._tx_bitrate_window = collections.deque()  # (send_time, packet_size)
        self._bitrate_window_seconds = 2.0
        self._percentile_window_seconds = 15.0

        self._down_latency_samples: Deque[Tuple[float, float]] = collections.deque()
        self._down_jitter_samples: Deque[Tuple[float, float]] = collections.deque()
        self._up_latency_samples: Deque[Tuple[float, float]] = collections.deque()
        self._up_jitter_samples: Deque[Tuple[float, float]] = collections.deque()

        self._last_send_wall: Optional[float] = None
        self._last_send_interval: Optional[float] = None

        # Audio receive path: jitter buffer + packet reordering
        self._audio_reorder: Dict[int, Tuple[bytes, int, float]] = {}
        self._audio_expected_seq: Optional[int] = None
        self._audio_jitter_buffer_ms = 40.0
        self._audio_max_wait_ms = 120.0
        self._audio_max_buffer_packets = 64
        self._stats_lock = threading.Lock()

        # Callbacks for received media
        self.on_audio_frame: Optional[Callable[[bytes, float], None]] = None
        self.on_video_frame: Optional[Callable[[bytes, float], None]] = None
        self.on_call_state_change: Optional[Callable[[CallState], None]] = None

        # Stats
        self.stats = {
            "packets_sent": 0,
            "packets_recv": 0,
            "bytes_sent": 0,
            "bytes_recv": 0,
            "latency_ms": 0,
            "loss_percent": 0.0,
            "jitter_ms": 0.0,
            "bitrate_kbps": 0.0,
            # Directional metrics (new, backward-compatible additions)
            "uplink_packets_sent": 0,
            "uplink_bytes_sent": 0,
            "uplink_latency_ms": 0.0,
            "uplink_latency_p50_ms": 0.0,
            "uplink_latency_p95_ms": 0.0,
            "uplink_jitter_ms": 0.0,
            "uplink_jitter_p50_ms": 0.0,
            "uplink_jitter_p95_ms": 0.0,
            "uplink_bitrate_kbps": 0.0,
            "downlink_packets_recv": 0,
            "downlink_bytes_recv": 0,
            "downlink_latency_ms": 0.0,
            "downlink_latency_p50_ms": 0.0,
            "downlink_latency_p95_ms": 0.0,
            "downlink_jitter_ms": 0.0,
            "downlink_jitter_p50_ms": 0.0,
            "downlink_jitter_p95_ms": 0.0,
            "downlink_bitrate_kbps": 0.0,
        }

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        vals = sorted(float(v) for v in values)
        if len(vals) == 1:
            return vals[0]
        idx = (len(vals) - 1) * max(0.0, min(1.0, p))
        lo = int(idx)
        hi = min(lo + 1, len(vals) - 1)
        frac = idx - lo
        return vals[lo] * (1.0 - frac) + vals[hi] * frac

    def _prune_samples(self, now: float, dq: Deque[Tuple[float, float]]):
        cutoff = now - self._percentile_window_seconds
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _refresh_percentiles_locked(self, now: float):
        self._prune_samples(now, self._down_latency_samples)
        self._prune_samples(now, self._down_jitter_samples)
        self._prune_samples(now, self._up_latency_samples)
        self._prune_samples(now, self._up_jitter_samples)

        dl = [v for _, v in self._down_latency_samples]
        dj = [v for _, v in self._down_jitter_samples]
        ul = [v for _, v in self._up_latency_samples]
        uj = [v for _, v in self._up_jitter_samples]

        self.stats["downlink_latency_p50_ms"] = round(self._percentile(dl, 0.50), 2)
        self.stats["downlink_latency_p95_ms"] = round(self._percentile(dl, 0.95), 2)
        self.stats["downlink_jitter_p50_ms"] = round(self._percentile(dj, 0.50), 2)
        self.stats["downlink_jitter_p95_ms"] = round(self._percentile(dj, 0.95), 2)
        self.stats["uplink_latency_p50_ms"] = round(self._percentile(ul, 0.50), 2)
        self.stats["uplink_latency_p95_ms"] = round(self._percentile(ul, 0.95), 2)
        self.stats["uplink_jitter_p50_ms"] = round(self._percentile(uj, 0.50), 2)
        self.stats["uplink_jitter_p95_ms"] = round(self._percentile(uj, 0.95), 2)

    def _record_uplink_stats_locked(self, packet_size: int):
        now = time.time()

        # Uplink latency in this UDP path is local enqueue-to-send (near-zero by design).
        latency_now = 0.0
        if self._tx_latency_ewma_ms <= 0.0:
            self._tx_latency_ewma_ms = latency_now
        else:
            self._tx_latency_ewma_ms = (self._tx_latency_ewma_ms * 0.8) + (latency_now * 0.2)

        jitter_now = 0.0
        if self._last_send_wall is not None:
            interval = max(0.0, now - self._last_send_wall)
            if self._last_send_interval is not None:
                jitter_now = abs((interval - self._last_send_interval) * 1000.0)
            self._last_send_interval = interval
        self._last_send_wall = now

        self._tx_jitter_ewma_ms = self._tx_jitter_ewma_ms + ((jitter_now - self._tx_jitter_ewma_ms) / 16.0)

        self._up_latency_samples.append((now, latency_now))
        self._up_jitter_samples.append((now, jitter_now))

        self._tx_bitrate_window.append((now, int(packet_size)))
        cutoff = now - self._bitrate_window_seconds
        while self._tx_bitrate_window and self._tx_bitrate_window[0][0] < cutoff:
            self._tx_bitrate_window.popleft()

        if len(self._tx_bitrate_window) >= 2:
            t0 = self._tx_bitrate_window[0][0]
            span = max(0.2, now - t0)
            bytes_in_window = sum(sz for _, sz in self._tx_bitrate_window)
            self.stats["uplink_bitrate_kbps"] = round((bytes_in_window * 8.0 / 1000.0) / span, 2)
        elif self._tx_bitrate_window:
            self.stats["uplink_bitrate_kbps"] = round((self._tx_bitrate_window[0][1] * 8.0) / 1000.0, 2)

        self.stats["uplink_latency_ms"] = round(self._tx_latency_ewma_ms, 2)
        self.stats["uplink_jitter_ms"] = round(self._tx_jitter_ewma_ms, 2)
        self._refresh_percentiles_locked(now)

    # ── Socket management ───────────────────────────────────

    def start(self):
        """Start the UDP media listener."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", MEDIA_PORT))
        self._sock.settimeout(0.5)
        self._running = True

        threading.Thread(
            target=self._recv_loop, daemon=True, name="media-recv"
        ).start()

        logger.info(f"Media engine listening on UDP port {MEDIA_PORT}")

    def stop(self):
        self._running = False
        self.end_call()
        if self._sock:
            self._sock.close()

    # ── Call management ─────────────────────────────────────

    def start_call(self, peer_ip: str, peer_media_port: int):
        """Begin a call to a peer."""
        self._peer_addr = (peer_ip, peer_media_port)
        self._call_state = CallState.ACTIVE
        self._seq_out = 0
        self._seq_in = 0
        with self._stats_lock:
            self.stats = {k: 0 for k in self.stats}
        self._last_recv_ts_ms = None
        self._last_recv_wall = None
        self._last_seq = None
        self._rx_expected_packets = 0
        self._rx_lost_packets = 0
        self._jitter_ewma_ms = 0.0
        self._latency_ewma_ms = 0.0
        self._tx_jitter_ewma_ms = 0.0
        self._tx_latency_ewma_ms = 0.0
        self._bitrate_window.clear()
        self._tx_bitrate_window.clear()
        self._down_latency_samples.clear()
        self._down_jitter_samples.clear()
        self._up_latency_samples.clear()
        self._up_jitter_samples.clear()
        self._last_send_wall = None
        self._last_send_interval = None
        self._audio_reorder.clear()
        self._audio_expected_seq = None
        # Ping send times keyed by seq number for RTT calculation
        self._ping_sent: dict = {}
        logger.info(f"Call started → {peer_ip}:{peer_media_port}")
        if self.on_call_state_change:
            self.on_call_state_change(CallState.ACTIVE)
        # Start CONTROL ping loop for live latency measurement
        threading.Thread(
            target=self._ping_loop, daemon=True, name="media-ping"
        ).start()

    def end_call(self):
        if self._call_state == CallState.ACTIVE:
            # Send end-of-call control packet
            if self._peer_addr:
                try:
                    self._send_control(b"END_CALL")
                except Exception:
                    pass
        self._call_state = CallState.ENDED
        self._peer_addr = None
        if self.on_call_state_change:
            self.on_call_state_change(CallState.ENDED)

    @property
    def in_call(self) -> bool:
        return self._call_state == CallState.ACTIVE

    # ── Sending ─────────────────────────────────────────────

    def _make_packet(self, ptype: int, payload: bytes) -> bytes:
        ts = int(time.time() * 1000)
        self._seq_out += 1
        header = struct.pack(HEADER_FORMAT, ptype, ts, self._seq_out, len(payload))
        return header + payload

    def send_audio(self, pcm_data: bytes):
        """Send an audio chunk to the connected peer."""
        if not self.in_call or not self._peer_addr:
            return
        packet = self._make_packet(MEDIA_AUDIO, pcm_data)
        try:
            self._sock.sendto(packet, self._peer_addr)
            with self._stats_lock:
                self.stats["packets_sent"] += 1
                self.stats["bytes_sent"] += len(packet)
                self.stats["uplink_packets_sent"] += 1
                self.stats["uplink_bytes_sent"] += len(packet)
                self._record_uplink_stats_locked(len(packet))
        except Exception as e:
            logger.debug(f"Audio send error: {e}")

    def send_video(self, jpeg_data: bytes):
        """Send a video frame (JPEG) to the connected peer.
        Fragments large frames into multiple UDP packets.
        """
        if not self.in_call or not self._peer_addr:
            return

        # Fragment if needed
        offset = 0
        frag_id = self._seq_out
        total_frags = (len(jpeg_data) + MAX_UDP_PAYLOAD - 1) // MAX_UDP_PAYLOAD

        while offset < len(jpeg_data):
            chunk = jpeg_data[offset:offset + MAX_UDP_PAYLOAD]
            # Embed fragment info in payload prefix
            frag_header = struct.pack("!IHH", frag_id, offset // MAX_UDP_PAYLOAD, total_frags)
            packet = self._make_packet(MEDIA_VIDEO, frag_header + chunk)
            try:
                self._sock.sendto(packet, self._peer_addr)
                with self._stats_lock:
                    self.stats["packets_sent"] += 1
                    self.stats["bytes_sent"] += len(packet)
                    self.stats["uplink_packets_sent"] += 1
                    self.stats["uplink_bytes_sent"] += len(packet)
                    self._record_uplink_stats_locked(len(packet))
            except Exception as e:
                logger.debug(f"Video send error: {e}")
            offset += MAX_UDP_PAYLOAD

    def _send_control(self, data: bytes):
        if self._peer_addr and self._sock:
            packet = self._make_packet(MEDIA_CONTROL, data)
            try:
                self._sock.sendto(packet, self._peer_addr)
                with self._stats_lock:
                    self.stats["packets_sent"] += 1
                    self.stats["bytes_sent"] += len(packet)
                    self.stats["uplink_packets_sent"] += 1
                    self.stats["uplink_bytes_sent"] += len(packet)
                    self._record_uplink_stats_locked(len(packet))
            except Exception:
                pass

    def _ping_loop(self):
        """
        Send CONTROL PING packets every 500 ms during an active call.
        On receiving a PONG the round-trip time is measured and used as
        the downlink_latency_ms metric, giving live latency even when
        there is no real audio/video stream.
        """
        PING_INTERVAL = 0.5  # seconds
        while self._running and self._call_state == CallState.ACTIVE:
            if self._peer_addr and self._sock:
                send_ts_ms = int(time.time() * 1000)
                seq = self._seq_out
                # Encode send timestamp in the ping payload so the pong can echo it
                ping_payload = b"PING" + struct.pack("!q", send_ts_ms)
                try:
                    pkt = self._make_packet(MEDIA_CONTROL, ping_payload)
                    self._sock.sendto(pkt, self._peer_addr)
                    if not hasattr(self, '_ping_sent'):
                        self._ping_sent = {}
                    self._ping_sent[seq] = time.time()
                    # Expire old pings
                    cutoff = time.time() - 10.0
                    self._ping_sent = {k: v for k, v in self._ping_sent.items()
                                       if v > cutoff}
                except Exception:
                    pass
            time.sleep(PING_INTERVAL)

    def _drain_audio_reorder(self, now_wall: float, force: bool = False):
        while True:
            if self._audio_expected_seq is None:
                if not self._audio_reorder:
                    return
                self._audio_expected_seq = min(self._audio_reorder.keys())

            cur = self._audio_reorder.get(int(self._audio_expected_seq))
            if cur is None:
                if not self._audio_reorder:
                    return
                oldest_wait = (now_wall - min(x[2] for x in self._audio_reorder.values())) * 1000.0
                if oldest_wait < self._audio_max_wait_ms and not force:
                    return
                # Missing expected sequence for too long: jump to earliest available.
                self._audio_expected_seq = min(self._audio_reorder.keys())
                cur = self._audio_reorder.get(int(self._audio_expected_seq))
                if cur is None:
                    return

            payload, ts, arrived = cur
            buffered_ms = (now_wall - arrived) * 1000.0
            if (buffered_ms < self._audio_jitter_buffer_ms) and not force and len(self._audio_reorder) < (self._audio_max_buffer_packets // 2):
                return

            del self._audio_reorder[int(self._audio_expected_seq)]
            self._audio_expected_seq = (int(self._audio_expected_seq) + 1) & 0xFFFFFFFF
            if self.on_audio_frame:
                self.on_audio_frame(payload, ts / 1000.0)

            # Hard bound for memory in pathological reorder/loss conditions.
            if len(self._audio_reorder) > self._audio_max_buffer_packets:
                while len(self._audio_reorder) > self._audio_max_buffer_packets:
                    k = min(self._audio_reorder.keys())
                    del self._audio_reorder[k]

    # ── Receiving ───────────────────────────────────────────

    @staticmethod
    def _seq_gap(prev_seq: int, cur_seq: int) -> int:
        """Return positive packet gap between previous and current 32-bit seq values."""
        if cur_seq >= prev_seq:
            return cur_seq - prev_seq
        return (0xFFFFFFFF - prev_seq) + cur_seq + 1

    def _recv_loop(self):
        """Main receive loop for media packets."""
        video_fragments: dict = {}

        while self._running:
            try:
                data, addr = self._sock.recvfrom(MAX_UDP_PAYLOAD + HEADER_SIZE + 100)
            except socket.timeout:
                continue
            except Exception:
                if self._running:
                    continue
                break

            if len(data) < HEADER_SIZE:
                continue

            ptype, ts, seq, plen = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
            payload = data[HEADER_SIZE:HEADER_SIZE + plen]

            now_ms = int(time.time() * 1000)
            now_wall = time.time()
            with self._stats_lock:
                self.stats["packets_recv"] += 1
                self.stats["bytes_recv"] += len(data)
                self.stats["downlink_packets_recv"] += 1
                self.stats["downlink_bytes_recv"] += len(data)
                latency_now = max(0.0, float(now_ms - ts))
                if self._latency_ewma_ms <= 0.0:
                    self._latency_ewma_ms = latency_now
                else:
                    # Быстрый EWMA для визуально стабильного WebRTC-подобного графика.
                    self._latency_ewma_ms = (self._latency_ewma_ms * 0.8) + (latency_now * 0.2)
                self.stats["latency_ms"] = round(self._latency_ewma_ms, 2)
                self.stats["downlink_latency_ms"] = round(self._latency_ewma_ms, 2)
                self._down_latency_samples.append((now_wall, latency_now))

                # Packet loss estimate using cumulative expected/received based on seq gaps.
                if self._last_seq is None:
                    self._rx_expected_packets += 1
                else:
                    gap = self._seq_gap(int(self._last_seq), int(seq))
                    if gap <= 0:
                        gap = 1
                    self._rx_expected_packets += gap
                    if gap > 1:
                        self._rx_lost_packets += (gap - 1)
                self._last_seq = seq
                if self._rx_expected_packets > 0:
                    loss = (self._rx_lost_packets / self._rx_expected_packets) * 100.0
                    self.stats["loss_percent"] = round(loss, 2)

                # RTP-style jitter approximation
                if self._last_recv_ts_ms is not None and self._last_recv_wall is not None:
                    sent_delta = max(0.0, (ts - self._last_recv_ts_ms) / 1000.0)
                    recv_delta = max(0.0, now_wall - self._last_recv_wall)
                    d_ms = abs((recv_delta - sent_delta) * 1000.0)
                    self._jitter_ewma_ms = self._jitter_ewma_ms + ((d_ms - self._jitter_ewma_ms) / 16.0)
                    self.stats["jitter_ms"] = round(self._jitter_ewma_ms, 2)
                    self.stats["downlink_jitter_ms"] = round(self._jitter_ewma_ms, 2)
                    self._down_jitter_samples.append((now_wall, d_ms))
                self._last_recv_ts_ms = ts
                self._last_recv_wall = now_wall

                # Sliding-window bitrate estimate (2s) closer to real RTC view.
                self._bitrate_window.append((now_wall, len(data)))
                cutoff = now_wall - self._bitrate_window_seconds
                while self._bitrate_window and self._bitrate_window[0][0] < cutoff:
                    self._bitrate_window.popleft()
                if len(self._bitrate_window) >= 2:
                    t0 = self._bitrate_window[0][0]
                    span = max(0.2, now_wall - t0)
                    bytes_in_window = sum(sz for _, sz in self._bitrate_window)
                    self.stats["bitrate_kbps"] = round((bytes_in_window * 8.0 / 1000.0) / span, 2)
                    self.stats["downlink_bitrate_kbps"] = self.stats["bitrate_kbps"]
                elif self._bitrate_window:
                    self.stats["bitrate_kbps"] = round((self._bitrate_window[0][1] * 8.0) / 1000.0, 2)
                    self.stats["downlink_bitrate_kbps"] = self.stats["bitrate_kbps"]

                self._refresh_percentiles_locked(now_wall)

            # Auto-accept incoming media as a call
            if self._call_state != CallState.ACTIVE and ptype in (MEDIA_AUDIO, MEDIA_VIDEO):
                self._peer_addr = addr
                self._call_state = CallState.ACTIVE
                if self.on_call_state_change:
                    self.on_call_state_change(CallState.ACTIVE)

            if ptype == MEDIA_AUDIO:
                self._audio_reorder[int(seq)] = (payload, ts, now_wall)
                if self._audio_expected_seq is None:
                    self._audio_expected_seq = int(seq)
                self._drain_audio_reorder(now_wall)

            elif ptype == MEDIA_VIDEO:
                # Reassemble fragments
                if len(payload) >= 8:
                    frag_id, frag_idx, total_frags = struct.unpack("!IHH", payload[:8])
                    frag_data = payload[8:]

                    if frag_id not in video_fragments:
                        video_fragments[frag_id] = {}
                    video_fragments[frag_id][frag_idx] = frag_data

                    if len(video_fragments[frag_id]) == total_frags:
                        # Reassemble
                        full_frame = b""
                        for i in range(total_frags):
                            full_frame += video_fragments[frag_id].get(i, b"")
                        del video_fragments[frag_id]

                        if self.on_video_frame:
                            self.on_video_frame(full_frame, ts / 1000.0)

                    # Cleanup old fragments
                    old = [k for k in video_fragments if k < frag_id - 30]
                    for k in old:
                        del video_fragments[k]

            elif ptype == MEDIA_CONTROL:
                if payload == b"END_CALL":
                    self._drain_audio_reorder(now_wall, force=True)
                    self._call_state = CallState.ENDED
                    self._peer_addr = None
                    if self.on_call_state_change:
                        self.on_call_state_change(CallState.ENDED)
                elif payload[:4] == b"PING" and len(payload) >= 12:
                    # Echo back a PONG with the original timestamp so sender can calc RTT
                    pong_payload = b"PONG" + payload[4:]
                    try:
                        if self._peer_addr and self._sock:
                            pkt = self._make_packet(MEDIA_CONTROL, pong_payload)
                            self._sock.sendto(pkt, self._peer_addr)
                    except Exception:
                        pass
                elif payload[:4] == b"PONG" and len(payload) >= 12:
                    # Measure RTT from echoed timestamp
                    try:
                        sent_ts_ms = struct.unpack("!q", payload[4:12])[0]
                        rtt_ms = max(0.0, float(int(time.time() * 1000) - sent_ts_ms))
                        # One-way latency estimate = RTT / 2
                        owd_ms = rtt_ms / 2.0
                        with self._stats_lock:
                            if self._latency_ewma_ms <= 0.0:
                                self._latency_ewma_ms = owd_ms
                            else:
                                self._latency_ewma_ms = (
                                    self._latency_ewma_ms * 0.7 + owd_ms * 0.3
                                )
                            self.stats["latency_ms"] = round(self._latency_ewma_ms, 2)
                            self.stats["downlink_latency_ms"] = round(self._latency_ewma_ms, 2)
                            self._down_latency_samples.append((now_wall, owd_ms))
                            self._refresh_percentiles_locked(now_wall)
                    except Exception:
                        pass

    def get_stats(self) -> dict:
        with self._stats_lock:
            return dict(self.stats)
