"""Server-owned credentials for individually created team accounts."""
from django.contrib.auth.models import User
from django.utils.text import slugify


def username_part(value):
    return slugify(value, allow_unicode=True).replace('-', '')


def available_staff_username(branch, first_name, last_name=''):
    parts = [username_part(branch.code), username_part(first_name)]
    if last_name:
        parts.append(username_part(last_name))
    base = '.'.join(parts)[:140]
    username, count = base, 2
    while User.objects.filter(username__iexact=username).exists():
        username = f'{base}.{count}'
        count += 1
    return username
