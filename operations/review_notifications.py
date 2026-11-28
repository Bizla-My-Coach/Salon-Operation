"""In-app review alerts, independent of external outbox delivery."""
from django.urls import reverse
from django.utils import timezone
from django.db.models import Case, When, Value, IntegerField

from .models import ReviewNotification
from .review_workflow import is_review_admin, is_review_senior, review_scope


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


def pending_followups(user, synthetic=False):
    """Open negative reviews in the user's scope, oldest first. Shown until the follow-up is done."""
    from .models import GoogleReview
    from .roster import working_branch
    if not can_receive_review_alerts(user) or synthetic and not is_review_admin(user):
        return GoogleReview.objects.none()
    query = GoogleReview.objects.filter(environment='sandbox' if synthetic else 'production')
    if not is_review_senior(user):
        branch = working_branch(user)
        query = query.filter(branch=branch) if branch else query.none()
    return (query.filter(GoogleReview.negative_q()).exclude(GoogleReview.closed_q())
        .select_related('branch', 'case').order_by('google_updated_at', 'id'))


def reminder_feed(user, synthetic=False):
    from .models import GoogleReview
    now = timezone.now()
    pending = pending_followups(user, synthetic)
    cut = now - GoogleReview.FOLLOWUP_DUE_AFTER
    items = []
    for review in pending[:10]:
        due = review.followup_due_at
        hours = int(abs((now - due).total_seconds()) // 3600)
        if due < now:
            label = 'Overdue by ' + (f'{hours // 24} day{"s" if hours >= 48 else ""}' if hours >= 24 else f'{max(hours, 1)}h')
        else:
            label = f'Follow up within {max(hours, 1)}h'
        items.append({'name': review.reviewer_name, 'stars': review.star_rating, 'branch': review.branch.name,
            'label': label, 'overdue': due < now,
            'url': reverse('google_review_detail', args=[review.pk]) + ('?demo=1' if synthetic else '')})
    return {'reminder_count': pending.count(),
        'overdue_count': pending.filter(created_at__lt=cut, google_updated_at__lt=cut).count(), 'reminders': items}


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
    reminders = reminder_feed(user, synthetic)
    return {'unread_count': unread, 'items': items, **reminders,
        'badge_count': unread + reminders['reminder_count']}


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
