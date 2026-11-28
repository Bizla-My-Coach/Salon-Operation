"""Simple review follow-up: negative reviews need a call and a note, positive ones close after 24 hours."""
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
        data = {'action': 'update', 'version': self.review.edit_token, 'call_outcome': 'SPOKE',
            'staff_spoken': 'on', 'justification': 'Spoke with the reviewer and checked with staff.'}
        data.update(extra)
        return self.client.post(reverse('google_review_action', args=[self.review.pk]), data)

    def add(self, event_id, rating=5, text='A lovely appointment.', name='Asha Menon'):
        data = base.GoogleReviewTests.positive(self)
        data['event_id'] = event_id
        source = data['source']
        source['review_id'] = event_id.upper()
        source['review_name'] = source['review_name'].rsplit('/', 1)[0] + '/' + source['review_id']
        source['review_snapshot'].update(star_rating=rating, text=text, reviewer_display_name=name)
        self.assertEqual(self.send(data).status_code, 200)
        return GoogleReview.objects.get(google_review_id=source['review_id'])

    def age(self, review, hours):
        from datetime import timedelta
        from django.utils import timezone
        then = timezone.now() - timedelta(hours=hours)
        GoogleReview.objects.filter(pk=review.pk).update(created_at=then, google_updated_at=then, google_created_at=then)

    def test_detail_has_one_simple_followup_form_for_every_role(self):
        for role in ('manager', 'gm', 'admin'):
            with self.subTest(role=role):
                self.client.force_login(self.people[role])
                response = self.client.get(reverse('google_review_detail', args=[self.review.pk]))
                self.assertContains(response, '<select', count=0)
                self.assertContains(response, '<textarea', count=1)
                self.assertEqual(list(response.context['update_form'].fields),
                    ['version', 'call_outcome', 'staff_spoken', 'justification'])
                for label in ['Find the customer', 'Spoke to customer', 'Not reachable',
                              'I spoke to the staff who served them', 'Complete follow-up', 'Save for later']:
                    self.assertContains(response, label)
                for label in ['Close now', 'Customer follow-up status', 'Staff follow-up status', 'Justification']:
                    self.assertNotContains(response, label)

    def test_negative_review_cannot_close_without_call_and_note(self):
        self.assertTrue(self.review.requires_followup)
        for values in [{'call_outcome': ''}, {'justification': ''}, {'justification': '   '}]:
            with self.subTest(values=values):
                self.assertEqual(self.update(intent='close', **values).status_code, 400)
                self.review.refresh_from_db()
                self.assertFalse(self.review.is_closed)
        self.assertFalse(GoogleReviewAction.objects.exists())
        self.assertEqual(self.update(intent='close', call_outcome='NOT_REACHABLE', staff_spoken='').status_code, 302)
        self.review.refresh_from_db()
        self.assertTrue(self.review.is_closed)
        self.assertEqual(self.review.call_outcome, 'NOT_REACHABLE')
        self.assertFalse(self.review.staff_spoken)
        self.assertEqual(self.review.case.closure_outcome, 'UNRESOLVED')
        self.assertEqual(self.review.closed_by, self.people['manager'])

    def test_save_for_later_keeps_review_open_and_old_work(self):
        issue = self.review.case.issues.get()
        issue.action_summary = 'Earlier individual issue work.'
        issue.save()
        public_reply = self.review.published_text
        self.assertEqual(self.update(call_outcome='', justification='Will call after 5pm.').status_code, 302)
        self.review.refresh_from_db()
        self.assertFalse(self.review.is_closed)
        self.assertEqual(self.review.justification, 'Will call after 5pm.')
        self.assertEqual(self.review.case.status, 'IN_PROGRESS')
        self.assertEqual(self.review.followup_updated_by, self.people['manager'])
        self.assertEqual(self.review.published_text, public_reply)
        issue.refresh_from_db()
        self.assertEqual(issue.action_summary, 'Earlier individual issue work.')
        self.assertEqual(GoogleReviewAction.objects.count(), 1)

    def test_complete_then_reopen(self):
        self.assertEqual(self.update(intent='close').status_code, 302)
        self.review.refresh_from_db()
        self.assertTrue(self.review.is_closed)
        self.assertEqual(self.review.case.closure_outcome, 'RESOLVED')
        self.assertEqual(self.review.customer_followup, 'CONTACTED')
        inbox = reverse('google_review_inbox')
        self.assertEqual(self.client.get(inbox + '?status=closed').context['page_obj'].paginator.count, 1)
        self.assertEqual(self.client.get(inbox).context['page_obj'].paginator.count, 0)
        self.assertContains(self.client.get(reverse('google_review_detail', args=[self.review.pk])), 'Reopen review')
        self.assertEqual(self.update(intent='reopen').status_code, 302)
        self.review.refresh_from_db()
        self.assertFalse(self.review.is_closed)
        self.assertEqual(self.client.get(inbox).context['page_obj'].paginator.count, 1)

    def test_star_rating_and_complaints_decide_negative(self):
        for rating in range(1, 6):
            review = self.add(f'rating-{rating}', rating=rating)
            self.assertEqual(review.requires_followup, rating <= 3)
        flagged = self.add('flagged-four', rating=4)
        GoogleReview.objects.filter(pk=flagged.pk).update(needs_followup=True)
        flagged.refresh_from_db()
        self.assertTrue(flagged.requires_followup)
        tabs = {t['key']: t['count'] for t in self.client.get(reverse('google_review_inbox')).context['tabs']}
        self.assertEqual(tabs, {'followup': 5, 'positive': 2, 'closed': 0, 'all': 7})

    def test_positive_review_closes_itself_after_24_hours_and_can_close_early(self):
        from django.core.management import call_command
        fresh, old = self.add('fresh-five'), self.add('old-five', rating=4)
        negative = self.add('old-two', rating=2)
        self.age(old, 25)
        self.age(negative, 72)
        page = self.client.get(reverse('google_review_detail', args=[fresh.pk]))
        self.assertContains(page, 'closes automatically in')
        self.assertContains(page, 'Close now')
        self.assertNotContains(page, 'Find the customer')
        call_command('close_positive_reviews', stdout=__import__('io').StringIO())
        old.refresh_from_db(); fresh.refresh_from_db(); negative.refresh_from_db()
        self.assertTrue(old.is_closed and old.auto_closed)
        self.assertIsNone(old.closed_by)
        self.assertEqual(old.closed_at, old.auto_close_at)
        self.assertFalse(fresh.is_closed)
        self.assertFalse(negative.is_closed)
        self.assertTrue(negative.followup_overdue)
        self.assertEqual(old.actions.get().action, 'Closed automatically')
        self.review = fresh
        self.assertEqual(self.client.post(reverse('google_review_action', args=[fresh.pk]), {
            'action': 'update', 'intent': 'close', 'version': fresh.edit_token}).status_code, 302)
        fresh.refresh_from_db()
        self.assertTrue(fresh.is_closed)
        self.assertFalse(fresh.auto_closed)

    def test_pages_close_due_positive_reviews_without_the_command(self):
        old = self.add('page-close', rating=5)
        self.age(old, 30)
        response = self.client.get(reverse('google_review_inbox') + '?status=closed')
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertContains(response, 'Closed automatically')

    def test_possible_visits_link_and_call(self):
        from django.utils import timezone
        from .models import Service, Visit, VisitService
        customer = Customer.objects.create(name='Asha Menon', mobile='9876543210')
        other = Customer.objects.create(name='Someone Else', mobile='9000000000')
        visit = Visit.objects.create(branch=self.branch, customer=customer, created_by=self.people['manager'])
        Visit.objects.create(branch=self.branch, customer=other, created_by=self.people['manager'])
        Visit.objects.create(branch=self.other_branch, customer=customer, created_by=self.people['manager'])
        service = Service.objects.create(code='HC1', name='Hair cut') if not Service.objects.exists() else Service.objects.first()
        VisitService.objects.create(visit=visit, service=service, employee=self.people['employee'], assigned_by=self.people['manager'])
        review = self.add('visit-match', rating=2, name='Asha M.')
        GoogleReview.objects.filter(pk=review.pk).update(google_created_at=timezone.now())
        url = reverse('google_review_detail', args=[review.pk])
        page = self.client.get(url)
        self.assertEqual([c['visit'].pk for c in page.context['possible_visits']], [visit.pk])
        self.assertContains(page, 'This is the customer')
        self.assertEqual(len(self.client.get(url, {'find': '9000'}).context['possible_visits']), 1)
        foreign = Visit.objects.get(branch=self.other_branch)
        action = reverse('google_review_action', args=[review.pk])
        self.assertEqual(self.client.post(action, {'action': 'link_visit', 'visit': foreign.pk}).status_code, 404)
        self.assertEqual(self.client.post(action, {'action': 'link_visit', 'visit': visit.pk}).status_code, 302)
        page = self.client.get(url)
        self.assertContains(page, 'href="tel:9876543210"')
        self.assertContains(page, self.people['employee'].username)
        self.assertEqual(self.client.post(action, {'action': 'not_found'}).status_code, 302)
        review.refresh_from_db()
        self.assertIsNone(review.visit_id)
        self.assertTrue(review.customer_not_found)

    def test_bell_and_dashboard_remind_until_negative_followup_is_done(self):
        self.add('reminder-five', rating=5)
        feed = self.client.get(reverse('review_notifications')).json()
        self.assertEqual(feed['reminder_count'], 1)
        self.assertEqual(feed['reminders'][0]['url'], reverse('google_review_detail', args=[self.review.pk]))
        self.assertEqual(feed['badge_count'], feed['unread_count'] + 1)
        self.age(self.review, 30)
        feed = self.client.get(reverse('review_notifications')).json()
        self.assertEqual(feed['overdue_count'], 1)
        self.assertTrue(feed['reminders'][0]['label'].startswith('Overdue by'))
        page = self.client.get(reverse('manager_dashboard'))
        self.assertContains(page, '1 negative Google review waiting for follow-up')
        self.assertContains(page, '1 overdue')
        self.client.force_login(self.people['other'])
        self.assertEqual(self.client.get(reverse('review_notifications')).json()['reminder_count'], 0)
        self.client.force_login(self.people['manager'])
        self.assertEqual(self.update(intent='close').status_code, 302)
        feed = self.client.get(reverse('review_notifications')).json()
        self.assertEqual((feed['reminder_count'], feed['reminders']), (0, []))
        self.assertNotContains(self.client.get(reverse('manager_dashboard')), 'waiting for follow-up')

    def test_invalid_values_save_nothing_and_csrf_is_required(self):
        for values in [{'call_outcome': 'BOGUS'}, {'justification': 'x' * 3001}, {'action': 'close'}, {'intent': 'bogus'}]:
            self.assertEqual(self.update(**values).status_code, 400)
        self.assertFalse(GoogleReviewAction.objects.exists())
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
        self.update(intent='close', justification='Saved original outcome.')
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
        self.assertEqual(response.context['update_form'].initial['call_outcome'], 'SPOKE')
        self.assertContains(response, 'Saved original outcome.')

    def test_failed_audit_rolls_back_status_and_case(self):
        with patch('operations.google_review_workflow.GoogleReviewAction.objects.create', side_effect=RuntimeError('Audit failed')):
            with self.assertRaises(RuntimeError):
                self.update(intent='close')
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
        self.assertEqual(self.client.get(inbox + '?status=all&branch=' + str(self.other_branch.pk)).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.client.get(reverse('google_review_detail', args=[other.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('google_review_action', args=[other.pk]), {'action': 'update'}).status_code, 404)
        for role in ('gm', 'admin'):
            self.client.force_login(self.people[role])
            response = self.client.get(inbox)
            self.assertEqual(self.client.get(inbox, {'status': 'all'}).context['page_obj'].paginator.count, 2)
            self.assertEqual([tab['label'] for tab in response.context['tabs']], ['Needs follow-up', 'Closing automatically', 'Closed', 'All'])
            response = self.client.get(inbox, {'status': 'all', 'branch': self.other_branch.pk, 'rating': '5',
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
        self.assertEqual(response.context['update_form'].initial['call_outcome'], 'SPOKE')
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
