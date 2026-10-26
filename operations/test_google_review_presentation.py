"""Reviewer metadata and role boundaries for the Google Review workspace."""
from copy import deepcopy
import json

from django.test import TestCase, override_settings
from django.urls import reverse

from . import test_google_reviews as base
from .models import Customer, GoogleReview, GoogleReviewLocation, ReviewCase, ReviewIssue


@override_settings(**base.CONFIG)
class GoogleReviewPresentationTests(TestCase):
    def setUp(self):
        base.GoogleReviewTests.setUp(self)
        self.payload.update(environment='production', synthetic=False)

    def send(self, payload=None):
        data = self.payload if payload is None else payload
        return self.api.post(reverse('google_review_results'), data=json.dumps(data),
            content_type='application/json', HTTP_X_INTEGRATION_KEY=base.PRODUCTION_TOKEN,
            HTTP_IDEMPOTENCY_KEY=data['event_id'])

    def test_cards_and_details_show_source_metadata_without_creating_a_customer(self):
        self.payload['source']['review_snapshot'].update(
            reviewer_display_name='Example Reviewer', reviewer_is_anonymous=False)
        self.assertEqual(self.send().status_code, 200)
        record = GoogleReview.objects.get()
        self.assertFalse(Customer.objects.exists())
        self.client.force_login(self.people['manager'])
        for url in [reverse('google_review_inbox'), reverse('google_review_detail', args=[record.pk]),
                    reverse('review_inbox'), reverse('review_detail', args=[record.case_id])]:
            with self.subTest(url=url):
                response = self.client.get(url)
                for value in ['Example Reviewer', record.review_text, self.branch.name,
                              record.google_review_id, '20 Oct 2026', 'Google Review']:
                    self.assertContains(response, value)
                self.assertRegex(response.content.decode(), rf'{record.star_rating}\s*(?:<small>)?/\s*5')
                self.assertNotContains(response, 'Tickets')
        self.assertEqual(record.case.customer_label, 'Example Reviewer')

    def test_optional_reviewer_fields_and_anonymous_flag_use_the_same_fallback(self):
        for metadata in [{}, {'reviewer_display_name': None}, {'reviewer_display_name': '   '},
                         {'reviewer_display_name': 'Private name', 'reviewer_is_anonymous': True}]:
            with self.subTest(metadata=metadata):
                data = base.GoogleReviewTests.positive(self)
                data['source']['review_snapshot'].update(metadata)
                data['event_id'] = 'reviewer-' + str(len(metadata)) + str(metadata)
                data['source']['review_id'] = 'REVIEW_' + str(GoogleReview.objects.count())
                data['source']['review_name'] = data['source']['review_name'].rsplit('/', 1)[0] + '/' + data['source']['review_id']
                self.assertEqual(self.send(data).status_code, 200)
                record = GoogleReview.objects.get(google_review_id=data['source']['review_id'])
                self.assertEqual(record.reviewer_name, 'Anonymous reviewer')
                self.client.force_login(self.people['manager'])
                for url in [reverse('google_review_inbox'), reverse('google_review_detail', args=[record.pk])]:
                    response = self.client.get(url)
                    self.assertContains(response, 'Anonymous reviewer')
                    self.assertNotContains(response, 'Private name')
        self.assertFalse(Customer.objects.exists())

    def test_existing_model_records_need_no_reviewer_metadata(self):
        self.assertEqual(self.send().status_code, 200)
        record = GoogleReview.objects.get()
        GoogleReview.objects.filter(pk=record.pk).update(reviewer_display_name='')
        record.refresh_from_db()
        self.assertFalse(record.reviewer_is_anonymous)
        self.assertEqual(record.reviewer_name, 'Anonymous reviewer')

    def test_reviewer_metadata_is_validated(self):
        for key, value in [('reviewer_is_anonymous', 'false'), ('reviewer_is_anonymous', 1),
                           ('reviewer_display_name', []), ('reviewer_display_name', 0)]:
            with self.subTest(key=key, value=value):
                data = deepcopy(self.payload)
                data['source']['review_snapshot'][key] = value
                self.assertEqual(self.send(data).status_code, 400)
        self.assertFalse(GoogleReview.objects.exists())

    def test_full_google_identity_deduplicates_independently_of_reviewer_name(self):
        self.payload['source']['review_snapshot']['reviewer_display_name'] = 'Same Reviewer'
        receipt = self.send().json()
        self.assertEqual(self.send().json(), receipt)
        other = deepcopy(self.payload)
        other['event_id'] = 'another-location-same-short-review-id'
        source = other['source']
        source['location_name'] = 'accounts/101013083477743854199/locations/OTHER_LOCATION'
        source['review_name'] = source['location_name'] + '/reviews/' + source['review_id']
        GoogleReviewLocation.objects.create(environment='production', location_name=source['location_name'], branch=self.branch)
        self.assertEqual(self.send(other).status_code, 200)
        self.assertEqual(GoogleReview.objects.count(), 2)
        self.assertEqual(ReviewCase.objects.count(), 2)
        self.assertEqual(ReviewIssue.objects.count(), 2)
        self.assertFalse(Customer.objects.exists())

    def test_only_admin_can_access_sandbox_analytics_reports_and_diagnostics(self):
        self.send()
        review = GoogleReview.objects.get()
        restricted = [reverse('google_review_analytics'), reverse('review_reports'),
            reverse('google_review_inbox') + '?demo=1', reverse('review_inbox') + '?demo=1',
            reverse('google_review_detail', args=[review.pk]) + '?demo=1',
            reverse('review_detail', args=[review.case_id]) + '?demo=1',
            reverse('review_notifications') + '?demo=1', reverse('google_review_assignments') + '?demo=1',
            reverse('review_create') + '?demo=1']
        for role in ['gm', 'manager', 'employee']:
            self.client.force_login(self.people[role])
            for url in restricted:
                with self.subTest(role=role, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(reverse('google_review_action', args=[review.pk]) + '?demo=1',
                {'action': 'close', 'version': review.google_updated_at.isoformat()}).status_code, 403)
            self.assertEqual(self.client.post(reverse('review_action', args=[review.case_id]) + '?demo=1',
                {'action': 'acknowledge', 'version': review.case.version}).status_code, 403)
            if role != 'employee':
                for url in [reverse('google_review_inbox'), reverse('google_review_detail', args=[review.pk]),
                            reverse('review_inbox'), reverse('review_detail', args=[review.case_id])]:
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    for label in ['Sandbox reviews', 'Branch analytics', 'Issue reporting', 'Review and analysis history',
                                  'AWS analysis received', 'Generated reply', 'name="sentiment"']:
                        self.assertNotContains(response, label)
        self.client.force_login(self.people['gm'])
        queue = self.client.get(reverse('google_review_assignments'))
        self.assertEqual(queue.status_code, 200)
        self.assertNotContains(queue, 'Unmatched store or location events')
        self.assertEqual(self.client.post(reverse('google_review_assignments'), {'event_id': '1'}).status_code, 403)
        self.client.force_login(self.people['admin'])
        for url in [reverse('google_review_analytics'), reverse('review_reports'),
                    reverse('google_review_inbox') + '?demo=1']:
            self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(reverse('google_review_detail', args=[review.pk]))
        for label in ['Sandbox reviews', 'Branch analytics', 'Issue reporting', 'Review and analysis history']:
            self.assertContains(response, label)
        self.assertEqual(self.client.get('/health/').status_code, 200)

    def test_reviewer_metadata_refresh_does_not_change_followup_version(self):
        self.payload['source']['review_snapshot']['reviewer_display_name'] = 'Old Name'
        self.send()
        case = ReviewCase.objects.get()
        version = case.version
        refreshed = deepcopy(self.payload)
        refreshed['event_id'] = 'metadata-refresh'
        refreshed['processing']['analyzed_at'] = '2026-10-20T10:00:10Z'
        refreshed['source']['review_snapshot']['reviewer_is_anonymous'] = True
        self.assertEqual(self.send(refreshed).status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.customer_label, 'Anonymous reviewer')
        self.assertEqual(case.version, version)
        self.assertEqual(GoogleReview.objects.count(), 1)
        self.assertEqual(ReviewIssue.objects.count(), 1)

    def test_anonymous_edit_preserves_followup_and_hides_old_name(self):
        self.payload['source']['review_snapshot']['reviewer_display_name'] = 'Old Name'
        self.send()
        record = GoogleReview.objects.get()
        issue = record.case.issues.get()
        issue.action_summary = 'Keep the manager follow-up.'
        issue.save()
        edited = deepcopy(self.payload)
        edited['event_id'] = 'reviewer-became-anonymous'
        edited['source']['review_snapshot']['reviewer_is_anonymous'] = True
        edited['source']['review_update_time'] = '2026-10-20T10:00:00Z'
        edited['processing'].update(analysis_run_id='anonymous-edit', analyzed_at='2026-10-20T10:00:10Z')
        edited['publication']['review_update_time'] = edited['source']['review_update_time']
        self.assertEqual(self.send(edited).status_code, 200)
        issue.refresh_from_db()
        self.assertEqual(issue.action_summary, 'Keep the manager follow-up.')
        record.refresh_from_db()
        self.assertEqual(record.case.customer_label, 'Anonymous reviewer')
        self.client.force_login(self.people['manager'])
        for url in [reverse('google_review_inbox'), reverse('review_inbox'), reverse('review_detail', args=[record.case_id])]:
            response = self.client.get(url)
            self.assertContains(response, 'Anonymous reviewer')
            self.assertNotContains(response, 'Old Name')
