"""Port conflicts, proxy identity and health checks (local fake servers only)."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import common
import manage
import validate


def test_port_in_use_detection(port):
    assert not common.port_in_use("127.0.0.1", port)
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        assert common.port_in_use("127.0.0.1", port)


def _serve(port, handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _settings(port):
    s = common.default_settings()
    s["proxy_port"] = port
    return s


def test_health_not_running(port):
    assert validate.proxy_health(_settings(port), "k").code == "not_running"


def test_foreign_listener_is_not_trusted(port):
    class Foreign(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = _serve(port, Foreign)
    try:
        assert validate.proxy_health(_settings(port), "k").code == "foreign_listener"
    finally:
        srv.shutdown()


def test_other_litellm_with_different_key_is_not_ours(port):
    class OtherProxy(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health/liveliness":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'"I\'m alive!"')
                return
            self.send_response(401)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = _serve(port, OtherProxy)
    try:
        assert validate.proxy_health(_settings(port), "sk-kgw-ours").code == "foreign_listener"
    finally:
        srv.shutdown()


def test_healthy_gateway_must_serve_alias(port):
    served = {"ids": ["kimi-k3"]}

    class Ours(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            if self.path == "/v1/models":
                assert self.headers["Authorization"] == "Bearer sk-kgw-ours"  # kgw-scan: allow
                self.wfile.write(json.dumps({"data": [{"id": i} for i in served["ids"]]}).encode())

        def log_message(self, *a):
            pass

    srv = _serve(port, Ours)
    try:
        assert validate.proxy_health(_settings(port), "sk-kgw-ours").ok
        served["ids"] = ["something-else"]
        assert validate.proxy_health(_settings(port), "sk-kgw-ours").code == "alias_missing"
    finally:
        srv.shutdown()


def test_start_refuses_when_port_taken_by_other_process(kgw_home, port, memory_keyring):
    s = _settings(port)
    common.save_settings(s)
    store = common.store_for(s)
    store.set(common.SECRET_BEDROCK_TOKEN, "tok-123456")
    store.set(common.SECRET_MASTER_KEY, common.generate_master_key())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        with pytest.raises(common.GatewayError) as exc:
            manage.start_proxy(s, store, background=True)
    assert exc.value.code == "port_in_use"


def test_stale_pid_file_is_ignored(kgw_home, port):
    s = _settings(port)
    common.write_pid(999999, s, "background")
    state = manage.proxy_state(s)
    assert not state["ours"] and state["pid"] is None
    assert common.read_pid() is None


def test_pid_of_unrelated_process_is_not_ours(kgw_home):
    import os

    assert not common.process_is_our_proxy(os.getpid())


def test_stop_does_not_touch_foreign_process(kgw_home, port, capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        assert manage.stop_proxy(_settings(port)) is False
    assert "not this gateway" in capsys.readouterr().out
