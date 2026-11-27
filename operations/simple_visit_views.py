"""Simple visit flow: manager assigns staff per category, staff tap a type card."""
from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .customer_types import (
    CATEGORIES, UNKNOWN, concern_codes, enabled_categories, option_codes, simple_flow_enabled, type_image, type_label,
)
from .decorators import roles_required
from .models import Branch, Customer, Visit, VisitAssignment
from .roster import working_branch, working_employees

OPEN_VISIT = ['WAITING', 'ASSIGNED', 'IN_PROGRESS', 'EMPLOYEE_DONE']


def _require_simple_flow():
    if not simple_flow_enabled():
        raise Http404('The simple visit flow is turned off.')


def staff_label(user):
    return user.get_full_name() or user.username


class StaffChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return staff_label(obj)


class CategoryAssignmentForm(forms.Form):
    """One optional staff dropdown per enabled category."""

    def __init__(self, *args, branch, visit, **kwargs):
        super().__init__(*args, **kwargs)
        self.existing = {item.category: item for item in visit.category_assignments.exclude(status='CANCELLED')}
        staff = working_employees(branch).order_by('first_name', 'username')
        self.rows = []
        for key in enabled_categories():
            current = self.existing.get(key)
            queryset = User.objects.filter(Q(pk__in=staff.values('pk')) | Q(pk=getattr(current, 'employee_id', None))).distinct()
            field = StaffChoiceField(
                queryset=queryset.order_by('first_name', 'username'), required=False,
                empty_label='No ' + CATEGORIES[key]['label'].lower() + ' service', label=CATEGORIES[key]['label'],
                initial=getattr(current, 'employee_id', None),
            )
            field.widget.attrs['class'] = 'form-control'
            locked = bool(current and current.status == 'DONE')
            field.disabled = locked
            self.fields[key] = field
            self.rows.append({'key': key, 'label': CATEGORIES[key]['label'], 'field': None, 'current': current,
                              'locked': locked, 'image': CATEGORIES[key]['options'][0][3]})

    def clean(self):
        data = super().clean()
        if not any(data.get(key) for key in self.fields):
            raise forms.ValidationError('Choose a staff member for at least one category.')
        return data


def _visit_status(visit):
    active = visit.category_assignments.exclude(status='CANCELLED')
    if active.exists() and not active.exclude(status='DONE').exists():
        return 'EMPLOYEE_DONE'
    if active.filter(status='DONE').exists():
        return 'IN_PROGRESS'
    return 'ASSIGNED' if active.exists() else 'WAITING'


@roles_required('MANAGER')
@require_http_methods(['GET', 'POST'])
def assign_staff(request, visit_id):
    _require_simple_flow()
    branch = working_branch(request.user)
    visit = get_object_or_404(Visit.objects.select_related('customer'), pk=visit_id, branch=branch)
    if visit.status not in OPEN_VISIT:
        messages.error(request, 'Staff cannot be changed after the invoice is added.')
        return redirect('manager_dashboard')
    form = CategoryAssignmentForm(request.POST or None, branch=branch, visit=visit)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            Visit.objects.select_for_update().get(pk=visit.pk)
            now = timezone.now()
            for key in form.fields:
                current = form.existing.get(key)
                if current and current.status == 'DONE':
                    continue
                employee = form.cleaned_data.get(key)
                if not employee:
                    if current:
                        current.delete()
                    continue
                if current:
                    if current.employee_id != employee.pk:
                        current.employee, current.assigned_by, current.assigned_at = employee, request.user, now
                        current.save(update_fields=['employee', 'assigned_by', 'assigned_at', 'updated_at'])
                else:
                    VisitAssignment.objects.update_or_create(
                        visit=visit, category=key,
                        defaults={'employee': employee, 'assigned_by': request.user, 'assigned_at': now,
                                  'status': 'ASSIGNED', 'selected_type': '', 'completed_at': None},
                    )
            visit.status = _visit_status(visit)
            visit.save(update_fields=['status', 'updated_at'])
        messages.success(request, f'Staff assigned for {visit.customer.name}.')
        return redirect('manager_dashboard')
    for row in form.rows:
        row['field'] = form[row['key']]
        row['previous'] = type_label(row['key'], getattr(visit.customer, CATEGORIES[row['key']]['customer_field']))
    return render(request, 'operations/simple_assign.html', {'form': form, 'visit': visit})


def _my_assignments(user):
    branch = working_branch(user)
    if branch is None:
        return VisitAssignment.objects.none()
    return VisitAssignment.objects.filter(
        employee=user, visit__branch=branch, visit__status__in=OPEN_VISIT,
    ).exclude(status='CANCELLED').select_related('visit__customer')


def employee_cards(user):
    """Cards for the staff home screen: waiting first, then today's recorded ones."""
    cards = list(_my_assignments(user).order_by('visit__created_at', 'id'))
    for card in cards:
        card.image = type_image(card.category, card.selected_type) if card.selected_type else CATEGORIES[card.category]['options'][0][3]
        previous = getattr(card.visit.customer, CATEGORIES[card.category]['customer_field'])
        card.previous_label = type_label(card.category, previous)
    return [c for c in cards if c.status == 'ASSIGNED'], [c for c in cards if c.status == 'DONE']


@roles_required('EMPLOYEE')
@require_http_methods(['GET', 'POST'])
def pick_type(request, pk):
    _require_simple_flow()
    assignment = get_object_or_404(_my_assignments(request.user), pk=pk)
    category = CATEGORIES[assignment.category]
    customer = assignment.visit.customer
    previous = getattr(customer, category['customer_field'])
    saved_concerns = assignment.concerns if assignment.status == 'DONE' else (customer.concerns or {}).get(assignment.category, [])
    if request.method == 'POST':
        choice = request.POST.get('type', '').strip().upper()
        if choice not in option_codes(assignment.category):
            messages.error(request, 'Tap one of the picture cards.')
            return redirect('pick_type', pk=pk)
        allowed = concern_codes(assignment.category)
        concerns = [code for code in dict.fromkeys(request.POST.getlist('concern')) if code in allowed]
        with transaction.atomic():
            visit = Visit.objects.select_for_update().get(pk=assignment.visit_id)
            assignment.selected_type, assignment.status, assignment.concerns = choice, 'DONE', concerns
            assignment.completed_at = timezone.now()
            assignment.save(update_fields=['selected_type', 'status', 'concerns', 'completed_at', 'updated_at'])
            customer = Customer.objects.select_for_update().get(pk=customer.pk)
            fields = ['concerns', 'updated_at']
            if choice != UNKNOWN:
                setattr(customer, category['customer_field'], choice)
                fields.append(category['customer_field'])
            customer.concerns = {**(customer.concerns or {}), assignment.category: concerns}
            customer.save(update_fields=fields)
            visit.status = _visit_status(visit)
            visit.save(update_fields=['status', 'updated_at'])
        messages.success(request, f"{customer.name}: {category['label'].lower()} type saved as {type_label(assignment.category, choice)}.")
        return redirect('employee_dashboard')
    chosen = assignment.selected_type or previous
    options = [{'code': code, 'label': label, 'hint': hint, 'image': image, 'selected': code == chosen}
               for code, label, hint, image in category['options']]
    concerns = [{'code': code, 'label': label, 'checked': code in saved_concerns} for code, label in category['concerns']]
    return render(request, 'operations/pick_type.html', {
        'assignment': assignment, 'category': category, 'customer': customer, 'options': options,
        'concerns': concerns, 'unsure_selected': chosen == UNKNOWN,
        'previous_label': type_label(assignment.category, previous),
        'visit_count': customer.visits.count(),
    })


def unassigned_visits(user):
    """Waiting customers the manager has not assigned yet; staff may take them."""
    branch = working_branch(user)
    if branch is None:
        return []
    visits = (Visit.objects.filter(branch=branch, status='WAITING').select_related('customer')
              .exclude(category_assignments__status__in=['ASSIGNED', 'DONE']).exclude(services__isnull=False)
              .distinct().order_by('created_at', 'id'))
    categories = [{'key': key, 'label': CATEGORIES[key]['label']} for key in enabled_categories()]
    return [{'visit': visit, 'categories': categories} for visit in visits]


@roles_required('EMPLOYEE')
@require_POST
def take_customer(request, visit_id, category):
    """Self-assign before the service starts when the manager has not assigned anyone."""
    _require_simple_flow()
    category = category.upper()
    if category not in enabled_categories():
        raise Http404
    branch = working_branch(request.user)
    try:
        with transaction.atomic():
            visit = get_object_or_404(Visit.objects.select_for_update(), pk=visit_id, branch=branch)
            taken = visit.category_assignments.exclude(status='CANCELLED').exists() or visit.services.exists()
            if visit.status != 'WAITING' or taken:
                messages.info(request, 'Someone has already been assigned to this customer.')
                return redirect('employee_dashboard')
            assignment = VisitAssignment.objects.create(
                visit=visit, category=category, employee=request.user, assigned_by=request.user,
            )
            visit.status = 'ASSIGNED'
            visit.save(update_fields=['status', 'updated_at'])
    except IntegrityError:
        messages.info(request, 'Someone has already been assigned to this customer.')
        return redirect('employee_dashboard')
    messages.success(request, f"You took {visit.customer.name} for {CATEGORIES[category]['label'].lower()}.")
    return redirect('pick_type', pk=assignment.pk)


@roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER')
def customer_types(request):
    """How many customers of each type each branch has served (latest pick per visit)."""
    _require_simple_flow()
    profile = getattr(request.user, 'profile', None)
    branches = Branch.objects.filter(active=True).order_by('name')
    if not request.user.is_superuser and profile and profile.role == 'MANAGER':
        branches = branches.filter(pk=getattr(working_branch(request.user), 'pk', None))
    chosen = request.GET.get('branch', '')
    if chosen.isdigit() and branches.filter(pk=int(chosen)).exists():
        branches = branches.filter(pk=int(chosen))
    done = VisitAssignment.objects.filter(status='DONE', visit__branch__in=branches)
    sections = []
    for key in enabled_categories():
        rows = {r['selected_type']: r['customers'] for r in done.filter(category=key).values('selected_type')
                .annotate(customers=Count('visit__customer', distinct=True))}
        total = sum(rows.values()) or 0
        options = [(code, label, image) for code, label, _hint, image in CATEGORIES[key]['options']]
        options.append((UNKNOWN, 'Not sure', 'img/types/unknown.svg'))
        items = [{'label': label, 'image': image, 'count': rows.get(code, 0),
                  'percent': round(rows.get(code, 0) * 100 / total) if total else 0} for code, label, image in options]
        concern_counts = {}
        for codes in done.filter(category=key).values_list('concerns', flat=True):
            for code in codes or []:
                concern_counts[code] = concern_counts.get(code, 0) + 1
        concerns = sorted(({'label': label, 'count': concern_counts.get(code, 0)} for code, label in CATEGORIES[key]['concerns']),
                          key=lambda item: -item['count'])
        sections.append({'label': CATEGORIES[key]['label'], 'items': items, 'total': total, 'concerns': concerns})
    return render(request, 'operations/customer_types.html', {
        'sections': sections, 'branches': Branch.objects.filter(pk__in=branches.values('pk')) if chosen else branches,
        'all_branches': Branch.objects.filter(active=True).order_by('name') if not (profile and profile.role == 'MANAGER') else [],
        'chosen': chosen,
        'customers_total': Customer.objects.filter(visits__branch__in=branches).distinct().count(),
        'customers_typed': Customer.objects.filter(visits__branch__in=branches).exclude(hair_type='', skin_type='', body_type='').distinct().count(),
    })
