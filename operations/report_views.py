"""Plain-language branch reports for admins, general managers and managers.

Admins and general managers see every branch; a manager sees only the branch
they run (today's roster, falling back to their home branch).
"""
from datetime import datetime, time, timedelta

from django.db.models import Avg, Count, F, Q, Sum
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from .decorators import roles_required
from .models import (
    Branch, FeedbackAnswer, GoogleReview, Invoice, Visit, VisitService, VisitTask,
)
from .roster import working_branch
from .workflow import OPENING_TASK_TYPES

PERIODS = [('today', 'Today', 1), ('week', 'Last 7 days', 7), ('month', 'Last 30 days', 30)]
OPEN_VISIT = ['WAITING', 'ASSIGNED', 'IN_PROGRESS', 'EMPLOYEE_DONE']
FINISHED_VISIT = ['VERIFIED', 'INVOICED', 'CLOSED']
DONE_SERVICE = ['EMPLOYEE_DONE', 'VERIFIED']


def _period(request):
    key = request.GET.get('period', 'week')
    for value, label, days in PERIODS:
        if value == key:
            today = timezone.localdate()
            first = today - timedelta(days=days - 1)
            start = timezone.make_aware(datetime.combine(first, time.min))
            return value, label, first, today, start
    return None


def _grouped(query, field, **aggregates):
    return {row[field]: row for row in query.values(field).annotate(**aggregates)}


def _open_google_reviews():
    closed = Q(case__closed_at__isnull=False) | Q(
        case__isnull=True, closed_at__isnull=False, closed_review_version=F('google_updated_at'))
    return GoogleReview.objects.filter(environment='production').exclude(closed)


def _branch_rows(branches, start):
    """One plain summary per branch, every figure scoped to the given branches."""
    ids = [b.pk for b in branches]
    visits = Visit.objects.filter(branch_id__in=ids)
    period_visits = _grouped(
        visits.filter(created_at__gte=start).exclude(status='CANCELLED'), 'branch_id',
        customers=Count('pk'), finished=Count('pk', filter=Q(status__in=FINISHED_VISIT)),
    )
    open_now = _grouped(
        visits.filter(status__in=OPEN_VISIT + ['VERIFIED']), 'branch_id',
        open=Count('pk', filter=Q(status__in=OPEN_VISIT)),
        to_bill=Count('pk', filter=Q(status='VERIFIED')),
    )
    to_check = _grouped(
        VisitService.objects.filter(visit__branch_id__in=ids, status='EMPLOYEE_DONE'),
        'visit__branch_id', total=Count('pk'),
    )
    bills = _grouped(
        Invoice.objects.filter(branch_id__in=ids, status='COMPLETED', created_at__gte=start), 'branch_id',
        count=Count('pk'), revenue=Sum('amount'),
    )
    services = _grouped(
        VisitService.objects.filter(visit__branch_id__in=ids, status__in=DONE_SERVICE,
                                    employee_completed_at__gte=start),
        'visit__branch_id', done=Count('pk'),
    )
    skipped = _grouped(
        VisitTask.objects.filter(visit_service__visit__branch_id__in=ids, status='SKIPPED',
                                 completed_at__gte=start).exclude(task_type__in=OPENING_TASK_TYPES),
        'visit_service__visit__branch_id', total=Count('pk'),
    )
    feedback = _grouped(
        FeedbackAnswer.objects.filter(feedback__visit__branch_id__in=ids, feedback__submitted_at__gte=start),
        'feedback__visit__branch_id', rating=Avg('rating'), count=Count('feedback', distinct=True),
    )
    google = _grouped(
        GoogleReview.objects.filter(environment='production', branch_id__in=ids, google_created_at__gte=start),
        'branch_id', count=Count('pk'), stars=Avg('star_rating'),
        low=Count('pk', filter=Q(star_rating__lte=2)),
    )
    google_open = _grouped(_open_google_reviews().filter(branch_id__in=ids), 'branch_id', total=Count('pk'))

    rows = []
    for branch in branches:
        pid = branch.pk
        v = period_visits.get(pid, {})
        b = bills.get(pid, {})
        g = google.get(pid, {})
        row = {
            'branch': branch,
            'customers': v.get('customers', 0),
            'finished': v.get('finished', 0),
            'open_now': open_now.get(pid, {}).get('open', 0),
            'to_bill': open_now.get(pid, {}).get('to_bill', 0),
            'to_check': to_check.get(pid, {}).get('total', 0),
            'bills': b.get('count', 0),
            'revenue': b.get('revenue') or 0,
            'services_done': services.get(pid, {}).get('done', 0),
            'skipped': skipped.get(pid, {}).get('total', 0),
            'feedback_rating': feedback.get(pid, {}).get('rating'),
            'feedback_count': feedback.get(pid, {}).get('count', 0),
            'google_count': g.get('count', 0),
            'google_stars': g.get('stars'),
            'google_low': g.get('low', 0),
            'google_open': google_open.get(pid, {}).get('total', 0),
        }
        row['average_bill'] = row['revenue'] / row['bills'] if row['bills'] else 0
        row['attention'] = _attention(row)
        rows.append(row)
    return rows


def _attention(row):
    items = []
    if row['to_check']:
        items.append(f"{row['to_check']} finished service{'s' if row['to_check'] != 1 else ''} waiting for manager check")
    if row['to_bill']:
        items.append(f"{row['to_bill']} visit{'s' if row['to_bill'] != 1 else ''} checked but bill not entered")
    if row['google_low']:
        items.append(f"{row['google_low']} low Google rating{'s' if row['google_low'] != 1 else ''} (1–2 stars)")
    if row['google_open']:
        items.append(f"{row['google_open']} Google review{'s' if row['google_open'] != 1 else ''} not yet closed")
    if row['skipped']:
        items.append(f"{row['skipped']} SOP step{'s' if row['skipped'] != 1 else ''} skipped by staff")
    return items


def _totals(rows):
    total = {key: sum(r[key] for r in rows) for key in (
        'customers', 'finished', 'open_now', 'bills', 'revenue', 'services_done', 'google_count',
        'google_open', 'feedback_count')}
    total['average_bill'] = total['revenue'] / total['bills'] if total['bills'] else 0
    weights = [(r['google_stars'], r['google_count']) for r in rows if r['google_count']]
    total['google_stars'] = sum(s * c for s, c in weights) / total['google_count'] if weights else None
    weights = [(r['feedback_rating'], r['feedback_count']) for r in rows if r['feedback_count']]
    total['feedback_rating'] = (sum(s * c for s, c in weights) / sum(c for _, c in weights)) if weights else None
    total['needs_attention'] = sum(1 for r in rows if r['attention'])
    return total


def _daily(branch, first, last, start):
    visits = dict(
        Visit.objects.filter(branch=branch, created_at__gte=start).exclude(status='CANCELLED')
        .values_list('created_at__date').annotate(total=Count('pk')).values_list('created_at__date', 'total')
    )
    money = dict(
        Invoice.objects.filter(branch=branch, status='COMPLETED', created_at__gte=start)
        .values_list('created_at__date').annotate(total=Sum('amount')).values_list('created_at__date', 'total')
    )
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    peak = max([visits.get(d, 0) for d in days] + [1])
    return [{'day': d, 'customers': visits.get(d, 0), 'revenue': money.get(d) or 0,
             'height': round(visits.get(d, 0) * 100 / peak)} for d in days]


def _branch_detail(branch, first, last, start):
    services = (
        VisitService.objects.filter(visit__branch=branch, status__in=DONE_SERVICE, employee_completed_at__gte=start)
        .values('service__name').annotate(total=Count('pk')).order_by('-total', 'service__name')[:6]
    )
    top = max([s['total'] for s in services] + [1])
    team = (
        VisitService.objects.filter(visit__branch=branch, employee_completed_at__gte=start, status__in=DONE_SERVICE)
        .values('employee__first_name', 'employee__last_name', 'employee__username')
        .annotate(done=Count('pk', distinct=True),
                  skipped=Count('tasks', filter=Q(tasks__status='SKIPPED') & ~Q(tasks__task_type__in=OPENING_TASK_TYPES)))
        .order_by('-done', 'employee__first_name')
    )
    feedback = dict(
        FeedbackAnswer.objects.filter(feedback__visit__branch=branch, feedback__submitted_at__gte=start)
        .values('feedback__visit__services__employee__username').annotate(avg=Avg('rating'))
        .values_list('feedback__visit__services__employee__username', 'avg')
    )
    staff = []
    for person in team:
        name = f"{person['employee__first_name']} {person['employee__last_name']}".strip() or person['employee__username']
        staff.append({'name': name, 'done': person['done'], 'skipped': person['skipped'],
                      'rating': feedback.get(person['employee__username'])})
    reviews = (
        GoogleReview.objects.filter(environment='production', branch=branch)
        .order_by('-google_created_at')[:5]
    )
    open_ids = set(_open_google_reviews().filter(branch=branch).values_list('pk', flat=True))
    return {
        'daily': _daily(branch, first, last, start) if first != last else [],
        'top_services': [{**s, 'width': round(s['total'] * 100 / top)} for s in services],
        'staff': staff,
        'recent_reviews': [{'review': r, 'open': r.pk in open_ids} for r in reviews],
    }


@roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER')
def branch_reports(request):
    period = _period(request)
    if period is None:
        return HttpResponseBadRequest('Choose a valid period.')
    key, label, first, last, start = period
    profile = request.user.profile
    see_all = request.user.is_superuser or profile.role in ('ADMIN', 'GENERAL_MANAGER')
    context = {'periods': PERIODS, 'period': key, 'period_label': label, 'first': first, 'last': last,
               'see_all': see_all}
    if see_all:
        branches = list(Branch.objects.filter(active=True).order_by('name', 'pk'))
        branch_id = request.GET.get('branch', '')
        if branch_id and not branch_id.isdigit():
            return HttpResponseBadRequest('Choose a valid branch.')
        selected = get_object_or_404(Branch, pk=branch_id) if branch_id else None
        context['branch_tabs'] = branches
    else:
        selected = working_branch(request.user) or profile.branch
        if selected is None:
            return render(request, 'operations/branch_reports.html', {**context, 'no_branch': True})
    if selected:
        row = _branch_rows([selected], start)[0]
        context.update({'selected': selected, 'row': row, **_branch_detail(selected, first, last, start)})
    else:
        rows = _branch_rows(branches, start)
        rows.sort(key=lambda r: (-r['revenue'], r['branch'].name))
        context.update({'rows': rows, 'total': _totals(rows)})
    return render(request, 'operations/branch_reports.html', context)
