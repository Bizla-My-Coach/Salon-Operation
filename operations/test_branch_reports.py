from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Branch, Customer, Invoice, Visit


class BranchReportTests(TestCase):
    def setUp(self):
        self.north = Branch.objects.create(code='N', name='North')
        self.south = Branch.objects.create(code='S', name='South')
        self.manager = self.person('manager', 'MANAGER', self.north)
        customer = Customer.objects.create(name='Customer')
        for branch, amount in [(self.north, 700), (self.south, 1900)]:
            visit = Visit.objects.create(branch=branch, customer=customer, status='CLOSED', created_by=self.manager)
            Invoice.objects.create(visit=visit, invoice_number=f'I-{branch.code}', amount=amount, entered_by=self.manager)

    def person(self, username, role, branch=None):
        user = User.objects.create_user(username, password='pw')
        user.profile.role = role
        user.profile.branch = branch
        user.profile.save()
        return user

    def test_manager_sees_only_own_branch(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('branch_reports'), {'branch': self.south.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected'], self.north)
        self.assertContains(response, '₹700')
        self.assertNotContains(response, '₹1,900')
        self.assertNotContains(response, 'South')

    def test_general_manager_and_admin_see_every_branch(self):
        for role in ('GENERAL_MANAGER', 'ADMIN'):
            self.client.force_login(self.person(role.lower(), role))
            response = self.client.get(reverse('branch_reports'))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.context['rows']), 2)
            self.assertEqual(response.context['total']['revenue'], 2600)
            self.assertContains(response, 'South')

    def test_employee_and_bad_period_are_refused(self):
        self.client.force_login(self.person('stylist', 'EMPLOYEE', self.north))
        self.assertEqual(self.client.get(reverse('branch_reports')).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('branch_reports'), {'period': 'year'}).status_code, 400)
