from pathlib import Path

from django.conf import settings
from django.http import FileResponse
from django.views.decorators.http import require_GET


def _public_asset(filename, content_type, cache_control):
    path = Path(settings.BASE_DIR) / 'static' / 'pwa' / filename
    response = FileResponse(path.open('rb'), content_type=content_type)
    response['Cache-Control'] = cache_control
    return response


@require_GET
def manifest(request):
    return _public_asset('manifest.webmanifest', 'application/manifest+json', 'public, max-age=3600')


@require_GET
def service_worker(request):
    response = _public_asset('service-worker.js', 'application/javascript', 'no-cache')
    response['Service-Worker-Allowed'] = '/'
    return response


@require_GET
def offline(request):
    return _public_asset('offline.html', 'text/html; charset=utf-8', 'public, max-age=3600')
