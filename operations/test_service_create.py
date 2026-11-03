from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import (
    InventoryItem, OperationalTask, Service, ServiceDetail, ServiceTaskSetting, SOPVersion, SubService, TaskInventory,
)
from .sop_versions import apply_flow, current_flow
from .service_create import suggest_code


class ServiceCreateTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("svc_admin", password="test-password")
        self.admin.profile.role = "ADMIN"
        self.admin.profile.save()
        self.client.force_login(self.admin)
        self.source = Service.objects.create(code="SER010", name="Men's Haircut", category="Hair",
                                             customer_type="Men", standard_duration_minutes=40, base_price=350)
        for code in ["SUB025", "WASH", "CUT"]:
            sub = SubService.objects.create(code=code, name=code.title())
            OperationalTask.objects.create(external_id=f"T1-{code}", sub_service=sub, sequence=10, name=f"{code} one")
            OperationalTask.objects.create(external_id=f"T2-{code}", sub_service=sub, sequence=20, name=f"{code} two")
        ServiceDetail.objects.create(external_id="D-SAN", service=self.source,
                                     sub_service=SubService.objects.get(code="SUB025"), sequence=5)
        apply_flow(self.source, [
            {"code": "CUT", "mandatory": True, "can_skip": False},
            {"code": "WASH", "mandatory": True, "can_skip": True,
             "tasks": [{"id": "T2-WASH", "can_skip": True}, {"id": "T1-WASH", "can_skip": False}]},
        ])
        shampoo = InventoryItem.objects.create(code="INV1", name="Shampoo")
        TaskInventory.objects.create(external_id="TI-SRC", task=OperationalTask.objects.get(external_id="T1-WASH"),
                                     service=self.source, inventory=shampoo, standard_quantity=15, unit="ml")
        self.url = reverse("service_create")

    def test_copy_keeps_steps_task_order_skips_and_inventory(self):
        response = self.client.post(self.url, {"start": "copy", "source": self.source.pk, "name": "Kids Haircut",
                                                "code": "ser011", "base_price": "250"})
        copy = Service.objects.get(code="SER011")
        self.assertRedirects(response, f"{reverse('service_catalog')}?service={copy.pk}", fetch_redirect_response=False)
        self.assertEqual(copy.name, "Kids Haircut")
        self.assertEqual(copy.base_price, 250)
        self.assertEqual((copy.category, copy.customer_type, copy.standard_duration_minutes), ("Hair", "Men", 40))
        self.assertEqual(current_flow(copy), current_flow(self.source))
        self.assertTrue(ServiceTaskSetting.objects.filter(service=copy, task__external_id="T2-WASH", can_skip=True).exists())
        self.assertEqual(TaskInventory.objects.filter(service=copy).count(), 1)
        version = SOPVersion.objects.get(service=copy)
        self.assertEqual((version.number, version.note), (1, "Created as a copy of Men's Haircut"))
        # The original is untouched.
        self.assertEqual(TaskInventory.objects.filter(service=self.source).count(), 1)
        self.assertFalse(SOPVersion.objects.filter(service=self.source).exists())

    def test_blank_service_starts_with_no_steps(self):
        self.client.post(self.url, {"start": "blank", "source": self.source.pk, "name": "Bridal Styling", "code": "BR1",
                                    "standard_duration_minutes": "75"})
        service = Service.objects.get(code="BR1")
        self.assertEqual(current_flow(service), [])
        self.assertEqual(service.standard_duration_minutes, 75)
        self.assertFalse(TaskInventory.objects.filter(service=service).exists())
        response = self.client.get(f"{reverse('service_catalog')}?service={service.pk}")
        self.assertContains(response, "This service has no steps yet")

    def test_duplicate_name_or_code_is_refused(self):
        response = self.client.post(self.url, {"start": "copy", "source": self.source.pk, "name": "men's haircut",
                                               "code": "SER010"})
        self.assertContains(response, "A service with this name already exists")
        self.assertContains(response, "This code is already used")
        self.assertEqual(Service.objects.count(), 1)

    def test_copy_needs_a_source(self):
        response = self.client.post(self.url, {"start": "copy", "name": "New", "code": "N1"})
        self.assertContains(response, "Choose the service to copy.")

    def test_only_admins_can_add_services(self):
        manager = User.objects.create_user("svc_manager", password="test-password")
        manager.profile.role = "GENERAL_MANAGER"
        manager.profile.save()
        self.client.force_login(manager)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        catalogue = self.client.get(f"{reverse('service_catalog')}?service={self.source.pk}")
        self.assertNotContains(catalogue, "Add service")
        self.assertNotContains(catalogue, "Copy this service")

    def test_code_suggestion_follows_existing_pattern(self):
        self.assertEqual(suggest_code(), "SER011")
        page = self.client.get(f"{self.url}?from={self.source.pk}")
        self.assertContains(page, 'value="SER011"')
