// Worst case for capture: the very first request is opened synchronously at
// the top of main, before the app ever yields to its event loop.
//
// A service extension call (like ext.dart.io.httpEnableTimelineLogging) is
// only handled once the app yields, so "resume, then enable logging" misses
// this request. Switching logging on while the app is still paused catches it.
// tests/test_capture_race.py runs both orders against this script.
//
//   dart run --enable-vm-service --pause-isolates-on-start \
//     bin/first_request.dart http://127.0.0.1:8080
import 'dart:io';

Future<void> main(List<String> args) async {
  final base = args.isNotEmpty ? args.first : 'http://127.0.0.1:8080';
  final client = HttpClient();

  final first =
      client.getUrl(Uri.parse('$base/orders')); // opened synchronously
  await (await (await first).close()).drain<void>();

  for (var i = 0; i < 3; i++) {
    final request = await client.getUrl(Uri.parse('$base/inbox'));
    await (await request.close()).drain<void>();
  }
  print('[first_request] sent 4');

  // Stay alive so the capture can read the profile.
  await Future<void>.delayed(const Duration(seconds: 30));
  client.close();
}
