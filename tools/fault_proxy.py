#!/usr/bin/env python3
"""A local pass-through proxy that breaks one endpoint on purpose.

Every request is forwarded to the real backend unchanged, except requests
that match the rule, which get a chosen failure instead:

    # /inbox answers 500, everything else reaches the backend
    python3 tools/fault_proxy.py --upstream https://api.example.test --port 9090 --path /inbox --status 500

    # /inbox: connection closed with no response
    python3 tools/fault_proxy.py --upstream http://127.0.0.1:8080 --path /inbox --drop

    # /inbox: forwarded, but 5 seconds late (combine with --status to delay an error)
    python3 tools/fault_proxy.py --upstream http://127.0.0.1:8080 --path /inbox --delay 5

``--path`` is matched against the path without the query string and accepts
shell-style wildcards (``/orders/*``). ``--times N`` faults only the first N
matching requests, which is handy for "fails once, then recovers" cases.

This is not an intercepting proxy: the app is pointed at it on purpose (see
repoint.py), so there is no certificate trickery and no system proxy setting.
Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import http.client
import json
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}


@dataclass
class Rule:
    path: str
    status: int | None = None
    drop: bool = False
    delay: float = 0.0
    method: str | None = None
    times: int | None = None
    body: str = '{"error":"injected by fault_proxy"}'
    hits: int = 0

    def matches(self, method: str, path: str) -> bool:
        if self.method and self.method.upper() != method.upper():
            return False
        if not fnmatch.fnmatchcase(path, self.path):
            return False
        return self.times is None or self.hits < self.times

    def describe(self) -> str:
        parts = []
        if self.delay:
            parts.append(f"delay {self.delay:g}s")
        if self.drop:
            parts.append("drop")
        elif self.status:
            parts.append(f"status {self.status}")
        if not parts:
            parts.append("pass")
        return f"{self.method or '*'} {self.path} -> {', '.join(parts)}"


@dataclass
class Hit:
    method: str
    path: str
    action: str  # "pass", "fault", or "gone" (the app hung up before the answer)
    status: int | None  # None when the connection was dropped
    ms: float


@dataclass
class FaultProxy:
    upstream: str
    rules: list[Rule] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 0
    quiet: bool = False
    hits: list[Hit] = field(default_factory=list)

    def __post_init__(self):
        u = urllib.parse.urlsplit(self.upstream)
        self._scheme = u.scheme or "http"
        self._netloc = u.netloc
        self._prefix = u.path.rstrip("/")
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "FaultProxy":
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence the default stderr log
                pass

            def _handle(self):
                proxy._serve(self)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # The app hanging up on us (after a drop or its own timeout) is expected.
                if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
                    super().handle_error(request, client_address)

        self._server = Server((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
                         daemon=True).start()
        self._log(f"[proxy] listening on {self.url} -> {self.upstream}")
        for r in self.rules:
            self._log(f"[proxy] rule: {r.describe()}")
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- internals ---------------------------------------------------------

    def _log(self, line: str) -> None:
        if not self.quiet:
            print(line, flush=True)

    def _match(self, method: str, path: str) -> Rule | None:
        with self._lock:
            for rule in self.rules:
                if rule.matches(method, path):
                    rule.hits += 1
                    return rule
        return None

    def _record(self, method, path, action, status, started) -> None:
        ms = (time.monotonic() - started) * 1000
        with self._lock:
            self.hits.append(Hit(method, path, action, status, ms))
        shown = status if status is not None else "dropped"
        self._log(f"[proxy] {action:5} {method} {path} -> {shown} ({ms:.0f}ms)")

    def _serve(self, h: BaseHTTPRequestHandler) -> None:
        started = time.monotonic()
        method = h.command
        path = urllib.parse.urlsplit(h.path).path
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else None

        rule = self._match(method, path)
        if rule and rule.delay:
            time.sleep(rule.delay)

        if rule and rule.drop:
            h.close_connection = True
            try:
                h.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._record(method, path, "fault", None, started)
            return

        if rule and rule.status:
            sent = self._reply(h, rule.status, [("Content-Type", "application/json")], rule.body.encode())
            self._record(method, path, "fault" if sent else "gone", rule.status, started)
            return

        status, headers, data = self._forward(method, h.path, h.headers, body)
        sent = self._reply(h, status, headers, data)
        self._record(method, path, "pass" if sent else "gone", status, started)

    @staticmethod
    def _reply(h: BaseHTTPRequestHandler, status: int, headers, data: bytes) -> bool:
        """Send a response; False if the app had already hung up (e.g. its own timeout)."""
        try:
            h.send_response(status)
            for k, v in headers:
                if k.lower() not in HOP_BY_HOP:
                    h.send_header(k, v)
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            if h.command != "HEAD":
                h.wfile.write(data)
            h.wfile.flush()
            return True
        except ConnectionError:
            h.close_connection = True
            return False

    def _forward(self, method, raw_path, in_headers, body):
        conn_cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(self._netloc, timeout=30)
        headers = {k: v for k, v in in_headers.items() if k.lower() not in HOP_BY_HOP}
        headers["Host"] = self._netloc
        try:
            conn.request(method, self._prefix + raw_path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.getheaders(), resp.read()
        except OSError as e:
            return 502, [("Content-Type", "application/json")], json.dumps(
                {"error": f"fault_proxy could not reach upstream: {e}"}).encode()
        finally:
            conn.close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upstream", required=True, help="real backend base URL, e.g. http://127.0.0.1:8080")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9090, help="0 picks a free port")
    ap.add_argument("--path", help="path to break, e.g. /inbox (wildcards allowed)")
    ap.add_argument("--method", help="only break this HTTP method")
    action = ap.add_mutually_exclusive_group()
    action.add_argument("--status", type=int, help="answer with this status code (500, 503, ...)")
    action.add_argument("--drop", action="store_true", help="close the connection without a response")
    ap.add_argument("--delay", type=float, default=0.0, help="seconds to wait before answering/forwarding")
    ap.add_argument("--times", type=int, help="only fault the first N matching requests")
    ap.add_argument("--body", default=Rule.body, help="response body for --status")
    args = ap.parse_args(argv)

    rules = []
    if args.path:
        if not (args.status or args.drop or args.delay):
            ap.error("--path needs --status, --drop or --delay")
        rules.append(Rule(path=args.path, status=args.status, drop=args.drop, delay=args.delay,
                          method=args.method, times=args.times, body=args.body))
    proxy = FaultProxy(args.upstream, rules, host=args.host, port=args.port).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        proxy.stop()


if __name__ == "__main__":
    main()
