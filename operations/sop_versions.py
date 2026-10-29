"""Per-service SOP flow versions.

A service's SOP flow is its ordered list of active sub-services (Service
Detail rows), each with its tasks in this service's order and skip settings. Sanitisation and consultation are excluded: the visit workflow
adds them once per visit on its own. Each change from the catalogue editor, a
workbook import or a restore is saved as a new, immutable SOPVersion.
"""
import hashlib
import json

from django.db import transaction
from django.db.models import Max

from .models import OperationalTask, Service, ServiceDetail, ServiceTaskSetting, SOPVersion, SubService
from .workflow import CONSULTATION_CODE, SANITISATION_CODE, sort_service_tasks

OPENING_CODES = (CONSULTATION_CODE, SANITISATION_CODE)


class FlowProblem(Exception):
    pass


def current_flow(service):
    return all_flows([service.pk])[service.pk]


def _step(detail, tasks_by_sub, settings):
    tasks = sort_service_tasks(tasks_by_sub.get(detail.sub_service_id, []), settings)
    return {"code": detail.sub_service.code, "name": detail.sub_service.name,
            "mandatory": detail.mandatory, "can_skip": detail.can_skip,
            "tasks": [{"id": task.external_id, "name": task.name, "can_skip": skip} for task, skip in tasks]}


def all_flows(service_ids=None):
    """Return {service_id: flow} with a few queries instead of several per service."""
    details = (
        ServiceDetail.objects.filter(active=True, sub_service__active=True)
        .exclude(sub_service__code__in=OPENING_CODES)
        .select_related("sub_service")
        .order_by("service_id", "sequence", "id")
    )
    services = Service.objects.all()
    if service_ids is not None:
        details = details.filter(service_id__in=service_ids)
        services = services.filter(pk__in=service_ids)
    details = list(details)
    tasks_by_sub = {}
    for task in OperationalTask.objects.filter(
        active=True, sub_service_id__in={detail.sub_service_id for detail in details},
    ).order_by("sequence", "id"):
        tasks_by_sub.setdefault(task.sub_service_id, []).append(task)
    settings = {}
    task_settings = ServiceTaskSetting.objects.all()
    if service_ids is not None:
        task_settings = task_settings.filter(service_id__in=service_ids)
    for item in task_settings:
        settings.setdefault(item.service_id, {})[item.task_id] = item
    flows = {pk: [] for pk in services.values_list("pk", flat=True)}
    for detail in details:
        flows.setdefault(detail.service_id, []).append(
            _step(detail, tasks_by_sub, settings.get(detail.service_id, {}))
        )
    return flows


def same_flow(first, second):
    def key(flow):
        return [_key(step) for step in flow]
    return key(first) == key(second)


def _key(step):
    # Versions saved before "can skip" or task settings existed lack those keys.
    tasks = tuple((task["id"], bool(task.get("can_skip", False))) for task in step.get("tasks", []))
    return (step["code"], bool(step["mandatory"]), bool(step.get("can_skip", False)), tasks)


def flow_digest(flows):
    payload = json.dumps(
        {str(pk): [_key(step) for step in flow] for pk, flow in sorted(flows.items())},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def latest_version(service):
    return service.sop_versions.order_by("-number").first()


def _next_number(service):
    return (service.sop_versions.aggregate(top=Max("number"))["top"] or 0) + 1


def record_version(service, steps, source, user=None, note="", before=None):
    """Save a version when the flow differs from the latest one.

    ``before`` is the flow prior to the change. When a service has no history
    yet it is saved first as a baseline, so the very first change can be
    compared and restored.
    """
    latest = latest_version(service)
    if latest is None:
        if before and not same_flow(before, steps):
            SOPVersion.objects.create(
                service=service, number=1, source="BASELINE", steps=before,
                note="Flow before version tracking started",
            )
    elif same_flow(latest.steps, steps):
        return None
    return SOPVersion.objects.create(
        service=service, number=_next_number(service), source=source, steps=steps,
        note=note[:300], created_by=user,
    )


def record_changed_versions(before_flows, source, user=None, note=""):
    """After a bulk change (workbook import), version every service whose flow moved."""
    after_flows = all_flows()
    created = []
    services = Service.objects.in_bulk(list(after_flows))
    for pk, after in after_flows.items():
        before = before_flows.get(pk, [])
        if same_flow(before, after):
            continue
        version = record_version(services[pk], after, source, user=user, note=note, before=before)
        if version:
            created.append(version)
    return created


def _new_detail_id(service, index):
    base = f"FLOW-{service.pk}-{index}"
    candidate, suffix = base, 1
    while ServiceDetail.objects.filter(external_id=candidate).exists():
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate[:30]


@transaction.atomic
def apply_flow(service, steps):
    """Make the service's active Service Detail rows match ``steps`` exactly.

    Existing rows are reused (keeping their quantities and notes), rows no
    longer in the flow are deactivated, never deleted, and sanitisation or
    consultation rows are left untouched.
    """
    codes = [step["code"] for step in steps]
    if any(code in OPENING_CODES for code in codes):
        raise FlowProblem("Sanitisation and consultation are added to every visit automatically; leave them out of the flow.")
    sub_services = {item.code: item for item in SubService.objects.filter(code__in=codes, active=True)}
    missing = sorted(set(codes) - set(sub_services))
    if missing:
        raise FlowProblem("Unknown or inactive sub-service codes: " + ", ".join(missing))

    rows = list(
        service.service_details.select_for_update()
        .exclude(sub_service__code__in=OPENING_CODES)
        .select_related("sub_service")
        .order_by("-active", "sequence", "id")
    )
    available = {}
    for row in rows:
        available.setdefault(row.sub_service.code, []).append(row)

    # Park every row on a temporary sequence first so reordering cannot clash
    # with the (service, sub_service, sequence) unique constraint.
    for offset, row in enumerate(rows):
        row.sequence = 1_000_000 + offset
        row.save(update_fields=["sequence", "updated_at"])

    used = set()
    for index, step in enumerate(steps, 1):
        reuse = available.get(step["code"], [])
        row = reuse.pop(0) if reuse else ServiceDetail(
            external_id=_new_detail_id(service, index), service=service,
            sub_service=sub_services[step["code"]], mapping_status="Flow",
        )
        row.sequence = index * 10
        row.mandatory = bool(step.get("mandatory", True))
        row.can_skip = bool(step.get("can_skip", False))
        row.active = True
        row.save()
        used.add(row.pk)

    for offset, row in enumerate(rows):
        if row.pk in used:
            continue
        row.active = False
        # Keep parked rows clear of the live 10, 20, 30... sequence range.
        row.sequence = 2_000_000 + offset
        row.save(update_fields=["active", "sequence", "updated_at"])

    for step in steps:
        _apply_task_settings(service, sub_services[step["code"]], step.get("tasks"))


def _apply_task_settings(service, sub_service, wanted):
    """Store this service's task order and skips for one sub-service.

    Settings that match the workbook order with no skips are removed, so the
    service simply follows the shared sub-service again.
    """
    tasks = list(sub_service.tasks.filter(active=True).order_by("sequence", "id"))
    existing = ServiceTaskSetting.objects.filter(service=service, task__sub_service=sub_service)
    by_id = {task.external_id: task for task in tasks}
    wanted = [item for item in (wanted or []) if item["id"] in by_id]
    listed = {item["id"] for item in wanted}
    wanted += [{"id": task.external_id, "can_skip": False} for task in tasks if task.external_id not in listed]
    if [item["id"] for item in wanted] == [task.external_id for task in tasks] and not any(
        item.get("can_skip") for item in wanted
    ):
        existing.delete()
        return
    for position, item in enumerate(wanted, 1):
        ServiceTaskSetting.objects.update_or_create(
            service=service, task=by_id[item["id"]],
            defaults={"position": position * 10, "can_skip": bool(item.get("can_skip", False))},
        )


@transaction.atomic
def restore_version(version, user):
    service = Service.objects.select_for_update().get(pk=version.service_id)
    before = current_flow(service)
    steps = [{"code": step["code"], "mandatory": step["mandatory"], "can_skip": step.get("can_skip", False),
              "tasks": step.get("tasks", [])} for step in version.steps]
    apply_flow(service, steps)
    after = current_flow(service)
    return record_version(
        service, after, "RESTORE", user=user, note=f"Restored version {version.number}", before=before,
    )


def compare(before, after):
    """Plain-language list of what changed between two flows."""
    before_codes = [step["code"] for step in before]
    after_codes = [step["code"] for step in after]
    names = {step["code"]: step.get("name") or step["code"] for step in before + after}
    changes = []
    for code in after_codes:
        if code not in before_codes:
            changes.append(("added", f"Added {names[code]}"))
    for code in before_codes:
        if code not in after_codes:
            changes.append(("removed", f"Removed {names[code]}"))
    kept_before = [code for code in before_codes if code in after_codes]
    kept_after = [code for code in after_codes if code in before_codes]
    if kept_before != kept_after:
        changes.append(("moved", "New order: " + " → ".join(names[code] for code in after_codes)))
    before_mandatory = {step["code"]: step["mandatory"] for step in before}
    for step in after:
        old = before_mandatory.get(step["code"])
        if old is not None and old != step["mandatory"]:
            label = "mandatory" if step["mandatory"] else "optional"
            changes.append(("changed", f"{names[step['code']]} is now {label}"))
    before_skip = {step["code"]: step.get("can_skip", False) for step in before}
    for step in after:
        old = before_skip.get(step["code"])
        if old is not None and old != step.get("can_skip", False):
            label = "can now be skipped" if step.get("can_skip") else "can no longer be skipped"
            changes.append(("changed", f"{names[step['code']]} {label}"))
    before_tasks = {step["code"]: step["tasks"] for step in before if "tasks" in step}
    for step in after:
        old, new = before_tasks.get(step["code"]), step.get("tasks")
        if old is None or new is None:
            continue
        old_ids, new_ids = [task["id"] for task in old], [task["id"] for task in new]
        if [i for i in old_ids if i in new_ids] != [i for i in new_ids if i in old_ids]:
            changes.append(("moved", f"{names[step['code']]} tasks: " + " → ".join(task["name"] for task in new)))
        old_skip = {task["id"]: task.get("can_skip", False) for task in old}
        for task in new:
            if task["id"] in old_skip and old_skip[task["id"]] != task.get("can_skip", False):
                label = "can now be skipped" if task.get("can_skip") else "can no longer be skipped"
                changes.append(("changed", f"{task['name']} {label} in {names[step['code']]}"))
    return changes
