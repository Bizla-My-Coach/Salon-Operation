from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Branch, Customer, Invoice, Visit, VisitAssignment


def make_user(username, role, branch):
    user = User.objects.create_user(username, password='test', first_name=username.title())
    user.profile.role = role
    user.profile.branch = branch
    user.profile.save()
    return user


@override_settings(SIMPLE_VISIT_FLOW=True, SIMPLE_VISIT_MANI_PEDI=True,
                   STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class SimpleVisitFlowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(code='B1', name='Koppam')
        self.other = Branch.objects.create(code='B2', name='Calicut')
        self.manager = make_user('manager', 'MANAGER', self.branch)
        self.anu = make_user('anu', 'EMPLOYEE', self.branch)
        self.bina = make_user('bina', 'EMPLOYEE', self.branch)
        self.outsider = make_user('outsider', 'EMPLOYEE', self.other)

    def create_customer(self, name='Riya', mobile='9876543210'):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('new_visit'), {'customer_name': name, 'mobile': mobile})
        visit = Visit.objects.latest('id')
        self.assertRedirects(response, reverse('assign_staff', args=[visit.pk]))
        return visit

    def key(self, visit):
        return f'p{visit.customer_id}'

    def staff(self, visit, user, *categories, key=None):
        key = key or self.key(visit)
        data = {f'staff_{key}_{user.pk}': '1'}
        if categories:
            data[f'category_{key}_{user.pk}'] = list(categories)
        return data

    def record(self, visit, data):
        return self.client.post(reverse('record_visit', args=[visit.pk]), data)

    def test_phone_is_required_in_the_simple_flow(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('new_visit'), {'customer_name': 'Riya', 'mobile': ''})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Visit.objects.exists())
        self.assertContains(response, 'Next: assign staff')

    def test_manager_assigns_several_staff_and_visit_closes_when_all_saved(self):
        visit = self.create_customer()
        key = self.key(visit)
        page = self.client.get(reverse('assign_staff', args=[visit.pk]))
        self.assertContains(page, 'Anu')
        self.assertContains(page, '+ Add family/friend')
        self.assertContains(page, 'staff choose Hair, Skin or Mani / Pedi themselves')
        self.assertNotContains(page, 'Outsider')
        response = self.client.post(reverse('assign_staff', args=[visit.pk]), {
            **self.staff(visit, self.anu, 'HAIR'), **self.staff(visit, self.bina),
        })
        self.assertRedirects(response, reverse('manager_dashboard'))
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'ASSIGNED')
        hair = visit.category_assignments.get(employee=self.anu)
        open_card = visit.category_assignments.get(employee=self.bina)
        self.assertEqual((hair.category, open_card.category, hair.customer_id), ('HAIR', '', visit.customer_id))

        self.client.force_login(self.anu)
        home = self.client.get(reverse('employee_dashboard'))
        self.assertContains(home, 'Riya')
        self.assertContains(home, reverse('record_visit', args=[visit.pk]))
        self.assertRedirects(self.client.get(reverse('pick_type', args=[hair.pk])), reverse('record_visit', args=[visit.pk]))
        page = self.client.get(reverse('record_visit', args=[visit.pk]))
        self.assertContains(page, 'img/types/hair-wavy.png')
        self.assertContains(page, 'Dandruff')
        self.assertContains(page, f'name="cat_{key}" value="HAIR" data-section-toggle="HAIR" checked')
        self.record(visit, {f'cat_{key}': 'HAIR', f'type_{key}_HAIR': 'WAVY',
                            f'concern_{key}_HAIR': ['DANDRUFF', 'HAIR_FALL', 'ACNE']})
        hair.refresh_from_db()
        self.assertEqual((hair.status, hair.selected_type, hair.concerns), ('DONE', 'WAVY', ['DANDRUFF', 'HAIR_FALL']))
        customer = Customer.objects.get(pk=visit.customer_id)
        self.assertEqual(customer.hair_type, 'WAVY')
        self.assertEqual(customer.concerns, {'HAIR': ['DANDRUFF', 'HAIR_FALL']})
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'IN_PROGRESS')

        # Bina's card has no category: she must tap one before Submit works.
        self.client.force_login(self.bina)
        self.assertContains(self.client.get(reverse('record_visit', args=[visit.pk])), 'Tap what you are doing')
        response = self.record(visit, {})
        self.assertContains(response, 'Tap what you did for Riya')
        self.record(visit, {f'cat_{key}': 'SKIN', f'type_{key}_SKIN': 'OILY'})
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'CLOSED')
        self.assertIsNotNone(visit.closed_at)
        self.assertFalse(Invoice.objects.filter(visit=visit).exists())

        self.client.force_login(self.manager)
        live = self.client.get(reverse('manager_dashboard'))
        self.assertContains(live, 'Finished today <span>1</span>')
        self.assertNotContains(live, 'data-copy="9876543210"')
        board = self.client.get(reverse('manager_dashboard') + '?view=finished')
        self.assertContains(board, 'aria-current="page">Finished today')
        self.assertContains(board, 'data-copy="9876543210"')
        self.assertContains(board, 'js/copy-phone.js')
        self.assertContains(board, 'Wavy')
        self.assertContains(board, 'Oily')
        self.assertNotContains(board, reverse('add_invoice', args=[visit.pk]))
        self.assertNotContains(board, reverse('assign_staff', args=[visit.pk]))

    def test_one_submit_saves_several_categories_on_one_card(self):
        visit = self.create_customer()
        key = self.key(visit)
        self.client.post(reverse('assign_staff', args=[visit.pk]), self.staff(visit, self.anu, 'HAIR', 'SKIN'))
        self.client.force_login(self.anu)
        home = self.client.get(reverse('employee_dashboard'))
        self.assertEqual(home.content.decode().count(reverse('record_visit', args=[visit.pk])), 1)
        self.assertContains(home, 'Hair, Skin')
        # Hair picked but no skin type: nothing is saved.
        response = self.record(visit, {f'cat_{key}': ['HAIR', 'SKIN'], f'type_{key}_HAIR': 'CURLY'})
        self.assertContains(response, 'Pick the skin type for Riya.')
        self.assertFalse(visit.category_assignments.filter(status='DONE').exists())
        # Staff can also add a category the manager did not pick (Manicure).
        self.record(visit, {f'cat_{key}': ['HAIR', 'SKIN', 'MANI'], f'type_{key}_HAIR': 'CURLY',
                            f'type_{key}_SKIN': 'DRY', f'type_{key}_MANI': 'ROUGH'})
        self.assertEqual(sorted(visit.category_assignments.values_list('category', 'status')),
                         [('HAIR', 'DONE'), ('MANI', 'DONE'), ('SKIN', 'DONE')])
        visit.refresh_from_db()
        customer = Customer.objects.get(pk=visit.customer_id)
        self.assertEqual(visit.status, 'CLOSED')
        self.assertEqual((customer.hair_type, customer.skin_type, customer.hand_type), ('CURLY', 'DRY', 'ROUGH'))

    def test_family_member_added_by_manager_is_tracked_separately(self):
        visit = self.create_customer(name='Sneha')
        main = self.key(visit)
        response = self.client.post(reverse('assign_staff', args=[visit.pk]), {
            **self.staff(visit, self.anu, 'HAIR'),
            'new_person': ['n1', 'n2'], 'name_n1': '', **self.staff(visit, self.anu, 'SKIN', key='n1'),
            **self.staff(visit, self.bina, 'NAILS', key='n1'),
            'name_n2': 'Anvi', **self.staff(visit, self.bina, 'HAIR', key='n2'),
        })
        self.assertRedirects(response, reverse('manager_dashboard'))
        guests = list(visit.guests.order_by('id'))
        self.assertEqual([g.name for g in guests], ['Guest 1', 'Anvi'])
        self.assertEqual({g.mobile for g in guests}, {'9876543210'})
        guest, anvi = guests
        self.assertEqual(sorted(visit.category_assignments.values_list('customer__name', 'employee__username', 'category')),
                         [('Anvi', 'bina', 'HAIR'), ('Guest 1', 'anu', 'SKIN'), ('Guest 1', 'bina', 'NAILS'), ('Sneha', 'anu', 'HAIR')])
        board = self.client.get(reverse('manager_dashboard'))
        self.assertContains(board, 'sv-person-label">Guest 1')
        self.assertContains(board, 'sv-person-label">Anvi')

        # Anu sees one card with both people, and records both with one Submit.
        self.client.force_login(self.anu)
        self.assertContains(self.client.get(reverse('employee_dashboard')), '<b>Guest 1</b> · Skin', html=False)
        g = f'p{guest.pk}'
        self.record(visit, {f'cat_{main}': 'HAIR', f'type_{main}_HAIR': 'STRAIGHT',
                            f'cat_{g}': 'SKIN', f'type_{g}_SKIN': 'SENSITIVE'})
        guest.refresh_from_db()
        self.assertEqual(guest.skin_type, 'SENSITIVE')
        self.assertEqual(Customer.objects.get(pk=visit.customer_id).skin_type, '')

        # Bina: Mani / Pedi for Guest 1 becomes Pedicure only, plus Anvi's hair.
        self.client.force_login(self.bina)
        page = self.client.get(reverse('record_visit', args=[visit.pk]))
        self.assertContains(page, 'Mani / Pedi: tap Manicure, Pedicure or both.')
        self.assertNotContains(page, 'aria-label="Sneha"')
        a = f'p{anvi.pk}'
        self.record(visit, {f'cat_{g}': 'PEDI', f'type_{g}_PEDI': 'CRACKED',
                            f'cat_{a}': 'HAIR', f'type_{a}_HAIR': 'COILY'})
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'CLOSED')
        self.assertEqual(visit.category_assignments.filter(employee=self.bina, customer=guest).get().category, 'PEDI')
        self.client.force_login(self.manager)
        report = self.client.get(reverse('customer_types'))
        self.assertContains(report, '3 of 3 customers have a type recorded')

    def test_staff_can_add_a_friend_on_their_card(self):
        visit = self.create_customer()
        key = self.key(visit)
        self.client.post(reverse('assign_staff', args=[visit.pk]), self.staff(visit, self.anu, 'HAIR'))
        self.client.force_login(self.anu)
        self.assertContains(self.client.get(reverse('record_visit', args=[visit.pk])), 'data-panel-template')
        self.record(visit, {f'cat_{key}': 'HAIR', f'type_{key}_HAIR': 'WAVY',
                            'new_person': ['n1', 'n2'], 'name_n1': 'Meera', 'cat_n1': 'SKIN', 'type_n1_SKIN': 'OILY',
                            'name_n2': ''})
        friend = visit.guests.get()
        self.assertEqual((friend.name, friend.skin_type), ('Meera', 'OILY'))
        card = visit.category_assignments.get(customer=friend)
        self.assertEqual((card.employee, card.assigned_by, card.status), (self.anu, self.anu, 'DONE'))
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'CLOSED')

    def test_named_guest_is_reused_on_the_next_visit(self):
        visit = self.create_customer(name='Sneha')
        self.client.post(reverse('assign_staff', args=[visit.pk]), {
            **self.staff(visit, self.anu), 'new_person': 'n1', 'name_n1': 'Anvi', **self.staff(visit, self.anu, key='n1')})
        first = visit.guests.get()
        second = self.create_customer(name='Sneha')
        self.client.post(reverse('assign_staff', args=[second.pk]), {
            **self.staff(second, self.anu), 'new_person': 'n1', 'name_n1': 'anvi', **self.staff(second, self.anu, key='n1')})
        self.assertEqual(second.guests.get(), first)

    def test_manager_can_remove_a_guest_without_saved_types(self):
        visit = self.create_customer()
        self.client.post(reverse('assign_staff', args=[visit.pk]), {
            **self.staff(visit, self.anu), 'new_person': 'n1', **self.staff(visit, self.bina, key='n1')})
        guest = visit.guests.get()
        self.client.post(reverse('assign_staff', args=[visit.pk]), {**self.staff(visit, self.anu), 'remove': f'p{guest.pk}'})
        self.assertFalse(visit.guests.exists())
        self.assertEqual(list(visit.category_assignments.values_list('employee__username', flat=True)), ['anu'])

    def test_tapping_a_category_on_a_tile_selects_that_staff_member(self):
        visit = self.create_customer()
        key = self.key(visit)
        self.client.post(reverse('assign_staff', args=[visit.pk]), {f'category_{key}_{self.bina.pk}': 'SKIN', f'category_{key}_{self.anu.pk}': ''})
        self.assertEqual(list(visit.category_assignments.values_list('employee__username', 'category')), [('bina', 'SKIN')])

    def test_one_staff_member_can_get_several_categories(self):
        visit = self.create_customer()
        key = self.key(visit)
        url = reverse('assign_staff', args=[visit.pk])
        self.client.post(url, self.staff(visit, self.anu, 'HAIR', 'NAILS'))
        self.assertEqual(sorted(visit.category_assignments.values_list('category', flat=True)), ['HAIR', 'NAILS'])
        nails = visit.category_assignments.get(category='NAILS')
        nails.category = 'MANI'
        nails.save()
        page = self.client.get(url)
        self.assertContains(page, f'name="category_{key}_{self.anu.pk}" value="NAILS" checked')
        # Saving again with the same picks keeps the staff member's Manicure card.
        self.client.post(url, self.staff(visit, self.anu, 'HAIR', 'NAILS'))
        self.assertEqual(sorted(visit.category_assignments.values_list('category', flat=True)), ['HAIR', 'MANI'])
        self.client.post(url, self.staff(visit, self.anu, 'SKIN'))
        self.assertEqual(list(visit.category_assignments.values_list('category', flat=True)), ['SKIN'])

    def test_manager_must_tick_someone(self):
        visit = self.create_customer()
        response = self.client.post(reverse('assign_staff', args=[visit.pk]), {'new_person': 'n1'})
        self.assertContains(response, 'Tap at least one staff member.')
        self.assertFalse(visit.category_assignments.exists())
        self.assertFalse(visit.guests.exists())

    def test_unticking_removes_an_unsaved_card(self):
        visit = self.create_customer()
        self.client.post(reverse('assign_staff', args=[visit.pk]), {**self.staff(visit, self.anu), **self.staff(visit, self.bina)})
        self.client.post(reverse('assign_staff', args=[visit.pk]), self.staff(visit, self.bina, 'SKIN'))
        self.assertEqual(list(visit.category_assignments.values_list('employee__username', 'category')), [('bina', 'SKIN')])

    def test_repeat_customer_sees_last_type_selected(self):
        customer = Customer.objects.create(name='Riya', mobile='9876543210', skin_type='DRY',
                                           concerns={'SKIN': ['TAN']})
        Visit.objects.create(branch=self.branch, customer=customer, created_by=self.manager, status='CLOSED')
        visit = Visit.objects.create(branch=self.branch, customer=customer, created_by=self.manager, status='ASSIGNED')
        VisitAssignment.objects.create(visit=visit, customer=customer, category='SKIN', employee=self.anu, assigned_by=self.manager)
        self.client.force_login(self.anu)
        page = self.client.get(reverse('record_visit', args=[visit.pk]))
        self.assertContains(page, 'Last time: Dry')
        self.assertContains(page, f'name="type_p{customer.pk}_SKIN" value="DRY" checked')
        self.assertContains(page, 'value="TAN" checked')

    def test_not_sure_keeps_the_customers_previous_type(self):
        customer = Customer.objects.create(name='Riya', mobile='9876543210', hair_type='CURLY')
        visit = Visit.objects.create(branch=self.branch, customer=customer, created_by=self.manager, status='ASSIGNED')
        card = VisitAssignment.objects.create(visit=visit, customer=customer, category='HAIR', employee=self.anu, assigned_by=self.manager)
        self.client.force_login(self.anu)
        k = f'p{customer.pk}'
        self.record(visit, {f'cat_{k}': 'HAIR', f'type_{k}_HAIR': 'UNKNOWN'})
        card.refresh_from_db()
        customer.refresh_from_db()
        self.assertEqual((card.status, card.selected_type, customer.hair_type), ('DONE', 'UNKNOWN', 'CURLY'))

    def test_invalid_type_is_refused(self):
        customer = Customer.objects.create(name='A')
        visit = Visit.objects.create(branch=self.branch, customer=customer, created_by=self.manager, status='ASSIGNED')
        card = VisitAssignment.objects.create(visit=visit, customer=customer, category='HAIR', employee=self.anu, assigned_by=self.manager)
        self.client.force_login(self.anu)
        k = f'p{customer.pk}'
        self.record(visit, {f'cat_{k}': 'HAIR', f'type_{k}_HAIR': 'OILY'})
        card.refresh_from_db()
        self.assertEqual(card.status, 'ASSIGNED')

    def test_staff_without_a_card_cannot_open_the_visit(self):
        visit = self.create_customer()
        self.client.post(reverse('assign_staff', args=[visit.pk]), self.staff(visit, self.anu, 'HAIR'))
        self.client.force_login(self.bina)
        self.assertEqual(self.client.get(reverse('record_visit', args=[visit.pk])).status_code, 404)

    def test_staff_can_take_an_unassigned_customer_once(self):
        visit = self.create_customer()
        self.client.force_login(self.anu)
        home = self.client.get(reverse('employee_dashboard'))
        self.assertContains(home, 'Waiting for staff')
        self.assertContains(home, reverse('take_customer', args=[visit.pk]))
        response = self.client.post(reverse('take_customer', args=[visit.pk]))
        card = VisitAssignment.objects.get(visit=visit)
        self.assertRedirects(response, reverse('record_visit', args=[visit.pk]))
        self.assertEqual((card.employee, card.assigned_by, card.category), (self.anu, self.anu, ''))
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'ASSIGNED')

        # Once taken, nobody else can take it and it leaves the waiting list.
        self.client.force_login(self.bina)
        self.assertNotContains(self.client.get(reverse('employee_dashboard')), 'Waiting for staff')
        self.client.post(reverse('take_customer', args=[visit.pk]))
        self.assertEqual(VisitAssignment.objects.filter(visit=visit).count(), 1)

        # The manager sees who took it.
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse('manager_dashboard')), '(self)')

    def test_staff_from_another_branch_cannot_take_the_customer(self):
        visit = self.create_customer()
        self.client.force_login(self.outsider)
        self.assertNotContains(self.client.get(reverse('employee_dashboard')), 'Riya')
        self.assertEqual(self.client.post(reverse('take_customer', args=[visit.pk])).status_code, 404)

    @override_settings(SIMPLE_VISIT_MANI_PEDI=False)
    def test_mani_pedi_can_be_turned_off(self):
        visit = self.create_customer()
        self.assertNotContains(self.client.get(reverse('assign_staff', args=[visit.pk])), 'value="NAILS"')
        VisitAssignment.objects.create(visit=visit, customer=visit.customer, employee=self.anu, assigned_by=self.manager)
        self.client.force_login(self.anu)
        page = self.client.get(reverse('record_visit', args=[visit.pk]))
        self.assertContains(page, 'value="SKIN"')
        self.assertNotContains(page, 'value="MANI"')

    def test_customer_types_report_counts_types_and_concerns(self):
        customer = Customer.objects.create(name='A', hair_type='WAVY')
        visit = Visit.objects.create(branch=self.branch, customer=customer, created_by=self.manager, status='EMPLOYEE_DONE')
        VisitAssignment.objects.create(visit=visit, customer=customer, category='HAIR', employee=self.anu, assigned_by=self.manager,
                                       status='DONE', selected_type='WAVY', concerns=['DANDRUFF'])
        self.client.force_login(self.manager)
        page = self.client.get(reverse('customer_types'))
        self.assertContains(page, '1 of 1 customers have a type recorded')
        self.assertContains(page, 'Hair · 1 customer')
        self.assertContains(page, '<b>1</b>Dandruff', html=False)

    @override_settings(SIMPLE_VISIT_FLOW=False)
    def test_switch_off_brings_back_the_sop_flow(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('new_visit'), {'customer_name': 'Riya', 'mobile': ''})
        visit = Visit.objects.latest('id')
        self.assertRedirects(response, reverse('edit_visit_services', args=[visit.pk]))
        self.assertEqual(self.client.get(reverse('assign_staff', args=[visit.pk])).status_code, 404)

    def test_mani_pedi_both_records_hands_and_feet(self):
        visit = self.create_customer()
        key = self.key(visit)
        self.client.post(reverse('assign_staff', args=[visit.pk]), self.staff(visit, self.anu, 'NAILS'))
        self.client.force_login(self.anu)
        page = self.client.get(reverse('record_visit', args=[visit.pk]))
        self.assertContains(page, 'hand-dry.png')
        self.assertContains(page, 'Nail fungus')
        self.record(visit, {f'cat_{key}': ['MANI', 'PEDI'], f'type_{key}_MANI': 'DRY', f'concern_{key}_MANI': 'BRITTLE',
                            f'type_{key}_PEDI': 'CRACKED', f'concern_{key}_PEDI': 'FUNGUS'})
        visit.refresh_from_db()
        customer = Customer.objects.get(pk=visit.customer_id)
        self.assertEqual(sorted(visit.category_assignments.values_list('category', flat=True)), ['MANI', 'PEDI'])
        self.assertEqual(visit.status, 'CLOSED')
        self.assertEqual((customer.hand_type, customer.foot_type), ('DRY', 'CRACKED'))
        self.assertEqual(customer.concerns, {'MANI': ['BRITTLE'], 'PEDI': ['FUNGUS']})


class StaffNameTests(TestCase):
    def test_first_name_then_last_name_then_username_number(self):
        from .staff_names import display_names
        make = lambda username, first, last='': User.objects.create_user(username, first_name=first, last_name=last)
        anu = make('br002.anu', 'Anu', 'Joseph')
        s1 = make('br002.shahin', 'Shahin', 'K')
        s2 = make('br002.shahin.2', 'Shahin', 'P')
        r1 = make('br002.rahul', 'Rahul')
        r2 = make('br002.rahul.2', 'Rahul')
        nobody = make('br002.x7', '')
        m1 = make('br003.meera', 'Meera')
        m2 = make('br004.meera', 'Meera')
        names = display_names([anu, s1, s2, r1, r2, nobody, m1, m2])
        self.assertEqual(names[anu.pk], 'Anu')
        self.assertEqual((names[s1.pk], names[s2.pk]), ('Shahin K', 'Shahin P'))
        self.assertEqual((names[r1.pk], names[r2.pk]), ('Rahul', 'Rahul #2'))
        self.assertEqual((names[m1.pk], names[m2.pk]), ('br003.meera', 'br004.meera'))
        self.assertEqual(names[nobody.pk], 'br002.x7')
