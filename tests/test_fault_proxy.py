"""Unit tests for fault_proxy.py against a throwaway Python upstream (no Dart needed)."""

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fault_proxy import FaultProxy, Rule


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        data = json.dumps({"path": self.path, "host": self.headers["Host"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(base, path, timeout=5):
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_passes_other_paths_through_unchanged(upstream):
    proxy = FaultProxy(upstream, [Rule("/inbox", status=500)], quiet=True).start()
    try:
        status, body = get(proxy.url, "/orders?page=2")
        assert status == 200
        assert json.loads(body)["path"] == "/orders?page=2"
        assert json.loads(body)["host"] == upstream.removeprefix("http://")
    finally:
        proxy.stop()


@pytest.mark.parametrize("code", [500, 503])
def test_injects_status_on_matching_path(upstream, code):
    proxy = FaultProxy(upstream, [Rule("/inbox", status=code)], quiet=True).start()
    try:
        assert get(proxy.url, "/inbox")[0] == code
        assert get(proxy.url, "/inbox?x=1")[0] == code  # query string is ignored for matching
        assert get(proxy.url, "/orders")[0] == 200
    finally:
        proxy.stop()


def test_drop_closes_without_response(upstream):
    proxy = FaultProxy(upstream, [Rule("/inbox", drop=True)], quiet=True).start()
    try:
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
            get(proxy.url, "/inbox")
        assert proxy.hits[-1].status is None
    finally:
        proxy.stop()


def test_delay_then_forward(upstream):
    proxy = FaultProxy(upstream, [Rule("/inbox", delay=0.4)], quiet=True).start()
    try:
        started = time.monotonic()
        assert get(proxy.url, "/inbox")[0] == 200
        assert time.monotonic() - started >= 0.4
    finally:
        proxy.stop()


def test_times_limits_the_fault_then_recovers(upstream):
    proxy = FaultProxy(upstream, [Rule("/inbox", status=503, times=2)], quiet=True).start()
    try:
        assert [get(proxy.url, "/inbox")[0] for _ in range(3)] == [503, 503, 200]
    finally:
        proxy.stop()


def test_wildcards_and_method_filter(upstream):
    proxy = FaultProxy(upstream, [Rule("/orders/*", status=500, method="POST")], quiet=True).start()
    try:
        assert get(proxy.url, "/orders/A-1001")[0] == 200  # GET is not matched
    finally:
        proxy.stop()


def test_unreachable_upstream_is_a_502():
    proxy = FaultProxy("http://127.0.0.1:9", [], quiet=True).start()
    try:
        assert get(proxy.url, "/orders")[0] == 502
    finally:
        proxy.stop()
