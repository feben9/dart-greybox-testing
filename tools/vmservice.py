"""Minimal Dart VM service client. Python 3 standard library only.

The VM service speaks JSON-RPC over a websocket, but it also answers plain
HTTP GET requests of the form

    http://127.0.0.1:PORT/TOKEN=/getVM
    http://127.0.0.1:PORT/TOKEN=/getIsolate?isolateId=isolates/123

which is all this module uses. No websocket library, no pip install.

Two encoding rules matter when you go through the GET form:
  * every parameter arrives in the VM as a string, so booleans are sent as
    "true"/"false";
  * list parameters (``argumentIds`` for ``invoke``) are parsed by the VM as
    "[a, b, c]": square brackets, comma + space between items, no quotes.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE_URL_RE = re.compile(r"(?:https?|wss?)://[\w.\-\[\]:]+/[^\s]*?=/")


class VmServiceError(RuntimeError):
    def __init__(self, method: str, error: dict):
        self.method = method
        self.error = error
        self.code = error.get("code")
        details = (error.get("data") or {}).get("details", "")
        super().__init__(f"{method}: {error.get('message')} {details}".strip())


def find_service_url(text: str) -> str | None:
    """Pull the VM service URL out of an app log line.

    Matches e.g. "The Dart VM service is listening on http://127.0.0.1:41234/AbC=/".
    """
    m = SERVICE_URL_RE.search(text)
    return m.group(0) if m else None


def _encode(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def int_id(n: int) -> str:
    """Object id for a small int. The VM resolves these without a heap object."""
    return f"objects/int-{n}"


TRUE_ID = "objects/bool-true"
FALSE_ID = "objects/bool-false"
NULL_ID = "objects/null"


class VmService:
    def __init__(self, url: str, timeout: float = 10.0):
        url = url.strip()
        if url.startswith("ws"):
            url = "http" + url[2:]
        if url.endswith("/ws"):
            url = url[:-2]
        if not url.endswith("/"):
            url += "/"
        self.base = url
        self.timeout = timeout
        self._classes: dict[str, list[dict]] = {}

    # -- transport ---------------------------------------------------------

    def call(self, method: str, **params):
        query = urllib.parse.urlencode(
            {k: _encode(v) for k, v in params.items() if v is not None}
        )
        url = self.base + method + ("?" + query if query else "")
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:  # the VM answers errors with 200, DDS may not
            body = json.loads(e.read() or b"{}")
        if "error" in body:
            raise VmServiceError(method, body["error"])
        return body.get("result", body)

    # -- isolates ----------------------------------------------------------

    def main_isolate(self, wait: float = 10.0) -> str:
        """Id of the app's main isolate (waits for it to appear)."""
        deadline = time.monotonic() + wait
        while True:
            isolates = self.call("getVM").get("isolates", [])
            if isolates:
                named = [i for i in isolates if i.get("name") == "main"]
                return (named or isolates)[0]["id"]
            if time.monotonic() > deadline:
                raise TimeoutError("no isolate appeared on the VM service")
            time.sleep(0.05)

    def pause_kind(self, isolate_id: str) -> str | None:
        return (self.call("getIsolate", isolateId=isolate_id).get("pauseEvent") or {}).get("kind")

    def wait_until_started(self, isolate_id: str, wait: float = 10.0) -> str | None:
        """Wait out the moment after launch when the isolate exists but isn't runnable.

        Right after start-up the isolate reports runnable=false and pause kind
        "None" for a few milliseconds before it reaches PauseStart. Returns the
        settled pause kind.
        """
        deadline = time.monotonic() + wait
        runnable_since = None
        while True:
            iso = self.call("getIsolate", isolateId=isolate_id)
            kind = (iso.get("pauseEvent") or {}).get("kind")
            if iso.get("runnable") and runnable_since is None:
                runnable_since = time.monotonic()
            if kind == "PauseStart" or (kind not in (None, "None") and iso.get("runnable")):
                return kind
            if runnable_since is not None and time.monotonic() - runnable_since > 0.5:
                return kind
            if time.monotonic() > deadline:
                raise TimeoutError("isolate never became runnable")
            time.sleep(0.02)

    def wait_for_pause_start(self, isolate_id: str, wait: float = 10.0) -> None:
        deadline = time.monotonic() + wait
        while self.pause_kind(isolate_id) != "PauseStart":
            if time.monotonic() > deadline:
                raise TimeoutError("isolate never reached PauseStart; was the app started paused?")
            time.sleep(0.05)

    def resume(self, isolate_id: str) -> None:
        self.call("resume", isolateId=isolate_id)

    def extension_rpcs(self, isolate_id: str) -> list[str]:
        return self.call("getIsolate", isolateId=isolate_id).get("extensionRPCs") or []

    # -- objects -----------------------------------------------------------

    def get_object(self, isolate_id: str, object_id: str) -> dict:
        return self.call("getObject", isolateId=isolate_id, objectId=object_id)

    def classes_named(self, isolate_id: str, name: str, library_part: str | None = None) -> list[dict]:
        """ClassRefs whose name matches, optionally filtered by library URI."""
        if isolate_id not in self._classes or not any(
                c.get("name") == name for c in self._classes[isolate_id]):
            # getClassList is large; cache it, refetch if a class isn't there yet
            self._classes[isolate_id] = self.call("getClassList", isolateId=isolate_id).get("classes", [])
        out = []
        for cls in self._classes[isolate_id]:
            if cls.get("name") != name:
                continue
            if library_part:
                lib = cls.get("library") or self.get_object(isolate_id, cls["id"]).get("library") or {}
                if library_part not in (lib.get("uri") or ""):
                    continue
            out.append(cls)
        return out

    def instances_of(self, isolate_id: str, class_id: str, limit: int = 50) -> list[dict]:
        res = self.call("getInstances", isolateId=isolate_id, objectId=class_id, limit=limit)
        return res.get("instances", [])

    def invoke(self, isolate_id: str, target_id: str, selector: str, args: list[str] = ()) -> dict:
        """Call ``selector`` on a live object (or class / library) with object-id args.

        Unlike ``evaluate``, ``invoke`` needs no expression compiler, so it
        works against a debug app on a device. The result is an InstanceRef,
        or an ErrorRef if the Dart code threw.
        """
        res = self.call(
            "invoke",
            isolateId=isolate_id,
            targetId=target_id,
            selector=selector,
            argumentIds=list(args),
            disableBreakpoints=True,
        )
        if res.get("type") in ("@Error", "Error"):
            raise VmServiceError("invoke " + selector, {"message": res.get("message", "error")})
        return res

    def string_fields(self, isolate_id: str, object_id: str) -> dict[str, str]:
        """String-valued fields of an instance, read without running Dart code."""
        obj = self.get_object(isolate_id, object_id)
        out = {}
        for f in obj.get("fields", []):
            name = f.get("name") or (f.get("decl") or {}).get("name")
            value = f.get("value") or {}
            if isinstance(value, dict) and value.get("kind") == "String":
                out[str(name)] = value.get("valueAsString", "")
        return out

    # -- dart:io HTTP profiler ---------------------------------------------

    def http_logging(self, isolate_id: str, enabled: bool = True) -> dict:
        return self.call("ext.dart.io.httpEnableTimelineLogging", isolateId=isolate_id, enabled=enabled)

    def http_profile(self, isolate_id: str, updated_since: int | None = None) -> dict:
        return self.call("ext.dart.io.getHttpProfile", isolateId=isolate_id, updatedSince=updated_since)
