import 'dart:async';

import 'package:dio/dio.dart';

import 'api_client.dart';

/// One "screen" of the app: where it loads from and how it summarises
/// the response for the user.
class Screen {
  const Screen(this.name, this.path, this.summarise);
  final String name;
  final String path;
  final String Function(dynamic json) summarise;
}

final screens = <Screen>[
  Screen('orders', '/orders', (j) => '${(j['orders'] as List).length} orders'),
  Screen('inbox', '/inbox', (j) {
    final unread = (j['messages'] as List).where((m) => m['unread'] == true);
    return '${unread.length} unread';
  }),
  Screen('profile', '/profile', (j) => '${j['name']}'),
];

/// The app's behaviour, minus the widgets: load every screen, retry
/// server errors with exponential backoff, and show the user either the
/// data or a "failed to load" state.
///
/// Output lines (the scenario test reads these):
///   [ui] inbox: 2 unread                     user-visible state changed
///   [ui] inbox: failed to load (HTTP 500)
///   [retry] inbox: HTTP 500, retry 1/3 in 200ms
///   [cycle 4] orders=ok inbox=HTTP 500 profile=ok requests=15
class InboxApp {
  InboxApp(
    this.api, {
    this.maxRetries = 3,
    this.baseBackoff = const Duration(milliseconds: 200),
  });

  final ApiClient api;
  final int maxRetries;
  final Duration baseBackoff;
  final _visible = <String, String>{};

  Future<void> refresh(int cycle) async {
    final results = await Future.wait(screens.map(_load));
    final summary = [
      for (var i = 0; i < screens.length; i++)
        '${screens[i].name}=${results[i]}'
    ].join(' ');
    print('[cycle $cycle] $summary requests=${api.requestsSent}');
  }

  Future<String> _load(Screen screen) async {
    for (var attempt = 0;; attempt++) {
      try {
        final response = await api.get(screen.path);
        _show(screen.name, screen.summarise(response.data));
        return 'ok';
      } on DioException catch (e) {
        final reason = _describe(e);
        if (!_isRetryable(e) || attempt == maxRetries) {
          _show(screen.name, 'failed to load ($reason)');
          return reason;
        }
        final wait = baseBackoff * (1 << attempt);
        print('[retry] ${screen.name}: $reason, '
            'retry ${attempt + 1}/$maxRetries in ${wait.inMilliseconds}ms');
        await Future<void>.delayed(wait);
      }
    }
  }

  void _show(String name, String text) {
    if (_visible[name] == text) return;
    _visible[name] = text;
    print('[ui] $name: $text');
  }

  static bool _isRetryable(DioException e) {
    final status = e.response?.statusCode;
    if (status != null) return status >= 500;
    // No response at all: dropped connection, refused, or timed out.
    return true;
  }

  static String _describe(DioException e) {
    final status = e.response?.statusCode;
    if (status != null) return 'HTTP $status';
    return switch (e.type) {
      DioExceptionType.connectionTimeout ||
      DioExceptionType.receiveTimeout ||
      DioExceptionType.sendTimeout =>
        'timed out',
      _ => 'connection lost',
    };
  }
}
