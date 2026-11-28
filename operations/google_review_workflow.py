"""Simple Google review follow-up.

Negative reviews (1-3 stars, or any review the analysis flags as a complaint) must be
followed up: a call outcome and a short note before they can be closed.  Positive
reviews need nothing and close automatically 24 hours after they arrive.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import GoogleReview, GoogleReviewAction, ReviewCase
from . import review_workflow as workflow

AUTO_CLOSE_NOTE = 'Closed automatically: positive review, no follow-up needed.'


def form_initial(review):
    values = {'version': review.edit_token, 'customer_phone': review.customer_phone, 'call_outcome': review.call_outcome,
        'staff_spoken': review.staff_spoken, 'justification': review.justification}
    if review.followup_updated_at:
        if not review.call_outcome and review.customer_followup == 'CONTACTED':
            values['call_outcome'] = 'SPOKE'
        return values
    if review.case_id:
        case = review.case
        issues = list(case.issues.all())
        values['justification'] = (case.closure_summary or '\n'.join(
            f'{issue.get_category_display()}: {issue.action_summary}' for issue in issues if issue.action_summary))[:3000]
        if issues and all(issue.customer_followup == 'CONTACTED' for issue in issues):
            values['call_outcome'] = 'SPOKE'
    return values


def _close_case(case, actor, *, closed_at, closed_by, outcome, summary, note):
    previous = case.status
    case.status = 'CLOSED'
    case.closed_at, case.closed_by = closed_at, closed_by
    case.closure_summary, case.closure_outcome = summary, outcome
    workflow.log(case, actor, 'Review updated', note, previous=previous, current=case.status)
    case.version += 1
    case.save()


def save_update(review, actor, data, *, intent='save'):
    """Called inside the view's transaction after locking the review row."""
    case = ReviewCase.objects.select_for_update().get(pk=review.case_id) if review.case_id else None
    if case:
        review.case = case
    if data['version'] != review.edit_token:
        return False
    closing = intent == 'close' or (intent != 'reopen' and review.is_closed)
    if intent == 'close' and review.requires_followup:
        errors = {}
        if not data.get('call_outcome'):
            errors['call_outcome'] = 'Choose whether you spoke to the customer.'
        if not (data.get('justification') or '').strip():
            errors['justification'] = 'Write a short note on what happened and what was done.'
        if errors:
            raise ValidationError(errors)
    if case:
        workflow.apply_sensitive_policy(case)
        if closing and case.requires_senior_closure and not workflow.is_review_senior(actor):
            raise ValidationError('An Admin or General Manager must close this sensitive review.')
    now = timezone.now()
    review.customer_phone = data.get('customer_phone') or ''
    review.call_outcome = data.get('call_outcome') or ''
    review.staff_spoken = bool(data.get('staff_spoken'))
    review.justification = (data.get('justification') or '').strip()
    # Keep the older status fields meaningful for reports built on them.
    review.customer_followup = 'CONTACTED' if review.call_outcome == 'SPOKE' else (
        'AWAITING_REPLY' if review.call_outcome == 'NOT_REACHABLE' else 'NOT_CONTACTED')
    review.staff_followup = 'RESOLVED' if closing else ('IN_PROGRESS' if (
        review.call_outcome or review.staff_spoken or review.justification) else 'NEW')
    review.followup_updated_at, review.followup_updated_by = now, actor
    review.followup_revision += 1
    if not closing:
        review.auto_closed = False
    review.closed_at = (review.closed_at or (case.closed_at if case else None) or now) if closing else None
    review.closed_by = (review.closed_by or (case.closed_by if case else None) or actor) if closing else None
    review.closed_review_version = review.google_updated_at if closing else None
    review.save(update_fields=['customer_phone', 'call_outcome', 'staff_spoken', 'customer_followup', 'staff_followup', 'justification',
        'followup_updated_at', 'followup_updated_by', 'followup_revision', 'auto_closed', 'closed_at', 'closed_by',
        'closed_review_version', 'updated_at'])
    note = (f'Review: {"Closed" if closing else "Open"}\nPhone: {review.customer_phone or "Not recorded"}\nCall: {review.get_call_outcome_display() or "Not recorded"}'
        f'\nStaff spoken to: {"Yes" if review.staff_spoken else "No"}\n{review.justification}')
    GoogleReviewAction.objects.create(review=review, actor=actor, action='Review updated',
        review_version=review.google_updated_at, note=note)
    if case:
        if closing:
            outcome = 'RESOLVED' if review.call_outcome == 'SPOKE' or not review.requires_followup else 'UNRESOLVED'
            _close_case(case, actor, closed_at=review.closed_at, closed_by=review.closed_by,
                outcome=outcome, summary=review.justification, note=note)
        else:
            previous = case.status
            case.status = 'IN_PROGRESS' if review.staff_followup == 'IN_PROGRESS' else 'NEW'
            case.closed_at = case.closed_by = None
            case.closure_summary, case.closure_outcome = '', ''
            workflow.log(case, actor, 'Review updated', note, previous=previous, current=case.status)
            case.version += 1
            case.save()
    return True


def auto_close_positive(queryset=None, now=None):
    """Close positive reviews that have been open for 24 hours. Safe to call often."""
    now = now or timezone.now()
    query = (queryset if queryset is not None else GoogleReview.objects.all())
    candidates = query.exclude(GoogleReview.negative_q()).exclude(GoogleReview.closed_q()).filter(
        created_at__lte=now - GoogleReview.AUTO_CLOSE_AFTER,
        google_updated_at__lte=now - GoogleReview.AUTO_CLOSE_AFTER).values_list('pk', flat=True)
    closed = 0
    for pk in list(candidates):
        with transaction.atomic():
            review = GoogleReview.objects.select_for_update().filter(pk=pk).first()
            if not review or review.requires_followup or review.is_closed or review.auto_close_at > now:
                continue
            review.closed_at, review.closed_by = review.auto_close_at, None
            review.closed_review_version = review.google_updated_at
            review.auto_closed = True
            review.staff_followup = 'RESOLVED'
            review.followup_revision += 1
            review.save(update_fields=['closed_at', 'closed_by', 'closed_review_version', 'auto_closed',
                'staff_followup', 'followup_revision', 'updated_at'])
            GoogleReviewAction.objects.create(review=review, actor=None, action='Closed automatically',
                review_version=review.google_updated_at, note=AUTO_CLOSE_NOTE)
            if review.case_id:
                case = ReviewCase.objects.select_for_update().get(pk=review.case_id)
                if not case.closed_at:
                    _close_case(case, None, closed_at=review.closed_at, closed_by=None,
                        outcome='NOT_ACTIONABLE', summary=AUTO_CLOSE_NOTE, note=AUTO_CLOSE_NOTE)
            closed += 1
    return closed
