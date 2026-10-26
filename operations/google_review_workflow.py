"""One review, two statuses and a justification, with the existing history retained."""
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import GoogleReviewAction, ReviewCase, ReviewIssue
from . import review_workflow as workflow


def form_initial(review):
    values = {'version': review.edit_token, 'customer_followup': review.customer_followup,
        'staff_followup': review.staff_followup, 'justification': review.justification}
    if review.followup_updated_at:
        if not review.is_closed and review.staff_followup in ReviewIssue.TERMINAL:
            values['staff_followup'] = 'IN_PROGRESS'
        return values
    if review.case_id:
        case = review.case
        issues = list(case.issues.all())
        values['justification'] = (case.closure_summary or '\n'.join(
            f'{issue.get_category_display()}: {issue.action_summary}' for issue in issues if issue.action_summary))[:3000]
        if issues and all(issue.customer_followup == 'CONTACTED' for issue in issues):
            values['customer_followup'] = 'CONTACTED'
        elif any(issue.customer_followup == 'AWAITING_REPLY' for issue in issues):
            values['customer_followup'] = 'AWAITING_REPLY'
        if case.closed_at:
            values['staff_followup'] = {'UNRESOLVED': 'CLOSED_UNRESOLVED', 'PARTIAL': 'CLOSED_UNRESOLVED',
                'NOT_ACTIONABLE': 'NOT_ACTIONABLE'}.get(case.closure_outcome, 'RESOLVED')
        elif case.status == 'READY' and issues:
            values['staff_followup'] = 'CLOSED_UNRESOLVED' if any(
                issue.status == 'CLOSED_UNRESOLVED' for issue in issues) else 'RESOLVED'
        elif case.status in ('IN_PROGRESS', 'ACKNOWLEDGED'):
            values['staff_followup'] = 'IN_PROGRESS'
        elif case.status == 'WAITING':
            values['staff_followup'] = 'WAITING'
    elif review.simple_closed:
        values['staff_followup'] = 'RESOLVED'
    return values


def save_update(review, actor, data):
    """Called inside the view's transaction after locking the review row."""
    case = ReviewCase.objects.select_for_update().get(pk=review.case_id) if review.case_id else None
    if case:
        review.case = case
    if data['version'] != review.edit_token:
        return False
    closing = data['staff_followup'] in ReviewIssue.TERMINAL
    if case:
        workflow.apply_sensitive_policy(case)
        if closing and case.requires_senior_closure and not workflow.is_review_senior(actor):
            raise ValidationError('An Admin or General Manager must close this sensitive review.')
    now = timezone.now()
    review.customer_followup = data['customer_followup']
    review.staff_followup = data['staff_followup']
    review.justification = data['justification']
    review.followup_updated_at, review.followup_updated_by = now, actor
    review.followup_revision += 1
    review.closed_at = now if closing else None
    review.closed_by = actor if closing else None
    review.closed_review_version = review.google_updated_at if closing else None
    review.save(update_fields=['customer_followup', 'staff_followup', 'justification', 'followup_updated_at',
        'followup_updated_by', 'followup_revision', 'closed_at', 'closed_by', 'closed_review_version', 'updated_at'])
    note = f'Customer: {review.get_customer_followup_display()}\nStaff: {review.get_staff_followup_display()}\n{review.justification}'
    GoogleReviewAction.objects.create(review=review, actor=actor, action='Review updated',
        review_version=review.google_updated_at, note=note)
    if case:
        previous = case.status
        case.status = 'CLOSED' if closing else {'NEW': 'NEW', 'WAITING': 'WAITING'}.get(review.staff_followup, 'IN_PROGRESS')
        case.closed_at, case.closed_by = review.closed_at, review.closed_by
        case.closure_summary = review.justification if closing else ''
        case.closure_outcome = {'RESOLVED': 'RESOLVED', 'CLOSED_UNRESOLVED': 'UNRESOLVED',
            'NOT_ACTIONABLE': 'NOT_ACTIONABLE'}.get(review.staff_followup, '')
        workflow.log(case, actor, 'Review updated', note, previous=previous, current=case.status)
        # The review-level status now drives this case; historical issue statuses
        # must not recalculate it back to the old per-issue workflow.
        case.version += 1
        case.save()
    return True
