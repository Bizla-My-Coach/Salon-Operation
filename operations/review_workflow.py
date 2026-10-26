"""Branch-scoped review handling. There is deliberately no Google publisher here."""
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (BranchDuty, ReviewAcknowledgement, ReviewActivity, ReviewCase, ReviewIssue,
                     ReviewNotification, ReviewReply, ReviewSnapshot)
from .roster import working_branch


class ReviewConflict(Exception):
    pass


def is_review_admin(user):
    profile = getattr(user, 'profile', None)
    return user.is_superuser or bool(profile and profile.role == 'ADMIN')


def is_review_senior(user):
    return user.is_superuser or user.profile.role in ('ADMIN', 'GENERAL_MANAGER')


def review_scope(user):
    cases = ReviewCase.objects.all()
    if is_review_senior(user):
        return cases
    branch = working_branch(user)
    return cases.filter(branch=branch) if branch else cases.none()


def branch_managers(branch):
    duties = BranchDuty.objects.filter(date=timezone.localdate())
    return User.objects.filter(is_active=True, profile__active=True).filter(
        Q(profile__role='MANAGER', profile__branch=branch) & ~Q(pk__in=duties.values('user_id'))
        | Q(profile__role__in=['MANAGER', 'GENERAL_MANAGER'], pk__in=duties.filter(status='WORK', branch=branch).values('user_id'))
    ).order_by('first_name', 'username')


def log(case, actor, action, note='', issue=None, previous='', current=''):
    return ReviewActivity.objects.create(case=case, actor=actor, action=action, note=note,
                                         issue=issue, from_status=previous, to_status=current)


def apply_sensitive_policy(case):
    case.requires_senior_closure = case.sensitive and settings.REVIEW_SENSITIVE_CLOSURE_SENIOR_APPROVAL
    case.requires_senior_followup = case.sensitive and settings.REVIEW_SENSITIVE_FOLLOWUP_SENIOR_APPROVAL


def assignment_outbox(case, reason='assignment'):
    if case.owner_id:
        ReviewNotification.objects.get_or_create(
            dedupe_key=f'{case.pk}:{reason}:{case.owner_id}:{case.version}',
            defaults={'case': case, 'recipient': case.owner, 'status': 'PREVIEW' if case.synthetic else 'PENDING'},
        )


def prepare_acknowledgement(case):
    """Prepare a fixed-template intent inside the assignment transaction, never send it."""
    ack, _ = ReviewAcknowledgement.objects.get_or_create(case=case, defaults={'template_key': 'pending-assignment'})
    if ack.status not in ['HELD', 'PREVIEW']:
        # Submitted/unknown/published intents must be reconciled, never silently replaced.
        return ack
    assigned = case.owner_id and branch_managers(case.branch).filter(pk=case.owner_id).exists()
    if not assigned:
        ack.status, ack.text, ack.template_key = 'HELD', '', 'pending-assignment'
        ack.assignment_confirmed_at = ack.assigned_manager = None
    else:
        if case.sensitive:
            key = 'sensitive-generic-v1'
            text = "We're sorry to hear about your experience. We take your concern seriously and have referred it to our branch manager for review."
        elif case.issues.exists() and (case.snapshot.rating or 0) >= 4:
            key = 'mixed-feedback-v1'
            text = "Thank you for your kind feedback. We're sorry that parts of your visit fell short, and have referred your concerns to our branch manager for review."
        elif case.issues.exists() or (case.snapshot.rating and case.snapshot.rating <= 2):
            key = 'concern-referral-v1'
            text = "Thank you for sharing your feedback. We're sorry your experience fell short and have referred it to our branch manager for review."
        else:
            key = 'thank-you-v1'
            text = 'Thank you for taking the time to share your feedback with our team.'
        ack.template_key, ack.text, ack.status = key, text, 'PREVIEW'
        if ack.assigned_manager_id != case.owner_id:
            ack.assignment_confirmed_at = timezone.now()
        ack.assigned_manager = case.owner
        ack.source_fetched_at = case.snapshot.fetched_at
    ack.save()
    return ack


def acknowledgement_context(case):
    """Recheck eligibility without mutating work or claiming publication on a GET."""
    ack = case.acknowledgement
    owner_valid = bool(case.owner_id and branch_managers(case.branch).filter(pk=case.owner_id).exists())
    eligible = bool(owner_valid and case.snapshot.available and ack.assigned_manager_id == case.owner_id
                    and ack.status == 'PREVIEW' and ack.text and ack.source_fetched_at == case.snapshot.fetched_at)
    if ack.status not in ['HELD', 'PREVIEW']:
        label = ack.get_status_display()
    elif not owner_valid:
        label = 'Held · manager assignment needed'
    elif not case.snapshot.available or ack.source_fetched_at != case.snapshot.fetched_at:
        label = 'Held · source refresh needed'
    else:
        label = ack.get_status_display()
    return {'record': ack, 'eligible': eligible, 'label': label}


def refresh_state(case):
    if case.closed_at:
        case.status = 'CLOSED'
        return
    states = list(case.issues.values_list('status', flat=True))
    open_states = [state for state in states if state not in ReviewIssue.TERMINAL]
    if states and not open_states:
        case.status = 'READY'
    elif open_states and all(state == 'WAITING' for state in open_states):
        case.status = 'WAITING'
    elif any(state in ['IN_PROGRESS', 'WAITING'] for state in states) or any(state in ReviewIssue.TERMINAL for state in states):
        case.status = 'IN_PROGRESS'
    elif case.acknowledged_at or 'ACKNOWLEDGED' in states:
        case.status = 'ACKNOWLEDGED'
    else:
        case.status = 'NEW'


def locked_case(user, case_id, expected_version, synthetic=False):
    # Recheck the effective branch inside the mutation, even for a guessed ID.
    from django.shortcuts import get_object_or_404
    case = get_object_or_404(review_scope(user).select_for_update(), pk=case_id, synthetic=synthetic)
    if case.version != expected_version:
        raise ReviewConflict('This review changed in another session. Reload it before saving again.')
    return case


def save_change(case):
    refresh_state(case)
    case.version += 1
    case.save()


@transaction.atomic
def record_review(*, branch, actor, subject, customer_label, text, rating, service_description,
                  issues, synthetic=False, source_key=None, ai_requires_human_review=False):
    """Staff intake / synthetic fixtures, not a machine-ingestion or Google API contract."""
    if source_key:
        existing = ReviewCase.objects.filter(source_key=source_key).first()
        if existing:
            if existing.branch_id != branch.pk or existing.synthetic != synthetic:
                raise ValidationError('The source reference already belongs to another review scope.')
            if not ReviewAcknowledgement.objects.filter(case=existing).exists():
                prepare_acknowledgement(existing)
                assignment_outbox(existing)
            return existing
    managers = list(branch_managers(branch))
    owner = managers[0] if len(managers) == 1 else None
    values = dict(branch=branch, owner=owner, subject=subject, customer_label=customer_label,
                  service_description=service_description, synthetic=synthetic,
                  sensitive=any(i['category'] in ['HEALTH', 'CONDUCT'] for i in issues),
                  ai_requires_human_review=ai_requires_human_review)
    if source_key:
        values['source_key'] = source_key
    case = ReviewCase.objects.create(**values)
    case.case_followup_requires_human = bool(issues or (rating and rating <= 2))
    apply_sensitive_policy(case)
    case.save(update_fields=['case_followup_requires_human', 'requires_senior_closure', 'requires_senior_followup'])
    ReviewSnapshot.objects.create(case=case, text=text, rating=rating)
    ReviewReply.objects.create(case=case)
    for issue in issues:
        ReviewIssue.objects.create(case=case, assignee=owner, **issue)
    assignment_outbox(case)
    prepare_acknowledgement(case)
    log(case, actor, 'Review recorded', 'Assigned to branch manager.' if owner else 'A branch manager needs to be assigned.')
    return case


@transaction.atomic
def acknowledge_case(user, case_id, version, synthetic=False):
    case = locked_case(user, case_id, version, synthetic)
    if case.closed_at:
        raise ValidationError('Reopen this review before recording more internal work.')
    if not case.acknowledged_at:
        case.acknowledged_at = timezone.now()
        log(case, user, 'Review acknowledged')
        save_change(case)
    return case


@transaction.atomic
def update_issue(user, case_id, issue_id, version, data, synthetic=False):
    case = locked_case(user, case_id, version, synthetic)
    if case.closed_at:
        raise ValidationError('Reopen the review before updating its issues.')
    issue = case.issues.get(pk=issue_id)
    assignee = data.get('assignee')
    if assignee and not branch_managers(case.branch).filter(pk=assignee.pk).exists():
        raise ValidationError('Choose a manager currently working at this branch.')
    issue.assignee = assignee
    previous = issue.status
    target = data['status']
    if issue.is_terminal and target != previous and target != 'IN_PROGRESS':
        raise ValidationError('Reopen a completed issue as In progress, with a reason in Action taken.')
    if previous != 'NEW' and target == 'NEW':
        raise ValidationError('An acknowledged issue cannot be reset to New.')
    reopening = issue.is_terminal and target == 'IN_PROGRESS'
    if not issue.is_terminal or reopening:
        if reopening:
            issue.cycle += 1
            issue.completed_at = issue.completed_by = None
        if target != 'NEW' and not issue.acknowledged_at:
            issue.acknowledged_at = timezone.now()
        if target in ReviewIssue.TERMINAL and not issue.completed_at:
            issue.completed_at, issue.completed_by = timezone.now(), user
    for field in ['status', 'confirmed_priority', 'action_summary', 'outcome', 'evidence_reference',
                  'customer_confirmation', 'waiting_reason', 'next_review_at']:
        setattr(issue, field, data.get(field, ''))
    if target != 'WAITING':
        issue.waiting_reason, issue.next_review_at = '', None
    issue.followup_submitted_at = timezone.now()
    issue.followup_submitted_by = user
    issue.save()
    if not case.acknowledged_at:
        case.acknowledged_at = timezone.now()
    note = issue.action_summary
    if issue.outcome:
        note += '\nOutcome: ' + issue.outcome
    if issue.confirmed_priority:
        note += '\nConfirmed priority: ' + issue.get_confirmed_priority_display()
    note += '\nCustomer confirmation: ' + issue.get_customer_confirmation_display()
    if issue.evidence_reference:
        note += '\nEvidence reference: ' + issue.evidence_reference
    if target == 'WAITING':
        note += '\n' + issue.get_waiting_reason_display()
        if issue.next_review_at:
            note += '; review ' + timezone.localtime(issue.next_review_at).strftime('%d %b %Y %H:%M')
    log(case, user, 'Issue reopened' if reopening else 'Issue updated', note, issue, previous, target)
    save_change(case)
    return case


@transaction.atomic
def submit_followup(user, case_id, issue_id, version, data, synthetic=False):
    case = locked_case(user, case_id, version, synthetic)
    issue = case.issues.get(pk=issue_id)
    # Keep the existing work/outcome history and server-side transition checks.
    target = data['staff_followup']
    if target == 'NEW' and issue.status == 'ACKNOWLEDGED':
        target = 'ACKNOWLEDGED'
    values = {name: getattr(issue, name) for name in ['confirmed_priority', 'assignee', 'outcome',
        'evidence_reference', 'customer_confirmation', 'waiting_reason', 'next_review_at']}
    values.update(status=target, action_summary=data['action_summary'])
    if target in ReviewIssue.TERMINAL:
        values['outcome'] = data['action_summary']
    if target == 'WAITING':
        values['waiting_reason'] = 'CUSTOMER' if data['customer_followup'] == 'AWAITING_REPLY' else 'INTERNAL'
    case = update_issue(user, case_id, issue_id, version, values, synthetic)
    issue.refresh_from_db()
    issue.customer_followup = data['customer_followup']
    issue.followup_submitted_at = timezone.now()
    issue.followup_submitted_by = user
    issue.save(update_fields=['customer_followup', 'followup_submitted_at', 'followup_submitted_by', 'updated_at'])
    log(case, user, 'Follow-up submitted',
        f'Customer: {issue.get_customer_followup_display()}\nStaff: {issue.staff_followup_label}\n{issue.action_summary}', issue=issue)
    return case


@transaction.atomic
def close_from_followups(user, case_id, version, synthetic=False):
    case = locked_case(user, case_id, version, synthetic)
    issues = list(case.issues.all())
    if not issues or any(not issue.is_terminal for issue in issues):
        raise ValidationError('Complete every staff follow-up before closing this review.')
    if any(not issue.action_summary.strip() for issue in issues):
        raise ValidationError('Add an action / service note to every issue before closing.')
    summary = '\n'.join(f'{issue.get_category_display()}: {issue.action_summary}' for issue in issues)[:3000]
    service = ((case.service_description + '\n') if case.service_description else '') + summary
    return close_case(user, case_id, version, summary, service[:3000], synthetic)


@transaction.atomic
def assign_case(user, case_id, version, owner, reason, synthetic=False):
    case = locked_case(user, case_id, version, synthetic)
    if not is_review_senior(user):
        raise ValidationError('Only Admin or General Manager can reassign a review.')
    if case.closed_at:
        raise ValidationError('Reopen the review before changing its assignment.')
    if owner and not branch_managers(case.branch).filter(pk=owner.pk).exists():
        raise ValidationError('Choose an active manager working at this branch today.')
    previous = str(case.owner) if case.owner else 'Unassigned'
    case.owner = owner
    case.issues.exclude(status__in=ReviewIssue.TERMINAL).update(assignee=owner)
    log(case, user, 'Manager assigned', f'{previous} → {owner or "Unassigned"}. {reason}')
    save_change(case)
    assignment_outbox(case)
    prepare_acknowledgement(case)
    return case


@transaction.atomic
def close_case(user, case_id, version, summary, service_description, synthetic=False):
    case = locked_case(user, case_id, version, synthetic)
    if case.closed_at:
        raise ValidationError('This review is already closed.')
    if case.requires_senior_closure and not is_review_senior(user):
        raise ValidationError('Admin or General Manager must review and close this sensitive review.')
    states = list(case.issues.values_list('status', flat=True))
    if any(state not in ReviewIssue.TERMINAL for state in states):
        raise ValidationError('Complete every issue before closing this review.')
    if not summary.strip() or not service_description.strip():
        raise ValidationError('A resolution summary and service description are required.')
    actionable = [state for state in states if state != 'NOT_ACTIONABLE']
    if not actionable:
        outcome = 'NOT_ACTIONABLE'
    elif all(state == 'RESOLVED' for state in actionable):
        outcome = 'RESOLVED'
    elif 'RESOLVED' in actionable:
        outcome = 'PARTIAL'
    else:
        outcome = 'UNRESOLVED'
    case.closed_at, case.closed_by = timezone.now(), user
    case.closure_summary, case.service_description, case.closure_outcome = summary, service_description, outcome
    log(case, user, 'Review closed', f'{case.get_closure_outcome_display()}\n{summary}\nService: {service_description}',
        previous=case.status, current='CLOSED')
    save_change(case)
    return case


@transaction.atomic
def reopen_case(user, case_id, version, reason, synthetic=False):
    case = locked_case(user, case_id, version, synthetic)
    if not case.closed_at or not reason.strip():
        raise ValidationError('Only a closed review can be reopened, with a reason.')
    case.closed_at = case.closed_by = None
    case.closure_summary = case.closure_outcome = ''
    log(case, user, 'Review reopened', reason, previous='CLOSED', current='READY')
    save_change(case)
    return case


@transaction.atomic
def update_reply(user, case_id, version, action, text='', note='', synthetic=False):
    case = locked_case(user, case_id, version, synthetic)
    if hasattr(case, 'google_review'):
        raise ValidationError('AWS manages Google replies independently. The application does not approve or publish them.')
    reply = case.reply
    if action != 'no_reply' and not case.snapshot.available:
        raise ValidationError('The source review has expired. Refresh it through an approved source before approving a reply.')
    if action in ['save_reply', 'submit_reply']:
        if not text.strip():
            raise ValidationError('Enter a public reply draft first.')
        reply.text, reply.author = text, user
        reply.revision += 1
        reply.approved_by = reply.approved_at = None
        reply.status = 'DRAFT' if action == 'save_reply' else 'AWAITING_APPROVAL'
    elif action == 'approve_reply':
        if case.requires_senior_followup and not is_review_senior(user):
            raise ValidationError('A sensitive reply requires Admin or General Manager approval.')
        if reply.status != 'AWAITING_APPROVAL':
            raise ValidationError('Submit the current draft for approval first.')
        if text != reply.text:
            raise ValidationError('The reply text was edited. Save and resubmit the draft before approving it.')
        reply.status, reply.approved_by, reply.approved_at = 'APPROVED', user, timezone.now()
    elif action == 'no_reply':
        if not note.strip():
            raise ValidationError('Record why no public reply is planned.')
        if case.requires_senior_followup and not is_review_senior(user):
            raise ValidationError('A senior reviewer must decide communication for a sensitive review.')
        reply.status = 'NO_REPLY'
        reply.approved_by, reply.approved_at = user, timezone.now()
    else:
        raise ValidationError('Choose a supported reply action.')
    reply.save()
    log(case, user, 'Public reply: ' + reply.get_status_display(), note or f'Draft revision {reply.revision}. Nothing posted to Google.')
    # Public-response decisions never acknowledge or close internal work.
    case.version += 1
    case.save(update_fields=['version', 'updated_at'])
    return case
