import pytest

pytest.importorskip("flask_socketio")

from core.node import MeshNode
from web import server


def test_health_and_ready_endpoints(tmp_workspace):
    node = MeshNode()
    server.node = node
    client = server.app.test_client()

    h = client.get("/health")
    assert h.status_code == 200
    assert h.get_json().get("status") == "ok"

    r = client.get("/ready")
    assert r.status_code == 503
    assert r.get_json().get("ready") is False


def test_metrics_endpoint_prometheus_format(tmp_workspace):
    node = MeshNode()
    server.node = node
    client = server.app.test_client()

    res = client.get("/metrics")
    assert res.status_code == 200
    body = res.data.decode("utf-8")
    assert "# TYPE meshlink_active_peers gauge" in body
    assert "meshlink_outbox_pending " in body
    assert "meshlink_delivery_retry_total " in body
    assert "meshlink_file_resume_total " in body


def test_network_diagnostics_endpoint(tmp_workspace):
    node = MeshNode()
    server.node = node
    client = server.app.test_client()

    res = client.get("/api/network/diagnostics")
    assert res.status_code == 200
    payload = res.get_json()
    assert "delivery" in payload
    assert "queue" in payload
    assert "file_transfer" in payload


def test_metrics_endpoint_when_node_not_initialized():
    server.node = None
    client = server.app.test_client()

    res = client.get("/metrics")
    assert res.status_code == 503
    body = res.data.decode("utf-8")
    assert "meshlink_ready 0" in body
