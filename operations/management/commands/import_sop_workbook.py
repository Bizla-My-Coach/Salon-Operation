from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from openpyxl import load_workbook

from operations.models import (
    Equipment,
    InventoryItem,
    OperationalTask,
    Service,
    ServiceDetail,
    ServiceTaskSetting,
    SubService,
    TaskEquipment,
    TaskInventory,
)


def rows(sheet):
    sheet.reset_dimensions()
    values = sheet.iter_rows(values_only=True)
    headers = [str(value).strip() if value is not None else "" for value in next(values, ())]
    for row in values:
        # Reset dimensions to read all rows, but real Excel files may omit trailing
        # blank cells. Keep every header so optional notes/columns remain present.
        record = {header: row[index] if index < len(row) else None
                  for index, header in enumerate(headers)}
        if any(value not in (None, "") for value in record.values()):
            yield record


def text(value):
    return "" if value is None else str(value).strip()


def integer(value, default=0):
    if value in (None, ""):
        return default
    return int(value)


def decimal(value, default=Decimal("0")):
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return default


def optional_decimal(value):
    return None if value in (None, "") else decimal(value)


def boolean(value):
    if isinstance(value, bool):
        return value
    return text(value).lower() in {"active", "true", "yes", "1", "y"}


def optional(record, column, convert, defaults, field=None):
    """Only update a field when its column is in the sheet, so older workbooks keep current values."""
    if column in record:
        defaults[field or column] = convert(record[column])


TASK_ORDER_SHEET = "Service Task Order"


def active(record):
    value = record.get("active", record.get("status", True))
    # A blank cell (e.g. a row added in Excel) stays active; only FALSE/inactive switches a row off.
    return True if value in (None, "") else boolean(value)


class Command(BaseCommand):
    help = "Import the external salon SOP workbook into operational master tables."

    def add_arguments(self, parser):
        parser.add_argument("workbook", nargs="?", default="data/Service_Operation_SOP_Upload_Workbook.xlsx")
        parser.add_argument("--dry-run", action="store_true", help="Validate the import transaction, then roll it back.")
        parser.add_argument("--validate-workflow", action="store_true", help="Require active sanitisation and consultation tasks.")

    @transaction.atomic
    def handle(self, *args, **options):
        path = Path(options["workbook"])
        if not path.exists():
            raise CommandError(f"Workbook not found: {path}")
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            self.import_book(book, options)
        finally:
            book.close()

    def import_book(self, book, options):
        required = {
            "Service Master", "Service Detail", "Sub-Service Master", "Task Master",
            "Task Inventory", "Task Equipment", "Inventory Master", "Equipment Master",
        }
        missing = required.difference(book.sheetnames)
        if missing:
            raise CommandError(f"Missing sheets: {', '.join(sorted(missing))}")

        counts = {}
        for row in rows(book["Service Master"]):
            defaults = {
                "name": text(row["service_name"]),
                "category": text(row["service_category"]),
                "customer_type": text(row["customer_type"]),
                "service_type": text(row["service_type"]),
                "standard_duration_minutes": integer(row["standard_duration_minutes"], 30),
                "current_version": text(row["current_version"]),
                "notes": text(row["notes"]),
                "active": active(row),
            }
            optional(row, "base_price", decimal, defaults)
            Service.objects.update_or_create(code=text(row["service_code"]), defaults=defaults)
        counts["services"] = Service.objects.count()

        for row in rows(book["Sub-Service Master"]):
            SubService.objects.update_or_create(
                code=text(row["sub_service_code"]),
                defaults={
                    "name": text(row["sub_service_name"]),
                    "category": text(row["category"]),
                    "standard_output_quantity": decimal(row["standard_output_quantity"], Decimal("1")),
                    "output_unit": text(row["output_unit"]),
                    "procedure_notes": text(row["procedure_notes"]),
                    "active": active(row),
                },
            )
        counts["sub-services"] = SubService.objects.count()

        for row in rows(book["Inventory Master"]):
            InventoryItem.objects.update_or_create(
                code=text(row["inventory_code"]),
                defaults={
                    "name": text(row["inventory_name"]),
                    "category": text(row["inventory_category"]),
                    "source_method": text(row["source_method"]),
                    "base_unit": text(row["base_unit"]),
                    "purchase_pack_quantity": optional_decimal(row["purchase_pack_quantity"]),
                    "purchase_pack_unit": text(row["purchase_pack_unit"]),
                    "resource_type": text(row["resource_type"]),
                    "notes": text(row["notes"]),
                    "active": active(row),
                },
            )
        counts["inventory items"] = InventoryItem.objects.count()

        for row in rows(book["Equipment Master"]):
            Equipment.objects.update_or_create(
                code=text(row["equipment_code"]),
                defaults={
                    "name": text(row["equipment_name"]),
                    "category": text(row["equipment_category"]),
                    "utility_type": text(row["utility_type"]),
                    "branch_code": text(row["branch_code"]),
                    "rated_power_kw": optional_decimal(row["rated_power_kw"]),
                    "water_litres_per_minute": optional_decimal(row["water_litres_per_minute"]),
                    "notes": text(row["notes"]),
                    "active": active(row),
                },
            )
        counts["equipment items"] = Equipment.objects.count()

        for row in rows(book["Task Master"]):
            OperationalTask.objects.update_or_create(
                external_id=text(row["task_id"]),
                defaults={
                    "sub_service": SubService.objects.get(code=text(row["sub_service_code"])),
                    "sequence": integer(row["task_sequence"], 10),
                    "activity_code": text(row["activity_code"]),
                    "name": text(row["task_name"]),
                    "role_code": text(row["role_code"]),
                    "employee_role": text(row["employee_role"]),
                    "active_labour_minutes": integer(row["active_labour_minutes"]),
                    "passive_time_minutes": integer(row["passive_time_minutes"]),
                    "supports_waiting": boolean(row.get("supports_waiting", False)),
                    "staff_instructions": text(row.get("staff_instructions", "")),
                    "required_skill": text(row["required_skill"]),
                    "allowed_staff_type": text(row["allowed_staff_type"]),
                    "default_customer_type": text(row["default_customer_type"]),
                    "is_skippable": boolean(row["is_skippable"]),
                    "notes": text(row["notes"]),
                    "active": active(row),
                },
            )
        counts["tasks"] = OperationalTask.objects.count()

        for row in rows(book["Service Detail"]):
            defaults = {
                "service": Service.objects.get(code=text(row["service_code"])),
                "sub_service": SubService.objects.get(code=text(row["sub_service_code"])),
                "sequence": integer(row["sequence"], 10),
                "required_quantity": decimal(row["required_quantity"], Decimal("1")),
                "mandatory": boolean(row["mandatory"]),
                "mapping_status": text(row["mapping_status"]),
                "notes": text(row["notes"]),
                "active": active(row),
            }
            optional(row, "can_skip", boolean, defaults)
            ServiceDetail.objects.update_or_create(external_id=text(row["service_detail_id"]), defaults=defaults)
        counts["service mappings"] = ServiceDetail.objects.count()

        for row in rows(book["Task Inventory"]):
            TaskInventory.objects.update_or_create(
                external_id=text(row["task_inventory_id"]),
                defaults={
                    "task": OperationalTask.objects.get(external_id=text(row["task_id"])),
                    "service": Service.objects.get(code=text(row["service_code"])),
                    "inventory": InventoryItem.objects.get(code=text(row["inventory_code"])),
                    "standard_quantity": decimal(row["standard_quantity"]),
                    "unit": text(row["unit"]),
                    "waste_percent": decimal(row["waste_percent"]),
                    "effective_quantity": decimal(row["effective_quantity"]),
                    "mapping_status": text(row["mapping_status"]),
                    "notes": text(row["notes"] or row["mapping_note"]),
                    "active": active(row),
                },
            )
        counts["inventory mappings"] = TaskInventory.objects.count()

        for row in rows(book["Task Equipment"]):
            TaskEquipment.objects.update_or_create(
                external_id=text(row["task_equipment_id"]),
                defaults={
                    "task": OperationalTask.objects.get(external_id=text(row["task_id"])),
                    "equipment": Equipment.objects.get(code=text(row["equipment_code"])),
                    "quantity_required": decimal(row["quantity_required"], Decimal("1")),
                    "equipment_usage_minutes": integer(row["equipment_usage_minutes"]),
                    "utility_type": text(row["utility_type"]),
                    "utility_minutes": integer(row["utility_minutes"]),
                    "notes": text(row["notes"]),
                    "active": active(row),
                },
            )
        counts["equipment mappings"] = TaskEquipment.objects.count()

        if TASK_ORDER_SHEET in book.sheetnames:
            counts["service task settings"] = self.import_task_order(book[TASK_ORDER_SHEET])

        if options["validate_workflow"]:
            for code in ("SUB025", "SUB001"):
                if not SubService.objects.filter(code=code, active=True, tasks__active=True).exists():
                    raise CommandError(f"Workflow requires {code} to have at least one active task.")
        summary = ", ".join(f"{name}: {count}" for name, count in counts.items())
        if options["dry_run"]:
            transaction.set_rollback(True)
            self.stdout.write(self.style.SUCCESS(f"Dry run successful; no changes saved — {summary}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Workbook imported successfully — {summary}"))

    def import_task_order(self, sheet):
        """The optional sheet lists every per-service task order or skip setting.

        When the sheet is present it is the full list: a setting missing from it
        is removed, so that service follows the shared task order again.
        """
        services = {item.code: item.pk for item in Service.objects.only("pk", "code")}
        tasks = {item.external_id: item.pk for item in OperationalTask.objects.only("pk", "external_id")}
        kept = set()
        for row in rows(sheet):
            service_code, task_id = text(row["service_code"]), text(row["task_id"])
            if service_code not in services:
                raise CommandError(f"{TASK_ORDER_SHEET}: unknown service code {service_code}.")
            if task_id not in tasks:
                raise CommandError(f"{TASK_ORDER_SHEET}: unknown task id {task_id}.")
            position = row.get("position")
            setting, _ = ServiceTaskSetting.objects.update_or_create(
                service_id=services[service_code], task_id=tasks[task_id],
                defaults={"position": None if position in (None, "") else integer(position),
                          "can_skip": boolean(row.get("can_skip", False))},
            )
            kept.add(setting.pk)
        ServiceTaskSetting.objects.exclude(pk__in=kept).delete()
        return ServiceTaskSetting.objects.count()
