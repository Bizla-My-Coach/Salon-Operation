from .models import SubService, VisitTask


CONSULTATION_CODE = "SUB001"
SANITISATION_CODE = "SUB025"
OPENING_TASK_TYPES = ("HYGIENE", "CONSULT")


def unfinished_verification_tasks(tasks):
    """An explicitly permitted skip satisfies the SOP with its required reason."""
    from django.db.models import Q
    allowed_skip = Q(status='SKIPPED', can_skip=True) & (
        Q(skip_reason_required=False) | ~Q(skip_reason='')
    )
    return tasks.exclude(Q(status='COMPLETED') | allowed_skip)


def _task_type(sub_service_code):
    if sub_service_code == CONSULTATION_CODE:
        return "CONSULT", "BEFORE"
    if sub_service_code == SANITISATION_CODE:
        return "HYGIENE", "BEFORE"
    return "SERVICE", "DURING"


def _append_sub_service_tasks(rows, visit_service, sub_service, start_sequence, required=True, skippable=False):
    task_type, phase = _task_type(sub_service.code)
    sequence = start_sequence
    for task in sub_service.tasks.filter(active=True).order_by("sequence", "id"):
        rows.append(
            VisitTask(
                visit_service=visit_service,
                source_operational_task=task,
                sequence=sequence,
                sub_service_name=sub_service.name,
                phase=phase,
                task_type=task_type,
                title=task.name,
                instructions=task.notes,
                active_labour_minutes=task.active_labour_minutes,
                passive_time_minutes=task.passive_time_minutes,
                supports_waiting=task.supports_waiting,
                staff_instructions=task.staff_instructions,
                required=required,
                can_skip=task.is_skippable or skippable,
                skip_reason_required=task.is_skippable or skippable,
            )
        )
        sequence += 10
    return sequence


def build_visit_tasks(visit):
    """Build one ordered execution plan for every service in a visit.

    Sanitisation and consultation are placed, in that order, only on the first
    service, even when every selected service maps those sub-services.
    """
    visit_services = list(visit.services.select_related("service").order_by("order_number", "id"))
    if not visit_services:
        return
    if VisitTask.objects.filter(visit_service__visit=visit).exclude(status="PENDING").exists():
        return

    VisitTask.objects.filter(visit_service__visit=visit).delete()
    rows = []
    consultation = SubService.objects.filter(code=CONSULTATION_CODE, active=True).first()
    sanitisation = SubService.objects.filter(code=SANITISATION_CODE, active=True).first()

    for index, visit_service in enumerate(visit_services):
        sequence = 10
        if index == 0:
            if sanitisation:
                sequence = _append_sub_service_tasks(rows, visit_service, sanitisation, sequence)
            if consultation:
                sequence = _append_sub_service_tasks(rows, visit_service, consultation, sequence)

        procedure_row_count = len(rows)
        details = (
            visit_service.service.service_details.filter(active=True, sub_service__active=True)
            .exclude(sub_service__code__in=[CONSULTATION_CODE, SANITISATION_CODE])
            .select_related("sub_service")
            .prefetch_related("sub_service__tasks")
            .order_by("sequence", "id")
        )
        for detail in details:
            sequence = _append_sub_service_tasks(
                rows,
                visit_service,
                detail.sub_service,
                sequence,
                required=detail.mandatory,
                skippable=detail.can_skip,
            )

        # Existing CSV/demo services continue to work until workbook data is imported.
        if len(rows) == procedure_row_count:
            for legacy in visit_service.service.sop_tasks.filter(active=True).exclude(
                task_type__in=["CONSULT", "HYGIENE"]
            ):
                rows.append(
                    VisitTask(
                        visit_service=visit_service,
                        source_task=legacy,
                        sequence=sequence,
                        phase=legacy.phase,
                        task_type=legacy.task_type,
                        title=legacy.title,
                        instructions=legacy.instructions,
                        required=legacy.required,
                        can_skip=legacy.can_skip,
                        skip_reason_required=legacy.skip_reason_required,
                    )
                )
                sequence += 10

    VisitTask.objects.bulk_create(rows)


def rebuild_service_tasks(visit_service, include_sanitisation=False, include_consultation=False):
    """Build a fresh snapshot only for an unstarted service assignment.

    This is used for a newly-added upcoming service or a replacement.  It must
    never rewrite task history belonging to other services in the visit.
    """
    if visit_service.status != "ASSIGNED":
        return
    if visit_service.tasks.exclude(status="PENDING").exists():
        return
    visit_service.tasks.all().delete()
    rows = []
    sequence = 10
    if include_sanitisation:
        sanitisation = SubService.objects.filter(code=SANITISATION_CODE, active=True).first()
        if sanitisation:
            sequence = _append_sub_service_tasks(rows, visit_service, sanitisation, sequence)
    if include_consultation:
        consultation = SubService.objects.filter(code=CONSULTATION_CODE, active=True).first()
        if consultation:
            sequence = _append_sub_service_tasks(rows, visit_service, consultation, sequence)

    procedure_row_count = len(rows)
    details = (
        visit_service.service.service_details.filter(active=True, sub_service__active=True)
        .exclude(sub_service__code__in=[CONSULTATION_CODE, SANITISATION_CODE])
        .select_related("sub_service")
        .prefetch_related("sub_service__tasks")
        .order_by("sequence", "id")
    )
    for detail in details:
        sequence = _append_sub_service_tasks(
            rows, visit_service, detail.sub_service, sequence, required=detail.mandatory,
            skippable=detail.can_skip,
        )
    if len(rows) == procedure_row_count:
        for legacy in visit_service.service.sop_tasks.filter(active=True).exclude(
            task_type__in=["CONSULT", "HYGIENE"]
        ):
            rows.append(
                VisitTask(
                    visit_service=visit_service,
                    source_task=legacy,
                    sequence=sequence,
                    phase=legacy.phase,
                    task_type=legacy.task_type,
                    title=legacy.title,
                    instructions=legacy.instructions,
                    required=legacy.required,
                    can_skip=legacy.can_skip,
                    skip_reason_required=legacy.skip_reason_required,
                )
            )
            sequence += 10
    VisitTask.objects.bulk_create(rows)


def task_resources(task, service):
    """Return service-specific inventory and shared equipment for a task."""
    inventory = task.inventory_requirements.filter(active=True, service=service).select_related("inventory")
    equipment = task.equipment_requirements.filter(active=True).select_related("equipment")
    return inventory, equipment
