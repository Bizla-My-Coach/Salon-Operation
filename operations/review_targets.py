"""Monthly Google review targets, split into daily targets, for dashboard bars."""
from django.utils import timezone

from .models import Branch, GoogleReview, ReviewTarget


def progress(target, today=None):
    today = today or timezone.localdate()
    daily = target.daily_targets()
    in_month = target.month.year == today.year and target.month.month == today.month
    counted = GoogleReview.objects.filter(environment='production', branch=target.branch,
        google_created_at__date__gte=target.month, google_created_at__date__lte=max(daily))
    dates = list(counted.values_list('google_created_at', flat=True))
    local = [timezone.localtime(value).date() for value in dates]
    done = sum(1 for d in local if target.month <= d <= max(daily))
    expected = sum(v for d, v in daily.items() if d <= today) if in_month else float(target.target)
    today_target = daily.get(today, 0.0)
    today_done = sum(1 for d in local if d == today)
    percent = min(100, round(done * 100 / target.target)) if target.target else 100
    marker = min(100, round(expected * 100 / target.target)) if target.target else 100
    gap = round(expected) - done
    return {'target': target, 'branch': target.branch, 'done': done, 'goal': target.target,
        'expected': round(expected), 'percent': percent, 'marker': marker,
        'today_target': today_target, 'today_goal': max(1, round(today_target)) if today_target else 0,
        'today_done': today_done, 'behind': max(0, gap), 'on_track': gap <= 0,
        'weekend_today': today.weekday() in target.weekend_set,
        'days_left': sum(1 for d in daily if d > today) if in_month else 0}


def month_targets(branch=None, today=None):
    today = today or timezone.localdate()
    targets = ReviewTarget.objects.filter(month=today.replace(day=1), branch__active=True).select_related('branch')
    if branch is not None:
        targets = targets.filter(branch=branch)
    return [progress(t, today) for t in targets.order_by('branch__name')]
