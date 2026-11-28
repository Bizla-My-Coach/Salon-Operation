"""Save an uploaded sales file and match each bill with the invoices entered in Hairship."""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .customer_types import simple_flow_enabled
from .daily_reports import DailyReportProblem
from .models import Invoice, InvoiceSettings, SalesInvoice, SalesInvoiceLine, SalesReportUpload, Visit


def phone_key(value):
    """Compare phones by their last 10 digits: +91 98470 12345 and 9847012345 are the same."""
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ''


def simple_visits(branch, days):
    """Simple-flow visits (staff cards, no bill) of a branch on the given days."""
    return (Visit.objects.filter(branch=branch, created_at__date__in=days, category_assignments__isnull=False)
            .exclude(status='CANCELLED').select_related('customer').prefetch_related('guests').distinct()
            .order_by('created_at', 'pk'))


def match_by_phone(rows):
    """Simple visit flow: a bill matches the visit with the same customer phone on the same day."""
    branch_id = rows[0].branch_id
    visits = defaultdict(list)
    for visit in simple_visits(branch_id, {row.sale_date for row in rows}):
        key = phone_key(visit.customer.mobile)
        if key:
            visits[(timezone.localtime(visit.created_at).date(), key)].append(visit)
    for row in rows:
        key = phone_key(row.phone)
        found = visits.get((row.sale_date, key), []) if key else []
        row.app_invoice, row.app_amount = None, None
        row.app_visit = found[0] if found else None
        row.app_visits = len(found)
        row.match_status = 'NO_PHONE' if not key else ('MATCHED' if found else 'NOT_IN_APP')
    SalesInvoice.objects.bulk_update(rows, ['match_status', 'app_invoice', 'app_amount', 'app_visit'])
    return rows


def match_invoices(rows, settings=None):
    """Set match_status/app_invoice/app_amount on SalesInvoice rows of one branch and save them."""
    rows = list(rows)
    if not rows:
        return rows
    if simple_flow_enabled():
        return match_by_phone(rows)
    settings = settings or InvoiceSettings.current()
    numbers = {row.invoice_number for row in rows}
    app = defaultdict(list)
    # Visits sharing one bill are entered as 977, 977/2, ...; they all count towards bill 977.
    for invoice in Invoice.objects.filter(branch_id=rows[0].branch_id, status='COMPLETED').order_by('invoice_number', 'pk'):
        number, fits = settings.shared_bill(invoice.invoice_number)
        if fits and number in numbers:
            app[number].append(invoice)
    for row in rows:
        _, fits = settings.canonical(row.raw_number)
        visits = app.get(row.invoice_number, []) if fits else []
        invoice = next((i for i in visits if '/' not in i.invoice_number[len(settings.invoice_prefix):]),
                       visits[0] if visits else None)
        row.app_invoice = invoice
        row.app_visits = len(visits)
        row.app_amount = sum((i.amount for i in visits), Decimal('0')) if visits else None
        if not fits:
            row.match_status = 'FORMAT'
        elif invoice is None:
            row.match_status = 'NOT_IN_APP'
        else:
            # The app stores the price-list total, so compare with the amount before discount and tax.
            excel = row.sub_total if row.sub_total is not None else row.gross_total
            row.match_status = 'MATCHED' if abs(excel - row.app_amount) <= settings.amount_tolerance else 'AMOUNT_DIFF'
    SalesInvoice.objects.bulk_update(rows, ['match_status', 'app_invoice', 'app_amount'])
    return rows


def existing_days(branch, parsed):
    days = {bill['sale_date'] for bill in parsed['invoices']}
    return sorted(set(SalesInvoice.objects.filter(branch=branch, sale_date__in=days)
                      .values_list('sale_date', flat=True)))


@transaction.atomic
def save_sales_upload(branch, user, file_name, parsed):
    """Replace the branch's bills for every day in the file, then match them. Returns the upload."""
    settings = InvoiceSettings.current()
    bills = parsed['invoices']
    for bill in bills:
        bill['invoice_number'] = settings.canonical(bill['raw_number'])[0][:60]
        if bill['sold_at'] is not None and timezone.is_naive(bill['sold_at']):
            bill['sold_at'] = timezone.make_aware(bill['sold_at'])
    days = {bill['sale_date'] for bill in bills}
    numbers = {bill['invoice_number'] for bill in bills}
    if len(numbers) != len(bills):
        raise DailyReportProblem('Two bills in the file have the same number once the invoice format is applied.')
    SalesInvoice.objects.filter(branch=branch, sale_date__in=days).delete()
    SalesInvoice.objects.filter(branch=branch, invoice_number__in=numbers).delete()
    upload = SalesReportUpload.objects.create(
        branch=branch, file_name=file_name[:255], kind=parsed['kind'], uploaded_by=user,
        first_date=min(days), last_date=max(days), invoice_count=len(bills),
        total_amount=sum((bill['gross_total'] for bill in bills), Decimal('0')),
        payment_columns=parsed['payment_columns'],
    )
    rows = SalesInvoice.objects.bulk_create(
        SalesInvoice(upload=upload, branch=branch, **{k: v for k, v in bill.items() if k != 'lines'})
        for bill in bills
    )
    SalesInvoiceLine.objects.bulk_create(
        SalesInvoiceLine(invoice=row, **line) for row, bill in zip(rows, bills) for line in bill['lines']
    )
    match_invoices(rows, settings)
    counts = {status: 0 for status, _ in SalesInvoice.MATCH}
    for row in rows:
        counts[row.match_status] += 1
    upload.matched_count = counts['MATCHED']
    upload.not_in_app_count = counts['NOT_IN_APP']
    upload.amount_diff_count = counts['AMOUNT_DIFF']
    upload.format_count = counts['FORMAT']
    upload.no_phone_count = counts['NO_PHONE']
    upload.save(update_fields=['matched_count', 'not_in_app_count', 'amount_diff_count', 'format_count', 'no_phone_count'])
    return upload
