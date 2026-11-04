"""Add a service from the catalogue: copy an existing one under a new name, or start blank."""
import re
from collections import Counter

from django import forms
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .decorators import roles_required
from .forms import BootstrapMixin
from .models import Service, TaskInventory
from .sop_versions import all_flows, apply_flow, current_flow, record_version

CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
COPIED_FIELDS = ("category", "customer_type", "service_type", "standard_duration_minutes", "base_price")


def suggest_code():
    """Next free code in the most common pattern, e.g. SER041 after SER040."""
    codes = list(Service.objects.values_list("code", flat=True))
    parts = [match.groups() for match in (re.match(r"^([A-Za-z_-]*?)(\d+)$", code) for code in codes) if match]
    if not parts:
        prefix, width, number = "SVC", 3, 0
    else:
        prefix = Counter(prefix for prefix, _ in parts).most_common(1)[0][0]
        same = [digits for p, digits in parts if p == prefix]
        width = max(len(digits) for digits in same)
        number = max(int(digits) for digits in same)
    taken = {code.upper() for code in codes}
    while True:
        number += 1
        candidate = f"{prefix}{number:0{width}d}"
        if candidate.upper() not in taken:
            return candidate


class ServiceCreateForm(BootstrapMixin, forms.Form):
    START_CHOICES = [("copy", "Copy an existing service"), ("blank", "Start from scratch")]
    start = forms.ChoiceField(choices=START_CHOICES, widget=forms.RadioSelect, initial="copy", label="How do you want to start?")
    source = forms.ModelChoiceField(
        queryset=Service.objects.filter(active=True), required=False, empty_label="Choose a service to copy",
        label="Service to copy",
        help_text="Its steps, task order, skip settings and inventory are copied. The original is not changed.",
    )
    name = forms.CharField(max_length=160, label="New service name")
    code = forms.CharField(max_length=30, label="Service code", help_text="A short unique code, used in reports and workbook imports.")
    category = forms.CharField(max_length=80, required=False)
    customer_type = forms.CharField(max_length=30, required=False, label="Customer type")
    service_type = forms.CharField(max_length=60, required=False, label="Service type")
    standard_duration_minutes = forms.IntegerField(min_value=1, max_value=1440, required=False, label="Standard minutes")
    base_price = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, label="Base price (₹)")

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if Service.objects.filter(name__iexact=name, active=True).exists():
            raise forms.ValidationError("A service with this name already exists. Choose another name.")
        return name

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        if not CODE_PATTERN.match(code):
            raise forms.ValidationError("Use only letters, numbers, - and _.")
        if Service.objects.filter(code__iexact=code).exists():
            raise forms.ValidationError("This code is already used by another service.")
        return code

    def clean(self):
        data = super().clean()
        if data.get("start") == "copy" and not data.get("source"):
            self.add_error("source", "Choose the service to copy.")
        if data.get("start") == "blank":
            data["source"] = None
        return data


def _inventory_id(service, index):
    base = f"TI-{service.pk}-{index}"
    candidate, suffix = base, 1
    while TaskInventory.objects.filter(external_id=candidate).exists():
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate[:30]


@transaction.atomic
def create_service(data, user):
    source = data.get("source")
    if source is not None:
        source = Service.objects.select_for_update().get(pk=source.pk)
    values = {}
    for field in COPIED_FIELDS:
        value = data.get(field)
        if value in (None, "") and source is not None:
            value = getattr(source, field)
        if value not in (None, ""):
            values[field] = value
    service = Service.objects.create(
        code=data["code"], name=data["name"], notes=source.notes if source else "", **values,
    )
    if source is None:
        record_version(service, [], "EDITOR", user=user, note="Created from scratch")
        return service
    steps = [{"code": step["code"], "mandatory": step["mandatory"], "can_skip": step["can_skip"],
              "tasks": step["tasks"]} for step in current_flow(source)]
    apply_flow(service, steps)
    for index, item in enumerate(source.task_inventory.filter(active=True).order_by("id"), 1):
        TaskInventory.objects.create(
            external_id=_inventory_id(service, index), task=item.task, service=service, inventory=item.inventory,
            standard_quantity=item.standard_quantity, unit=item.unit, waste_percent=item.waste_percent,
            effective_quantity=item.effective_quantity, mapping_status="Copied", notes=item.notes,
        )
    record_version(service, current_flow(service), "EDITOR", user=user, note=f"Created as a copy of {source.name}")
    return service


@roles_required("ADMIN")
@require_http_methods(["GET", "POST"])
def service_create(request):
    initial = {"code": suggest_code()}
    raw = request.GET.get("from", "")
    if raw.isdigit() and Service.objects.filter(pk=int(raw), active=True).exists():
        initial.update(start="copy", source=int(raw))
    elif request.GET.get("start") == "blank":
        initial["start"] = "blank"
    form = ServiceCreateForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            service = create_service(form.cleaned_data, request.user)
        except IntegrityError:
            form.add_error("code", "This code was just taken. Try another one.")
        else:
            source = form.cleaned_data.get("source")
            if source:
                messages.success(request, f"{service.name} was created as a copy of {source.name}. Change its steps below; {source.name} is not affected.")
            else:
                messages.success(request, f"{service.name} was created. Add its steps below.")
            return redirect(f"{reverse('service_catalog')}?service={service.pk}")
    sources = {
        str(item.pk): {field: str(getattr(item, field)) for field in COPIED_FIELDS} | {"steps": steps}
        for item, steps in _sources_with_step_counts()
    }
    return render(request, "operations/service_create.html", {"form": form, "sources": sources})


def _sources_with_step_counts():
    services = list(Service.objects.filter(active=True))
    flows = all_flows([item.pk for item in services])
    return [(item, len(flows.get(item.pk, []))) for item in services]
