# Grey-box HTTP testing for Flutter apps, without a proxy or root

Flutter apps ignore the phone's proxy settings, so Charles, Proxyman and Burp
see nothing. On an unrooted Android device you can't force the traffic through
them either. That leaves a gap in a lot of test plans: *what does the app do
when one endpoint returns a 500, hangs, or drops the connection?* Those cases
often end up marked "can't test".

This repo shows the technique I use to close that gap on **debug builds**. It
uses the Dart VM service, the same debugging port DevTools connects to:

1. **Watch every request.** The `dart:io` HTTP profiler records each request
   the app makes (method, URL, status, timing). Switch it on before the app's
   first line runs, then poll it.
2. **Repoint one object in the running app.** The VM service's `invoke` RPC can
   call a getter, setter or method on a live object. I call the setter of the
   HTTP client's `baseUrl` so the app talks to a local proxy instead of the
   real server.
3. **Break one endpoint on purpose.** The local proxy forwards everything to
   the real server unchanged, except one path, which gets a 500, a 503, a
   dropped connection or a delay.

Then I watch the app's user-visible state, its retries, and the captured
traffic, put the URL back, and check that it recovers.

Everything here runs locally: a small Dart client stands in for the Flutter
app, a tiny Dart backend stands in for the server, and Python scripts (standard
library only) do the grey-box work. The end-to-end scenario runs in CI.

```
demo_app/
  bin/main.dart           "orders / inbox / profile" client (dio, retries with backoff)
  bin/first_request.dart  worst case for capture: first request at the top of main
backend/                  tiny dart:io JSON server: /orders /inbox /profile
tools/
  vmservice.py   VM service client over plain HTTP GET (no websocket library)
  vm_capture.py  HTTP profiler capture from the first request
  fault_proxy.py pass-through proxy that breaks one path
  repoint.py     find the live dio BaseOptions and invoke its baseUrl setter
  scenario.py    the whole thing end to end, with checks
tests/
  test_fault_proxy.py   proxy unit tests (no Dart needed)
  test_scenario.py      end-to-end: 500 on /inbox, and a dropped connection
  test_capture_race.py  why logging is switched on while the app is paused
```

## Why the usual proxy tools don't work

- Flutter's networking is `dart:io`, which has its own socket and TLS stack.
  It doesn't read the Android or iOS system proxy, so a Wi-Fi proxy setting
  changes nothing.
- Even with traffic forced through a proxy, it would need to trust the proxy's
  certificate. On Android 7+ apps don't trust user-installed CAs by default,
  and Flutter doesn't read the user store at all. Fixing either needs root, a
  rebuilt app, or code changes in the client.
- Patching the app (a debug menu to switch servers, a mock flag) works, but
  only for the cases someone thought of in advance, and it isn't the build
  you were given to test.

A debug build already contains a better hook: the VM service.

## The technique, step by step

### 0. Start the app paused

The capture has to be in place before the first request, and real apps call
their backend in the first few hundred milliseconds. So start the app paused:

```sh
dart run --enable-vm-service=0 --pause-isolates-on-start bin/main.dart --base-url http://127.0.0.1:8080
# The Dart VM service is listening on http://127.0.0.1:61523/kD9...=/
```

For a Flutter debug build, see [On a real Flutter app](#on-a-real-flutter-app-android).

### 1. Capture every request, from the first one

The VM service answers JSON-RPC over a websocket, and also plain HTTP GET:

```sh
curl -s 'http://127.0.0.1:61523/kD9...=/getVM'
curl -s 'http://127.0.0.1:61523/kD9...=/getIsolate?isolateId=isolates/123'
```

That is all `tools/vmservice.py` uses, so the tools need nothing beyond
Python 3.

The documented switch for the HTTP profiler is the service extension
`ext.dart.io.httpEnableTimelineLogging`. The extension is already listed while
the app is paused at start, but it is Dart code running in the app, so a call
to it just waits until the app runs. And once the app runs, its own code goes
first: anything opened before the app first yields is never recorded.

So `vm_capture.py` does this:

1. find the main isolate and wait for it to settle at `PauseStart` (for a few
   milliseconds after launch it reports `runnable: false`, pause kind `None`);
2. **while still paused**, `invoke` the static setter
   `HttpClient.enableTimelineLogging = true` (selector
   `set:enableTimelineLogging`, argument `objects/bool-true`). The class lives
   in `dart:_http`, which `dart:io` re-exports. `invoke` runs on a paused
   isolate, and the app stays paused;
3. resume;
4. also call `ext.dart.io.httpEnableTimelineLogging?enabled=true` as soon as
   the isolate lists it (belt and braces);
5. poll `ext.dart.io.getHttpProfile?updatedSince=<last timestamp>` and print
   each finished request.

```sh
python3 tools/vm_capture.py --vm http://127.0.0.1:61523/kD9...=/ --out capture.jsonl
# [capture] isolate isolates/336745674576075, logging on (before resume)
# +  0.000s GET     200    39ms  http://127.0.0.1:8080/orders
# +  0.011s GET     200    31ms  http://127.0.0.1:8080/inbox
# +  0.011s GET     200    31ms  http://127.0.0.1:8080/profile
```

**Does the order really matter?** `tests/test_capture_race.py` answers it.
`demo_app/bin/first_request.dart` opens its first request synchronously at
the top of `main`. Resume-then-enable records 3 of its 4 requests (the first
is missing, every time); enable-while-paused records all 4. With the dio demo
app both orders happened to catch everything, because dio's first request
comes a few milliseconds later while its code is being compiled. Real apps
vary, so the tool doesn't rely on luck. The scenario checks that the capture
holds exactly as many requests as the app says it sent.

**Dropped and timed-out requests.** When a connection is dropped, or the
client gives up on a timeout, `dart:io` records no response *and no error*:
the profile entry just stops at "Request sent". `vm_capture.py` flags such
entries as `no response after Ns` once they've waited `--stall` seconds
(default 5), so they don't vanish from the capture.

### 2. Start the fault proxy

```sh
python3 tools/fault_proxy.py --upstream http://127.0.0.1:8080 --port 9090 --path /inbox --status 500
python3 tools/fault_proxy.py --upstream http://127.0.0.1:8080 --port 9090 --path /inbox --drop
python3 tools/fault_proxy.py --upstream http://127.0.0.1:8080 --port 9090 --path /inbox --delay 5
python3 tools/fault_proxy.py --upstream http://127.0.0.1:8080 --port 9090 --path /inbox --status 503 --times 2
```

Everything except the matching path is forwarded to the upstream unchanged
(headers, body, status), so the rest of the app keeps working against the real
server. `--times N` breaks only the first N matching requests: "fails twice,
then the retry succeeds".

### 3. Repoint the app's HTTP client with `invoke`

```sh
python3 tools/repoint.py --vm http://127.0.0.1:61523/kD9...=/ --list
# objects/1206/0           BaseOptions.baseUrl = http://127.0.0.1:8080

python3 tools/repoint.py --vm http://127.0.0.1:61523/kD9...=/ --from http://127.0.0.1:8080 --to http://127.0.0.1:9090
# objects/1811/0 BaseOptions.baseUrl: http://127.0.0.1:8080 -> http://127.0.0.1:9090 (via set:baseUrl, 4 invokes)
# revert: python3 tools/repoint.py --vm ... --from http://127.0.0.1:9090 --to http://127.0.0.1:8080
```

How it finds and changes the value, with RPCs that need no compiler:

| Step | RPC | What it does |
|---|---|---|
| Find the class | `getClassList` | every loaded class; keep `BaseOptions` from a `package:dio/` library |
| Find the live object | `getInstances(objectId=<class id>)` | the `BaseOptions` instances on the heap, one per Dio client |
| Read the URL | `invoke(target=<instance>, selector="get:baseUrl")` | runs dio's own getter |
| Build the new URL in the app | `invoke` of `substring`, `toRadixString`, `+`, `Uri.decodeComponent` | see below |
| Set it | `invoke(target=<instance>, selector="set:baseUrl", argumentIds=[<string id>])` | runs dio's own setter |
| Confirm | `invoke(... "get:baseUrl")` again | must equal the new URL |

Selectors matter: getters and setters are `get:name` and `set:name`. A bare
`baseUrl` selector calls the getter and then tries to *call the result*
(`String has no instance method 'call'`), and `baseUrl=` is not found.

**Getting a string into the app.** `invoke` arguments are object ids in the
app's heap; you can't pass `"http://127.0.0.1:9090"` from Python. Small ints
are the exception: `objects/int-9090` always means 9090, no heap object
needed. The obvious next step, `String.fromCharCode(...)`, doesn't work:
it's a factory constructor, and `invoke` only reaches methods, getters and
setters. So `repoint.py` builds the string from methods that exist on every
Dart VM:

| Piece of the new URL | How it's made inside the app |
|---|---|
| text that's already in the old URL (`http://127.0.0.1:`) | `oldUrl.substring(0, 17)` |
| a run of `[0-9a-z]` (`9090`, `api`) | `objects/int-<n>` then `.toRadixString(36)` |
| a run of `[0-9A-Z]` | the same, then `.toUpperCase()` |
| anything else (`:`, `_`, `é`) | `Uri.decodeComponent("%3A")`, a static method on `Uri` |
| the `%` for that | `Uri.encodeComponent(5.toStringAsExponential())` is `"5e%2B0"`; take `substring(2, 3)` |
| joining pieces | the `+` operator (selector `+`) |

Runs of `[0-9a-z]` are capped at 5 characters so the int stays a small int
even on devices where small ints are 31 bits. `http://127.0.0.1:8080` to
`http://127.0.0.1:9090` takes 4 invokes; a URL with no shared text, capitals,
`_` and a non-ASCII character took 67 invokes and 50 ms locally.

Because dio reads `options.baseUrl` each time it builds a request, the very
next request goes to the proxy. Nothing restarts, no app code changes, and the
demo app has no test hook: `repoint.py` works on any dio-based app. For a
client that keeps its URL somewhere else, pass `--class`, `--library` and
`--property` (for example `--class ApiConfig --library package:my_app/ --property baseUrl`).

### 4. Observe, then restore

The app prints its user-visible state; on a phone you'd watch the screen and
the capture side by side:

```
[ui] inbox: 2 unread
[retry] inbox: HTTP 500, retry 1/3 in 200ms
[retry] inbox: HTTP 500, retry 2/3 in 400ms
[retry] inbox: HTTP 500, retry 3/3 in 800ms
[ui] inbox: failed to load (HTTP 500)
[cycle 5] orders=ok inbox=HTTP 500 profile=ok requests=22
```

Run `repoint.py` again with the revert line it printed, and the inbox
recovers on the next refresh.

## Try it

Needs the Dart SDK (3.3+) and Python 3.10+. No Flutter. Verified with Dart
3.13.7 on macOS (arm64).

```sh
(cd demo_app && dart pub get) && (cd backend && dart pub get)
python3 tools/scenario.py            # prints each step and PASS/FAIL per check
python3 -m pip install -r requirements-dev.txt && python3 -m pytest -v
```

The pytest suite has 12 tests and takes about 15 seconds: 8 proxy unit tests
(no Dart needed), the full scenario, a dropped-connection run, and the two
capture-order tests.

Or by hand, in four terminals:

```sh
(cd backend && dart run bin/server.dart --port 8080)
(cd demo_app && dart run --enable-vm-service=8181 --pause-isolates-on-start bin/main.dart)
python3 tools/vm_capture.py --vm http://127.0.0.1:8181/<token>=/
python3 tools/fault_proxy.py --upstream http://127.0.0.1:8080 --port 9090 --path /inbox --status 500
python3 tools/repoint.py --vm http://127.0.0.1:8181/<token>=/ --to http://127.0.0.1:9090
```

### What the scenario checks

1. The app is at `PauseStart` before any request.
2. HTTP logging is switched on before resume.
3. Baseline: all three screens load.
4. The capture holds every request the app sent, starting with the first.
5. Repoint: the live `BaseOptions.baseUrl` now reads the proxy URL.
6. Inbox shows "failed to load (HTTP 500)".
7. Inbox retried exactly 3 times (200, 400, 800 ms backoff) before giving up.
8. Orders and profile keep loading in the same refresh.
9. The capture shows `/inbox` 500s and `/orders` 200s through the proxy.
10. The proxy faulted only `/inbox`.
11. Repoint back; inbox recovers; the latest `/inbox` is a 200 from the real backend.

Logs, `capture.jsonl` and `checks.json` land in `artifacts/` (uploaded by CI).

The other modes against the same app (checked by hand and, for `--drop`, by
`tests/test_scenario.py`):

| Proxy rule | What the app shows | What the capture shows |
|---|---|---|
| `--status 500` / `503` | 3 retries, then `failed to load (HTTP 500)` | 4 x 500 per refresh |
| `--drop` | 3 retries, then `failed to load (connection lost)` | no response, no error ("no response after Ns") |
| `--delay 5` (client timeout is 3 s) | 3 retries, then `failed to load (timed out)` | same as `--drop` |
| `--status 503 --times 2` | 2 retries, then the data | 2 x 503, then 200 |

## On a real Flutter app (Android)

The tools don't care whether the VM service belongs to a Dart CLI or a Flutter
app on a phone. What changes is how you reach it.

1. **Get a debug build** (`flutter build apk --debug`, or the debug APK your
   developers share). Release and profile builds are AOT-compiled; release
   builds have no VM service at all.
2. **Start it paused.** With source access: `flutter run --start-paused`. With
   only the APK: the Flutter engine reads shell arguments from intent extras on
   debug builds, e.g.
   `adb shell am start -n <package>/<activity> --ez start-paused true`
   (check this against your engine version).
3. **Find the VM service URL.** The engine logs it:
   `adb logcat -s flutter` shows
   `The Dart VM service is listening on http://127.0.0.1:43521/xYz…=/`.
4. **Forward the port to your laptop**: `adb forward tcp:43521 tcp:43521`.
   Now `--vm http://127.0.0.1:43521/xYz…=/` works from the laptop.
5. **Start the proxy on the laptop** with `--upstream https://<the real API host>`,
   and **reverse the proxy port to the phone**: `adb reverse tcp:9090 tcp:9090`.
   On the phone, `http://127.0.0.1:9090` now reaches the laptop's proxy.
6. **Repoint** with `--to http://127.0.0.1:9090/<same path prefix as before>`.
7. When `flutter run` / an IDE is attached it also starts DDS, which takes
   over the VM service; connect the tools to the URL DDS prints instead.

## Safety

- **Debug builds only. Never production.** This needs a debuggable build and a
  debugging connection; it is a test-environment technique.
- **Observation plus controlled fault injection, nothing else.** The proxy
  only forwards or fails requests. It doesn't rewrite payloads, and it never
  points a test build at someone else's server.
- **Put it back.** `repoint.py` prints the revert command every time, and the
  app's own URL comes back on restart anyway.
- **Mind what you capture.** Captures include URLs, and `getHttpProfileRequest`
  can return headers and bodies with tokens. Keep capture files out of tickets
  and repos unless they're scrubbed.

## Limits

- **Debug builds only.** Release builds (Android and iOS) are AOT-compiled with
  the VM service compiled out. Profile builds keep the VM service and the HTTP
  profiler, but AOT tree-shaking means `invoke` may not find the getter or
  setter you need.
- **iOS:** debug builds can't be launched from the home screen on iOS 14+, so
  in practice you're attached through `flutter run` / Xcode; the tools then
  connect to the URL it prints (port-forwarded over USB). iOS release has no
  VM service.
- **`evaluate` is not available on a device build.** Expression evaluation
  needs a Dart-to-kernel compiler. On a phone, the Flutter engine doesn't ship
  one; `flutter run` supplies it from the host. Attached straight to a debug
  build you were handed, `evaluate` fails, and `invoke` (which calls existing
  compiled code) is what's left. That's why the tools here use only
  `getClassList`, `getInstances`, `getObject` and `invoke`. (The standalone
  Dart VM used in this demo can evaluate; the tools deliberately don't.)
- **It needs a settable value.** dio's `BaseOptions.baseUrl` has a setter. If
  an app keeps its URL in a `final` field or a `const`, there's no setter to
  invoke; look for the object that *reads* the URL instead (a config object,
  an interceptor) or ask for a debug hook.
- **Plain HTTP between the app and the proxy.** If the app refuses `http://`
  URLs, the proxy would need TLS with a certificate the app trusts, which is
  out of scope here.
- **`dart:io` traffic only.** The profiler records `dart:io` `HttpClient`
  traffic (dio, `package:http` on mobile). Clients built on native stacks
  (`cronet_http`, `cupertino_http`) only show up if they report into the
  profiler themselves, and repointing them depends on how they store the URL.

## CI

`.github/workflows/ci.yml` sets up Dart and Python, runs `dart analyze`, then
`python -m pytest`: the proxy unit tests and the end-to-end scenario against
the real Dart VM. Logs and the capture are uploaded as artifacts.

## License

MIT, see [LICENSE](LICENSE).
