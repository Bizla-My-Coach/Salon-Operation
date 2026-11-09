"""Rename service codes in place from an old → new sheet."""
import io

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from .models import Service, ServiceDetail, SOPVersion, SubService


class ServiceCodeRenameTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("code_admin", password="test-password")
        self.admin.profile.role = "ADMIN"
        self.admin.profile.save()
        self.client.force_login(self.admin)
        self.url = reverse("service_code_rename")
        self.a = Service.objects.create(code="SER013", name="Silver package", category="Wedding")
        self.b = Service.objects.create(code="SER294", name="Diamond package", category="Wedding")
        self.c = Service.objects.create(code="PK-SMGOLD", name="Pure gold facial", category="Facial")
        sub = SubService.objects.create(code="SUB100", name="Cleansing")
        ServiceDetail.objects.create(external_id="SD-1", service=self.c, sub_service=sub, sequence=10)
        SOPVersion.objects.create(service=self.c, number=1, source="EDITOR", steps=[])

    def sheet(self, pairs, extra_header=False):
        book = Workbook()
        sheet = book.active
        sheet.append(["new_code", "old_code"] + (["service_name"] if extra_header else []))
        for old, new in pairs:
            sheet.append([new, old])
        output = io.BytesIO()
        book.save(output)
        return SimpleUploadedFile("codes.xlsx", output.getvalue())

    def check(self, pairs):
        return self.client.post(self.url, {"sheet": self.sheet(pairs)})

    def confirm(self, response):
        return self.client.post(self.url, {"action": "confirm", "plan": response.context["preview"]["plan"]}, follow=True)

    def test_only_admins(self):
        for role in ["GENERAL_MANAGER", "MANAGER", "EMPLOYEE"]:
            self.admin.profile.role = role
            self.admin.profile.save()
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.get(reverse("service_code_sheet")).status_code, 403)

    def test_swap_and_rename_keep_steps_history_and_ids(self):
        response = self.check([("SER013", "SER294"), ("SER294", "SER013"), ("PK-SMGOLD", "ser100")])
        self.assertContains(response, "3 codes will change")
        self.assertEqual(Service.objects.get(pk=self.a.pk).code, "SER013")  # preview only
        page = self.confirm(response)
        self.assertContains(page, "Renamed 3 service codes")
        self.a.refresh_from_db(); self.b.refresh_from_db(); self.c.refresh_from_db()
        self.assertEqual((self.a.code, self.b.code, self.c.code), ("SER294", "SER013", "SER100"))
        self.assertEqual(Service.objects.count(), 3)
        self.assertEqual(self.c.service_details.count(), 1)
        self.assertEqual(self.c.sop_versions.count(), 1)
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.c.pk),
                                                change_message="Service code renamed from PK-SMGOLD to SER100").exists())

    def test_clash_with_a_code_not_being_renamed_is_refused(self):
        response = self.check([("PK-SMGOLD", "SER013")])
        self.assertContains(response, "SER013 is already used by Silver package")
        self.assertNotContains(response, "Rename 1 code")

    def test_bad_rows_are_listed(self):
        response = self.check([("NOPE", "SER500"), ("SER013", "SER501"), ("SER013", "SER502"),
                               ("SER294", "SER501"), ("PK-SMGOLD", "bad code")])
        for text in ["Row 2: no service has the code NOPE", "SER013 is already renamed on row 3",
                     "SER501 is also the new code on row 3", "BAD CODE is not a valid code"]:
            self.assertContains(response, text)

    def test_preview_goes_stale_when_codes_change(self):
        response = self.check([("SER013", "SER001")])
        Service.objects.filter(pk=self.b.pk).update(code="SER777")
        page = self.confirm(response)
        self.assertContains(page, "Service codes changed since the preview")
        self.a.refresh_from_db()
        self.assertEqual(self.a.code, "SER013")

    def test_unchanged_sheet_and_csv(self):
        upload = SimpleUploadedFile("codes.csv", "old_code,new_code\nSER013,SER013\n".encode())
        response = self.client.post(self.url, {"sheet": upload})
        self.assertContains(response, "Nothing to rename")

    def test_downloaded_sheet_uploads_back_unchanged(self):
        response = self.client.get(reverse("service_code_sheet"))
        book = load_workbook(io.BytesIO(response.content))
        rows = list(book.active.iter_rows(values_only=True))
        self.assertEqual(rows[0][:2], ("old_code", "new_code"))
        self.assertEqual(len(rows), 4)
        page = self.client.post(self.url, {"sheet": SimpleUploadedFile("s.xlsx", response.content)})
        self.assertContains(page, "Nothing to rename")
