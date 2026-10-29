from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .branch_imports import apply_branch_import, stage_branch_import
from .forms import InvoiceForm
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

    def test_account_creation_requires_a_branch_even_for_admins(self):
        response = self.create_account('123')
        self.assertEqual(response.status_code, 200)
        self.assertIn('branch', response.context['form'].errors)
        self.assertFalse(User.objects.filter(username='new-administrator').exists())

    def test_account_creation_generates_a_valid_password(self):
        from django.contrib.auth.password_validation import validate_password
        self.client.force_login(self.admin)
        response = self.client.post(reverse('create_user'), {
            'branch': self.branch.pk, 'first_name': 'Synthetic', 'role': 'ADMIN',
            'username': 'forged', 'password': '123',
        })
        user = response.context['created_account']
        password = response.context['generated_password']
        self.assertEqual(user.profile.role, 'ADMIN')
        self.assertEqual(len(password), 8)
        self.assertTrue(user.check_password(password))
        validate_password(password, user=user)

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
        for number, status in enumerate(['VOID', 'DRAFT', 'COMPLETED'], 977):
            with self.subTest(status=status):
                other = self.verified_visit()
                Invoice.objects.create(visit=other, invoice_number=f'INV/26-27/{number}', entered_by=self.manager, status=status)
                response = self.client.post(reverse('add_invoice', args=[current.pk]), self.invoice_payload(str(number)))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'This invoice number is already in use in this branch.')
                self.assertFalse(Invoice.objects.filter(visit=current).exists())
                current.refresh_from_db()
                self.customer.refresh_from_db()
                self.assertEqual(current.status, 'VERIFIED')
                self.assertEqual(self.customer.name, 'Original customer')

    def test_own_draft_can_be_completed_with_its_reserved_number(self):
        visit = self.verified_visit()
        invoice = Invoice.objects.create(
            visit=visit, invoice_number='INV/26-27/977', entered_by=self.manager, status='DRAFT',
        )
        self.client.force_login(self.manager)
        page = self.client.get(reverse('add_invoice', args=[visit.pk]))
        self.assertEqual(page.context['form']['invoice_number'].value(), '977')
        response = self.client.post(reverse('add_invoice', args=[visit.pk]), self.invoice_payload('977'))
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
        Invoice.objects.create(visit=other, invoice_number='INV/26-27/977', entered_by=self.manager)
        self.client.force_login(self.manager)
        # Model a competing request claiming the number after validation succeeds.
        with patch('operations.forms.CompleteInvoiceForm.clean_invoice_number', return_value='INV/26-27/977'):
            response = self.client.post(reverse('add_invoice', args=[visit.pk]), self.invoice_payload('977'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This invoice number is already in use in this branch.')
        self.customer.refresh_from_db()
        visit.refresh_from_db()
        self.assertEqual(self.customer.name, 'Original customer')
        self.assertEqual(visit.status, 'VERIFIED')
        self.assertFalse(Invoice.objects.filter(visit=visit).exists())
        self.assertFalse(Feedback.objects.filter(visit=visit).exists())

    def test_different_branches_can_save_the_same_invoice_number(self):
        other_branch = Branch.objects.create(code='REVIEW-B', name='Synthetic B')
        other = self.verified_visit()
        other.branch = other_branch
        other.save()
        existing = Invoice.objects.create(visit=other, invoice_number='INV/26-27/977', entered_by=self.manager)
        current = self.verified_visit()
        self.client.force_login(self.manager)
        response = self.client.post(reverse('add_invoice', args=[current.pk]), self.invoice_payload('977'))
        self.assertRedirects(response, reverse('manager_dashboard'))
        invoice = Invoice.objects.get(visit=current)
        self.assertEqual(invoice.invoice_number, existing.invoice_number)
        self.assertEqual(invoice.branch_id, self.branch.pk)
        self.assertEqual(existing.branch_id, other_branch.pk)
        self.assertEqual(Invoice.objects.filter(invoice_number='INV/26-27/977').count(), 2)

    def test_invoice_database_constraint_rejects_duplicates_in_the_same_branch(self):
        first, second = self.verified_visit(), self.verified_visit()
        Invoice.objects.create(visit=first, invoice_number='INV/26-27/977', entered_by=self.manager)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Invoice.objects.create(visit=second, invoice_number='INV/26-27/977', entered_by=self.manager)

    def test_invoice_entry_accepts_only_ascii_numeric_suffix(self):
        current = self.verified_visit()
        self.client.force_login(self.manager)
        for number in ['INV/26-27/977', '977/1', '-977', '9.77', '9e2', '٩٧٧', 'abc']:
            with self.subTest(number=number):
                response = self.client.post(reverse('add_invoice', args=[current.pk]), self.invoice_payload(number))
                self.assertEqual(response.status_code, 200)
                self.assertIn('invoice_number', response.context['form'].errors)
                self.assertFalse(Invoice.objects.filter(visit=current).exists())
        response = self.client.get(reverse('add_invoice', args=[current.pk]))
        self.assertContains(response, 'Saved as INV/26-27/977.')
        self.assertContains(response, 'inputmode="numeric"')

    def test_leading_zeroes_cannot_bypass_branch_duplicate_check(self):
        first, second = self.verified_visit(), self.verified_visit()
        Invoice.objects.create(visit=first, invoice_number='INV/26-27/977', entered_by=self.manager)
        self.client.force_login(self.manager)
        response = self.client.post(reverse('add_invoice', args=[second.pk]), self.invoice_payload('000977'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This invoice number is already in use in this branch.')
        self.assertFalse(Invoice.objects.filter(visit=second).exists())

    def test_new_invoice_saves_canonical_number_and_authoritative_branch(self):
        current = self.verified_visit()
        self.client.force_login(self.manager)
        response = self.client.post(reverse('add_invoice', args=[current.pk]), {
            **self.invoice_payload('000977'), 'branch': '999999',
        })
        self.assertRedirects(response, reverse('manager_dashboard'))
        invoice = Invoice.objects.get(visit=current)
        self.assertEqual(invoice.invoice_number, 'INV/26-27/977')
        self.assertEqual(invoice.branch_id, current.branch_id)

    def test_model_form_reports_same_branch_duplicate_for_admin(self):
        first, second = self.verified_visit(), self.verified_visit()
        Invoice.objects.create(visit=first, invoice_number='INV/26-27/977', entered_by=self.manager)
        form = InvoiceForm({'invoice_number': 'INV/26-27/977'}, instance=Invoice(visit=second, entered_by=self.manager))
        self.assertFalse(form.is_valid())
        self.assertIn('invoice_number', form.errors)

    def test_manager_cannot_invoice_a_different_branch_visit(self):
        current = self.verified_visit()
        current.branch = Branch.objects.create(code='REVIEW-B', name='Synthetic B')
        current.save()
        self.client.force_login(self.manager)
        response = self.client.post(reverse('add_invoice', args=[current.pk]), self.invoice_payload('977'))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Invoice.objects.filter(visit=current).exists())


class InvoiceBranchMigrationTests(TransactionTestCase):
    def test_existing_invoice_gets_visit_branch_without_rewriting_history(self):
        before = ('operations', '0011_branchcsvimport')
        after = ('operations', '0012_invoice_branch_number')
        executor = MigrationExecutor(connection)
        current_schema = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([before])
            apps = executor.loader.project_state([before]).apps
            actor = apps.get_model('auth', 'User').objects.create(username='migration-actor')
            branch = apps.get_model('operations', 'Branch').objects.create(code='MIGRATION', name='Synthetic branch')
            customer = apps.get_model('operations', 'Customer').objects.create(name='Synthetic customer')
            visit = apps.get_model('operations', 'Visit').objects.create(
                branch=branch, customer=customer, created_by=actor, status='INVOICED',
            )
            legacy = apps.get_model('operations', 'Invoice').objects.create(
                visit=visit, entered_by=actor, invoice_number='LEGACY-KEEP', amount=123,
            )
            executor = MigrationExecutor(connection)
            executor.migrate([after])
            apps = executor.loader.project_state([after]).apps
            invoice = apps.get_model('operations', 'Invoice').objects.get(pk=legacy.pk)
            self.assertEqual(invoice.branch_id, branch.pk)
            self.assertEqual(invoice.invoice_number, 'LEGACY-KEEP')
            self.assertEqual(invoice.amount, 123)
            self.assertEqual(invoice.visit_id, visit.pk)
        finally:
            MigrationExecutor(connection).migrate(current_schema)
