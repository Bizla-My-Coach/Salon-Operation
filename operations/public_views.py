"""Public information pages (no sign-in) used as the Google OAuth app home page,
privacy policy and terms of service. They contain no customer or staff data."""
from django.shortcuts import render
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_safe

CONTACT_EMAIL = 'info@hairship.in'
LAST_UPDATED = '13 November 2026'


def _page(request, template, title):
    return render(request, template, {'page_title': title, 'contact_email': CONTACT_EMAIL, 'last_updated': LAST_UPDATED})


@require_safe
@cache_control(public=True, max_age=3600)
def about(request):
    return _page(request, 'public/about.html', 'About Hairship Operations')


@require_safe
@cache_control(public=True, max_age=3600)
def privacy(request):
    return _page(request, 'public/privacy.html', 'Privacy Policy')


@require_safe
@cache_control(public=True, max_age=3600)
def terms(request):
    return _page(request, 'public/terms.html', 'Terms of Service')
