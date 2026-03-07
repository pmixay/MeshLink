from core.node import MeshNode


class _DummyDiscovery:
    def __init__(self):
        self._peers = {}

    def get_peer(self, peer_id):
        return self._peers.get(peer_id)


class _DummyPeer:
    def __init__(self, ip="127.0.0.1", tcp_port=20001, name="Peer"):
        self.ip = ip
        self.tcp_port = tcp_port
        self.file_port = tcp_port + 2
        self.media_port = tcp_port + 1
        self.name = name


def test_group_message_roundtrip_in_memory(monkeypatch):
    node = MeshNode()
    node.discovery = _DummyDiscovery()
    node.discovery._peers["p1"] = _DummyPeer()

    monkeypatch.setattr(node.crypto, "is_trusted", lambda pid: True)
    monkeypatch.setattr(node.msg_server, "send_to_peer", lambda *args, **kwargs: True)

    g = node.create_group("QA", ["p1"])
    r = node.send_group_text(g["group_id"], "hello all")
    assert r is not None

    chat = node.get_chat(f"group:{g['group_id']}")
    assert any(m.get("text") == "hello all" for m in chat)


def test_add_group_members(monkeypatch):
    node = MeshNode()
    node.discovery = _DummyDiscovery()
    node.discovery._peers["p1"] = _DummyPeer(tcp_port=21001)
    node.discovery._peers["p2"] = _DummyPeer(tcp_port=21011)

    monkeypatch.setattr(node.crypto, "is_trusted", lambda pid: pid in ("p1", "p2"))
    monkeypatch.setattr(node.msg_server, "send_to_peer", lambda *args, **kwargs: True)

    g = node.create_group("Ops", ["p1"])
    upd = node.add_group_members(g["group_id"], ["p2"])
    assert upd is not None
    assert "p2" in (upd.get("members") or [])

