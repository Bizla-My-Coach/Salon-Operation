"""Edit a service's SOP card order in the catalogue, with per-service version history."""
from django.contrib import messages
from django.db import DatabaseError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .decorators import roles_required
from .models import Service, SOPVersion, SubService
from .sop_versions import (
    OPENING_CODES, FlowProblem, apply_flow, compare, current_flow, flow_digest, record_version, restore_version,
)


def service_flow_digest(service, flow=None):
    return flow_digest({service.pk: current_flow(service) if flow is None else flow})


def _back(service, anchor=""):
    return redirect(f"{reverse('service_catalog')}?service={service.pk}{anchor}")


def _position(request, steps):
    try:
        index = int(request.POST.get("index", ""))
    except ValueError:
        return None
    return index if 0 <= index < len(steps) else None


@roles_required("ADMIN")
@require_POST
def sop_edit(request, service_id):
    action = request.POST.get("action")
    with transaction.atomic():
        service = get_object_or_404(Service.objects.select_for_update(), pk=service_id)
        before = current_flow(service)
        # Two admins (or two tabs) editing at once must not overwrite each other.
        if request.POST.get("digest") != service_flow_digest(service, before):
            messages.error(request, "This service's steps changed since you opened the page. Check the list and try again.")
            return _back(service)
        steps = [{"code": step["code"], "mandatory": step["mandatory"]} for step in before]
        index = _position(request, steps)
        if action in ("up", "down", "remove") and index is None:
            messages.error(request, "Choose a step to change.")
            return _back(service)
        if action == "up" and index > 0:
            steps[index - 1], steps[index] = steps[index], steps[index - 1]
            note, target = f"Moved {before[index]['name']} up", index - 1
        elif action == "down" and index < len(steps) - 1:
            steps[index + 1], steps[index] = steps[index], steps[index + 1]
            note, target = f"Moved {before[index]['name']} down", index + 1
        elif action == "remove":
            steps.pop(index)
            note, target = f"Removed {before[index]['name']}", max(index - 1, 0)
        elif action == "add":
            raw = request.POST.get("sub_service", "")
            sub = SubService.objects.filter(pk=int(raw), active=True).exclude(
                code__in=OPENING_CODES).first() if raw.isdigit() else None
            if not sub:
                messages.error(request, "Choose a sub-service to add.")
                return _back(service, "#add-step")
            steps.append({"code": sub.code, "mandatory": request.POST.get("mandatory") == "on"})
            note, target = f"Added {sub.name}", len(steps) - 1
        else:
            return _back(service)
        try:
            apply_flow(service, steps)
        except (FlowProblem, DatabaseError) as exc:
            transaction.set_rollback(True)
            messages.error(request, str(exc) if isinstance(exc, FlowProblem) else "SOP data is busy. Nothing was changed; try again shortly.")
            return _back(service)
        version = record_version(service, current_flow(service), "EDITOR", user=request.user, note=note, before=before)
    if version:
        messages.success(request, f"{note}. Saved as version {version.number}. New visits use this order.")
    return _back(service, f"#step-{target + 1}")


@roles_required("ADMIN", "GENERAL_MANAGER", "MANAGER")
@require_GET
def sop_history(request, service_id):
    service = get_object_or_404(Service, pk=service_id)
    versions = list(service.sop_versions.select_related("created_by").order_by("-number"))
    entries = []
    for index, version in enumerate(versions):
        previous = versions[index + 1] if index + 1 < len(versions) else None
        entries.append({
            "version": version,
            "changes": compare(previous.steps, version.steps) if previous else [],
            "is_latest": index == 0,
        })
    return render(request, "operations/sop_history.html", {
        "service": service, "entries": entries, "current": current_flow(service),
        "can_restore": request.user.is_superuser or getattr(getattr(request.user, "profile", None), "role", "") == "ADMIN",
    })


@roles_required("ADMIN")
@require_POST
def sop_restore(request, version_id):
    version = get_object_or_404(SOPVersion, pk=version_id)
    try:
        restored = restore_version(version, request.user)
    except FlowProblem as exc:
        messages.error(request, str(exc))
    except DatabaseError:
        messages.error(request, "SOP data is busy. Nothing was changed; try again shortly.")
    else:
        if restored:
            messages.success(request, f"Restored version {version.number} as version {restored.number}.")
        else:
            messages.info(request, "The current SOP already matches that version.")
    return redirect("sop_history", service_id=version.service_id)
