from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .models import Branch, GoogleReview, GoogleReviewAction, GoogleReviewEvent, ReviewIssue
from .review_views import review_roles, demo_mode
from .review_workflow import is_review_senior, review_scope, branch_managers
from .roster import working_branch
from .google_review_ingestion import ingest_review, EventConflict


def scope(request):
    query = GoogleReview.objects.filter(environment='sandbox' if demo_mode(request) else 'production')
    if not is_review_senior(request.user):
        branch = working_branch(request.user)
        query = query.filter(branch=branch) if branch else query.none()
    return query


def filtered(request):
    query = scope(request)
    branch = request.GET.get('branch', '')
    if branch.isdigit() and is_review_senior(request.user):
        query = query.filter(branch_id=branch)
    if request.GET.get('sentiment') in ('positive', 'negative', 'mixed', 'neutral', 'unknown'):
        query = query.filter(sentiment=request.GET['sentiment'])
    if request.GET.get('priority') in ('LOW', 'NORMAL', 'HIGH', 'URGENT'):
        query = query.filter(priority=request.GET['priority'])
    search = request.GET.get('q', '').strip()[:180]
    if search:
        query = query.filter(Q(review_text__icontains=search) | Q(reviewer_display_name__icontains=search))
    return query


def context(request):
    return {'demo': demo_mode(request), 'senior': is_review_senior(request.user),
        'branches': Branch.objects.filter(google_reviews__in=scope(request)).distinct(),
        'selected_branch': request.GET.get('branch', ''), 'selected_sentiment': request.GET.get('sentiment', ''),
        'selected_priority': request.GET.get('priority', ''), 'search': request.GET.get('q', ''),
        'sentiments': ['positive', 'negative', 'mixed', 'neutral', 'unknown'],
        'priorities': ['URGENT', 'HIGH', 'NORMAL', 'LOW']}


@never_cache
@review_roles
@require_GET
def inbox(request):
    query = filtered(request).select_related('branch', 'case', 'case__owner')
    tab = request.GET.get('status', '')
    if tab == 'open':
        query = query.filter(Q(case__isnull=False, case__closed_at__isnull=True) |
            Q(case__isnull=True) & (Q(closed_at__isnull=True) | ~Q(closed_review_version=F('google_updated_at'))))
    elif tab == 'closed':
        query = query.filter(Q(case__closed_at__isnull=False) |
            Q(case__isnull=True, closed_at__isnull=False, closed_review_version=F('google_updated_at')))
    elif tab == 'unresolved':
        query = query.filter(Q(case__issues__status='CLOSED_UNRESOLVED') | Q(case__closure_outcome__in=['UNRESOLVED', 'PARTIAL'])).distinct()
    page = Paginator(query, 20).get_page(request.GET.get('page'))
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'reviews/google_inbox.html', {**context(request), 'page_obj': page,
        'page_query': params.urlencode(), 'selected_status': tab,
        'statuses': [('', 'All reviews'), ('open', 'Open'), ('closed', 'Closed'), ('unresolved', 'Unresolved')]})


@never_cache
@review_roles
@require_GET
def detail(request, review_id):
    review = get_object_or_404(scope(request).select_related('case', 'branch', 'closed_by'), pk=review_id)
    return render(request, 'reviews/google_detail.html', {**context(request), 'review': review,
        'events': review.events.all()[:30], 'actions': review.actions.select_related('actor').order_by('-created_at')[:30]})


@never_cache
@review_roles
@require_POST
@transaction.atomic
def action(request, review_id):
    review = get_object_or_404(scope(request).select_for_update(), pk=review_id)
    kind, note = request.POST.get('action'), request.POST.get('note', '').strip()[:1500]
    if request.POST.get('version') != review.google_updated_at.isoformat():
        return render(request, 'reviews/google_detail.html', {**context(request), 'review': review,
            'error': 'The review was edited. Reload it before saving.'}, status=409)
    if review.case_id or review.needs_followup:
        messages.error(request, 'Use the investigation workflow for this review.')
    elif kind == 'close':
        if not review.simple_closed:
            review.closed_at, review.closed_by = timezone.now(), request.user
            review.closed_review_version = review.google_updated_at
            review.save(update_fields=['closed_at', 'closed_by', 'closed_review_version'])
            GoogleReviewAction.objects.create(review=review, actor=request.user, action='Closed without investigation',
                review_version=review.google_updated_at, note=note)
    elif kind == 'reopen' and note:
        review.closed_at = review.closed_by = review.closed_review_version = None
        review.save(update_fields=['closed_at', 'closed_by', 'closed_review_version'])
        GoogleReviewAction.objects.create(review=review, actor=request.user, action='Reopened', note=note,
            review_version=review.google_updated_at)
    else:
        messages.error(request, 'Reopening requires a reason.')
    return redirect(reverse('google_review_detail', args=[review.pk]) + ('?demo=1' if demo_mode(request) else ''))


@never_cache
@review_roles
@require_GET
def analytics(request):
    records = filtered(request)
    cases = review_scope(request.user).filter(google_review__in=records)
    issues = ReviewIssue.objects.filter(case__in=cases)
    resolved_cases = cases.filter(closure_outcome='RESOLVED', closed_at__isnull=False)
    duration = resolved_cases.aggregate(value=Avg(ExpressionWrapper(
        F('closed_at') - F('received_at'), output_field=DurationField())))['value']
    rows = records.values('branch__name').annotate(total=Count('id'), rating=Avg('star_rating'),
        complaints=Count('id', filter=Q(needs_followup=True)), positive=Count('id', filter=Q(sentiment='positive'))).order_by('branch__name')
    backlog = sum(not c.owner_id or not branch_managers(
        c.branch).filter(pk=c.owner_id).exists() for c in cases.filter(closed_at__isnull=True).select_related('branch'))
    categories = list(issues.values('category').annotate(total=Count('id')).order_by('-total'))
    for row in categories:
        row['label'] = dict(ReviewIssue.CATEGORIES).get(row['category'], 'Other')
    return render(request, 'reviews/google_analytics.html', {**context(request), 'total': records.count(),
        'average_rating': records.aggregate(value=Avg('star_rating'))['value'],
        'rating_rows': [{'label': str(n) + ' stars', 'count': records.filter(star_rating=n).count()} for n in range(5, 0, -1)],
        'sentiment_rows': records.values('sentiment').annotate(total=Count('id')).order_by('sentiment'),
        'positive': records.filter(sentiment='positive').count(),
        'mixed': records.filter(sentiment='mixed', needs_followup=True).count(),
        'categories': categories,
        'urgent': records.filter(priority='URGENT', case__closed_at__isnull=True, case__isnull=False).count(),
        'backlog': backlog, 'unresolved': issues.exclude(status__in=['RESOLVED', 'NOT_ACTIONABLE']).count(),
        'resolution_hours': duration.total_seconds() / 3600 if duration else None,
        'publication_rows': records.values('publication_status').annotate(total=Count('id')).order_by('publication_status'),
        'branch_rows': rows})


@never_cache
@review_roles
@require_http_methods(['GET', 'POST'])
def assignment_queue(request):
    if not is_review_senior(request.user):
        raise PermissionDenied
    env = 'sandbox' if demo_mode(request) else 'production'
    if request.method == 'POST':
        event_id = request.POST.get('event_id', '')
        if not event_id.isdigit():
            from django.http import Http404
            raise Http404
        event = get_object_or_404(GoogleReviewEvent, pk=event_id, environment=env, status='quarantined')
        try:
            result, _ = ingest_review(event.payload)
            messages.success(request, 'Review committed.' if result.get('committed') else 'Store code or location still needs an active branch mapping.')
        except (ValidationError, EventConflict) as exc:
            messages.error(request, str(exc))
        return redirect(reverse('google_review_assignments') + ('?demo=1' if demo_mode(request) else ''))
    cases = review_scope(request.user).filter(synthetic=demo_mode(request), closed_at__isnull=True)
    unassigned = [c for c in cases.select_related('branch', 'owner') if not c.owner_id or not branch_managers(c.branch).filter(pk=c.owner_id).exists()]
    return render(request, 'reviews/google_assignments.html', {**context(request), 'cases': unassigned,
        'quarantined': GoogleReviewEvent.objects.filter(environment=env, status='quarantined')[:100]})
