"""
MeshLink — Web Interface Server (v2)
Flask + Socket.IO with WebRTC signaling relay.
"""

import os
import json
import time
import logging
import shutil
import tempfile
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

try:
    from ..core.node import MeshNode
    from ..core.config import WEB_PORT, NODE_ID, NODE_NAME, LOCAL_IP, DOWNLOADS_DIR
except ImportError:
    from core.node import MeshNode
    from core.config import WEB_PORT, NODE_ID, NODE_NAME, LOCAL_IP, DOWNLOADS_DIR

logger = logging.getLogger("meshlink.web")


def _prometheus_text(metrics: dict) -> str:
    lines = [
        "# HELP meshlink_active_peers Number of currently active peers.",
        "# TYPE meshlink_active_peers gauge",
        f"meshlink_active_peers {float(metrics.get('active_peers', 0))}",
        "# HELP meshlink_outbox_pending Pending outbox messages.",
        "# TYPE meshlink_outbox_pending gauge",
        f"meshlink_outbox_pending {float(metrics.get('outbox_pending', 0))}",
        "# HELP meshlink_relay_pending Pending relay forwards in-process.",
        "# TYPE meshlink_relay_pending gauge",
        f"meshlink_relay_pending {float(metrics.get('relay_pending', 0))}",
        "# HELP meshlink_relay_drops_total Total relay drops due to backpressure.",
        "# TYPE meshlink_relay_drops_total counter",
        f"meshlink_relay_drops_total {float(metrics.get('relay_drops_total', 0))}",
        "# HELP meshlink_delivery_retry_total Delivery retries.",
        "# TYPE meshlink_delivery_retry_total counter",
        f"meshlink_delivery_retry_total {float(metrics.get('delivery_retry_total', 0))}",
        "# HELP meshlink_delivery_fail_total Delivery failures.",
        "# TYPE meshlink_delivery_fail_total counter",
        f"meshlink_delivery_fail_total {float(metrics.get('delivery_fail_total', 0))}",
        "# HELP meshlink_delivery_latency_sum_seconds Sum of delivery latencies.",
        "# TYPE meshlink_delivery_latency_sum_seconds counter",
        f"meshlink_delivery_latency_sum_seconds {float(metrics.get('delivery_latency_sum_seconds', 0.0))}",
        "# HELP meshlink_delivery_latency_count Count of measured delivery latencies.",
        "# TYPE meshlink_delivery_latency_count counter",
        f"meshlink_delivery_latency_count {float(metrics.get('delivery_latency_count', 0.0))}",
        "# HELP meshlink_file_resume_total Number of resumed file transfers.",
        "# TYPE meshlink_file_resume_total counter",
        f"meshlink_file_resume_total {float(metrics.get('file_resume_total', 0.0))}",
        "# HELP meshlink_session_rotations_total Total cryptographic session rotations.",
        "# TYPE meshlink_session_rotations_total counter",
        f"meshlink_session_rotations_total {float(metrics.get('session_rotations_total', 0))}",
        "# HELP meshlink_session_expired_total Total expired sessions.",
        "# TYPE meshlink_session_expired_total counter",
        f"meshlink_session_expired_total {float(metrics.get('session_expired_total', 0))}",
        "# HELP meshlink_trusted_policy_incoming_text_drops_total Trusted-policy incoming text drops.",
        "# TYPE meshlink_trusted_policy_incoming_text_drops_total counter",
        f"meshlink_trusted_policy_incoming_text_drops_total {float(metrics.get('trusted_policy_incoming_text_drops_total', 0))}",
        "# HELP meshlink_trusted_policy_incoming_file_drops_total Trusted-policy incoming file drops.",
        "# TYPE meshlink_trusted_policy_incoming_file_drops_total counter",
        f"meshlink_trusted_policy_incoming_file_drops_total {float(metrics.get('trusted_policy_incoming_file_drops_total', 0))}",
        "# HELP meshlink_trusted_policy_incoming_call_drops_total Trusted-policy incoming call drops.",
        "# TYPE meshlink_trusted_policy_incoming_call_drops_total counter",
        f"meshlink_trusted_policy_incoming_call_drops_total {float(metrics.get('trusted_policy_incoming_call_drops_total', 0))}",
        "# HELP meshlink_trusted_policy_outgoing_text_blocks_total Trusted-policy outgoing text blocks.",
        "# TYPE meshlink_trusted_policy_outgoing_text_blocks_total counter",
        f"meshlink_trusted_policy_outgoing_text_blocks_total {float(metrics.get('trusted_policy_outgoing_text_blocks_total', 0))}",
        "# HELP meshlink_trusted_policy_outgoing_file_blocks_total Trusted-policy outgoing file blocks.",
        "# TYPE meshlink_trusted_policy_outgoing_file_blocks_total counter",
        f"meshlink_trusted_policy_outgoing_file_blocks_total {float(metrics.get('trusted_policy_outgoing_file_blocks_total', 0))}",
        "# HELP meshlink_trusted_policy_outgoing_call_blocks_total Trusted-policy outgoing call blocks.",
        "# TYPE meshlink_trusted_policy_outgoing_call_blocks_total counter",
        f"meshlink_trusted_policy_outgoing_call_blocks_total {float(metrics.get('trusted_policy_outgoing_call_blocks_total', 0))}",
        "# HELP meshlink_security_events_total Total security events emitted.",
        "# TYPE meshlink_security_events_total counter",
        f"meshlink_security_events_total {float(metrics.get('security_events_total', 0))}",
    ]
    return "\n".join(lines) + "\n"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.config["SECRET_KEY"] = os.urandom(24).hex()
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2GB upload

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    max_http_buffer_size=100 * 1024 * 1024)

node: MeshNode = None  # type: ignore


def init_app(mesh_node: MeshNode):
    global node
    node = mesh_node

    # Standard events → SocketIO
    node.on("peer_joined", lambda d: socketio.emit("peer_joined", d))
    node.on("peer_left", lambda d: socketio.emit("peer_left", d))
    node.on("message", lambda d: socketio.emit("message", d))
    node.on("typing", lambda d: socketio.emit("typing", d))
    node.on("call_incoming", lambda d: socketio.emit("call_incoming", d))
    node.on("call_outgoing", lambda d: socketio.emit("call_outgoing", d))
    node.on("call_accepted", lambda d: socketio.emit("call_accepted", d))
    node.on("call_rejected", lambda d: socketio.emit("call_rejected", d))
    node.on("call_ended", lambda d: socketio.emit("call_ended", d))
    node.on("file_progress", lambda d: socketio.emit("file_progress", d))
    node.on("file_complete", lambda d: socketio.emit("file_complete", d))
    node.on("message_status", lambda d: socketio.emit("message_status", d))
    node.on("media_stats", lambda d: socketio.emit("media_stats", d))
    node.on("security_event", lambda d: socketio.emit("security_event", d))
    node.on("network_diagnostics", lambda d: socketio.emit("network_diagnostics", d))

    # WebRTC signaling relay: TCP peer → local browser
    node.on("webrtc_offer",  lambda d: socketio.emit("webrtc_offer", d))
    node.on("webrtc_answer", lambda d: socketio.emit("webrtc_answer", d))
    node.on("webrtc_ice",    lambda d: socketio.emit("webrtc_ice", d))

    # Security events
    node.on("seed_paired", lambda d: socketio.emit("seed_paired", d))


# ── Routes ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info")
def api_info():
    return jsonify(node.get_info())


@app.route("/api/peers")
def api_peers():
    return jsonify(node.get_peers())


@app.route("/api/chat/<peer_id>")
def api_chat(peer_id):
    return jsonify(node.get_chat(peer_id))


@app.route("/api/transfers")
def api_transfers():
    return jsonify(node.get_transfers())


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    peer_id = request.form.get("peer_id", "")
    if not peer_id:
        return jsonify({"error": "No peer_id"}), 400

    f = request.files["file"]
    # secure_filename prevents path traversal attacks (e.g. "../../etc/passwd")
    safe_name = secure_filename(f.filename or "upload")
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400

    # Stage uploaded file inside DOWNLOADS_DIR so it is accessible by the
    # asynchronous send_worker thread after this request returns.
    # We use a unique sub-directory to avoid name collisions.
    stage_dir = os.path.join(DOWNLOADS_DIR, ".upload_stage")
    os.makedirs(stage_dir, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="meshlink_up_", dir=stage_dir)
    tmp_path = os.path.join(tmp_dir, safe_name)

    try:
        f.save(tmp_path)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"Failed to save upload: {e}"}), 500

    file_id = node.send_file(peer_id, tmp_path)
    if not file_id:
        # Transfer rejected before even starting (peer unknown, policy block, etc.).
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": "Transfer failed"}), 500

    # Schedule temp dir cleanup after the transfer finishes.
    # We wrap the existing on_complete so we don't lose the original callback.
    _orig_on_complete = node.file_mgr.on_complete

    def _on_complete_with_cleanup(transfer):
        if _orig_on_complete:
            try:
                _orig_on_complete(transfer)
            except Exception:
                pass
        if transfer.file_id == file_id:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            # Restore original callback now that this upload's cleanup is done.
            node.file_mgr.on_complete = _orig_on_complete

    node.file_mgr.on_complete = _on_complete_with_cleanup
    return jsonify({"file_id": file_id, "filename": safe_name})


@app.route("/downloads/<path:filename>")
def download_file(filename):
    return send_from_directory(DOWNLOADS_DIR, filename)


@app.route("/health")
def health():
    if node is None:
        return jsonify({"status": "degraded", "reason": "node_not_initialized"}), 503
    return jsonify(node.get_health_snapshot())


@app.route("/ready")
def ready():
    if node is None:
        return jsonify({"ready": False, "reason": "node_not_initialized"}), 503
    ok = node.is_ready()
    return jsonify({"ready": ok, "active_peers": len(node.get_peers())}), (200 if ok else 503)


@app.route("/metrics")
def metrics():
    if node is None:
        body = "meshlink_ready 0\n"
        return app.response_class(body, mimetype="text/plain; version=0.0.4; charset=utf-8", status=503)
    metrics_snapshot = node.get_metrics_snapshot()
    body = _prometheus_text(metrics_snapshot)
    return app.response_class(body, mimetype="text/plain; version=0.0.4; charset=utf-8")


# ── Security / Seed-pairing / Blacklist API ──────────────

@app.route("/api/seed/generate", methods=["POST"])
def api_seed_generate():
    """Generate a fresh 6-char pairing seed to show to the user."""
    seed = node.generate_pairing_seed()
    return jsonify({"seed": seed})


@app.route("/api/seed/pair", methods=["POST"])
def api_seed_pair():
    """Activate seed-pairing with a specific peer."""
    data    = request.get_json(force=True)
    peer_id = data.get("peer_id", "")
    seed    = data.get("seed", "").strip().upper()
    if not peer_id or not seed:
        return jsonify({"error": "peer_id and seed required"}), 400
    ok = node.pair_with_seed(peer_id, seed)
    if ok:
        return jsonify({"status": "paired", "peer_id": peer_id})
    return jsonify({"error": "Pairing failed (invalid seed or unknown peer)"}), 400


@app.route("/api/security/blacklist", methods=["GET"])
def api_blacklist_get():
    return jsonify({"blacklist": node.get_blacklist()})


@app.route("/api/security/blacklist", methods=["POST"])
def api_blacklist_add():
    data    = request.get_json(force=True)
    peer_id = data.get("peer_id", "")
    if not peer_id:
        return jsonify({"error": "peer_id required"}), 400
    node.blacklist_peer(peer_id)
    return jsonify({"status": "blacklisted", "peer_id": peer_id})


@app.route("/api/security/blacklist/<peer_id>", methods=["DELETE"])
def api_blacklist_remove(peer_id):
    node.unblacklist_peer(peer_id)
    return jsonify({"status": "removed", "peer_id": peer_id})


@app.route("/api/security/banned")
def api_banned():
    """Returns peers currently under temporary rate-limit ban."""
    return jsonify({"banned": node.get_banned_peers()})


@app.route("/api/security/events")
def api_security_events():
    limit_raw = request.args.get("limit", "200")
    try:
        limit = int(limit_raw)
    except Exception:
        limit = 200
    return jsonify({"events": node.get_security_events(limit)})


@app.route("/api/security/snapshot")
def api_security_snapshot():
    return jsonify(node.get_security_snapshot())


@app.route("/api/network/diagnostics")
def api_network_diagnostics():
    return jsonify(node.get_network_diagnostics())


# ── Socket.IO events ────────────────────────────────────

@socketio.on("connect")
def on_connect():
    emit("node_info", node.get_info())
    emit("peers_list", node.get_peers())
    emit("network_diagnostics", node.get_network_diagnostics())


@socketio.on("send_message")
def on_send_message(data):
    peer_id = data.get("peer_id", "")
    text = data.get("text", "")
    if peer_id and text:
        result = node.send_text(peer_id, text)
        if result:
            emit("message_sent", result)
        else:
            emit("error", {"message": "Failed to send (peer unavailable, trust policy, or transport failure)"})


@socketio.on("typing")
def on_typing(data):
    peer_id = data.get("peer_id", "")
    if peer_id:
        node.send_typing(peer_id)


@socketio.on("get_peers")
def on_get_peers():
    emit("peers_list", node.get_peers())


@socketio.on("get_chat")
def on_get_chat(data):
    peer_id = data.get("peer_id", "")
    if peer_id:
        emit("chat_history", {"peer_id": peer_id, "messages": node.get_chat(peer_id)})


# ── Call signaling ──────────────────────────────────────

@socketio.on("start_call")
def on_start_call(data):
    peer_id = data.get("peer_id", "")
    call_type = data.get("call_type", "audio")
    if peer_id:
        success = node.start_call(peer_id, call_type)
        if not success:
            emit("error", {"message": "Call failed"})


@socketio.on("accept_call")
def on_accept_call(data):
    peer_id = data.get("peer_id", "")
    if peer_id:
        node.accept_call(peer_id)


@socketio.on("reject_call")
def on_reject_call(data):
    peer_id = data.get("peer_id", "")
    if peer_id:
        node.reject_call(peer_id)


@socketio.on("end_call")
def on_end_call(data=None):
    node.end_call()


# ── WebRTC signaling: browser → TCP peer ────────────────

@socketio.on("seed_pair")
def on_seed_pair(data):
    peer_id = data.get("peer_id", "")
    seed    = data.get("seed", "").strip().upper()
    if peer_id and seed:
        ok = node.pair_with_seed(peer_id, seed)
        emit("seed_pair_result", {"ok": ok, "peer_id": peer_id})


@socketio.on("blacklist_peer")
def on_blacklist_peer(data):
    peer_id = data.get("peer_id", "")
    action  = data.get("action", "add")   # "add" | "remove"
    if not peer_id:
        return
    if action == "remove":
        node.unblacklist_peer(peer_id)
    else:
        node.blacklist_peer(peer_id)
    emit("blacklist_updated", {"peer_id": peer_id, "action": action})


# ── WebRTC signaling: browser → TCP peer ────────────────

@socketio.on("webrtc_offer")
def on_webrtc_offer(data):
    peer_id = data.get("peer_id", "")
    if peer_id:
        node.send_webrtc_signal(peer_id, "offer", {
            "sdp": data.get("sdp", {}),
            "call_type": data.get("call_type", "audio"),
        })


@socketio.on("webrtc_answer")
def on_webrtc_answer(data):
    peer_id = data.get("peer_id", "")
    if peer_id:
        node.send_webrtc_signal(peer_id, "answer", {
            "sdp": data.get("sdp", {}),
        })


@socketio.on("webrtc_ice")
def on_webrtc_ice(data):
    peer_id = data.get("peer_id", "")
    if peer_id:
        node.send_webrtc_signal(peer_id, "ice", {
            "candidate": data.get("candidate"),
        })


def run_server():
    logger.info(f"Web UI: http://{LOCAL_IP}:{WEB_PORT}")
    socketio.run(
        app, host="0.0.0.0", port=WEB_PORT,
        debug=False, allow_unsafe_werkzeug=True, use_reloader=False,
    )
