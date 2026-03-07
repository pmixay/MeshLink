"""
MeshLink — Media Service (microservice module)

Re-exports the UDP media engine from core/media.py.
Provides real-time voice and video streaming with:

  - Low-latency UDP transport (20 ms audio chunks, 24 fps video)
  - CONTROL ping/pong for live RTT measurement even without audio/video
  - RTP-style jitter estimation and packet loss tracking
  - Sliding-window bitrate estimation
  - Jitter buffer for audio packet reordering
  - Video fragment reassembly for large JPEG frames
"""

from core.media import (  # noqa: F401
    MediaEngine,
    CallState,
    MEDIA_AUDIO,
    MEDIA_VIDEO,
    MEDIA_CONTROL,
)

__all__ = ["MediaEngine", "CallState", "MEDIA_AUDIO", "MEDIA_VIDEO", "MEDIA_CONTROL"]
