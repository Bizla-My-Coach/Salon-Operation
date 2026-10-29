from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Branch, BranchDuty, TaskTimingSegment, VisitTask
from .tests import CombinedServiceWorkflowTests
from .timing import add_timing_summary


@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class FlowRegressionTests(TestCase):
    setUp = CombinedServiceWorkflowTests.setUp
    create_visit = CombinedServiceWorkflowTests.create_visit
    assignment_payload = CombinedServiceWorkflowTests.assignment_payload

    def test_opening_confirmation_locks_reorder_and_keeps_consultation_accessible(self):
        visit, first, second = self.create_visit()
        hygiene = first.tasks.get(task_type='HYGIENE')
        consultation = first.tasks.get(task_type='CONSULT')
        self.client.force_login(self.employee)
        self.client.post(reverse('task_action', args=[hygiene.pk, 'confirm']))
        self.client.force_login(self.manager)
        page = self.client.get(reverse('edit_visit_services', args=[visit.pk]))
        self.assertTrue(page.context['formset'].forms[0].execution_locked)
        response = self.client.post(reverse('edit_visit_services', args=[visit.pk]),
                                    self.assignment_payload(first, second, first_order=2, second_order=1))
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        self.assertEqual(first.order_number, 1)
        hygiene.refresh_from_db()
        self.assertEqual(hygiene.status, 'COMPLETED')
        self.client.force_login(self.employee)
        self.client.post(reverse('task_action', args=[consultation.pk, 'confirm']))
        consultation.refresh_from_db()
        self.assertEqual(consultation.status, 'COMPLETED')

    def test_confirmed_opening_cannot_be_deleted_or_changed_by_stale_assignment_post(self):
        visit, first, second = self.create_visit()
        first.tasks.filter(task_type='HYGIENE').update(status='COMPLETED')
        payload = self.assignment_payload(first, second)
        payload.update({'services-0-DELETE': 'on', 'services-0-service': str(self.service_b.pk)})
        self.client.force_login(self.manager)
        self.client.post(reverse('edit_visit_services', args=[visit.pk]), payload)
        first.refresh_from_db()
        self.assertEqual(first.service_id, self.service_a.pk)
        self.assertEqual(first.tasks.get(task_type='HYGIENE').status, 'COMPLETED')

    def test_permitted_required_skip_can_be_submitted_and_verified(self):
        visit, first, second = self.create_visit()
        first.tasks.filter(task_type__in=['HYGIENE', 'CONSULT']).update(status='COMPLETED')
        task = first.tasks.get(task_type='SERVICE')
        task.can_skip = task.required = True
        task.save()
        self.client.force_login(self.employee)
        self.client.post(reverse('task_action', args=[task.pk, 'skip']), {'skip_reason_choice': 'Customer requested to skip'})
        self.client.post(reverse('finish_service', args=[first.pk]))
        second.tasks.update(status='COMPLETED')
        self.client.post(reverse('finish_service', args=[second.pk]))
        self.client.force_login(self.manager)
        response = self.client.post(reverse('verify_visit', args=[visit.pk]))
        self.assertRedirects(response, reverse('manager_dashboard'))
        visit.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(visit.status, 'VERIFIED')
        self.assertEqual(task.status, 'SKIPPED')
        self.assertEqual(task.skip_reason, 'Customer requested to skip')

    def test_illegal_skip_or_missing_reason_cannot_submit_or_verify(self):
        for can_skip, reason in [(False, 'Reason'), (True, '')]:
            visit, first, _ = self.create_visit()
            first.tasks.update(status='COMPLETED')
            task = first.tasks.get(task_type='SERVICE')
            task.status = 'SKIPPED'
            task.can_skip = can_skip
            task.skip_reason = reason
            task.skip_reason_required = True
            task.save()
            self.client.force_login(self.employee)
            self.client.post(reverse('finish_service', args=[first.pk]))
            first.refresh_from_db()
            self.assertEqual(first.status, 'ASSIGNED')
            first.status = 'EMPLOYEE_DONE'
            first.save()
            self.client.force_login(self.manager)
            self.client.post(reverse('verify_service', args=[first.pk]), {'manager_notes': ''})
            first.refresh_from_db()
            self.assertEqual(first.status, 'EMPLOYEE_DONE')

    def test_verification_cannot_reopen_terminal_visits_or_approve_unsubmitted_service(self):
        for status in ['CLOSED', 'INVOICED', 'CANCELLED']:
            visit, first, second = self.create_visit()
            for item in [first, second]:
                item.tasks.update(status='COMPLETED')
                item.status = 'VERIFIED'
                item.save()
            visit.status = status
            visit.save()
            self.client.force_login(self.manager)
            self.client.post(reverse('verify_service', args=[first.pk]), {'manager_notes': 'Must not reopen'})
            self.client.post(reverse('verify_visit', args=[visit.pk]))
            visit.refresh_from_db()
            self.assertEqual(visit.status, status)
        _, first, _ = self.create_visit()
        first.tasks.update(status='COMPLETED')
        self.client.post(reverse('verify_service', args=[first.pk]), {'manager_notes': ''})
        first.refresh_from_db()
        self.assertEqual(first.status, 'ASSIGNED')

    def test_incomplete_optional_task_cannot_be_approved(self):
        _, first, _ = self.create_visit()
        first.tasks.update(status='COMPLETED')
        VisitTask.objects.create(visit_service=first, sequence=100, title='Optional',
                                 task_type='SERVICE', phase='DURING', required=False)
        first.status = 'EMPLOYEE_DONE'
        first.save()
        self.client.force_login(self.manager)
        self.client.post(reverse('verify_service', args=[first.pk]), {'manager_notes': ''})
        first.refresh_from_db()
        self.assertEqual(first.status, 'EMPLOYEE_DONE')

    def test_expired_cover_retains_only_own_historically_authorized_assignments(self):
        today = timezone.localdate()
        cover = Branch.objects.create(code='COVER', name='Cover branch')
        visit, first, second = self.create_visit()
        visit.branch = cover
        visit.save()
        assigned = timezone.now() - timedelta(days=1)
        visit.services.update(assigned_at=assigned)
        BranchDuty.objects.create(user=self.employee, date=timezone.localdate(assigned),
                                  status='WORK', branch=cover, updated_by=self.manager)
        first.tasks.filter(task_type__in=['HYGIENE', 'CONSULT']).update(status='COMPLETED')
        self.client.force_login(self.employee)
        dashboard = self.client.get(reverse('employee_dashboard'))
        self.assertEqual({j.pk for j in dashboard.context['jobs']}, {first.pk, second.pk})
        task = first.tasks.get(task_type='SERVICE')
        self.client.post(reverse('task_action', args=[task.pk, 'start']))
        task.refresh_from_db()
        self.assertEqual(task.status, 'IN_PROGRESS')
        self.client.post(reverse('task_action', args=[task.pk, 'complete']))
        self.client.post(reverse('finish_service', args=[first.pk]))
        first.refresh_from_db()
        self.assertEqual(first.status, 'EMPLOYEE_DONE')
        other, other_first, _ = self.create_visit()
        other.branch = cover
        other.save()
        self.assertEqual(self.client.get(reverse('execute_service', args=[other_first.pk])).status_code, 404)
        BranchDuty.objects.create(user=self.employee, date=today, status='LEAVE', updated_by=self.manager)
        self.assertEqual(list(self.client.get(reverse('employee_dashboard')).context['jobs']), [])
        self.assertEqual(self.client.get(reverse('execute_service', args=[second.pk])).status_code, 404)

    def test_application_detail_pages_do_not_require_django_admin_privileges(self):
        visit, _, _ = self.create_visit()
        for role in ['ADMIN', 'GENERAL_MANAGER']:
            user = User.objects.create_user('detail_' + role)
            user.profile.role = role
            user.profile.save()
            self.client.force_login(user)
            response = self.client.get(reverse('admin_visits'))
            self.assertContains(response, f'href="{reverse("visit_detail", args=[visit.pk])}"')
            self.assertNotContains(response, 'href="/admin/"')
            detail = self.client.get(reverse('visit_detail', args=[visit.pk]))
            self.assertEqual(detail.status_code, 200)
            self.assertContains(detail, self.customer.name)
            self.assertContains(detail, 'Working time:')
            self.assertEqual(self.client.post(reverse('visit_detail', args=[visit.pk]), {}).status_code, 405)

    def test_starting_cover_keeps_previous_home_branch_work_accessible(self):
        visit, first, _ = self.create_visit()
        visit.services.update(assigned_at=timezone.now() - timedelta(days=1))
        cover = Branch.objects.create(code='NEWCOVER', name='New cover branch')
        BranchDuty.objects.create(user=self.employee, date=timezone.localdate(), status='WORK',
                                  branch=cover, updated_by=self.manager)
        self.client.force_login(self.employee)
        self.assertIn(first.pk, {j.pk for j in self.client.get(reverse('employee_dashboard')).context['jobs']})
        self.assertEqual(self.client.get(reverse('execute_service', args=[first.pk])).status_code, 200)

    def test_visit_details_enforce_manager_branch_and_deny_employees(self):
        visit, _, _ = self.create_visit()
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('visit_detail', args=[visit.pk])).status_code, 200)
        self.manager.profile.branch = Branch.objects.create(code='OTHER', name='Other')
        self.manager.profile.save()
        self.assertEqual(self.client.get(reverse('visit_detail', args=[visit.pk])).status_code, 404)
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('visit_detail', args=[visit.pk])).status_code, 403)

    def test_supplied_username_and_password_cannot_override_generated_credentials(self):
        admin = User.objects.create_superuser('user_admin', 'a@example.com', 'test')
        self.client.force_login(admin)
        response = self.client.post(reverse('create_user'), {
            'username': self.employee.username, 'first_name': 'Duplicate', 'password': 'Example123!',
            'role': 'EMPLOYEE', 'branch': self.branch.pk,
        })
        self.assertEqual(response.status_code, 200)
        user = response.context['created_account']
        self.assertNotEqual(user.username, self.employee.username)
        self.assertFalse(user.check_password('Example123!'))
        self.assertEqual(User.objects.filter(username=self.employee.username).count(), 1)

    def timed_task(self):
        visit, first, _ = self.create_visit()
        task = first.tasks.get(task_type='SERVICE')
        start = timezone.now() - timedelta(minutes=30)
        task.started_at = start
        task.completed_at = start + timedelta(minutes=30)
        task.status = 'COMPLETED'
        task.supports_waiting = True
        task.save()
        for kind, left, right in [('ACTIVE', 0, 3), ('WAITING', 3, 28), ('ACTIVE', 28, 30)]:
            TaskTimingSegment.objects.create(task=task, kind=kind, started_at=start + timedelta(minutes=left),
                                            ended_at=start + timedelta(minutes=right))
        return visit, first, task

    def test_manager_sees_total_and_working_time_staff_see_no_timing(self):
        visit, first, task = self.timed_task()
        self.client.force_login(self.manager)
        page = self.client.get(reverse('manager_dashboard'))
        row = next(v for v in page.context['visits'] if v.pk == visit.pk)
        self.assertEqual(row.total_task_time, '30 minutes')
        self.assertEqual(row.working_time, '5 minutes')
        self.assertEqual(row.waiting_time, '25 minutes')
        self.assertContains(page, 'Working time: 5 minutes')
        self.assertContains(page, 'Total task time: 30 minutes')
        self.client.force_login(self.employee)
        for url in [reverse('employee_dashboard'), reverse('execute_service', args=[first.pk])]:
            response = self.client.get(url)
            self.assertNotContains(response, 'data-elapsed')
            self.assertNotContains(response, 'Working time:')
            self.assertNotContains(response, '30 minutes')
            self.assertNotContains(response, 'Hands-on:')
        self.assertEqual(task.timing_segments.count(), 3)

    def test_timing_summary_excludes_cancelled_and_opening_tasks_and_counts_open_segments(self):
        visit, first, task = self.timed_task()
        opening = first.tasks.get(task_type='HYGIENE')
        start = timezone.now() - timedelta(minutes=2)
        TaskTimingSegment.objects.create(task=opening, kind='ACTIVE', started_at=start)
        task.status = 'CANCELLED'
        task.save()
        add_timing_summary(first, first.tasks.prefetch_related('timing_segments'))
        self.assertEqual(first.total_seconds, 0)
        task.status = 'WAITING'
        task.save()
        TaskTimingSegment.objects.create(task=task, kind='WAITING', started_at=start)
        add_timing_summary(first, [task], now=start + timedelta(seconds=60))
        self.assertEqual(first.working_seconds, 300)
        self.assertEqual(first.waiting_seconds, 1560)
        self.assertEqual(first.total_seconds, 1860)

    def test_reassignment_preserves_opening_checks_for_non_one_order(self):
        visit, first, second = self.create_visit()
        first.order_number = 5
        first.save()
        second.order_number = 10
        second.save()
        hygiene = first.tasks.get(task_type='HYGIENE')
        hygiene.status = 'COMPLETED'
        hygiene.save()
        self.client.force_login(self.manager)
        self.client.post(reverse('cancel_and_reassign_service', args=[first.pk]), {
            'cancellation_reason': 'Shift change', 'employee': self.employee.pk, 'chair': '',
        })
        replacement = visit.services.get(replaces=first)
        self.assertFalse(replacement.tasks.filter(task_type='HYGIENE').exists())
        self.assertTrue(replacement.tasks.filter(task_type='CONSULT', status='PENDING').exists())
        self.client.force_login(self.employee)
        self.client.post(reverse('task_action', args=[replacement.tasks.get(task_type='CONSULT').pk, 'confirm']))
        self.assertEqual(replacement.tasks.get(task_type='CONSULT').status, 'COMPLETED')
