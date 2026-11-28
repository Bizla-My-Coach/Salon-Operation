"""Simple visit flow: name and phone, assign staff, staff tap type cards.

No invoice, review or verification: the visit closes by itself once every
assigned staff member has saved their card.
"""
import re

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .customer_types import (
    CATEGORIES, NAILS, NAILS_IMAGE, UNKNOWN, assign_choices, concern_codes, enabled_categories,
    option_codes, simple_flow_enabled, type_image, type_label,
)
from .decorators import roles_required
from .models import Branch, Customer, Visit, VisitAssignment
from .roster import working_branch, working_employees
from .staff_names import display_names

OPEN_VISIT = ['WAITING', 'ASSIGNED', 'IN_PROGRESS']
ANY_IMAGE = 'img/types/any.png'


def _require_simple_flow():
    if not simple_flow_enabled():
        raise Http404('The simple visit flow is turned off.')


def staff_label(user):
    return user.get_full_name() or user.username


def card_image(card, show_type=True):
    if show_type and card.selected_type:
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


def people_rows(visit, cards, skip_empty=False):
    """Cards grouped by person for the manager board: customer first, then family / friends."""
    people = [visit.customer] + sorted(visit.guests.all(), key=lambda guest: guest.pk)
    rows = [{'name': person.name, 'pk': person.pk, 'cards': []} for person in people]
    by_pk = {row['pk']: row for row in rows}
    for card in cards:
        by_pk.setdefault(card.customer_id or visit.customer_id, rows[0])['cards'].append(card)
    return [row for row in rows if row['cards'] or not skip_empty]


def _group(category):
    """The manager-level choice a card belongs to: HAIR, SKIN, NAILS or '' (any)."""
    return NAILS if category in (NAILS, 'MANI', 'PEDI') else category


NEW_KEY = re.compile(r'^n\d{1,3}$')


def visit_people(visit):
    """The main customer first, then family / friends in the order they were added."""
    return [visit.customer] + list(visit.guests.order_by('id'))


def _guest_name(raw, number):
    return (raw or '').strip()[:120] or f'Guest {number}'


def _add_guest(visit, raw_name):
    """Add a family member or friend; a named person seen before on this phone is reused."""
    number = visit.guests.count() + 1
    name = _guest_name(raw_name, number)
    main = visit.customer
    guest = None
    if (raw_name or '').strip() and main.mobile:
        guest = (Customer.objects.filter(mobile=main.mobile, name__iexact=name).exclude(pk=main.pk)
                 .exclude(pk__in=visit.guests.values('pk')).order_by('-id').first())
    if guest is None:
        guest = Customer.objects.create(name=name, mobile=main.mobile)
    visit.guests.add(guest)
    return guest


def _new_keys(request):
    return [key for key in dict.fromkeys(request.POST.getlist('new_person')) if NEW_KEY.match(key)]


@roles_required('MANAGER')
@require_http_methods(['GET', 'POST'])
def assign_staff(request, visit_id):
    """Assign staff for each person on the visit: the customer plus any family / friends.

    Each staff tile can take several categories, or none (staff then choose).
    """
    _require_simple_flow()
    branch = working_branch(request.user)
    visit = get_object_or_404(Visit.objects.select_related('customer'), pk=visit_id, branch=branch)
    if visit.status not in OPEN_VISIT:
        messages.error(request, 'This visit is already finished.')
        return redirect('manager_dashboard')
    categories = [choice['key'] for choice in assign_choices()]
    people = visit_people(visit)
    cards = {}
    for card in visit.category_assignments.exclude(status='CANCELLED').select_related('employee').order_by('id'):
        cards.setdefault((card.customer_id or visit.customer_id, card.employee_id), []).append(card)
    staff = list(working_employees(branch).order_by('first_name', 'username'))
    staff_ids = {person.pk for person in staff}
    for items in cards.values():
        if items[0].employee_id not in staff_ids:
            staff.append(items[0].employee)
            staff_ids.add(items[0].employee_id)
    keys = [(f'p{person.pk}', person) for person in people]
    error = ''
    if request.method == 'POST':
        keys += [(key, None) for key in _new_keys(request)]
        removed = set(request.POST.getlist('remove')) - {keys[0][0]}
        chosen = {}
        for key, person in keys:
            if key in removed:
                continue
            for employee in staff:
                picked = [c for c in dict.fromkeys(request.POST.getlist(f'category_{key}_{employee.pk}')) if c in categories]
                # Tapping a category button on a tile also selects that staff member.
                if request.POST.get(f'staff_{key}_{employee.pk}') or picked:
                    chosen.setdefault(key, {})[employee.pk] = (employee, picked)
        has_done = any(card.status == 'DONE' for items in cards.values() for card in items)
        if not chosen and not has_done:
            error = 'Tap at least one staff member.'
        else:
            with transaction.atomic():
                visit = Visit.objects.select_for_update().get(pk=visit.pk)
                for key, person in keys:
                    if person is not None and key in removed:
                        mine = [card for (cid, _e), items in cards.items() if cid == person.pk for card in items]
                        if not any(card.status == 'DONE' for card in mine):
                            for card in mine:
                                card.delete()
                            visit.guests.remove(person)
                        continue
                    if key in removed:
                        continue
                    raw_name = request.POST.get(f'name_{key}', '').strip()
                    if person is None:
                        if not chosen.get(key) and not raw_name:
                            continue
                        person = _add_guest(visit, raw_name)
                    elif person.pk != visit.customer_id and raw_name and raw_name != person.name:
                        person.name = raw_name[:120]
                        person.save(update_fields=['name', 'updated_at'])
                    picks = chosen.get(key, {})
                    for (cid, employee_id), items in cards.items():
                        if cid == person.pk and employee_id not in picks:
                            for card in items:
                                if card.status == 'ASSIGNED':
                                    card.delete()
                    for employee_id, (employee, picked) in picks.items():
                        items = cards.get((person.pk, employee_id), [])
                        covered = {_group(card.category) for card in items if card.status == 'DONE'}
                        if picked:
                            for card in items:
                                if card.status == 'ASSIGNED' and _group(card.category) not in picked:
                                    card.delete()
                                elif card.status == 'ASSIGNED':
                                    covered.add(_group(card.category))
                            for category in picked:
                                if category not in covered:
                                    VisitAssignment.objects.create(visit=visit, customer=person, category=category,
                                                                   employee=employee, assigned_by=request.user)
                        elif not items:
                            VisitAssignment.objects.create(visit=visit, customer=person, employee=employee,
                                                           assigned_by=request.user)
                _settle_visit(visit)
            total = len({employee_id for picks in chosen.values() for employee_id in picks})
            messages.success(request, f'{visit.customer.name} sent to {total} staff.')
            return redirect('manager_dashboard')
    names = display_names(staff)
    today = timezone.localdate()
    busy = dict(VisitAssignment.objects.filter(
        employee__in=staff, status='ASSIGNED', visit__branch=branch, assigned_at__date=today,
    ).exclude(visit=visit).values('employee').annotate(n=Count('visit', distinct=True)).values_list('employee', 'n'))
    ordered = sorted(staff, key=lambda p: names[p.pk].lower())

    def panel(key, person, number):
        rows = []
        for employee in ordered:
            items = cards.get((person.pk, employee.pk), []) if person else []
            if request.method == 'POST':
                picked = set(request.POST.getlist(f'category_{key}_{employee.pk}'))
                checked = bool(request.POST.get(f'staff_{key}_{employee.pk}') or picked)
            else:
                picked = {_group(card.category) for card in items if card.category}
                checked = bool(items)
            done = {_group(card.category) for card in items if card.status == 'DONE'}
            rows.append({
                'pk': employee.pk, 'name': names[employee.pk], 'busy': busy.get(employee.pk, 0),
                'initial': names[employee.pk][:1].upper(), 'checked': checked or bool(done),
                'saved': ', '.join(f'{card.category_label} {card.type_label}' for card in items if card.status == 'DONE'),
                'cats': [{**choice, 'checked': choice['key'] in picked or choice['key'] in done,
                          'locked': choice['key'] in done} for choice in assign_choices()],
            })
        is_main = person is not None and person.pk == visit.customer_id
        has_done = person is not None and any(
            card.status == 'DONE' for (cid, _e), items in cards.items() if cid == person.pk for card in items)
        typed = request.POST.get(f'name_{key}', '') if request.method == 'POST' else ''
        return {
            'key': key, 'main': is_main, 'new': person is None, 'number': number,
            'name': person.name if person else _guest_name(typed, number),
            'typed_name': typed if request.method == 'POST' else ('' if is_main or person is None else person.name),
            'can_remove': not is_main and not has_done, 'rows': rows,
            'count': sum(1 for row in rows if row['checked']),
        }
    first_new = visit.guests.count() + 1
    numbers = {key: first_new + i for i, key in enumerate(k for k, p in keys if p is None)}
    panels = [panel(key, person, numbers.get(key, 0)) for key, person in keys]
    blank = panel('__KEY__', None, 0)
    return render(request, 'operations/simple_assign.html', {
        'visit': visit, 'panels': panels, 'blank_panel': blank, 'error': error, 'show_search': len(ordered) > 6,
    })


def _my_assignments(user):
    branch = working_branch(user)
    if branch is None:
        return VisitAssignment.objects.none()
    return VisitAssignment.objects.filter(
        employee=user, visit__branch=branch, visit__status__in=OPEN_VISIT,
    ).exclude(status='CANCELLED').select_related('visit__customer', 'customer')


def _summary(cards, done):
    """One line per person, e.g. [("Sneha", "Hair, Skin"), ("Anvi", "Mani / Pedi")]."""
    people = {}
    for card in cards:
        label = f'{card.category_label} {card.type_label}' if done else (card.category_label if card.category else 'Choose')
        people.setdefault(card.person.pk, (card.person.name, []))[1].append(label)
    return [{'name': name, 'what': ', '.join(dict.fromkeys(labels))} for name, labels in people.values()]


def employee_cards(user):
    """One card per visit waiting for this staff member, then visits they finished today."""
    branch = working_branch(user)
    waiting_cards = list(_my_assignments(user).order_by('visit__created_at', 'id'))
    open_visits = {card.visit_id for card in waiting_cards if card.status == 'ASSIGNED'}
    waiting = {}
    for card in waiting_cards:
        if card.visit_id in open_visits:
            waiting.setdefault(card.visit_id, []).append(card)
    saved_cards = list(VisitAssignment.objects.filter(
        employee=user, visit__branch=branch, status='DONE', completed_at__date=timezone.localdate(),
    ).exclude(visit_id__in=open_visits).select_related('visit__customer', 'customer')
        .order_by('-completed_at')[:40]) if branch else []
    saved = {}
    for card in saved_cards:
        saved.setdefault(card.visit_id, []).append(card)

    def build(groups, done):
        result = []
        for cards in groups.values():
            visit = cards[0].visit
            first = cards[0]
            result.append({
                'visit': visit, 'image': card_image(first) if len(cards) == 1 else ANY_IMAGE,
                'people': _summary([c for c in cards if c.status == 'DONE'] if done else
                                   [c for c in cards if c.status == 'ASSIGNED'], done),
                'can_change': visit.status in OPEN_VISIT,
                'when': max(c.completed_at for c in cards if c.completed_at) if done else visit.created_at,
            })
        return result
    return build(waiting, False), build(saved, True)


@roles_required('EMPLOYEE')
def pick_type(request, pk):
    """Old card link: open the visit's single record page."""
    _require_simple_flow()
    assignment = get_object_or_404(_my_assignments(request.user), pk=pk)
    return redirect('record_visit', visit_id=assignment.visit_id)


def _category_choices():
    return [key for key in ('HAIR', 'SKIN', 'MANI', 'PEDI') if key in enabled_categories()]


@roles_required('EMPLOYEE')
@require_http_methods(['GET', 'POST'])
def record_visit(request, visit_id):
    """One page per visit: every person, every category, one Submit."""
    _require_simple_flow()
    mine_qs = _my_assignments(request.user).filter(visit_id=visit_id)
    if not mine_qs.exists():
        raise Http404('No card for you on this visit.')
    visit = mine_qs.first().visit
    people = visit_people(visit)
    all_cards = list(visit.category_assignments.exclude(status='CANCELLED').select_related('employee'))
    mine, others = {}, {}
    for card in all_cards:
        target = mine if card.employee_id == request.user.pk else others
        target.setdefault(card.customer_id or visit.customer_id, []).append(card)
    shown = [person for person in people if person.pk in mine or not any(
        (card.customer_id or visit.customer_id) == person.pk for card in all_cards)]
    keys = [(f'p{person.pk}', person) for person in shown]
    cats = _category_choices()
    errors = []
    if request.method == 'POST':
        keys += [(key, None) for key in _new_keys(request)]
        new_numbers = {key: visit.guests.count() + i for i, key in enumerate(_new_keys(request), start=1)}
        plan, any_picked = [], False
        for number, (key, person) in enumerate(keys, start=1):
            raw_name = request.POST.get(f'name_{key}', '')
            label = person.name if person else _guest_name(raw_name, new_numbers[key])
            picked = [c for c in dict.fromkeys(request.POST.getlist(f'cat_{key}')) if c in cats]
            waiting = person is not None and any(card.status == 'ASSIGNED' for card in mine.get(person.pk, []))
            if not picked:
                if waiting:
                    errors.append(f'Tap what you did for {label}: Hair, Skin, Manicure or Pedicure.')
                continue
            any_picked = True
            entries = []
            for category in picked:
                choice = request.POST.get(f'type_{key}_{category}', '').strip().upper()
                if choice not in option_codes(category):
                    errors.append(f'Pick the {CATEGORIES[category]["label"].lower()} type for {label}.')
                    continue
                allowed = concern_codes(category)
                concerns = [c for c in dict.fromkeys(request.POST.getlist(f'concern_{key}_{category}')) if c in allowed]
                entries.append((category, choice, concerns))
            plan.append((person, raw_name, entries))
        if not any_picked and not errors:
            errors.append('Tap at least one of Hair, Skin, Manicure or Pedicure.')
        if not errors:
            now = timezone.now()
            with transaction.atomic():
                visit = Visit.objects.select_for_update().get(pk=visit.pk)
                for person, raw_name, entries in plan:
                    if person is None:
                        person = _add_guest(visit, raw_name)
                    cards = mine.get(person.pk, [])
                    by_category = {card.category: card for card in cards if card.category in CATEGORIES}
                    spare = [card for card in cards if card.category not in CATEGORIES and card.status == 'ASSIGNED']
                    used = set()
                    customer = Customer.objects.select_for_update().get(pk=person.pk)
                    fields = {'concerns', 'updated_at'}
                    for category, choice, concerns in entries:
                        card = by_category.get(category) or (spare.pop(0) if spare else None)
                        if card is None:
                            card = VisitAssignment(visit=visit, customer=person, employee=request.user,
                                                   assigned_by=request.user)
                        card.customer = person
                        card.category, card.selected_type, card.concerns = category, choice, concerns
                        card.status, card.completed_at = 'DONE', now
                        card.save()
                        used.add(card.pk)
                        if choice != UNKNOWN:
                            setattr(customer, CATEGORIES[category]['customer_field'], choice)
                            fields.add(CATEGORIES[category]['customer_field'])
                        customer.concerns = {**(customer.concerns or {}), category: concerns}
                    customer.save(update_fields=sorted(fields))
                    for card in cards:
                        if card.pk not in used:
                            card.delete()  # unticked or never needed (e.g. Mani / Pedi became Manicure only)
                _settle_visit(visit)
            messages.success(request, f'{visit.customer.name}: saved.')
            return redirect('employee_dashboard')

    def panel(key, person, number):
        cards = mine.get(person.pk, []) if person else []
        by_category = {card.category: card for card in cards if card.category in CATEGORIES}
        posted = request.method == 'POST'
        picked = set(request.POST.getlist(f'cat_{key}')) if posted else set(by_category)
        sections = []
        for category in cats:
            info = CATEGORIES[category]
            card = by_category.get(category)
            previous = getattr(person, info['customer_field']) if person else ''
            if posted:
                chosen = request.POST.get(f'type_{key}_{category}', '')
                concerns = set(request.POST.getlist(f'concern_{key}_{category}'))
            elif card and card.status == 'DONE':
                chosen, concerns = card.selected_type, set(card.concerns)
            else:
                chosen, concerns = previous, set((person.concerns or {}).get(category, []) if person else [])
            sections.append({
                'key': category, 'label': info['label'], 'question': info['question'], 'open': category in picked,
                'previous': type_label(category, previous), 'unsure': chosen == UNKNOWN,
                'options': [{'code': code, 'label': label, 'hint': hint, 'image': image, 'selected': code == chosen}
                            for code, label, hint, image in info['options']],
                'concerns': [{'code': code, 'label': label, 'checked': code in concerns} for code, label in info['concerns']],
            })
        asked = [card for card in cards if card.status == 'ASSIGNED' and card.category not in CATEGORIES]
        hint = ''
        if any(card.category == NAILS for card in asked):
            hint = 'Mani / Pedi: tap Manicure, Pedicure or both.'
        elif asked:
            hint = 'Tap what you are doing: Hair, Skin, Manicure or Pedicure.'
        with_others = []
        if person:
            names = display_names([card.employee for card in others.get(person.pk, [])])
            with_others = [f'{names[card.employee_id]}: {card.category_label}' for card in others.get(person.pk, [])]
        typed = request.POST.get(f'name_{key}', '') if posted else ''
        return {
            'key': key, 'new': person is None, 'main': person is not None and person.pk == visit.customer_id,
            'name': person.name if person else _guest_name(typed, number), 'typed_name': typed,
            'sections': sections, 'hint': hint, 'others': with_others,
            'visits': person.visits.count() + person.guest_visits.count() if person else 0,
        }
    first_new = visit.guests.count() + 1
    numbers = {key: first_new + i for i, key in enumerate(k for k, p in keys if p is None)}
    panels = [panel(key, person, numbers.get(key, 0)) for key, person in keys]
    return render(request, 'operations/record_visit.html', {
        'visit': visit, 'panels': panels, 'blank_panel': panel('__KEY__', None, 0), 'errors': errors,
        'next_guest': first_new + len(numbers),
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
        VisitAssignment.objects.create(visit=visit, customer=visit.customer, employee=request.user, assigned_by=request.user)
        _settle_visit(visit)
    messages.success(request, f'You took {visit.customer.name}.')
    return redirect('record_visit', visit_id=visit.pk)


@roles_required('ADMIN', 'GENERAL_MANAGER')
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
                .annotate(customers=Count('customer', distinct=True))}
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
    served = Customer.objects.filter(Q(visits__branch__in=branches) | Q(guest_visits__branch__in=branches)).distinct()
    return render(request, 'operations/customer_types.html', {
        'sections': sections, 'branches': Branch.objects.filter(pk__in=branches.values('pk')) if chosen else branches,
        'all_branches': Branch.objects.filter(active=True).order_by('name') if not (profile and profile.role == 'MANAGER') else [],
        'chosen': chosen,
        'customers_total': served.count(),
        'customers_typed': served.exclude(hair_type='', skin_type='', hand_type='', foot_type='').count(),
    })
