import io
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from .daily_reports import DailyReportProblem, parse_sales_workbook
from .forms import CompleteInvoiceForm
from .models import (
    Branch, Customer, Invoice, InvoiceSettings, SalesInvoice, SalesReportUpload, Visit,
)

BILL_HEADERS = ['Sr No', 'Invoice #', 'Customer', 'Customer No.', 'Gender', 'Date', 'Sale Type', 'UPI/QR', 'Cash',
                'Sub Total', 'Discount Amount', 'Net Total', 'Tax Amount', 'Pending Amount', 'Gross Total']
ITEM_HEADERS = ['Ref#', 'Customer Name', 'Customer No.', 'Offering Type', 'Offering Name', 'Offering Quantity',
                'Transaction Date', 'Staff', 'Gender', 'Sale Type', 'Offering Amount', 'Discount Amount',
                'Net Amount', 'Tax Amount', 'Gross Amount', 'Staff Amount', 'Staff Percentage', 'Duration(Min)']


def workbook(headers, rows, name='sales.xlsx'):
    book = Workbook()
    book.active.append(list(headers))
    for row in rows:
        book.active.append(list(row))
    book.active.append([None] * len(headers))
    book.active.append(['Total'] + [None] * (len(headers) - 1))
    out = io.BytesIO()
    book.save(out)
    return SimpleUploadedFile(name, out.getvalue())


def bill_rows(day='10/04/2026', wallet=False):
    rows = [
        [1, 'INV/26-27/12', 'Test Customer A', '9000000001', 'Male', f'{day} 06:10 PM', 'SALE', 300, 0, 300, 0, 300, 15, 0, 315],
        [2, 'INV/26-27/11', 'Test Customer B', '9000000002', 'Female', f'{day} 05:00 PM', 'SALE', 0, 500, 500, 10, 490, 24.5, 0, 515],
        [3, 'INV/25-26/990', 'Test Customer C', '', 'Female', f'{day} 11:00 AM', 'SALE', 100, 0, 100, 0, 100, 5, 5, 105],
    ]
    if wallet:
        for row, paid in zip(rows, (15, 15, 0)):
            row.insert(9, paid)
    return rows


class SalesWorkbookParserTests(TestCase):
    def read(self, upload, **kwargs):
        return parse_sales_workbook(upload.read(), **kwargs)

    def test_bill_layout_with_and_without_wallet(self):
        plain = self.read(workbook(BILL_HEADERS, bill_rows()))
        self.assertEqual((plain['kind'], plain['payment_columns'], len(plain['invoices'])), ('INVOICE', ['UPI', 'Cash'], 3))
        self.assertEqual(plain['invoices'][0]['payments'], {'UPI': '300.00'})
        self.assertEqual(plain['invoices'][0]['gross_total'], 315)
        self.assertEqual(plain['invoices'][0]['sale_date'], date(2026, 4, 10))
        headers = BILL_HEADERS[:9] + ['Wallet'] + BILL_HEADERS[9:]
        walleted = self.read(workbook(headers, bill_rows(wallet=True)))
        self.assertEqual(walleted['payment_columns'], ['UPI', 'Cash', 'Wallet'])
        self.assertEqual(walleted['invoices'][0]['payments'], {'UPI': '300.00', 'Wallet': '15.00'})
        self.assertEqual(walleted['invoices'][2]['payments'], {'UPI': '100.00'})
        self.assertEqual(walleted['invoices'][0]['sub_total'], 300)

    def test_service_layout_adds_lines_into_one_bill(self):
        rows = [
            ['INV/26-27/5', 'Test Customer', '+91 9000000003', 'SERVICE', 'Hair Cut', 1, '02/04/2026 08:41 PM', 'Stylist One',
             'Female', 'Sale', 1200, 20.4, 1179.6, 58.98, 1239, 1179.6, '100.00', 15],
            ['INV/26-27/5', 'Test Customer', '+91 9000000003', 'SERVICE', 'Hair Spa', 1, '02/04/2026 08:41 PM', 'Stylist Two',
             'Female', 'Sale', 350, 5.95, 344.05, 17.2, 361, 344.05, '100.00', 30],
            ['INV/26-27/4', 'Other', '', 'SERVICE', 'Beard', 1, '01/04/2026 10:00 AM', 'Stylist One',
             'Male', 'Sale', 150, 0, 150, 7.5, 158, 150, '100.00', 15],
        ]
        parsed = self.read(workbook(ITEM_HEADERS, rows))
        self.assertEqual(parsed['kind'], 'ITEM')
        first = parsed['invoices'][0]
        self.assertEqual((first['gross_total'], first['sub_total'], len(first['lines'])), (1600, 1550, 2))
        self.assertEqual(first['staff'], 'Stylist One, Stylist Two')
        self.assertEqual(first['lines'][1]['duration_minutes'], 30)
        self.assertEqual(first['payments'], {})

    def test_unreadable_files_are_explained(self):
        with self.assertRaisesMessage(DailyReportProblem, 'bill number column'):
            self.read(workbook(['Customer', 'Amount'], [['A', 1]]))
        with self.assertRaisesMessage(DailyReportProblem, 'cannot read the date'):
            self.read(workbook(BILL_HEADERS, bill_rows(day='99/99/2026')))
        with self.assertRaisesMessage(DailyReportProblem, 'not a readable'):
            parse_sales_workbook(b'not a workbook')
        no_date = workbook(['Bill No', 'Amount'], [['7', 100]])
        self.assertEqual(self.read(no_date, fallback_date=date(2026, 4, 3))['invoices'][0]['sale_date'], date(2026, 4, 3))


class DailyReportViewTests(TestCase):
    def setUp(self):
        self.north = Branch.objects.create(code='N', name='North')
        self.south = Branch.objects.create(code='S', name='South')
        self.manager = self.person('manager', 'MANAGER', self.north)
        customer = Customer.objects.create(name='App Customer')
        for number, amount in (('INV/26-27/11', 500), ('INV/26-27/12', 250), ('INV/26-27/13', 900)):
            visit = Visit.objects.create(branch=self.north, customer=customer, status='CLOSED', created_by=self.manager)
            Invoice.objects.create(visit=visit, invoice_number=number, amount=amount, entered_by=self.manager)
        Invoice.objects.update(created_at=timezone.make_aware(timezone.datetime(2026, 4, 10, 12)))

    def person(self, username, role, branch=None):
        user = User.objects.create_user(username, password='pw')
        user.profile.role = role
        user.profile.branch = branch
        user.profile.save()
        return user

    def upload(self, file, **extra):
        return self.client.post(reverse('daily_report'), {'date': '2026-04-10', 'report_file': file, **extra})

    def test_manager_upload_matches_app_invoices_for_own_branch(self):
        self.client.force_login(self.manager)
        response = self.upload(workbook(BILL_HEADERS, bill_rows()), branch=self.south.pk)
        self.assertRedirects(response, reverse('daily_report') + '?date=2026-04-10', fetch_redirect_response=False)
        status = dict(SalesInvoice.objects.values_list('invoice_number', 'match_status'))
        self.assertEqual(status, {'INV/26-27/11': 'MATCHED', 'INV/26-27/12': 'AMOUNT_DIFF', 'INV/25-26/990': 'FORMAT'})
        self.assertTrue(all(row.branch == self.north for row in SalesInvoice.objects.all()))
        upload = SalesReportUpload.objects.get()
        self.assertEqual((upload.matched_count, upload.amount_diff_count, upload.format_count), (1, 1, 1))
        page = self.client.get(reverse('daily_report'), {'date': '2026-04-10'})
        self.assertContains(page, '₹935')
        self.assertEqual([i.invoice_number for i in page.context['app_only']], ['INV/26-27/13'])
        self.assertEqual(page.context['by_payment'][0]['name'], 'Cash')
        issues = self.client.get(reverse('daily_report'), {'date': '2026-04-10', 'show': 'FORMAT'})
        self.assertEqual([i.raw_number for i in issues.context['invoices']], ['INV/25-26/990'])

    def test_matching_follows_the_admin_invoice_format_and_tolerance(self):
        settings = InvoiceSettings.current()
        settings.amount_tolerance = 10
        settings.save()
        self.client.force_login(self.manager)
        self.upload(workbook(BILL_HEADERS, bill_rows()))
        self.assertEqual(SalesInvoice.objects.get(invoice_number='INV/26-27/12').match_status, 'AMOUNT_DIFF')
        settings.amount_tolerance = 60
        settings.save()
        page = self.client.get(reverse('daily_report'), {'date': '2026-04-10'})
        self.assertEqual(page.context['counts']['MATCHED'], 2)
        settings.invoice_prefix = 'BILL-'
        settings.save()
        self.assertEqual(settings.canonical('bill-0042'), ('BILL-42', True))
        self.assertEqual(settings.canonical('42'), ('BILL-42', True))
        self.assertEqual(settings.canonical('INV/26-27/42')[1], False)

    def test_existing_days_need_replace(self):
        self.client.force_login(self.manager)
        self.upload(workbook(BILL_HEADERS, bill_rows()))
        rows = bill_rows()
        rows[0][-1] = 999
        self.upload(workbook(BILL_HEADERS, rows))
        self.assertEqual(SalesInvoice.objects.get(invoice_number='INV/26-27/12').gross_total, 315)
        self.upload(workbook(BILL_HEADERS, rows), replace='yes')
        self.assertEqual(SalesInvoice.objects.get(invoice_number='INV/26-27/12').gross_total, 999)
        self.assertEqual(SalesInvoice.objects.count(), 3)

    def test_file_named_for_another_branch_and_future_dates_are_refused(self):
        self.client.force_login(self.manager)
        self.upload(workbook(BILL_HEADERS, bill_rows(), name='2026-04_South.xlsx'))
        future = (timezone.localdate() + timedelta(days=2)).strftime('%d/%m/%Y')
        self.upload(workbook(BILL_HEADERS, bill_rows(day=future)))
        self.assertFalse(SalesInvoice.objects.exists())

    def test_admin_picks_branch_and_employee_is_refused(self):
        self.client.force_login(self.person('boss', 'ADMIN'))
        self.upload(workbook(BILL_HEADERS, bill_rows(), name='2026-04_South.xlsx'), branch=self.south.pk)
        self.assertEqual(set(SalesInvoice.objects.values_list('match_status', flat=True)), {'NOT_IN_APP', 'FORMAT'})
        self.assertEqual(self.client.get(reverse('daily_report_template')).status_code, 200)
        self.client.force_login(self.person('stylist', 'EMPLOYEE', self.north))
        self.assertEqual(self.client.get(reverse('daily_report')).status_code, 403)
        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('daily_report'), {'date': future}).status_code, 400)


class InvoiceSettingsTests(TestCase):
    def test_only_admin_changes_format_and_bill_entry_uses_it(self):
        branch = Branch.objects.create(code='N', name='North')
        manager = User.objects.create_user('manager', password='pw')
        manager.profile.role, manager.profile.branch = 'MANAGER', branch
        manager.profile.save()
        self.client.force_login(manager)
        self.assertEqual(self.client.get(reverse('invoice_settings')).status_code, 403)
        admin = User.objects.create_user('boss', password='pw')
        admin.profile.role = 'ADMIN'
        admin.profile.save()
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse('invoice_settings')), 'INV/26-27/977')
        self.client.post(reverse('invoice_settings'), {'invoice_prefix': 'INV/27-28/', 'amount_tolerance': '2'})
        self.assertEqual(InvoiceSettings.current().invoice_prefix, 'INV/27-28/')
        self.client.post(reverse('invoice_settings'), {'invoice_prefix': 'INV2', 'amount_tolerance': '2'})
        self.assertEqual(InvoiceSettings.current().invoice_prefix, 'INV/27-28/')
        form = CompleteInvoiceForm({'customer_name': 'A', 'mobile': '9000000000', 'invoice_number': '0977'}, branch=branch)
        form.is_valid()
        self.assertEqual(form.cleaned_data.get('invoice_number'), 'INV/27-28/977')


class SharedInvoiceTests(TestCase):
    """Two visits paid on one bill: the second visit is entered as 977/2."""

    def setUp(self):
        self.north = Branch.objects.create(code='N', name='North')
        self.manager = User.objects.create_user('manager', password='pw')
        self.manager.profile.role, self.manager.profile.branch = 'MANAGER', self.north
        self.manager.profile.save()
        self.customer = Customer.objects.create(name='Shared Customer')

    def visit(self, number=None, amount=500):
        visit = Visit.objects.create(branch=self.north, customer=self.customer, status='CLOSED', created_by=self.manager)
        if number:
            Invoice.objects.create(visit=visit, invoice_number=number, amount=amount, entered_by=self.manager)
        return visit

    def form(self, number, invoice=None):
        form = CompleteInvoiceForm({'customer_name': 'A', 'mobile': '', 'invoice_number': number},
                                   branch=self.north, invoice=invoice)
        form.is_valid()
        return form

    def test_second_visit_can_share_a_bill_with_slash_number(self):
        self.visit('INV/26-27/977')
        self.assertIn('977/2', self.form('977').errors['invoice_number'][0])
        self.assertEqual(self.form('977/2').cleaned_data['invoice_number'], 'INV/26-27/977/2')
        self.assertEqual(self.form('0977/02').cleaned_data['invoice_number'], 'INV/26-27/977/2')
        self.visit('INV/26-27/977/2')
        self.assertIn('977/3', self.form('977/2').errors['invoice_number'][0])
        self.assertEqual(self.form('977/3').cleaned_data['invoice_number'], 'INV/26-27/977/3')

    def test_slash_number_needs_the_first_visit_and_a_part_of_two_or_more(self):
        self.assertIn('Enter 978 on the first visit', self.form('978/2').errors['invoice_number'][0])
        self.visit('INV/26-27/978')
        self.assertIn('/2 or higher', self.form('978/1').errors['invoice_number'][0])
        self.assertIn('invoice_number', self.form('978/2/3').errors)
        self.assertIn('invoice_number', self.form('978-2').errors)

    def test_matching_adds_up_every_visit_on_the_shared_bill(self):
        self.visit('INV/26-27/11', amount=500)
        self.visit('INV/26-27/11/2', amount=300)
        Invoice.objects.update(created_at=timezone.make_aware(timezone.datetime(2026, 4, 10, 12)))
        self.client.force_login(self.manager)
        rows = [[1, 'INV/26-27/11', 'Shared Customer', '', 'Female', '10/04/2026 05:00 PM', 'SALE',
                 0, 800, 800, 0, 800, 40, 0, 840]]
        self.client.post(reverse('daily_report'), {'date': '2026-04-10', 'report_file': workbook(BILL_HEADERS, rows)})
        row = SalesInvoice.objects.get()
        self.assertEqual((row.match_status, row.app_amount, row.app_invoice.invoice_number),
                         ('MATCHED', 800, 'INV/26-27/11'))
        page = self.client.get(reverse('daily_report'), {'date': '2026-04-10'})
        self.assertEqual(page.context['app_only'], [])
        self.assertContains(page, '2 visits on this bill')
