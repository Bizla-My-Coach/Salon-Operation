from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, close_old_connections, connections
from django.core.management import call_command, CommandError
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .models import (Branch, GoogleReview, GoogleReviewEvent, GoogleReviewLocation, GoogleReplyPublication,
    GoogleReviewAction, ReviewCase, ReviewIssue, ReviewNotification)
from .google_review_ingestion import ingest_review


TOKEN = 'sandbox-test-token-' + 's' * 40
PRODUCTION_TOKEN = 'production-test-token-' + 'p' * 40
EXAMPLE = json.loads((Path(__file__).resolve().parent.parent / 'docs/examples/google-review-result.json').read_text())
LOCATION = EXAMPLE['source']['review_name'].rsplit('/reviews/', 1)[0]
CONFIG = {'REVIEW_API_TOKENS': {'sandbox': TOKEN, 'production': PRODUCTION_TOKEN},
    'REVIEW_API_REQUIRE_HTTPS': False,
    'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher'],
    'STORAGES': {'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}}


@override_settings(**CONFIG)
class GoogleReviewTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(code='GOOGLE-A', name='Google branch A')
        self.other_branch = Branch.objects.create(code='GOOGLE-B', name='Private Google branch B')
        self.people = {}
        for name, role, branch in [('manager', 'MANAGER', self.branch), ('other', 'MANAGER', self.other_branch),
                                   ('admin', 'ADMIN', None), ('gm', 'GENERAL_MANAGER', None), ('employee', 'EMPLOYEE', self.branch)]:
            user = User.objects.create_user('google-' + name, password='Test-only-password!')
            user.profile.role, user.profile.branch = role, branch
            user.profile.save()
            self.people[name] = user
        for env in ('sandbox', 'production'):
            GoogleReviewLocation.objects.create(environment=env, location_name=LOCATION, branch=self.branch)
        self.payload = deepcopy(EXAMPLE)
        self.api = Client(enforce_csrf_checks=True)

    def send(self, payload=None, publication=False, token=TOKEN):
        return self.api.post(reverse('google_review_publications' if publication else 'google_review_ingest'),
            data=json.dumps(payload or self.payload), content_type='application/json', HTTP_AUTHORIZATION='Bearer ' + token)

    def positive(self):
        data = deepcopy(self.payload)
        data['source']['review_snapshot'] = {'star_rating': 5, 'text': 'A lovely appointment.'}
        data['analysis'] = {'sentiment': 'positive', 'complaint_detected': False, 'issues': [],
            'requires_human_review': False, 'review_reason': 'none', 'case_followup_requires_human': False, 'human_review_scope': 'none'}
        return data

    def test_exact_sensitive_example_commits_real_ids_and_independent_publication(self):
        response = self.send()
        self.assertEqual(response.status_code, 200)
        record = GoogleReview.objects.get()
        case = record.case
        self.assertEqual(response.json(), {'event_id': self.payload['event_id'], 'status': 'committed',
            'committed': True, 'review_record_id': str(record.pk), 'case_id': str(case.pk),
            'issues_saved_count': 1, 'assignment_confirmed': True})
        self.assertEqual(record.analysis, self.payload['analysis'])
        self.assertEqual(record.processing, self.payload['processing'])
        self.assertEqual(record.events.get().payload, self.payload)
        self.assertEqual(record.publication_status, 'published')
        self.assertEqual(record.published_text, self.payload['publication']['reply_text'])
        self.assertTrue(record.requires_human_review)
        self.assertEqual(case.owner, self.people['manager'])
        self.assertIsNone(case.closed_at)
        self.assertEqual(case.issues.get().ai_priority, 'URGENT')
        self.assertEqual(case.acknowledgement.status, 'NO_REPLY')
        self.assertEqual(case.notifications.count(), 1)
        self.assertIn('no-store', response['Cache-Control'])

    def test_duplicate_returns_original_receipt_after_staff_actions(self):
        first = self.send().json()
        record = GoogleReview.objects.get()
        record.case.owner = None
        record.case.save()
        second = self.send()
        self.assertEqual(second.json(), first)
        self.assertEqual(GoogleReview.objects.count(), 1)
        self.assertEqual(ReviewCase.objects.count(), 1)
        self.assertEqual(ReviewIssue.objects.count(), 1)
        self.assertEqual(ReviewNotification.objects.count(), 1)
        self.assertEqual(GoogleReviewEvent.objects.count(), 1)

    def test_reused_event_id_with_different_payload_is_a_conflict(self):
        self.send()
        data = deepcopy(self.payload)
        data['source']['review_snapshot']['text'] = 'Different content'
        self.assertEqual(self.send(data).status_code, 409)
        self.assertEqual(GoogleReviewEvent.objects.count(), 1)

    def test_positive_can_close_and_reopen_without_complaint_or_investigation(self):
        data = self.positive()
        result = self.send(data).json()
        self.assertIsNone(result['case_id'])
        self.assertFalse(result['assignment_confirmed'])
        self.assertFalse(ReviewCase.objects.exists())
        self.assertFalse(ReviewNotification.objects.exists())
        record = GoogleReview.objects.get()
        self.client.force_login(self.people['manager'])
        url = reverse('google_review_action', args=[record.pk]) + '?demo=1'
        values = {'action': 'close', 'version': record.google_updated_at.isoformat()}
        self.assertEqual(self.client.post(url, values).status_code, 302)
        self.client.post(url, values)
        record.refresh_from_db()
        self.assertTrue(record.simple_closed)
        self.assertEqual(record.closed_by, self.people['manager'])
        self.assertEqual(GoogleReviewAction.objects.count(), 1)
        self.assertFalse(ReviewCase.objects.exists())
        self.client.post(url, {**values, 'action': 'reopen', 'note': 'Check this appreciation again.'})
        record.refresh_from_db()
        self.assertFalse(record.simple_closed)
        self.assertEqual(GoogleReviewAction.objects.count(), 2)

    def test_mixed_high_rating_still_creates_complaint_case(self):
        self.payload['source']['review_snapshot']['star_rating'] = 5
        self.payload['analysis']['sentiment'] = 'mixed'
        self.assertEqual(self.send().status_code, 200)
        self.assertTrue(GoogleReview.objects.get().needs_followup)
        self.assertEqual(ReviewIssue.objects.count(), 1)

    def test_rating_only_needs_clarification_without_inventing_issue(self):
        data = self.positive()
        data['source']['review_snapshot'] = {'star_rating': 1, 'text': ''}
        result = self.send(data).json()
        self.assertIsNotNone(result['case_id'])
        self.assertEqual(result['issues_saved_count'], 0)
        self.assertEqual(ReviewNotification.objects.count(), 1)
        self.client.force_login(self.people['manager'])
        case = ReviewCase.objects.get()
        self.assertContains(self.client.get(reverse('review_inbox') + '?demo=1'), case.subject)
        self.assertContains(self.client.get(reverse('review_detail', args=[case.pk]) + '?demo=1'), 'Record clarification outcome')
        response = self.client.post(reverse('review_action', args=[case.pk]) + '?demo=1',
            {'action': 'close_no_issue', 'version': case.version, 'reason': 'Contacted customer; no specific concern was identified.'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ReviewIssue.objects.exists())

    def test_review_edit_preserves_staff_actions_and_history_and_rejects_older_overwrite(self):
        self.send()
        record = GoogleReview.objects.get()
        issue = record.case.issues.get()
        issue.status, issue.action_summary, issue.customer_confirmation = 'IN_PROGRESS', 'Manager arranged a follow-up.', 'CONFIRMED'
        issue.save()
        data = deepcopy(self.payload)
        data['event_id'] = 'edited-review'
        data['source']['review_update_time'] = '2026-10-20T09:05:00Z'
        data['source']['review_snapshot']['text'] = 'Edited review with more context.'
        data['processing'].update(analysis_run_id='edited-analysis', analyzed_at='2026-10-20T09:05:10Z')
        data['publication'].update(review_update_time='2026-10-20T09:05:00Z', published_at='2026-10-20T09:05:15Z')
        self.assertEqual(self.send(data).status_code, 200)
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'IN_PROGRESS')
        self.assertEqual(issue.action_summary, 'Manager arranged a follow-up.')
        self.assertEqual(issue.customer_confirmation, 'CONFIRMED')
        self.assertEqual(ReviewIssue.objects.count(), 1)
        stale = deepcopy(self.payload)
        stale['event_id'] = 'late-old-review'
        self.assertEqual(self.send(stale).status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.review_text, data['source']['review_snapshot']['text'])
        self.assertEqual(record.events.count(), 3)

    def test_removed_issues_retain_history_and_new_issues_reopen_closed_case(self):
        self.send()
        record = GoogleReview.objects.get()
        old = record.case.issues.get()
        old.status, old.action_summary = 'RESOLVED', 'Original manager action.'
        old.save()
        from .review_workflow import close_from_followups
        close_from_followups(self.people['manager'], record.case_id, record.case.version, synthetic=True)
        data = deepcopy(self.payload)
        data['event_id'] = 'new-issue-after-close'
        data['source']['review_update_time'] = '2026-10-20T09:01:00Z'
        data['publication']['review_update_time'] = '2026-10-20T09:01:00Z'
        data['processing'].update(analysis_run_id='run-2', analyzed_at='2026-10-20T09:01:00Z')
        data['analysis']['issues'][0]['reported_issue'] = 'An additional distinct reported injury concern.'
        self.assertEqual(self.send(data).status_code, 200)
        old.refresh_from_db()
        record.case.refresh_from_db()
        self.assertFalse(old.source_active)
        self.assertEqual(old.action_summary, 'Original manager action.')
        self.assertEqual(record.case.issues.count(), 2)
        self.assertIsNone(record.case.closed_at)

    def test_publication_before_review_reconciles_without_closing_investigation(self):
        event = deepcopy(self.payload)
        event.update(schema_version='hairship.review-publication.v1', event_id='publication-first')
        first = self.send(event, publication=True)
        self.assertTrue(first.json()['reconciliation_pending'])
        self.assertEqual(GoogleReplyPublication.objects.count(), 1)
        self.assertFalse(GoogleReview.objects.exists())
        self.payload['publication'].update(status='pending', published=False, published_at=None)
        self.assertEqual(self.send().status_code, 200)
        review = GoogleReview.objects.get()
        self.assertEqual(review.publication_status, 'published')
        self.assertIsNone(review.case.closed_at)
        self.assertEqual(self.send(event, publication=True).json(), first.json())
        self.assertEqual(GoogleReviewEvent.objects.filter(review=review).count(), 2)

    def test_failed_and_older_publications_do_not_change_staff_status_or_current_version(self):
        self.send()
        event = deepcopy(self.payload)
        event.update(schema_version='hairship.review-publication.v1', event_id='failed-publication')
        event['publication'].update(status='failed', published=False, published_at=None,
            error_code='GOOGLE_TIMEOUT', occurred_at='2026-10-20T09:00:20Z')
        self.assertEqual(self.send(event, publication=True).status_code, 200)
        review = GoogleReview.objects.get()
        self.assertEqual(review.publication_status, 'failed')
        self.assertEqual(review.publication_failure_code, 'GOOGLE_TIMEOUT')
        self.assertEqual(review.case.status, 'NEW')
        old = deepcopy(event)
        old['event_id'] = 'older-publication'
        old['publication'].update(status='pending', occurred_at='2026-10-20T09:00:01Z')
        self.send(old, publication=True)
        review.refresh_from_db()
        self.assertEqual(review.publication_status, 'failed')

    def test_missing_assignment_is_saved_and_visible_in_administrator_queue(self):
        self.people['manager'].profile.active = False
        self.people['manager'].profile.save()
        result = self.send().json()
        self.assertFalse(result['assignment_confirmed'])
        self.assertIsNone(ReviewCase.objects.get().owner_id)
        self.client.force_login(self.people['admin'])
        self.assertContains(self.client.get(reverse('google_review_assignments') + '?demo=1'), 'Google review')
        self.client.force_login(self.people['manager'])
        self.assertEqual(self.client.get(reverse('google_review_assignments') + '?demo=1').status_code, 403)

    def test_unknown_location_is_quarantined_and_can_be_retried_after_mapping(self):
        GoogleReviewLocation.objects.all().delete()
        first = self.send()
        self.assertEqual(first.status_code, 422)
        self.assertFalse(first.json()['committed'])
        self.assertEqual(GoogleReviewEvent.objects.get().status, 'quarantined')
        self.assertFalse(GoogleReview.objects.exists())
        GoogleReviewLocation.objects.create(environment='sandbox', location_name=LOCATION, branch=self.branch)
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(GoogleReviewEvent.objects.count(), 1)
        self.assertEqual(GoogleReview.objects.count(), 1)

    def test_store_code_positive_example_commits_without_a_location_mapping(self):
        GoogleReviewLocation.objects.all().delete()
        self.branch.store_code = 'HS-001'
        self.branch.save()
        payload = json.loads((Path(__file__).resolve().parent.parent /
            'docs/examples/google-review-store-code-positive.json').read_text())
        url = reverse('google_review_results')
        response = self.api.post(url, data=json.dumps(payload), content_type='application/json',
            HTTP_X_INTEGRATION_KEY=TOKEN, HTTP_IDEMPOTENCY_KEY=payload['event_id'])
        self.assertEqual(response.status_code, 200, response.content)
        review = GoogleReview.objects.get()
        self.assertEqual(review.branch, self.branch)
        self.assertEqual(review.profile_key, 'koppam')
        self.assertEqual(review.publication_status, 'published')
        self.assertEqual(review.published_text, payload['publication']['reply_text'])
        self.assertEqual(review.events.get().payload, payload)
        self.assertIsNone(response.json()['case_id'])
        self.assertEqual(response.json()['issues_saved_count'], 0)
        self.assertFalse(response.json()['assignment_confirmed'])
        self.assertFalse(ReviewCase.objects.exists())
        self.assertFalse(ReviewNotification.objects.exists())
        retry = self.api.post(url, data=json.dumps(payload), content_type='application/json',
            HTTP_X_INTEGRATION_KEY=TOKEN)
        self.assertEqual(retry.json(), response.json())
        self.assertEqual(GoogleReviewEvent.objects.count(), 1)
        self.assertEqual(GoogleReview.objects.count(), 1)

    def test_store_code_routes_complaint_to_branch_manager_without_location_mapping(self):
        GoogleReviewLocation.objects.all().delete()
        self.branch.store_code = 'HS-001'
        self.branch.save()
        self.payload['source'].update(store_code=' HS-001 ', profile_key='not-a-branch-selector')
        response = self.send()
        self.assertEqual(response.status_code, 200)
        review = GoogleReview.objects.get()
        self.assertEqual(review.branch, self.branch)
        self.assertEqual(review.case.branch, self.branch)
        self.assertEqual(review.case.owner, self.people['manager'])
        self.assertTrue(response.json()['assignment_confirmed'])
        self.client.force_login(self.people['other'])
        self.assertEqual(self.client.get(reverse('google_review_detail', args=[review.pk]) + '?demo=1').status_code, 404)

    def test_unknown_store_code_never_falls_back_to_valid_location_and_can_be_replayed(self):
        self.payload['source']['store_code'] = 'HS-001'
        response = self.send()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['status'], 'unmapped_store_code')
        self.assertFalse(response.json()['committed'])
        self.assertFalse(GoogleReview.objects.exists())
        self.assertEqual(GoogleReviewEvent.objects.get().status, 'quarantined')
        self.client.force_login(self.people['admin'])
        self.assertContains(self.client.get(reverse('google_review_assignments') + '?demo=1'), 'HS-001')
        self.branch.store_code = 'HS-001'
        self.branch.save()
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(GoogleReviewEvent.objects.count(), 1)

    def test_store_code_must_match_case_and_an_active_branch(self):
        self.branch.store_code = 'HS-001'
        self.branch.save()
        self.payload['source']['store_code'] = 'hs-001'
        self.assertEqual(self.send().status_code, 422)
        self.payload['event_id'] = 'inactive-store'
        self.payload['source']['store_code'] = 'HS-001'
        self.branch.active = False
        self.branch.save()
        self.assertEqual(self.send().status_code, 422)
        self.assertFalse(GoogleReview.objects.exists())

    def test_store_code_and_location_conflicts_do_not_save_partial_data(self):
        self.other_branch.store_code = 'HS-001'
        self.other_branch.save()
        self.payload['source']['store_code'] = 'HS-001'
        self.assertEqual(self.send().status_code, 409)
        self.assertFalse(GoogleReviewEvent.objects.exists())
        self.assertFalse(GoogleReview.objects.exists())
        self.assertFalse(ReviewCase.objects.exists())

    def test_inactive_location_cannot_be_bypassed_by_store_code(self):
        self.branch.store_code = 'HS-001'
        self.branch.save()
        GoogleReviewLocation.objects.filter(environment='sandbox').update(active=False)
        self.payload['source']['store_code'] = 'HS-001'
        response = self.send()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['status'], 'unmapped_location')
        self.assertFalse(GoogleReview.objects.exists())

    def test_store_code_changes_cannot_move_an_existing_review_or_case(self):
        GoogleReviewLocation.objects.all().delete()
        self.branch.store_code = 'HS-001'
        self.branch.save()
        self.payload['source']['store_code'] = 'HS-001'
        self.assertEqual(self.send().status_code, 200)
        self.branch.store_code = None
        self.branch.save()
        self.other_branch.store_code = 'HS-001'
        self.other_branch.save()
        self.payload['event_id'] = 'reassigned-store-code'
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(GoogleReview.objects.get().branch, self.branch)
        self.assertEqual(ReviewCase.objects.get().branch, self.branch)
        self.assertEqual(GoogleReviewEvent.objects.count(), 1)

    def test_supplied_store_codes_are_validated_before_mapping(self):
        for value in [None, 123, [], {}, '', '   ', 'x' * 121, 'HS-\x001', 'HS-\x7f1']:
            with self.subTest(value=value):
                self.payload['source']['store_code'] = value
                self.assertEqual(self.send().status_code, 400)
        self.assertFalse(GoogleReviewEvent.objects.exists())

    def test_sandbox_is_separate_from_production_analytics_and_notifications(self):
        self.send()
        production = self.positive()
        production.update(environment='production', synthetic=False)
        self.assertEqual(self.send(production, token=PRODUCTION_TOKEN).status_code, 200)
        self.assertEqual(GoogleReview.objects.count(), 2)
        self.client.force_login(self.people['manager'])
        self.assertEqual(self.client.get(reverse('review_notifications')).json()['unread_count'], 0)
        self.assertEqual(self.client.get(reverse('review_notifications') + '?demo=1').json()['unread_count'], 1)
        self.assertEqual(self.client.get(reverse('google_review_analytics')).context['total'], 1)
        self.assertEqual(self.client.get(reverse('google_review_analytics')).context['positive'], 1)
        self.assertEqual(self.client.get(reverse('google_review_analytics') + '?demo=1').context['positive'], 0)
        production['synthetic'] = True
        production['event_id'] = 'forbidden-production-demo'
        self.assertEqual(self.send(production, token=PRODUCTION_TOKEN).status_code, 400)

    def test_positive_sandbox_review_has_a_discoverable_test_view_without_a_ticket(self):
        data = self.positive()
        data['event_id'] = 'example-v3-positive'
        response = self.api.post(reverse('google_review_results'), data=json.dumps(data),
            content_type='application/json', HTTP_X_INTEGRATION_KEY=TOKEN)
        self.assertEqual(response.status_code, 200)
        record = GoogleReview.objects.get(pk=response.json()['review_record_id'])
        self.assertIsNone(record.case_id)
        self.client.force_login(self.people['admin'])
        production = self.client.get(reverse('google_review_inbox'))
        self.assertContains(production, 'Sandbox reviews')
        self.assertContains(production, reverse('google_review_inbox') + '?demo=1')
        self.assertNotContains(production, data['source']['review_snapshot']['text'])
        sandbox = self.client.get(reverse('google_review_inbox') + '?demo=1')
        self.assertContains(sandbox, data['source']['review_snapshot']['text'])
        self.assertContains(sandbox, 'No investigation needed')
        self.assertContains(sandbox, reverse('google_review_detail', args=[record.pk]) + '?demo=1')
        detail = self.client.get(reverse('google_review_detail', args=[record.pk]) + '?demo=1')
        self.assertContains(detail, data['source']['review_snapshot']['text'])
        self.assertEqual(self.client.get(reverse('google_review_analytics')).context['total'], 0)
        self.assertEqual(self.client.get(reverse('google_review_analytics') + '?demo=1').context['positive'], 1)

    def test_receipt_failure_rolls_back_review_case_issues_and_event(self):
        with patch('operations.google_review_ingestion.receipt', side_effect=IntegrityError('Synthetic persistence failure')):
            with self.assertRaises(IntegrityError):
                self.send()
        for model in [GoogleReview, GoogleReviewEvent, GoogleReplyPublication, ReviewCase, ReviewIssue, ReviewNotification]:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), 0)

    def test_read_only_review_diagnostics_matches_receipt_and_location(self):
        receipt = self.send(self.positive()).json()
        counts = {model: model.objects.count() for model in [GoogleReview, GoogleReviewEvent, GoogleReviewLocation, ReviewCase]}
        output = io.StringIO()
        call_command('inspect_google_review', review_id=receipt['review_record_id'],
            event_id=self.payload['event_id'], expected_branch=self.branch.code, stdout=output)
        report = json.loads(output.getvalue())
        self.assertTrue(report['database']['dashboard_and_ingestion_same_database'])
        self.assertEqual(report['review']['id'], receipt['review_record_id'])
        self.assertTrue(report['event']['receipt_matches_persisted_review'])
        self.assertTrue(report['event']['requested_review_matches_event'])
        self.assertEqual(report['event']['receipt'], receipt)
        self.assertEqual(report['mapping']['branch_name'], self.branch.name)
        self.assertTrue(report['mapping']['expected_branch_matches'])
        self.assertTrue(report['mapping']['review_branch_matches'])
        self.assertTrue(report['scope']['sandbox_excluded_from_production_views'])
        self.assertEqual(report['all_reviews_url'], '/reviews/google/?demo=1')
        self.assertIsNone(report['review']['case_id'])
        for model, count in counts.items():
            self.assertEqual(model.objects.count(), count)

    def test_review_diagnostics_reports_missing_record_without_inventing_a_mapping(self):
        GoogleReviewLocation.objects.filter(environment='sandbox').delete()
        output = io.StringIO()
        call_command('inspect_google_review', review_id='56835f2d-dba5-46d4-b928-60716be0e9b5',
            event_id='example-v3-positive',
            location_name='accounts/101013083477743854199/locations/12925644274742814696',
            expected_branch='Koppam', stdout=output)
        report = json.loads(output.getvalue())
        self.assertIsNone(report['review'])
        self.assertIsNone(report['event'])
        self.assertIsNone(report['mapping'])
        self.assertFalse(GoogleReview.objects.exists())

    def test_branch_and_role_scope_protect_reviews_analytics_and_actions(self):
        self.send(self.positive())
        review = GoogleReview.objects.get()
        self.client.force_login(self.people['other'])
        self.assertEqual(self.client.get(reverse('google_review_detail', args=[review.pk]) + '?demo=1').status_code, 404)
        self.assertEqual(self.client.get(reverse('google_review_analytics') + '?demo=1').context['total'], 0)
        self.assertEqual(self.client.post(reverse('google_review_action', args=[review.pk]) + '?demo=1', {}).status_code, 404)
        self.client.force_login(self.people['employee'])
        self.assertEqual(self.client.get(reverse('google_review_inbox')).status_code, 403)
        self.client.force_login(self.people['gm'])
        self.assertEqual(self.client.get(reverse('google_review_inbox') + '?demo=1').context['page_obj'].paginator.count, 1)

    def test_authentication_validation_https_and_bounded_payload(self):
        self.assertEqual(self.send(token='wrong').status_code, 401)
        self.assertEqual(self.send(token=PRODUCTION_TOKEN).status_code, 401)
        with override_settings(REVIEW_API_TOKENS={}):
            self.assertEqual(self.send().status_code, 503)
        with override_settings(REVIEW_API_REQUIRE_HTTPS=True):
            self.assertEqual(self.send().status_code, 403)
        self.assertEqual(self.api.post(reverse('google_review_ingest'), data='x' * (512 * 1024 + 1), content_type='application/json').status_code, 413)
        bad = deepcopy(self.payload)
        bad['source']['account_name'] = 'accounts/foreign'
        self.assertEqual(self.send(bad).status_code, 400)
        self.assertFalse(GoogleReview.objects.exists())

    def test_transaction_failure_cannot_return_receipt_or_leave_partial_records(self):
        with patch('operations.google_review_ingestion.sync_case', side_effect=RuntimeError('Simulated persistence failure')):
            with self.assertRaises(RuntimeError):
                self.send()
        self.assertFalse(GoogleReview.objects.exists())
        self.assertFalse(GoogleReviewEvent.objects.exists())
        self.assertFalse(ReviewCase.objects.exists())

    def test_application_cannot_approve_google_replies(self):
        self.send()
        from .review_workflow import update_reply
        from django.core.exceptions import ValidationError
        case = ReviewCase.objects.get()
        with self.assertRaises(ValidationError):
            update_reply(self.people['manager'], case.pk, case.version, 'approve_reply', 'Proposed reply', synthetic=True)

    def test_malformed_publication_and_boolean_are_rejected_without_partial_records(self):
        for key, value in [('publication', None), ('analysis', {'requires_human_review': True, 'case_followup_requires_human': 'yes'})]:
            with self.subTest(key=key):
                data = deepcopy(self.payload)
                data[key] = value
                self.assertEqual(self.send(data).status_code, 400)
                self.assertFalse(GoogleReviewEvent.objects.exists())

    def test_stable_issue_id_preserves_actions_when_analysis_rewords_issue(self):
        self.payload['analysis']['issues'][0]['issue_id'] = 'health-1'
        self.send()
        issue = ReviewIssue.objects.get()
        issue.action_summary = 'Existing staff arrangement.'
        issue.save()
        later = deepcopy(self.payload)
        later['event_id'] = 'reworded-analysis'
        later['processing'].update(analysis_run_id='run-2', analyzed_at='2026-10-20T09:00:20Z')
        later['analysis']['issues'][0]['reported_issue'] = 'Reworded source health concern.'
        self.assertEqual(self.send(later).status_code, 200)
        issue.refresh_from_db()
        self.assertEqual(ReviewIssue.objects.count(), 1)
        self.assertEqual(issue.action_summary, 'Existing staff arrangement.')
        self.assertEqual(issue.reported_issue, 'Reworded source health concern.')

    def test_old_google_version_publication_cannot_replace_current_version(self):
        self.send()
        self.send(json.loads((Path(__file__).resolve().parent.parent / 'docs/examples/google-review-edited.json').read_text()))
        old = deepcopy(self.payload)
        old.update(schema_version='hairship.review-publication.v1', event_id='old-version-later-delivery')
        old['publication'].update(status='failed', published=False, published_at=None, error_code='OLD_VERSION_FAILED', occurred_at='2026-10-20T10:00:00Z')
        self.assertEqual(self.send(old, publication=True).status_code, 200)
        self.assertEqual(GoogleReview.objects.get().publication_status, 'published')

    def test_edited_appreciation_reopens_current_version_and_keeps_close_history(self):
        data = self.positive()
        self.send(data)
        review = GoogleReview.objects.get()
        self.client.force_login(self.people['manager'])
        self.client.post(reverse('google_review_action', args=[review.pk]) + '?demo=1',
            {'action': 'close', 'version': review.google_updated_at.isoformat()})
        data['event_id'] = 'appreciation-edit'
        data['source']['review_update_time'] = '2026-10-20T09:01:00Z'
        data['processing']['analyzed_at'] = '2026-10-20T09:01:10Z'
        data['publication']['review_update_time'] = data['source']['review_update_time']
        self.assertEqual(self.send(data).status_code, 200)
        review.refresh_from_db()
        self.assertFalse(review.simple_closed)
        self.assertEqual(review.actions.count(), 1)
        self.assertFalse(ReviewCase.objects.exists())

    def test_sandbox_seed_is_guarded_repeatable_and_preserves_staff_notes(self):
        self.branch.code = 'DEMO-RV-A'
        self.branch.save()
        with patch('operations.management.commands.seed_google_review_demo.settings') as demo:
            demo.DEBUG, demo.APP_ENV = False, 'production'
            with self.assertRaises(CommandError):
                call_command('seed_google_review_demo', stdout=io.StringIO())
            demo.DEBUG, demo.APP_ENV = True, 'development'
            demo.DATABASES = {'default': {'NAME': 'isolated_review_preview'}}
            demo.BASE_DIR = Path(__file__).resolve().parent.parent
            with patch.dict(os.environ, {'SALONOPS_CODESPACES': 'false'}):
                call_command('seed_google_review_demo', stdout=io.StringIO())
                issue = ReviewIssue.objects.first()
                issue.action_summary = 'Keep the staff note after restarting the demo.'
                issue.save()
                counts = (GoogleReview.objects.count(), ReviewIssue.objects.count(), GoogleReviewEvent.objects.count())
                call_command('seed_google_review_demo', stdout=io.StringIO())
                self.assertEqual(counts, (GoogleReview.objects.count(), ReviewIssue.objects.count(), GoogleReviewEvent.objects.count()))
                issue.refresh_from_db()
                self.assertEqual(issue.action_summary, 'Keep the staff note after restarting the demo.')
                self.assertFalse(GoogleReview.objects.exclude(environment='sandbox').exists())

    def test_api_secret_is_private_never_printed_and_not_overwritten_or_written_to_git(self):
        with TemporaryDirectory() as directory:
            output = io.StringIO()
            path = Path(directory) / 'token.json'
            call_command('create_review_api_secret', output=str(path), stdout=output)
            token = json.loads(path.read_text())['headers']['X-Integration-Key']
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(token, output.getvalue())
            with self.assertRaises(CommandError):
                call_command('create_review_api_secret', output=str(path), stdout=output)
            (Path(directory) / '.git').mkdir()
            with self.assertRaises(CommandError):
                call_command('create_review_api_secret', output=str(Path(directory) / 'other.json'), stdout=output)

    def test_unresolved_outcome_is_not_counted_as_resolved(self):
        self.send()
        case = ReviewCase.objects.get()
        self.client.force_login(self.people['manager'])
        response = self.client.post(reverse('review_action', args=[case.pk]) + '?demo=1', {
            'action': 'followup', 'issue_id': str(case.issues.get().pk), 'version': case.version,
            'customer_followup': 'CONTACTED', 'staff_followup': 'CLOSED_UNRESOLVED',
            'action_summary': 'Investigated and arranged further external assistance; still unresolved.'})
        self.assertEqual(response.status_code, 302)
        case.refresh_from_db()
        self.client.post(reverse('review_action', args=[case.pk]) + '?demo=1', {'action': 'close_simple', 'version': case.version})
        case.refresh_from_db()
        self.assertEqual(case.closure_outcome, 'UNRESOLVED')
        review = GoogleReview.objects.get()
        self.assertEqual(review.handling_status, 'Unresolved')
        report = self.client.get(reverse('google_review_analytics') + '?demo=1')
        self.assertEqual(report.context['unresolved'], 1)
        self.assertIsNone(report.context['resolution_hours'])


@override_settings(**CONFIG)
class GoogleReviewConcurrencyTests(TransactionTestCase):
    def test_concurrent_identical_first_deliveries_create_one_record_and_receipt(self):
        branch = Branch.objects.create(code='CONCURRENT', name='Concurrent test branch')
        GoogleReviewLocation.objects.create(environment='sandbox', location_name=LOCATION, branch=branch)
        def deliver(_):
            close_old_connections()
            try:
                return ingest_review(deepcopy(EXAMPLE))
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(deliver, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(GoogleReview.objects.count(), 1)
        self.assertEqual(GoogleReviewEvent.objects.count(), 1)
        self.assertEqual(ReviewIssue.objects.count(), 1)
