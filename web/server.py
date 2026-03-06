"""
MeshLink — Web Interface Server (v2)
Flask + Socket.IO with WebRTC signaling relay.
"""

import os
import json
import time
import logging
import tempfile
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit

try:
    from ..core.node import MeshNode
    from ..core.config import WEB_PORT, NODE_ID, NODE_NAME, LOCAL_IP, DOWNLOADS_DIR
except ImportError:
    from core.node import MeshNode
    from core.config import WEB_PORT, NODE_ID, NODE_NAME, LOCAL_IP, DOWNLOADS_DIR

logger = logging.getLogger("meshlink.web")

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
    # Save to temp
    tmp_dir = tempfile.mkdtemp(prefix="meshlink_")
    tmp_path = os.path.join(tmp_dir, f.filename)
    f.save(tmp_path)

    file_id = node.send_file(peer_id, tmp_path)
    if file_id:
        return jsonify({"file_id": file_id, "filename": f.filename})
    return jsonify({"error": "Transfer failed"}), 500


@app.route("/downloads/<path:filename>")
def download_file(filename):
    return send_from_directory(DOWNLOADS_DIR, filename)


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


# ── Socket.IO events ────────────────────────────────────

@socketio.on("connect")
def on_connect():
    emit("node_info", node.get_info())
    emit("peers_list", node.get_peers())


@socketio.on("send_message")
def on_send_message(data):
    peer_id = data.get("peer_id", "")
    text = data.get("text", "")
    if peer_id and text:
        result = node.send_text(peer_id, text)
        if result:
            emit("message_sent", result)
        else:
            emit("error", {"message": "Failed to send"})


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
