#!/usr/bin/env python3
"""Point a running debug app's HTTP client at another base URL, and back.

    # what clients are there, and where do they point?
    python3 tools/repoint.py --vm http://127.0.0.1:PORT/TOKEN=/ --list

    # send the app's traffic through the fault proxy
    python3 tools/repoint.py --vm ... --to http://127.0.0.1:9090

    # put it back (the tool prints this line for you)
    python3 tools/repoint.py --vm ... --from http://127.0.0.1:9090 --to http://127.0.0.1:8080

How it works, using only VM service RPCs that run without an expression
compiler (so they also work on a debug app on a device):

  1. Find the class. ``getClassList`` lists every loaded class; we keep the
     ones named ``--class`` (default ``BaseOptions``) from a library whose URI
     contains ``--library`` (default ``package:dio/``).
  2. Find the live objects. ``getInstances`` on that class returns the
     instances currently on the heap: one per Dio client the app built.
  3. Read the current value. ``invoke`` the getter (``get:baseUrl``) on each
     instance; this runs the app's own getter and returns a String ref.
  4. Build the new URL inside the app. ``invoke`` only accepts arguments that
     are object ids in the app's heap, and you can't send a Python string. We
     build it from things the VM can hand us: small ints have fixed ids
     (``objects/int-9090``), ``9090.toRadixString(36)`` turns one into text,
     ``Uri.decodeComponent("%3A")`` makes any symbol, ``substring`` reuses the
     part of the old URL that doesn't change, and ``+`` joins the pieces
     (see StringBuilder; ``String.fromCharCode`` is a factory constructor,
     which ``invoke`` can't call).
  5. ``invoke`` the setter (``set:baseUrl``) with that String's id, then read
     the getter again to confirm.

Every dio request reads ``options.baseUrl`` when it is sent, so the very next
request goes to the new URL. Nothing is restarted and no app code changes.
Standard library only.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from vmservice import VmService, VmServiceError, int_id

SETTER_SELECTORS = ("set:{p}", "{p}=")
GETTER_SELECTORS = ("get:{p}", "{p}")


@dataclass
class Target:
    instance_id: str
    class_name: str
    value: str


LOWER36 = set("0123456789abcdefghijklmnopqrstuvwxyz")
UPPER36 = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
CHUNK = 5  # 36**5 < 2**30: stays a Smi even on 32-bit / compressed-pointer devices


def plan_string(text: str, seed: str = "") -> list[tuple]:
    """Split ``text`` into pieces the VM can produce without a compiler.

    ("seed", start, end)  reuse seed[start:end] via String.substring
    ("radix", "a1b")      int.toRadixString(36): a run of [0-9a-z]
    ("upper", "abc")      the same, then String.toUpperCase: a run of [0-9A-Z]
    ("pct", "_")          anything else: Uri.decodeComponent("%5F")
    """
    pieces, i = [], 0
    while i < len(text):
        # longest run that already exists in the seed string
        length, at = 0, -1
        for n in range(len(text) - i, 0, -1):
            at = seed.find(text[i:i + n]) if seed else -1
            if at >= 0:
                length = n
                break
        c = text[i]
        if length >= 2 or (length == 1 and c not in LOWER36 and c not in UPPER36):
            pieces.append(("seed", at, at + length))
            i += length
            continue
        for kind, alphabet in (("radix", LOWER36), ("upper", UPPER36)):
            if c in alphabet and (kind == "radix" or not c.isdigit()):
                run = c
                if c != "0":  # a leading zero would be lost in the int
                    while (i + len(run) < len(text) and len(run) < CHUNK
                           and text[i + len(run)] in alphabet):
                        run += text[i + len(run)]
                pieces.append((kind, run if kind == "radix" else run.lower()))
                i += len(run)
                break
        else:
            if pieces and pieces[-1][0] == "pct":  # one decode for a run of symbols
                pieces[-1] = ("pct", pieces[-1][1] + c)
            else:
                pieces.append(("pct", c))
            i += 1
    return pieces


class StringBuilder:
    """Makes a String inside the app's isolate using only ``invoke``.

    ``invoke`` arguments must be ids of objects already in the app, and
    factory constructors such as ``String.fromCharCode`` can't be invoked.
    What *can* be done without a compiler:

    * ``objects/int-N`` is a valid id for any small int, no heap object needed;
    * ``N.toRadixString(36)`` turns an int into a run of ``[0-9a-z]``;
    * ``.toUpperCase()``, ``.substring(a, b)`` and ``+`` work on any String;
    * ``Uri.decodeComponent("%3A")`` (a static method) makes any character.
      The ``%`` itself comes from ``Uri.encodeComponent(5.toStringAsExponential())``
      = ``"5e%2B0"``.
    """

    def __init__(self, vm: VmService, isolate_id: str):
        self.vm = vm
        self.iso = isolate_id
        self.calls = 0
        self._uri_class: str | None = None
        self._percent: str | None = None

    def _invoke(self, target, selector, args=()) -> dict:
        self.calls += 1
        return self.vm.invoke(self.iso, target, selector, list(args))

    def _id(self, target, selector, args=()) -> str:
        return self._invoke(target, selector, args)["id"]

    def _uri(self) -> str:
        if self._uri_class is None:
            found = self.vm.classes_named(self.iso, "Uri", "dart:core")
            if not found:
                raise RuntimeError("dart:core Uri class not found")
            self._uri_class = found[0]["id"]
        return self._uri_class

    def _percent_sign(self) -> str:
        if self._percent is None:
            exp = self._id(int_id(5), "toStringAsExponential")      # "5e+0"
            enc = self._id(self._uri(), "encodeComponent", [exp])   # "5e%2B0"
            self._percent = self._id(enc, "substring", [int_id(2), int_id(3)])  # "%"
        return self._percent

    def _piece(self, piece: tuple, seed_id: str | None) -> str:
        kind = piece[0]
        if kind == "seed":
            return self._id(seed_id, "substring", [int_id(piece[1]), int_id(piece[2])])
        if kind in ("radix", "upper"):
            sid = self._id(int_id(int(piece[1], 36)), "toRadixString", [int_id(36)])
            return self._id(sid, "toUpperCase") if kind == "upper" else sid
        # pct: "%XX%YY..." for the UTF-8 bytes, then Uri.decodeComponent
        encoded = None
        for byte in piece[1].encode("utf-8"):
            hex_id = self._id(int_id(byte), "toRadixString", [int_id(16)])
            if byte < 16:
                hex_id = self._id(self._id(int_id(0), "toString"), "+", [hex_id])
            part = self._id(self._percent_sign(), "+", [hex_id])
            encoded = part if encoded is None else self._id(encoded, "+", [part])
        return self._id(self._uri(), "decodeComponent", [encoded])

    def build(self, text: str, seed_id: str | None = None, seed_text: str = "") -> str:
        """Id of a String equal to ``text``, reusing parts of ``seed`` (e.g. the old URL)."""
        if seed_id and text == seed_text:
            return seed_id
        current = None
        for piece in plan_string(text, seed_text if seed_id else ""):
            pid = self._piece(piece, seed_id)
            current = pid if current is None else self._id(current, "+", [pid])
        if current is None:  # empty string
            current = self._id(self._id(int_id(0), "toString"), "substring", [int_id(0), int_id(0)])
        return current


def _invoke_first(vm, iso, target, selectors, args=()):
    last = None
    for sel in selectors:
        try:
            return sel, vm.invoke(iso, target, sel, list(args))
        except VmServiceError as e:
            last = e
    raise last


def read_property(vm: VmService, iso: str, instance_id: str, prop: str) -> tuple[str, str]:
    """(value, string id) of a String property, via its getter, falling back to fields."""
    try:
        _, res = _invoke_first(vm, iso, instance_id, [s.format(p=prop) for s in GETTER_SELECTORS])
        if res.get("kind") == "String" and not res.get("valueAsStringIsTruncated"):
            return res.get("valueAsString", ""), res["id"]
    except VmServiceError:
        pass
    fields = vm.string_fields(iso, instance_id)
    for name in (prop, "_" + prop):
        if name in fields:
            return fields[name], None
    raise RuntimeError(f"{instance_id} has no readable String property '{prop}'")


def find_targets(vm: VmService, iso: str, class_name: str, library: str | None, prop: str) -> list[Target]:
    targets = []
    for cls in vm.classes_named(iso, class_name, library):
        for inst in vm.instances_of(iso, cls["id"]):
            try:
                value, _ = read_property(vm, iso, inst["id"], prop)
            except RuntimeError:
                continue
            targets.append(Target(inst["id"], cls["name"], value))
    return targets


def repoint(vm: VmService, iso: str, to: str, *, from_: str | None = None,
            class_name: str = "BaseOptions", library: str | None = "package:dio/",
            prop: str = "baseUrl") -> list[dict]:
    """Set ``prop`` to ``to`` on every matching live instance. Returns what changed."""
    targets = find_targets(vm, iso, class_name, library, prop)
    if from_ is not None:
        targets = [t for t in targets if t.value == from_]
    if not targets:
        raise RuntimeError(f"no live {class_name} with {prop}"
                           + (f" == {from_!r}" if from_ else "") + " found")
    builder = StringBuilder(vm, iso)
    changes = []
    for t in targets:
        calls_before = builder.calls
        old_value, old_id = read_property(vm, iso, t.instance_id, prop)
        new_id = builder.build(to, old_id, old_value if old_id else "")
        selector, _ = _invoke_first(vm, iso, t.instance_id,
                                    [s.format(p=prop) for s in SETTER_SELECTORS], [new_id])
        now, _ = read_property(vm, iso, t.instance_id, prop)
        if now != to:
            raise RuntimeError(f"setter ran but {prop} reads {now!r}, expected {to!r}")
        changes.append({"instance": t.instance_id, "class": t.class_name, "old": old_value,
                        "new": now, "setter": selector, "invokes": builder.calls - calls_before + 1})
    return changes


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vm", required=True, help="VM service URL printed by the app")
    ap.add_argument("--list", action="store_true", help="list matching live objects and exit")
    ap.add_argument("--to", help="new base URL")
    ap.add_argument("--from", dest="from_", help="only change objects currently pointing here")
    ap.add_argument("--class", dest="class_name", default="BaseOptions", help="class to look for")
    ap.add_argument("--library", default="package:dio/", help="library URI must contain this ('' = any)")
    ap.add_argument("--property", default="baseUrl", help="String property to change")
    args = ap.parse_args(argv)

    vm = VmService(args.vm)
    iso = vm.main_isolate()
    library = args.library or None
    if vm.wait_until_started(iso) == "PauseStart":
        sys.exit("the app is still paused at start: resume it first (vm_capture.py does)")

    if args.list or not args.to:
        for t in find_targets(vm, iso, args.class_name, library, args.property):
            print(f"{t.instance_id:24} {t.class_name}.{args.property} = {t.value}")
        return

    try:
        changes = repoint(vm, iso, args.to, from_=args.from_, class_name=args.class_name,
                          library=library, prop=args.property)
    except (RuntimeError, VmServiceError) as e:
        sys.exit(f"repoint failed: {e}")
    for c in changes:
        print(f"{c['instance']} {c['class']}.{args.property}: {c['old']} -> {c['new']} "
              f"(via {c['setter']}, {c['invokes']} invokes)")
        print(f"revert: python3 tools/repoint.py --vm {args.vm} --from {c['new']} --to {c['old']}")


if __name__ == "__main__":
    main()
