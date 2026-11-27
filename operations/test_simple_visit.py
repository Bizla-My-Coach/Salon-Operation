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


@override_settings(SIMPLE_VISIT_FLOW=True, SIMPLE_VISIT_BODY=True,
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

    def test_phone_is_required_in_the_simple_flow(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('new_visit'), {'customer_name': 'Riya', 'mobile': ''})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Visit.objects.exists())
        self.assertContains(response, 'Next: assign staff')

    def test_manager_assigns_several_staff_and_visit_closes_when_all_saved(self):
        visit = self.create_customer()
        page = self.client.get(reverse('assign_staff', args=[visit.pk]))
        self.assertContains(page, 'Anu')
        self.assertContains(page, 'Staff chooses')
        self.assertNotContains(page, 'Outsider')
        response = self.client.post(reverse('assign_staff', args=[visit.pk]), {
            f'staff_{self.anu.pk}': '1', f'category_{self.anu.pk}': 'HAIR',
            f'staff_{self.bina.pk}': '1', f'category_{self.bina.pk}': '',
        })
        self.assertRedirects(response, reverse('manager_dashboard'))
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'ASSIGNED')
        hair = visit.category_assignments.get(employee=self.anu)
        open_card = visit.category_assignments.get(employee=self.bina)
        self.assertEqual((hair.category, open_card.category), ('HAIR', ''))

        self.client.force_login(self.anu)
        home = self.client.get(reverse('employee_dashboard'))
        self.assertContains(home, 'Riya')
        self.assertContains(home, reverse('pick_type', args=[hair.pk]))
        picker = self.client.get(reverse('pick_type', args=[hair.pk]))
        self.assertContains(picker, 'img/types/hair-wavy.svg')
        self.assertContains(picker, 'Dandruff')
        self.client.post(reverse('pick_type', args=[hair.pk]),
                         {'type': 'WAVY', 'concern': ['DANDRUFF', 'HAIR_FALL', 'ACNE']})
        hair.refresh_from_db()
        self.assertEqual((hair.status, hair.selected_type, hair.concerns), ('DONE', 'WAVY', ['DANDRUFF', 'HAIR_FALL']))
        customer = Customer.objects.get(pk=visit.customer_id)
        self.assertEqual(customer.hair_type, 'WAVY')
        self.assertEqual(customer.concerns, {'HAIR': ['DANDRUFF', 'HAIR_FALL']})
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'IN_PROGRESS')

        # Bina's card has no category: she taps Skin first, then the type.
        self.client.force_login(self.bina)
        self.assertEqual(self.client.get(reverse('pick_type', args=[hair.pk])).status_code, 404)
        chooser = self.client.get(reverse('pick_type', args=[open_card.pk]))
        self.assertContains(chooser, 'What are you doing for this customer?')
        self.client.post(reverse('pick_type', args=[open_card.pk]), {'category': 'SKIN'})
        self.assertContains(self.client.get(reverse('pick_type', args=[open_card.pk])), 'skin-oily.svg')
        self.client.post(reverse('pick_type', args=[open_card.pk]), {'type': 'OILY'})
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'CLOSED')
        self.assertIsNotNone(visit.closed_at)
        self.assertFalse(Invoice.objects.filter(visit=visit).exists())

        self.client.force_login(self.manager)
        board = self.client.get(reverse('manager_dashboard'))
        self.assertContains(board, 'Finished today')
        self.assertContains(board, 'Wavy')
        self.assertContains(board, 'Oily')
        self.assertNotContains(board, reverse('add_invoice', args=[visit.pk]))
        self.assertNotContains(board, reverse('assign_staff', args=[visit.pk]))

    def test_save_and_add_another_keeps_visit_open(self):
        visit = self.create_customer()
        self.client.post(reverse('assign_staff', args=[visit.pk]), {f'staff_{self.anu.pk}': '1', f'category_{self.anu.pk}': 'HAIR'})
        card = visit.category_assignments.get()
        self.client.force_login(self.anu)
        response = self.client.post(reverse('pick_type', args=[card.pk]), {'type': 'CURLY', 'another': '1'})
        second = visit.category_assignments.exclude(pk=card.pk).get()
        self.assertRedirects(response, reverse('pick_type', args=[second.pk]))
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'IN_PROGRESS')
        self.client.post(reverse('pick_type', args=[second.pk]), {'category': 'SKIN'})
        self.client.post(reverse('pick_type', args=[second.pk]), {'type': 'DRY'})
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'CLOSED')
        customer = Customer.objects.get(pk=visit.customer_id)
        self.assertEqual((customer.hair_type, customer.skin_type), ('CURLY', 'DRY'))

    def test_manager_must_tick_someone(self):
        visit = self.create_customer()
        response = self.client.post(reverse('assign_staff', args=[visit.pk]), {})
        self.assertContains(response, 'Tick at least one staff member.')
        self.assertFalse(visit.category_assignments.exists())

    def test_unticking_removes_an_unsaved_card(self):
        visit = self.create_customer()
        self.client.post(reverse('assign_staff', args=[visit.pk]), {f'staff_{self.anu.pk}': '1', f'staff_{self.bina.pk}': '1'})
        self.client.post(reverse('assign_staff', args=[visit.pk]), {f'staff_{self.bina.pk}': '1', f'category_{self.bina.pk}': 'SKIN'})
        self.assertEqual(list(visit.category_assignments.values_list('employee__username', 'category')), [('bina', 'SKIN')])

    def test_repeat_customer_sees_last_type_selected(self):
        customer = Customer.objects.create(name='Riya', mobile='9876543210', skin_type='DRY',
                                           concerns={'SKIN': ['TAN']})
        Visit.objects.create(branch=self.branch, customer=customer, created_by=self.manager, status='CLOSED')
        visit = Visit.objects.create(branch=self.branch, customer=customer, created_by=self.manager, status='ASSIGNED')
        card = VisitAssignment.objects.create(visit=visit, category='SKIN', employee=self.anu, assigned_by=self.manager)
        self.client.force_login(self.anu)
        page = self.client.get(reverse('pick_type', args=[card.pk]))
        self.assertContains(page, 'last time: Dry')
        self.assertContains(page, 'value="DRY" required checked')
        self.assertContains(page, 'value="TAN" checked')

    def test_not_sure_keeps_the_customers_previous_type(self):
        customer = Customer.objects.create(name='Riya', mobile='9876543210', hair_type='CURLY')
        visit = Visit.objects.create(branch=self.branch, customer=customer, created_by=self.manager, status='ASSIGNED')
        card = VisitAssignment.objects.create(visit=visit, category='HAIR', employee=self.anu, assigned_by=self.manager)
        self.client.force_login(self.anu)
        self.client.post(reverse('pick_type', args=[card.pk]), {'type': 'UNKNOWN'})
        card.refresh_from_db()
        customer.refresh_from_db()
        self.assertEqual((card.status, card.selected_type, customer.hair_type), ('DONE', 'UNKNOWN', 'CURLY'))

    def test_invalid_type_is_refused(self):
        visit = Visit.objects.create(branch=self.branch, customer=Customer.objects.create(name='A'),
                                     created_by=self.manager, status='ASSIGNED')
        card = VisitAssignment.objects.create(visit=visit, category='HAIR', employee=self.anu, assigned_by=self.manager)
        self.client.force_login(self.anu)
        self.client.post(reverse('pick_type', args=[card.pk]), {'type': 'OILY'})
        card.refresh_from_db()
        self.assertEqual(card.status, 'ASSIGNED')

    def test_staff_can_take_an_unassigned_customer_once(self):
        visit = self.create_customer()
        self.client.force_login(self.anu)
        home = self.client.get(reverse('employee_dashboard'))
        self.assertContains(home, 'Waiting for staff')
        self.assertContains(home, reverse('take_customer', args=[visit.pk]))
        response = self.client.post(reverse('take_customer', args=[visit.pk]))
        card = VisitAssignment.objects.get(visit=visit)
        self.assertRedirects(response, reverse('pick_type', args=[card.pk]))
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

    @override_settings(SIMPLE_VISIT_BODY=False)
    def test_body_category_can_be_turned_off(self):
        visit = self.create_customer()
        self.assertNotContains(self.client.get(reverse('assign_staff', args=[visit.pk])), 'value="BODY"')
        card = VisitAssignment.objects.create(visit=visit, employee=self.anu, assigned_by=self.manager)
        self.client.force_login(self.anu)
        page = self.client.get(reverse('pick_type', args=[card.pk]))
        self.assertContains(page, 'value="SKIN"')
        self.assertNotContains(page, 'value="BODY"')

    def test_customer_types_report_counts_types_and_concerns(self):
        visit = Visit.objects.create(branch=self.branch, customer=Customer.objects.create(name='A', hair_type='WAVY'),
                                     created_by=self.manager, status='EMPLOYEE_DONE')
        VisitAssignment.objects.create(visit=visit, category='HAIR', employee=self.anu, assigned_by=self.manager,
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
