from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .models import Visit
from .tests import CombinedServiceWorkflowTests
from .timing import compact_duration


class CompactDurationTests(SimpleTestCase):
    def test_short_durations_keep_same_minute_rounding(self):
        for seconds, expected in [(0, '0m'), (29, '0m'), (30, '1m'),
                                  (300, '5m'), (3600, '1h'), (6000, '1h 40m')]:
            with self.subTest(seconds=seconds):
                self.assertEqual(compact_duration(seconds), expected)


@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ManagerCardTests(TestCase):
    setUp = CombinedServiceWorkflowTests.setUp
    create_visit = CombinedServiceWorkflowTests.create_visit

    def dashboard(self):
        self.client.force_login(self.manager)
        page = self.client.get(reverse('manager_dashboard'))
        self.assertEqual(page.status_code, 200)
        return page

    def test_compact_page_heading_preserves_new_visit_action(self):
        page = self.dashboard()
        self.assertContains(page, 'class="page-head split manager-page-head"')
        self.assertContains(page, '<h1>Live operations</h1>')
        self.assertContains(page, f'href="{reverse("new_visit")}" class="button primary">+ New service visit</a>')

    def test_submitted_card_has_timings_review_and_add_services(self):
        visit, first, second = self.create_visit()
        visit.services.update(status='EMPLOYEE_DONE')
        visit.status = 'EMPLOYEE_DONE'
        visit.save()
        page = self.dashboard()
        self.assertContains(page, self.customer.name)
        self.assertContains(page, '<dt>Total</dt>')
        self.assertContains(page, '<dt>Working</dt>')
        self.assertContains(page, '<dt>Waiting</dt>')
        self.assertContains(page, 'title="Total task time: 0 minutes"')
        self.assertContains(page, '>0m</dd>', count=3)
        self.assertContains(page, f'href="{reverse("verify_visit", args=[visit.pk])}"', count=1)
        self.assertNotContains(page, f'href="{reverse("visit_detail", args=[visit.pk])}"')
        self.assertContains(page, f'href="{reverse("edit_visit_services", args=[visit.pk])}"', count=1)
        self.assertContains(page, '>Add services</a>')
        self.assertNotContains(page, 'Review every service, task')
        self.assertNotContains(page, 'class="visit-actions"')
        self.assertNotContains(page, 'class="progress"')
        self.assertContains(page, '>Submitted</span>', count=2)

    def test_upcoming_edit_order_and_active_reassignment_stay_available(self):
        visit, first, second = self.create_visit()
        first.status = 'IN_PROGRESS'
        first.save()
        page = self.dashboard()
        self.assertContains(page, f'href="{reverse("edit_visit_services", args=[visit.pk])}"', count=1)
        self.assertContains(page, f'href="{reverse("cancel_and_reassign_service", args=[first.pk])}"')
        self.assertNotContains(page, f'href="{reverse("cancel_and_reassign_service", args=[second.pk])}"')
        self.assertContains(page, 'class="manager-service-progress"', count=2)
        self.assertContains(page, '2 services')
        self.assertContains(page, 'employee · No chair', count=2)
        self.assertLess(page.content.index(self.service_a.name.encode()),
                        page.content.index(self.service_b.name.encode()))

    def test_confirmed_opening_retains_reassignment_and_add_services(self):
        visit, first, second = self.create_visit()
        first.tasks.filter(task_type='HYGIENE').update(status='COMPLETED')
        second.status = 'EMPLOYEE_DONE'
        second.save()
        page = self.dashboard()
        self.assertContains(page, f'href="{reverse("edit_visit_services", args=[visit.pk])}"', count=1)
        self.assertContains(page, '>Add services</a>')
        self.assertContains(page, f'href="{reverse("cancel_and_reassign_service", args=[first.pk])}"')

    def test_single_started_service_keeps_add_services_action(self):
        visit, first, second = self.create_visit()
        second.delete()
        first.tasks.filter(task_type='HYGIENE').update(status='COMPLETED')
        for status in ['ASSIGNED', 'IN_PROGRESS', 'PAUSED', 'EMPLOYEE_DONE']:
            with self.subTest(status=status):
                first.status = status
                first.save()
                visit.status = 'EMPLOYEE_DONE' if status == 'EMPLOYEE_DONE' else 'IN_PROGRESS'
                visit.save()
                page = self.dashboard()
                self.assertContains(page, f'href="{reverse("edit_visit_services", args=[visit.pk])}"', count=1)
                self.assertContains(page, '>Add services</a>')
                self.assertNotContains(page, '>Edit assignments</a>')

    def test_cancelled_assignments_are_collapsed_and_excluded_from_active_count(self):
        visit, first, second = self.create_visit()
        second.status = 'CANCELLED'
        second.cancellation_reason = 'Staff change'
        second.save()
        page = self.dashboard()
        self.assertContains(page, '<details class="manager-cancelled-history">')
        self.assertContains(page, '<summary>1 cancelled assignment</summary>')
        self.assertNotContains(page, '<details class="manager-cancelled-history" open')
        self.assertContains(page, '1 service</p>')
        self.assertContains(page, 'Cancelled: Staff change')
        row = next(v for v in page.context['visits'] if v.pk == visit.pk)
        self.assertEqual([s.pk for s in row.active_services], [first.pk])
        self.assertEqual([s.pk for s in row.cancelled_services], [second.pk])

    def test_empty_visit_keeps_assignment_action(self):
        visit = Visit.objects.create(branch=self.branch, customer=self.customer,
                                     created_by=self.manager, status='WAITING')
        page = self.dashboard()
        self.assertContains(page, 'No services assigned yet.')
        self.assertContains(page, 'Assign services')
        self.assertContains(page, f'href="{reverse("edit_visit_services", args=[visit.pk])}"', count=1)

    def test_invoice_and_feedback_actions_remain_single_header_actions(self):
        visit, _, _ = self.create_visit()
        visit.services.update(status='VERIFIED')
        visit.status = 'VERIFIED'
        visit.save()
        page = self.dashboard()
        self.assertContains(page, f'href="{reverse("add_invoice", args=[visit.pk])}"', count=1)
        self.assertNotContains(page, f'href="{reverse("edit_visit_services", args=[visit.pk])}"')
        visit.status = 'INVOICED'
        visit.save()
        page = self.dashboard()
        self.assertContains(page, f'href="{reverse("collect_feedback", args=[visit.pk])}"', count=1)
        self.assertNotContains(page, f'href="{reverse("add_invoice", args=[visit.pk])}"')
