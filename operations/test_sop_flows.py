from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import (
    Branch, Customer, OperationalTask, Service, ServiceDetail, SOPVersion, SubService, Visit, VisitService,
)
from .sop_flow_views import service_flow_digest
from .sop_versions import FlowProblem, all_flows, apply_flow, current_flow, record_changed_versions
from .workflow import build_visit_tasks


class SOPEditorTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("flow_admin", password="test-password")
        self.admin.profile.role = "ADMIN"
        self.admin.profile.save()
        self.client.force_login(self.admin)
        self.service = Service.objects.create(code="HAIRCUT", name="Haircut & Styling")
        for code, name in [("SUB025", "Sanitisation"), ("SUB001", "Consultation"), ("WASH", "Hair wash"),
                           ("CUT", "Haircut"), ("FINISH", "Blow dry"), ("MASK", "Hair mask")]:
            sub = SubService.objects.create(code=code, name=name)
            OperationalTask.objects.create(external_id=f"T-{code}", sub_service=sub, name=f"{name} task")
        for sequence, code in [(5, "SUB025"), (10, "WASH"), (20, "CUT"), (30, "FINISH")]:
            ServiceDetail.objects.create(external_id=f"D-{code}", service=self.service,
                                         sub_service=SubService.objects.get(code=code), sequence=sequence)
        self.url = reverse("sop_edit", args=[self.service.pk])
        self.catalogue = f"{reverse('service_catalog')}?service={self.service.pk}"

    def codes(self):
        return [step["code"] for step in current_flow(self.service)]

    def edit(self, action, index=None, **extra):
        data = {"action": action, "digest": service_flow_digest(self.service), **extra}
        if index is not None:
            data["index"] = index
        return self.client.post(self.url, data)

    def test_catalogue_shows_editing_controls_only_to_admins(self):
        page = self.client.get(self.catalogue)
        self.assertContains(page, 'aria-label="Move Hair wash down"')
        self.assertContains(page, "Add a step")
        self.assertContains(page, reverse("sop_history", args=[self.service.pk]))
        self.assertNotContains(page, '<option value="%d">Sanitisation' % SubService.objects.get(code="SUB025").pk)
        for role in ["GENERAL_MANAGER", "MANAGER"]:
            self.admin.profile.role = role
            self.admin.profile.save()
            page = self.client.get(self.catalogue)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, "Add a step")
            self.assertNotContains(page, reverse("sop_edit", args=[self.service.pk]))

    def test_move_wash_to_the_end_and_version_it(self):
        self.assertRedirects(self.edit("down", 0), f"{self.catalogue}#step-2", fetch_redirect_response=False)
        self.edit("down", 1)
        self.assertEqual(self.codes(), ["CUT", "FINISH", "WASH"])
        # Sanitisation mapping is left alone; nothing is deleted.
        self.assertTrue(ServiceDetail.objects.get(external_id="D-SUB025").active)
        self.assertEqual(ServiceDetail.objects.filter(service=self.service).count(), 4)
        versions = list(SOPVersion.objects.order_by("number"))
        self.assertEqual([(v.number, v.source) for v in versions], [(1, "BASELINE"), (2, "EDITOR"), (3, "EDITOR")])
        self.assertEqual([step["code"] for step in versions[0].steps], ["WASH", "CUT", "FINISH"])
        self.assertEqual(versions[2].note, "Moved Hair wash down")
        self.assertEqual(versions[2].created_by, self.admin)
        self.edit("up", 2)
        self.assertEqual(self.codes(), ["CUT", "WASH", "FINISH"])

    def test_moving_past_the_ends_changes_nothing(self):
        self.edit("up", 0)
        self.edit("down", 2)
        self.assertEqual(self.codes(), ["WASH", "CUT", "FINISH"])
        self.assertFalse(SOPVersion.objects.exists())

    def test_remove_and_add_steps(self):
        self.edit("remove", 0)
        self.assertEqual(self.codes(), ["CUT", "FINISH"])
        self.assertFalse(ServiceDetail.objects.get(external_id="D-WASH").active)
        mask = SubService.objects.get(code="MASK")
        self.edit("add", sub_service=mask.pk)
        flow = current_flow(self.service)
        self.assertEqual([step["code"] for step in flow], ["CUT", "FINISH", "MASK"])
        self.assertFalse(flow[-1]["mandatory"])
        # Adding a removed step back reuses its old row and settings.
        self.edit("add", sub_service=SubService.objects.get(code="WASH").pk, mandatory="on")
        self.assertTrue(ServiceDetail.objects.get(external_id="D-WASH").active)
        self.assertEqual(self.codes(), ["CUT", "FINISH", "MASK", "WASH"])
        self.assertEqual(SOPVersion.objects.filter(source="EDITOR").count(), 3)

    def test_opening_steps_and_unknown_ids_cannot_be_added(self):
        self.edit("add", sub_service=SubService.objects.get(code="SUB001").pk)
        self.edit("add", sub_service=999999)
        self.edit("add", sub_service="abc")
        self.edit("remove", 9)
        self.assertEqual(self.codes(), ["WASH", "CUT", "FINISH"])
        self.assertFalse(SOPVersion.objects.exists())

    def test_stale_page_is_refused(self):
        stale = service_flow_digest(self.service)
        self.edit("down", 0)
        response = self.client.post(self.url, {"action": "remove", "index": 0, "digest": stale}, follow=True)
        self.assertContains(response, "changed since you opened the page")
        self.assertEqual(self.codes(), ["CUT", "WASH", "FINISH"])

    def test_new_visits_use_the_edited_order(self):
        self.edit("down", 0)
        self.edit("down", 1)
        branch = Branch.objects.create(code="FLOW", name="Flow branch")
        visit = Visit.objects.create(branch=branch, customer=Customer.objects.create(name="Flow"), created_by=self.admin)
        VisitService.objects.create(visit=visit, service=self.service, employee=self.admin, assigned_by=self.admin)
        build_visit_tasks(visit)
        titles = list(visit.services.get().tasks.order_by("sequence").values_list("sub_service_name", flat=True))
        self.assertEqual(titles, ["Sanitisation", "Consultation", "Haircut", "Blow dry", "Hair wash"])

    def test_restore_brings_back_an_old_version(self):
        self.edit("down", 0)
        baseline = SOPVersion.objects.get(number=1)
        response = self.client.post(reverse("sop_restore", args=[baseline.pk]))
        self.assertRedirects(response, reverse("sop_history", args=[self.service.pk]))
        self.assertEqual(self.codes(), ["WASH", "CUT", "FINISH"])
        latest = SOPVersion.objects.order_by("-number").first()
        self.assertEqual((latest.number, latest.source), (3, "RESTORE"))
        history = self.client.get(reverse("sop_history", args=[self.service.pk]))
        self.assertContains(history, "Restored version 1")
        self.assertContains(history, "Moved Hair wash down")

    def test_apply_flow_refuses_opening_and_unknown_steps(self):
        with self.assertRaises(FlowProblem):
            apply_flow(self.service, [{"code": "SUB001", "mandatory": True}])
        with self.assertRaises(FlowProblem):
            apply_flow(self.service, [{"code": "NOPE", "mandatory": True}])

    def test_bulk_changes_are_versioned_only_for_changed_services(self):
        other = Service.objects.create(code="OTHER", name="Other")
        before = all_flows()
        ServiceDetail.objects.filter(external_id="D-WASH").update(sequence=99)
        created = record_changed_versions(before, "WORKBOOK", user=self.admin, note="Workbook test.xlsx")
        self.assertEqual([version.service for version in created], [self.service])
        self.assertFalse(other.sop_versions.exists())
        self.assertEqual(list(self.service.sop_versions.order_by("number").values_list("source", flat=True)),
                         ["BASELINE", "WORKBOOK"])

    def test_roles(self):
        self.client.logout()
        self.assertEqual(self.edit("down", 0).status_code, 302)
        for role in ["GENERAL_MANAGER", "MANAGER", "EMPLOYEE"]:
            self.admin.profile.role = role
            self.admin.profile.save()
            self.client.force_login(self.admin)
            self.assertEqual(self.edit("down", 0).status_code, 403)
            self.assertEqual(self.client.post(reverse("sop_restore", args=[1])).status_code, 403)
            expected = 403 if role == "EMPLOYEE" else 200
            self.assertEqual(self.client.get(reverse("sop_history", args=[self.service.pk])).status_code, expected)
        self.assertEqual(self.codes(), ["WASH", "CUT", "FINISH"])
