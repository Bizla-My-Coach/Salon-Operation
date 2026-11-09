"""Rename service codes in place from an old code → new code sheet.

Visits, SOP steps, versions, inventory and reports all link to a service by
its database id, so changing ``Service.code`` on the existing record keeps
everything attached. Renaming through the SOP workbook would instead create new
services, which is why this is a separate, previewed, all-or-nothing action.
"""
import csv
import hashlib
import io
import json
from zipfile import BadZipFile

from django import forms
from django.contrib import messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core import signing
from django.db import DatabaseError, IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from .decorators import roles_required
from .models import Service
from .service_create import CODE_PATTERN

MAX_UPLOAD = 1024 * 1024
MAX_ROWS = 5000
SIGNING_SALT = "service-code-rename"


class RenameProblem(Exception):
    pass


def _clean(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def read_pairs(name, payload):
    """Return [(row number, old, new)] from an .xlsx or .csv with old_code and new_code columns."""
    if not payload or len(payload) > MAX_UPLOAD:
        raise RenameProblem("Choose an .xlsx or .csv file no larger than 1 MB.")
    if name.lower().endswith(".csv"):
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise RenameProblem("Save the CSV as UTF-8 and try again.") from exc
        rows = list(csv.reader(io.StringIO(text)))
    else:
        try:
            book = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        except (BadZipFile, ValueError, KeyError, OSError) as exc:
            raise RenameProblem("This is not a readable .xlsx file.") from exc
        try:
            sheet = book.worksheets[0]
            sheet.reset_dimensions()
            rows = []
            for row in sheet.iter_rows(values_only=True):
                rows.append(list(row))
                if len(rows) > MAX_ROWS + 1:
                    raise RenameProblem(f"Use at most {MAX_ROWS} rows.")
        finally:
            book.close()
    if not rows:
        raise RenameProblem("The file is empty.")
    headers = [_clean(value).lower() for value in rows[0]]
    if "old_code" not in headers or "new_code" not in headers:
        raise RenameProblem("The first row needs the column names old_code and new_code.")
    old_at, new_at = headers.index("old_code"), headers.index("new_code")
    pairs = []
    for number, row in enumerate(rows[1:], 2):
        old = _clean(row[old_at]) if old_at < len(row) else ""
        new = _clean(row[new_at]) if new_at < len(row) else ""
        if old or new:
            pairs.append((number, old, new.upper()))
    if len(pairs) > MAX_ROWS:
        raise RenameProblem(f"Use at most {MAX_ROWS} rows.")
    return pairs


def plan_renames(pairs):
    """Check every row against the live catalogue; return (renames, unchanged count, problems)."""
    services = {item.code: item for item in Service.objects.all()}
    lower = {code.lower(): code for code in services}
    problems, renames, seen_old, seen_new, unchanged = [], [], {}, {}, 0
    for number, old, new in pairs:
        if not old or not new:
            problems.append(f"Row {number}: fill in both old_code and new_code.")
            continue
        current = lower.get(old.lower())
        if current is None:
            problems.append(f"Row {number}: no service has the code {old}.")
            continue
        if current in seen_old:
            problems.append(f"Row {number}: {current} is already renamed on row {seen_old[current]}.")
            continue
        seen_old[current] = number
        if len(new) > 30 or not CODE_PATTERN.match(new):
            problems.append(f"Row {number}: {new} is not a valid code. Use up to 30 letters, numbers, - and _.")
            continue
        if new.lower() in seen_new:
            problems.append(f"Row {number}: {new} is also the new code on row {seen_new[new.lower()]}.")
            continue
        seen_new[new.lower()] = number
        if new == current:
            unchanged += 1
            continue
        renames.append((current, new))
    # A new code may only be taken by a service that is itself being renamed away.
    moving = {old.lower() for old, _ in renames}
    for old, new in renames:
        holder = lower.get(new.lower())
        if holder is not None and holder.lower() not in moving and holder != old:
            problems.append(f"{new} is already used by {services[holder].name}, which is not being renamed.")
    rows = [{"old": old, "new": new, "name": services[old].name, "category": services[old].category,
             "active": services[old].active} for old, new in renames]
    return rows, unchanged, problems


def codes_digest():
    payload = json.dumps(sorted(Service.objects.values_list("pk", "code")))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@transaction.atomic
def apply_renames(renames, user):
    """Rename every code in one transaction, parking codes first so swaps cannot clash."""
    services = {item.code: item for item in Service.objects.select_for_update().filter(code__in=[o for o, _ in renames])}
    if len(services) != len(renames):
        raise RenameProblem("Some services changed since the preview. Upload the sheet again.")
    for service in services.values():
        service.code = f"~RENAME-{service.pk}"[:30]
        service.save(update_fields=["code", "updated_at"])
    content_type = ContentType.objects.get_for_model(Service)
    for old, new in renames:
        service = services[old]
        service.code = new
        service.save(update_fields=["code", "updated_at"])
        LogEntry.objects.create(
            user=user, content_type=content_type, object_id=str(service.pk), object_repr=str(service)[:200],
            action_flag=CHANGE, change_message=f"Service code renamed from {old} to {new}",
        )
    return len(renames)


class RenameUploadForm(forms.Form):
    sheet = forms.FileField(label="Code sheet (.xlsx or .csv)", widget=forms.ClearableFileInput(attrs={
        "accept": ".xlsx,.csv", "class": "form-control"}))

    def clean_sheet(self):
        upload = self.cleaned_data["sheet"]
        if not upload.name.lower().endswith((".xlsx", ".csv")) or upload.size > MAX_UPLOAD:
            raise forms.ValidationError("Choose an .xlsx or .csv file no larger than 1 MB.")
        return upload


@roles_required("ADMIN")
@never_cache
@require_http_methods(["GET", "POST"])
def service_code_rename(request):
    form, preview = RenameUploadForm(), None
    if request.method == "POST" and request.POST.get("action") == "confirm":
        try:
            data = signing.loads(request.POST.get("plan", ""), salt=SIGNING_SALT, max_age=3600)
            if data["user"] != request.user.pk:
                raise signing.BadSignature("other user")
            if data["digest"] != codes_digest():
                raise RenameProblem("Service codes changed since the preview. Upload the sheet again.")
            renames = [tuple(pair) for pair in data["renames"]]
            # Re-check against the live catalogue in case anything moved.
            rows, _, problems = plan_renames([(0, old, new) for old, new in renames])
            if problems:
                raise RenameProblem(problems[0])
            count = apply_renames(renames, request.user)
        except signing.BadSignature:
            messages.error(request, "This preview expired. Upload the sheet again.")
        except RenameProblem as exc:
            messages.error(request, str(exc))
        except (IntegrityError, DatabaseError):
            messages.error(request, "Codes could not be changed and nothing was saved. Try again shortly.")
        else:
            messages.success(request, f"Renamed {count} service code{'s' if count != 1 else ''}. "
                             "Download a fresh SOP workbook before your next upload.")
        return redirect("service_code_rename")
    if request.method == "POST":
        form = RenameUploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.cleaned_data["sheet"]
            try:
                pairs = read_pairs(upload.name, upload.read(MAX_UPLOAD + 1))
                rows, unchanged, problems = plan_renames(pairs)
            except RenameProblem as exc:
                form.add_error("sheet", str(exc))
            else:
                plan = signing.dumps({"user": request.user.pk, "digest": codes_digest(),
                                      "renames": [[row["old"], row["new"]] for row in rows]}, salt=SIGNING_SALT)
                preview = {"rows": rows, "unchanged": unchanged, "problems": problems, "plan": plan,
                           "filename": upload.name}
    return render(request, "operations/service_code_rename.html", {
        "form": form, "preview": preview, "service_count": Service.objects.count(),
    })


@roles_required("ADMIN")
@never_cache
@require_GET
def service_code_sheet(request):
    """Every current code with a blank new_code column to fill in."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Service codes"
    sheet.append(["old_code", "new_code", "service_name (info)", "service_category (info)", "active (info)"])
    for service in Service.objects.order_by("category", "name"):
        sheet.append([service.code, service.code, service.name, service.category, service.active])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E79")
    for column, width in zip("ABCDE", (14, 14, 52, 26, 12)):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"
    output = io.BytesIO()
    book.save(output)
    response = HttpResponse(output.getvalue(),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="hairship-service-codes-{timezone.localdate():%Y-%m-%d}.xlsx"'
    return response
