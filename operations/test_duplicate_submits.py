import uuid

from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Customer, Visit, VisitService
from .tests import CombinedServiceWorkflowTests


@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class DuplicateSubmitAndSharedCustomerTests(TestCase):
    setUp = CombinedServiceWorkflowTests.setUp
    create_visit = CombinedServiceWorkflowTests.create_visit
    assignment_payload = CombinedServiceWorkflowTests.assignment_payload

    def new_visit(self, name, mobile='', token=None):
        data = {'customer_name': name, 'mobile': mobile}
        if token:
            data['submit_token'] = str(token)
        return self.client.post(reverse('new_visit'), data)

    def test_shared_mobile_never_renames_other_open_cards(self):
        self.client.force_login(self.manager)
        for name in ['Anu', 'Biju', 'Chitra']:
            self.new_visit(name, '9876543210', uuid.uuid4())
        names = list(Visit.objects.filter(branch=self.branch).order_by('id').values_list('customer__name', flat=True))
        self.assertEqual(names, ['Anu', 'Biju', 'Chitra'])
        dashboard = self.client.get(reverse('manager_dashboard')).content.decode()
        for name in ['Anu', 'Biju', 'Chitra']:
            self.assertIn(f'<h2>{name}</h2>', dashboard)

    def test_returning_customer_with_same_name_and_mobile_is_reused(self):
        self.client.force_login(self.manager)
        self.new_visit('Anu', '9876543210', uuid.uuid4())
        self.new_visit('anu ', '9876543210', uuid.uuid4())
        self.assertEqual(Customer.objects.filter(mobile='9876543210').count(), 1)
        self.assertEqual(Visit.objects.filter(customer__mobile='9876543210').count(), 2)

    def test_repeated_create_press_makes_one_visit(self):
        self.client.force_login(self.manager)
        token = uuid.uuid4()
        first = self.new_visit('Anu', '', token)
        second = self.new_visit('Anu', '', token)
        visit = Visit.objects.get(customer__name='Anu')
        self.assertEqual(Visit.objects.filter(customer__name='Anu').count(), 1)
        self.assertRedirects(first, reverse('edit_visit_services', args=[visit.pk]), fetch_redirect_response=False)
        self.assertRedirects(second, reverse('edit_visit_services', args=[visit.pk]), fetch_redirect_response=False)

    def test_create_page_carries_a_fresh_token(self):
        self.client.force_login(self.manager)
        page = self.client.get(reverse('new_visit')).content.decode()
        self.assertIn('name="submit_token"', page)
        self.assertIn('data-single-submit', page)

    def test_repeated_assignment_press_adds_services_once(self):
        visit, first, second = self.create_visit()
        self.client.force_login(self.manager)
        page = self.client.get(reverse('edit_visit_services', args=[visit.pk]))
        token = str(page.context['assignment_token'])
        payload = self.assignment_payload(first, second)
        payload.update({
            'assignment_token': token, 'services-TOTAL_FORMS': '3',
            'services-2-id': '', 'services-2-order_number': '3',
            'services-2-service': str(self.service_b.pk), 'services-2-employee': str(self.employee.pk),
            'services-2-chair': '',
        })
        self.client.post(reverse('edit_visit_services', args=[visit.pk]), payload)
        self.client.post(reverse('edit_visit_services', args=[visit.pk]), payload)
        self.assertEqual(VisitService.objects.filter(visit=visit).exclude(status='CANCELLED').count(), 3)
        # A new page load gets a new token, so later edits still save.
        payload['assignment_token'] = str(uuid.uuid4())
        payload['services-2-order_number'] = '4'
        payload['services-2-id'] = ''
        self.client.post(reverse('edit_visit_services', args=[visit.pk]), payload)
        self.assertEqual(VisitService.objects.filter(visit=visit).exclude(status='CANCELLED').count(), 4)

    def test_invoice_name_change_does_not_rename_other_visits(self):
        visit, first, second = self.create_visit()
        other = Visit.objects.create(branch=self.branch, customer=self.customer, status='WAITING', created_by=self.manager)
        first.status = second.status = 'VERIFIED'
        first.save(); second.save()
        visit.status = 'VERIFIED'; visit.save()
        self.client.force_login(self.manager)
        self.client.post(reverse('add_invoice', args=[visit.pk]),
                         {'customer_name': 'Renamed', 'mobile': '', 'invoice_number': '5001'})
        visit.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(visit.customer.name, 'Renamed')
        self.assertEqual(other.customer.name, 'Test Customer')
