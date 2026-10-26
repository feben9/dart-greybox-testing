#!/usr/bin/env python3
"""End-to-end scenario: break one endpoint of a live app and watch it cope.

    python3 tools/scenario.py            # needs `dart` on PATH
    python3 tools/scenario.py --quiet --artifacts artifacts/

Steps
  1. start the backend on a free port;
  2. start the demo app paused, with the VM service on;
  3. start the HTTP capture (logging on before the first request), resume;
  4. baseline: all three screens load, and the capture holds every request
     the app says it sent;
  5. start fault_proxy with "/inbox -> 500" and repoint the app's dio client
     at it;
  6. expect: inbox shows "failed to load (HTTP 500)" after exactly 3 retries,
     while orders and profile keep loading through the proxy;
  7. repoint back to the backend; expect inbox to recover.

Prints one line per check and exits non-zero if any check fails. The pytest
test in tests/test_scenario.py calls run() and asserts on the same checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from fault_proxy import FaultProxy, Rule
from repoint import repoint
from vm_capture import HttpCapture
from vmservice import VmService, find_service_url

ROOT = Path(__file__).resolve().parent.parent
CYCLE_RE = re.compile(r"^\[cycle (\d+)\] orders=(\S+) inbox=(.+?) profile=(\S+) requests=(\d+)$")


class Proc:
    """A child process whose output lines are collected (and optionally echoed)."""

    def __init__(self, name: str, cmd: list[str], cwd: Path, echo: bool):
        self.name = name
        self.lines: list[str] = []
        self._cv = threading.Condition()
        self.p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1, start_new_session=True)
        threading.Thread(target=self._read, args=(echo,), daemon=True).start()

    def _read(self, echo: bool) -> None:
        for raw in self.p.stdout:
            line = raw.rstrip("\n")
            with self._cv:
                self.lines.append(line)
                self._cv.notify_all()
            if echo:
                print(f"  {self.name:7}| {line}", flush=True)

    def mark(self) -> int:
        with self._cv:
            return len(self.lines)

    def wait_for(self, test, timeout: float, since: int = 0):
        """Wait for a line after index ``since``. ``test`` is a regex or a callable."""
        if isinstance(test, str):
            rx = re.compile(test)
            test = lambda line: rx.search(line)  # noqa: E731
        deadline = time.monotonic() + timeout
        i = since
        with self._cv:
            while True:
                while i < len(self.lines):
                    hit = test(self.lines[i])
                    if hit:
                        return hit, i
                    i += 1
                left = deadline - time.monotonic()
                if left <= 0 or self.p.poll() is not None and i >= len(self.lines):
                    tail = "\n".join(self.lines[-15:])
                    raise TimeoutError(f"{self.name}: no matching line within {timeout}s. Last lines:\n{tail}")
                self._cv.wait(min(left, 0.2))

    def stop(self) -> None:
        if self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGTERM)
                self.p.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class Checks:
    def __init__(self, echo: bool):
        self.items: list[dict] = []
        self.echo = echo

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append({"check": name, "ok": bool(ok), "detail": detail})
        if self.echo:
            print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)
        return ok

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.items if not c["ok"]]


def cycles(lines: list[str], since: int = 0) -> list[tuple[int, re.Match]]:
    out = []
    for i in range(since, len(lines)):
        m = CYCLE_RE.match(lines[i])
        if m:
            out.append((i, m))
    return out


def endpoint(uri: str | None) -> str:
    return (uri or "").split("?")[0].rsplit("/", 1)[-1]


def start_backend(echo: bool = False) -> tuple[Proc, str]:
    """Start backend/ on a free port; return the process and its base URL."""
    proc = Proc("backend", ["dart", "run", "bin/server.dart", "--port", "0", "--quiet"],
                ROOT / "backend", echo)
    m, _ = proc.wait_for(r"listening on (http://\S+)", 120)
    return proc, m.group(1)


def start_paused(script: str, args: list[str], echo: bool = False,
                 name: str = "app") -> tuple[Proc, VmService, str]:
    """Run a demo_app script paused at start; return (process, VM service, isolate id)."""
    proc = Proc(name, ["dart", "run", "--enable-vm-service=0", "--pause-isolates-on-start",
                       "--no-serve-devtools", script, *args], ROOT / "demo_app", echo)
    vm_url, _ = proc.wait_for(find_service_url, 180)
    vm = VmService(vm_url)
    iso = vm.main_isolate()
    vm.wait_for_pause_start(iso)
    return proc, vm, iso


def run(echo: bool = True, artifacts: Path | None = None) -> Checks:
    checks = Checks(echo)
    say = (lambda s: print(s, flush=True)) if echo else (lambda s: None)
    backend = app = cap = proxy = None
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)
    try:
        # 1. backend
        say("== start backend")
        backend, backend_url = start_backend(echo)

        # 2. app, paused at start
        say("== start app paused")
        app, vm, iso = start_paused("bin/main.dart", ["--base-url", backend_url], echo)
        checks.check("app is paused at start before any request", vm.pause_kind(iso) == "PauseStart")

        # 3. capture from the first request
        say("== capture on, resume")
        out = (artifacts / "capture.jsonl") if artifacts else None
        if out and out.exists():
            out.unlink()
        cap = HttpCapture(vm, iso, out_path=str(out) if out else None, echo=echo).start()
        checks.check("HTTP logging switched on before resume", cap.early_enable)
        cap.run_in_background(0.2)

        # 4. baseline
        say("== baseline")
        _, idx = app.wait_for(r"^\[cycle 2\] ", 30)
        cap.poll()  # the app sleeps ~1s after a cycle line, so nothing is in flight now
        m = CYCLE_RE.match(app.lines[idx])
        sent = int(m.group(5))
        done = [r for r in cap.snapshot() if r["done"]]
        checks.check("baseline: every screen loads",
                     m.group(2) == "ok" and m.group(3) == "ok" and m.group(4) == "ok", app.lines[idx])
        checks.check("capture holds every request from the first one",
                     len(done) == sent, f"app sent {sent}, captured {len(done)}")
        first = done[:3]
        checks.check("first captured requests are the first screen loads (200)",
                     sorted(endpoint(r["uri"]) for r in first) == ["inbox", "orders", "profile"]
                     and all(r["status"] == 200 for r in first),
                     ", ".join(f"{endpoint(r['uri'])}={r['status']}" for r in first))

        # 5. fault: /inbox -> 500, everything else passes through
        say("== inject 500 on /inbox")
        proxy = FaultProxy(backend_url, [Rule("/inbox", status=500)], quiet=not echo).start()
        mark = app.mark()
        changes = repoint(vm, iso, proxy.url, from_=backend_url)
        checks.check("repoint: dio BaseOptions.baseUrl now points at the proxy",
                     len(changes) == 1 and changes[0]["new"] == proxy.url,
                     f"via {changes[0]['setter']}" if changes else "no change")

        app.wait_for(r"^\[ui\] inbox: failed to load \(HTTP 500\)$", 20, since=mark)
        _, failed_idx = app.wait_for(lambda l: (c := CYCLE_RE.match(l)) and c.group(3) == "HTTP 500",
                                     20, since=mark)
        prev = [i for i, _ in cycles(app.lines, mark) if i < failed_idx]
        start_idx = prev[-1] if prev else mark
        retries = [l for l in app.lines[start_idx:failed_idx] if l.startswith("[retry] inbox: HTTP 500")]
        failed_cycle = CYCLE_RE.match(app.lines[failed_idx])
        checks.check("inbox shows its failed state", True, "[ui] inbox: failed to load (HTTP 500)")
        checks.check("inbox retried exactly 3 times before giving up", len(retries) == 3,
                     f"{len(retries)} retries: " + " | ".join(r.split(", ", 1)[1] for r in retries))
        checks.check("orders and profile keep loading during the fault",
                     failed_cycle.group(2) == "ok" and failed_cycle.group(4) == "ok", app.lines[failed_idx])

        time.sleep(0.5)
        cap.poll()
        via_proxy = [r for r in cap.snapshot() if (r["uri"] or "").startswith(proxy.url)]
        inbox_500 = [r for r in via_proxy if endpoint(r["uri"]) == "inbox" and r["status"] == 500]
        orders_ok = [r for r in via_proxy if endpoint(r["uri"]) == "orders" and r["status"] == 200]
        checks.check("capture: /inbox -> 500 via the proxy, at least 1 + 3 retries", len(inbox_500) >= 4,
                     f"{len(inbox_500)} x 500")
        checks.check("capture: /orders -> 200 via the proxy (real backend reached)", len(orders_ok) >= 1,
                     f"{len(orders_ok)} x 200")
        proxied_inbox = [h for h in proxy.hits if h.path == "/inbox"]
        checks.check("proxy: only /inbox was faulted",
                     all(h.action == "fault" for h in proxied_inbox)
                     and all(h.action == "pass" for h in proxy.hits if h.path != "/inbox"),
                     f"{len(proxy.hits)} proxied, {len(proxied_inbox)} faulted")

        # 7. restore
        say("== restore")
        mark = app.mark()
        back = repoint(vm, iso, backend_url, from_=proxy.url)
        checks.check("repoint back to the real backend", len(back) == 1 and back[0]["new"] == backend_url)
        app.wait_for(r"^\[ui\] inbox: 2 unread$", 20, since=mark)
        _, ok_idx = app.wait_for(lambda l: (c := CYCLE_RE.match(l)) and c.group(3) == "ok", 20, since=mark)
        checks.check("inbox recovers after restore", True, app.lines[ok_idx])
        time.sleep(0.3)
        cap.poll()
        last_inbox = [r for r in cap.snapshot() if endpoint(r["uri"]) == "inbox" and r["done"]][-1]
        checks.check("capture: latest /inbox -> 200 from the real backend",
                     last_inbox["uri"].startswith(backend_url) and last_inbox["status"] == 200,
                     f"{last_inbox['uri']} {last_inbox['status']}")
    except Exception as e:  # report, don't hide: a crash is a failed check
        checks.check("scenario ran to the end", False, f"{type(e).__name__}: {e}")
    finally:
        if cap:
            cap.stop()
        if proxy:
            proxy.stop()
        for p in (app, backend):
            if p:
                p.stop()
        if artifacts:
            artifacts.mkdir(parents=True, exist_ok=True)
            for p in (app, backend):
                if p:
                    (artifacts / f"{p.name}.log").write_text("\n".join(p.lines) + "\n")
            (artifacts / "checks.json").write_text(json.dumps(checks.items, indent=2) + "\n")
    return checks


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true", help="only print the checks")
    ap.add_argument("--artifacts", type=Path, help="write logs, capture.jsonl and checks.json here")
    args = ap.parse_args(argv)
    checks = run(echo=not args.quiet, artifacts=args.artifacts)
    if args.quiet:
        for c in checks.items:
            print(f"{'PASS' if c['ok'] else 'FAIL'}  {c['check']}" + (f"  [{c['detail']}]" if c["detail"] else ""))
    total, bad = len(checks.items), len(checks.failed)
    print(f"\n{total - bad}/{total} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
