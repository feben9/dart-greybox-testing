#!/usr/bin/env python3
"""Record every dart:io HTTP request a debug Dart/Flutter app makes, from the first one.

    python3 tools/vm_capture.py --vm http://127.0.0.1:PORT/TOKEN=/ --out capture.jsonl

Start the app paused (``--pause-isolates-on-start`` for dart, ``--start-paused``
for a Flutter app) so the capture is in place before the first request:

  1. find the main isolate and check it is sitting at PauseStart;
  2. switch on the HTTP profiler. While paused, no Dart code has run yet, so
     the service extension can't answer; instead we ``invoke`` the static
     setter ``HttpClient.enableTimelineLogging = true`` on the paused isolate
     (no expression compiler needed);
  3. resume;
  4. call ``ext.dart.io.httpEnableTimelineLogging`` as soon as the isolate
     lists it (the documented switch; harmless if already on);
  5. poll ``ext.dart.io.getHttpProfile`` with ``updatedSince`` and print each
     request once it has finished: method, URI, status, duration.

If step 2 fails, the tool carries on with step 4 alone and says so: requests
made in the few milliseconds before step 4 lands may then be missing.

Standard library only. Uses the VM service's HTTP GET form, no websocket.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from vmservice import TRUE_ID, VmService, VmServiceError

ENABLE_EXT = "ext.dart.io.httpEnableTimelineLogging"


def summarise(r: dict) -> dict:
    """Flatten one HttpProfileRequest into the fields a tester cares about."""
    req = r.get("request") or {}
    resp = r.get("response") or {}
    start = r.get("startTime")
    end = resp.get("endTime") or r.get("endTime")
    error = req.get("error") or resp.get("error") or r.get("error")
    done = error is not None or resp.get("endTime") is not None
    return {
        "id": str(r.get("id")),
        "method": r.get("method"),
        "uri": r.get("uri"),
        "status": resp.get("statusCode"),
        "error": error,
        "start_us": start,
        "ms": round((end - start) / 1000, 1) if (start and end) else None,
        "done": bool(done),
    }


def format_line(s: dict, t0_us: int | None = None) -> str:
    rel = f"+{(s['start_us'] - t0_us) / 1e6:7.3f}s " if (t0_us and s["start_us"]) else ""
    status = s["status"] if s["status"] is not None else ("----" if not s["done"] else "ERR")
    ms = f"{s['ms']:.0f}ms" if s["ms"] is not None else "-"
    tail = f"  ({s['error']})" if s["error"] else ""
    return f"{rel}{s['method']:6} {status!s:>4} {ms:>7}  {s['uri']}{tail}"


class HttpCapture:
    """Polls the HTTP profile of one isolate and keeps one record per request."""

    def __init__(self, vm: VmService, isolate_id: str, out_path: str | None = None,
                 echo: bool = True, path_filter: str | None = None, stall: float = 5.0):
        self.vm = vm
        self.isolate_id = isolate_id
        self.out_path = out_path
        self.echo = echo
        self.path_filter = path_filter
        self.records: dict[str, dict] = {}
        self.early_enable = False
        self.stall = stall
        self._printed: set[str] = set()
        self._stalled: set[str] = set()
        self._since: int | None = None
        self._t0: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._out = open(out_path, "a", encoding="utf-8") if out_path else None

    # -- start-up ------------------------------------------------------------

    def enable_before_resume(self) -> bool:
        """Switch the profiler on while the isolate is still at PauseStart."""
        for cls in self.vm.classes_named(self.isolate_id, "HttpClient", "dart:_http"):
            for selector in ("set:enableTimelineLogging", "enableTimelineLogging="):
                try:
                    self.vm.invoke(self.isolate_id, cls["id"], selector, [TRUE_ID])
                    self.early_enable = True
                    return True
                except VmServiceError:
                    continue
        return False

    def enable_via_extension(self, wait: float = 10.0) -> None:
        deadline = time.monotonic() + wait
        while ENABLE_EXT not in self.vm.extension_rpcs(self.isolate_id):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{ENABLE_EXT} never registered; is this a debug build with dart:io?")
            time.sleep(0.005)
        self.vm.http_logging(self.isolate_id, True)

    def start(self, resume: bool = True) -> "HttpCapture":
        paused = self.vm.wait_until_started(self.isolate_id) == "PauseStart"
        if paused and not self.enable_before_resume():
            self._say("[capture] could not switch logging on before resume; "
                      "the first requests may be missed")
        if paused and resume:
            self.vm.resume(self.isolate_id)
        elif not paused:
            self._say("[capture] isolate was not paused at start; capture begins now")
        self.enable_via_extension()
        return self

    # -- polling ---------------------------------------------------------------

    def poll(self) -> list[dict]:
        """Fetch what changed since the last poll; return newly finished requests."""
        profile = self.vm.http_profile(self.isolate_id, self._since)
        self._since = profile.get("timestamp", self._since)
        finished = []
        with self._lock:
            for raw in profile.get("requests", []):
                s = summarise(raw)
                if self.path_filter and self.path_filter not in (s["uri"] or ""):
                    continue
                self.records[s["id"]] = s
                if self._t0 is None or (s["start_us"] and s["start_us"] < self._t0):
                    self._t0 = s["start_us"]
                if s["done"] and s["id"] not in self._printed:
                    self._printed.add(s["id"])
                    finished.append(s)
            # dart:io records no response and no error when a connection is
            # dropped or the client gives up on a timeout: the entry just stays
            # at "Request sent". Flag those once they have waited `stall` seconds.
            now = profile.get("timestamp")
            stalled = []
            if now:
                for s in self.records.values():
                    if (not s["done"] and s["id"] not in self._stalled and s["start_us"]
                            and now - s["start_us"] > self.stall * 1e6):
                        self._stalled.add(s["id"])
                        s["error"] = f"no response after {self.stall:g}s (dropped, timed out or still waiting)"
                        stalled.append(s)
        for s in sorted(finished + stalled, key=lambda x: x["start_us"] or 0):
            self._say(format_line(s, self._t0))
            if self._out:
                self._out.write(json.dumps(s) + "\n")
                self._out.flush()
        return finished

    def run_in_background(self, interval: float = 0.25) -> "HttpCapture":
        def loop():
            while not self._stop.is_set():
                try:
                    self.poll()
                except (VmServiceError, OSError):
                    if self._stop.is_set():
                        break
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            self.poll()  # pick up the stragglers
        except (VmServiceError, OSError):
            pass
        if self._out:
            self._out.close()
            self._out = None

    # -- queries used by the scenario -----------------------------------------

    def snapshot(self) -> list[dict]:
        with self._lock:
            return sorted(self.records.values(), key=lambda s: s["start_us"] or 0)

    def _say(self, line: str) -> None:
        if self.echo:
            print(line, flush=True)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vm", required=True, help="VM service URL printed by the app")
    ap.add_argument("--out", help="append one JSON line per finished request to this file")
    ap.add_argument("--seconds", type=float, help="stop after this long (default: until Ctrl-C)")
    ap.add_argument("--path", help="only show requests whose URI contains this text")
    ap.add_argument("--no-resume", action="store_true", help="leave a paused isolate paused")
    ap.add_argument("--interval", type=float, default=0.25, help="poll interval in seconds")
    ap.add_argument("--stall", type=float, default=5.0,
                    help="report a request as 'no response' after this many seconds")
    args = ap.parse_args(argv)

    vm = VmService(args.vm)
    isolate = vm.main_isolate()
    cap = HttpCapture(vm, isolate, out_path=args.out, path_filter=args.path, stall=args.stall)
    cap.start(resume=not args.no_resume)
    print(f"[capture] isolate {isolate}, logging on "
          f"({'before' if cap.early_enable else 'after'} resume)", file=sys.stderr, flush=True)
    cap.run_in_background(args.interval)
    try:
        if args.seconds:
            time.sleep(args.seconds)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        cap.stop()
        print(f"[capture] {len(cap.records)} requests recorded", file=sys.stderr)


if __name__ == "__main__":
    main()
