"""AWS receive-only integration. Nothing here calls Google or authorizes a reply."""
import hashlib
import json
import re
from datetime import UTC, timedelta

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (Branch, GoogleReview, GoogleReviewEvent, GoogleReviewLocation, GoogleReplyPublication,
    ReviewCase, ReviewSnapshot, ReviewReply, ReviewAcknowledgement, ReviewIssue, ReviewNotification)
from . import review_workflow as workflow


class EventConflict(Exception):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def text(data, key, maximum, required=False):
    value = data.get(key, '')
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValidationError(f'{key} must be a {"nonempty " if required else ""}string of at most {maximum} characters.')
    return value


def timestamp(data, key):
    raw = text(data, key, 50, True)
    try:
        value = parse_datetime(raw)
    except ValueError:
        value = None
    if value is None or timezone.is_naive(value):
        raise ValidationError(f'{key} must be an ISO 8601 timestamp with a timezone.')
    return value


def integer(data, key, low, high):
    value = data.get(key)
    if type(value) is not int or not low <= value <= high:
        raise ValidationError(f'{key} must be an integer between {low} and {high}.')
    return value


def boolean(data, key):
    value = data.get(key, False)
    if type(value) is not bool:
        raise ValidationError(f'{key} must be a boolean.')
    return value


def envelope(payload):
    if not isinstance(payload, dict) or payload.get('schema_version') not in ('hairship.review-result.v1', 'hairship.review-publication.v1'):
        raise ValidationError('Use hairship.review-result.v1 or hairship.review-publication.v1.')
    env = payload.get('environment')
    if env not in ('sandbox', 'production'):
        raise ValidationError('environment must be sandbox or production.')
    event_id = text(payload, 'event_id', 160, True)
    source = payload.get('source')
    if not isinstance(source, dict) or source.get('provider') != 'google_business_profile':
        raise ValidationError('source.provider must be google_business_profile.')
    if boolean(payload, 'synthetic') and env == 'production':
        raise ValidationError('Synthetic events must use the sandbox environment and credential.')
    name = text(source, 'review_name', 400, True)
    match = re.fullmatch(r'accounts/([^/]+)/locations/([^/]+)/reviews/([^/]+)', name)
    if not match:
        raise ValidationError('review_name must be the full accounts/.../locations/.../reviews/... resource name.')
    if len(match[1]) > 100 or len(match[2]) > 140 or len(match[3]) > 160:
        raise ValidationError('Google resource identifier is too long.')
    account = f'accounts/{match[1]}'
    location = f'{account}/locations/{match[2]}'
    if source.get('account_name') != account or source.get('location_name') not in (location, f'locations/{match[2]}') or source.get('review_id') != match[3]:
        raise ValidationError('Source account, location and review IDs must match review_name.')
    processing = payload.get('processing')
    if not isinstance(processing, dict):
        raise ValidationError('processing must be an object.')
    worker = text(processing, 'worker_version', 120)
    revision = integer(processing, 'result_revision', 0, 9223372036854775807) if 'result_revision' in processing else 0
    if worker in ('hairship-gmb-sqs-worker-v2', 'hairship-gmb-sqs-worker-v3') and 'result_revision' not in processing:
        raise ValidationError('processing.result_revision is required for the queue worker.')
    store_code = text(source, 'store_code', 120, required='store_code' in source)
    if any(ord(char) < 32 or ord(char) == 127 for char in store_code):
        raise ValidationError('store_code must not contain control characters.')
    store_code = store_code.strip()
    return {'environment': env, 'event_id': event_id, 'review_name': name,
            'account_name': account, 'location_name': location, 'google_review_id': match[3],
            'store_code': store_code,
            'profile_key': text(source, 'profile_key', 120), 'processing': processing,
            'analysis_version': text(processing, 'analysis_run_id', 160, True),
            'analyzed_at': timestamp(processing, 'analyzed_at'), 'result_revision': revision,
            'fetched_at': timestamp(source, 'fetched_at') if source.get('fetched_at') else None}


def publication_data(data, context):
    if not isinstance(data, dict):
        raise ValidationError('publication must be an object.')
    state = data.get('status')
    if state not in ('pending', 'submitted', 'published', 'failed', 'uncertain', 'superseded', 'skipped', 'human_review'):
        raise ValidationError('Unknown publication status.')
    published_at = timestamp(data, 'published_at') if data.get('published_at') else None
    actual_text = text(data, 'reply_text', 4000)
    failure = text({**data, 'error_code': data.get('error_code') or ''}, 'error_code', 120)
    if boolean(data, 'published') != (state == 'published'):
        raise ValidationError('publication.published must agree with publication.status.')
    if state == 'published' and (not actual_text.strip() or not published_at):
        raise ValidationError('Published outcomes require reply_text and published_at.')
    if state == 'failed' and not failure:
        raise ValidationError('Failed outcomes require error_code.')
    submitted_at = timestamp(data, 'submitted_at') if data.get('submitted_at') else None
    google = data.get('google_reply')
    if google is not None and not isinstance(google, dict):
        raise ValidationError('publication.google_reply must be an object or null.')
    google = google or {}
    google_time = timestamp(google, 'updateTime') if google.get('updateTime') else None
    observed = context['fetched_at'] or context['analyzed_at']
    occurred = timestamp(data, 'occurred_at') if data.get('occurred_at') else published_at or submitted_at or observed
    # Across analysis runs, only affirmative Google evidence can replace an
    # approved reply. A fresh UUID or a newer uncertain/skipped observation cannot.
    rank = 0
    if state in ('published', 'superseded') or state == 'failed' and google.get('reviewReplyState') == 'REJECTED':
        rank = 3
    elif state == 'failed':
        rank = 2
    elif state == 'submitted':
        rank = 1
    evidence_at = (observed if state == 'superseded' else google_time or published_at or submitted_at or occurred)
    return {'review_updated_at': timestamp(data, 'review_update_time'),
        'analysis_version': context['analysis_version'], 'result_revision': context['result_revision'],
        'evidence_rank': rank, 'evidence_at': evidence_at, 'observed_at': observed,
        'occurred_at': occurred, 'status': state,
        'actual_reply_text': actual_text if state in ('published', 'submitted') else '', 'published_at': published_at,
        'failure_code': failure, 'reply_kind': text(data, 'reply_kind', 60)}


def review_data(payload):
    base = envelope(payload)
    if payload['schema_version'] != 'hairship.review-result.v1':
        raise ValidationError('Review ingestion requires hairship.review-result.v1.')
    source = payload['source']
    review, analysis = source.get('review_snapshot'), payload.get('analysis')
    if not isinstance(review, dict) or not isinstance(analysis, dict):
        raise ValidationError('review and analysis must be JSON objects.')
    created, updated = timestamp(source, 'review_create_time'), timestamp(source, 'review_update_time')
    if updated < created:
        raise ValidationError('updated_at cannot precede created_at.')
    sentiment = analysis.get('sentiment', 'unknown')
    if sentiment not in ('positive', 'negative', 'mixed', 'neutral', 'unknown'):
        raise ValidationError('Unsupported sentiment.')
    priority = text(analysis, 'priority', 10).upper() or 'NORMAL'
    if priority not in dict(ReviewIssue.PRIORITIES):
        raise ValidationError('Unsupported priority.')
    issues = {}
    for key in ('issues', 'complaints'):
        entries = analysis.get(key, [])
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValidationError(f'analysis.{key} must be an array of at most 100 issue objects.')
        for item in entries:
            if not isinstance(item, dict):
                raise ValidationError('Issue entries must be objects.')
            aliases = {'injury_or_health': 'HEALTH', 'health': 'HEALTH', 'injury': 'HEALTH',
                'waiting_time': 'WAITING', 'wait_time': 'WAITING', 'service_quality': 'SERVICE',
                'staff_communication': 'STAFF', 'staff_attitude': 'STAFF', 'sensitive_conduct': 'CONDUCT'}
            raw_category = text(item, 'category', 100).lower() or 'other'
            category = aliases.get(raw_category, raw_category.upper())
            if category not in dict(ReviewIssue.CATEGORIES):
                category = 'OTHER'
            description = text(item, 'reported_issue', 2000, True)
            identity = text(item, 'issue_id', 120) or 'derived:' + fingerprint([category, ' '.join(description.casefold().split())])
            item_priority = text(item, 'priority', 10).upper() or priority
            if category not in dict(ReviewIssue.CATEGORIES) or item_priority not in dict(ReviewIssue.PRIORITIES):
                raise ValidationError('Unsupported issue category or priority.')
            values = {'category': category, 'reported_issue': description,
                      'ai_priority': 'URGENT' if category == 'HEALTH' else item_priority}
            if identity in issues and issues[identity] != values:
                raise ValidationError('The same issue_id cannot describe different issues in one event.')
            issues[identity] = values
    if len(issues) > 100:
        raise ValidationError('At most 100 distinct issues are supported per review.')
    rating = integer(review, 'star_rating', 1, 5)
    review_text = text(review, 'text', 6000)
    human_review = boolean(analysis, 'requires_human_review')
    human_followup = boolean(analysis, 'case_followup_requires_human')
    human = human_review or human_followup
    complaint = boolean(analysis, 'complaint_detected')
    sensitive = any(i['category'] in ('HEALTH', 'CONDUCT') for i in issues.values())
    ranks = {'LOW': 1, 'NORMAL': 2, 'HIGH': 3, 'URGENT': 4}
    priority = max([priority] + [i['ai_priority'] for i in issues.values()], key=ranks.get)
    if any(i['category'] == 'HEALTH' for i in issues.values()):
        priority = 'URGENT'
    publication = publication_data(payload['publication'], base) if 'publication' in payload else None
    base.update(star_rating=rating, review_text=review_text,
        reviewer_display_name=text(review, 'reviewer_display_name', 120),
        google_created_at=created, google_updated_at=updated,
        analysis=analysis, sentiment=sentiment, priority=priority, requires_human_review=human_review,
        needs_followup=bool(issues or human or complaint or (rating <= 2 and not review_text.strip())),
        generated_reply=text(analysis, 'reply_text', 4000) or text(payload, 'generated_reply', 4000) or text(payload.get('publication', {}), 'reply_text', 4000),
        issues=issues, incoming_issue_count=len(analysis.get('issues', [])), sensitive=sensitive)
    base['version_fingerprint'] = source_fingerprint(created, updated, rating, review_text)
    base['publication'] = publication
    if base['publication'] and base['publication']['review_updated_at'] != updated:
        raise ValidationError('An ingestion publication must describe the ingested review version.')
    return base


def source_fingerprint(created, updated, rating, review_text):
    # fetched_at, existing_reply, analysis diagnostics and moderation results may
    # legitimately change while the actual Google customer review is unchanged.
    return fingerprint([created.astimezone(UTC).isoformat(), updated.astimezone(UTC).isoformat(), rating, review_text])


def lock(namespace, value):
    # Stable per-event and per-review transaction locks also serialize first inserts.
    key = int.from_bytes(hashlib.sha256(f'{namespace}:{value}'.encode()).digest()[:8], 'big', signed=True)
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [key])


def begin_event(payload, values, kind):
    env, identity = values['environment'], values['event_id']
    lock('google-event', f'{env}:{identity}')
    digest = fingerprint(payload)
    existing = GoogleReviewEvent.objects.filter(environment=env, event_id=identity).first()
    if existing:
        if existing.fingerprint != digest or existing.kind != kind:
            raise EventConflict('event_id was already used for a different payload.')
        if existing.status == 'committed':
            return existing, True
        return existing, False
    return GoogleReviewEvent.objects.create(environment=env, event_id=identity, kind=kind,
        fingerprint=digest, payload=payload, review_name=values['review_name']), False


def reconcile_publication(review):
    matching = GoogleReplyPublication.objects.filter(environment=review.environment,
        review_name=review.review_name, review_updated_at=review.google_updated_at)
    runs = {}
    for result in matching.select_related('event').order_by('analysis_version', '-result_revision', '-occurred_at', '-event__event_id'):
        runs.setdefault(result.analysis_version, result)
    def evidence_order(result):
        return (result.evidence_rank, result.evidence_at or result.occurred_at,
                result.observed_at or result.occurred_at, result.status == 'published', result.event.event_id)
    latest = max(runs.values(), key=evidence_order, default=None)
    review.publication_status = latest.status if latest else 'pending'
    review.published_text = latest.actual_reply_text if latest and latest.status == 'published' else ''
    review.published_at = latest.published_at if latest and latest.status == 'published' else None
    review.publication_failure_code = latest.failure_code if latest else ''
    review.save(update_fields=['publication_status', 'published_text', 'published_at', 'publication_failure_code', 'updated_at'])
    GoogleReviewEvent.objects.filter(environment=review.environment, review_name=review.review_name,
        kind='publication', review__isnull=True).update(review=review)


def save_publication(event, values, data):
    GoogleReplyPublication.objects.get_or_create(event=event, defaults={
        'environment': values['environment'], 'review_name': values['review_name'], **data})


def sync_case(review, data, same_version=False):
    case = ReviewCase.objects.select_for_update().get(pk=review.case_id) if review.case_id else None
    if case:
        review.case = case
    if not case and not data['needs_followup']:
        return
    if not case:
        managers = list(workflow.branch_managers(review.branch))
        owner = managers[0] if len(managers) == 1 else None
        case = ReviewCase.objects.create(branch=review.branch, owner=owner,
            source_key='google:' + review.environment + ':' + hashlib.sha256(review.review_name.encode()).hexdigest(),
            synthetic=review.environment == 'sandbox',
            subject=f'Google review · {review.star_rating}/5 · follow-up',
            customer_label=review.reviewer_display_name, sensitive=data['sensitive'],
            case_followup_requires_human=True, ai_requires_human_review=data['requires_human_review'])
        workflow.apply_sensitive_policy(case)
        case.save()
        ReviewSnapshot.objects.create(case=case, text=review.review_text, rating=review.star_rating)
        ReviewReply.objects.create(case=case)
        ReviewAcknowledgement.objects.create(case=case, template_key='aws-owned', status='NO_REPLY')
        review.case = case
        review.save(update_fields=['case'])
        workflow.log(case, None, 'Google review received', 'Internal follow-up only. AWS owns public reply publication.')
    else:
        # Source edits never replace staff notes, confirmations, assignees or outcomes.
        case.sensitive = case.sensitive or data['sensitive']
        case.ai_requires_human_review = data['requires_human_review']
        case.case_followup_requires_human = case.case_followup_requires_human or data['needs_followup']
        snapshot = case.snapshot
        snapshot.text, snapshot.rating = review.review_text, review.star_rating
        snapshot.fetched_at, snapshot.expires_at = timezone.now(), timezone.now() + timedelta(days=30)
        snapshot.save()
        if not case.owner_id:
            managers = list(workflow.branch_managers(review.branch))
            if len(managers) == 1:
                case.owner = managers[0]
                workflow.log(case, None, 'Manager assigned', 'Eligible manager found for the mapped branch.')
    workflow.apply_sensitive_policy(case)
    incoming, reconciled = data['issues'], set()
    exact = {issue.aws_issue_id: issue for issue in case.issues.filter(aws_issue_id__in=incoming)}
    reserved = {issue.pk for issue in exact.values()}
    new_issues = 0
    for identity, source in incoming.items():
        issue = exact.get(identity)
        if not issue and same_version and identity.startswith('derived:'):
            # Independent preparation retries can reword the same Google issue.
            # Reconcile one-to-one within its category rather than cloning tasks.
            issue = case.issues.filter(source_review_updated_at=review.google_updated_at, category=source['category'],
                aws_issue_id__startswith='derived:').exclude(pk__in=reconciled | reserved).first()
        created = issue is None
        if created:
            issue = ReviewIssue.objects.create(case=case, aws_issue_id=identity, **source, assignee=case.owner,
                source_review_updated_at=review.google_updated_at)
            new_issues += 1
        else:
            for key, value in source.items():
                setattr(issue, key, value)
            issue.source_active = True
            issue.source_review_updated_at = review.google_updated_at
            issue.save(update_fields=[*source, 'source_active', 'source_review_updated_at', 'updated_at'])
        reconciled.add(issue.pk)
    case.issues.exclude(aws_issue_id='').exclude(pk__in=reconciled).update(source_active=False)
    if case.closed_at and new_issues:
        workflow.log(case, None, 'Case reopened by new issue',
            f'Previous closure: {case.closure_summary}', previous=case.status, current='NEW')
        case.closed_at = case.closed_by = None
        case.closure_summary = case.closure_outcome = ''
    workflow.log(case, None, 'AWS analysis received',
        f'Analysis version {data["analysis_version"]}. Prior staff actions retained; {new_issues} new issue(s).')
    workflow.save_change(case)
    if case.issues.filter(category='HEALTH').exists() and review.priority != 'URGENT':
        review.priority = 'URGENT'
        review.save(update_fields=['priority'])
    if case.owner_id:
        ReviewNotification.objects.get_or_create(
            dedupe_key=f'google:{review.pk}:{review.google_updated_at.astimezone(UTC).isoformat()}:{case.owner_id}',
            defaults={'case': case, 'recipient': case.owner, 'status': 'PREVIEW' if case.synthetic else 'PENDING'})


def receipt(event, review, incoming_issue_count):
    case = review.case
    assigned = bool(case and case.owner_id and workflow.branch_managers(case.branch).filter(pk=case.owner_id).exists())
    return {'event_id': event.event_id, 'status': 'committed', 'committed': True,
        'review_record_id': str(review.pk), 'case_id': str(case.pk) if case else None,
        'issues_saved_count': incoming_issue_count, 'assignment_confirmed': assigned}


@transaction.atomic
def ingest_review(payload):
    data = review_data(payload)
    event, duplicate = begin_event(payload, data, 'review')
    if duplicate:
        return event.receipt, 200
    lock('google-review', data['environment'] + ':' + data['review_name'])
    mapping = GoogleReviewLocation.objects.select_for_update().filter(
        environment=data['environment'], location_name=data['location_name']).first()
    branch = None
    unmapped_status = 'unmapped_location'
    if data['store_code']:
        branch = Branch.objects.select_for_update().filter(store_code=data['store_code'], active=True).first()
        unmapped_status = 'unmapped_store_code'
        if branch and mapping:
            if mapping.branch_id != branch.pk:
                raise EventConflict('Store code and Google location map to different branches.')
            if not mapping.active:
                branch = None
                unmapped_status = 'unmapped_location'
    elif mapping and mapping.active:
        branch = Branch.objects.select_for_update().filter(pk=mapping.branch_id, active=True).first()
    if not branch:
        event.status = 'quarantined'
        event.receipt = {'event_id': event.event_id, 'status': unmapped_status, 'committed': False}
        event.save(update_fields=['status', 'receipt'])
        return event.receipt, 422
    review = GoogleReview.objects.select_for_update().filter(
        environment=data['environment'], review_name=data['review_name']).first()
    source_fields = ['account_name', 'location_name', 'google_review_id', 'star_rating', 'review_text',
        'reviewer_display_name', 'google_created_at', 'google_updated_at', 'analysis_version', 'analysis',
        'version_fingerprint', 'sentiment', 'priority', 'requires_human_review', 'needs_followup', 'generated_reply',
        'processing', 'analyzed_at', 'profile_key', 'result_revision']
    same_version = bool(review and review.google_updated_at == data['google_updated_at'])
    if review and review.branch_id != branch.pk:
        raise EventConflict('Review branch changed. Resolve the existing review assignment before replaying.')
    if same_version and source_fingerprint(review.google_created_at, review.google_updated_at,
            review.star_rating, review.review_text) != data['version_fingerprint']:
        raise EventConflict('The same Google review version cannot have different customer text or rating.')
    highest = GoogleReplyPublication.objects.filter(environment=data['environment'], review_name=data['review_name'],
        review_updated_at=data['google_updated_at'], analysis_version=data['analysis_version']).aggregate(value=Max('result_revision'))['value']
    if same_version and review.analysis_version == data['analysis_version']:
        highest = max(highest if highest is not None else -1, review.result_revision)
    stale = bool(review and (data['google_updated_at'] < review.google_updated_at or
        same_version and 'result_revision' in data['processing'] and highest is not None and data['result_revision'] <= highest))
    if not review:
        review = GoogleReview.objects.create(environment=data['environment'], review_name=data['review_name'],
            branch=branch, **{key: data[key] for key in source_fields})
        sync_case(review, data)
    elif not stale:
        analysis_changed = review.analysis != data['analysis']
        update_source = (not same_version or data['analyzed_at'] > review.analyzed_at or
            review.analysis_version == data['analysis_version'] and data['result_revision'] > review.result_revision)
        if update_source:
            for key in source_fields:
                setattr(review, key, data[key])
            review.save(update_fields=[*source_fields, 'updated_at'])
        if not same_version or analysis_changed or not review.case_id and data['needs_followup']:
            sync_case(review, data, same_version=same_version)
    event.review, event.status = review, 'committed'
    if data['publication']:
        save_publication(event, data, data['publication'])
    reconcile_publication(review)
    event.receipt = ({'event_id': event.event_id, 'status': 'stale', 'committed': True} if stale
        else receipt(event, review, data['incoming_issue_count']))
    event.save(update_fields=['review', 'status', 'receipt'])
    return event.receipt, 200


@transaction.atomic
def ingest_publication(payload):
    values = envelope(payload)
    data = publication_data(payload.get('publication'), values)
    event, duplicate = begin_event(payload, values, 'publication')
    if duplicate:
        return event.receipt, 200
    lock('google-review', values['environment'] + ':' + values['review_name'])
    save_publication(event, values, data)
    review = GoogleReview.objects.select_for_update().filter(
        environment=values['environment'], review_name=values['review_name']).first()
    if review:
        reconcile_publication(review)
    event.review = review
    event.receipt = {'event_id': event.event_id, 'status': 'committed', 'committed': True,
        'review_record_id': str(review.pk) if review else None, 'reconciliation_pending': review is None}
    event.save(update_fields=['review', 'receipt'])
    return event.receipt, 200
