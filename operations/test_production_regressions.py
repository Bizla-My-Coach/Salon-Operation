from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .branch_imports import apply_branch_import, stage_branch_import
from .models import Branch, Chair, Customer, Feedback, Invoice, Service, Visit, VisitService


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}},
)
class ProductionRegressionTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(code='REVIEW-A', name='Synthetic A')
        self.admin = User.objects.create_superuser('review-admin', password='Synthetic-admin-pass!83')
        self.manager = User.objects.create_user('review-manager')
        self.manager.profile.role = 'MANAGER'
        self.manager.profile.branch = self.branch
        self.manager.profile.save()
        self.customer = Customer.objects.create(name='Original customer')
        self.service = Service.objects.create(code='REVIEW-S', name='Synthetic service', base_price=100)

    def verified_visit(self):
        visit = Visit.objects.create(
            branch=self.branch, customer=self.customer, created_by=self.manager, status='VERIFIED',
        )
        VisitService.objects.create(
            visit=visit, service=self.service, employee=self.manager,
            assigned_by=self.manager, status='VERIFIED',
        )
        return visit

    def create_account(self, password):
        self.client.force_login(self.admin)
        return self.client.post(reverse('create_user'), {
            'username': 'new-administrator', 'first_name': 'Synthetic',
            'password': password, 'role': 'ADMIN',
        })

    def invoice_payload(self, number):
        return {'customer_name': 'Changed customer', 'mobile': '', 'invoice_number': number}

    def test_account_creation_rejects_weak_and_user_similar_passwords(self):
        for password in ['123', 'password', '1234567890123', 'new-administrator']:
            with self.subTest(password=password):
                response = self.create_account(password)
                self.assertEqual(response.status_code, 200)
                self.assertIn('password', response.context['form'].errors)
                self.assertFalse(User.objects.filter(username='new-administrator').exists())

    def test_account_creation_preserves_valid_password_whitespace(self):
        password = '  Random-test-password!72  '
        response = self.create_account(password)
        self.assertRedirects(response, reverse('create_user'))
        user = User.objects.get(username='new-administrator')
        self.assertEqual(user.profile.role, 'ADMIN')
        self.assertTrue(user.check_password(password))
        self.assertFalse(user.check_password(password.strip()))

    def test_chair_import_keeps_existing_branch_chair_and_visit_reference(self):
        chair = Chair.objects.create(branch=self.branch, code='C1', name='Original chair')
        visit = self.verified_visit()
        assignment = visit.services.get()
        assignment.chair = chair
        assignment.save()
        payload = (
            'BRANCH_CODE,BRANCH_NAME,LOCATION,PHONE,CHAIR_CODE,CHAIR_NAME\n'
            'REVIEW-B,Synthetic B,,,C1,New branch chair\n'
        ).encode()
        staged = stage_branch_import(self.admin, 'synthetic.csv', payload)
        apply_branch_import(staged.pk, self.admin)
        chair.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(chair.branch_id, self.branch.pk)
        self.assertEqual(chair.name, 'Original chair')
        self.assertEqual(assignment.chair_id, chair.pk)
        self.assertEqual(Chair.objects.filter(code='C1').count(), 2)
        self.assertTrue(Chair.objects.filter(branch__code='REVIEW-B', code='C1').exists())

    def test_legacy_chair_import_cannot_bypass_branch_validation(self):
        self.client.force_login(self.admin)
        chair = Chair.objects.create(branch=self.branch, code='C1', name='Original chair')
        response = self.client.post(reverse('csv_import'), {'kind': 'chairs'})
        self.assertEqual(response.status_code, 400)
        chair.refresh_from_db()
        self.assertEqual(chair.branch_id, self.branch.pk)

    def test_invoice_rejects_existing_numbers_in_every_status_without_changes(self):
        current = self.verified_visit()
        self.client.force_login(self.manager)
        for status in ['VOID', 'DRAFT', 'COMPLETED']:
            with self.subTest(status=status):
                other = self.verified_visit()
                number = 'REVIEW-' + status
                Invoice.objects.create(visit=other, invoice_number=number, entered_by=self.manager, status=status)
                response = self.client.post(reverse('add_invoice', args=[current.pk]), self.invoice_payload(number))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'This invoice number is already in use.')
                self.assertFalse(Invoice.objects.filter(visit=current).exists())
                current.refresh_from_db()
                self.customer.refresh_from_db()
                self.assertEqual(current.status, 'VERIFIED')
                self.assertEqual(self.customer.name, 'Original customer')

    def test_own_draft_can_be_completed_with_its_reserved_number(self):
        visit = self.verified_visit()
        invoice = Invoice.objects.create(
            visit=visit, invoice_number='OWN-DRAFT', entered_by=self.manager, status='DRAFT',
        )
        self.client.force_login(self.manager)
        response = self.client.post(reverse('add_invoice', args=[visit.pk]), self.invoice_payload('OWN-DRAFT'))
        self.assertRedirects(response, reverse('manager_dashboard'))
        invoice.refresh_from_db()
        visit.refresh_from_db()
        self.assertEqual(invoice.status, 'COMPLETED')
        self.assertEqual(invoice.amount, 100)
        self.assertEqual(visit.status, 'INVOICED')
        self.assertEqual(Invoice.objects.filter(visit=visit).count(), 1)

    def test_invoice_database_collision_after_validation_rolls_back_customer_update(self):
        visit = self.verified_visit()
        other = self.verified_visit()
        Invoice.objects.create(visit=other, invoice_number='CLAIMED', entered_by=self.manager)
        self.client.force_login(self.manager)
        # Model a competing request claiming the number after validation succeeds.
        with patch('operations.forms.CompleteInvoiceForm.clean_invoice_number', return_value='CLAIMED'):
            response = self.client.post(reverse('add_invoice', args=[visit.pk]), self.invoice_payload('CLAIMED'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This invoice number is already in use.')
        self.customer.refresh_from_db()
        visit.refresh_from_db()
        self.assertEqual(self.customer.name, 'Original customer')
        self.assertEqual(visit.status, 'VERIFIED')
        self.assertFalse(Invoice.objects.filter(visit=visit).exists())
        self.assertFalse(Feedback.objects.filter(visit=visit).exists())
