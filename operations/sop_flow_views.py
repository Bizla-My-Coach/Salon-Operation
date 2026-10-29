"""Edit a service's SOP card order in the catalogue, with per-service version history."""
import copy

from django.contrib import messages
from django.db import DatabaseError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .decorators import roles_required
from .models import Service, ServiceDetail, SOPVersion, SubService
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
        steps = copy.deepcopy(before)
        index = _position(request, steps)
        if action in ("up", "down", "remove", "skip", "skip_everywhere", "task_up", "task_down", "task_skip") and index is None:
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
        elif action == "skip":
            allow = request.POST.get("allow") == "on"
            steps[index]["can_skip"] = allow
            verb = "can now be skipped" if allow else "can no longer be skipped"
            note, target = f"{before[index]['name']} {verb}", index
        elif action in ("task_up", "task_down", "task_skip"):
            tasks = steps[index]["tasks"]
            try:
                position = int(request.POST.get("task", ""))
            except ValueError:
                position = -1
            if not 0 <= position < len(tasks):
                messages.error(request, "Choose a task to change.")
                return _back(service)
            name, step_name = tasks[position]["name"], before[index]["name"]
            if action == "task_up" and position > 0:
                tasks[position - 1], tasks[position] = tasks[position], tasks[position - 1]
                note = f"Moved {name} up in {step_name}"
            elif action == "task_down" and position < len(tasks) - 1:
                tasks[position + 1], tasks[position] = tasks[position], tasks[position + 1]
                note = f"Moved {name} down in {step_name}"
            elif action == "task_skip":
                allow = request.POST.get("allow") == "on"
                tasks[position]["can_skip"] = allow
                verb = "can now be skipped" if allow else "can no longer be skipped"
                note = f"{name} {verb} in {step_name}"
            else:
                return _back(service, f"#step-{index + 1}")
            target = index
        elif action == "skip_everywhere":
            allow = request.POST.get("allow") == "on"
            count = _skip_everywhere(before[index]["code"], allow, request.user)
            verb = "skippable" if allow else "not skippable"
            messages.success(request, f"{before[index]['name']} is now {verb} in {count} service(s). Each change was saved as a new version.")
            return _back(service, f"#step-{index + 1}")
        elif action == "add":
            raw = request.POST.get("sub_service", "")
            sub = SubService.objects.filter(pk=int(raw), active=True).exclude(
                code__in=OPENING_CODES).first() if raw.isdigit() else None
            if not sub:
                messages.error(request, "Choose a sub-service to add.")
                return _back(service, "#add-step")
            steps.append({"code": sub.code, "mandatory": request.POST.get("mandatory") == "on",
                          "can_skip": request.POST.get("can_skip") == "on"})
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


def _skip_everywhere(code, allow, user):
    """Set "can skip" on this sub-service in every service that uses it."""
    service_ids = (
        ServiceDetail.objects.filter(sub_service__code=code, active=True, sub_service__active=True)
        .exclude(can_skip=allow).values_list("service_id", flat=True).distinct()
    )
    services = list(Service.objects.select_for_update().filter(pk__in=list(service_ids)).order_by("pk"))
    verb = "can now be skipped" if allow else "can no longer be skipped"
    for service in services:
        before = current_flow(service)
        name = next((step["name"] for step in before if step["code"] == code), code)
        service.service_details.filter(sub_service__code=code, active=True).update(can_skip=allow)
        record_version(service, current_flow(service), "EDITOR", user=user,
                       note=f"{name} {verb} (all services)", before=before)
    return len(services)


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
