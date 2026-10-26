// Stand-in for a Flutter app: a client that refreshes three screens
// (orders, inbox, profile) from its backend every second.
//
//   dart run --enable-vm-service --pause-isolates-on-start \
//     bin/main.dart --base-url http://127.0.0.1:8080
import 'package:demo_app/api_client.dart';
import 'package:demo_app/inbox_app.dart';

Future<void> main(List<String> args) async {
  final baseUrl = _arg(args, '--base-url') ?? 'http://127.0.0.1:8080';
  final interval =
      Duration(milliseconds: int.parse(_arg(args, '--interval-ms') ?? '1000'));

  final api = ApiClient(baseUrl);
  final app = InboxApp(api);
  print('[app] started, backend $baseUrl');

  for (var cycle = 1;; cycle++) {
    await app.refresh(cycle);
    await Future<void>.delayed(interval);
  }
}

String? _arg(List<String> args, String name) {
  final i = args.indexOf(name);
  return (i >= 0 && i + 1 < args.length) ? args[i + 1] : null;
}
