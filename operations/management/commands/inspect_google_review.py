"""Read-only trace of an ingestion receipt and the review's dashboard scope."""
import hashlib
import json
from uuid import UUID

from django.core.management.base import BaseCommand
from django.db import connections, router
from django.urls import reverse

from operations.models import GoogleReview, GoogleReviewEvent, GoogleReviewLocation


class Command(BaseCommand):
    help = 'Inspect a Google review, ingestion event, database identity and branch mapping without changing data.'

    def add_arguments(self, parser):
        parser.add_argument('--review-id', required=True, type=UUID)
        parser.add_argument('--event-id', required=True)
        parser.add_argument('--environment', choices=['sandbox', 'production'], default='sandbox')
        parser.add_argument('--location-name', help='Full accounts/.../locations/... identity, if the record is missing.')
        parser.add_argument('--expected-branch', help='Expected branch name or code, compared without case differences.')

    def handle(self, *args, **options):
        env = options['environment']
        review_id = UUID(str(options['review_id']))
        aliases = {'dashboard_read': router.db_for_read(GoogleReview),
                   'ingestion_write': router.db_for_write(GoogleReview)}
        fingerprints = {}
        for alias in set(aliases.values()):
            with connections[alias].cursor() as cursor:
                cursor.execute('SELECT current_database(), inet_server_addr()::text, inet_server_port(), current_schema()')
                identity = cursor.fetchone()
            # Compare deployments without exposing hostnames, database names or credentials.
            fingerprints[alias] = hashlib.sha256(repr(identity).encode()).hexdigest()[:24]

        review = GoogleReview.objects.select_related('branch').filter(pk=review_id).first()
        event = GoogleReviewEvent.objects.select_related('review', 'review__branch').filter(
            environment=env, event_id=options['event_id']).first()
        location = options['location_name']
        if not location:
            source_review = review or (event.review if event else None)
            if source_review:
                location = source_review.location_name
            elif event:
                source = event.payload.get('source', {})
                location = source.get('location_name', '')
                if location.startswith('locations/'):
                    location = source.get('account_name', '').rstrip('/') + '/' + location
        mapping = GoogleReviewLocation.objects.select_related('branch').filter(
            environment=env, location_name=location).first() if location else None

        def review_info(item):
            if not item:
                return None
            suffix = '?demo=1' if item.environment == 'sandbox' else ''
            return {'id': str(item.pk), 'environment': item.environment,
                    'profile_key': item.profile_key, 'account_name': item.account_name,
                    'location_name': item.location_name, 'branch_id': item.branch_id,
                    'branch_code': item.branch.code, 'branch_name': item.branch.name,
                    'sentiment': item.sentiment, 'case_id': str(item.case_id) if item.case_id else None,
                    'detail_url': reverse('google_review_detail', args=[item.pk]) + suffix}

        receipt_review = event.receipt.get('review_record_id') if event else None
        persisted = event.review if event else None
        receipt_consistent = (bool(persisted and (event.receipt.get('status') == 'stale'
                                                 or str(persisted.pk) == receipt_review)
                                   and persisted.environment == env)
                              if event and event.receipt.get('committed') and event.kind == 'review' else None)
        expected = options['expected_branch']
        suffix = '?demo=1' if env == 'sandbox' else ''
        report = {
            'database': {'aliases': aliases, 'fingerprints': fingerprints,
                         'dashboard_and_ingestion_same_database':
                         fingerprints[aliases['dashboard_read']] == fingerprints[aliases['ingestion_write']]},
            'scope': {'requested_environment': env, 'tenant_boundary': 'branch',
                      'sandbox_excluded_from_production_views': env == 'sandbox'},
            'requested_review_id': str(review_id),
            'review': review_info(review),
            'event': {'event_id': event.event_id, 'environment': event.environment,
                      'status': event.status, 'receipt': event.receipt,
                      'synthetic': event.payload.get('synthetic', False),
                      'requested_review_matches_event': event.review_id == review_id,
                      'receipt_matches_persisted_review': receipt_consistent,
                      'persisted_review': review_info(persisted)} if event else None,
            'location_name': location,
            'mapping': {'environment': mapping.environment, 'branch_id': mapping.branch_id,
                        'branch_code': mapping.branch.code, 'branch_name': mapping.branch.name,
                        'active': mapping.active and mapping.branch.active,
                        'expected_branch_matches': expected.casefold() in
                        {mapping.branch.name.casefold(), mapping.branch.code.casefold()} if expected else None,
                        'review_branch_matches': review.branch_id == mapping.branch_id if review else None}
                       if mapping else None,
            'all_reviews_url': reverse('google_review_inbox') + suffix,
            'note': 'Run this in the same deployment/container that received the POST. '
                    'An absent record in another database does not invalidate a receipt. '
                    'Positive reviews without follow-up appear in All reviews and need no investigation ticket.',
        }
        self.stdout.write(json.dumps(report, indent=2))
