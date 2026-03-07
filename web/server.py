"""
MeshLink — Web Interface Server
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
    from ..core.config import (
        WEB_PORT, NODE_ID, NODE_NAME, LOCAL_IP, DOWNLOADS_DIR,
        WEBRTC_STUN, WEBRTC_TURN_URL, WEBRTC_TURN_USER, WEBRTC_TURN_PASS, WEBRTC_ICE_POLICY,
    )
except ImportError:
    from core.node import MeshNode
    from core.config import (
        WEB_PORT, NODE_ID, NODE_NAME, LOCAL_IP, DOWNLOADS_DIR,
        WEBRTC_STUN, WEBRTC_TURN_URL, WEBRTC_TURN_USER, WEBRTC_TURN_PASS, WEBRTC_ICE_POLICY,
    )

logger = logging.getLogger("meshlink.web")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.config["SECRET_KEY"] = os.urandom(24).hex()
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    max_http_buffer_size=100 * 1024 * 1024)

node: MeshNode = None  # type: ignore


def init_app(mesh_node: MeshNode):
    global node
    node = mesh_node

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
    node.on("statistics", lambda d: socketio.emit("statistics", d))
    node.on("security_event", lambda d: socketio.emit("security_event", d))

    node.on("webrtc_offer",  lambda d: socketio.emit("webrtc_offer", d))
    node.on("webrtc_answer", lambda d: socketio.emit("webrtc_answer", d))
    node.on("webrtc_ice",    lambda d: socketio.emit("webrtc_ice", d))
    node.on("seed_paired", lambda d: socketio.emit("seed_paired", d))
    node.on("seed_pair_result", lambda d: socketio.emit("seed_pair_result", d))
    node.on("group_created", lambda d: socketio.emit("group_created", d))
    node.on("group_updated", lambda d: socketio.emit("group_updated", d))
    node.on("group_message", lambda d: socketio.emit("group_message", d))


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

@app.route("/api/groups")
def api_groups():
    return jsonify(node.get_groups())

@app.route("/api/statistics")
def api_statistics():
    return jsonify(node.get_statistics())

@app.route("/api/webrtc/config")
def api_webrtc_config():
    ice_servers = []
    for stun in WEBRTC_STUN:
        ice_servers.append({"urls": stun})
    if WEBRTC_TURN_URL:
        turn = {"urls": WEBRTC_TURN_URL}
        if WEBRTC_TURN_USER:
            turn["username"] = WEBRTC_TURN_USER
        if WEBRTC_TURN_PASS:
            turn["credential"] = WEBRTC_TURN_PASS
        ice_servers.append(turn)
    return jsonify({
        "iceServers": ice_servers,
        "iceTransportPolicy": WEBRTC_ICE_POLICY,
        "fallbackPolicy": {
            "enabled": bool(WEBRTC_TURN_URL),
            "retryRelayOnFailure": True,
            "maxFallbackAttempts": 1,
        },
    })

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    peer_id = request.form.get("peer_id", "")
    if not peer_id:
        return jsonify({"error": "No peer_id"}), 400

    f = request.files["file"]
    safe_name = secure_filename(f.filename or "upload")
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400

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
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": "Transfer failed (peer unavailable or trust policy)"}), 500

    _orig_on_complete = node.file_mgr.on_complete
    def _on_complete_with_cleanup(transfer):
        if _orig_on_complete:
            try:
                _orig_on_complete(transfer)
            except Exception:
                pass
        if transfer.file_id == file_id:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            node.file_mgr.on_complete = _orig_on_complete
    node.file_mgr.on_complete = _on_complete_with_cleanup
    return jsonify({"file_id": file_id, "filename": safe_name})


@app.route("/downloads/<path:filename>")
def downloads(filename: str):
    """Serve received files from the local downloads directory."""
    # `send_from_directory` will prevent path traversal.
    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True)

@app.route("/api/add_peer", methods=["POST"])
def api_add_peer():
    data = request.get_json()
    ip = data.get("ip", "").strip()
    tcp_port = data.get("tcp_port", 5151)
    name = data.get("name", "").strip()
    if not ip:
        return jsonify({"error": "IP required"}), 400
    try:
        tcp_port = int(tcp_port)
    except ValueError:
        return jsonify({"error": "Invalid port"}), 400
    node.add_manual_peer(ip, tcp_port, name)
    return jsonify({"status": "ok"})

@app.route("/health")
def health():
    if node is None:
        return jsonify({"status": "degraded"}), 503
    return jsonify(node.get_health_snapshot())

@app.route("/ready")
def ready():
    if node is None:
        return jsonify({"ready": False}), 503
    ok = node.is_ready()
    return jsonify({"ready": ok}), (200 if ok else 503)


# ── Security / Seed-pairing / Blacklist API ──────────────

@app.route("/api/seed/generate", methods=["POST"])
def api_seed_generate():
    data = request.get_json(force=True)
    peer_id = data.get("peer_id", "")
    if not peer_id:
        return jsonify({"error": "peer_id required"}), 400
    peers = node.get_peers()
    if not any(p['peer_id'] == peer_id for p in peers):
        return jsonify({"error": "Peer not found"}), 404
    return jsonify({"seed": node.generate_pairing_seed(peer_id)})

@app.route("/api/seed/pair", methods=["POST"])
def api_seed_pair():
    data = request.get_json(force=True)
    peer_id = data.get("peer_id", "")
    seed = data.get("seed", "").strip().upper()
    if not peer_id or not seed:
        return jsonify({"error": "peer_id and seed required"}), 400
    result = node.pair_with_seed(peer_id, seed)
    if result["ok"]:
        return jsonify({"status": "paired", "peer_id": peer_id})
    if result["reason"] == "own_seed":
        return jsonify({"error": "Cannot pair with your own seed code"}), 400
    return jsonify({"error": "Pairing failed"}), 400

@app.route("/api/security/blacklist", methods=["GET"])
def api_blacklist_get():
    return jsonify({"blacklist": node.get_blacklist()})

@app.route("/api/security/blacklist", methods=["POST"])
def api_blacklist_add():
    data = request.get_json(force=True)
    peer_id = data.get("peer_id", "")
    if not peer_id:
        return jsonify({"error": "peer_id required"}), 400
    node.blacklist_peer(peer_id)
    return jsonify({"status": "blacklisted", "peer_id": peer_id})

@app.route("/api/security/blacklist/<peer_id>", methods=["DELETE"])
def api_blacklist_remove(peer_id):
    node.unblacklist_peer(peer_id)
    return jsonify({"status": "removed", "peer_id": peer_id})

@app.route("/api/security/events")
def api_security_events():
    limit = int(request.args.get("limit", "200") or 200)
    return jsonify({"events": node.get_security_events(limit)})

@app.route("/metrics")
def api_metrics():
    if not node:
        return "# meshlink_ready 0\n", 503
    stats = node.get_statistics()
    lines = ["# HELP meshlink_ready MeshLink node readiness (1=ready, 0=not ready)",
             "# TYPE meshlink_ready gauge",
             f"meshlink_ready 1",
             "# HELP meshlink_active_peers Number of active peers",
             "# TYPE meshlink_active_peers gauge",
             f"meshlink_active_peers {stats.get('active_peers', 0)}",
             "# HELP meshlink_outbox_pending Number of pending outgoing messages",
             "# TYPE meshlink_outbox_pending gauge",
             f"meshlink_outbox_pending {stats.get('outbox_pending', 0)}",
             "# HELP meshlink_delivery_retry_total Total delivery retries",
             "# TYPE meshlink_delivery_retry_total counter",
             f"meshlink_delivery_retry_total {stats.get('delivery_retry_total', 0)}",
             "# HELP meshlink_file_resume_total Total file resume operations",
             "# TYPE meshlink_file_resume_total counter",
             f"meshlink_file_resume_total {stats.get('file_resume_total', 0)}"]
    return "\n".join(lines) + "\n", 200

@app.route("/api/network/diagnostics")
def api_network_diagnostics():
    if not node:
        return jsonify({"error": "Node not initialized"}), 503
    return jsonify({
        "delivery": node.msg_server.get_delivery_diagnostics() if hasattr(node.msg_server, 'get_delivery_diagnostics') else {},
        "queue": node.msg_server.get_queue_diagnostics() if hasattr(node.msg_server, 'get_queue_diagnostics') else {},
        "file_transfer": node.file_mgr.get_diagnostics() if hasattr(node.file_mgr, 'get_diagnostics') else {},
    })


# ── Socket.IO events ────────────────────────────────────

@socketio.on("connect")
def on_connect():
    emit("node_info", node.get_info())
    emit("peers_list", node.get_peers())
    emit("groups_list", node.get_groups())
    emit("statistics", node.get_statistics())

@socketio.on("send_message")
def on_send_message(data):
    peer_id = data.get("peer_id", "")
    text = data.get("text", "")
    if peer_id and text:
        result = node.send_text(peer_id, text)
        if result:
            emit("message_sent", result)
        else:
            emit("error", {"message": "Failed to send (peer unavailable or trust policy)"})

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

@socketio.on("create_group")
def on_create_group(data):
    name = (data or {}).get("name", "")
    members = list((data or {}).get("members", []) or [])
    group = node.create_group(name, members)
    emit("group_created", group)
    emit("groups_list", node.get_groups())

@socketio.on("get_groups")
def on_get_groups():
    emit("groups_list", node.get_groups())

@socketio.on("send_group_message")
def on_send_group_message(data):
    group_id = (data or {}).get("group_id", "")
    text = (data or {}).get("text", "")
    out = node.send_group_text(group_id, text)
    if out:
        emit("group_message_sent", out)
    else:
        emit("error", {"message": "Failed to send group message"})

@socketio.on("start_call")
def on_start_call(data):
    peer_id = data.get("peer_id", "")
    call_type = data.get("call_type", "audio")
    if peer_id:
        if not node.start_call(peer_id, call_type):
            emit("error", {"message": "Call failed (peer unavailable or not trusted)"})

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

@socketio.on("seed_pair")
def on_seed_pair(data):
    peer_id = data.get("peer_id", "")
    seed = data.get("seed", "").strip().upper()
    if peer_id and seed:
        result = node.pair_with_seed(peer_id, seed)
        emit("seed_pair_result", {**result, "peer_id": peer_id})

@socketio.on("blacklist_peer")
def on_blacklist_peer(data):
    peer_id = data.get("peer_id", "")
    action = data.get("action", "add")
    if not peer_id:
        return
    if action == "remove":
        node.unblacklist_peer(peer_id)
    else:
        node.blacklist_peer(peer_id)
    emit("blacklist_updated", {"peer_id": peer_id, "action": action})

@socketio.on("webrtc_offer")
def on_webrtc_offer(data):
    peer_id = data.get("peer_id", "")
    if peer_id:
        node.send_webrtc_signal(peer_id, "offer", {
            "sdp": data.get("sdp", {}), "call_type": data.get("call_type", "audio"),
        })

@socketio.on("webrtc_answer")
def on_webrtc_answer(data):
    peer_id = data.get("peer_id", "")
    if peer_id:
        node.send_webrtc_signal(peer_id, "answer", {"sdp": data.get("sdp", {})})

@socketio.on("webrtc_ice")
def on_webrtc_ice(data):
    peer_id = data.get("peer_id", "")
    if peer_id:
        node.send_webrtc_signal(peer_id, "ice", {"candidate": data.get("candidate")})


def run_server():
    logger.info(f"Web UI: http://localhost:{WEB_PORT}")
    socketio.run(
        app, host="0.0.0.0", port=WEB_PORT,
        debug=False, allow_unsafe_werkzeug=True, use_reloader=False,
    )
