"""Replay synthetic worker events only into an explicitly isolated development DB."""
import json
import os
from copy import deepcopy
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from operations.google_review_ingestion import ingest_publication, ingest_review
from operations.management.commands.seed_codespaces import validate_environment
from operations.models import Branch, GoogleReviewLocation


class Command(BaseCommand):
    help = 'Seed sandbox Google reviews in Codespaces or a dedicated *_review_preview development database.'

    @transaction.atomic
    def handle(self, *args, **options):
        codespaces = os.environ.get('SALONOPS_CODESPACES') == 'true'
        if codespaces:
            validate_environment(settings, os.environ)
        elif not (settings.DEBUG and settings.APP_ENV == 'development'
                  and settings.DATABASES['default']['NAME'].endswith('_review_preview')):
            raise CommandError('Use the isolated Codespaces database or a *_review_preview development database.')
        branch = Branch.objects.filter(code='CS-DEMO' if codespaces else 'DEMO-RV-A').first()
        if not branch:
            raise CommandError('Run seed_codespaces or seed_review_demo first to create the demo accounts and branch.')
        examples = Path(settings.BASE_DIR) / 'docs/examples'

        def load(name):
            return json.loads((examples / name).read_text())

        sensitive = load('google-review-result.json')
        location = sensitive['source']['review_name'].rsplit('/reviews/', 1)[0]
        GoogleReviewLocation.objects.get_or_create(environment='sandbox', location_name=location, defaults={'branch': branch})
        for name in ('google-review-result.json', 'google-review-positive.json', 'google-review-mixed.json',
                     'google-review-rating-only.json', 'google-review-edited.json'):
            result, status = ingest_review(load(name))
            if status != 200:
                raise CommandError('Demo ingestion needs an active, consistent sandbox location mapping.')
        # An exact duplicate returns its original receipt and leaves manager progress intact.
        ingest_review(sensitive)
        publication = load('google-review-publication.json')
        ingest_publication(publication)
        later_review = load('google-review-positive.json')
        later_review['source'] = deepcopy(publication['source'])
        later_review['source']['review_snapshot'] = {'star_rating': 5, 'text': 'Thank you for a great appointment.'}
        later_review['event_id'] = 'demo-review-after-publication'
        later_review['publication'].update(status='pending', published=False, published_at=None)
        ingest_review(later_review)
        unstaffed, _ = Branch.objects.get_or_create(code='GOOGLE-DEMO-NO-MGR', defaults={'name': 'Google demo · Unassigned'})
        missing = deepcopy(sensitive)
        missing['event_id'] = 'demo-unassigned-review'
        missing['source'].update(location_name='locations/EXAMPLE_UNASSIGNED', review_id='EXAMPLE_UNASSIGNED',
            review_name=sensitive['source']['account_name'] + '/locations/EXAMPLE_UNASSIGNED/reviews/EXAMPLE_UNASSIGNED')
        GoogleReviewLocation.objects.get_or_create(environment='sandbox',
            location_name=missing['source']['review_name'].rsplit('/reviews/', 1)[0], defaults={'branch': unstaffed})
        ingest_review(missing)
        self.stdout.write(self.style.SUCCESS('Sandbox Google reviews ready: /reviews/google/?demo=1'))
        self.stdout.write('Appreciation, mixed, sensitive/edit/duplicate, rating-only, publication-before-ingestion and unassigned examples. No Google writes or live alerts.')
