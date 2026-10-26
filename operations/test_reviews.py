from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Branch, BranchDuty, ReviewCase, ReviewIssue, ReviewSnapshot, ReviewNotification
from . import review_workflow as workflow


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}},
)
class ReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = Branch.objects.create(code='REV-A', name='Review branch A')
        cls.b = Branch.objects.create(code='REV-B', name='Private other branch')
        for attr, role, branch in [('manager', 'MANAGER', cls.a), ('other', 'MANAGER', cls.b),
                                   ('admin', 'ADMIN', None), ('gm', 'GENERAL_MANAGER', None),
                                   ('employee', 'EMPLOYEE', cls.a)]:
            person = User.objects.create_user('review_' + attr, password='Test-review!24')
            person.profile.role, person.profile.branch = role, branch
            person.profile.save()
            setattr(cls, attr, person)

    def setUp(self):
        self.client.force_login(self.manager)
        self.case = self.record()

    def admin_report(self):
        client = Client()
        client.force_login(self.admin)
        return client.get(reverse('review_reports'))

    def record(self, **extra):
        values = dict(branch=self.a, actor=self.admin, subject='Waiting and communication', customer_label='Fictional reviewer',
                      text='Nice haircut but a wait and poor updates.', rating=5, service_description='Cut and styling.',
                      issues=[{'category': 'WAITING', 'reported_issue': 'Reported long wait.', 'ai_priority': 'HIGH'},
                              {'category': 'STAFF', 'reported_issue': 'Reported missing updates.', 'ai_priority': 'NORMAL'}])
        values.update(extra)
        return workflow.record_review(**values)

    def url(self, name='review_detail', case=None, demo=False):
        args = [case.pk if case else self.case.pk] if name in ['review_detail', 'review_action'] else []
        return reverse(name, args=args) + ('?demo=1' if demo else '')

    def issue_data(self, issue=None, **extra):
        self.case.refresh_from_db()
        issue = issue or self.case.issues.first()
        values = dict(action='issue', issue_id=str(issue.pk), version=self.case.version, status='IN_PROGRESS',
                      assignee=self.manager.pk, confirmed_priority='NORMAL', action_summary='Reviewed reception handover.',
                      outcome='', customer_confirmation='UNKNOWN', evidence_reference='', waiting_reason='', next_review_at='')
        values.update(extra)
        return values

    def update(self, issue=None, **extra):
        return self.client.post(self.url('review_action'), self.issue_data(issue, **extra))

    def complete_all(self, status='RESOLVED'):
        for issue in self.case.issues.all():
            self.assertEqual(self.update(issue, status=status, outcome='Outcome documented.').status_code, 302)
        self.case.refresh_from_db()

    def close_data(self, **extra):
        self.case.refresh_from_db()
        data = dict(action='close', version=self.case.version, summary='Follow-up completed.', service_description='Corrective styling and finish check.')
        data.update(extra)
        return data

    def test_one_review_has_all_issues_and_source_key_retry_preserves_ids(self):
        case = self.record(source_key='trusted-demo-key', synthetic=True)
        issue_ids = list(case.issues.values_list('pk', flat=True))
        repeated = self.record(source_key='trusted-demo-key', synthetic=True)
        self.assertEqual(case.pk, repeated.pk)
        self.assertEqual(list(repeated.issues.values_list('pk', flat=True)), issue_ids)
        self.assertEqual(case.issues.count(), 2)
        with self.assertRaises(ValidationError):
            self.record(source_key='trusted-demo-key', synthetic=True, branch=self.b)

    def test_notification_feed_refreshes_new_assignments_and_deduplicates_intake(self):
        url = reverse('review_notifications')
        self.assertEqual(self.client.get(url).json()['unread_count'], 1)
        new = self.record(subject='New assigned review', source_key='new-alert')
        self.record(subject='Do not duplicate', source_key='new-alert')
        feed = self.client.get(url)
        self.assertEqual(feed.json()['unread_count'], 2)
        self.assertEqual(feed.json()['items'][0]['subject'], new.subject)
        self.assertEqual(new.notifications.count(), 1)
        self.assertIn('no-store', feed['Cache-Control'])
        self.assertContains(self.client.get(reverse('manager_dashboard')), 'Review notifications, 2 unread')

    def test_notification_open_marks_only_recipient_read_without_accepting_review(self):
        alert = self.case.notifications.get()
        url = reverse('review_notification_open', args=[alert.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertIsNone(ReviewNotification.objects.get(pk=alert.pk).read_at)
        self.assertRedirects(self.client.post(url), self.url())
        alert.refresh_from_db()
        self.assertIsNotNone(alert.read_at)
        self.assertIsNone(alert.delivered_at)
        self.case.refresh_from_db()
        self.assertIsNone(self.case.acknowledged_at)
        read_at = alert.read_at
        self.client.post(url)
        alert.refresh_from_db()
        self.assertEqual(alert.read_at, read_at)
        self.assertEqual(self.client.get(reverse('review_notifications')).json()['unread_count'], 0)

    def test_notification_scope_blocks_other_recipient_inactive_and_employee(self):
        private = self.record(branch=self.b, subject='Private notification')
        private_alert = private.notifications.get()
        feed = self.client.get(reverse('review_notifications')).json()
        self.assertNotIn(private.subject, str(feed))
        self.assertEqual(self.client.post(reverse('review_notification_open', args=[private_alert.pk])).status_code, 404)
        for person in [self.employee, self.admin]:
            self.client.force_login(person)
            response = self.client.get(reverse('review_notifications'))
            if person == self.employee:
                self.assertEqual(response.status_code, 403)
            else:
                self.assertEqual(response.json()['items'], [])
                self.assertEqual(self.client.post(reverse('review_notification_open', args=[private_alert.pk])).status_code, 404)
        self.client.force_login(self.manager)
        self.manager.profile.active = False
        self.manager.profile.save()
        self.assertEqual(self.client.get(reverse('review_notifications')).status_code, 403)

    def test_mark_all_notifications_read_is_csrf_protected_and_demo_isolated(self):
        demo = self.record(synthetic=True, subject='Synthetic alert')
        other = self.record(branch=self.b)
        url = reverse('review_notifications_read_all')
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.manager)
        self.assertEqual(protected.post(url).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 405)
        result = self.client.post(url, HTTP_ACCEPT='application/json')
        self.assertEqual(result.json()['unread_count'], 0)
        self.assertIsNone(demo.notifications.get().read_at)
        self.assertIsNone(other.notifications.get().read_at)
        self.assertEqual(self.client.get(reverse('review_notifications') + '?demo=1').status_code, 403)
        demo.owner = self.admin
        demo.save()
        demo.notifications.update(recipient=self.admin)
        self.client.force_login(self.admin)
        demo_feed = self.client.get(reverse('review_notifications') + '?demo=1').json()
        self.assertEqual(demo_feed['unread_count'], 1)
        self.assertIn('?demo=1', demo_feed['items'][0]['open_url'])
        alert = demo.notifications.get()
        self.assertEqual(self.client.post(reverse('review_notification_open', args=[alert.pk])).status_code, 404)
        self.assertRedirects(self.client.post(reverse('review_notification_open', args=[alert.pk]) + '?demo=1'), self.url(case=demo, demo=True))

    def test_notifications_follow_current_assignment_and_effective_branch(self):
        self.record(issues=[], subject='No issue notification')
        empty = Branch.objects.create(code='REV-NO-OWNER', name='Unassigned branch')
        unassigned = self.record(branch=empty)
        self.assertFalse(unassigned.notifications.exists())
        self.assertEqual(self.client.get(reverse('review_notifications')).json()['unread_count'], 1)
        duty = BranchDuty.objects.create(user=self.manager, date=timezone.localdate(), status='WORK', branch=self.b, updated_by=self.admin)
        self.assertEqual(self.client.get(reverse('review_notifications')).json()['unread_count'], 0)
        duty.delete()
        duty = BranchDuty.objects.create(user=self.other, date=timezone.localdate(), status='WORK', branch=self.a, updated_by=self.admin)
        workflow.assign_case(self.admin, self.case.pk, self.case.version, self.other, 'Cover manager assigned.')
        self.assertEqual(self.client.get(reverse('review_notifications')).json()['unread_count'], 0)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('review_notifications')).json()['unread_count'], 1)

    def test_notification_feed_limits_rows_and_prioritizes_unread_without_losing_count(self):
        alert = self.case.notifications.get()
        for number in range(25):
            ReviewNotification.objects.create(case=self.case, recipient=self.manager,
                dedupe_key='feed-test-' + str(number), read_at=timezone.now())
        feed = self.client.get(reverse('review_notifications')).json()
        self.assertEqual(len(feed['items']), 20)
        self.assertEqual(feed['items'][0]['id'], alert.pk)
        self.assertEqual(feed['unread_count'], 1)

    def test_opening_pages_does_not_acknowledge_and_source_is_escaped(self):
        self.case.snapshot.text = '<script>alert("customer input")</script>'
        self.case.snapshot.save()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>alert')
        self.assertContains(response, 'Accept review')
        self.assertNotContains(response, 'Automatic public acknowledgement')
        self.assertNotContains(response, 'Accountability')
        self.assertNotContains(response, 'Optional controlled response')
        self.case.refresh_from_db()
        self.assertIsNone(self.case.acknowledged_at)
        self.assertIn('no-store', response['Cache-Control'])

    def test_branch_lists_details_mutations_and_metrics_are_scoped(self):
        other_case = self.record(branch=self.b, subject='Other branch secret')
        response = self.client.get(reverse('review_inbox') + '?branch=' + str(self.b.pk))
        self.assertContains(response, self.case.subject)
        self.assertNotContains(response, other_case.subject)
        self.assertEqual(response.context['metrics']['open_cases'], 1)
        self.assertEqual(response.context['metrics']['open_issues'], 2)
        self.assertEqual(self.client.get(self.url(case=other_case)).status_code, 404)
        self.assertEqual(self.client.post(self.url('review_action', other_case), {'action': 'acknowledge', 'version': 1}).status_code, 404)
        self.assertEqual(self.client.get(reverse('review_reports')).status_code, 403)

    def test_employees_inactive_users_anonymous_and_csrf_are_denied(self):
        self.client.force_login(self.employee)
        for url in [self.url(), reverse('review_inbox'), reverse('review_reports'), reverse('review_create')]:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.manager)
        self.manager.profile.active = False
        self.manager.profile.save()
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.manager.profile.active = True
        self.manager.profile.save()
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.manager)
        self.assertEqual(protected.post(self.url('review_action'), self.issue_data()).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url()).status_code, 302)

    def test_cover_and_leave_follow_effective_branch(self):
        other_case = self.record(branch=self.b)
        duty = BranchDuty.objects.create(user=self.manager, branch=self.b, status='WORK', date=timezone.localdate(), updated_by=self.admin)
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(self.client.get(self.url(case=other_case)).status_code, 200)
        self.assertFalse(workflow.branch_managers(self.a).filter(pk=self.manager.pk).exists())
        duty.status, duty.branch = 'LEAVE', None
        duty.save()
        self.assertEqual(self.client.get(self.url(case=other_case)).status_code, 404)
        self.assertEqual(self.client.get(reverse('review_inbox')).context['metrics']['open_cases'], 0)

    def test_senior_roles_see_all_and_employee_has_no_review_sidebar(self):
        other_case = self.record(branch=self.b)
        for senior in [self.admin, self.gm]:
            self.client.force_login(senior)
            self.assertEqual(self.client.get(self.url(case=other_case)).status_code, 200)
            response = self.client.get(reverse('review_inbox'))
            if senior == self.admin:
                self.assertContains(response, 'Issue reporting')
            else:
                self.assertNotContains(response, 'Issue reporting')
        self.client.force_login(self.employee)
        self.assertNotContains(self.client.get(reverse('employee_dashboard')), 'Customer experience')

    def test_explicit_acknowledgement_is_recorded_once(self):
        response = self.client.post(self.url('review_action'), {'action': 'acknowledge', 'version': 1})
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        first = self.case.acknowledged_at
        self.assertEqual(self.case.status, 'ACKNOWLEDGED')
        self.client.post(self.url('review_action'), {'action': 'acknowledge', 'version': self.case.version})
        self.case.refresh_from_db()
        self.assertEqual(self.case.acknowledged_at, first)
        self.assertEqual(self.case.activities.filter(action='Review acknowledged').count(), 1)

    def test_one_resolved_issue_does_not_close_parent(self):
        self.assertEqual(self.update(status='RESOLVED', outcome='Reception handover improved.').status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, 'IN_PROGRESS')
        self.assertIsNone(self.case.closed_at)
        issue = self.case.issues.first()
        self.assertEqual(issue.customer_confirmation, 'UNKNOWN')
        self.assertEqual(self.client.post(self.url('review_action'), self.close_data()).status_code, 400)
        self.assertEqual(self.admin_report().context['open_issues'], 1)

    def test_waiting_requires_reason_and_future_followup(self):
        for extra in [{}, {'waiting_reason': 'CUSTOMER'}, {'waiting_reason': 'CUSTOMER', 'next_review_at': '2020-01-01T09:00'}]:
            self.assertEqual(self.update(status='WAITING', **extra).status_code, 400)
        future = (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.assertEqual(self.update(status='WAITING', waiting_reason='CUSTOMER', next_review_at=future).status_code, 302)
        for issue in self.case.issues.exclude(status='WAITING'):
            self.assertEqual(self.update(issue, status='WAITING', waiting_reason='INTERNAL', next_review_at=future).status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, 'WAITING')
        self.assertEqual(self.admin_report().context['open_cases'], 1)

    def test_terminal_outcome_and_action_are_required(self):
        self.assertEqual(self.update(status='RESOLVED').status_code, 400)
        self.assertEqual(self.update(status='NOT_ACTIONABLE', outcome='Not actionable.', action_summary='').status_code, 400)
        self.assertEqual(self.case.issues.filter(status='NEW').count(), 2)

    def test_explicit_closure_requires_notes_and_keeps_history_when_reopened(self):
        self.complete_all()
        self.assertEqual(self.case.status, 'READY')
        for extra in [{'summary': ''}, {'service_description': ''}]:
            self.assertEqual(self.client.post(self.url('review_action'), self.close_data(**extra)).status_code, 400)
        self.assertEqual(self.client.post(self.url('review_action'), self.close_data()).status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.closure_outcome, 'RESOLVED')
        self.assertEqual(self.case.closed_by, self.manager)
        self.assertEqual(self.update(status='IN_PROGRESS').status_code, 400)
        self.assertEqual(self.client.post(self.url('review_action'), {'action': 'reopen', 'version': self.case.version, 'reason': 'Customer requests another follow-up.'}).status_code, 302)
        self.assertEqual(self.update(status='IN_PROGRESS', action_summary='Customer requests another check.').status_code, 302)
        self.assertTrue(self.case.activities.filter(action='Review closed', note__contains='Corrective styling').exists())
        issue = self.case.issues.first()
        self.assertEqual(issue.cycle, 2)
        self.assertIsNone(issue.completed_at)

    def test_partial_unresolved_and_not_actionable_are_not_counted_as_solved(self):
        issues = list(self.case.issues.all())
        self.update(issues[0], status='RESOLVED', outcome='Fixed.')
        self.update(issues[1], status='CLOSED_UNRESOLVED', outcome='Could not establish a remedy; escalated.')
        self.client.post(self.url('review_action'), self.close_data())
        self.case.refresh_from_db()
        self.assertEqual(self.case.closure_outcome, 'PARTIAL')
        result = self.admin_report().context
        self.assertEqual(result['outcomes'][0]['count'], 0)
        self.assertEqual(result['outcomes'][1]['count'], 1)

    def test_zero_issue_ratings_have_no_invented_work_and_need_disposition(self):
        for rating, text in [(1, ''), (5, 'Great service, no waiting.')]:
            case = self.record(rating=rating, text=text, issues=[])
            self.assertEqual(case.issues.count(), 0)
            closed = workflow.close_case(self.manager, case.pk, case.version, 'No specific issue available; triaged.', 'No corrective service requested.')
            self.assertEqual(closed.closure_outcome, 'NOT_ACTIONABLE')

    def test_sensitive_case_allows_manager_closure_by_default_and_preserves_human_work(self):
        self.case = self.record(issues=[{'category': 'HEALTH', 'reported_issue': 'Customer reports discomfort.', 'ai_priority': 'URGENT'}])
        self.assertTrue(self.case.sensitive)
        self.assertFalse(self.case.ai_requires_human_review)
        self.assertTrue(self.case.case_followup_requires_human)
        self.assertFalse(self.case.requires_senior_closure)
        self.complete_all()
        self.assertEqual(self.client.post(self.url('review_action'), self.close_data()).status_code, 302)

    @override_settings(REVIEW_SENSITIVE_CLOSURE_SENIOR_APPROVAL=True)
    def test_senior_closure_gate_applies_only_when_configured(self):
        self.case = self.record(issues=[{'category': 'HEALTH', 'reported_issue': 'Reported discomfort.'}])
        self.assertTrue(self.case.requires_senior_closure)
        self.complete_all()
        self.assertEqual(self.client.post(self.url('review_action'), self.close_data()).status_code, 400)
        self.client.force_login(self.gm)
        self.assertEqual(self.client.post(self.url('review_action'), self.close_data()).status_code, 302)

    def test_stale_version_returns_409_without_losing_action(self):
        old_data = self.issue_data(action_summary='Old browser tab action.')
        self.assertEqual(self.update(action_summary='Newer action.').status_code, 302)
        response = self.client.post(self.url('review_action'), old_data)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'changed in another session', status_code=409)
        self.assertEqual(self.case.issues.first().action_summary, 'Newer action.')

    def test_assignment_must_be_to_current_branch_manager_and_needs_senior(self):
        self.assertEqual(self.case.owner, self.manager)
        data = dict(action='assign', version=self.case.version, owner=self.manager.pk, reason='Assign a manager.')
        self.assertEqual(self.client.post(self.url('review_action'), data).status_code, 400)
        self.client.force_login(self.admin)
        data['owner'] = self.other.pk
        self.assertEqual(self.client.post(self.url('review_action'), data).status_code, 400)
        data['owner'] = ''
        self.assertEqual(self.client.post(self.url('review_action'), data).status_code, 302)
        self.case.refresh_from_db()
        self.assertIsNone(self.case.owner)
        self.assertEqual(self.admin_report().context['unassigned'], 1)

    def test_public_reply_never_changes_work_and_edit_invalidates_approval(self):
        version = self.case.version
        for action in ['submit_reply', 'approve_reply', 'save_reply']:
            response = self.client.post(self.url('review_action'), {'action': action, 'version': version, 'text': 'Thank you. We will follow up.'})
            self.assertEqual(response.status_code, 302)
            self.case.refresh_from_db()
            version = self.case.version
            self.assertEqual(self.case.status, 'NEW')
            self.assertIsNone(self.case.acknowledged_at)
        self.case.reply.refresh_from_db()
        self.assertEqual(self.case.reply.status, 'DRAFT')
        self.assertIsNone(self.case.reply.approved_by)
        self.assertEqual(self.client.post(self.url('review_action'), {'action': 'publish', 'version': version}).status_code, 400)

    def test_optional_senior_followup_policy_and_empty_or_expired_drafts(self):
        self.case.sensitive = True
        self.case.requires_senior_followup = True
        self.case.save()
        data = {'action': 'submit_reply', 'version': self.case.version, 'text': ''}
        self.assertEqual(self.client.post(self.url('review_action'), data).status_code, 400)
        data['text'] = 'We would like to follow up privately.'
        self.assertEqual(self.client.post(self.url('review_action'), data).status_code, 302)
        self.case.refresh_from_db()
        data.update(action='approve_reply', version=self.case.version)
        self.assertEqual(self.client.post(self.url('review_action'), data).status_code, 400)
        self.client.force_login(self.admin)
        self.case.snapshot.expires_at = timezone.now() - timedelta(days=1)
        self.case.snapshot.save()
        self.assertEqual(self.client.post(self.url('review_action'), data).status_code, 400)

    def test_sensitive_auto_ack_uses_only_generic_template_without_approval_wait(self):
        case = self.record(ai_requires_human_review=True,
            issues=[{'category': 'HEALTH', 'reported_issue': 'PRIVATE reported health detail.', 'ai_priority': 'URGENT'}])
        ack = case.acknowledgement
        context = workflow.acknowledgement_context(case)
        self.assertTrue(context['eligible'])
        self.assertEqual(ack.status, 'PREVIEW')
        self.assertEqual(ack.template_key, 'sensitive-generic-v1')
        self.assertNotIn('PRIVATE', ack.text)
        self.assertIn('have referred', ack.text)
        self.assertTrue(case.ai_requires_human_review)
        self.assertTrue(case.case_followup_requires_human)
        self.assertIsNone(case.acknowledged_at)
        self.assertIsNone(ack.acknowledgement_sent_at)
        self.assertIsNone(ack.publication_confirmed_at)
        self.assertEqual(case.notifications.count(), 1)
        self.assertIsNone(case.notifications.first().delivered_at)

    def test_unassigned_ack_is_held_without_referral_claim_until_assignment(self):
        branch = Branch.objects.create(code='REV-EMPTY', name='No manager')
        case = self.record(branch=branch, synthetic=True, source_key='no-owner-ack')
        self.assertEqual(case.acknowledgement.status, 'HELD')
        self.assertEqual(case.acknowledgement.text, '')
        self.assertFalse(workflow.acknowledgement_context(case)['eligible'])
        self.assertEqual(case.notifications.count(), 0)
        BranchDuty.objects.create(user=self.manager, branch=branch, date=timezone.localdate(), status='WORK', updated_by=self.admin)
        assigned = workflow.assign_case(self.admin, case.pk, case.version, self.manager, 'Confirmed branch duty.', synthetic=True)
        self.assertTrue(workflow.acknowledgement_context(assigned)['eligible'])
        self.assertEqual(assigned.notifications.count(), 1)
        self.assertEqual(assigned.notifications.first().status, 'PREVIEW')

    def test_ack_intent_is_idempotent_and_issue_updates_do_not_change_public_text(self):
        original = self.case.acknowledgement
        original_id, original_text = original.intent_id, original.text
        workflow.prepare_acknowledgement(self.case)
        self.assertEqual(self.case.notifications.count(), 1)
        self.update(status='RESOLVED', outcome='Follow-up completed.')
        original.refresh_from_db()
        self.assertEqual(original.intent_id, original_id)
        self.assertEqual(original.text, original_text)
        self.assertIsNone(original.acknowledgement_sent_at)
        self.assertEqual(self.case.notifications.count(), 1)

    def test_assignment_failure_rolls_back_ack_and_notification_intent(self):
        from unittest.mock import patch
        before = ReviewCase.objects.count()
        with patch('operations.review_workflow.assignment_outbox', side_effect=RuntimeError('Outbox unavailable')):
            with self.assertRaises(RuntimeError):
                self.record(source_key='failed-create')
        self.assertEqual(ReviewCase.objects.count(), before)
        self.assertFalse(ReviewCase.objects.filter(source_key='failed-create').exists())

    def test_ack_eligibility_rechecks_source_and_effective_owner(self):
        self.assertTrue(workflow.acknowledgement_context(self.case)['eligible'])
        BranchDuty.objects.create(user=self.manager, date=timezone.localdate(), status='LEAVE', updated_by=self.admin)
        self.assertFalse(workflow.acknowledgement_context(self.case)['eligible'])
        BranchDuty.objects.filter(user=self.manager).delete()
        self.case.snapshot.expires_at = timezone.now() - timedelta(seconds=1)
        self.case.snapshot.save()
        self.assertFalse(workflow.acknowledgement_context(self.case)['eligible'])

    def test_changed_followup_text_cannot_approve_an_older_draft(self):
        case = workflow.update_reply(self.manager, self.case.pk, self.case.version, 'submit_reply', 'Current text')
        with self.assertRaises(ValidationError):
            workflow.update_reply(self.manager, case.pk, case.version, 'approve_reply', 'Edited but unsaved text')

    def test_synthetic_records_are_isolated_from_live_lists_metrics_and_mutations(self):
        fake = self.record(synthetic=True, subject='Demo only ticket')
        self.assertNotContains(self.client.get(reverse('review_inbox')), fake.subject)
        self.assertEqual(self.admin_report().context['open_cases'], 1)
        self.assertEqual(self.client.get(self.url(case=fake)).status_code, 404)
        self.assertEqual(self.client.get(self.url(case=fake, demo=True)).status_code, 403)
        self.assertEqual(self.client.post(self.url('review_action', fake), {'action': 'acknowledge', 'version': fake.version}).status_code, 404)
        self.client.force_login(self.admin)
        sandbox = self.client.get(reverse('review_inbox') + '?demo=1')
        self.assertContains(sandbox, 'Sandbox reviews')
        self.assertContains(sandbox, 'Excluded from production reviews, analytics and live manager alerts.')

    def test_intake_branch_scope_additional_issues_and_safe_invalid_ids(self):
        data = dict(branch=self.b.pk, subject='Record review', customer_label='', rating=1, text='',
                    service_description='Haircut', category='', reported_issue='')
        self.assertEqual(self.client.post(reverse('review_create'), data).status_code, 200)
        self.assertFalse(ReviewCase.objects.filter(subject='Record review').exists())
        data['branch'] = self.a.pk
        self.assertEqual(self.client.post(reverse('review_create'), data).status_code, 302)
        self.assertEqual(ReviewCase.objects.get(subject='Record review').issues.count(), 0)
        response = self.client.post(self.url('review_action'), {'action': 'add_issue', 'version': self.case.version,
            'category': 'HEALTH', 'reported_issue': 'Customer reports discomfort.'})
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.issues.count(), 3)
        self.assertTrue(self.case.sensitive)
        self.assertEqual(self.client.post(self.url('review_action'), {'action': 'issue', 'issue_id': 'bad'}).status_code, 400)

    def test_expiry_hides_and_clears_source_but_preserves_internal_work(self):
        snapshot = self.case.snapshot
        snapshot.expires_at = timezone.now() - timedelta(days=1)
        snapshot.save()
        response = self.client.get(self.url())
        self.assertNotContains(response, snapshot.text)
        self.assertContains(response, 'original review is no longer available')
        call_command('expire_review_snapshots', stdout=StringIO())
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.text, '')
        self.assertIsNone(snapshot.rating)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, 'NEW')
        self.assertEqual(self.case.issues.count(), 2)

    def test_system_intake_and_unassigned_ticket_render_without_nullable_actor_errors(self):
        branch = Branch.objects.create(code='REV-NONE', name='Unassigned example')
        case = self.record(branch=branch, actor=None)
        self.client.force_login(self.admin)
        response = self.client.get(self.url(case=case))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'System')
        self.assertContains(response, 'Customer follow-up')

    def test_simplified_form_has_only_followup_statuses_and_note(self):
        response = self.client.get(self.url())
        form = response.context['issues'][0].form
        self.assertEqual(list(form.fields), ['version', 'customer_followup', 'staff_followup', 'action_summary'])
        self.assertNotContains(response, 'datetime-local')
        self.assertContains(response, 'The submission time is saved automatically.')

    def test_followup_submission_records_server_time_actor_and_contact_separately(self):
        issue = self.case.issues.first()
        before = timezone.now()
        response = self.client.post(self.url('review_action'), dict(action='followup', issue_id=str(issue.pk),
            version=self.case.version, customer_followup='CONTACTED', staff_followup='IN_PROGRESS',
            action_summary='Spoke with customer; staff are checking the finish.',
            followup_submitted_at='1900-01-01T00:00', followup_submitted_by=self.other.pk))
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        self.assertEqual(issue.customer_followup, 'CONTACTED')
        self.assertEqual(issue.customer_confirmation, 'UNKNOWN')
        self.assertGreaterEqual(issue.followup_submitted_at, before)
        self.assertLessEqual(issue.followup_submitted_at, timezone.now())
        self.assertEqual(issue.followup_submitted_by, self.manager)
        self.assertIsNone(issue.completed_at)
        self.assertTrue(self.case.activities.filter(action='Follow-up submitted', note__contains='Contacted').exists())

    def test_simple_closure_keeps_service_notes_and_requires_every_issue_completed(self):
        for index, issue in enumerate(self.case.issues.all()):
            self.case.refresh_from_db()
            response = self.client.post(self.url('review_action'), dict(action='followup', issue_id=str(issue.pk),
                version=self.case.version, customer_followup='AWAITING_REPLY', staff_followup='RESOLVED',
                action_summary='Corrective styling and reception follow-up completed.'))
            self.assertEqual(response.status_code, 302)
            self.case.refresh_from_db()
            if index == 0:
                self.assertEqual(self.client.post(self.url('review_action'), {'action': 'close_simple', 'version': self.case.version}).status_code, 400)
        self.assertEqual(self.client.post(self.url('review_action'), {'action': 'close_simple', 'version': self.case.version}).status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, 'CLOSED')
        self.assertIn('Corrective styling', self.case.service_description)
        self.assertFalse(self.case.issues.exclude(customer_confirmation='UNKNOWN').exists())

    def test_manager_queue_excludes_appreciations_but_includes_rating_only_followup(self):
        positive = self.record(subject='Positive with no issues', rating=5, issues=[])
        low = self.record(subject='Low rating with no text', rating=1, text='', issues=[])
        other = self.record(branch=self.b, subject='Another branch review')
        response = self.client.get(reverse('review_inbox') + '?tab=all')
        self.assertNotContains(response, positive.subject)
        self.assertContains(response, low.subject)
        self.assertContains(response, self.case.subject)
        self.client.force_login(self.gm)
        response = self.client.get(reverse('review_inbox'))
        self.assertEqual(response.context['active_tab'], 'all')
        for case in [positive, low, other, self.case]:
            self.assertContains(response, case.subject)

    def test_general_manager_filters_combine_branch_open_closed_and_highest_effective_priority(self):
        urgent = self.record(subject='Urgent case', issues=[{'category': 'HEALTH', 'reported_issue': 'Customer concern.', 'ai_priority': 'URGENT'}])
        other = self.record(branch=self.b, subject='Different branch urgent', issues=[{'category': 'OTHER', 'reported_issue': 'Concern.', 'ai_priority': 'URGENT'}])
        self.client.force_login(self.gm)
        response = self.client.get(reverse('review_inbox'), {'branch': self.a.pk, 'tab': 'open', 'priority': 'URGENT'})
        self.assertContains(response, urgent.subject)
        self.assertNotContains(response, other.subject)
        self.assertNotContains(response, self.case.subject)
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        # Health concerns remain urgent even if staff select a lower priority.
        urgent.issues.update(confirmed_priority='NORMAL')
        response = self.client.get(reverse('review_inbox'), {'branch': self.a.pk, 'tab': 'all', 'priority': 'URGENT'})
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        urgent.issues.update(status='RESOLVED', action_summary='Follow-up finished.', outcome='Recorded.')
        workflow.close_from_followups(self.gm, urgent.pk, urgent.version)
        response = self.client.get(reverse('review_inbox'), {'branch': self.a.pk, 'tab': 'closed', 'priority': 'URGENT'})
        self.assertContains(response, urgent.subject)
        self.assertNotContains(response, self.case.subject)
