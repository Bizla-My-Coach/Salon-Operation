from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Branch
from .tests import CombinedServiceWorkflowTests


# These tests cover the SOP step flow; the simple visit flow has its own tests.
@override_settings(SIMPLE_VISIT_FLOW=False)
@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class WorkflowInterfaceTests(TestCase):
    setUp = CombinedServiceWorkflowTests.setUp
    create_visit = CombinedServiceWorkflowTests.create_visit
    assignment_payload = CombinedServiceWorkflowTests.assignment_payload

    def page(self, name, *args):
        self.client.force_login(self.manager)
        response = self.client.get(reverse(name, args=args))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="workflow-page"')
        self.assertContains(response, 'css/workflows.css')
        self.assertContains(response, 'Back to visits')
        return response

    def test_create_fields_are_labeled_and_mobile_keyboard_is_configured(self):
        response = self.page('new_visit')
        for field in ['customer_name', 'mobile', 'invoice_number']:
            self.assertContains(response, f'for="id_{field}"')
        self.assertContains(response, 'Step 1 of 2')
        self.assertContains(response, 'inputmode="numeric"')
        self.assertContains(response, 'autocomplete="name"')
        self.assertContains(response, 'Continue to assignments')
        self.assertTrue(response.context['form'].fields['invoice_number'].disabled)
        response = self.client.post(reverse('new_visit'), {'customer_name': '', 'mobile': 'abc'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'has-errors')
        self.assertContains(response, 'Enter a valid 10-digit mobile number.')

    def test_assignment_rows_and_new_row_have_unique_accessible_labels(self):
        visit, first, _ = self.create_visit()
        response = self.page('edit_visit_services', visit.pk)
        for prefix in ['services-0', 'services-1', 'services-__prefix__']:
            self.assertContains(response, f'id="{prefix}-service-search"')
            self.assertContains(response, f'aria-controls="{prefix}-service-options"')
            for field in ['order_number', 'employee', 'chair']:
                self.assertContains(response, f'for="id_{prefix}-{field}"')
        self.assertContains(response, self.customer.name)
        self.assertContains(response, 'name="services-TOTAL_FORMS"')
        self.assertContains(response, 'Save service assignments')
        first.tasks.filter(task_type='HYGIENE').update(status='COMPLETED')
        response = self.page('edit_visit_services', visit.pk)
        self.assertTrue(response.context['formset'].forms[0].execution_locked)
        self.assertContains(response, 'Started work is locked.')
        self.assertContains(response, reverse('cancel_and_reassign_service', args=[first.pk]))

    def test_assignment_validation_errors_still_render_and_customer_text_is_escaped(self):
        visit, first, second = self.create_visit()
        self.customer.name = '<script>alert("test")</script>'
        self.customer.save()
        self.client.force_login(self.manager)
        response = self.client.post(reverse('edit_visit_services', args=[visit.pk]),
                                    self.assignment_payload(first, second, first_order=1, second_order=1))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'unique order number')
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, self.customer.name)

    def submitted_visit(self):
        visit, first, second = self.create_visit()
        for item in [first, second]:
            item.tasks.update(status='COMPLETED')
            item.status = 'EMPLOYEE_DONE'
            item.employee_notes = 'Customer comfortable, all steps completed.'
            item.save()
        visit.status = 'EMPLOYEE_DONE'
        visit.save()
        first.tasks.filter(task_type='SERVICE').update(note='Checked finish with customer.')
        return visit, first, second

    def test_both_verification_pages_show_notes_checklist_and_manager_only_timings(self):
        visit, first, second = self.submitted_visit()
        for name, pk in [('verify_visit', visit.pk), ('verify_service', first.pk)]:
            response = self.page(name, pk)
            self.assertContains(response, '<details class="workflow-checklist" open>')
            self.assertContains(response, 'Total task time')
            self.assertContains(response, 'Working time')
            self.assertContains(response, first.employee_notes)
            self.assertContains(response, 'Checked finish with customer.')
            self.assertContains(response, self.customer.name)
        response = self.page('verify_visit', visit.pk)
        self.assertLess(response.content.index(self.service_a.name.encode()), response.content.index(self.service_b.name.encode()))
        for item in [first, second]:
            self.assertContains(response, f'name="manager_notes_{item.pk}"')
        response = self.client.post(reverse('verify_visit', args=[visit.pk]), {
            f'manager_notes_{first.pk}': 'Reviewed first', f'manager_notes_{second.pk}': 'Reviewed second',
        })
        self.assertRedirects(response, reverse('manager_dashboard'))
        visit.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(visit.status, 'VERIFIED')
        self.assertEqual(first.manager_notes, 'Reviewed first')

    def test_single_service_approval_and_role_branch_guards_are_preserved(self):
        visit, first, _ = self.submitted_visit()
        self.client.force_login(self.manager)
        self.assertRedirects(self.client.post(reverse('verify_service', args=[first.pk]), {'manager_notes': 'Approved'}),
                             reverse('manager_dashboard'))
        first.refresh_from_db()
        self.assertEqual(first.manager_notes, 'Approved')
        self.manager.profile.branch = Branch.objects.create(code='OTHERUI', name='Other branch')
        self.manager.profile.save()
        for name, pk in [('edit_visit_services', visit.pk), ('verify_visit', visit.pk), ('verify_service', first.pk)]:
            self.assertEqual(self.client.get(reverse(name, args=[pk])).status_code, 404)
        self.client.force_login(self.employee)
        for name, args in [('new_visit', []), ('edit_visit_services', [visit.pk]),
                           ('verify_visit', [visit.pk]), ('verify_service', [first.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403)
