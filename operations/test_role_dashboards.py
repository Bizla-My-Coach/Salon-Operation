from django.contrib.auth.models import User
from django.templatetags.static import static
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Branch, BranchDuty, Customer, Service, Visit, VisitService


class RoleDashboardTests(TestCase):
    def setUp(self):
        self.north = Branch.objects.create(code='N', name='North')
        self.south = Branch.objects.create(code='S', name='South')
        self.manager = self.person('manager', 'MANAGER', self.north)
        self.gm = self.person('general', 'GENERAL_MANAGER')
        self.employee = self.person('employee', branch=self.north)
        self.customer = Customer.objects.create(name='Customer')
        self.service = Service.objects.create(code='CUT', name='Haircut')

    def person(self, username, role='EMPLOYEE', branch=None):
        user = User.objects.create_user(username)
        user.profile.role = role
        user.profile.branch = branch
        user.profile.save()
        return user

    def visit(self, branch, status):
        return Visit.objects.create(branch=branch, customer=self.customer, status=status, created_by=self.manager)

    def test_manager_uses_current_roster_and_excludes_other_branch_work(self):
        away = self.person('away', branch=self.north)
        leave = self.person('leave', branch=self.north)
        cover = self.person('cover', branch=self.south)
        inactive = self.person('inactive', branch=self.north)
        inactive.profile.active = False
        inactive.profile.save()
        disabled = self.person('disabled', branch=self.north)
        disabled.is_active = False
        disabled.save()
        for user, branch, status in [(away, self.south, 'WORK'), (leave, None, 'LEAVE'),
                                     (cover, self.north, 'WORK'), (self.gm, self.north, 'WORK')]:
            BranchDuty.objects.create(updated_by=self.gm, user=user, date=timezone.localdate(), branch=branch, status=status)
        visit = self.visit(self.north, 'ASSIGNED')
        VisitService.objects.create(visit=visit, service=self.service, employee=self.employee, assigned_by=self.manager)
        self.visit(self.south, 'WAITING')
        self.visit(self.north, 'CLOSED')
        self.visit(self.north, 'CANCELLED')
        self.client.force_login(self.manager)
        response = self.client.get(reverse('manager_summary'), {'branch': self.south.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({u.pk for u in response.context['active_users']},
                         {self.manager.pk, self.employee.pk, cover.pk, self.gm.pk})
        self.assertEqual(response.context['pending_visits'], 1)
        self.assertEqual(next(u for u in response.context['active_users'] if u == self.employee).dashboard_pending, 1)
        self.assertContains(response, '<span>Dashboard</span>', html=True)
        self.assertContains(response, 'Start services')
        self.assertNotContains(response, '>away<')

    def test_general_manager_all_branches_and_branch_tabs(self):
        older = self.visit(self.north, 'EMPLOYEE_DONE')
        Visit.objects.filter(pk=older.pk).update(created_at=timezone.now() - timezone.timedelta(days=2))
        self.visit(self.south, 'VERIFIED')
        self.visit(self.south, 'INVOICED')
        self.client.force_login(self.gm)
        response = self.client.get(reverse('general_manager_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['pending_visits'], 3)
        self.assertContains(response, 'All branches')
        self.assertContains(response, '?branch=' + str(self.south.pk))
        response = self.client.get(reverse('general_manager_dashboard'), {'branch': self.south.pk})
        self.assertEqual(response.context['pending_visits'], 2)
        self.assertEqual(response.context['active_users'], [])
        self.assertEqual([r['count'] for r in response.context['pending_chart']], [0, 0, 0, 0, 1, 1])
        self.assertEqual(self.client.get(reverse('general_manager_dashboard'), {'branch': 'bad'}).status_code, 400)
        self.assertEqual(self.client.get(reverse('general_manager_dashboard'), {'branch': 99999}).status_code, 404)

    def test_staff_has_no_management_dashboard_access_or_navigation(self):
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('manager_dashboard')).status_code, 403)
        self.assertEqual(self.client.get(reverse('general_manager_dashboard')).status_code, 403)
        response = self.client.get(reverse('employee_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<span>Dashboard</span>', html=True)

    def test_off_duty_manager_cannot_open_branch_dashboard(self):
        BranchDuty.objects.create(updated_by=self.gm, user=self.manager, date=timezone.localdate(), status='LEAVE')
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('manager_dashboard')).status_code, 403)

    def test_management_pages_enable_live_updates_for_current_branch(self):
        for user, page, query in [
            (self.manager, 'manager_dashboard', {}),
            (self.manager, 'manager_summary', {}),
            (self.gm, 'general_manager_dashboard', {'branch': self.north.pk}),
        ]:
            with self.subTest(page=page):
                self.client.force_login(user)
                response = self.client.get(reverse(page), query)
                self.assertContains(response, f'data-live-dashboard="{page}"')
                self.assertContains(response, static('js/live-dashboard.js'))
                self.assertContains(response, 'data-live-status')
                self.assertEqual(response.context['dashboard_branch'], self.north)

    def test_overview_contains_operations_and_dashboard_contains_summaries(self):
        own = self.visit(self.north, 'WAITING')
        other = self.visit(self.south, 'WAITING')
        for actor, overview, summary in [
            (self.manager, 'manager_dashboard', 'manager_summary'),
            (self.gm, 'general_manager_overview', 'general_manager_dashboard'),
        ]:
            if actor == self.gm:
                BranchDuty.objects.create(user=actor, branch=self.north, status='WORK',
                                          date=timezone.localdate(), updated_by=actor)
            self.client.force_login(actor)
            response = self.client.get(reverse(overview), {'branch': self.south.pk})
            self.assertEqual(response.status_code, 200)
            self.assertEqual({visit.pk for visit in response.context['visits']}, {own.pk})
            self.assertContains(response, '<h1>Live operations</h1>', html=True)
            self.assertNotContains(response, 'class="branch-dashboard"')
            sidebar = response.content.decode().split('<nav>', 1)[1].split('</nav>', 1)[0]
            self.assertLess(sidebar.index('<span>Overview</span>'), sidebar.index('<span>Dashboard</span>'))
            self.assertContains(response, 'data-live-interval="5000"')
            self.assertContains(response, 'data-live-region="page"')
            response = self.client.get(reverse(summary))
            self.assertContains(response, 'class="branch-dashboard"')
            self.assertNotContains(response, 'class="visit-list manager-visit-list"')
            self.assertContains(response, 'data-live-interval="5000"')
            self.assertContains(response, 'data-live-region="page"')

    def test_general_manager_overview_without_assignment_has_no_branch_data(self):
        self.visit(self.north, 'WAITING')
        self.client.force_login(self.gm)
        self.assertRedirects(self.client.get(reverse('dashboard')), reverse('general_manager_overview'))
        response = self.client.get(reverse('general_manager_overview'), {'branch': self.north.pk})
        self.assertContains(response, 'No working branch is assigned')
        self.assertNotIn('visits', response.context)
        self.assertNotContains(response, 'class="visit-card manager-visit-card"')
        self.assertContains(response, 'data-live-interval="5000"')

    def test_manager_summary_preserves_role_and_roster_authorization(self):
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('manager_summary')).status_code, 403)
        self.assertEqual(self.client.get(reverse('general_manager_overview')).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('general_manager_overview')).status_code, 403)
        BranchDuty.objects.create(user=self.manager, status='LEAVE', date=timezone.localdate(), updated_by=self.gm)
        self.assertEqual(self.client.get(reverse('manager_summary')).status_code, 403)

    def test_site_polling_intervals_and_safe_form_regions(self):
        admin = self.person('overview_admin', 'ADMIN')
        for user, page, interval, full in [
            (admin, 'admin_dashboard', 5000, True),
            (admin, 'admin_reports', 30000, True),
            (admin, 'admin_visits', 5000, True),
            (self.employee, 'employee_dashboard', 5000, True),
            (self.manager, 'manager_staff', 5000, True),
            (self.gm, 'branch_roster', 30000, False),
            (self.employee, 'my_profile', 30000, False),
            (self.manager, 'new_visit', 30000, False),
        ]:
            with self.subTest(page=page):
                self.client.force_login(user)
                response = self.client.get(reverse(page))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'data-live-interval="{interval}"')
                self.assertContains(response, static('js/live-dashboard.js'), count=1)
                self.assertEqual(response.context['live_full_page'], full)
                if full:
                    self.assertContains(response, 'data-live-region="page"')
                else:
                    self.assertNotContains(response, 'data-live-region="page"')
                    self.assertContains(response, 'data-live-region="heading"')

    def test_staff_execution_receives_service_changes_and_delegated_controls(self):
        visit = self.visit(self.north, 'ASSIGNED')
        service = VisitService.objects.create(visit=visit, service=self.service,
                    employee=self.employee, assigned_by=self.manager)
        self.client.force_login(self.employee)
        response = self.client.get(reverse('execute_service', args=[service.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-live-interval="5000"')
        self.assertContains(response, 'data-live-region="page"')
        self.assertContains(response, static('js/service-controls.js'))
        self.assertContains(response, 'name="employee_notes"')

    def test_login_page_does_not_poll(self):
        response = self.client.get(reverse('login'))
        self.assertNotContains(response, 'data-live-page')
        self.assertNotContains(response, 'js/live-dashboard')

    def test_health(self):
        self.assertEqual(self.client.get(reverse('health')).json()['status'], 'ok')
