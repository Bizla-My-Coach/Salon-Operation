"""In-app review alerts, independent of external outbox delivery."""
from django.urls import reverse
from django.utils import timezone
from django.db.models import Case, When, Value, IntegerField

from .models import ReviewNotification
from .review_workflow import is_review_admin, review_scope


def can_receive_review_alerts(user):
    profile = getattr(user, 'profile', None)
    return bool(user.is_authenticated and user.is_active and (
        user.is_superuser or profile and profile.active and profile.role in ('ADMIN', 'GENERAL_MANAGER', 'MANAGER')))


def visible_notifications(user, synthetic=False):
    if not can_receive_review_alerts(user) or synthetic and not is_review_admin(user):
        return ReviewNotification.objects.none()
    return ReviewNotification.objects.filter(
        recipient=user, case__owner=user, case__synthetic=synthetic,
        case__in=review_scope(user).filter(case_followup_requires_human=True),
    )


def notification_feed(user, synthetic=False):
    query = visible_notifications(user, synthetic)
    unread = query.filter(read_at__isnull=True).count()
    items = []
    recent = query.select_related('case__branch').annotate(read_rank=Case(
        When(read_at__isnull=True, then=Value(0)), default=Value(1), output_field=IntegerField(),
    )).order_by('read_rank', '-created_at', '-pk')[:20]
    for alert in recent:
        items.append({
            'id': alert.pk, 'subject': alert.case.subject, 'branch': alert.case.branch.name,
            'time': timezone.localtime(alert.created_at).strftime('%d %b, %H:%M'),
            'unread': alert.read_at is None,
            'open_url': reverse('review_notification_open', args=[alert.pk]) + ('?demo=1' if synthetic else ''),
        })
    return {'unread_count': unread, 'items': items}


def review_notifications(request):
    if not can_receive_review_alerts(request.user):
        return {}
    demo = request.GET.get('demo') == '1' and is_review_admin(request.user)
    suffix = '?demo=1' if demo else ''
    return {
        'review_alerts': notification_feed(request.user, demo),
        'review_alert_feed_url': reverse('review_notifications') + suffix,
        'review_alert_read_all_url': reverse('review_notifications_read_all') + suffix,
        'review_alert_inbox_url': reverse('google_review_inbox') + suffix,
    }
