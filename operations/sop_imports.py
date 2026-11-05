"""Bounded uploads using the same atomic importer as the CLI; no public media."""
import gzip
import hashlib
import io
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.core import serializers
from django.core.exceptions import ObjectDoesNotExist
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from openpyxl import load_workbook

from .models import (
    Equipment, InventoryItem, OperationalTask, Service, ServiceDetail, ServiceTaskSetting,
    SOPWorkbookImport, SubService, TaskEquipment, TaskInventory,
)
from .sop_exports import TASK_ORDER_SHEET, catalogue_reference, read_reference
from .sop_versions import all_flows, compare, record_changed_versions

MAX_UPLOAD = 10 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_ROWS = 10000
MASTER_MODELS = (Service, SubService, InventoryItem, Equipment, OperationalTask,
                 ServiceDetail, TaskInventory, TaskEquipment, ServiceTaskSetting)
TABLE_LABELS = {
    Service: "Services", SubService: "Sub-services", InventoryItem: "Inventory items", Equipment: "Equipment",
    OperationalTask: "Tasks", ServiceDetail: "Service steps", TaskInventory: "Task inventory",
    TaskEquipment: "Task equipment", ServiceTaskSetting: "Per-service task order and skips",
}
SERVICE_FIELDS = {
    "name": "Name", "category": "Category", "customer_type": "Customer type", "service_type": "Service type",
    "standard_duration_minutes": "Standard minutes", "base_price": "Base price",
}
MAX_PREVIEW_SERVICES = 300
# Optional sheet: when present it is the full list of per-service task settings.
TASK_ORDER_SCHEMA = "service_code task_id position can_skip"
# Required by import_sop_workbook. Extra columns are allowed; missing ones are not.
SCHEMA = {
    "Service Master": "service_code service_name service_category customer_type service_type standard_duration_minutes current_version notes",
    "Sub-Service Master": "sub_service_code sub_service_name category standard_output_quantity output_unit procedure_notes",
    "Inventory Master": "inventory_code inventory_name inventory_category source_method base_unit purchase_pack_quantity purchase_pack_unit resource_type notes",
    "Equipment Master": "equipment_code equipment_name equipment_category utility_type branch_code rated_power_kw water_litres_per_minute notes",
    "Task Master": "task_id sub_service_code task_sequence activity_code task_name role_code employee_role active_labour_minutes passive_time_minutes required_skill allowed_staff_type default_customer_type is_skippable notes",
    "Service Detail": "service_detail_id service_code sub_service_code sequence required_quantity mandatory mapping_status notes",
    "Task Inventory": "task_inventory_id task_id service_code inventory_code standard_quantity unit waste_percent effective_quantity mapping_status notes mapping_note",
    "Task Equipment": "task_equipment_id task_id equipment_code quantity_required equipment_usage_minutes utility_type utility_minutes notes",
}
INTEGER_COLUMNS = {"standard_duration_minutes", "task_sequence", "sequence", "active_labour_minutes",
                   "passive_time_minutes", "equipment_usage_minutes", "utility_minutes", "position"}
DECIMAL_COLUMNS = {"standard_output_quantity", "purchase_pack_quantity", "rated_power_kw",
                   "water_litres_per_minute", "required_quantity", "standard_quantity",
                   "waste_percent", "effective_quantity", "quantity_required", "base_price"}


class ImportProblem(Exception):
    pass


def validate_workbook(payload):
    if not payload or len(payload) > MAX_UPLOAD:
        raise ImportProblem("Choose an .xlsx workbook no larger than 10 MB.")
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if (len(entries) > 1000 or sum(item.file_size for item in entries) > MAX_EXPANDED
                    or any(item.flag_bits & 1 for item in entries)):
                raise ImportProblem("Workbook is encrypted or exceeds the safe workbook limits.")
            if any(item.filename.lower().endswith("vbaproject.bin") for item in entries):
                raise ImportProblem("Macro-enabled workbooks are not supported. Use .xlsx.")
        book = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    except (BadZipFile, ValueError, KeyError, OSError) as exc:
        raise ImportProblem("This is not a readable .xlsx workbook.") from exc
    try:
        missing = set(SCHEMA).difference(book.sheetnames)
        if missing:
            raise ImportProblem("Missing sheets: " + ", ".join(sorted(missing)))
        total = 0
        sheets = dict(SCHEMA)
        if TASK_ORDER_SHEET in book.sheetnames:
            sheets[TASK_ORDER_SHEET] = TASK_ORDER_SCHEMA
        for name, schema in sheets.items():
            sheet = book[name]
            # Untrusted dimensions must not hide data or force billions of rows.
            sheet.reset_dimensions()
            iterator = sheet.iter_rows(values_only=True)
            first = next(iterator, ())
            headers = [str(value).strip() if value is not None else "" for value in first]
            meaningful = [header for header in headers if header]
            if len(meaningful) != len(set(meaningful)):
                raise ImportProblem(f"{name}: duplicate column names.")
            absent = set(schema.split()).difference(headers)
            if absent:
                raise ImportProblem(f"{name}: missing columns: {', '.join(sorted(absent))}")
            # Task settings are identified by service and task together.
            keys = [headers.index(column) for column in schema.split()[:2 if name == TASK_ORDER_SHEET else 1]]
            identifiers = set()
            for number, row in enumerate(iterator, 2):
                total += 1
                if total > MAX_ROWS:
                    raise ImportProblem("Workbook exceeds 10,000 rows. Ask an administrator to use the CLI.")
                if not any(value not in (None, "") for value in row):
                    continue
                parts = [str(row[key]).strip() if key < len(row) and row[key] is not None else "" for key in keys]
                identifier = "|".join(parts)
                if not all(parts) or identifier in identifiers:
                    raise ImportProblem(f"{name}, row {number}: missing or duplicate identifier.")
                identifiers.add(identifier)
                for column, value in zip(headers, row):
                    if column in INTEGER_COLUMNS | DECIMAL_COLUMNS and value not in (None, ""):
                        try:
                            numeric = Decimal(str(value))
                            valid = numeric.is_finite() and numeric >= 0
                            if column in INTEGER_COLUMNS:
                                valid = valid and numeric == numeric.to_integral_value()
                        except (InvalidOperation, ValueError):
                            valid = False
                        if not valid:
                            raise ImportProblem(f"{name}, row {number}: {column} must be a valid non-negative number.")
            if name in {"Service Master", "Sub-Service Master", "Task Master", "Service Detail"} and not identifiers:
                raise ImportProblem(f"{name}: at least one data row is required.")
        return read_reference(book)
    finally:
        book.close()


def expire_stages():
    SOPWorkbookImport.objects.filter(status="PENDING", expires_at__lte=timezone.now()).update(
        status="EXPIRED", workbook=b"",
    )


def _master_rows():
    """{model: {pk: field values}} without timestamps, which every import touches."""
    rows = {}
    for model in MASTER_MODELS:
        fields = [field.attname for field in model._meta.concrete_fields
                  if field.attname not in ("id", "created_at", "updated_at")]
        rows[model] = {item["id"]: {name: item[name] for name in fields}
                       for item in model.objects.values("id", *fields)}
    return rows


def _plain(value):
    return "blank" if value in (None, "") else str(value)


def _preview(flows_before, rows_before):
    """Describe what the import would change; called inside a rolled-back transaction."""
    rows_after = _master_rows()
    tables = []
    for model in MASTER_MODELS:
        before, after = rows_before[model], rows_after[model]
        new = len(set(after) - set(before))
        removed = len(set(before) - set(after))
        changed = sum(1 for pk, values in after.items() if pk in before and before[pk] != values)
        if new or changed or removed:
            tables.append({"name": TABLE_LABELS[model], "new": new, "changed": changed, "removed": removed})
    flows_after = all_flows()
    services_before, services_after = rows_before[Service], rows_after[Service]
    services = []
    for pk, values in sorted(services_after.items(), key=lambda item: (item[1]["category"], item[1]["name"])):
        old = services_before.get(pk)
        changes = []
        if old is None:
            steps = flows_after.get(pk, [])
            changes.append(("added", "New service with " + (
                " → ".join(step["name"] for step in steps) if steps else "no steps yet")))
        else:
            for field, label in SERVICE_FIELDS.items():
                if old[field] != values[field]:
                    changes.append(("changed", f"{label}: {_plain(old[field])} → {_plain(values[field])}"))
            if old["active"] != values["active"]:
                changes.append(("removed" if not values["active"] else "added",
                                "Service switched off" if not values["active"] else "Service switched on again"))
            changes += compare(flows_before.get(pk, []), flows_after.get(pk, []))
        if changes:
            services.append({"code": values["code"], "name": values["name"], "category": values["category"],
                             "changes": [{"kind": kind, "text": text} for kind, text in changes]})
    return {"tables": tables, "services": services[:MAX_PREVIEW_SERVICES],
            "more_services": max(0, len(services) - MAX_PREVIEW_SERVICES)}


class _DryRun(Exception):
    pass


def master_snapshot():
    objects = [obj for model in MASTER_MODELS for obj in model.objects.order_by("pk")]
    return serializers.serialize("json", objects).encode("utf-8")


def lock_masters():
    # PostgreSQL is authoritative. Also blocks admin/CSV master edits during an import.
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('lock_timeout')")
        previous = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('lock_timeout', '5s', true)")
        tables = sorted(connection.ops.quote_name(model._meta.db_table) for model in MASTER_MODELS)
        cursor.execute("LOCK TABLE " + ", ".join(tables) + " IN SHARE ROW EXCLUSIVE MODE")
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous])


@contextmanager
def private_workbook(payload):
    with tempfile.TemporaryDirectory(prefix="salonops-sop-") as directory:
        path = Path(directory) / "workbook.xlsx"
        path.write_bytes(payload)
        path.chmod(0o600)
        yield path


def run_import(payload, dry_run):
    output = io.StringIO()
    with private_workbook(payload) as path:
        try:
            call_command("import_sop_workbook", str(path), dry_run=dry_run, validate_workflow=True, stdout=output, no_color=True)
        except CommandError as exc:
            raise ImportProblem(str(exc)) from exc
        except (ObjectDoesNotExist, ValueError, TypeError, KeyError, DatabaseError) as exc:
            # Do not echo cell values, SQL, paths or workbook contents into pages/logs.
            raise ImportProblem("Import validation failed. Check identifiers, references, column values and field lengths.") from exc
    # Keep only the record totals after the status words.
    return output.getvalue().strip().split("— ", 1)[-1]


@transaction.atomic
def stage_workbook(user, filename, payload):
    reference = validate_workbook(payload)
    lock_masters()
    before = master_snapshot()
    if reference and reference != catalogue_reference(before):
        raise ImportProblem(
            "The catalogue changed after this workbook was downloaded, so uploading it could undo those changes. "
            "Download a fresh workbook, make your change again and upload that."
        )
    flows_before, rows_before = all_flows(), _master_rows()
    try:
        # Run the real import, read what it changed, then roll it all back.
        with transaction.atomic():
            summary = run_import(payload, dry_run=False)
            preview = _preview(flows_before, rows_before)
            raise _DryRun
    except _DryRun:
        pass
    preview["downloaded_here"] = bool(reference)
    # One pending workbook per administrator; no unattended repeated imports.
    SOPWorkbookImport.objects.filter(created_by=user, status="PENDING").update(
        status="CANCELLED", workbook=b"",
    )
    return SOPWorkbookImport.objects.create(
        created_by=user, filename=Path(filename.replace("\\", "/")).name[:160],
        sha256=hashlib.sha256(payload).hexdigest(), workbook=payload,
        master_digest=hashlib.sha256(before).hexdigest(), summary=summary, preview=preview,
        expires_at=timezone.now() + timedelta(hours=1),
    )


@transaction.atomic
def apply_workbook(stage_id, user):
    stage = SOPWorkbookImport.objects.select_for_update().get(pk=stage_id, created_by=user)
    if stage.status != "PENDING" or stage.expires_at <= timezone.now():
        raise ImportProblem("This preview expired or has already been used. Upload and validate again.")
    payload = bytes(stage.workbook)
    if hashlib.sha256(payload).hexdigest() != stage.sha256:
        raise ImportProblem("The staged workbook changed. Upload and validate again.")
    lock_masters()
    before = master_snapshot()
    if hashlib.sha256(before).hexdigest() != stage.master_digest:
        raise ImportProblem("Master data changed after validation. Upload again to review a fresh preview.")
    # Snapshot and master changes commit together or both roll back. No visit models touched.
    stage.master_snapshot = gzip.compress(before, mtime=0)
    flows_before = all_flows()
    stage.summary = run_import(payload, dry_run=False)
    record_changed_versions(flows_before, "WORKBOOK", user=user, note=f"Workbook {stage.filename}")
    stage.status = "APPLIED"
    stage.applied_at = timezone.now()
    stage.workbook = b""
    stage.save(update_fields=["master_snapshot", "summary", "status", "applied_at", "workbook"])
    return stage
