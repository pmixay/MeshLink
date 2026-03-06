import os
import time
import tempfile
import multiprocessing as mp
import queue
from pathlib import Path

import pytest


def _node_process_main(cmd_q: "mp.Queue", resp_q: "mp.Queue", cfg: dict):
    os.environ["MESHLINK_NODE_NAME"] = cfg["name"]
    os.environ["MESHLINK_NODE_ID"] = cfg["node_id"]
    os.environ["MESHLINK_WEB_PORT"] = str(cfg["web_port"])
    os.environ["MESHLINK_TCP_PORT"] = str(cfg["tcp_port"])
    os.environ["MESHLINK_MEDIA_PORT"] = str(cfg["media_port"])
    os.environ["MESHLINK_FILE_PORT"] = str(cfg["file_port"])
    os.environ["MESHLINK_DISCOVERY_PORT"] = str(cfg["discovery_port"])
    os.environ["MESHLINK_DOWNLOADS"] = cfg["downloads_dir"]
    os.environ["MESHLINK_DATA_DIR"] = cfg["data_dir"]

    from core.node import MeshNode

    node = MeshNode()
    node.start()
    resp_q.put({"evt": "ready", "node_id": cfg["node_id"]})

    running = True
    while running:
        try:
            cmd = cmd_q.get(timeout=0.2)
        except queue.Empty:
            continue
        except Exception:
            continue

        cid = cmd.get("id")
        op = cmd.get("op")
        try:
            if op == "stop":
                running = False
                resp_q.put({"id": cid, "ok": True})
            elif op == "get_peers":
                resp_q.put({"id": cid, "ok": True, "data": node.get_peers()})
            elif op == "get_chat":
                resp_q.put({"id": cid, "ok": True, "data": node.get_chat(cmd.get("peer_id", ""))})
            elif op == "send_text":
                out = node.send_text(cmd.get("peer_id", ""), cmd.get("text", ""))
                resp_q.put({"id": cid, "ok": out is not None, "data": out})
            elif op == "send_file":
                out = node.send_file(cmd.get("peer_id", ""), cmd.get("path", ""))
                resp_q.put({"id": cid, "ok": bool(out), "data": out})
            else:
                resp_q.put({"id": cid, "ok": False, "error": f"unknown op {op}"})
        except Exception as e:
            resp_q.put({"id": cid, "ok": False, "error": str(e)})

    try:
        node.stop()
    except Exception:
        pass


def _rpc(cmd_q: "mp.Queue", resp_q: "mp.Queue", seq: int, op: str, timeout: float = 8.0, **kwargs) -> dict:
    payload = {"id": seq, "op": op, **kwargs}
    cmd_q.put(payload)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = resp_q.get(timeout=0.5)
        except queue.Empty:
            continue
        if msg.get("id") == seq:
            return msg
    raise TimeoutError(f"RPC timeout op={op}")


def _wait_peers(cmd_q: "mp.Queue", resp_q: "mp.Queue", seq_start: int, expected_min: int, timeout: float = 25.0) -> int:
    seq = seq_start
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = _rpc(cmd_q, resp_q, seq, "get_peers", timeout=3.0)
        seq += 1
        if res.get("ok") and len(res.get("data") or []) >= expected_min:
            return seq
        time.sleep(0.35)
    raise TimeoutError("peer discovery timeout")


def _wait_chat_text(cmd_q: "mp.Queue", resp_q: "mp.Queue", seq_start: int, peer_id: str, text: str, timeout: float = 25.0) -> int:
    seq = seq_start
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = _rpc(cmd_q, resp_q, seq, "get_chat", peer_id=peer_id, timeout=3.0)
        seq += 1
        if res.get("ok"):
            chat = res.get("data") or []
            if any((m.get("text") == text and m.get("msg_type") == "text") for m in chat):
                return seq
        time.sleep(0.3)
    raise TimeoutError(f"chat text not found: {text}")


@pytest.mark.integration
def test_multiprocess_text_file_and_restart_recovery():
    ctx = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="meshlink_mp_") as td:
        base = Path(td)

        nodes = {
            "a": {
                "name": "NodeA", "node_id": "nodea",
                "web_port": 28080, "tcp_port": 25151, "media_port": 25152, "file_port": 25153,
                "discovery_port": 25150,
                "downloads_dir": str(base / "a_downloads"), "data_dir": str(base / "a_data"),
            },
            "b": {
                "name": "NodeB", "node_id": "nodeb",
                "web_port": 28081, "tcp_port": 26151, "media_port": 26152, "file_port": 26153,
                "discovery_port": 25150,
                "downloads_dir": str(base / "b_downloads"), "data_dir": str(base / "b_data"),
            },
            "c": {
                "name": "NodeC", "node_id": "nodec",
                "web_port": 28082, "tcp_port": 27151, "media_port": 27152, "file_port": 27153,
                "discovery_port": 25150,
                "downloads_dir": str(base / "c_downloads"), "data_dir": str(base / "c_data"),
            },
        }

        for n in nodes.values():
            Path(n["downloads_dir"]).mkdir(parents=True, exist_ok=True)
            Path(n["data_dir"]).mkdir(parents=True, exist_ok=True)

        qa_cmd, qa_resp = ctx.Queue(), ctx.Queue()
        qb_cmd, qb_resp = ctx.Queue(), ctx.Queue()
        qc_cmd, qc_resp = ctx.Queue(), ctx.Queue()

        pa = ctx.Process(target=_node_process_main, args=(qa_cmd, qa_resp, nodes["a"]), daemon=True)
        pb = ctx.Process(target=_node_process_main, args=(qb_cmd, qb_resp, nodes["b"]), daemon=True)
        pc = ctx.Process(target=_node_process_main, args=(qc_cmd, qc_resp, nodes["c"]), daemon=True)

        pa.start(); pb.start(); pc.start()
        seq = 1
        procs = [pa, pb, pc]

        try:
            # дождаться поднятия процессов
            for resp in (qa_resp, qb_resp, qc_resp):
                m = resp.get(timeout=15)
                assert m.get("evt") == "ready"

            seq = _wait_peers(qa_cmd, qa_resp, seq, expected_min=2)
            seq = _wait_peers(qb_cmd, qb_resp, seq, expected_min=2)
            seq = _wait_peers(qc_cmd, qc_resp, seq, expected_min=2)

            # 1) send_text()
            r = _rpc(qa_cmd, qa_resp, seq, "send_text", peer_id="nodeb", text="mp-hello-1", timeout=10.0)
            seq += 1
            assert r.get("ok") is True
            seq = _wait_chat_text(qb_cmd, qb_resp, seq, peer_id="nodea", text="mp-hello-1", timeout=25.0)

            # 2) send_file()
            file_src = base / "payload.bin"
            file_src.write_bytes((b"meshlink-file-payload-" * 4096))
            r2 = _rpc(qa_cmd, qa_resp, seq, "send_file", peer_id="nodeb", path=str(file_src), timeout=10.0)
            seq += 1
            assert r2.get("ok") is True

            download_path = Path(nodes["b"]["downloads_dir"]) / "payload.bin"
            deadline = time.time() + 35.0
            while time.time() < deadline:
                if download_path.exists() and download_path.stat().st_size == file_src.stat().st_size:
                    break
                time.sleep(0.25)
            assert download_path.exists()
            assert download_path.stat().st_size == file_src.stat().st_size

            # 3) recovery after kill/restart
            pb.terminate()
            pb.join(timeout=8)
            assert pb.exitcode is not None

            qb_cmd, qb_resp = ctx.Queue(), ctx.Queue()
            pb = ctx.Process(target=_node_process_main, args=(qb_cmd, qb_resp, nodes["b"]), daemon=True)
            pb.start()
            procs[1] = pb
            m = qb_resp.get(timeout=15)
            assert m.get("evt") == "ready"

            seq = _wait_peers(qa_cmd, qa_resp, seq, expected_min=2, timeout=35.0)
            seq = _wait_peers(qb_cmd, qb_resp, seq, expected_min=2, timeout=35.0)

            r3 = _rpc(qa_cmd, qa_resp, seq, "send_text", peer_id="nodeb", text="mp-after-restart", timeout=10.0)
            seq += 1
            assert r3.get("ok") is True
            _wait_chat_text(qb_cmd, qb_resp, seq, peer_id="nodea", text="mp-after-restart", timeout=30.0)
        finally:
            for cq, rq, p in ((qa_cmd, qa_resp, pa), (qb_cmd, qb_resp, pb), (qc_cmd, qc_resp, pc)):
                try:
                    if p.is_alive():
                        _rpc(cq, rq, seq, "stop", timeout=2.0)
                        seq += 1
                except Exception:
                    pass
                if p.is_alive():
                    p.terminate()
                p.join(timeout=5)
