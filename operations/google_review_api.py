import hmac
import json

from django.conf import settings
from django.core.exceptions import RequestDataTooBig, ValidationError
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .google_review_ingestion import EventConflict, ingest_publication, ingest_review


MAX_EVENT_BYTES = 512 * 1024


def reject_constant(value):
    raise ValueError('Non-finite JSON numbers are not supported.')


def receive(request, operation, legacy_auth=False):
    if settings.REVIEW_API_REQUIRE_HTTPS and not request.is_secure():
        return JsonResponse({'error': 'https_required', 'committed': False}, status=403)
    if request.content_type != 'application/json':
        return JsonResponse({'error': 'application_json_required', 'committed': False}, status=415)
    try:
        if int(request.META.get('CONTENT_LENGTH') or 0) > MAX_EVENT_BYTES:
            raise RequestDataTooBig()
        body = request.body
        if len(body) > MAX_EVENT_BYTES:
            raise RequestDataTooBig()
        payload = json.loads(body, parse_constant=reject_constant)
    except RequestDataTooBig:
        return JsonResponse({'error': 'event_too_large', 'committed': False}, status=413)
    except (ValueError, UnicodeError, RecursionError):
        return JsonResponse({'error': 'invalid_json', 'committed': False}, status=400)
    env = payload.get('environment') if isinstance(payload, dict) else None
    if env not in ('sandbox', 'production'):
        return JsonResponse({'error': 'invalid_environment', 'committed': False}, status=400)
    token = settings.REVIEW_API_TOKENS.get(env, '')
    if not token or len(token) < 32:
        return JsonResponse({'error': 'integration_not_configured', 'committed': False}, status=503)
    supplied = request.headers.get('X-Integration-Key')
    expected = token
    if supplied is None and legacy_auth:
        supplied = request.headers.get('Authorization', '')
        expected = 'Bearer ' + token
    if supplied is None or not hmac.compare_digest(supplied.encode(), expected.encode()):
        return JsonResponse({'error': 'invalid_authentication', 'committed': False}, status=401)
    idempotency = request.headers.get('Idempotency-Key')
    if idempotency is not None and idempotency != payload.get('event_id'):
        return JsonResponse({'error': 'idempotency_key_mismatch', 'committed': False}, status=409)
    try:
        result, status = operation(payload)
    except ValidationError as exc:
        return JsonResponse({'error': 'invalid_event', 'details': exc.messages, 'committed': False}, status=400)
    except EventConflict as exc:
        return JsonResponse({'error': 'event_conflict', 'detail': str(exc), 'committed': False}, status=409)
    # The operation's transaction has committed before this success is returned.
    return JsonResponse(result, status=status)


@csrf_exempt
@never_cache
@require_POST
def reviews(request):
    return receive(request, ingest_review, legacy_auth=True)


@csrf_exempt
@never_cache
@require_POST
def publications(request):
    return receive(request, ingest_publication, legacy_auth=True)


@csrf_exempt
@never_cache
@require_POST
def results(request):
    return receive(request, ingest_review)
