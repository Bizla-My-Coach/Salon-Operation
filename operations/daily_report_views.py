"""Daily report: upload the billing software's sales Excel and match it with app invoices.

Managers work on their own branch; admins and general managers pick a branch.
One upload may cover one day or a whole month: every bill keeps its own date.
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db.models import Count, Sum
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .daily_reports import MAX_DAILY_UPLOAD, DailyReportProblem, parse_sales_workbook, template_workbook
from .decorators import roles_required
from .forms import BootstrapMixin
from .models import Branch, Invoice, InvoiceSettings, SalesInvoice, SalesReportUpload
from .roster import working_branch
from .sales_matching import existing_days, match_invoices, save_sales_upload

HISTORY_DAYS = 14
FILTERS = [('', 'All'), ('NOT_IN_APP', 'Not in app'), ('AMOUNT_DIFF', 'Amount differs'),
           ('FORMAT', 'Wrong format'), ('MATCHED', 'Matched')]


def _see_all(user):
    return user.is_superuser or user.profile.role in ('ADMIN', 'GENERAL_MANAGER')


def _branch(request, value):
    """The branch this request may work on, or None when there is none."""
    if not _see_all(request.user):
        return working_branch(request.user) or request.user.profile.branch
    branches = Branch.objects.filter(active=True).order_by('name', 'pk')
    if value and str(value).isdigit():
        return branches.filter(pk=value).first()
    return branches.first()


def _day(value):
    today = timezone.localdate()
    if not value:
        return today
    try:
        chosen = date.fromisoformat(value)
    except ValueError:
        return None
    return chosen if chosen <= today else None


def _url(day, branch, see_all, show=''):
    query = f'?date={day.isoformat()}'
    if see_all and branch:
        query += f'&branch={branch.pk}'
    if show:
        query += f'&show={show}'
    return reverse('daily_report') + query


def _bars(groups):
    total = sum((g['amount'] for g in groups.values()), Decimal('0')) or Decimal('1')
    rows = [{'name': n, **g, 'width': round(g['amount'] * 100 / total)} for n, g in groups.items()]
    return sorted(rows, key=lambda r: (-r['amount'], r['name']))


def _payment_split(invoices):
    groups = defaultdict(lambda: {'bills': 0, 'amount': Decimal('0')})
    for invoice in invoices:
        for label, paid in invoice.payments.items():
            groups[label]['bills'] += 1
            groups[label]['amount'] += Decimal(paid)
        if invoice.pending:
            groups['Pending']['bills'] += 1
            groups['Pending']['amount'] += invoice.pending
    return _bars(groups)


def _staff_split(invoices):
    groups = defaultdict(lambda: {'bills': 0, 'amount': Decimal('0')})
    for invoice in invoices:
        lines = list(invoice.lines.all())
        if lines:
            for staff in {line.staff or 'Not given' for line in lines}:
                groups[staff]['bills'] += 1
            for line in lines:
                groups[line.staff or 'Not given']['amount'] += line.gross_total
        elif invoice.staff:
            groups[invoice.staff]['bills'] += 1
            groups[invoice.staff]['amount'] += invoice.gross_total
    return _bars(groups)


def _wrong_branch(branch, file_name):
    """Another branch's name in the file name (e.g. 2026-04_Koppam.xlsx) without this one's."""
    name = file_name.lower()
    if branch.name.lower() in name or branch.code.lower() in name:
        return None
    for other in Branch.objects.filter(active=True).exclude(pk=branch.pk):
        if len(other.name) >= 3 and other.name.lower() in name:
            return other
    return None


def _upload(request, branch, day, see_all):
    back = redirect(_url(day, branch, see_all))
    sheet = request.FILES.get('report_file')
    if not sheet or not sheet.name.lower().endswith('.xlsx') or sheet.size > MAX_DAILY_UPLOAD:
        messages.error(request, 'Choose an .xlsx file no larger than 5 MB.')
        return back
    other = _wrong_branch(branch, sheet.name)
    if other:
        messages.error(request, f'"{sheet.name}" looks like the {other.name} file, but you are uploading for '
                                f'{branch.name}. Choose the right branch or rename the file.')
        return back
    try:
        parsed = parse_sales_workbook(sheet.read(MAX_DAILY_UPLOAD + 1), fallback_date=day)
    except DailyReportProblem as problem:
        messages.error(request, str(problem))
        return back
    today = timezone.localdate()
    future = [bill['raw_number'] for bill in parsed['invoices'] if bill['sale_date'] > today]
    if future:
        messages.error(request, f'Bill {future[0]} has a future date. Check the dates in the file.')
        return back
    already = existing_days(branch, parsed)
    if already and request.POST.get('replace') != 'yes':
        messages.error(request, f'{len(already)} day{"s" if len(already) != 1 else ""} in this file already '
                                f'{"have" if len(already) != 1 else "has"} bills saved (first: {already[0]:%d %b %Y}). '
                                'Tick "Replace days already uploaded" and upload again.')
        return back
    try:
        upload = save_sales_upload(branch, request.user, sheet.name, parsed)
    except DailyReportProblem as problem:
        messages.error(request, str(problem))
        return back
    span = (f'{upload.first_date:%d %b}' if upload.first_date == upload.last_date
            else f'{upload.first_date:%d %b} – {upload.last_date:%d %b %Y}')
    messages.success(request, (
        f'Saved {upload.invoice_count} bills for {span}. Matched with app: {upload.matched_count}, '
        f'not in app: {upload.not_in_app_count}, amount differs: {upload.amount_diff_count}, '
        f'wrong number format: {upload.format_count}.'))
    return redirect(_url(upload.last_date, branch, see_all))


@roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER')
@require_http_methods(['GET', 'POST'])
def daily_report(request):
    data = request.POST if request.method == 'POST' else request.GET
    see_all = _see_all(request.user)
    day = _day(data.get('date', ''))
    if day is None:
        return HttpResponseBadRequest('Choose today or an earlier date.')
    branch = _branch(request, data.get('branch'))
    settings = InvoiceSettings.current()
    context = {'see_all': see_all, 'day': day, 'today': timezone.localdate(), 'settings': settings,
               'max_mb': MAX_DAILY_UPLOAD // (1024 * 1024)}
    if see_all:
        context['branches'] = Branch.objects.filter(active=True).order_by('name', 'pk')
    if branch is None:
        return render(request, 'operations/daily_report.html', {**context, 'no_branch': True})
    if request.method == 'POST':
        return _upload(request, branch, day, see_all)

    invoices = match_invoices(
        SalesInvoice.objects.filter(branch=branch, sale_date=day).prefetch_related('lines'), settings)
    show = data.get('show', '')
    if show not in dict(FILTERS):
        show = ''
    counts = defaultdict(int)
    for invoice in invoices:
        counts[invoice.match_status] += 1
        invoice.kind_lines = bool(invoice.lines.all()) or bool(invoice.staff)
    numbers = {invoice.invoice_number for invoice in invoices}
    app_only = [i for i in Invoice.objects.filter(branch=branch, status='COMPLETED', created_at__date=day)
                .select_related('visit__customer').order_by('invoice_number')
                if settings.canonical(i.invoice_number)[0] not in numbers]
    total = sum((i.gross_total for i in invoices), Decimal('0'))
    first = day - timedelta(days=HISTORY_DAYS - 1)
    saved = dict(SalesInvoice.objects.filter(branch=branch, sale_date__range=(first, day))
                 .values('sale_date').annotate(total=Sum('gross_total')).values_list('sale_date', 'total'))
    history = [{'day': d, 'amount': saved.get(d), 'url': _url(d, branch, see_all), 'current': d == day}
               for d in (first + timedelta(days=i) for i in range(HISTORY_DAYS))]
    context.update({
        'branch': branch, 'invoices': [i for i in invoices if not show or i.match_status == show],
        'all_count': len(invoices), 'total': total, 'average': total / len(invoices) if invoices else 0,
        'counts': counts, 'app_only': app_only, 'show': show,
        'filters': [{'key': k, 'label': l, 'url': _url(day, branch, see_all, k),
                     'count': len(invoices) if not k else counts.get(k, 0)} for k, l in FILTERS],
        'by_payment': _payment_split(invoices), 'by_staff': _staff_split(invoices),
        'has_payments': any(i.payments or i.pending for i in invoices),
        'has_lines': any(i.kind_lines for i in invoices),
        'uploads': SalesReportUpload.objects.filter(branch=branch).select_related('uploaded_by')[:5],
        'history': history, 'missing_days': sum(1 for h in history if h['amount'] is None),
        'previous_url': _url(day - timedelta(days=1), branch, see_all),
        'next_url': _url(day + timedelta(days=1), branch, see_all) if day < context['today'] else None,
    })
    context['columns'] = 5 + context['has_payments'] + context['has_lines']
    for upload in context['uploads']:
        upload.url = _url(upload.last_date, branch, see_all)
    return render(request, 'operations/daily_report.html', context)


@roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER')
@require_GET
def daily_report_template(request):
    response = HttpResponse(template_workbook(),
                            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="hairship-sales-report-template.xlsx"'
    return response


class InvoiceSettingsForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = InvoiceSettings
        fields = ['invoice_prefix', 'amount_tolerance']
        labels = {'invoice_prefix': 'Invoice number format (text before the number)',
                  'amount_tolerance': 'Allowed amount difference (₹)'}

    def clean_invoice_prefix(self):
        value = self.cleaned_data['invoice_prefix'].strip()
        if not value or value[-1].isdigit():
            raise forms.ValidationError('End the format with a letter or symbol, for example INV/26-27/.')
        return value


@roles_required('ADMIN')
@require_http_methods(['GET', 'POST'])
def invoice_settings(request):
    settings = InvoiceSettings.current()
    form = InvoiceSettingsForm(request.POST or None, instance=settings)
    if request.method == 'POST' and form.is_valid():
        saved = form.save(commit=False)
        saved.updated_by = request.user
        saved.save()
        messages.success(request, f'Saved. New bills are numbered {saved.invoice_prefix}977 and uploads are matched with this format.')
        return redirect('invoice_settings')
    stats = SalesInvoice.objects.values('match_status').annotate(total=Count('pk'))
    return render(request, 'operations/invoice_settings.html', {
        'form': form, 'settings': settings, 'stats': {row['match_status']: row['total'] for row in stats},
        'example': settings.invoice_prefix + '977',
    })
