from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import InvoiceForm, VisitCreateForm, VisitServiceFormSet
from .models import (
    Branch,
    Customer,
    Feedback,
    FeedbackAnswer,
    FeedbackQuestion,
    Invoice,
    OperationalTask,
    Service,
    ServiceDetail,
    SubService,
    Visit,
    VisitService,
)
from .workflow import build_visit_tasks


class CombinedServiceWorkflowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(code="B1", name="Test Branch")
        self.manager = User.objects.create_user("manager", password="test")
        self.manager.profile.role = "MANAGER"
        self.manager.profile.branch = self.branch
        self.manager.profile.save()
        self.employee = User.objects.create_user("employee", password="test")
        self.employee.profile.role = "EMPLOYEE"
        self.employee.profile.branch = self.branch
        self.employee.profile.save()
        self.customer = Customer.objects.create(name="Test Customer")

        self.consult = SubService.objects.create(code="SUB001", name="Client Consultation")
        self.sanitise = SubService.objects.create(code="SUB025", name="Sanitisation")
        self.work_a = SubService.objects.create(code="SUB100", name="First procedure")
        self.work_b = SubService.objects.create(code="SUB200", name="Second procedure")
        for number, sub_service in enumerate([self.consult, self.sanitise, self.work_a, self.work_b], start=1):
            OperationalTask.objects.create(
                external_id=f"T{number}", sub_service=sub_service, sequence=1, name=sub_service.name
            )
        self.service_a = Service.objects.create(code="S1", name="Service One")
        self.service_b = Service.objects.create(code="S2", name="Service Two")
        ServiceDetail.objects.create(external_id="D1", service=self.service_a, sub_service=self.work_a, sequence=1)
        ServiceDetail.objects.create(external_id="D2", service=self.service_a, sub_service=self.sanitise, sequence=2)
        ServiceDetail.objects.create(external_id="D3", service=self.service_b, sub_service=self.consult, sequence=1)
        ServiceDetail.objects.create(external_id="D4", service=self.service_b, sub_service=self.work_b, sequence=2)

    def create_visit(self):
        visit = Visit.objects.create(
            branch=self.branch, customer=self.customer, status="ASSIGNED", created_by=self.manager
        )
        first = VisitService.objects.create(
            visit=visit, service=self.service_a, order_number=1, employee=self.employee, assigned_by=self.manager
        )
        second = VisitService.objects.create(
            visit=visit, service=self.service_b, order_number=2, employee=self.employee, assigned_by=self.manager
        )
        build_visit_tasks(visit)
        return visit, first, second

    def test_health_endpoint_checks_database(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def assignment_payload(self, first, second, first_order=1, second_order=2, employee=None):
        employee = employee or self.employee
        return {
            "services-TOTAL_FORMS": "2",
            "services-INITIAL_FORMS": "2",
            "services-MIN_NUM_FORMS": "0",
            "services-MAX_NUM_FORMS": "1000",
            "services-0-id": str(first.pk),
            "services-0-order_number": str(first_order),
            "services-0-service": str(first.service_id),
            "services-0-employee": str(employee.pk),
            "services-0-chair": "",
            "services-1-id": str(second.pk),
            "services-1-order_number": str(second_order),
            "services-1-service": str(second.service_id),
            "services-1-employee": str(employee.pk),
            "services-1-chair": "",
        }

    def test_combined_plan_has_one_sanitisation_then_one_consultation(self):
        visit, first, second = self.create_visit()
        all_tasks = list(visit.services.order_by("order_number").values_list("tasks__title", flat=True))
        self.assertEqual(all_tasks.count("Client Consultation"), 1)
        self.assertEqual(all_tasks.count("Sanitisation"), 1)
        self.assertEqual(
            list(first.tasks.order_by("sequence").values_list("title", flat=True)),
            ["Sanitisation", "Client Consultation", "First procedure"],
        )
        self.assertEqual(
            list(second.tasks.order_by("sequence").values_list("title", flat=True)),
            ["Second procedure"],
        )

    def test_employee_can_open_second_service_but_opening_checks_still_block_work(self):
        _, _, second = self.create_visit()
        self.client.force_login(self.employee)
        response = self.client.get(reverse("execute_service", args=[second.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["service_actions_locked"])
        task = second.tasks.get(task_type="SERVICE")
        self.client.post(reverse("task_action", args=[task.pk, "start"]))
        task.refresh_from_db()
        self.assertEqual(task.status, "PENDING")
        self.assertFalse(task.timing_segments.exists())
        self.client.post(reverse("finish_service", args=[second.pk]))
        second.refresh_from_db()
        self.assertEqual(second.status, "ASSIGNED")

    def test_visit_details_validate_mobile_and_disable_invoice(self):
        form = VisitCreateForm(data={"customer_name": "Customer", "mobile": "9876543210"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.fields["invoice_number"].disabled)
        invalid = VisitCreateForm(data={"customer_name": "Customer", "mobile": "12345"})
        self.assertFalse(invalid.is_valid())

    def test_manager_creates_customer_before_assigning_services(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("new_visit"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Available after verification")
        response = self.client.post(reverse("new_visit"), {
            "customer_name": "New Customer", "mobile": "9876543210"
        })
        visit = Visit.objects.get(customer__mobile="9876543210")
        self.assertRedirects(response, reverse("edit_visit_services", args=[visit.pk]))

    def test_login_page_never_renders_the_manager_sidebar(self):
        page = self.client.get(reverse("login"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'id="app-sidebar"')
        self.client.force_login(self.manager)
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard"))

    def test_assignment_page_has_old_phone_compatible_service_search_and_select(self):
        visit, _, _ = self.create_visit()
        self.client.force_login(self.manager)
        response = self.client.get(reverse("edit_visit_services", args=[visit.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["formset"].total_form_count(), 2)
        self.assertContains(response, 'class="form-control service-combobox-input"')
        self.assertContains(response, 'class="service-combobox-menu"')
        self.assertContains(response, f'value="{self.service_a}"')
        self.assertContains(response, 'name="services-0-service"')
        self.assertNotContains(response, "<datalist")

    def test_first_assignment_page_starts_with_one_row_but_reedit_has_no_extra_row(self):
        visit = Visit.objects.create(
            branch=self.branch, customer=self.customer, status="WAITING", created_by=self.manager
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse("edit_visit_services", args=[visit.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["formset"].total_form_count(), 1)
        self.assertContains(response, 'id="id_services-0-order_number"')
        self.assertContains(response, 'id="id_services-0-service"')
        self.assertContains(response, "+ Add another service")

        assigned_visit, _, _ = self.create_visit()
        response = self.client.get(reverse("edit_visit_services", args=[assigned_visit.pk]))
        self.assertEqual(response.context["formset"].total_form_count(), 2)

    def test_employee_task_cards_only_show_task_name_and_mobile_status(self):
        _, first, _ = self.create_visit()
        task = first.tasks.first()
        task.instructions = "Long confusing instructions must not appear on the card."
        task.save(update_fields=["instructions"])
        self.client.force_login(self.employee)
        response = self.client.get(reverse("execute_service", args=[first.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, task.title)
        self.assertContains(response, task.get_status_display())
        self.assertNotContains(response, task.instructions)
        self.assertNotContains(response, task.get_phase_display())

    def test_stylist_on_later_service_sees_and_confirms_opening_checks_on_colleagues_service(self):
        visit, first, second = self.create_visit()
        colleague = User.objects.create_user("colleague", password="test")
        colleague.profile.role = "EMPLOYEE"
        colleague.profile.branch = self.branch
        colleague.profile.save()
        second.employee = colleague
        second.save(update_fields=["employee"])
        sanitisation = first.tasks.get(task_type="HYGIENE")
        consultation = first.tasks.get(task_type="CONSULT")
        self.client.force_login(colleague)

        page = self.client.get(reverse("execute_service", args=[second.pk]))
        self.assertContains(page, "Visit opening checks")
        self.assertContains(page, reverse("task_action", args=[sanitisation.pk, "confirm"]))

        self.client.post(reverse("task_action", args=[sanitisation.pk, "confirm"]))
        self.client.post(reverse("task_action", args=[consultation.pk, "confirm"]))
        sanitisation.refresh_from_db()
        consultation.refresh_from_db()
        self.assertEqual((sanitisation.status, consultation.status), ("COMPLETED", "COMPLETED"))
        self.assertEqual(consultation.performed_by, colleague)

        task = second.tasks.get(task_type="SERVICE")
        self.client.post(reverse("task_action", args=[task.pk, "start"]))
        task.refresh_from_db()
        self.assertEqual(task.status, "IN_PROGRESS")

        # A stylist with no service on the visit still cannot touch its checks.
        visit2, other_first, _ = self.create_visit()
        other_hygiene = other_first.tasks.get(task_type="HYGIENE")
        VisitService.objects.filter(visit=visit2).update(employee=self.employee)
        self.assertEqual(self.client.post(reverse("task_action", args=[other_hygiene.pk, "confirm"])).status_code, 404)

    def test_opening_checks_use_ordered_one_click_confirmation_without_timing(self):
        visit, first, _ = self.create_visit()
        sanitisation, consultation, procedure = list(first.tasks.order_by("sequence"))
        self.client.force_login(self.employee)

        page = self.client.get(reverse("execute_service", args=[first.pk]))
        self.assertContains(page, "Visit opening checks")
        self.assertContains(page, "Confirm sanitisation, then consultation.")
        self.assertContains(page, reverse("task_action", args=[sanitisation.pk, "confirm"]))
        self.assertNotContains(page, reverse("task_action", args=[sanitisation.pk, "start"]))

        self.client.post(reverse("task_action", args=[consultation.pk, "confirm"]))
        consultation.refresh_from_db()
        self.assertEqual(consultation.status, "PENDING")

        self.client.post(reverse("task_action", args=[procedure.pk, "start"]))
        procedure.refresh_from_db()
        self.assertEqual(procedure.status, "PENDING")

        self.client.post(reverse("task_action", args=[sanitisation.pk, "confirm"]))
        sanitisation.refresh_from_db()
        first.refresh_from_db()
        visit.refresh_from_db()
        self.assertEqual(sanitisation.status, "COMPLETED")
        self.assertIsNone(sanitisation.started_at)
        self.assertIsNotNone(sanitisation.completed_at)
        self.assertEqual(sanitisation.performed_by, self.employee)
        self.assertEqual(first.status, "ASSIGNED")
        self.assertEqual(visit.status, "ASSIGNED")

        self.client.post(reverse("task_action", args=[consultation.pk, "confirm"]))
        self.client.post(reverse("task_action", args=[procedure.pk, "start"]))
        consultation.refresh_from_db()
        procedure.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(consultation.status, "COMPLETED")
        self.assertIsNone(consultation.started_at)
        self.assertEqual(procedure.status, "IN_PROGRESS")
        self.assertEqual(first.status, "IN_PROGRESS")

    def test_manager_can_reorder_services_and_rebuild_pending_plan(self):
        visit, first, second = self.create_visit()
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("edit_visit_services", args=[visit.pk]),
            self.assignment_payload(first, second, first_order=2, second_order=1),
        )
        self.assertRedirects(response, reverse("manager_dashboard"))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.order_number, 1)
        self.assertEqual(first.order_number, 2)
        self.assertEqual(
            list(second.tasks.order_by("sequence").values_list("title", flat=True)),
            ["Sanitisation", "Client Consultation", "Second procedure"],
        )
        self.assertEqual(
            list(first.tasks.order_by("sequence").values_list("title", flat=True)),
            ["First procedure"],
        )

        dashboard = self.client.get(reverse("manager_dashboard"))
        displayed_visit = next(item for item in dashboard.context["visits"] if item.pk == visit.pk)
        displayed_services = list(displayed_visit.services.all())
        self.assertEqual([item.pk for item in displayed_services], [second.pk, first.pk])
        self.assertEqual([item.order_number for item in displayed_services], [1, 2])

    def test_visit_service_formset_prefills_existing_assignments(self):
        visit, _, _ = self.create_visit()
        formset = VisitServiceFormSet(instance=visit, branch=self.branch, prefix="services")
        self.assertEqual([form.instance.service_id for form in formset.initial_forms], [self.service_a.pk, self.service_b.pk])
        self.assertEqual([form.instance.employee_id for form in formset.initial_forms], [self.employee.pk, self.employee.pk])

    def test_edit_page_contains_previous_services_in_execution_order(self):
        visit, _, _ = self.create_visit()
        self.client.force_login(self.manager)

        response = self.client.get(reverse("edit_visit_services", args=[visit.pk]))

        self.assertEqual(response.status_code, 200)
        formset = response.context["formset"]
        self.assertEqual([form.instance.service_id for form in formset.initial_forms], [self.service_a.pk, self.service_b.pk])

    def test_reassignment_preserves_existing_services_and_order(self):
        visit, first, second = self.create_visit()
        replacement = User.objects.create_user("replacement", password="test")
        replacement.profile.role = "EMPLOYEE"
        replacement.profile.branch = self.branch
        replacement.profile.save()
        original_ids = [first.pk, second.pk]
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("edit_visit_services", args=[visit.pk]),
            self.assignment_payload(first, second, employee=replacement),
        )

        self.assertRedirects(response, reverse("manager_dashboard"))
        assignments = list(visit.services.order_by("order_number", "id"))
        self.assertEqual([item.pk for item in assignments], original_ids)
        self.assertEqual(
            [item.service_id for item in assignments],
            [self.service_a.pk, self.service_b.pk],
        )
        self.assertEqual([item.order_number for item in assignments], [1, 2])
        self.assertTrue(all(item.employee_id == replacement.pk for item in assignments))

        self.client.force_login(replacement)
        response = self.client.get(reverse("employee_dashboard"))
        jobs = list(response.context["jobs"])
        self.assertEqual([job.pk for job in jobs], original_ids)
        self.assertEqual([job.order_number for job in jobs], [1, 2])

    def test_manager_locks_started_service_but_can_edit_upcoming_service(self):
        visit, first, _ = self.create_visit()
        task = first.tasks.first()
        task.status = "IN_PROGRESS"
        task.save(update_fields=["status"])
        self.client.force_login(self.manager)
        first.status = "IN_PROGRESS"
        first.save(update_fields=["status"])
        response = self.client.get(reverse("edit_visit_services", args=[visit.pk]))
        self.assertEqual(response.status_code, 200)
        first_form = response.context["formset"].initial_forms[0]
        self.assertTrue(first_form.fields["service"].disabled)
        self.assertFalse(response.context["formset"].initial_forms[1].fields["service"].disabled)

    def test_manager_adds_service_to_started_visit_without_changing_history(self):
        from .models import TaskTimingSegment

        visit, first, second = self.create_visit()
        second.delete()
        started_at = timezone.now() - timedelta(minutes=5)
        first.status = "IN_PROGRESS"
        first.started_at = started_at
        first.save()
        visit.status = "IN_PROGRESS"
        visit.save()
        first.tasks.filter(task_type__in=["HYGIENE", "CONSULT"]).update(
            status="COMPLETED", started_at=started_at, completed_at=started_at,
            performed_by=self.employee,
        )
        task = first.tasks.get(task_type="SERVICE")
        task.status = "IN_PROGRESS"
        task.started_at = started_at
        task.performed_by = self.employee
        task.save()
        segment = TaskTimingSegment.objects.create(task=task, kind="ACTIVE", started_at=started_at)
        original_tasks = list(first.tasks.order_by("pk").values())
        original_segment = TaskTimingSegment.objects.filter(pk=segment.pk).values().get()
        self.client.force_login(self.manager)
        url = reverse("edit_visit_services", args=[visit.pk])
        page = self.client.get(url)
        self.assertContains(page, "+ Add another service")
        self.assertTrue(page.context["formset"].initial_forms[0].execution_locked)

        response = self.client.post(url, {
            "services-TOTAL_FORMS": "2", "services-INITIAL_FORMS": "1",
            "services-0-id": str(first.pk),
            # Locked inputs must ignore attempts to change or delete started work.
            "services-0-order_number": "99", "services-0-service": str(self.service_b.pk),
            "services-0-employee": str(self.manager.pk), "services-0-DELETE": "on",
            "services-1-order_number": "2", "services-1-service": str(self.service_b.pk),
            "services-1-employee": str(self.employee.pk), "services-1-chair": "",
        })

        self.assertRedirects(response, reverse("manager_dashboard"))
        first.refresh_from_db()
        self.assertEqual((first.service_id, first.employee_id, first.order_number, first.status, first.started_at),
                         (self.service_a.pk, self.employee.pk, 1, "IN_PROGRESS", started_at))
        self.assertEqual(list(first.tasks.order_by("pk").values()), original_tasks)
        self.assertEqual(TaskTimingSegment.objects.filter(pk=segment.pk).values().get(), original_segment)
        added = visit.services.exclude(pk=first.pk).get()
        self.assertEqual((added.service_id, added.order_number, added.status), (self.service_b.pk, 2, "ASSIGNED"))
        self.assertEqual(list(added.tasks.values_list("title", flat=True)), ["Second procedure"])
        self.assertEqual(visit.services.filter(tasks__task_type="HYGIENE").count(), 1)
        self.assertEqual(visit.services.filter(tasks__task_type="CONSULT").count(), 1)
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("execute_service", args=[added.pk])).status_code, 200)

    def test_other_branch_manager_cannot_edit_visit(self):
        visit, _, _ = self.create_visit()
        other_branch = Branch.objects.create(code="B2", name="Other Branch")
        other_manager = User.objects.create_user("other-manager", password="test")
        other_manager.profile.role = "MANAGER"
        other_manager.profile.branch = other_branch
        other_manager.profile.save()
        self.client.force_login(other_manager)
        response = self.client.get(reverse("edit_visit_services", args=[visit.pk]))
        self.assertEqual(response.status_code, 404)

    def test_admin_can_render_service_catalogue(self):
        admin = User.objects.create_superuser("admin", "admin@example.com", "test")
        self.client.force_login(admin)
        response = self.client.get(reverse("service_catalog"), {"service": self.service_a.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "First procedure")
        self.assertNotContains(response, "Client Consultation")
        self.assertNotContains(response, "Sanitisation")

    def test_admin_visit_explorer_searches_and_filters_visit_cards(self):
        visit, _, _ = self.create_visit()
        first_service = visit.services.order_by("order_number").first()
        opening_task = first_service.tasks.filter(task_type="HYGIENE").get()
        opening_task.status = "COMPLETED"
        opening_task.started_at = timezone.now() - timedelta(minutes=60)
        opening_task.completed_at = timezone.now()
        opening_task.save(update_fields=["status", "started_at", "completed_at"])
        timed_task = first_service.tasks.filter(task_type="SERVICE").get()
        timed_task.status = "COMPLETED"
        timed_task.started_at = timezone.now() - timedelta(minutes=100)
        timed_task.completed_at = timezone.now()
        timed_task.save(update_fields=["status", "started_at", "completed_at"])
        admin = User.objects.create_superuser("visit-admin", "visit-admin@example.com", "test")
        self.client.force_login(admin)

        response = self.client.get(reverse("admin_visits"), {
            "q": self.customer.name,
            "branch": self.branch.pk,
            "service": self.service_a.pk,
            "status": "ASSIGNED",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page"].paginator.count, 1)
        self.assertContains(response, self.customer.name)
        self.assertContains(response, f"Visit #{visit.pk}")
        self.assertContains(response, self.service_a.name)
        self.assertContains(response, "Apply filters")
        self.assertContains(response, "Total task time")
        self.assertContains(response, "1 hour 40 minutes")
        self.assertNotContains(response, "<small>Token</small>", html=True)
        self.assertNotContains(response, "<small>Invoice</small>", html=True)

        first_service.status = "EMPLOYEE_DONE"
        first_service.save(update_fields=["status"])
        response = self.client.get(reverse("admin_visits"), {"q": self.customer.name})
        self.assertContains(response, "1 hour 40 minutes")

    def test_admin_reports_include_revenue_tasks_and_feedback(self):
        visit, _, _ = self.create_visit()
        Invoice.objects.create(
            visit=visit,
            invoice_number="REPORT-1",
            amount=Decimal("250.00"),
            entered_by=self.manager,
        )
        feedback = Feedback.objects.create(visit=visit, submitted_at=timezone.now(), suggestion="Great visit")
        question = FeedbackQuestion.objects.filter(active=True).first()
        FeedbackAnswer.objects.create(feedback=feedback, question=question, rating=5)
        admin = User.objects.create_superuser("report-admin", "report-admin@example.com", "test")
        self.client.force_login(admin)

        response = self.client.get(reverse("admin_reports"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["visits"], 1)
        self.assertEqual(response.context["summary"]["revenue"], Decimal("250.00"))
        self.assertEqual(response.context["summary"]["feedback_average"], 5)
        self.assertContains(response, "Branch performance")
        self.assertContains(response, "Service performance")
        self.assertContains(response, "Great visit")

    def test_admin_reports_exclude_visit_opening_checks_from_service_time(self):
        _, first, _ = self.create_visit()
        opening = first.tasks.filter(task_type="HYGIENE").get()
        opening.active_labour_minutes = 30
        opening.passive_time_minutes = 5
        opening.save(update_fields=["active_labour_minutes", "passive_time_minutes"])
        procedure = first.tasks.filter(task_type="SERVICE").get()
        procedure.active_labour_minutes = 40
        procedure.passive_time_minutes = 7
        procedure.save(update_fields=["active_labour_minutes", "passive_time_minutes"])
        admin = User.objects.create_superuser("time-admin", "time-admin@example.com", "test")
        self.client.force_login(admin)

        response = self.client.get(reverse("admin_reports"))

        self.assertEqual(response.context["task_summary"]["labour_minutes"], 40)
        self.assertEqual(response.context["task_summary"]["passive_minutes"], 7)
        service_row = next(row for row in response.context["service_rows"] if row.pk == self.service_a.pk)
        self.assertEqual(service_row.labour_minutes, 40)
        self.assertEqual(service_row.passive_minutes, 7)

    def test_visit_related_labels_include_customer_and_visit(self):
        visit, first, _ = self.create_visit()
        feedback = Feedback.objects.create(visit=visit)
        question = FeedbackQuestion.objects.filter(active=True).first()
        answer = FeedbackAnswer.objects.create(feedback=feedback, question=question, rating=4)

        self.assertIn(self.customer.name, str(visit))
        self.assertIn(self.customer.name, str(first))
        self.assertIn(self.customer.name, str(first.tasks.first()))
        self.assertIn(self.customer.name, str(feedback))
        self.assertIn(self.customer.name, str(answer))
        self.assertIn(f"Visit #{visit.pk}", str(feedback))

        admin = User.objects.create_superuser("records-admin", "records-admin@example.com", "test")
        self.client.force_login(admin)
        feedback_page = self.client.get(reverse("admin:operations_feedback_changelist"))
        answer_page = self.client.get(reverse("admin:operations_feedbackanswer_changelist"))
        self.assertContains(feedback_page, self.customer.name)
        self.assertContains(feedback_page, f"Visit #{visit.pk}")
        self.assertContains(answer_page, self.customer.name)

    def test_manager_cannot_open_admin_reports_or_visit_explorer(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("admin_reports")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin_visits")).status_code, 403)

    def test_invoice_form_does_not_request_payment_method(self):
        form = InvoiceForm()
        self.assertEqual(list(form.fields), ["invoice_number"])

    def test_combined_visit_creates_one_invoice_and_one_feedback_request(self):
        visit, first, second = self.create_visit()
        first.status = second.status = "VERIFIED"
        first.save(update_fields=["status"])
        second.save(update_fields=["status"])
        visit.status = "VERIFIED"
        visit.save(update_fields=["status"])
        self.client.force_login(self.manager)

        dashboard = self.client.get(reverse("manager_dashboard"))
        self.assertContains(dashboard, "Add invoice", count=1)

        invoice_url = reverse("add_invoice", args=[visit.pk])
        response = self.client.post(invoice_url, {
            "customer_name": self.customer.name,
            "mobile": "9876543210",
            "invoice_number": "977",
        })
        self.assertRedirects(response, reverse("manager_dashboard"))
        self.assertEqual(Invoice.objects.filter(visit=visit).count(), 1)
        self.assertEqual(Invoice.objects.get(visit=visit).invoice_number, 'INV/26-27/977')
        self.assertEqual(Feedback.objects.filter(visit=visit).count(), 1)
        self.assertEqual(
            Invoice.objects.get(visit=visit).amount,
            self.service_a.base_price + self.service_b.base_price,
        )

        response = self.client.post(invoice_url, {
            "customer_name": self.customer.name,
            "mobile": "9876543210",
            "invoice_number": "978",
        })
        self.assertRedirects(response, reverse("manager_dashboard"))
        self.assertEqual(Invoice.objects.filter(visit=visit).count(), 1)
        self.assertEqual(Feedback.objects.filter(visit=visit).count(), 1)

    def test_visit_level_verify_page_lists_services_in_order_and_verifies_all(self):
        visit, first, second = self.create_visit()
        for item in [first, second]:
            item.tasks.update(status="COMPLETED")
            item.status = "EMPLOYEE_DONE"
            item.employee_notes = f"Note from {item.service.name}"
            item.save(update_fields=["status", "employee_notes"])
        visit.status = "EMPLOYEE_DONE"
        visit.save(update_fields=["status"])
        self.client.force_login(self.manager)
        page = self.client.get(reverse("verify_visit", args=[visit.pk]))
        self.assertContains(page, self.service_a.name)
        self.assertContains(page, self.service_b.name)
        self.assertContains(page, "Verify all services")
        response = self.client.post(reverse("verify_visit", args=[visit.pk]), {
            f"manager_notes_{first.pk}": "Checked first",
            f"manager_notes_{second.pk}": "Checked second",
        })
        self.assertRedirects(response, reverse("manager_dashboard"))
        self.assertFalse(visit.services.exclude(status="CANCELLED").exclude(status="VERIFIED").exists())

    def test_started_service_can_be_cancelled_and_reassigned_without_deleting_history(self):
        visit, first, _ = self.create_visit()
        old_task = first.tasks.first()
        old_task.status = "IN_PROGRESS"
        old_task.save(update_fields=["status"])
        first.status = "IN_PROGRESS"
        first.save(update_fields=["status"])
        replacement_staff = User.objects.create_user("replacement-active", password="test")
        replacement_staff.profile.role = "EMPLOYEE"
        replacement_staff.profile.branch = self.branch
        replacement_staff.profile.save()
        self.client.force_login(self.manager)
        response = self.client.post(reverse("cancel_and_reassign_service", args=[first.pk]), {
            "cancellation_reason": "Started accidentally",
            "employee": replacement_staff.pk,
            "chair": "",
        })
        self.assertRedirects(response, reverse("manager_dashboard"))
        first.refresh_from_db()
        old_task.refresh_from_db()
        replacement = VisitService.objects.get(replaces=first)
        self.assertEqual(first.status, "CANCELLED")
        self.assertEqual(old_task.status, "CANCELLED")
        self.assertEqual(replacement.employee, replacement_staff)
        self.assertTrue(replacement.tasks.exists())
        replacement_titles = list(replacement.tasks.order_by("sequence").values_list("title", flat=True))
        self.assertEqual(replacement_titles[:2], ["Sanitisation", "Client Consultation"])

    def test_manager_feedback_back_keeps_visit_open_and_submit_closes_it(self):
        visit, first, second = self.create_visit()
        first.status = second.status = "VERIFIED"
        first.save(update_fields=["status"]); second.save(update_fields=["status"])
        visit.status = "INVOICED"; visit.save(update_fields=["status"])
        Invoice.objects.create(visit=visit, invoice_number="FB-1", entered_by=self.manager)
        self.client.force_login(self.manager)
        url = reverse("collect_feedback", args=[visit.pk])
        page = self.client.get(url)
        self.assertContains(page, "Back to overview")
        visit.refresh_from_db(); self.assertEqual(visit.status, "INVOICED")
        payload = {f"q_{question.pk}": "5" for question in FeedbackQuestion.objects.filter(active=True)}
        response = self.client.post(url, payload)
        self.assertRedirects(response, reverse("manager_dashboard"))
        visit.refresh_from_db(); self.assertEqual(visit.status, "CLOSED")

    def test_manager_can_close_visit_from_feedback_page_without_answers(self):
        visit, first, second = self.create_visit()
        first.status = second.status = "VERIFIED"
        first.save(update_fields=["status"]); second.save(update_fields=["status"])
        visit.status = "INVOICED"; visit.save(update_fields=["status"])
        Invoice.objects.create(visit=visit, invoice_number="FB-CLOSE-1", entered_by=self.manager)
        self.client.force_login(self.manager)
        url = reverse("collect_feedback", args=[visit.pk])

        page = self.client.get(url)
        self.assertContains(page, "Close without feedback")
        response = self.client.post(url, {"action": "close_visit"})

        self.assertRedirects(response, reverse("manager_dashboard"))
        visit.refresh_from_db()
        self.assertEqual(visit.status, "CLOSED")
        self.assertIsNotNone(visit.closed_at)
        feedback = Feedback.objects.get(visit=visit)
        self.assertIsNone(feedback.submitted_at)
        self.assertFalse(feedback.answers.exists())
        dashboard = self.client.get(reverse("manager_dashboard"))
        self.assertFalse(dashboard.context["visits"].filter(pk=visit.pk).exists())

    def test_feedback_uses_standard_faces_and_equal_row_action_class(self):
        visit, _, _ = self.create_visit()
        visit.status = 'INVOICED'
        visit.save()
        self.client.force_login(self.manager)
        page = self.client.get(reverse('collect_feedback', args=[visit.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'class="feedback-actions"', count=1)
        self.assertNotContains(page, 'class="verification-actions"')
        self.assertContains(page, 'Back to overview')
        self.assertContains(page, 'Close without feedback')
        self.assertContains(page, 'Complete feedback')
        self.assertContains(page, 'formnovalidate')
        count = FeedbackQuestion.objects.filter(active=True).count()
        for face in ['😞', '🙁', '😐', '🙂', '😀']:
            self.assertContains(page, face, count=count)
        for face in ['😣', '🤩']:
            self.assertNotContains(page, face)

    def test_public_feedback_keeps_single_submit_button_and_same_rating_values(self):
        visit, _, _ = self.create_visit()
        feedback = Feedback.objects.create(visit=visit)
        page = self.client.get(reverse('feedback_form', args=[feedback.public_token]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Submit feedback')
        self.assertNotContains(page, 'class="feedback-actions"')
        self.assertNotContains(page, 'Close without feedback')
        for question in FeedbackQuestion.objects.filter(active=True):
            for rating in range(1, 6):
                self.assertContains(page, f'name="q_{question.pk}" value="{rating}" required')

    def test_default_feedback_questions_are_bilingual(self):
        questions = list(FeedbackQuestion.objects.filter(active=True).order_by("sequence"))
        self.assertEqual(len(questions), 5)
        self.assertTrue(all("\n" in question.text for question in questions))
        self.assertEqual(
            questions[0].text,
            "അന്തിമ ഫലത്തിൽ നിങ്ങൾ എത്രത്തോളം തൃപ്തനാണ്?\n"
            "How satisfied are you with the final result?",
        )
