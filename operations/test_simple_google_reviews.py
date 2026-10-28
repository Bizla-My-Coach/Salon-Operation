"""The single review form, state tabs and branch permissions."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import test_google_reviews as base
from .models import Customer, GoogleReview, GoogleReviewAction, ReviewCase, ReviewIssue


@override_settings(**base.CONFIG)
class SimpleGoogleReviewTests(TestCase):
    def setUp(self):
        base.GoogleReviewTests.setUp(self)
        self.payload.update(environment='production', synthetic=False)
        self.send()
        self.review = GoogleReview.objects.get()
        self.client.force_login(self.people['manager'])

    def send(self, payload=None):
        return self.api.post(reverse('google_review_results'), data=json.dumps(payload or self.payload),
            content_type='application/json', HTTP_X_INTEGRATION_KEY=base.PRODUCTION_TOKEN)

    def update(self, **extra):
        self.review.refresh_from_db()
        data = {'action': 'update', 'version': self.review.edit_token, 'customer_followup': 'CONTACTED',
            'staff_followup': 'IN_PROGRESS', 'justification': 'Spoke with the reviewer and checked with staff.'}
        data.update(extra)
        return self.client.post(reverse('google_review_action', args=[self.review.pk]), data)

    def test_detail_has_exactly_two_dropdowns_and_one_justification_for_every_role(self):
        for role in ('manager', 'gm', 'admin'):
            with self.subTest(role=role):
                self.client.force_login(self.people[role])
                response = self.client.get(reverse('google_review_detail', args=[self.review.pk]))
                self.assertContains(response, '<select', count=2)
                self.assertContains(response, '<textarea', count=1)
                self.assertEqual(list(response.context['update_form'].fields),
                    ['version', 'customer_followup', 'staff_followup', 'justification'])
                for label in ['Customer review', 'System reply', 'Customer follow-up status',
                              'Staff follow-up status', 'Justification', 'Save review']:
                    self.assertContains(response, label)
                for label in ['Open follow-up', 'Assign manager', 'Add another reported issue',
                              'Review activity', 'Generated reply', 'Review and analysis history']:
                    self.assertNotContains(response, label)

    def test_one_update_saves_actor_and_time_preserving_old_issue_work_and_public_reply(self):
        issue = self.review.case.issues.get()
        issue.action_summary = 'Earlier individual issue work.'
        issue.save()
        public_reply = self.review.published_text
        self.assertEqual(self.update(staff_followup='WAITING', customer_followup='AWAITING_REPLY').status_code, 302)
        self.review.refresh_from_db()
        self.assertEqual(self.review.customer_followup, 'AWAITING_REPLY')
        self.assertEqual(self.review.staff_followup, 'WAITING')
        self.assertEqual(self.review.case.status, 'WAITING')
        self.assertEqual(self.review.followup_updated_by, self.people['manager'])
        self.assertIsNotNone(self.review.followup_updated_at)
        self.assertEqual(self.review.published_text, public_reply)
        issue.refresh_from_db()
        self.assertEqual(issue.action_summary, 'Earlier individual issue work.')
        self.assertEqual(issue.status, 'NEW')
        self.assertFalse(Customer.objects.exists())
        self.assertEqual(GoogleReviewAction.objects.count(), 1)
        self.assertFalse(self.review.is_closed)

    def test_terminal_staff_statuses_close_and_same_form_can_reopen(self):
        for status, outcome in [('RESOLVED', 'RESOLVED'), ('CLOSED_UNRESOLVED', 'UNRESOLVED'),
                                ('NOT_ACTIONABLE', 'NOT_ACTIONABLE')]:
            with self.subTest(status=status):
                self.assertEqual(self.update(staff_followup=status).status_code, 302)
                self.review.refresh_from_db()
                self.assertTrue(self.review.is_closed)
                self.assertEqual(self.review.case.closure_outcome, outcome)
                self.assertEqual(self.client.get(reverse('google_review_inbox') + '?status=closed').context['page_obj'].paginator.count, 1)
                self.assertEqual(self.client.get(reverse('google_review_inbox') + '?status=open').context['page_obj'].paginator.count, 0)
                self.assertEqual(self.update(staff_followup='IN_PROGRESS', justification='Reviewer asked us to check again.').status_code, 302)
                self.review.refresh_from_db()
                self.assertFalse(self.review.is_closed)
                self.assertIsNone(self.review.closed_at)
                self.assertEqual(self.client.get(reverse('google_review_inbox') + '?status=open').context['page_obj'].paginator.count, 1)

    def test_appreciations_and_rating_only_reviews_use_the_same_form(self):
        for rating in (5, 1):
            data = base.GoogleReviewTests.positive(self)
            data['event_id'] = f'simple-rating-{rating}'
            source = data['source']
            source['review_id'] = f'SIMPLE_RATING_{rating}'
            source['review_name'] = source['review_name'].rsplit('/', 1)[0] + '/' + source['review_id']
            source['review_snapshot'].update(star_rating=rating, text='')
            self.assertEqual(self.send(data).status_code, 200)
            self.review = GoogleReview.objects.get(google_review_id=source['review_id'])
            self.assertEqual(self.update(staff_followup='RESOLVED').status_code, 302)
            self.review.refresh_from_db()
            self.assertTrue(self.review.is_closed)
        self.assertEqual(ReviewIssue.objects.count(), 1)

    def test_invalid_status_or_blank_justification_saves_nothing_and_csrf_is_required(self):
        for values in [{'justification': ''}, {'justification': ' '}, {'staff_followup': 'BOGUS'},
                       {'customer_followup': 'BOGUS'}, {'justification': 'x' * 3001}, {'action': 'close'}]:
            self.assertEqual(self.update(**values).status_code, 400)
        self.assertFalse(GoogleReviewAction.objects.exists())
        self.review.refresh_from_db()
        self.assertIsNone(self.review.followup_updated_at)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.people['manager'])
        self.assertEqual(client.post(reverse('google_review_action', args=[self.review.pk]), {'action': 'update'}).status_code, 403)

    def test_stale_staff_and_google_versions_do_not_overwrite_saved_work(self):
        token = self.review.edit_token
        self.assertEqual(self.update(justification='Latest staff work.').status_code, 302)
        self.assertEqual(self.update(version=token, justification='Stale overwrite.').status_code, 409)
        self.review.refresh_from_db()
        self.assertEqual(self.review.justification, 'Latest staff work.')
        token = self.review.edit_token
        edited = deepcopy(self.payload)
        edited['event_id'] = 'source-edit'
        edited['source']['review_update_time'] = '2026-10-20T10:00:00Z'
        edited['processing']['analyzed_at'] = '2026-10-20T10:00:10Z'
        edited['publication']['review_update_time'] = edited['source']['review_update_time']
        self.assertEqual(self.send(edited).status_code, 200)
        self.assertEqual(self.update(version=token).status_code, 409)
        self.review.refresh_from_db()
        self.assertEqual(self.review.justification, 'Latest staff work.')

    def test_new_issue_reopens_review_without_removing_justification(self):
        self.update(staff_followup='RESOLVED', justification='Saved original outcome.')
        edited = deepcopy(self.payload)
        edited['event_id'] = 'additional-concern'
        edited['source']['review_update_time'] = '2026-10-20T10:00:00Z'
        edited['processing']['analyzed_at'] = '2026-10-20T10:00:10Z'
        edited['publication']['review_update_time'] = edited['source']['review_update_time']
        edited['analysis']['issues'][0]['reported_issue'] = 'A different reported injury concern.'
        self.assertEqual(self.send(edited).status_code, 200)
        self.review.refresh_from_db()
        self.assertFalse(self.review.is_closed)
        response = self.client.get(reverse('google_review_detail', args=[self.review.pk]))
        self.assertEqual(response.context['update_form'].initial['staff_followup'], 'IN_PROGRESS')
        self.assertContains(response, 'Saved original outcome.')

    def test_failed_audit_rolls_back_status_and_case(self):
        with patch('operations.google_review_workflow.GoogleReviewAction.objects.create', side_effect=RuntimeError('Audit failed')):
            with self.assertRaises(RuntimeError):
                self.update(staff_followup='RESOLVED')
        self.review.refresh_from_db()
        self.assertIsNone(self.review.followup_updated_at)
        self.assertFalse(self.review.is_closed)

    def test_manager_scope_and_senior_filters_and_tabs(self):
        data = base.GoogleReviewTests.positive(self)
        data['event_id'] = 'other-branch-rating-five'
        data['source'].update(store_code='OTHER-STORE', location_name='locations/OTHER',
            review_id='OTHER', review_name=data['source']['account_name'] + '/locations/OTHER/reviews/OTHER')
        self.other_branch.store_code = 'OTHER-STORE'
        self.other_branch.save()
        self.assertEqual(self.send(data).status_code, 200)
        other = GoogleReview.objects.get(branch=self.other_branch)
        inbox = reverse('google_review_inbox')
        self.assertEqual(self.client.get(inbox + '?branch=' + str(self.other_branch.pk)).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.client.get(reverse('google_review_detail', args=[other.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('google_review_action', args=[other.pk]), {'action': 'update'}).status_code, 404)
        for role in ('gm', 'admin'):
            self.client.force_login(self.people[role])
            response = self.client.get(inbox)
            self.assertEqual(response.context['page_obj'].paginator.count, 2)
            self.assertEqual([tab['label'] for tab in response.context['tabs']], ['All reviews', 'Open reviews', 'Closed reviews'])
            response = self.client.get(inbox, {'branch': self.other_branch.pk, 'rating': '5',
                'since': '2026-10-20', 'until': '2026-10-20', 'q': 'OTHER'})
            self.assertEqual(response.context['page_obj'].paginator.count, 1)
            for tab in response.context['tabs']:
                self.assertIn('rating=5', tab['query'])
                self.assertIn('since=2026-10-20', tab['query'])
            self.assertEqual(self.client.get(inbox, {'since': 'bad'}).status_code, 200)
            response = self.client.get(inbox, {'since': '2026-10-21', 'until': '2026-10-20'})
            self.assertTrue(response.context['filter_form'].errors)
        self.client.force_login(self.people['employee'])
        self.assertEqual(self.client.get(inbox).status_code, 403)

    def test_notifications_open_simple_review_and_old_notes_prefill_form(self):
        issue = self.review.case.issues.get()
        issue.action_summary, issue.customer_followup = 'Previous manager note.', 'CONTACTED'
        issue.save()
        response = self.client.get(reverse('google_review_detail', args=[self.review.pk]))
        self.assertContains(response, 'Previous manager note.')
        self.assertEqual(response.context['update_form'].initial['customer_followup'], 'CONTACTED')
        alert = self.review.case.notifications.get()
        response = self.client.post(reverse('review_notification_open', args=[alert.pk]))
        self.assertRedirects(response, reverse('google_review_detail', args=[self.review.pk]))
        self.assertEqual(self.client.get('/health/').status_code, 200)

    def test_date_range_is_available_to_managers_and_senior_roles(self):
        for role in ('manager', 'gm', 'admin'):
            self.client.force_login(self.people[role])
            for page in ('google_review_inbox', 'review_inbox'):
                with self.subTest(role=role, page=page):
                    url = reverse(page)
                    response = self.client.get(url, {'since': '2026-10-20', 'until': '2026-10-20'})
                    self.assertContains(response, 'type="date"', count=2)
                    self.assertEqual(response.context['page_obj'].paginator.count, 1)
                    for dates in [
                        {'since': '2026-10-21'}, {'until': '2026-10-19'},
                        {'since': 'invalid'}, {'since': '2026-10-21', 'until': '2026-10-20'},
                    ]:
                        response = self.client.get(url, dates)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.context['page_obj'].paginator.count, 0)
                    response = self.client.get(url)
                    self.assertEqual(response.context['page_obj'].paginator.count, 1)

    def test_date_range_uses_google_posting_date_and_survives_tabs(self):
        from datetime import datetime
        from django.utils import timezone
        # Received today, but posted just before local midnight on the chosen end date.
        posted = timezone.make_aware(datetime(2026, 10, 20, 23, 59, 59))
        GoogleReview.objects.filter(pk=self.review.pk).update(google_created_at=posted)
        for page in ('google_review_inbox', 'review_inbox'):
            response = self.client.get(reverse(page), {'since': '2026-10-20', 'until': '2026-10-20'})
            self.assertEqual(response.context['page_obj'].paginator.count, 1)
            self.assertIn('since=2026-10-20', response.context['page_query'])
            self.assertIn('until=2026-10-20', response.context['page_query'])
            if page == 'google_review_inbox':
                for tab in response.context['tabs']:
                    self.assertIn('since=2026-10-20', tab['query'])
            else:
                self.assertIn('since=2026-10-20', response.context['tab_prefix'])
        response = self.client.get(reverse('google_review_inbox'), {
            'branch': self.other_branch.pk, 'since': '2026-10-20', 'until': '2026-10-20'})
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertEqual(list(response.context['filter_form'].fields), ['since', 'until'])
