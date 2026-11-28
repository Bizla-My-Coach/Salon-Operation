from django import template

from operations.review_targets import month_targets

register = template.Library()


@register.inclusion_tag('includes/review_target_bars.html')
def review_target_bars(branch=None, all_branches=False):
    """Bars for this month's Google review targets: one branch, or every branch."""
    if branch is None and not all_branches:
        return {'rows': []}
    rows = month_targets(None if all_branches else branch)
    return {'rows': rows, 'single': not all_branches}


@register.inclusion_tag('includes/review_followup_banner.html', takes_context=True)
def review_followup_banner(context):
    """A reminder strip for negative Google reviews still waiting for follow-up."""
    from operations.review_notifications import reminder_feed
    request = context.get('request')
    if request is None or not request.user.is_authenticated:
        return {}
    return reminder_feed(request.user)


@register.filter
def short_span(value, now=None):
    """'5h', '2d' or '20m' between now and a datetime, in either direction."""
    from django.utils import timezone
    if not value:
        return ''
    seconds = abs(((now or timezone.now()) - value).total_seconds())
    if seconds >= 86400:
        return f'{int(seconds // 86400)}d'
    if seconds >= 3600:
        return f'{int(seconds // 3600)}h'
    return f'{max(1, int(seconds // 60))}m'
