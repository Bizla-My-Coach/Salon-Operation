"""The receiving contract: no worker queue, AWS identity or Google writes required."""
from copy import deepcopy
import json
import importlib
from pathlib import Path

from django.test import LiveServerTestCase, TestCase, TransactionTestCase, override_settings
from django.apps import apps
from django.db import connection
from django.urls import reverse

from . import test_google_reviews as base
from .models import GoogleReview, GoogleReviewEvent, GoogleReplyPublication, ReviewCase, ReviewIssue, ReviewNotification
from .review_workflow import close_from_followups


@override_settings(**base.CONFIG)
class ContractV3Tests(TestCase):
    def setUp(self):
        base.GoogleReviewTests.setUp(self)
        self.payload['processing'].update(worker_version='hairship-gmb-sqs-worker-v3', result_revision=10)

    def send(self, payload=None, **headers):
        data = self.payload if payload is None else payload
        return self.api.post(reverse('google_review_results'), data=json.dumps(data),
            content_type='application/json', HTTP_X_INTEGRATION_KEY=base.TOKEN,
            HTTP_IDEMPOTENCY_KEY=data['event_id'], **headers)

    def outcome(self, payload, status, revision, event_id):
        data = deepcopy(payload)
        data['event_id'] = event_id
        data['processing']['result_revision'] = revision
        data['publication'].update(status=status, published=status == 'published',
            published_at='2026-10-20T09:00:15Z' if status == 'published' else None,
            error_code='GOOGLE_REJECTED' if status == 'failed' else None)
        return data

    def fixture(self, name, has_case, issues, publication):
        data = json.loads((Path(__file__).resolve().parent.parent / 'docs/examples/worker-v2' / name).read_text())
        first = self.send(data)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertNotIn('Location', first)
        receipt = first.json()
        record = GoogleReview.objects.get()
        self.assertEqual(record.events.get().payload, data)
        self.assertEqual(record.processing, data['processing'])
        self.assertEqual(record.analysis, data['analysis'])
        self.assertEqual(record.publication_status, publication)
        self.assertEqual(record.needs_followup, has_case)
        self.assertEqual(bool(record.case_id), has_case)
        self.assertEqual(receipt['issues_saved_count'], issues)
        self.assertEqual(receipt['assignment_confirmed'], has_case)
        if has_case:
            self.assertIsNone(record.case.closed_at)
        else:
            self.assertIsNone(receipt['case_id'])
        self.assertEqual(self.send(data).json(), receipt)
        self.assertEqual(GoogleReviewEvent.objects.count(), 1)
        self.assertEqual(ReviewIssue.objects.count(), issues)
        self.assertEqual(ReviewNotification.objects.count(), int(has_case))

    def test_exact_positive_worker_fixture(self):
        self.fixture('positive.json', False, 0, 'published')

    def test_exact_mixed_worker_fixture(self):
        self.fixture('mixed.json', True, 1, 'published')
        self.assertEqual(GoogleReview.objects.get().star_rating, 5)

    def test_exact_rating_only_worker_fixture(self):
        self.fixture('rating-only.json', True, 0, 'human_review')

    def test_exact_sensitive_worker_fixture(self):
        self.fixture('sensitive.json', True, 1, 'published')
        self.assertEqual(ReviewIssue.objects.get().ai_priority, 'URGENT')
        self.assertTrue(GoogleReview.objects.get().requires_human_review)

    def test_all_v3_publication_states(self):
        for state in ('pending', 'submitted', 'published', 'failed', 'uncertain', 'superseded', 'skipped', 'human_review'):
            with self.subTest(state=state):
                data = self.outcome(self.payload, state, 1, 'state-' + state)
                data['source']['review_id'] = 'STATE_' + state
                data['source']['review_name'] = data['source']['review_name'].rsplit('/', 1)[0] + '/' + data['source']['review_id']
                data['publication']['future_diagnostic'] = {'value': 'retained'}
                response = self.send(data)
                self.assertEqual(response.status_code, 200, response.content)
                record = GoogleReview.objects.get(review_name=data['source']['review_name'])
                self.assertEqual(record.publication_status, state)
                self.assertIsNone(record.case.closed_at)
                if state != 'published':
                    self.assertFalse(record.published_text)
                    self.assertIsNone(record.published_at)
                self.assertEqual(record.events.get().payload, data)

    def test_revision_gaps_and_out_of_order_delivery_preserve_manager_work(self):
        newer = self.outcome(self.payload, 'failed', 900, 'a-revision-900')
        self.assertEqual(self.send(newer).status_code, 200)
        case = ReviewCase.objects.get()
        issue = case.issues.get()
        issue.action_summary, issue.status = 'Existing manager arrangement.', 'IN_PROGRESS'
        issue.save()
        version = case.version
        older = self.outcome(self.payload, 'uncertain', 2, 'z-revision-2')
        stale = self.send(older)
        self.assertEqual(stale.json(), {'event_id': older['event_id'], 'status': 'stale', 'committed': True})
        self.assertEqual(self.send(older).json(), stale.json())
        self.assertEqual(GoogleReview.objects.get().publication_status, 'failed')
        approved = self.outcome(self.payload, 'published', 10000, 'revision-10000')
        self.assertEqual(self.send(approved).status_code, 200)
        self.assertEqual(GoogleReview.objects.get().publication_status, 'published')
        self.assertEqual(GoogleReview.objects.get().result_revision, 10000)
        issue.refresh_from_db()
        case.refresh_from_db()
        self.assertEqual(issue.action_summary, 'Existing manager arrangement.')
        self.assertEqual(issue.status, 'IN_PROGRESS')
        self.assertEqual(case.version, version)
        self.assertEqual(ReviewIssue.objects.count(), 1)
        self.assertEqual(ReviewNotification.objects.count(), 1)

    def test_other_run_uncertain_or_skipped_cannot_undo_confirmed_publication(self):
        self.send()
        for index, state in enumerate(('uncertain', 'skipped', 'pending', 'submitted'), 1):
            data = self.outcome(self.payload, state, 10000 + index, 'other-run-' + state)
            data['processing'].update(analysis_run_id='independent-' + state, analyzed_at=f'2026-10-20T10:0{index}:00Z')
            data['source']['fetched_at'] = f'2026-10-20T10:0{index}:00Z'
            data['publication']['google_reply'] = None
            self.assertEqual(self.send(data).status_code, 200)
            self.assertEqual(GoogleReview.objects.get().publication_status, 'published')
        self.assertEqual(ReviewCase.objects.count(), 1)
        self.assertEqual(ReviewIssue.objects.count(), 1)
        self.assertEqual(ReviewNotification.objects.count(), 1)

    def test_migration_backfills_existing_evidence_and_reuses_existing_alert(self):
        self.send()
        GoogleReplyPublication.objects.update(evidence_rank=0, evidence_at=None, observed_at=None, result_revision=0)
        ReviewNotification.objects.update(dedupe_key='legacy-alert')
        migration = importlib.import_module('operations.migrations.0020_review_contract_v3')
        migration.backfill_evidence(apps, connection.schema_editor())
        result = GoogleReplyPublication.objects.get()
        self.assertEqual(result.evidence_rank, 3)
        self.assertEqual(result.result_revision, 10)
        self.assertIsNotNone(result.evidence_at)
        other = self.outcome(self.payload, 'uncertain', 100, 'after-upgrade')
        other['processing'].update(analysis_run_id='post-upgrade-run', analyzed_at='2026-10-20T10:00:00Z')
        other['analysis']['issues'][0]['reported_issue'] = 'A rewording of the same source concern.'
        self.assertEqual(self.send(other).status_code, 200)
        self.assertEqual(GoogleReview.objects.get().publication_status, 'published')
        self.assertEqual(ReviewNotification.objects.count(), 1)

    def test_newer_google_rejection_from_another_run_is_applied(self):
        self.send()
        data = self.outcome(self.payload, 'failed', 1, 'confirmed-rejection')
        data['processing'].update(analysis_run_id='another-run', analyzed_at='2026-10-20T10:00:00Z')
        data['source']['fetched_at'] = '2026-10-20T10:00:00Z'
        data['publication']['google_reply'] = {'reviewReplyState': 'REJECTED', 'updateTime': '2026-10-20T10:00:00Z'}
        self.assertEqual(self.send(data).status_code, 200)
        review = GoogleReview.objects.get()
        self.assertEqual(review.publication_status, 'failed')
        self.assertIsNone(review.case.closed_at)

    def test_independent_run_rewording_reuses_tasks_and_keeps_closed_outcome(self):
        self.send()
        case = ReviewCase.objects.get()
        issue = case.issues.get()
        issue.status, issue.action_summary = 'RESOLVED', 'Corrective action completed.'
        issue.save()
        close_from_followups(self.people['manager'], case.pk, case.version, synthetic=True)
        data = deepcopy(self.payload)
        data['event_id'] = 'independent-preparation'
        data['processing'].update(analysis_run_id='different-preparation', analyzed_at='2026-10-20T09:02:00Z', result_revision=1)
        data['source']['fetched_at'] = '2026-10-20T09:02:00Z'
        data['source']['review_snapshot']['existing_reply'] = {'comment': 'Current public acknowledgement.'}
        data['analysis']['issues'][0]['reported_issue'] = 'The same scalp concern, reworded during repeated preparation.'
        response = self.send(data)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['issues_saved_count'], 1)
        self.assertEqual(ReviewIssue.objects.count(), 1)
        self.assertEqual(ReviewNotification.objects.count(), 1)
        issue.refresh_from_db()
        case.refresh_from_db()
        self.assertEqual(issue.action_summary, 'Corrective action completed.')
        self.assertEqual(issue.status, 'RESOLVED')
        self.assertEqual(case.closure_outcome, 'RESOLVED')
        self.assertIsNotNone(case.closed_at)

    def test_receipt_counts_incoming_issues_not_historical_or_manual_tasks(self):
        self.send()
        case = ReviewCase.objects.get()
        ReviewIssue.objects.create(case=case, category='OTHER', reported_issue='Staff recorded another concern.')
        data = deepcopy(self.payload)
        data['event_id'] = 'edited-with-one-incoming-issue'
        data['source']['review_update_time'] = '2026-10-20T10:00:00Z'
        data['publication']['review_update_time'] = data['source']['review_update_time']
        data['processing'].update(analysis_run_id='edit-analysis', analyzed_at='2026-10-20T10:00:10Z', result_revision=1)
        data['analysis']['issues'][0].update(category='billing', reported_issue='Customer asks for a payment clarification.')
        result = self.send(data)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['issues_saved_count'], 1)
        self.assertEqual(case.issues.count(), 3)
        self.assertEqual(ReviewCase.objects.count(), 1)

    def test_same_category_reconciliation_reserves_exact_matches(self):
        self.payload['analysis']['issues'] = [
            {'category': 'waiting_time', 'reported_issue': 'Wait at arrival.', 'priority': 'normal'},
            {'category': 'waiting_time', 'reported_issue': 'Wait at checkout.', 'priority': 'normal'}]
        self.send()
        old_ids = set(ReviewIssue.objects.values_list('pk', flat=True))
        data = deepcopy(self.payload)
        data['event_id'] = 'reworded-and-reordered'
        data['processing'].update(analysis_run_id='repeated-run', analyzed_at='2026-10-20T09:02:00Z', result_revision=1)
        data['analysis']['issues'] = [
            {'category': 'waiting_time', 'reported_issue': 'Reworded checkout delay.', 'priority': 'normal'},
            self.payload['analysis']['issues'][0]]
        self.assertEqual(self.send(data).status_code, 200)
        self.assertEqual(set(ReviewIssue.objects.values_list('pk', flat=True)), old_ids)
        self.assertEqual(ReviewIssue.objects.filter(source_active=True).count(), 2)
        self.assertEqual(ReviewNotification.objects.count(), 1)

    def test_issue_reappearing_in_another_run_of_same_version_reuses_its_history(self):
        self.send()
        issue = ReviewIssue.objects.get()
        issue.action_summary = 'Manager follow-up retained.'
        issue.save()
        empty = deepcopy(self.payload)
        empty['event_id'] = 'analysis-with-no-issues'
        empty['processing'].update(analysis_run_id='empty-analysis', analyzed_at='2026-10-20T09:02:00Z')
        empty['analysis']['issues'] = []
        self.assertEqual(self.send(empty).status_code, 200)
        issue.refresh_from_db()
        self.assertFalse(issue.source_active)
        restored = deepcopy(self.payload)
        restored['event_id'] = 'issue-reappears'
        restored['processing'].update(analysis_run_id='restored-analysis', analyzed_at='2026-10-20T09:03:00Z')
        restored['analysis']['issues'][0]['reported_issue'] = 'Reworded scalp issue from the same customer review version.'
        self.assertEqual(self.send(restored).status_code, 200)
        issue.refresh_from_db()
        self.assertTrue(issue.source_active)
        self.assertEqual(issue.action_summary, 'Manager follow-up retained.')
        self.assertEqual(ReviewIssue.objects.count(), 1)
        self.assertEqual(ReviewNotification.objects.count(), 1)

    def test_new_version_does_not_reuse_an_unrelated_historical_issue(self):
        self.send()
        case = ReviewCase.objects.get()
        old = case.issues.get()
        old.status, old.action_summary = 'RESOLVED', 'Previous concern resolved.'
        old.save()
        close_from_followups(self.people['manager'], case.pk, case.version, synthetic=True)
        edited = deepcopy(self.payload)
        edited['event_id'] = 'new-version-initial-analysis'
        edited['source']['review_update_time'] = '2026-10-20T10:00:00Z'
        edited['publication']['review_update_time'] = edited['source']['review_update_time']
        edited['processing'].update(analysis_run_id='new-version-run', analyzed_at='2026-10-20T10:00:10Z')
        edited['analysis']['issues'] = []
        self.assertEqual(self.send(edited).status_code, 200)
        later = deepcopy(edited)
        later['event_id'] = 'new-version-new-concern'
        later['processing'].update(analysis_run_id='new-version-another-run', analyzed_at='2026-10-20T10:01:00Z')
        later['analysis']['issues'] = [{'category': 'injury_or_health', 'reported_issue': 'A different health concern in the edited review.', 'priority': 'urgent'}]
        self.assertEqual(self.send(later).status_code, 200)
        case.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(case.issues.count(), 2)
        self.assertIsNone(case.closed_at)
        self.assertEqual(old.status, 'RESOLVED')
        self.assertEqual(old.action_summary, 'Previous concern resolved.')

    def test_authentication_and_both_canonical_paths_have_no_redirects(self):
        url = reverse('google_review_results')
        for headers in ({}, {'HTTP_X_INTEGRATION_KEY': 'wrong'}, {'HTTP_AUTHORIZATION': 'Bearer ' + base.TOKEN}):
            result = self.api.post(url, data=json.dumps(self.payload), content_type='application/json', **headers)
            self.assertEqual(result.status_code, 401)
            self.assertNotIn('Location', result)
        with override_settings(REVIEW_API_REQUIRE_HTTPS=True):
            first = self.send(secure=True)
            self.assertEqual(first.status_code, 200)
            second = self.api.post(url + '/', data=json.dumps(self.payload), content_type='application/json',
                HTTP_X_INTEGRATION_KEY=base.TOKEN, secure=True)
            self.assertEqual(second.json(), first.json())
            self.assertNotIn('Location', second)
        self.assertEqual(self.api.get(url).status_code, 405)

    def test_invalid_revision_and_idempotency_header_never_commit(self):
        for revision in (True, -1, 1.5, '12', 9223372036854775808):
            data = deepcopy(self.payload)
            data['processing']['result_revision'] = revision
            self.assertEqual(self.send(data).status_code, 400)
        result = self.api.post(reverse('google_review_results'), data=json.dumps(self.payload), content_type='application/json',
            HTTP_X_INTEGRATION_KEY=base.TOKEN, HTTP_IDEMPOTENCY_KEY='different-id')
        self.assertEqual(result.status_code, 409)
        self.assertFalse(GoogleReviewEvent.objects.exists())

    def test_moderation_timeout_stays_submitted_and_never_resolves_case(self):
        data = self.outcome(self.payload, 'submitted', 100, 'moderation-window-ended')
        data['publication'].update(error_code='moderation_not_confirmed_within_retry_window', submitted=True,
            submitted_at='2026-10-20T09:00:15Z', visibility_confirmed=False)
        self.assertEqual(self.send(data).status_code, 200)
        review = GoogleReview.objects.get()
        self.assertEqual(review.publication_status, 'submitted')
        self.assertFalse(review.published_text)
        self.assertIsNone(review.case.closed_at)


@override_settings(**base.CONFIG)
class ContractV3LiveHTTPTests(LiveServerTestCase):
    def setUp(self):
        base.GoogleReviewTests.setUp(self)
        self.payload['processing'].update(worker_version='hairship-gmb-sqs-worker-v3', result_revision=100)

    def test_real_post_has_json_auth_errors_and_committed_receipts_without_redirects(self):
        from urllib.error import HTTPError
        from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

        class RejectRedirects(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        client = build_opener(ProxyHandler({}), RejectRedirects())  # Loopback server only.
        url = self.live_server_url + reverse('google_review_results')
        body = json.dumps(self.payload).encode()
        with client.open(self.live_server_url + '/health/', timeout=10) as response:
            self.assertEqual(response.status, 200)
        for key in ('', 'wrong'):
            with self.assertRaises(HTTPError) as raised:
                client.open(Request(url, data=body, headers={'Content-Type': 'application/json', 'X-Integration-Key': key}), timeout=10)
            self.assertEqual(raised.exception.code, 401)
            self.assertIsNone(raised.exception.headers.get('Location'))
            self.assertEqual(json.load(raised.exception)['error'], 'invalid_authentication')
        request = Request(url, data=body, headers={'Content-Type': 'application/json', 'X-Integration-Key': base.TOKEN,
            'Idempotency-Key': self.payload['event_id']})
        with client.open(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.headers.get('Location'))
            receipt = json.load(response)
        with client.open(request, timeout=10) as response:
            self.assertEqual(json.load(response), receipt)
        self.assertTrue(receipt['committed'])
        self.assertEqual(receipt['review_record_id'], str(GoogleReview.objects.get().pk))
        self.assertEqual(receipt['case_id'], str(ReviewCase.objects.get().pk))
        self.assertEqual(ReviewNotification.objects.count(), 1)


@override_settings(**base.CONFIG)
class ContractV3ConcurrentTests(TransactionTestCase):
    def setUp(self):
        base.GoogleReviewTests.setUp(self)

    def test_concurrent_independent_runs_of_one_google_version_create_one_case_and_alert(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections, connections
        from .google_review_ingestion import ingest_review

        def deliver(index):
            close_old_connections()
            try:
                data = deepcopy(self.payload)
                data['event_id'] = f'independent-event-{index}'
                data['processing'].update(worker_version='hairship-gmb-sqs-worker-v3',
                    analysis_run_id=f'independent-run-{index}', result_revision=100)
                return ingest_review(data)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(deliver, range(2)))
        self.assertTrue(all(status == 200 and result['committed'] for result, status in results))
        self.assertEqual(GoogleReview.objects.count(), 1)
        self.assertEqual(ReviewCase.objects.count(), 1)
        self.assertEqual(ReviewIssue.objects.count(), 1)
        self.assertEqual(ReviewNotification.objects.count(), 1)
