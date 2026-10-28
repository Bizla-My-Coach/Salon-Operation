from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .models import Branch, VisitTask
from .tests import CombinedServiceWorkflowTests


class GroupedTaskTimingTests(TestCase):
    setUp = CombinedServiceWorkflowTests.setUp
    create_visit = CombinedServiceWorkflowTests.create_visit

    def setup_tasks(self):
        visit, first, _ = self.create_visit()
        other, other_first, _ = self.create_visit()
        for v in [visit, other]:
            VisitTask.objects.filter(visit_service__visit=v, task_type__in=['HYGIENE', 'CONSULT']).update(status='COMPLETED')
        a = first.tasks.get(task_type='SERVICE')
        b = other_first.tasks.get(task_type='SERVICE')
        a.supports_waiting = True
        a.staff_instructions = 'Apply, wait, resume and remove.'
        a.save()
        self.client.force_login(self.employee)
        return a, b

    def action(self, task, action, now=None):
        with patch('operations.views.timezone.now', return_value=now or timezone.now()):
            response = self.client.post(reverse('task_action', args=[task.pk, action]))
        task.refresh_from_db()
        return response

    def test_different_visits_can_start_and_resume_together_with_separate_timers(self):
        a, b = self.setup_tasks()
        t = timezone.now()
        self.action(a, 'start', t)
        self.action(b, 'start', t)
        self.assertEqual(b.status, 'IN_PROGRESS')
        self.action(a, 'wait', t + timedelta(minutes=3))
        self.action(a, 'resume', t + timedelta(minutes=10))
        self.assertEqual(a.status, 'IN_PROGRESS')
        self.assertEqual(b.status, 'IN_PROGRESS')
        self.action(b, 'complete', t + timedelta(minutes=11))
        self.action(a, 'complete', t + timedelta(minutes=12))
        self.assertEqual(a.labour_seconds, 5 * 60)
        self.assertEqual(a.waiting_seconds, 7 * 60)
        self.assertEqual(a.timing_segments.count(), 3)
        self.assertFalse(a.timing_segments.filter(ended_at__isnull=True).exists())
        self.assertEqual(b.labour_seconds, 11 * 60)
        self.assertEqual(b.waiting_seconds, 0)
        self.assertEqual(b.timing_segments.count(), 1)

    def test_services_in_same_visit_run_and_resume_together_with_separate_timers(self):
        a, _ = self.setup_tasks()
        later = a.visit_service.visit.services.get(order_number=2)
        task = later.tasks.get(task_type='SERVICE')
        t = timezone.now()
        self.action(a, 'start', t)
        self.action(task, 'start', t + timedelta(minutes=1))
        self.assertEqual(task.status, 'IN_PROGRESS')
        self.action(a, 'wait', t + timedelta(minutes=3))
        self.action(a, 'resume', t + timedelta(minutes=5))
        self.assertEqual(a.status, 'IN_PROGRESS')
        self.action(task, 'complete', t + timedelta(minutes=7))
        self.client.post(reverse('finish_service', args=[later.pk]))
        later.refresh_from_db()
        a.visit_service.visit.refresh_from_db()
        self.assertEqual(later.status, 'EMPLOYEE_DONE')
        self.assertEqual(a.visit_service.visit.status, 'IN_PROGRESS')
        self.action(a, 'complete', t + timedelta(minutes=10))
        self.client.post(reverse('finish_service', args=[a.visit_service_id]))
        a.visit_service.visit.refresh_from_db()
        self.assertEqual(a.visit_service.visit.status, 'EMPLOYEE_DONE')
        self.assertEqual(a.labour_seconds, 8 * 60)
        self.assertEqual(a.waiting_seconds, 2 * 60)
        self.assertEqual(task.labour_seconds, 6 * 60)
        self.assertFalse(a.timing_segments.filter(ended_at__isnull=True).exists())
        self.assertFalse(task.timing_segments.filter(ended_at__isnull=True).exists())

    def test_same_service_allows_multiple_active_tasks_and_resume(self):
        a, _ = self.setup_tasks()
        b = VisitTask.objects.create(
            visit_service=a.visit_service, sequence=100, phase='DURING',
            task_type='SERVICE', title='Another task',
        )
        self.action(a, 'start')
        self.action(b, 'start')
        self.assertEqual(b.status, 'IN_PROGRESS')
        self.action(a, 'wait')
        self.action(b, 'start')
        self.assertEqual(b.status, 'IN_PROGRESS')
        self.action(a, 'resume')
        self.assertEqual(a.status, 'IN_PROGRESS')
        self.action(b, 'complete')
        self.action(a, 'resume')
        self.assertEqual(a.status, 'IN_PROGRESS')

    def test_later_service_can_start_before_first_service_after_opening_checks(self):
        a, _ = self.setup_tasks()
        later = a.visit_service.visit.services.get(order_number=2)
        task = later.tasks.get(task_type='SERVICE')
        self.action(task, 'start')
        self.assertEqual(task.status, 'IN_PROGRESS')
        a.refresh_from_db()
        self.assertEqual(a.status, 'PENDING')

    def test_parallel_work_preserves_task_group_sequence_rules(self):
        a, _ = self.setup_tasks()
        later = VisitTask.objects.create(
            visit_service=a.visit_service, sequence=100, phase='DURING',
            task_type='SERVICE', title='Finishing', staff_instructions='Finish after the procedure.',
        )
        self.action(a, 'start')
        self.action(later, 'start')
        self.assertEqual(later.status, 'PENDING')
        self.action(a, 'complete')
        self.action(later, 'start')
        self.assertEqual(later.status, 'IN_PROGRESS')

    def test_back_button_preserves_active_work_and_dashboard_opens_all_assigned_services(self):
        a, b = self.setup_tasks()
        self.action(a, 'start')
        page = self.client.get(reverse('execute_service', args=[a.visit_service_id]))
        self.assertContains(page, 'Back to my services')
        self.assertContains(page, f'href="{reverse("employee_dashboard")}"')
        dashboard = self.client.get(reverse('employee_dashboard'))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, self.customer.name)
        self.assertContains(dashboard, self.service_a.name)
        self.assertContains(dashboard, self.service_b.name)
        self.assertContains(dashboard, f'Visit #{a.visit_service.visit_id}')
        self.assertContains(dashboard, f'href="{reverse("execute_service", args=[b.visit_service_id])}"')
        later = a.visit_service.visit.services.get(order_number=2)
        self.assertContains(dashboard, f'href="{reverse("execute_service", args=[later.pk])}"')
        self.assertNotContains(dashboard, 'Finish the earlier service')
        a.refresh_from_db()
        self.assertEqual(a.status, 'IN_PROGRESS')
        self.assertEqual(a.timing_segments.filter(ended_at__isnull=True).count(), 1)

    def test_waiting_task_cannot_complete_skip_or_finish_service(self):
        a, b = self.setup_tasks()
        self.action(a, 'start')
        self.action(a, 'wait')
        self.action(a, 'complete')
        self.assertEqual(a.status, 'WAITING')
        a.can_skip = True
        a.skip_reason_required = False
        a.save()
        self.action(a, 'skip')
        self.assertEqual(a.status, 'WAITING')
        self.client.post(reverse('finish_service', args=[a.visit_service_id]))
        a.visit_service.refresh_from_db()
        self.assertEqual(a.visit_service.status, 'IN_PROGRESS')

    def test_actions_are_idempotent_and_non_waiting_tasks_cannot_wait(self):
        a, b = self.setup_tasks()
        self.action(b, 'start')
        self.action(b, 'start')
        self.action(b, 'wait')
        self.assertEqual(b.status, 'IN_PROGRESS')
        self.assertEqual(b.timing_segments.count(), 1)
        self.action(b, 'complete')
        self.action(b, 'complete')
        self.assertEqual(b.timing_segments.count(), 1)
        self.action(a, 'start')
        self.action(a, 'wait')
        self.action(a, 'wait')
        self.assertEqual(a.timing_segments.count(), 2)

    def test_other_employee_and_wrong_branch_cannot_act(self):
        a, _ = self.setup_tasks()
        stranger = User.objects.create_user('stranger')
        stranger.profile.role = 'EMPLOYEE'
        stranger.profile.branch = self.branch
        stranger.profile.save()
        self.client.force_login(stranger)
        self.assertEqual(self.action(a, 'start').status_code, 404)
        self.assertEqual(list(self.client.get(reverse('employee_dashboard')).context['jobs']), [])
        self.assertEqual(self.client.get(reverse('execute_service', args=[a.visit_service_id])).status_code, 404)
        self.employee.profile.branch = Branch.objects.create(code='OTHER', name='Other')
        self.employee.profile.save()
        self.client.force_login(self.employee)
        self.assertEqual(self.action(a, 'start').status_code, 404)
        self.assertEqual(list(self.client.get(reverse('employee_dashboard')).context['jobs']), [])
        self.assertEqual(self.client.get(reverse('execute_service', args=[a.visit_service_id])).status_code, 404)

    def test_non_employee_cannot_access_staff_pages_or_actions(self):
        a, _ = self.setup_tasks()
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('employee_dashboard')).status_code, 403)
        self.assertEqual(self.client.get(reverse('execute_service', args=[a.visit_service_id])).status_code, 403)
        self.assertEqual(self.action(a, 'start').status_code, 403)

    def test_wait_ui_instructions_and_snapshot_protection(self):
        a, _ = self.setup_tasks()
        self.action(a, 'start')
        self.action(a, 'wait')
        response = self.client.get(reverse('execute_service', args=[a.visit_service_id]))
        self.assertContains(response, 'Resume')
        self.assertContains(response, 'Apply, wait, resume and remove.')
        self.assertNotContains(response, 'data-elapsed=')
        self.assertNotContains(response, 'Hands-on:')
        source = a.source_operational_task
        source.name = 'Changed master title'
        source.save()
        from .workflow import build_visit_tasks
        build_visit_tasks(a.visit_service.visit)
        a.refresh_from_db()
        self.assertNotEqual(a.title, source.name)
        self.assertEqual(a.status, 'WAITING')

    def test_legacy_snapshot_preserved_and_legacy_active_interval_adopted(self):
        a, _ = self.setup_tasks()
        t = timezone.now() - timedelta(minutes=5)
        a.status = 'IN_PROGRESS'
        a.started_at = t
        a.save()
        self.action(a, 'wait', t + timedelta(minutes=4))
        self.assertEqual(a.labour_seconds, 240)

    def test_waiting_cancellation_closes_segment_without_erasing_history(self):
        a, _ = self.setup_tasks()
        self.action(a, 'start')
        self.action(a, 'wait')
        self.client.force_login(self.manager)
        response = self.client.post(reverse('cancel_and_reassign_service', args=[a.visit_service_id]), {
            'cancellation_reason': 'Shift change', 'employee': self.employee.pk, 'chair': '',
        })
        self.assertEqual(response.status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.status, 'CANCELLED')
        self.assertEqual(a.timing_segments.count(), 2)
        self.assertFalse(a.timing_segments.filter(ended_at__isnull=True).exists())


class ConcurrentTaskStartTests(TransactionTestCase):
    setUp = CombinedServiceWorkflowTests.setUp
    create_visit = CombinedServiceWorkflowTests.create_visit

    def simultaneous_starts(self, tasks):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connection, connections
        from django.test import Client
        if connection.vendor != 'postgresql':
            self.skipTest('Requires authoritative PostgreSQL row locks')
        clients=[Client(), Client()]
        for client in clients:
            client.force_login(self.employee)
        barrier=Barrier(2)
        def start(pair):
            client, task=pair
            try:
                barrier.wait(timeout=10)
                return client.post(reverse('task_action',args=[task.pk,'start'])).status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(start,zip(clients,tasks))),[302,302])

    def test_simultaneous_starts_in_different_visits_both_succeed(self):
        _, first, _ = self.create_visit()
        _, second, _ = self.create_visit()
        VisitTask.objects.filter(task_type__in=['HYGIENE', 'CONSULT']).update(status='COMPLETED')
        tasks = [first.tasks.get(task_type='SERVICE'), second.tasks.get(task_type='SERVICE')]
        self.simultaneous_starts(tasks)
        self.assertEqual(VisitTask.objects.filter(status='IN_PROGRESS').count(), 2)

    def test_simultaneous_starts_in_same_service_both_succeed(self):
        _, first, _ = self.create_visit()
        VisitTask.objects.filter(task_type__in=['HYGIENE', 'CONSULT']).update(status='COMPLETED')
        procedure = first.tasks.get(task_type='SERVICE')
        another = VisitTask.objects.create(
            visit_service=first, sequence=100, phase='DURING',
            task_type='SERVICE', title='Another task',
        )
        self.simultaneous_starts([procedure, another])
        self.assertEqual(VisitTask.objects.filter(status='IN_PROGRESS').count(), 2)

    def test_simultaneous_starts_in_different_services_in_same_visit_both_succeed(self):
        _, first, second = self.create_visit()
        VisitTask.objects.filter(task_type__in=['HYGIENE', 'CONSULT']).update(status='COMPLETED')
        tasks = [first.tasks.get(task_type='SERVICE'), second.tasks.get(task_type='SERVICE')]
        self.simultaneous_starts(tasks)
        tasks[1].refresh_from_db()
        self.assertEqual(tasks[1].status, 'IN_PROGRESS')
        self.assertEqual(VisitTask.objects.filter(status='IN_PROGRESS').count(), 2)

