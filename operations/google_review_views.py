from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponseBadRequest
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .models import Branch, GoogleReview, GoogleReviewEvent, ReviewIssue
from .review_views import review_roles, review_admin_roles, demo_mode
from .review_workflow import is_review_admin, is_review_senior, review_scope, branch_managers
from .roster import working_branch
from .google_review_ingestion import ingest_review, EventConflict
from .review_forms import GoogleReviewFilterForm, GoogleReviewUpdateForm
from .google_review_workflow import form_initial, save_update
from .google_review_presentation import review_findings


def scope(request):
    query = GoogleReview.objects.filter(environment='sandbox' if demo_mode(request) else 'production')
    if not is_review_senior(request.user):
        branch = working_branch(request.user)
        query = query.filter(branch=branch) if branch else query.none()
    return query


def filtered(request):
    query = scope(request)
    if is_review_senior(request.user):
        form = filter_form(request)
        if not form.is_valid():
            return query.none()
        data = form.cleaned_data
        if data['rating']:
            query = query.filter(star_rating=data['rating'])
        if data['since']:
            query = query.filter(google_created_at__date__gte=data['since'])
        if data['until']:
            query = query.filter(google_created_at__date__lte=data['until'])
    branch = request.GET.get('branch', '')
    if branch.isdigit() and is_review_senior(request.user):
        query = query.filter(branch_id=branch)
    if is_review_admin(request.user) and request.GET.get('sentiment') in ('positive', 'negative', 'mixed', 'neutral', 'unknown'):
        query = query.filter(sentiment=request.GET['sentiment'])
    if request.GET.get('priority') in ('LOW', 'NORMAL', 'HIGH', 'URGENT'):
        query = query.filter(priority=request.GET['priority'])
    search = request.GET.get('q', '').strip()[:180]
    if search:
        query = query.filter(Q(review_text__icontains=search) | Q(reviewer_display_name__icontains=search) |
            Q(google_review_id__icontains=search))
    return query


def filter_form(request):
    branches = Branch.objects.filter(google_reviews__in=scope(request)).distinct().order_by('name')
    return GoogleReviewFilterForm(request.GET, branches=branches)


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
    if tab not in ('', 'open', 'closed'):
        tab = ''
    closed = Q(case__closed_at__isnull=False) | Q(case__isnull=True, closed_at__isnull=False,
        closed_review_version=F('google_updated_at'))
    params = request.GET.copy()
    params.pop('page', None)
    params.pop('status', None)
    tabs = []
    for key, label, count in [('', 'All reviews', query.count()), ('open', 'Open reviews', query.exclude(closed).count()),
                             ('closed', 'Closed reviews', query.filter(closed).count())]:
        params['status'] = key
        tabs.append({'key': key, 'label': label, 'count': count, 'query': params.urlencode()})
    if tab == 'open':
        query = query.exclude(closed)
    elif tab == 'closed':
        query = query.filter(closed)
    page = Paginator(query, 20).get_page(request.GET.get('page'))
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'reviews/google_inbox.html', {**context(request), 'page_obj': page,
        'page_query': params.urlencode(), 'selected_status': tab,
        'tabs': tabs, 'filter_form': filter_form(request) if is_review_senior(request.user) else None})


def detail_context(request, review, form=None):
    return {**context(request), **review_findings(review), 'review': review,
        'update_form': form if form is not None else GoogleReviewUpdateForm(initial=form_initial(review))}


@never_cache
@review_roles
@require_GET
def detail(request, review_id):
    review = get_object_or_404(scope(request).select_related('case', 'branch', 'closed_by'), pk=review_id)
    return render(request, 'reviews/google_detail.html', detail_context(request, review))


@never_cache
@review_roles
@require_POST
@transaction.atomic
def action(request, review_id):
    review = get_object_or_404(scope(request).select_for_update(), pk=review_id)
    if request.POST.get('action') != 'update':
        return HttpResponseBadRequest('Choose a valid review update.')
    form = GoogleReviewUpdateForm(request.POST)
    if form.is_valid() and request.POST.get('intent') == 'close' and form.cleaned_data['staff_followup'] not in ReviewIssue.TERMINAL:
        form.add_error('staff_followup', 'Choose Resolved, Unresolved or Not actionable before closing the review.')
    if form.is_valid():
        try:
            saved = save_update(review, request.user, form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            if not saved:
                form.add_error(None, 'This review changed in another session. Reload it before saving.')
                return render(request, 'reviews/google_detail.html', detail_context(request, review, form), status=409)
            messages.success(request, 'Review saved.')
            return redirect(reverse('google_review_detail', args=[review.pk]) + ('?demo=1' if demo_mode(request) else ''))
    return render(request, 'reviews/google_detail.html', detail_context(request, review, form), status=400)


@never_cache
@review_admin_roles
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
        if not is_review_admin(request.user):
            raise PermissionDenied
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
        'quarantined': GoogleReviewEvent.objects.filter(environment=env, status='quarantined')[:100]
            if is_review_admin(request.user) else []})
