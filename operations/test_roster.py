from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import VisitServiceAssignmentForm
from .models import Branch, BranchDuty, Customer, Service, Visit, VisitService
from .roster import working_branch, working_employees


@override_settings(STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class BranchRosterTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.a = Branch.objects.create(code="A", name="Branch A")
        self.b = Branch.objects.create(code="B", name="Branch B")
        self.admin = self.person("admin", "ADMIN")
        self.gm = self.person("gm", "GENERAL_MANAGER")
        self.manager = self.person("manager", "MANAGER", self.a)
        self.employee = self.person("employee", "EMPLOYEE", self.a)

    def person(self, name, role, branch=None):
        user = User.objects.create_user(name, password="test")
        user.profile.role = role
        user.profile.branch = branch
        user.profile.save()
        return user

    def duty(self, user, branch=None, status="WORK", date=None):
        return BranchDuty.objects.create(
            user=user, branch=branch, status=status, date=date or self.today,
            updated_by=self.admin,
        )

    def test_general_manager_has_one_identity_and_only_rostered_manager_access(self):
        self.assertEqual(self.client.get(reverse("health")).json()["status"], "ok")
        self.client.force_login(self.gm)
        self.assertEqual(self.client.get(reverse("manager_dashboard")).status_code, 403)
        roster_page = self.client.get(reverse("branch_roster"))
        self.assertEqual(roster_page.status_code, 200)
        self.assertContains(roster_page, "No branch assigned today")
        self.assertNotContains(roster_page, f'href="{reverse("manager_dashboard")}"')
        self.assertEqual(self.client.get(reverse("admin_reports")).status_code, 200)
        self.assertEqual(self.client.get(reverse("create_user")).status_code, 403)
        self.duty(self.gm, self.b)
        manager_page = self.client.get(reverse("manager_dashboard"))
        self.assertEqual(manager_page.status_code, 200)
        self.assertContains(manager_page, "Live operations · Branch B")
        response = self.client.post(reverse("new_visit"), {
            "customer_name": "Roster Customer", "mobile": "",
        })
        self.assertEqual(response.status_code, 302)
        visit = Visit.objects.get(customer__name="Roster Customer")
        self.assertEqual(visit.branch, self.b)
        self.assertEqual(visit.created_by, self.gm)

    def test_general_manager_overview_is_distinct_from_roster(self):
        self.client.force_login(self.gm)
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("general_manager_dashboard"))
        response = self.client.get(reverse("general_manager_dashboard"))
        self.assertContains(response, "Business overview")
        self.assertContains(response, "Set today's branch")
        self.assertNotContains(response, 'name="start_date"')
        nav = response.content.decode().split("<nav>", 1)[1].split("</nav>", 1)[0]
        self.assertEqual(nav.count(f'href="{reverse("branch_roster")}"'), 1)

    def test_service_catalogue_available_to_managers_but_not_employees(self):
        for user in [self.admin, self.gm, self.manager]:
            self.client.force_login(user)
            response = self.client.get(reverse("service_catalog"))
            self.assertContains(response, "Service catalogue")
            self.assertContains(response, f'href="{reverse("service_catalog")}"')
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("service_catalog")).status_code, 403)

    def test_admin_creates_general_manager_without_django_admin_privileges(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("create_user"), {
            "username": "new_gm", "first_name": "New GM", "password": "safe-test-password",
            "role": "GENERAL_MANAGER", "branch": "", "employee_code": "", "job_title": "General manager",
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="new_gm")
        self.assertEqual(user.profile.role, "GENERAL_MANAGER")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_manager_cover_changes_scope_not_home_branch_or_visit_history(self):
        customer = Customer.objects.create(name="Previous Customer")
        old_visit = Visit.objects.create(branch=self.a, customer=customer, created_by=self.manager)
        self.duty(self.manager, self.b)
        self.assertEqual(self.manager.profile.branch, self.a)
        self.assertEqual(working_branch(self.manager), self.b)
        self.client.force_login(self.manager)
        response = self.client.get(reverse("edit_visit_services", args=[old_visit.pk]))
        self.assertEqual(response.status_code, 404)
        old_visit.refresh_from_db()
        self.assertEqual(old_visit.branch, self.a)

    def test_manager_on_leave_has_no_branch_operations(self):
        self.duty(self.manager, status="LEAVE")
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse("dashboard")), "Not rostered today")
        self.assertEqual(self.client.get(reverse("manager_dashboard")).status_code, 403)

    def test_employee_cover_and_leave_control_assignment_lists(self):
        self.assertIn(self.employee, working_employees(self.a))
        self.assertNotIn(self.employee, working_employees(self.b))
        duty = self.duty(self.employee, self.b)
        self.assertNotIn(self.employee, working_employees(self.a))
        self.assertIn(self.employee, working_employees(self.b))
        self.assertIn(self.employee, VisitServiceAssignmentForm(branch=self.b).fields["employee"].queryset)
        duty.status = "LEAVE"
        duty.branch = None
        duty.save()
        self.assertIsNone(working_branch(self.employee))
        self.assertNotIn(self.employee, working_employees(self.a))
        self.assertNotIn(self.employee, working_employees(self.b))

    def test_roster_page_is_authorized_and_writes_date_range(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("branch_roster")).status_code, 403)
        self.client.force_login(self.gm)
        tomorrow = self.today + timedelta(days=1)
        response = self.client.post(reverse("branch_roster"), {
            "user": self.manager.pk,
            "start_date": self.today.isoformat(),
            "end_date": tomorrow.isoformat(),
            "status": "WORK", "branch": self.b.pk,
        })
        self.assertRedirects(response, reverse("branch_roster"))
        self.assertEqual(BranchDuty.objects.filter(user=self.manager, branch=self.b, updated_by=self.gm).count(), 2)
        self.assertEqual(self.manager.profile.branch, self.a)

    def test_open_employee_service_blocks_same_day_transfer(self):
        customer = Customer.objects.create(name="Working Customer")
        service = Service.objects.create(code="S1", name="Service")
        visit = Visit.objects.create(branch=self.a, customer=customer, created_by=self.manager)
        VisitService.objects.create(
            visit=visit, service=service, employee=self.employee, assigned_by=self.manager,
        )
        self.client.force_login(self.gm)
        response = self.client.post(reverse("branch_roster"), {
            "user": self.employee.pk,
            "start_date": self.today.isoformat(),
            "end_date": self.today.isoformat(),
            "status": "WORK", "branch": self.b.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reassign this employee")
        self.assertFalse(BranchDuty.objects.filter(user=self.employee).exists())

    def test_leave_today_replaces_working_duty_with_branch_still_selected(self):
        duty = self.duty(self.employee, self.b)
        self.client.force_login(self.gm)
        response = self.client.post(reverse("branch_roster"), {
            "user": self.employee.pk,
            "start_date": self.today.isoformat(),
            "end_date": self.today.isoformat(),
            "status": "LEAVE", "branch": self.b.pk,
        })
        self.assertRedirects(response, reverse("branch_roster"))
        duty.refresh_from_db()
        self.assertEqual(duty.status, "LEAVE")
        self.assertIsNone(duty.branch)
        self.assertEqual(duty.updated_by, self.gm)
        self.assertEqual(BranchDuty.objects.filter(user=self.employee, date=self.today).count(), 1)
        self.assertIsNone(working_branch(self.employee))
        self.assertNotIn(self.employee, working_employees(self.a))
        self.assertNotIn(self.employee, working_employees(self.b))
        self.employee.profile.refresh_from_db()
        self.assertEqual(self.employee.profile.branch, self.a)

    def test_leave_range_without_working_branch(self):
        self.client.force_login(self.admin)
        tomorrow = self.today + timedelta(days=1)
        response = self.client.post(reverse("branch_roster"), {
            "user": self.manager.pk,
            "start_date": self.today.isoformat(),
            "end_date": tomorrow.isoformat(),
            "status": "LEAVE", "branch": "",
        })
        self.assertRedirects(response, reverse("branch_roster"))
        self.assertEqual(BranchDuty.objects.filter(
            user=self.manager, status="LEAVE", branch__isnull=True,
            date__range=(self.today, tomorrow),
        ).count(), 2)

    def test_leave_with_selected_branch_still_requires_open_service_reassignment(self):
        duty = self.duty(self.employee, self.a)
        customer = Customer.objects.create(name="Working Customer")
        service = Service.objects.create(code="S1", name="Service")
        visit = Visit.objects.create(branch=self.a, customer=customer, created_by=self.manager)
        VisitService.objects.create(
            visit=visit, service=service, employee=self.employee, assigned_by=self.manager,
        )
        self.client.force_login(self.gm)
        response = self.client.post(reverse("branch_roster"), {
            "user": self.employee.pk,
            "start_date": self.today.isoformat(),
            "end_date": self.today.isoformat(),
            "status": "LEAVE", "branch": self.a.pk,
        })
        self.assertContains(response, "Reassign this employee")
        self.assertNotContains(response, "Leave must not have a working branch.")
        duty.refresh_from_db()
        self.assertEqual(duty.status, "WORK")
        self.assertEqual(duty.branch, self.a)

    def test_working_duty_still_requires_branch(self):
        self.client.force_login(self.gm)
        response = self.client.post(reverse("branch_roster"), {
            "user": self.employee.pk,
            "start_date": self.today.isoformat(),
            "end_date": self.today.isoformat(),
            "status": "WORK", "branch": "",
        })
        self.assertContains(response, "Choose a working branch.")
        self.assertFalse(BranchDuty.objects.filter(user=self.employee).exists())
