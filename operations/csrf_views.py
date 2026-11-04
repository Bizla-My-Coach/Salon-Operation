"""Friendly CSRF failure handling, mainly for stale sign-in forms on phones."""
import logging

from django.http import HttpResponseRedirect
from django.middleware.csrf import REASON_NO_CSRF_COOKIE
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.cache import never_cache

logger = logging.getLogger('salonops.csrf')


@never_cache
def csrf_failure(request, reason=''):
    logger.warning('CSRF failure on %s %s: %s', request.method, request.path, reason)
    cookies_blocked = reason == REASON_NO_CSRF_COOKIE
    login_url = reverse('login')
    if request.path == login_url and not cookies_blocked:
        # A stale sign-in form (old tab, or another sign-in rotated the token):
        # reload a fresh form instead of showing a bare 403.
        params = {'expired': '1'}
        next_url = request.GET.get('next') or request.POST.get('next')
        if next_url and url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            params['next'] = next_url
        return HttpResponseRedirect(f'{login_url}?{urlencode(params)}')
    return render(request, 'registration/csrf_failure.html', {
        'plain_layout': True, 'cookies_blocked': cookies_blocked, 'login_url': login_url,
    }, status=403)
