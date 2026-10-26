"""Why the capture switches logging on while the app is still paused.

demo_app/bin/first_request.dart opens its first request synchronously at the
top of main. A service extension call can only run once the app yields, so:

  * resume, then call ext.dart.io.httpEnableTimelineLogging -> the first
    request is missing from the profile;
  * invoke HttpClient.enableTimelineLogging = true while paused, then resume
    -> all four requests are there.
"""

import shutil

import pytest

from scenario import endpoint, start_backend, start_paused
from vm_capture import HttpCapture

pytestmark = pytest.mark.skipif(shutil.which("dart") is None, reason="needs the Dart SDK on PATH")


def captured_paths(early: bool) -> list[str]:
    backend, url = start_backend()
    app = None
    try:
        app, vm, iso = start_paused("bin/first_request.dart", [url], name="first_request")
        cap = HttpCapture(vm, iso, echo=False)
        if early:
            cap.start()  # invoke the static setter while paused, then resume
            assert cap.early_enable
        else:
            vm.resume(iso)
            cap.enable_via_extension()
        app.wait_for(r"\[first_request\] sent 4", 30)
        cap.poll()
        return [endpoint(r["uri"]) for r in cap.snapshot() if r["done"]]
    finally:
        for p in (app, backend):
            if p:
                p.stop()


def test_enable_after_resume_misses_the_first_request():
    assert captured_paths(early=False) == ["inbox", "inbox", "inbox"]


def test_enable_while_paused_catches_every_request():
    assert captured_paths(early=True) == ["orders", "inbox", "inbox", "inbox"]
