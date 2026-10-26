# Testing server errors in a Flutter app when your proxy sees nothing

*By Feben Tibebe*

Every test plan I write has rows like these: "Inbox fails to load: the user
sees an error state." "The app retries a 5xx with backoff, then gives up."
"After the server recovers, the screen recovers without a restart." They are
some of the most useful rows in the plan, because they're where real users
get stuck.

In one test cycle, a whole block of those rows came back marked "Not Run:
can't make the server fail on demand". Nobody had skipped them out of laziness.
We had tried the usual tools, and none of them could reach the app.

This is how I got those rows running, using only what a debug build already
contains. The code is in the repo that goes with this post.

## Why the proxy saw nothing

My first move for any "make the server misbehave" case is an intercepting
proxy: Charles, Proxyman or Burp, with a breakpoint or a rewrite rule on one
endpoint. It didn't work, for two reasons.

Flutter apps don't use the phone's networking stack. They use `dart:io`, which
opens its own sockets and doesn't read the system proxy setting. I could set
a Wi-Fi proxy on the test phone all day and the app's traffic would go straight
past it.

Even if I could force the traffic through, the proxy would have to decrypt
HTTPS, which means the app has to trust the proxy's certificate. On a modern,
unrooted Android phone that's not something a tester can arrange.

A "simulate errors" switch in the app covers only the cases someone thought
of in advance, and breaking the shared test backend breaks it for everyone.

## The hook that was already there

A Flutter debug build runs on the Dart VM with its debugging service switched
on: the port DevTools connects to. It's a JSON-RPC API, and two parts of it
were exactly what I needed.

The first is the **HTTP profiler**. `dart:io` can record every request the app
makes: method, URL, status, timing, even headers. You switch it on with a
service extension and read it back with another:

```
ext.dart.io.httpEnableTimelineLogging   enabled=true
ext.dart.io.getHttpProfile              updatedSince=<timestamp>
```

The second is the **`invoke` RPC**. It calls a method, getter or setter on a
live object inside the running app. Most Flutter apps make their HTTP calls
through one client object, usually a dio `Dio` whose `BaseOptions` holds the
`baseUrl`. If I can change that one string, I decide where the app's requests
go.

You might reach for `evaluate` instead, which runs a Dart expression in the
app. On a phone it doesn't work: evaluating an expression needs a compiler, and
the Flutter engine on a device doesn't have one. `flutter run` lends it one from
the developer's laptop, but a tester attached to a debug build they were handed
has no such thing. `invoke` calls code that is already compiled, so it works.

## Step 1: see every request, from the first one

Real apps call their backend within milliseconds of starting, so the capture
has to be ready before the first line of the app runs. I start the app paused.
For Flutter that's `flutter run --start-paused`, or the `start-paused` launch
flag on a debug APK.

There's a catch. While the app is paused, the profiler's service extension
can't answer, because it is itself Dart code in the paused app. Once you
resume, the app's own code runs before your call does. I tested it: with a
request opened at the top of `main`, "resume, then switch logging on" missed
that first request every time. So while the app is still paused, I `invoke`
the static setter that the extension would have called:

```
invoke  targetId=<dart:io HttpClient class>
        selector=set:enableTimelineLogging
        argumentIds=[objects/bool-true]
```

Then I resume and start polling. My test for the capture is simple: the number
of requests captured must match the number the app says it sent, from the first
one.

## Step 2: change one value in the running app

To find the client, I ask the VM for its class list, keep the class called
`BaseOptions` from the dio package, and ask for its live instances. One
instance per Dio client the app built. Then:

```
invoke  targetId=<BaseOptions instance>  selector=get:baseUrl
invoke  targetId=<BaseOptions instance>  selector=set:baseUrl  argumentIds=[<string id>]
```

That last argument took me longest to work out. `invoke` arguments are
references to objects already inside the app, and you can't pass a string from
your laptop. Small integers are the exception: the VM understands
`objects/int-9090` without a heap object. `String.fromCharCode` would be the
obvious next step, but it's a constructor, and `invoke` can't call those. So I
build the new URL from plain methods: `substring` reuses the start of the old
URL, `9090.toRadixString(36)` turns numbers into letters and digits,
`Uri.decodeComponent("%3A")` makes any symbol, and `+` joins the pieces.
Repointing `:8080` to `:9090` takes four calls. And it means the technique
works on an app with no test hooks at all.

## Step 3: break exactly one endpoint

The new URL points at a small proxy on my laptop. It forwards every request to
the real server untouched, except the one path I choose:

```sh
python3 tools/fault_proxy.py --upstream https://api.example.test \
  --port 9090 --path /inbox --status 500
```

Other modes: `--drop` closes the connection without answering, `--delay 5`
answers late, and `--times 2` fails twice and then lets the retry through.
One surprise: for a dropped connection or a client timeout, the profiler
records no response and no error. The entry just stops at "Request sent", so
my capture tool flags anything left waiting as "no response". On
Android, `adb forward` brings the VM service port to the laptop, and
`adb reverse` makes the laptop's proxy reachable from the phone as
`127.0.0.1:9090`.

## What a run looks like

With `/inbox` returning 500 and everything else passing through:

```
[retry] inbox: HTTP 500, retry 1/3 in 200ms
[retry] inbox: HTTP 500, retry 2/3 in 400ms
[retry] inbox: HTTP 500, retry 3/3 in 800ms
[ui] inbox: failed to load (HTTP 500)
[cycle 5] orders=ok inbox=HTTP 500 profile=ok requests=22
```

The capture shows the same story from the network side: four `/inbox` 500s per
refresh, and `/orders` and `/profile` still getting 200s from the real server
through the proxy. Then I set the URL back, and the inbox recovers on the next
refresh without a restart.

In the repo, CI runs all of this as one scenario on every push.

## What it unblocked

Back in that test cycle, the "can't make the server fail" rows became ordinary
rows. Error states, retry counts and backoff timing, partial failures where one
screen breaks and the others don't, slow responses that hit a timeout, dropped
connections, and recovery afterwards. Each ran on the build under test, on a
real phone, against the shared backend, without touching the backend for
anyone else.

The capture changed how I write bugs, too. "Inbox shows a blank screen" became
"after three 500s on `/inbox`, the app shows a blank screen instead of the error
state; requests and timings attached". Developers can act on that straight
away.

## The rules I keep

- **Debug builds only, never production.** Release builds don't have a VM
  service anyway, and that's the right design.
- **Observe and inject, nothing else.** The proxy forwards or fails requests. It
  doesn't rewrite data.
- **Always put it back.** My repoint tool prints the revert command every time.
- **Captures are sensitive.** They can include tokens. Scrub them before
  attaching anything to a ticket.

## Where it stops

It needs a debuggable build and a cable (or wireless debugging). iOS debug
builds have to be launched from Xcode or `flutter run`. Apps that keep their
base URL in a `final` field have no setter to call, so you look for another
object to change or ask for a hook. And it only sees `dart:io` traffic.

Within those limits it closed a gap I'd lived with for a long time. If your
Flutter test plan has a column of "Not Run: needs a backend failure", it's
worth an afternoon. The tools use only the Python standard library.
