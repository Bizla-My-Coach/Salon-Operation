"""Simple visit flow: name and phone, assign staff, staff tap type cards.

No invoice, review or verification: the visit closes by itself once every
assigned staff member has saved their card.
"""
from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .customer_types import (
    CATEGORIES, NAILS, NAILS_IMAGE, NAILS_LABEL, UNKNOWN, assign_choices, concern_codes, enabled_categories,
    option_codes, simple_flow_enabled, type_image, type_label,
)
from .decorators import roles_required
from .models import Branch, Customer, Visit, VisitAssignment
from .roster import working_branch, working_employees

OPEN_VISIT = ['WAITING', 'ASSIGNED', 'IN_PROGRESS']
ANY_IMAGE = 'img/types/any.png'


def _require_simple_flow():
    if not simple_flow_enabled():
        raise Http404('The simple visit flow is turned off.')


def staff_label(user):
    return user.get_full_name() or user.username


def card_image(card):
    if card.selected_type:
        return type_image(card.category, card.selected_type)
    if card.category == NAILS:
        return NAILS_IMAGE
    if card.category in CATEGORIES:
        return CATEGORIES[card.category]['options'][0][3]
    return ANY_IMAGE


def _settle_visit(visit):
    """Set the visit status from its cards; close it once every card is saved."""
    cards = visit.category_assignments.exclude(status='CANCELLED')
    now = timezone.now()
    if cards.exists() and not cards.exclude(status='DONE').exists():
        visit.status, visit.closed_at = 'CLOSED', now
    elif cards.filter(status='DONE').exists():
        visit.status, visit.closed_at = 'IN_PROGRESS', None
    else:
        visit.status, visit.closed_at = ('ASSIGNED' if cards.exists() else 'WAITING'), None
    visit.save(update_fields=['status', 'closed_at', 'updated_at'])


@roles_required('MANAGER')
@require_http_methods(['GET', 'POST'])
def assign_staff(request, visit_id):
    """Tick one or more staff; the category per staff member is optional."""
    _require_simple_flow()
    branch = working_branch(request.user)
    visit = get_object_or_404(Visit.objects.select_related('customer'), pk=visit_id, branch=branch)
    if visit.status not in OPEN_VISIT:
        messages.error(request, 'This visit is already finished.')
        return redirect('manager_dashboard')
    categories = [choice['key'] for choice in assign_choices()]
    current = {}
    for card in visit.category_assignments.exclude(status='CANCELLED').select_related('employee').order_by('id'):
        current.setdefault(card.employee_id, card)
    staff = list(working_employees(branch).order_by('first_name', 'username'))
    staff_ids = {person.pk for person in staff}
    staff += [card.employee for card in current.values() if card.employee_id not in staff_ids]
    error = ''
    if request.method == 'POST':
        chosen = {}
        for person in staff:
            if request.POST.get(f'staff_{person.pk}'):
                category = request.POST.get(f'category_{person.pk}', '').upper()
                chosen[person.pk] = (person, category if category in categories else '')
        locked = {pk for pk, card in current.items() if card.status == 'DONE'}
        if not chosen and not locked:
            error = 'Tick at least one staff member.'
        else:
            with transaction.atomic():
                visit = Visit.objects.select_for_update().get(pk=visit.pk)
                for pk, card in current.items():
                    if card.status == 'ASSIGNED' and pk not in chosen:
                        card.delete()
                for pk, (person, category) in chosen.items():
                    card = current.get(pk)
                    if card and card.status == 'DONE':
                        continue
                    if card:
                        if card.category != category:
                            card.category = category
                            card.save(update_fields=['category', 'updated_at'])
                    else:
                        VisitAssignment.objects.create(visit=visit, category=category, employee=person,
                                                       assigned_by=request.user)
                _settle_visit(visit)
            messages.success(request, f'{visit.customer.name} sent to {len(chosen) or len(locked)} staff.')
            return redirect('manager_dashboard')
    rows = []
    for person in staff:
        card = current.get(person.pk)
        rows.append({
            'person': person, 'name': staff_label(person), 'card': card,
            'checked': bool(card) if request.method == 'GET' else bool(request.POST.get(f'staff_{person.pk}')),
            'category': (card.category if card else '') if request.method == 'GET' else request.POST.get(f'category_{person.pk}', ''),
            'locked': bool(card and card.status == 'DONE'),
        })
    return render(request, 'operations/simple_assign.html', {
        'visit': visit, 'rows': rows, 'error': error,
        'categories': assign_choices(),
    })


def _my_assignments(user):
    branch = working_branch(user)
    if branch is None:
        return VisitAssignment.objects.none()
    return VisitAssignment.objects.filter(
        employee=user, visit__branch=branch, visit__status__in=OPEN_VISIT,
    ).exclude(status='CANCELLED').select_related('visit__customer')


def employee_cards(user):
    """Cards waiting for this staff member, then the ones they saved today."""
    branch = working_branch(user)
    waiting = list(_my_assignments(user).filter(status='ASSIGNED').order_by('visit__created_at', 'id'))
    saved = list(VisitAssignment.objects.filter(
        employee=user, visit__branch=branch, status='DONE', completed_at__date=timezone.localdate(),
    ).select_related('visit__customer').order_by('-completed_at')[:20]) if branch else []
    cards = waiting + saved
    for card in cards:
        card.can_change = card.visit.status in OPEN_VISIT
        card.image = card_image(card)
        previous = getattr(card.visit.customer, CATEGORIES[card.category]['customer_field']) if card.category in CATEGORIES else ''
        card.previous_label = type_label(card.category, previous)
    return waiting, saved


@roles_required('EMPLOYEE')
@require_http_methods(['GET', 'POST'])
def pick_type(request, pk):
    _require_simple_flow()
    assignment = get_object_or_404(_my_assignments(request.user), pk=pk)
    customer = assignment.visit.customer
    if assignment.category not in CATEGORIES or request.GET.get('change') == 'category' and assignment.status == 'ASSIGNED':
        return _pick_category(request, assignment, customer)
    category = CATEGORIES[assignment.category]
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
            another = None
            if request.POST.get('another'):
                # Keep the visit open for one more card, e.g. skin after hair.
                another = VisitAssignment.objects.create(visit=visit, employee=request.user, assigned_by=request.user)
            _settle_visit(visit)
        if another:
            messages.success(request, f"{category['label']} saved. Now pick the next type for {customer.name}.")
            return redirect('pick_type', pk=another.pk)
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
        'can_change_category': assignment.status == 'ASSIGNED',
    })


def _pick_category(request, assignment, customer):
    """The category is still open: staff tap Hair, Skin or Mani / Pedi first.

    Mani / Pedi then asks Manicure, Pedicure or Both; Both adds a second card
    for the same staff member so hands and feet are recorded separately.
    """
    nails_step = assignment.category == NAILS and request.GET.get('change') != 'category'
    if request.method == 'POST':
        choice = request.POST.get('category', '').upper()
        allowed = {'MANI', 'PEDI', 'BOTH'} if nails_step else {item['key'] for item in assign_choices()}
        if choice not in allowed or choice in ('MANI', 'PEDI', 'BOTH') and 'MANI' not in enabled_categories():
            messages.error(request, 'Tap one of the pictures.')
            return redirect('pick_type', pk=assignment.pk)
        with transaction.atomic():
            if choice == 'BOTH':
                VisitAssignment.objects.create(visit=assignment.visit, category='PEDI', employee=request.user,
                                               assigned_by=assignment.assigned_by)
                choice = 'MANI'
            assignment.category = choice
            assignment.save(update_fields=['category', 'updated_at'])
        return redirect('pick_type', pk=assignment.pk)
    if nails_step:
        choices = [
            {'key': 'MANI', 'label': 'Manicure', 'image': 'img/types/hand-normal.png', 'note': 'Hands'},
            {'key': 'PEDI', 'label': 'Pedicure', 'image': 'img/types/foot-normal.png', 'note': 'Feet'},
            {'key': 'BOTH', 'label': 'Both', 'image': NAILS_IMAGE, 'note': 'Hands, then feet'},
        ]
        for item in choices[:2]:
            previous = type_label(item['key'], getattr(customer, CATEGORIES[item['key']]['customer_field']))
            if previous:
                item['note'] = f'Last time: {previous}'
        question = 'Manicure, pedicure or both?'
    else:
        choices = []
        for item in assign_choices():
            note = ''
            if item['key'] in CATEGORIES:
                previous = type_label(item['key'], getattr(customer, CATEGORIES[item['key']]['customer_field']))
                note = f'Last time: {previous}' if previous else ''
            choices.append({**item, 'note': note or 'Pick the type next'})
        question = 'What are you doing for this customer?'
    return render(request, 'operations/pick_category.html', {
        'assignment': assignment, 'customer': customer, 'choices': choices, 'question': question,
        'visit_count': customer.visits.count(),
    })


def unassigned_visits(user):
    """Waiting customers the manager has not assigned yet; staff may take them."""
    branch = working_branch(user)
    if branch is None:
        return []
    return list(Visit.objects.filter(branch=branch, status='WAITING').select_related('customer')
                .exclude(category_assignments__status__in=['ASSIGNED', 'DONE']).exclude(services__isnull=False)
                .distinct().order_by('created_at', 'id'))


@roles_required('EMPLOYEE')
@require_POST
def take_customer(request, visit_id):
    """Self-assign before the service starts when the manager has not assigned anyone."""
    _require_simple_flow()
    branch = working_branch(request.user)
    with transaction.atomic():
        visit = get_object_or_404(Visit.objects.select_for_update(), pk=visit_id, branch=branch)
        taken = visit.category_assignments.exclude(status='CANCELLED').exists() or visit.services.exists()
        if visit.status != 'WAITING' or taken:
            messages.info(request, 'Someone has already been assigned to this customer.')
            return redirect('employee_dashboard')
        assignment = VisitAssignment.objects.create(visit=visit, employee=request.user, assigned_by=request.user)
        _settle_visit(visit)
    messages.success(request, f'You took {visit.customer.name}.')
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
        'customers_typed': Customer.objects.filter(visits__branch__in=branches).exclude(hair_type='', skin_type='', hand_type='', foot_type='').distinct().count(),
    })
