"""Central import navigation and administrator-only branch/chair onboarding."""
import csv
import io
import logging
from django import forms
from django.contrib import messages
from django.db import DatabaseError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods
from .branch_imports import MAX_BRANCH_UPLOAD, BranchImportProblem, apply_branch_import, expire_branch_imports, stage_branch_import
from .decorators import roles_required
from .forms import BootstrapMixin
from .models import BranchCSVImport
from .staff_import_views import StaffCSVConfirmForm

logger = logging.getLogger(__name__)


@roles_required('ADMIN', 'GENERAL_MANAGER')
@never_cache
@require_GET
def import_data(request):
    return render(request, 'operations/import_data.html', {
        'can_import_master': request.user.is_superuser or request.user.profile.role == 'ADMIN',
    })


class BranchUploadForm(BootstrapMixin, forms.Form):
    csv_file = forms.FileField(label='Branches and chairs CSV', widget=forms.ClearableFileInput(attrs={'accept': '.csv'}))

    def clean_csv_file(self):
        upload = self.cleaned_data['csv_file']
        if not upload.name.lower().endswith('.csv') or upload.size > MAX_BRANCH_UPLOAD:
            raise forms.ValidationError('Choose a .csv file no larger than 512 KB.')
        return upload


class BranchConfirmForm(StaffCSVConfirmForm):
    confirmed = forms.BooleanField(label='I reviewed the branch and chair changes and approve this import.')


@roles_required('ADMIN')
@never_cache
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
def branch_csv_import(request):
    expire_branch_imports()
    form, confirm_form = BranchUploadForm(), BranchConfirmForm(actor=request.user)
    stage = None
    identifier = request.POST.get('stage') if request.method == 'POST' else request.GET.get('stage')
    if identifier:
        try:
            stage = get_object_or_404(BranchCSVImport, pk=identifier, created_by=request.user)
        except (ValueError, forms.ValidationError):
            return HttpResponseBadRequest('Invalid import reference.')
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'validate':
            form = BranchUploadForm(request.POST, request.FILES)
            if form.is_valid():
                upload = form.cleaned_data['csv_file']
                try:
                    stage = stage_branch_import(request.user, upload.name, upload.read(MAX_BRANCH_UPLOAD + 1))
                except BranchImportProblem as exc:
                    form.add_error('csv_file', str(exc))
                except DatabaseError:
                    form.add_error('csv_file', 'Branch data is busy. Nothing was imported; validate again shortly.')
                except Exception:
                    logger.error('Branch CSV validation failed for operator %s', request.user.pk)
                    form.add_error('csv_file', 'Unable to read this CSV. Check the template and try again.')
                else:
                    return redirect(f'{request.path}?stage={stage.pk}')
        elif action == 'confirm' and stage:
            confirm_form = BranchConfirmForm(request.POST, actor=request.user)
            if confirm_form.is_valid():
                try:
                    apply_branch_import(stage.pk, request.user)
                except BranchImportProblem as exc:
                    messages.error(request, str(exc))
                except DatabaseError:
                    messages.error(request, 'Import failed and was rolled back. Nothing was imported; try again shortly.')
                except Exception:
                    logger.error('Branch import failed for operator %s', request.user.pk)
                    messages.error(request, 'Import failed and was rolled back. No partial updates were saved.')
                else:
                    messages.success(request, 'Branches and chairs imported. Existing staff, rosters and visit records were not changed.')
                    return redirect(f'{request.path}?stage={stage.pk}')
                stage.refresh_from_db()
        elif action == 'cancel' and stage:
            discarded = BranchCSVImport.objects.filter(pk=stage.pk, created_by=request.user, status='PENDING').update(status='CANCELLED', plan={})
            messages.info(request, 'Preview discarded. Branches and chairs were not changed.' if discarded else 'This preview is no longer pending. No changes were made.')
            return redirect('branch_csv_import')
        else:
            return HttpResponseBadRequest('Choose validate, confirm or cancel.')
    return render(request, 'operations/branch_csv_import.html', {
        'form': form, 'confirm_form': confirm_form, 'stage': stage,
        'history': BranchCSVImport.objects.filter(created_by=request.user).defer('plan')[:10],
    })


@roles_required('ADMIN')
@never_cache
@require_GET
def branch_csv_template(request):
    output = io.StringIO(newline='')
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['BRANCH_CODE', 'BRANCH_NAME', 'LOCATION', 'PHONE', 'CHAIR_CODE', 'CHAIR_NAME', 'STORE_CODE'])
    writer.writerows([
        ['EXAMPLE', 'Example branch', 'Example location', '', 'C01', 'Chair 1', 'HS-001'],
        ['EXAMPLE', 'Example branch', 'Example location', '', 'C02', 'Chair 2', 'HS-001'],
    ])
    response = HttpResponse(output.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="branch-chair-upload-template.csv"'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
