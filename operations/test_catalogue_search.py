from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Service, SubService


class CatalogueSearchTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user("cat_admin", password="test-password")
        admin.profile.role = "ADMIN"
        admin.profile.save()
        self.client.force_login(admin)
        # Category order would put Facial before Hair; codes are deliberately out of name order.
        self.zebra = Service.objects.create(code="SER001", name="zebra wash", category="Hair")
        self.gold = Service.objects.create(code="SER009", name="Gold Facial", category="Facial")
        self.aroma = Service.objects.create(code="SER005", name="Aroma Magic Silver Facial", category="Facial")
        Service.objects.create(code="SER099", name="Archived", category="Facial", active=False)
        for code, name in [("SUB9", "toner"), ("SUB1", "Massage cream"), ("SUB5", "Cleanse")]:
            SubService.objects.create(code=code, name=name)

    def test_service_dropdown_is_a_to_z_and_searchable(self):
        page = self.client.get(reverse("service_catalog"))
        names = [s.name for s in page.context["form"].fields["service"].queryset]
        self.assertEqual(names, ["Aroma Magic Silver Facial", "Gold Facial", "zebra wash"])
        html = page.content.decode()
        self.assertLess(html.index("Aroma Magic Silver Facial · Facial · SER005"), html.index("Gold Facial · Facial · SER009"))
        self.assertLess(html.index("Gold Facial · Facial · SER009"), html.index("zebra wash · Hair · SER001"))
        self.assertContains(page, 'data-filters="id_service"')
        self.assertContains(page, "3 services, A to Z")
        self.assertContains(page, "js/select-filter.")

    def test_add_step_dropdown_is_a_to_z_and_searchable(self):
        page = self.client.get(f"{reverse('service_catalog')}?service={self.gold.pk}")
        self.assertEqual([s.name for s in page.context["editor"]["sub_services"]], ["Cleanse", "Massage cream", "toner"])
        self.assertContains(page, 'data-filters="add-sub-service"')
        self.assertContains(page, "3 sub-services, A to Z")
