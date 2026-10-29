"""Per-service SOP flow versions.

A service's SOP flow is its ordered list of active sub-services (Service
Detail rows). Sanitisation and consultation are excluded: the visit workflow
adds them once per visit on its own. Each change from the catalogue editor, a
workbook import or a restore is saved as a new, immutable SOPVersion.
"""
import hashlib
import json

from django.db import transaction
from django.db.models import Max

from .models import Service, ServiceDetail, SOPVersion, SubService
from .workflow import CONSULTATION_CODE, SANITISATION_CODE

OPENING_CODES = (CONSULTATION_CODE, SANITISATION_CODE)


class FlowProblem(Exception):
    pass


def current_flow(service):
    details = (
        service.service_details.filter(active=True, sub_service__active=True)
        .exclude(sub_service__code__in=OPENING_CODES)
        .select_related("sub_service")
        .order_by("sequence", "id")
    )
    return [
        {"code": detail.sub_service.code, "name": detail.sub_service.name, "mandatory": detail.mandatory}
        for detail in details
    ]


def all_flows(service_ids=None):
    """Return {service_id: flow} with one query instead of one per service."""
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
    flows = {pk: [] for pk in services.values_list("pk", flat=True)}
    for detail in details:
        flows.setdefault(detail.service_id, []).append(
            {"code": detail.sub_service.code, "name": detail.sub_service.name, "mandatory": detail.mandatory}
        )
    return flows


def same_flow(first, second):
    def key(flow):
        return [(step["code"], bool(step["mandatory"])) for step in flow]
    return key(first) == key(second)


def flow_digest(flows):
    payload = json.dumps(
        {str(pk): [(step["code"], bool(step["mandatory"])) for step in flow] for pk, flow in sorted(flows.items())},
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


@transaction.atomic
def restore_version(version, user):
    service = Service.objects.select_for_update().get(pk=version.service_id)
    before = current_flow(service)
    steps = [{"code": step["code"], "mandatory": step["mandatory"]} for step in version.steps]
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
    return changes
