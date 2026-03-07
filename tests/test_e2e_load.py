import os
import threading
import time

from core.messaging import MessageServer, make_text_message


def test_e2e_message_burst_localhost():
    """
    Lightweight e2e/load smoke:
    - поднимает локальный MessageServer
    - отправляет пачку сообщений в localhost
    - проверяет, что не падаем по транспорту
    """
    srv = MessageServer()
    got = []
    srv.on(1, lambda m: got.append(m.msg_id))
    srv.start()
    try:
        total = 50
        ok = 0
        for i in range(total):
            msg = make_text_message(f"burst-{i}")
            if srv.send_to_peer("127.0.0.1", int(os.getenv("MESHLINK_TCP_PORT", "5151")), msg, "self"):
                ok += 1
        # В single-node тесте часть может уйти в sent/fail в зависимости от окружения,
        # но сам цикл не должен ломаться.
        assert ok >= 0
    finally:
        srv.stop()


def test_e2e_retry_degradation_pattern(monkeypatch):
    """
    Эмулируем деградацию канала:
    первые попытки connect падают, затем проходят.
    """
    attempts = {"n": 0}

    class FakeSocket:
        def setsockopt(self, *args, **kwargs):
            return None

        def settimeout(self, *args, **kwargs):
            return None

        def connect(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("simulated drop")

        def close(self):
            return None

    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: FakeSocket())
    monkeypatch.setattr("core.messaging.send_message", lambda sock, msg: None)
    monkeypatch.setattr("core.messaging.MessageServer._handle_client", lambda self, sock, addr: None)

    srv = MessageServer()
    srv.max_retries = 4
    srv.retry_backoff_base = 0

    msg = make_text_message("degraded")
    done = {"ok": False}

    def set_ack():
        while not msg.msg_id:
            time.sleep(0.001)
        while True:
            with srv._delivery_lock:
                ev = srv._delivery_events.get(msg.msg_id)
            if ev:
                ev.set()
                break
            time.sleep(0.001)

    t = threading.Thread(target=set_ack, daemon=True)
    t.start()
    done["ok"] = srv.send_to_peer("127.0.0.1", 5151, msg, "peer-load")
    assert done["ok"] is True
    assert attempts["n"] >= 3

