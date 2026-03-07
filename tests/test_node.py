from core.messaging import Message, MsgType
from core.node import MeshNode, ChatMessage
from core.crypto import CryptoManager


class _DummyDiscovery:
    def __init__(self):
        self._peers = {}

    def get_peer(self, peer_id):
        return self._peers.get(peer_id)

    def get_peers(self):
        return []

    def mark_trusted(self, peer_id):
        return None


class _DummyPeer:
    def __init__(self, ip="127.0.0.1", tcp_port=9999, file_port=9998, name="Peer"):
        self.ip = ip
        self.tcp_port = tcp_port
        self.file_port = file_port
        self.name = name


def test_delivery_status_updates_chat_and_emits(tmp_workspace):
    node = MeshNode()
    emitted = []
    node.on("message_status", lambda d: emitted.append(d))

    node.chats.setdefault("peer1", []).append(ChatMessage(
        msg_id="m-1",
        sender_id="me",
        sender_name="Me",
        text="hello",
        timestamp=1.0,
        is_me=True,
        status="sent",
    ))

    node._on_delivery_status({"peer_id": "peer1", "msg_id": "m-1", "status": "delivered"})
    assert node.chats["peer1"][0].status == "delivered"
    assert emitted and emitted[-1]["status"] == "delivered"


def test_trusted_only_blocks_outgoing_untrusted(monkeypatch):
    node = MeshNode()
    node.discovery = _DummyDiscovery()
    node.discovery._peers["p1"] = _DummyPeer()

    monkeypatch.setattr("core.config.TRUSTED_ONLY_PRIVATE_CHATS", True)
    monkeypatch.setattr(node.crypto, "is_trusted", lambda peer_id: False)

    res = node.send_text("p1", "secret")
    assert res is None
    snap = node.get_security_snapshot()
    assert any(ev["event"] == "outgoing_text_blocked_untrusted" for ev in snap["events"])


def test_relay_invalid_signature_is_dropped_and_recorded(monkeypatch):
    node = MeshNode()
    monkeypatch.setattr(node.crypto, "verify_from", lambda *args, **kwargs: False)

    msg = Message(
        msg_type=MsgType.MESH_RELAY,
        sender_id="peer-x",
        sender_name="X",
        payload={"inner_type": MsgType.TEXT, "inner_payload": {"text": "t"}},
        msg_id="relay-1",
        signature="sig",
    )
    node._on_mesh_relay(msg)
    events = node.get_security_events(20)
    assert any(e["event"] == "dropped_invalid_relay_signature" for e in events)


def test_security_snapshot_contains_expected_keys():
    node = MeshNode()
    snap = node.get_security_snapshot()
    assert "trusted_only_private_chats" in snap
    assert "blacklist" in snap
    assert "banned" in snap
    assert "events" in snap


def test_call_policy_blocks_outgoing_untrusted(monkeypatch):
    node = MeshNode()
    node.discovery = _DummyDiscovery()
    node.discovery._peers["p1"] = _DummyPeer()

    monkeypatch.setattr("core.config.TRUSTED_ONLY_CALL", True)
    monkeypatch.setattr(node.crypto, "is_trusted", lambda peer_id: False)

    ok = node.start_call("p1", "audio")
    assert ok is False
    events = node.get_security_events(20)
    assert any(ev["event"] == "outgoing_call_blocked_untrusted" for ev in events)


def test_call_policy_blocks_incoming_untrusted(monkeypatch):
    node = MeshNode()
    incoming = []
    node.on("call_incoming", lambda d: incoming.append(d))

    monkeypatch.setattr("core.config.TRUSTED_ONLY_CALL", True)
    monkeypatch.setattr(node.crypto, "is_trusted", lambda peer_id: False)

    msg = Message(
        msg_type=MsgType.CALL_INVITE,
        sender_id="peer-c",
        sender_name="PeerC",
        payload={"call_type": "audio"},
        msg_id="c-1",
    )
    node._on_call_invite(msg)
    assert incoming == []
    events = node.get_security_events(20)
    assert any(ev["event"] == "incoming_call_blocked_untrusted" for ev in events)


def test_peer_activity_rotates_expired_or_old_session(monkeypatch):
    node = MeshNode()
    node.crypto._sessions["peer-r"] = object()  # type: ignore[assignment]
    monkeypatch.setattr(node.crypto, "check_session_ttl", lambda peer_id: True)
    monkeypatch.setattr(node.crypto, "should_rotate_session", lambda peer_id: True)
    monkeypatch.setattr(node.crypto, "rotate_session", lambda peer_id: True)

    node._on_peer_activity("peer-r")

    events = node.get_security_events(20)
    assert any(ev["event"] == "session_rotated" for ev in events)


def test_peer_activity_expires_stale_session(monkeypatch):
    node = MeshNode()
    peer_crypto = CryptoManager()
    node.crypto.establish_session("peer-exp", peer_crypto.public_key_b64)

    meta = node.crypto._session_meta["peer-exp"]
    past = meta["last_used"] - 10_000
    meta["created_at"] = past
    meta["last_rotated"] = past
    meta["last_used"] = past
    meta["ttl_seconds"] = 1

    node._on_peer_activity("peer-exp")

    assert node.crypto.has_session("peer-exp") is False
    events = node.get_security_events(20)
    assert any(ev["event"] == "session_expired" for ev in events)
