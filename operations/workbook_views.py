"""Admin-only, CSRF-protected SOP imports without server-shell access."""
import logging

from django import forms
from django.contrib import messages
from django.db import DatabaseError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .decorators import roles_required
from .models import SOPWorkbookImport
from .sop_exports import build_export
from .sop_imports import MAX_UPLOAD, ImportProblem, apply_workbook, expire_stages, master_snapshot, stage_workbook

logger = logging.getLogger(__name__)


class WorkbookUploadForm(forms.Form):
    workbook = forms.FileField(label="SOP workbook (.xlsx)", widget=forms.ClearableFileInput(attrs={
        "accept": ".xlsx", "class": "form-control",
    }))

    def clean_workbook(self):
        upload = self.cleaned_data["workbook"]
        if not upload.name.lower().endswith(".xlsx") or upload.size > MAX_UPLOAD:
            raise forms.ValidationError("Choose an .xlsx workbook no larger than 10 MB.")
        return upload


class WorkbookConfirmForm(forms.Form):
    confirmed = forms.BooleanField(label="I reviewed the preview and approve updating global SOP master data.")


@roles_required("ADMIN")
@never_cache
@require_http_methods(["GET", "POST"])
def workbook_import(request):
    expire_stages()
    form = WorkbookUploadForm()
    confirm_form = WorkbookConfirmForm()
    stage = None
    # Query string and hidden UUID must always belong to this authenticated admin.
    identifier = request.POST.get("stage") if request.method == "POST" else request.GET.get("stage")
    if identifier:
        try:
            stage = get_object_or_404(
                SOPWorkbookImport.objects.defer("workbook", "master_snapshot"),
                pk=identifier, created_by=request.user,
            )
        except (ValueError, forms.ValidationError):
            return HttpResponseBadRequest("Invalid import reference.")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "validate":
            form = WorkbookUploadForm(request.POST, request.FILES)
            if form.is_valid():
                upload = form.cleaned_data["workbook"]
                try:
                    stage = stage_workbook(request.user, upload.name, upload.read(MAX_UPLOAD + 1))
                except ImportProblem as exc:
                    form.add_error("workbook", str(exc))
                except DatabaseError:
                    form.add_error("workbook", "Master data is busy. Wait a moment and validate again.")
                except Exception:
                    # Never log uploaded contents, paths or raw SQL/error strings.
                    logger.error("SOP upload validation failed for administrator %s", request.user.pk)
                    form.add_error("workbook", "Unable to read this workbook. Check the format and try again.")
                else:
                    return redirect(f"{request.path}?stage={stage.pk}")
        elif action == "confirm" and stage:
            confirm_form = WorkbookConfirmForm(request.POST)
            if confirm_form.is_valid():
                try:
                    apply_workbook(stage.pk, request.user)
                except ImportProblem as exc:
                    messages.error(request, str(exc))
                except DatabaseError:
                    messages.error(request, "Master data is busy. Nothing was imported; try again shortly.")
                except Exception:
                    logger.error("SOP import failed for administrator %s", request.user.pk)
                    messages.error(request, "Import failed and was rolled back. No partial updates were saved.")
                else:
                    messages.success(request, "SOP workbook imported. Existing visit task snapshots were not changed.")
                    return redirect("workbook_import")
        elif action == "cancel" and stage:
            SOPWorkbookImport.objects.filter(pk=stage.pk, created_by=request.user, status="PENDING").update(
                status="CANCELLED", workbook=b"",
            )
            messages.info(request, "Workbook discarded; master data was not changed.")
            return redirect("workbook_import")
        else:
            return HttpResponseBadRequest("Choose validate, confirm or cancel.")
    history = SOPWorkbookImport.objects.filter(created_by=request.user).defer(
        "workbook", "master_snapshot",
    )[:10]
    return render(request, "operations/workbook_import.html", {
        "form": form, "confirm_form": confirm_form, "stage": stage, "history": history,
    })


@roles_required("ADMIN")
@never_cache
@require_GET
def workbook_snapshot(request, import_id):
    entry = get_object_or_404(SOPWorkbookImport, pk=import_id, created_by=request.user, status="APPLIED")
    response = HttpResponse(bytes(entry.master_snapshot), content_type="application/gzip")
    response["Content-Disposition"] = f'attachment; filename="sop-master-before-{entry.pk}.json.gz"'
    return response


@roles_required("ADMIN")
@never_cache
@require_GET
def workbook_export(request):
    """The live SOP master data as a workbook that can be edited and uploaded again."""
    filename, payload = build_export(master_snapshot())
    response = HttpResponse(
        payload, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
