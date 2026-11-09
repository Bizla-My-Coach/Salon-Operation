"""Download the live SOP master data as a workbook that uploads straight back.

The sheets and columns match ``import_sop_workbook``. Extra columns that only
help people read or filter (service name, category, step name ...) are marked
"(info)" and ignored on upload. A "Read me" sheet carries a reference to the
master data as it was at download time, so an upload made after someone else
changed the catalogue is stopped instead of silently undoing their change.
"""
import hashlib
import io

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .models import (
    Equipment, InventoryItem, OperationalTask, Service, ServiceDetail, ServiceTaskSetting,
    SubService, TaskEquipment, TaskInventory,
)

README_SHEET = "Read me"
REFERENCE_LABEL = "Catalogue reference"
TASK_ORDER_SHEET = "Service Task Order"
INFO = " (info)"

HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
INFO_FILL = PatternFill("solid", fgColor="7F7F7F")


def catalogue_reference(snapshot):
    return hashlib.sha256(snapshot).hexdigest()


def _sheets():
    services = list(Service.objects.order_by("category", "code"))
    service_by_pk = {item.pk: item for item in services}
    sub_services = list(SubService.objects.order_by("code"))
    sub_by_pk = {item.pk: item for item in sub_services}
    tasks = list(OperationalTask.objects.select_related("sub_service").order_by("sub_service__code", "sequence", "id"))
    task_by_pk = {item.pk: item for item in tasks}

    yield "Service Master", [
        "service_code", "service_name", "service_category", "customer_type", "service_type",
        "standard_duration_minutes", "base_price", "current_version", "notes", "active",
    ], [[s.code, s.name, s.category, s.customer_type, s.service_type, s.standard_duration_minutes,
         s.base_price, s.current_version, s.notes, s.active] for s in services]

    yield "Sub-Service Master", [
        "sub_service_code", "sub_service_name", "category", "standard_output_quantity", "output_unit",
        "procedure_notes", "active",
    ], [[s.code, s.name, s.category, s.standard_output_quantity, s.output_unit, s.procedure_notes, s.active]
        for s in sub_services]

    yield "Inventory Master", [
        "inventory_code", "inventory_name", "inventory_category", "source_method", "base_unit",
        "purchase_pack_quantity", "purchase_pack_unit", "resource_type", "notes", "active",
    ], [[i.code, i.name, i.category, i.source_method, i.base_unit, i.purchase_pack_quantity,
         i.purchase_pack_unit, i.resource_type, i.notes, i.active] for i in InventoryItem.objects.order_by("code")]

    yield "Equipment Master", [
        "equipment_code", "equipment_name", "equipment_category", "utility_type", "branch_code",
        "rated_power_kw", "water_litres_per_minute", "notes", "active",
    ], [[e.code, e.name, e.category, e.utility_type, e.branch_code, e.rated_power_kw,
         e.water_litres_per_minute, e.notes, e.active] for e in Equipment.objects.order_by("code")]

    yield "Task Master", [
        "task_id", "sub_service_code", "sub_service_name" + INFO, "task_sequence", "activity_code", "task_name",
        "role_code", "employee_role", "active_labour_minutes", "passive_time_minutes", "supports_waiting",
        "staff_instructions", "required_skill", "allowed_staff_type", "default_customer_type", "is_skippable",
        "notes", "active",
    ], [[t.external_id, t.sub_service.code, t.sub_service.name, t.sequence, t.activity_code, t.name,
         t.role_code, t.employee_role, t.active_labour_minutes, t.passive_time_minutes, t.supports_waiting,
         t.staff_instructions, t.required_skill, t.allowed_staff_type, t.default_customer_type,
         t.is_skippable, t.notes, t.active] for t in tasks]

    details = sorted(
        ServiceDetail.objects.all(),
        key=lambda d: (service_by_pk[d.service_id].category, service_by_pk[d.service_id].code,
                       not d.active, d.sequence, d.pk),
    )
    yield "Service Detail", [
        "service_detail_id", "service_code", "service_name" + INFO, "service_category" + INFO,
        "sub_service_code", "sub_service_name" + INFO, "sequence", "required_quantity", "mandatory",
        "can_skip", "mapping_status", "notes", "active",
    ], [[d.external_id, service_by_pk[d.service_id].code, service_by_pk[d.service_id].name,
         service_by_pk[d.service_id].category, sub_by_pk[d.sub_service_id].code,
         sub_by_pk[d.sub_service_id].name, d.sequence, d.required_quantity, d.mandatory, d.can_skip,
         d.mapping_status, d.notes, d.active] for d in details]

    inventory = {item.pk: item for item in InventoryItem.objects.all()}
    yield "Task Inventory", [
        "task_inventory_id", "task_id", "task_name" + INFO, "service_code", "service_name" + INFO,
        "inventory_code", "inventory_name" + INFO, "standard_quantity", "unit", "waste_percent",
        "effective_quantity", "mapping_status", "notes", "mapping_note", "active",
    ], [[r.external_id, task_by_pk[r.task_id].external_id, task_by_pk[r.task_id].name,
         service_by_pk[r.service_id].code, service_by_pk[r.service_id].name, inventory[r.inventory_id].code,
         inventory[r.inventory_id].name, r.standard_quantity, r.unit, r.waste_percent, r.effective_quantity,
         r.mapping_status, r.notes, "", r.active]
        for r in TaskInventory.objects.order_by("service_id", "task_id", "id")]

    equipment = {item.pk: item for item in Equipment.objects.all()}
    yield "Task Equipment", [
        "task_equipment_id", "task_id", "task_name" + INFO, "equipment_code", "equipment_name" + INFO,
        "quantity_required", "equipment_usage_minutes", "utility_type", "utility_minutes", "notes", "active",
    ], [[r.external_id, task_by_pk[r.task_id].external_id, task_by_pk[r.task_id].name,
         equipment[r.equipment_id].code, equipment[r.equipment_id].name, r.quantity_required,
         r.equipment_usage_minutes, r.utility_type, r.utility_minutes, r.notes, r.active]
        for r in TaskEquipment.objects.order_by("task_id", "id")]

    settings = sorted(
        ServiceTaskSetting.objects.all(),
        key=lambda s: (service_by_pk[s.service_id].code, task_by_pk[s.task_id].sub_service.code,
                       s.position is None, s.position or 0, task_by_pk[s.task_id].sequence),
    )
    yield TASK_ORDER_SHEET, [
        "service_code", "service_name" + INFO, "task_id", "task_name" + INFO, "sub_service_code" + INFO,
        "position", "can_skip",
    ], [[service_by_pk[s.service_id].code, service_by_pk[s.service_id].name, task_by_pk[s.task_id].external_id,
         task_by_pk[s.task_id].name, task_by_pk[s.task_id].sub_service.code, s.position, s.can_skip]
        for s in settings]


README = [
    "How to use this workbook",
    "",
    "1. Change rows in the sheets, save as .xlsx, then upload it on Import SOP workbook.",
    "2. Columns marked (info) are only there to help you read and filter. They are ignored on upload.",
    "3. Do not change the ID columns of existing rows (service_code, task_id, service_detail_id ...). "
    "The upload matches rows by these IDs. A changed service_code creates a new service; "
    "use Rename service codes in the app to change codes.",
    "4. To add a row, give it a new unique ID, e.g. NEW-001. Steps run in Service Detail 'sequence' order "
    "(10, 20, 30 ...). Use 15 to place a step between 10 and 20.",
    "5. To remove a step or service, set its 'active' cell to FALSE. Deleting a row does not remove it. "
    "A blank 'active' cell counts as active.",
    "   Fill in TRUE or FALSE for 'mandatory' on new steps: a blank cell means the step is optional.",
    "6. A new step must exist in Sub-Service Master, with its tasks in Task Master, before Service Detail uses it.",
    "7. 'Service Task Order' is the full list of per-service task orders and task skips. Remove a row there "
    "to make that service follow the shared task order again.",
    "8. The upload shows every service that will change before you confirm. Each changed service gets a new "
    "version in its SOP history.",
    "9. If anyone changes the catalogue after you download, the upload stops. Download a fresh copy and "
    "redo your change.",
    "",
]


def build_export(snapshot):
    """Return (filename, xlsx bytes) for the current master data."""
    book = Workbook()
    readme = book.active
    readme.title = README_SHEET
    for line in README:
        readme.append([line])
    readme["A1"].font = Font(bold=True, size=14)
    now = timezone.localtime()
    readme.append(["Downloaded", now.strftime("%d %b %Y, %H:%M")])
    readme.append([REFERENCE_LABEL, catalogue_reference(snapshot)])
    readme.column_dimensions["A"].width = 110
    readme.column_dimensions["B"].width = 70
    for name, headers, records in _sheets():
        sheet = book.create_sheet(name)
        sheet.append(headers)
        for record in records:
            sheet.append(record)
        for index, header in enumerate(headers, 1):
            cell = sheet.cell(row=1, column=index)
            cell.font = HEADER_FONT
            cell.fill = INFO_FILL if header.endswith(INFO) else HEADER_FILL
            sheet.column_dimensions[get_column_letter(index)].width = max(12, min(40, len(header) + 4))
        sheet.freeze_panes = "B2"
        sheet.auto_filter.ref = sheet.dimensions
    output = io.BytesIO()
    book.save(output)
    book.close()
    return f"hairship-sop-{now:%Y-%m-%d-%H%M}.xlsx", output.getvalue()


def read_reference(book):
    """The catalogue reference stamped on a downloaded workbook, or '' for other workbooks."""
    if README_SHEET not in book.sheetnames:
        return ""
    sheet = book[README_SHEET]
    sheet.reset_dimensions()
    for row in sheet.iter_rows(max_row=60, max_col=2, values_only=True):
        if row and row[0] == REFERENCE_LABEL and len(row) > 1 and row[1]:
            return str(row[1]).strip()[:64]
    return ""
