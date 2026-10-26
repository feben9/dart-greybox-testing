import 'package:dio/dio.dart';

/// The app's single HTTP client, built the way most Flutter apps build it:
/// one [Dio] with a [BaseOptions] that holds the backend's base URL.
///
/// Nothing here is test-only. There is deliberately no "setBaseUrl" hook:
/// the grey-box tools change `dio.options.baseUrl` on the live object
/// through the Dart VM service, which works on any dio-based app.
class ApiClient {
  ApiClient(String baseUrl)
      : dio = Dio(BaseOptions(
          baseUrl: baseUrl,
          connectTimeout: const Duration(seconds: 2),
          receiveTimeout: const Duration(seconds: 3),
          responseType: ResponseType.json,
        ));

  final Dio dio;

  /// Every attempt the app makes, retries included. The scenario compares
  /// this with what the VM service captured, to prove nothing was missed.
  int requestsSent = 0;

  Future<Response<dynamic>> get(String path) {
    requestsSent++;
    return dio.get<dynamic>(path);
  }
}
