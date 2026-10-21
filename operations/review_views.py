from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Case, When, Value, IntegerField, Max
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .decorators import roles_required
from .models import Branch, ReviewCase, ReviewIssue
from .review_forms import (AddIssueForm, AssignmentForm, ClosureForm, IssueUpdateForm, ReasonForm,
                           ReplyForm, ReviewIntakeForm, VersionForm, FollowupUpdateForm)
from . import review_workflow as workflow
from .review_notifications import notification_feed, visible_notifications

review_roles = roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER')


@never_cache
@review_roles
@require_GET
def notifications(request):
    return JsonResponse(notification_feed(request.user, demo_mode(request)))


@never_cache
@review_roles
@require_POST
def notification_open(request, notification_id):
    demo = demo_mode(request)
    alert = get_object_or_404(visible_notifications(request.user, demo), pk=notification_id)
    visible_notifications(request.user, demo).filter(pk=alert.pk, read_at__isnull=True).update(read_at=timezone.now())
    return redirect(case_url(alert.case, demo))


@never_cache
@review_roles
@require_POST
def notifications_read_all(request):
    demo = demo_mode(request)
    visible_notifications(request.user, demo).filter(read_at__isnull=True).update(read_at=timezone.now())
    if request.headers.get('Accept') == 'application/json':
        return JsonResponse(notification_feed(request.user, demo))
    return redirect(reverse('review_inbox') + ('?demo=1' if demo else ''))


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


def priority_cases(query):
    rules = [When(issues__category='HEALTH', then=Value(4))]
    for value, rank in [('URGENT', 4), ('HIGH', 3), ('NORMAL', 2), ('LOW', 1)]:
        rules.append(When(Q(issues__confirmed_priority=value) | Q(issues__confirmed_priority='', issues__ai_priority=value), then=Value(rank)))
    return query.annotate(priority_rank=Max(Case(*rules, When(issues__isnull=False, then=Value(2)), default=Value(0), output_field=IntegerField())))


@never_cache
@review_roles
@require_GET
def inbox(request):
    senior = workflow.is_review_senior(request.user)
    base = scoped_cases(request)
    branches = Branch.objects.filter(review_cases__in=base).distinct()
    # Positive / empty-issue reviews remain available to HQ, outside the branch work queue.
    if not senior:
        base = base.filter(Q(issues__isnull=False) | Q(case_followup_requires_human=True)).distinct()
    query = priority_cases(base)
    branch_id = request.GET.get('branch', '')
    if branch_id.isdigit() and senior:
        query = query.filter(branch_id=branch_id)
    search = request.GET.get('q', '').strip()[:180]
    if search:
        query = query.filter(Q(subject__icontains=search) | Q(customer_label__icontains=search))
    priorities = [('', 'All priorities'), ('URGENT', 'Urgent'), ('HIGH', 'High'), ('NORMAL', 'Normal'), ('LOW', 'Low')]
    selected_priority = request.GET.get('priority', '')
    ranks = {'URGENT': 4, 'HIGH': 3, 'NORMAL': 2, 'LOW': 1}
    if selected_priority in ranks:
        query = query.filter(priority_rank=ranks[selected_priority])
    else:
        selected_priority = ''
    metrics = {
        'open_cases': query.exclude(status='CLOSED').count(),
        'open_issues': ReviewIssue.objects.filter(case__in=query).exclude(status__in=ReviewIssue.TERMINAL).count(),
        'closed': query.filter(status='CLOSED').count(),
        'total': query.count(),
    }
    tabs = [('all', 'All tickets', query.count()),
            ('open', 'Open', query.exclude(status='CLOSED').count()),
            ('closed', 'Closed', query.filter(status='CLOSED').count())]
    active_tab = request.GET.get('tab', 'all' if senior else 'open')
    if active_tab not in ['all', 'open', 'closed']:
        active_tab = 'open'
    if active_tab == 'open':
        query = query.exclude(status='CLOSED')
    elif active_tab == 'closed':
        query = query.filter(status='CLOSED')
    query = decorated_cases(query).order_by('-priority_rank', 'received_at')
    page = Paginator(query, 15).get_page(request.GET.get('page'))
    for case in page:
        case.priority_label = {4: 'Urgent', 3: 'High', 2: 'Normal', 1: 'Low'}.get(case.priority_rank, '')
        case.priority_suggested = any(
            issue.ai_priority and not issue.confirmed_priority and ranks.get(issue.ai_priority) == case.priority_rank
            for issue in case.issues.all())
    params = request.GET.copy()
    params.pop('page', None)
    params.pop('tab', None)
    tab_prefix = params.urlencode()
    params['tab'] = active_tab
    priority_params = params.copy()
    priority_params.pop('priority', None)
    prefix = priority_params.urlencode()
    priority_links = [(key, label, (prefix + '&' if prefix else '') + 'priority=' + key) for key, label in priorities]
    return render(request, 'reviews/inbox.html', {
        'page_obj': page, 'metrics': metrics, 'tabs': tabs, 'active_tab': active_tab,
        'tab_prefix': tab_prefix + '&' if tab_prefix else '', 'page_query': params.urlencode(),
        'demo': demo_mode(request), 'senior': senior, 'branches': branches,
        'selected_branch': branch_id, 'search': search, 'selected_priority': selected_priority,
        'priority_links': priority_links,
    })


def detail_context(request, case, bound=None, bound_issue=None):
    issues = list(case.issues.select_related('assignee', 'followup_submitted_by'))
    version = {'version': case.version}
    for issue in issues:
        issue.form = bound if bound_issue == issue.pk else FollowupUpdateForm(case=case, issue=issue)
        issue.expanded = bound_issue == issue.pk
    return {
        'case': case, 'issues': issues, 'demo': demo_mode(request), 'senior': workflow.is_review_senior(request.user),
        'owner_valid': assignment_valid(case), 'open_issues': sum(not issue.is_terminal for issue in issues),
        'complete_count': sum(issue.is_terminal for issue in issues),
        'google_review': getattr(case, 'google_review', None),
        'unresolved': case.closure_outcome in ('PARTIAL', 'UNRESOLVED') or any(i.status == 'CLOSED_UNRESOLVED' for i in issues),
        'activities': case.activities.select_related('actor', 'issue')[:100],
        'version_form': VersionForm(initial=version),
        'closure_form': ClosureForm(initial={**version, 'service_description': case.service_description}),
        'assignment_form': AssignmentForm(case=case, initial={**version, 'owner': case.owner_id}),
        'reason_form': ReasonForm(initial=version),
        'reply_form': ReplyForm(initial={**version, 'text': case.reply.text}),
        'add_issue_form': AddIssueForm(initial=version),

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
    if kind in ['issue', 'followup']:
        try:
            issue_id = UUID(request.POST.get('issue_id', ''))
        except (ValueError, TypeError, AttributeError):
            return HttpResponseBadRequest('Choose a valid issue.')
        issue = get_object_or_404(case.issues, pk=issue_id)
        form_class = FollowupUpdateForm if kind == 'followup' else IssueUpdateForm
        form = form_class(request.POST, case=case, issue=issue)
    elif kind == 'close':
        form = ClosureForm(request.POST)
    elif kind == 'assign':
        form = AssignmentForm(request.POST, case=case)
    elif kind in ['reopen', 'close_no_issue']:
        form = ReasonForm(request.POST)
    elif kind == 'add_issue':
        form = AddIssueForm(request.POST)
    elif kind in ['save_reply', 'submit_reply', 'approve_reply', 'no_reply']:
        form = ReplyForm(request.POST)
    elif kind in ['acknowledge', 'close_simple']:
        form = VersionForm(request.POST)
    else:
        return HttpResponseBadRequest('Unknown ticket action.')
    if form.is_valid():
        data = form.cleaned_data
        args = (request.user, case.pk, data['version'])
        demo = demo_mode(request)
        try:
            if kind == 'followup':
                workflow.submit_followup(request.user, case.pk, issue.pk, data['version'], data, demo)
            elif kind == 'close_simple':
                workflow.close_from_followups(*args, demo)
            elif kind == 'close_no_issue':
                if case.issues.exists():
                    raise ValidationError('Use the issue follow-up flow for this review.')
                workflow.close_case(*args, data['reason'], case.service_description or 'No service issue reported.', demo)
            elif kind == 'issue':
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
    form_keys = {'close': 'closure_form', 'assign': 'assignment_form', 'reopen': 'reason_form', 'add_issue': 'add_issue_form', 'close_no_issue': 'reason_form'}
    if kind not in ['issue', 'followup']:
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
    if not workflow.is_review_senior(request.user):
        cases = cases.filter(Q(issues__isnull=False) | Q(case_followup_requires_human=True)).distinct()
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
