"""The whole technique, end to end, against the real Dart VM (needs `dart` on PATH)."""

import shutil
import time
from pathlib import Path

import pytest

import scenario
from fault_proxy import FaultProxy, Rule
from repoint import repoint
from vm_capture import HttpCapture

ARTIFACTS = Path(__file__).resolve().parent.parent / "artifacts"
pytestmark = pytest.mark.skipif(shutil.which("dart") is None, reason="needs the Dart SDK on PATH")


def test_inbox_500_retries_fails_and_recovers():
    checks = scenario.run(echo=False, artifacts=ARTIFACTS)
    report = "\n".join(
        f"{'PASS' if c['ok'] else 'FAIL'}  {c['check']}  {c['detail']}" for c in checks.items
    )
    print(report)
    assert not checks.failed, "failed checks:\n" + report
    assert len(checks.items) >= 14


def test_dropped_connection_shows_as_no_response():
    """--drop: the app reports "connection lost"; the profile has no response and no error."""
    backend, backend_url = scenario.start_backend()
    app = cap = proxy = None
    try:
        app, vm, iso = scenario.start_paused("bin/main.dart", ["--base-url", backend_url])
        cap = HttpCapture(vm, iso, echo=False, stall=1.0).start()
        app.wait_for(r"^\[cycle 1\] ", 30)

        proxy = FaultProxy(backend_url, [Rule("/inbox", drop=True)], quiet=True).start()
        mark = app.mark()
        repoint(vm, iso, proxy.url, from_=backend_url)
        app.wait_for(r"^\[ui\] inbox: failed to load \(connection lost\)$", 20, since=mark)
        retries = [line for line in app.lines[mark:] if line.startswith("[retry] inbox: connection lost")]
        assert len(retries) == 3

        time.sleep(1.5)
        cap.poll()
        dropped = [r for r in cap.snapshot() if (r["uri"] or "").startswith(proxy.url + "/inbox")]
        assert len(dropped) >= 4
        assert all(r["status"] is None and not r["done"] for r in dropped)
        first_cycle = dropped[:4]  # later attempts may not have waited `stall` seconds yet
        assert all("no response" in (r["error"] or "") for r in first_cycle)
        orders = [r for r in cap.snapshot() if (r["uri"] or "").startswith(proxy.url + "/orders")]
        assert any(r["status"] == 200 for r in orders)
    finally:
        if cap:
            cap.stop()
        if proxy:
            proxy.stop()
        for p in (app, backend):
            if p:
                p.stop()
