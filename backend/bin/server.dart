// A tiny JSON backend for the demo app. dart:io only, no packages.
//
//   dart run bin/server.dart --port 0     # 0 = pick a free port
//
// Prints "[backend] listening on http://127.0.0.1:<port>" once it is ready.
import 'dart:convert';
import 'dart:io';

const _orders = {
  'orders': [
    {'id': 'A-1001', 'status': 'shipped', 'total': 42.50},
    {'id': 'A-1002', 'status': 'processing', 'total': 18.00},
    {'id': 'A-1003', 'status': 'delivered', 'total': 7.25},
  ],
};

const _inbox = {
  'messages': [
    {
      'id': 'm1',
      'from': 'Support',
      'subject': 'Your order shipped',
      'unread': true
    },
    {
      'id': 'm2',
      'from': 'Billing',
      'subject': 'Receipt for A-1002',
      'unread': true
    },
    {'id': 'm3', 'from': 'Support', 'subject': 'Welcome', 'unread': false},
  ],
};

const _profile = {'id': 'u-7f3a', 'name': 'Alex Doe', 'plan': 'standard'};

Future<void> main(List<String> args) async {
  final port = int.parse(_arg(args, '--port') ?? '8080');
  final quiet = args.contains('--quiet');
  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, port);
  print('[backend] listening on http://127.0.0.1:${server.port}');

  await for (final req in server) {
    final body = switch (req.uri.path) {
      '/orders' => _orders,
      '/inbox' => _inbox,
      '/profile' => _profile,
      '/health' => {'ok': true},
      _ => null,
    };
    final status = body == null ? HttpStatus.notFound : HttpStatus.ok;
    req.response
      ..statusCode = status
      ..headers.contentType = ContentType.json
      ..write(jsonEncode(body ?? {'error': 'not found'}));
    await req.response.close();
    if (!quiet) print('[backend] ${req.method} ${req.uri.path} $status');
  }
}

String? _arg(List<String> args, String name) {
  final i = args.indexOf(name);
  return (i >= 0 && i + 1 < args.length) ? args[i + 1] : null;
}
