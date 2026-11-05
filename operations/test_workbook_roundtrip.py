"""Download the live SOP workbook, edit it like Excel would, and upload it back."""
import io
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from .models import (
    OperationalTask, Service, ServiceDetail, ServiceTaskSetting, SOPVersion, SOPWorkbookImport, SubService,
)
from .sop_exports import README_SHEET, REFERENCE_LABEL, TASK_ORDER_SHEET
from .sop_versions import apply_flow, current_flow


class WorkbookRoundTripTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("sop_admin", password="test-password")
        self.admin.profile.role = "ADMIN"
        self.admin.profile.save()
        self.client.force_login(self.admin)
        self.url = reverse("workbook_import")
        subs = {}
        for code, name in [("SUB025", "Sanitisation"), ("SUB001", "Consultation"), ("SUB100", "Cleansing"),
                           ("SUB200", "Massage"), ("SUB300", "Mask"), ("SUB400", "Haircut")]:
            subs[code] = SubService.objects.create(code=code, name=name)
            OperationalTask.objects.create(external_id=f"T-{code}-1", sub_service=subs[code], sequence=1,
                                           name=f"{name} first", active_labour_minutes=2)
            OperationalTask.objects.create(external_id=f"T-{code}-2", sub_service=subs[code], sequence=2,
                                           name=f"{name} second", active_labour_minutes=3)
        self.subs = subs
        self.gold = Service.objects.create(code="FAC01", name="Gold facial", category="Facial", base_price=1500)
        self.basic = Service.objects.create(code="FAC02", name="Basic facial", category="Facial", base_price=900)
        self.cut = Service.objects.create(code="HAIR01", name="Haircut", category="Hair", base_price=300)
        for service, codes in [(self.gold, ["SUB100", "SUB300"]), (self.basic, ["SUB100", "SUB300"]),
                               (self.cut, ["SUB400"])]:
            for index, code in enumerate(codes, 1):
                ServiceDetail.objects.create(external_id=f"SD-{service.code}-{index}", service=service,
                                             sub_service=subs[code], sequence=index * 10)

    def download(self):
        response = self.client.get(reverse("workbook_export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        return load_workbook(io.BytesIO(response.content))

    def upload(self, book, filename="edited.xlsx"):
        output = io.BytesIO()
        book.save(output)
        return self.client.post(self.url, {
            "action": "validate", "workbook": SimpleUploadedFile(filename, output.getvalue()),
        })

    def confirm(self):
        stage = SOPWorkbookImport.objects.get(status="PENDING")
        return self.client.post(self.url, {"action": "confirm", "stage": str(stage.pk), "confirmed": "on"})

    @staticmethod
    def rows(sheet):
        headers = [cell.value for cell in sheet[1]]
        return headers, [dict(zip(headers, row)) for row in sheet.iter_rows(min_row=2, values_only=True)]

    @staticmethod
    def append(sheet, values):
        headers = [cell.value for cell in sheet[1]]
        sheet.append([values.get(header) for header in headers])

    def test_only_admins_can_download(self):
        for role in ["GENERAL_MANAGER", "MANAGER", "EMPLOYEE"]:
            self.admin.profile.role = role
            self.admin.profile.save()
            self.assertEqual(self.client.get(reverse("workbook_export")).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("workbook_export")).status_code, 302)

    def test_download_has_every_sheet_names_and_a_catalogue_reference(self):
        book = self.download()
        self.assertEqual(book.sheetnames[0], README_SHEET)
        for name in ["Service Master", "Sub-Service Master", "Inventory Master", "Equipment Master", "Task Master",
                     "Service Detail", "Task Inventory", "Task Equipment", TASK_ORDER_SHEET]:
            self.assertIn(name, book.sheetnames)
        labels = {row[0]: row[1] for row in book[README_SHEET].iter_rows(values_only=True) if row[0]}
        self.assertEqual(len(labels[REFERENCE_LABEL]), 64)
        _, details = self.rows(book["Service Detail"])
        gold = [row for row in details if row["service_code"] == "FAC01"]
        self.assertEqual([row["sub_service_name (info)"] for row in gold], ["Cleansing", "Mask"])
        self.assertEqual({row["service_category (info)"] for row in gold}, {"Facial"})

    def test_unchanged_round_trip_changes_nothing(self):
        before = {service.pk: current_flow(service) for service in Service.objects.all()}
        response = self.upload(self.download())
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response["Location"])
        self.assertContains(page, "No service steps, names or prices change.")
        self.assertContains(page, "Nothing in the workbook differs")
        self.assertNotContains(page, "was not downloaded from this page")
        self.confirm()
        self.assertEqual({service.pk: current_flow(service) for service in Service.objects.all()}, before)
        self.assertFalse(SOPVersion.objects.exists())

    def test_add_a_step_to_every_facial_from_excel(self):
        book = self.download()
        details = book["Service Detail"]
        _, rows = self.rows(details)
        for number, service in enumerate(sorted({row["service_code"] for row in rows
                                                 if row["service_category (info)"] == "Facial"}), 1):
            # Massage between Cleansing (10) and Mask (20).
            self.append(details, {"service_detail_id": f"NEW-{number}", "service_code": service,
                                  "sub_service_code": "SUB200", "sequence": 15, "required_quantity": 1,
                                  "mandatory": True, "can_skip": False, "active": True})
        response = self.upload(book)
        page = self.client.get(response["Location"])
        self.assertContains(page, "2 services will change")
        self.assertContains(page, "Added Massage after Cleansing")
        self.assertNotContains(page, "Haircut</strong>")
        self.assertEqual(ServiceDetail.objects.filter(sub_service__code="SUB200").count(), 0)  # preview only
        self.assertEqual(self.confirm().status_code, 302)
        for service in (self.gold, self.basic):
            self.assertEqual([step["name"] for step in current_flow(service)], ["Cleansing", "Massage", "Mask"])
            version = service.sop_versions.order_by("-number").first()
            self.assertEqual(version.source, "WORKBOOK")
        self.assertEqual([step["name"] for step in current_flow(self.cut)], ["Haircut"])
        self.assertFalse(self.cut.sop_versions.exists())

    def test_add_many_services_with_price_from_excel(self):
        book = self.download()
        for number in range(1, 4):
            code = f"FAC1{number}"
            self.append(book["Service Master"], {
                "service_code": code, "service_name": f"Facial {number}", "service_category": "Facial",
                "standard_duration_minutes": 45, "base_price": 1000 + number, "active": True,
            })
            self.append(book["Service Detail"], {"service_detail_id": f"NEW-{code}", "service_code": code,
                                                 "sub_service_code": "SUB100", "sequence": 10, "mandatory": True})
        page = self.client.get(self.upload(book)["Location"])
        self.assertContains(page, "New service with Cleansing")
        self.confirm()
        created = Service.objects.get(code="FAC12")
        self.assertEqual(created.base_price, Decimal("1002"))
        self.assertEqual([step["name"] for step in current_flow(created)], ["Cleansing"])
        self.assertEqual(created.sop_versions.get().source, "WORKBOOK")

    def test_price_skip_and_switch_off_are_uploaded(self):
        book = self.download()
        headers, _ = self.rows(book["Service Master"])
        for row in book["Service Master"].iter_rows(min_row=2):
            if row[headers.index("service_code")].value == "FAC02":
                row[headers.index("base_price")].value = 950
        headers, _ = self.rows(book["Service Detail"])
        for row in book["Service Detail"].iter_rows(min_row=2):
            if row[headers.index("service_detail_id")].value == "SD-FAC01-2":
                row[headers.index("can_skip")].value = True
            if row[headers.index("service_detail_id")].value == "SD-HAIR01-1":
                row[headers.index("active")].value = False
        page = self.client.get(self.upload(book)["Location"])
        self.assertContains(page, "Base price: 900.00 → 950")
        self.assertContains(page, "Mask can now be skipped")
        self.assertContains(page, "Removed Haircut")
        self.confirm()
        self.basic.refresh_from_db()
        self.assertEqual(self.basic.base_price, Decimal("950"))
        self.assertTrue(ServiceDetail.objects.get(external_id="SD-FAC01-2").can_skip)
        self.assertEqual(current_flow(self.cut), [])

    def test_per_service_task_order_survives_and_can_be_cleared(self):
        steps = current_flow(self.gold)
        steps[0]["tasks"].reverse()
        steps[0]["tasks"][0]["can_skip"] = True
        apply_flow(self.gold, steps)
        self.assertEqual(ServiceTaskSetting.objects.count(), 2)
        self.confirm_upload(self.download())
        self.assertEqual([task["name"] for task in current_flow(self.gold)[0]["tasks"]],
                         ["Cleansing second", "Cleansing first"])
        self.assertTrue(current_flow(self.gold)[0]["tasks"][0]["can_skip"])

        book = self.download()
        sheet = book[TASK_ORDER_SHEET]
        sheet.delete_rows(2, sheet.max_row)
        page = self.client.get(self.upload(book)["Location"])
        self.assertContains(page, "Cleansing tasks: Cleansing first → Cleansing second")
        self.confirm()
        self.assertFalse(ServiceTaskSetting.objects.exists())

    def confirm_upload(self, book):
        self.assertEqual(self.upload(book).status_code, 302)
        self.assertEqual(self.confirm().status_code, 302)

    def test_upload_stops_when_catalogue_changed_after_download(self):
        book = self.download()
        # Someone else edits the catalogue meanwhile.
        steps = current_flow(self.basic)
        steps.reverse()
        apply_flow(self.basic, steps)
        response = self.upload(book)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The catalogue changed after this workbook was downloaded")
        self.assertFalse(SOPWorkbookImport.objects.exists())
        self.assertEqual([step["name"] for step in current_flow(self.basic)], ["Mask", "Cleansing"])
        # A fresh download goes through.
        self.assertEqual(self.upload(self.download()).status_code, 302)

    def test_task_order_sheet_rejects_duplicates(self):
        book = self.download()
        sheet = book[TASK_ORDER_SHEET]
        for _ in range(2):
            self.append(sheet, {"service_code": "FAC01", "task_id": "T-SUB100-1", "position": 10})
        response = self.upload(book)
        self.assertContains(response, "Service Task Order, row 3: missing or duplicate identifier.")
