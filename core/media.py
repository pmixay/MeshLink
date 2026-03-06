"""
MeshLink — Media Streaming Engine (Voice & Video)
Uses UDP for low-latency audio/video transmission.
Audio: raw PCM → opus-like compression via audioop
Video: JPEG-compressed frames over UDP
"""

import io
import time
import struct
import socket
import logging
import threading
import collections
from enum import IntEnum
from typing import Callable, Optional

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
        self._bitrate_window = collections.deque()  # (recv_time, packet_size)
        self._bitrate_window_seconds = 2.0
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
        }

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
        self._bitrate_window.clear()
        logger.info(f"Call started → {peer_ip}:{peer_media_port}")
        if self.on_call_state_change:
            self.on_call_state_change(CallState.ACTIVE)

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
            except Exception as e:
                logger.debug(f"Video send error: {e}")
            offset += MAX_UDP_PAYLOAD

    def _send_control(self, data: bytes):
        if self._peer_addr:
            packet = self._make_packet(MEDIA_CONTROL, data)
            self._sock.sendto(packet, self._peer_addr)

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
                latency_now = max(0.0, float(now_ms - ts))
                if self._latency_ewma_ms <= 0.0:
                    self._latency_ewma_ms = latency_now
                else:
                    # Быстрый EWMA для визуально стабильного WebRTC-подобного графика.
                    self._latency_ewma_ms = (self._latency_ewma_ms * 0.8) + (latency_now * 0.2)
                self.stats["latency_ms"] = round(self._latency_ewma_ms, 2)

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
                elif self._bitrate_window:
                    self.stats["bitrate_kbps"] = round((self._bitrate_window[0][1] * 8.0) / 1000.0, 2)

            # Auto-accept incoming media as a call
            if self._call_state != CallState.ACTIVE and ptype in (MEDIA_AUDIO, MEDIA_VIDEO):
                self._peer_addr = addr
                self._call_state = CallState.ACTIVE
                if self.on_call_state_change:
                    self.on_call_state_change(CallState.ACTIVE)

            if ptype == MEDIA_AUDIO:
                if self.on_audio_frame:
                    self.on_audio_frame(payload, ts / 1000.0)

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
                    self._call_state = CallState.ENDED
                    self._peer_addr = None
                    if self.on_call_state_change:
                        self.on_call_state_change(CallState.ENDED)

    def get_stats(self) -> dict:
        with self._stats_lock:
            return dict(self.stats)
