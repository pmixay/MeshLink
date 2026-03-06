import threading
import time

from core import messaging
from core.messaging import Message, MsgType, MessageServer


def test_message_roundtrip_and_msg_id_generation():
    m = Message(msg_type=MsgType.TEXT, sender_id="n1", sender_name="Node1", payload={"text": "hi"})
    raw = m.to_bytes()
    assert m.msg_id
    body = raw[4:]
    parsed = Message.from_bytes(body)
    assert parsed.msg_id == m.msg_id
    assert parsed.payload["text"] == "hi"


def test_persist_and_restore_chat_history_with_status_updates(tmp_workspace):
    messaging.persist_chat_entry({
        "peer_id": "peer-a",
        "msg_id": "m1",
        "sender_id": "peer-a",
        "sender_name": "Alice",
        "text": "hello",
        "is_me": False,
        "msg_type": "text",
        "status": "sent",
        "timestamp": 1.0,
    })
    messaging.persist_chat_entry({
        "kind": "status_update",
        "peer_id": "peer-a",
        "msg_id": "m1",
        "status": "delivered",
        "timestamp": 2.0,
    })

    restored = messaging.load_persisted_chats()
    assert "peer-a" in restored
    assert len(restored["peer-a"]) == 1
    assert restored["peer-a"][0]["status"] == "delivered"


def test_send_to_peer_sets_failed_on_transport_error(monkeypatch):
    class BadSocket:
        def setsockopt(self, *args, **kwargs):
            return None

        def settimeout(self, *args, **kwargs):
            return None

        def connect(self, *args, **kwargs):
            raise ConnectionError("boom")

        def close(self):
            return None

    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: BadSocket())

    srv = MessageServer()
    srv.max_retries = 2
    srv.retry_backoff_base = 0

    msg = Message(msg_type=MsgType.TEXT, sender_id="me", sender_name="Me", payload={"text": "x"})
    ok = srv.send_to_peer("127.0.0.1", 9999, msg, "peer-b")
    assert ok is False
    assert srv.get_delivery_status(msg.msg_id) == "failed"


def test_send_to_peer_delivered_when_ack_event_set(monkeypatch):
    class FakeSocket:
        def setsockopt(self, *args, **kwargs):
            return None

        def settimeout(self, *args, **kwargs):
            return None

        def connect(self, *args, **kwargs):
            return None

        def close(self):
            return None

    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: FakeSocket())
    monkeypatch.setattr("core.messaging.send_message", lambda sock, msg: None)
    monkeypatch.setattr("core.messaging.MessageServer._handle_client", lambda self, sock, addr: None)

    srv = MessageServer()
    srv.max_retries = 1
    srv.delivery_wait_timeout = 1.0

    msg = Message(msg_type=MsgType.TEXT, sender_id="me", sender_name="Me", payload={"text": "x"})

    def ack_soon():
        while not msg.msg_id:
            time.sleep(0.001)
        time.sleep(0.02)
        with srv._delivery_lock:
            ev = srv._delivery_events.get(msg.msg_id)
        if ev:
            ev.set()

    t = threading.Thread(target=ack_soon, daemon=True)
    t.start()

    ok = srv.send_to_peer("127.0.0.1", 9999, msg, "peer-c")
    assert ok is True
    assert srv.get_delivery_status(msg.msg_id) in ("sent", "delivered")

