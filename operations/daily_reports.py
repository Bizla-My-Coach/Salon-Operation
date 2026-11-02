"""Read a branch's sales Excel export from the billing software.

Two layouts are understood, both with column names in the first row:

* one row per bill ("Sale Report"): Invoice #, Customer, Date, payment columns
  such as UPI/QR, Cash, Card and Wallet (any of them may be missing), Sub Total,
  Discount, Net Total, Tax, Pending and Gross Total;
* one row per service ("Sale Staff Report"): Ref#, Offering Name, Staff and
  per-line amounts. Lines with the same bill number are added up into one bill.

Each bill keeps its own date from the file, so one upload can cover a single
day or a whole month. Files without a date column use the day picked in the app.
"""
import io
import re
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zipfile import BadZipFile, ZipFile

from openpyxl import Workbook, load_workbook

MAX_DAILY_UPLOAD = 5 * 1024 * 1024
MAX_EXPANDED = 80 * 1024 * 1024
MAX_ROWS = 20000
HEADER_SEARCH_ROWS = 10

# Field -> accepted column names after _header() clean-up.
COLUMNS = {
    'invoice': ('ref no', 'invoice no', 'invoice number', 'bill no', 'bill number', 'invoice', 'bill',
                'receipt no', 'invoice id', 'bill id'),
    'customer': ('customer', 'customer name', 'client', 'client name', 'guest name'),
    'phone': ('customer no', 'phone', 'mobile', 'mobile no', 'phone number', 'contact', 'contact no'),
    'date': ('transaction date', 'date', 'invoice date', 'bill date', 'sale date', 'date time'),
    'sale_type': ('sale type',),
    'item_type': ('offering type', 'item type'),
    'item_name': ('offering name', 'service', 'service name', 'item', 'item name', 'services'),
    'quantity': ('offering quantity', 'quantity', 'qty'),
    'staff': ('staff', 'stylist', 'employee', 'staff name'),
    'sub_total': ('sub total', 'subtotal', 'offering amount', 'item amount'),
    'discount': ('discount amount', 'discount'),
    'net_total': ('net total', 'net amount'),
    'tax': ('tax amount', 'tax', 'gst', 'gst amount'),
    'gross_total': ('gross total', 'gross amount', 'grand total', 'total amount', 'bill amount', 'total', 'amount'),
    'pending': ('pending amount', 'pending', 'due amount', 'balance'),
    'duration': ('duration(min)', 'duration (min)', 'duration', 'duration min'),
}
# Payment columns are optional and any mix may appear (Wallet only in some exports).
PAYMENTS = OrderedDict([
    ('upi/qr', 'UPI'), ('upi', 'UPI'), ('qr', 'UPI'), ('gpay', 'UPI'),
    ('cash', 'Cash'), ('card', 'Card'), ('credit card', 'Card'), ('debit card', 'Card'),
    ('wallet', 'Wallet'), ('online', 'Online'), ('bank', 'Bank'), ('bank transfer', 'Bank'),
    ('cheque', 'Cheque'), ('gift card', 'Gift card'), ('voucher', 'Voucher'),
])
TEMPLATE_HEADERS = ['Invoice #', 'Customer', 'Customer No.', 'Date', 'UPI/QR', 'Cash', 'Card', 'Wallet',
                    'Sub Total', 'Discount Amount', 'Net Total', 'Tax Amount', 'Pending Amount', 'Gross Total']
TEXT_LIMITS = {'customer': 150, 'phone': 30, 'sale_type': 30, 'item_type': 40, 'item_name': 200, 'staff': 120}
SUMMARY_WORDS = {'total', 'grand total', 'sub total', 'subtotal', 'totals'}
DATE_FORMATS = ('%d/%m/%Y %I:%M %p', '%d/%m/%Y %H:%M', '%d/%m/%Y', '%d-%m-%Y %I:%M %p', '%d-%m-%Y %H:%M',
                '%d-%m-%Y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d', '%d %b %Y', '%d.%m.%Y')


class DailyReportProblem(Exception):
    pass


def _header(value):
    text = str(value or '').strip().lower().replace('#', ' no').replace('.', ' ').replace('_', ' ')
    return ' '.join(text.split())


def _text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return ' '.join(str(value).split())


def _money(value, row, column):
    if value in (None, ''):
        return None
    cleaned = re.sub(r'(₹|rs\.?|inr|,|\s)', '', str(value), flags=re.I)
    try:
        amount = Decimal(cleaned).quantize(Decimal('0.01'))
    except InvalidOperation as exc:
        raise DailyReportProblem(f'Row {row}: "{value}" in {column} is not a number.') from exc
    if abs(amount) >= Decimal('100000000'):
        raise DailyReportProblem(f'Row {row}: {column} {value} is too large.')
    return amount


def _when(value, row):
    """Return (date, datetime-or-None) for a cell, or raise for unreadable dates."""
    if isinstance(value, datetime):
        return value.date(), value
    if isinstance(value, date):
        return value, None
    text = _text(value)
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        has_time = '%H' in fmt or '%I' in fmt
        return parsed.date(), parsed if has_time else None
    raise DailyReportProblem(f'Row {row}: cannot read the date "{text}". Use day/month/year, for example 30/04/2026.')


def _open(payload):
    if not payload or len(payload) > MAX_DAILY_UPLOAD:
        raise DailyReportProblem('Choose an .xlsx file no larger than 5 MB.')
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if (len(entries) > 500 or sum(item.file_size for item in entries) > MAX_EXPANDED
                    or any(item.flag_bits & 1 for item in entries)):
                raise DailyReportProblem('This file is password-protected or too large to read safely.')
            if any(item.filename.lower().endswith('vbaproject.bin') for item in entries):
                raise DailyReportProblem('Macro-enabled files are not supported. Save it as .xlsx.')
        return load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    except (BadZipFile, ValueError, KeyError, OSError) as exc:
        raise DailyReportProblem('This is not a readable .xlsx file. In Excel use Save As → Excel Workbook (.xlsx).') from exc


def _columns(headers):
    found, used = {}, set()
    for field, names in COLUMNS.items():
        for name in names:  # Earlier names are preferred, e.g. "gross total" over "total".
            if name in headers and headers.index(name) not in used:
                found[field] = headers.index(name)
                used.add(found[field])
                break
    payments = OrderedDict()
    for index, header in enumerate(headers):
        if index not in used and header in PAYMENTS:
            payments[index] = PAYMENTS[header]
    return found, payments


def parse_sales_workbook(payload, fallback_date=None):
    """Return {'kind', 'invoices': [...], 'payment_columns': [...]}, or raise DailyReportProblem."""
    book = _open(payload)
    try:
        sheet = book.worksheets[0]
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        columns = payments = None
        number = 0
        for number, row in enumerate(rows, 1):
            headers = [_header(v) for v in row]
            columns, payments = _columns(headers)
            if 'invoice' in columns:
                break
            if number >= HEADER_SEARCH_ROWS:
                break
        if not columns or 'invoice' not in columns:
            raise DailyReportProblem('Could not find a bill number column such as "Invoice #", "Ref#" or "Bill No" in the first rows.')
        amount_field = next((f for f in ('gross_total', 'net_total', 'sub_total') if f in columns), None)
        if amount_field is None:
            raise DailyReportProblem('Could not find an amount column such as "Gross Total" or "Amount".')
        if 'date' not in columns and fallback_date is None:
            raise DailyReportProblem('The file has no date column. Pick the day before uploading.')
        kind = 'ITEM' if 'item_name' in columns else 'INVOICE'

        def cell(row, field):
            index = columns.get(field)
            return row[index] if index is not None and index < len(row) else None

        bills = OrderedDict()
        seen = 0
        for line_no, row in enumerate(rows, number + 1):
            if not any(v not in (None, '') for v in row):
                continue
            raw = _text(cell(row, 'invoice'))
            first = _text(row[0]).lower() if row else ''
            if first in SUMMARY_WORDS or raw.lower() in SUMMARY_WORDS:
                continue  # Billing exports add a totals row at the bottom.
            if not raw:
                raise DailyReportProblem(f'Row {line_no}: the bill number is empty.')
            seen += 1
            if seen > MAX_ROWS:
                raise DailyReportProblem(f'The file has more than {MAX_ROWS} rows. Split it into smaller files.')
            day, moment = _when(cell(row, 'date'), line_no) if 'date' in columns else (fallback_date, None)
            amounts = {f: _money(cell(row, f), line_no, f.replace('_', ' '))
                       for f in ('sub_total', 'discount', 'net_total', 'tax', 'gross_total', 'pending')}
            gross = amounts[amount_field] if amounts['gross_total'] is None else amounts['gross_total']
            text = {f: _text(cell(row, f))[:limit] for f, limit in TEXT_LIMITS.items()}
            bill = bills.get(raw)
            if bill is None:
                bill = bills[raw] = {
                    'raw_number': raw[:60], 'sale_date': day, 'sold_at': moment,
                    'customer_name': text['customer'], 'phone': text['phone'], 'sale_type': text['sale_type'],
                    'sub_total': None, 'discount': Decimal('0'), 'net_total': None, 'tax': Decimal('0'),
                    'gross_total': Decimal('0'), 'pending': Decimal('0'), 'payments': {}, 'staff': [], 'lines': [],
                }
            elif kind == 'INVOICE':
                raise DailyReportProblem(f'Row {line_no}: bill {raw} appears twice.')
            for field in ('sub_total', 'net_total'):
                if amounts[field] is not None:
                    bill[field] = (bill[field] or Decimal('0')) + amounts[field]
            for field in ('discount', 'tax', 'pending'):
                bill[field] += amounts[field] or Decimal('0')
            bill['gross_total'] += gross or Decimal('0')
            if kind == 'INVOICE' or not bill['payments']:
                for index, label in payments.items():
                    paid = _money(row[index] if index < len(row) else None, line_no, label)
                    if paid:
                        bill['payments'][label] = str(Decimal(bill['payments'].get(label, '0')) + paid)
            if text['staff'] and text['staff'] not in bill['staff']:
                bill['staff'].append(text['staff'])
            if kind == 'ITEM':
                quantity = _money(cell(row, 'quantity'), line_no, 'quantity')
                duration = _money(cell(row, 'duration'), line_no, 'duration')
                bill['lines'].append({
                    'row_number': line_no, 'item_type': text['item_type'], 'item_name': text['item_name'],
                    'quantity': quantity if quantity is not None else Decimal('1'), 'staff': text['staff'],
                    'sub_total': amounts['sub_total'], 'discount': amounts['discount'] or Decimal('0'),
                    'net_total': amounts['net_total'], 'gross_total': gross or Decimal('0'),
                    'duration_minutes': int(duration) if duration is not None and duration >= 0 else None,
                })
        if not bills:
            raise DailyReportProblem('No bills found under the header row.')
        for bill in bills.values():
            bill['staff'] = ', '.join(bill['staff'])[:255]
        return {'kind': kind, 'invoices': list(bills.values()),
                'payment_columns': list(OrderedDict.fromkeys(payments.values()))}
    finally:
        book.close()


def template_workbook():
    book = Workbook()
    sheet = book.active
    sheet.title = 'Sale Report'
    sheet.append(TEMPLATE_HEADERS)
    sheet.append(['INV/26-27/1001', 'Sample customer', '9800000000', '30/04/2026 11:15 AM',
                  450, 0, 0, 0, 450, 0, 450, 22.5, 0, 473])
    sheet.append(['INV/26-27/1002', 'Sample customer 2', '', '30/04/2026 12:40 PM',
                  0, 1000, 0, 260, 1200, 0, 1200, 60, 0, 1260])
    for column, width in zip('ABCDEFGHIJKLMN', (16, 22, 14, 20, 9, 9, 9, 9, 10, 10, 10, 10, 10, 11)):
        sheet.column_dimensions[column].width = width
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
