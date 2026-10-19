from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, Case, When, Value, IntegerField
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .decorators import roles_required
from .models import Branch, ReviewCase, ReviewIssue
from .review_forms import (AddIssueForm, AssignmentForm, ClosureForm, IssueUpdateForm, ReasonForm,
                           ReplyForm, ReviewIntakeForm, VersionForm)
from . import review_workflow as workflow

review_roles = roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER')


def demo_mode(request):
    return request.GET.get('demo') == '1'


def case_url(case, demo=False):
    return reverse('review_detail', args=[case.pk]) + ('?demo=1' if demo else '')


def scoped_cases(request):
    return workflow.review_scope(request.user).filter(synthetic=demo_mode(request))


def decorated_cases(query):
    return query.select_related('branch', 'owner', 'snapshot', 'reply', 'acknowledgement').prefetch_related('issues__assignee').annotate(
        issue_count=Count('issues', distinct=True),
        open_issue_count=Count('issues', filter=~Q(issues__status__in=ReviewIssue.TERMINAL), distinct=True),
    )


def assignment_valid(case):
    return bool(case.owner_id and workflow.branch_managers(case.branch).filter(pk=case.owner_id).exists())


@never_cache
@review_roles
@require_GET
def inbox(request):
    query = scoped_cases(request)
    senior = workflow.is_review_senior(request.user)
    branches = Branch.objects.filter(review_cases__in=query).distinct()
    branch_id = request.GET.get('branch', '')
    if branch_id.isdigit() and senior:
        query = query.filter(branch_id=branch_id)
    search = request.GET.get('q', '').strip()[:180]
    if search:
        query = query.filter(Q(subject__icontains=search) | Q(customer_label__icontains=search))
    all_cases = list(query.select_related('branch', 'owner'))
    unassigned_ids = [case.pk for case in all_cases if case.status != 'CLOSED' and not assignment_valid(case)]
    metrics = {
        'open_cases': query.exclude(status='CLOSED').count(),
        'open_issues': ReviewIssue.objects.filter(case__in=query).exclude(status__in=ReviewIssue.TERMINAL).count(),
        'needs_attention': query.filter(status__in=['NEW', 'ACKNOWLEDGED']).count(),
        'ready': query.filter(status='READY').count(),
    }
    tabs = [
        ('attention', 'Needs attention', query.filter(status__in=['NEW', 'ACKNOWLEDGED']).count()),
        ('progress', 'In progress', query.filter(status__in=['IN_PROGRESS', 'READY']).count()),
        ('waiting', 'Waiting', query.filter(status='WAITING').count()),
        ('closed', 'Closed', query.filter(status='CLOSED').count()),
        ('replies', 'Follow-ups awaiting approval', query.filter(reply__status='AWAITING_APPROVAL').count()),
        ('all', 'All reviews', query.count()),
    ]
    if senior:
        tabs.insert(4, ('unassigned', 'Needs assignment', len(unassigned_ids)))
    active_tab = request.GET.get('tab', 'attention')
    if active_tab not in {tab[0] for tab in tabs}:
        active_tab = 'attention'
    if active_tab == 'attention':
        query = query.filter(status__in=['NEW', 'ACKNOWLEDGED'])
    elif active_tab == 'progress':
        query = query.filter(status__in=['IN_PROGRESS', 'READY'])
    elif active_tab == 'waiting':
        query = query.filter(status='WAITING')
    elif active_tab == 'closed':
        query = query.filter(status='CLOSED')
    elif active_tab == 'replies':
        query = query.filter(reply__status='AWAITING_APPROVAL')
    elif active_tab == 'unassigned':
        query = query.filter(pk__in=unassigned_ids)
    overdue = ReviewIssue.objects.filter(case_id=OuterRef('pk'), due_at__lt=timezone.now()).exclude(status__in=ReviewIssue.TERMINAL)
    untriaged = ReviewIssue.objects.filter(case_id=OuterRef('pk'), confirmed_priority='', ai_priority='URGENT')
    query = decorated_cases(query).annotate(overdue=Exists(overdue), urgent_suggestion=Exists(untriaged)).annotate(
        triage_order=Case(When(Q(status__in=['NEW', 'ACKNOWLEDGED']) & (Q(sensitive=True) | Q(urgent_suggestion=True)), then=Value(0)),
                         When(overdue=True, then=Value(1)), default=Value(2), output_field=IntegerField())
    ).order_by('triage_order', 'received_at')
    page = Paginator(query, 15).get_page(request.GET.get('page'))
    for case in page:
        case.owner_valid = assignment_valid(case)
        case.ack = workflow.acknowledgement_context(case)
    params = request.GET.copy()
    params.pop('page', None)
    params.pop('tab', None)
    tab_prefix = params.urlencode()
    params['tab'] = active_tab
    return render(request, 'reviews/inbox.html', {
        'page_obj': page, 'metrics': metrics, 'tabs': tabs, 'active_tab': active_tab,
        'tab_prefix': tab_prefix + '&' if tab_prefix else '', 'page_query': params.urlencode(),
        'demo': demo_mode(request), 'senior': senior, 'branches': branches,
        'selected_branch': branch_id, 'search': search,
    })


def detail_context(request, case, bound=None, bound_issue=None):
    issues = list(case.issues.select_related('assignee'))
    version = {'version': case.version}
    for issue in issues:
        issue.form = bound if bound_issue == issue.pk else IssueUpdateForm(case=case, issue=issue)
        issue.expanded = bound_issue == issue.pk
    return {
        'case': case, 'issues': issues, 'demo': demo_mode(request), 'senior': workflow.is_review_senior(request.user),
        'owner_valid': assignment_valid(case), 'open_issues': sum(not issue.is_terminal for issue in issues),
        'complete_count': sum(issue.is_terminal for issue in issues),
        'activities': case.activities.select_related('actor', 'issue')[:100],
        'version_form': VersionForm(initial=version),
        'closure_form': ClosureForm(initial={**version, 'service_description': case.service_description}),
        'assignment_form': AssignmentForm(case=case, initial={**version, 'owner': case.owner_id}),
        'reason_form': ReasonForm(initial=version),
        'reply_form': ReplyForm(initial={**version, 'text': case.reply.text}),
        'add_issue_form': AddIssueForm(initial=version),
        'ack': workflow.acknowledgement_context(case),
    }


@never_cache
@review_roles
@require_GET
def detail(request, case_id):
    case = get_object_or_404(scoped_cases(request).select_related('branch', 'owner', 'snapshot', 'reply', 'closed_by'), pk=case_id)
    return render(request, 'reviews/detail.html', detail_context(request, case))


@never_cache
@review_roles
@require_POST
def action(request, case_id):
    case = get_object_or_404(scoped_cases(request).select_related('branch', 'reply', 'snapshot'), pk=case_id)
    kind = request.POST.get('action')
    issue = None
    if kind == 'issue':
        try:
            issue_id = UUID(request.POST.get('issue_id', ''))
        except (ValueError, TypeError, AttributeError):
            return HttpResponseBadRequest('Choose a valid issue.')
        issue = get_object_or_404(case.issues, pk=issue_id)
        form = IssueUpdateForm(request.POST, case=case, issue=issue)
    elif kind == 'close':
        form = ClosureForm(request.POST)
    elif kind == 'assign':
        form = AssignmentForm(request.POST, case=case)
    elif kind == 'reopen':
        form = ReasonForm(request.POST)
    elif kind == 'add_issue':
        form = AddIssueForm(request.POST)
    elif kind in ['save_reply', 'submit_reply', 'approve_reply', 'no_reply']:
        form = ReplyForm(request.POST)
    elif kind == 'acknowledge':
        form = VersionForm(request.POST)
    else:
        return HttpResponseBadRequest('Unknown ticket action.')
    if form.is_valid():
        data = form.cleaned_data
        args = (request.user, case.pk, data['version'])
        demo = demo_mode(request)
        try:
            if kind == 'issue':
                workflow.update_issue(request.user, case.pk, issue.pk, data['version'], data, demo)
            elif kind == 'close':
                workflow.close_case(*args, data['summary'], data['service_description'], demo)
            elif kind == 'assign':
                workflow.assign_case(*args, data['owner'], data['reason'], demo)
            elif kind == 'reopen':
                workflow.reopen_case(*args, data['reason'], demo)
            elif kind == 'acknowledge':
                workflow.acknowledge_case(*args, demo)
            elif kind == 'add_issue':
                with transaction.atomic():
                    locked = workflow.locked_case(*args, demo)
                    if locked.closed_at:
                        raise ValidationError('Reopen the ticket before adding an issue.')
                    created = ReviewIssue.objects.create(case=locked, category=data['category'], reported_issue=data['reported_issue'], assignee=locked.owner)
                    if created.category in ['HEALTH', 'CONDUCT']:
                        locked.sensitive = True
                        workflow.apply_sensitive_policy(locked)
                    locked.case_followup_requires_human = True
                    workflow.log(locked, request.user, 'Issue added', 'Customer-reported issue recorded.', issue=created)
                    workflow.save_change(locked)
                    workflow.assignment_outbox(locked, reason=f'new-issue-{created.pk}')
                    workflow.prepare_acknowledgement(locked)
            else:
                workflow.update_reply(*args, kind, data.get('text', ''), data.get('note', ''), demo)
        except workflow.ReviewConflict as exc:
            messages.error(request, str(exc))
            case.refresh_from_db()
            return render(request, 'reviews/detail.html', detail_context(request, case), status=409)
        except ValidationError as exc:
            form.add_error(None, exc)
            messages.error(request, ' '.join(exc.messages))
        else:
            messages.success(request, 'Ticket updated.' if 'reply' not in kind else 'Reply decision saved. Nothing has been posted to Google.')
            return redirect(case_url(case, demo))
    context = detail_context(request, case, form if issue else None, issue.pk if issue else None)
    form_keys = {'close': 'closure_form', 'assign': 'assignment_form', 'reopen': 'reason_form', 'add_issue': 'add_issue_form'}
    if kind != 'issue':
        context[form_keys.get(kind, 'reply_form' if 'reply' in kind else 'version_form')] = form
    context['action_error'] = True
    return render(request, 'reviews/detail.html', context, status=400)


@never_cache
@review_roles
@require_http_methods(['GET', 'POST'])
def create(request):
    form = ReviewIntakeForm(request.POST if request.method == 'POST' else None, user=request.user)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        issues = [{'category': data['category'], 'reported_issue': data['reported_issue']}] if data['category'] else []
        case = workflow.record_review(branch=data['branch'], actor=request.user, subject=data['subject'],
            customer_label=data['customer_label'], text=data['text'], rating=data['rating'],
            service_description=data['service_description'], issues=issues, synthetic=demo_mode(request))
        messages.success(request, 'Review ticket recorded. Add any other reported issues on the ticket.')
        return redirect(case_url(case, demo_mode(request)))
    return render(request, 'reviews/create.html', {'form': form, 'demo': demo_mode(request)})


@never_cache
@review_roles
@require_GET
def reports(request):
    cases = scoped_cases(request)
    issues = ReviewIssue.objects.filter(case__in=cases)
    open_cases = cases.exclude(status='CLOSED')
    open_issues = issues.exclude(status__in=ReviewIssue.TERMINAL)
    resolved = issues.filter(status='RESOLVED').count()
    actionable = issues.exclude(status='NOT_ACTIONABLE').count()
    outcomes = [{'label': label, 'count': cases.filter(closure_outcome=key).count()} for key, label in ReviewCase.OUTCOMES]
    branch_rows = []
    for branch in Branch.objects.filter(review_cases__in=cases).distinct():
        subset = cases.filter(branch=branch)
        branch_rows.append({'branch': branch, 'open': subset.exclude(status='CLOSED').count(),
                            'resolved': subset.filter(closure_outcome='RESOLVED').count(),
                            'other': subset.filter(status='CLOSED').exclude(closure_outcome='RESOLVED').count()})
    workload = open_cases.values('owner__username', 'owner__first_name').annotate(total=Count('id')).order_by('-total')
    unassigned = sum(not assignment_valid(case) for case in open_cases.select_related('branch', 'owner'))
    return render(request, 'reviews/reports.html', {
        'demo': demo_mode(request), 'senior': workflow.is_review_senior(request.user), 'as_of': timezone.now(),
        'open_cases': open_cases.count(), 'open_issues': open_issues.count(), 'unassigned': unassigned,
        'overdue': open_issues.filter(due_at__lt=timezone.now()).count(),
        'resolved_issues': resolved, 'actionable_issues': actionable,
        'excluded_issues': issues.filter(status='NOT_ACTIONABLE').count(),
        'outcomes': outcomes, 'branch_rows': branch_rows, 'workload': workload,
        'unconfigured': open_issues.filter(due_at__isnull=True).count(),
        'awaiting_reply': cases.filter(reply__status='AWAITING_APPROVAL').count(),
        'ack_prepared': cases.filter(acknowledgement__status='PREVIEW').count(),
        'ack_sent': cases.filter(acknowledgement__acknowledgement_sent_at__isnull=False).count(),
        'ack_published': cases.filter(acknowledgement__publication_confirmed_at__isnull=False).count(),
    })
