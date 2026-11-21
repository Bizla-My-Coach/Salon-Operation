import datetime
from zoneinfo import ZoneInfo

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Branch, Customer, Visit

IST = ZoneInfo("Asia/Kolkata")


class VisitAdminBulkStatusTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(code="B1", name="Test Branch")
        self.admin_user = User.objects.create_superuser("boss", password="test")
        self.admin_user.profile.role = "ADMIN"
        self.admin_user.profile.save()
        self.customer = Customer.objects.create(name="Test Customer")
        self.client.force_login(self.admin_user)
        self.url = reverse("admin:operations_visit_changelist")

    def visit(self, status, day):
        visit = Visit.objects.create(branch=self.branch, customer=self.customer, status=status,
                                     created_by=self.admin_user)
        created = datetime.datetime(2026, 11, day, 21, 0, tzinfo=IST)
        Visit.objects.filter(pk=visit.pk).update(created_at=created)
        return visit

    def test_date_filter_shows_visits_up_to_and_including_the_day(self):
        on_day = self.visit("ASSIGNED", 20)
        before = self.visit("WAITING", 1)
        after = self.visit("ASSIGNED", 21)
        response = self.client.get(self.url, {"created_until": "2026-11-20"})
        ids = {v.pk for v in response.context["cl"].result_list}
        self.assertEqual(ids, {on_day.pk, before.pk})
        self.assertNotIn(after.pk, ids)
        self.assertContains(response, 'name="created_until"')

    def test_bad_date_is_ignored(self):
        self.visit("ASSIGNED", 20)
        self.visit("ASSIGNED", 21)
        response = self.client.get(self.url, {"created_until": "not-a-date"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["cl"].result_list), 2)

    def test_every_status_has_an_action(self):
        response = self.client.get(self.url)
        choices = {name for name, _ in response.context["action_form"].fields["action"].choices}
        for code, _ in Visit.STATUS:
            self.assertIn(f"mark_visits_{code.lower()}", choices)

    def test_bulk_close_changes_only_selected_and_sets_closed_time(self):
        a = self.visit("IN_PROGRESS", 19)
        b = self.visit("INVOICED", 20)
        already = self.visit("CLOSED", 18)
        untouched = self.visit("ASSIGNED", 21)
        response = self.client.post(self.url, {
            "action": "mark_visits_closed",
            "_selected_action": [a.pk, b.pk, already.pk],
        }, follow=True)
        self.assertContains(response, "2 visit(s) changed to Closed. 1 were already Closed.")
        for v in (a, b):
            v.refresh_from_db()
            self.assertEqual(v.status, "CLOSED")
            self.assertIsNotNone(v.closed_at)
        already.refresh_from_db()
        self.assertIsNone(already.closed_at)  # previous close time is not overwritten
        untouched.refresh_from_db()
        self.assertEqual(untouched.status, "ASSIGNED")
        self.assertEqual(LogEntry.objects.filter(object_id__in=[str(a.pk), str(b.pk)]).count(), 2)

    def test_select_all_with_date_filter_changes_every_matching_visit(self):
        old = [self.visit("ASSIGNED", d) for d in (1, 10, 20)]
        new = self.visit("ASSIGNED", 21)
        self.client.post(self.url + "?created_until=2026-11-20", {
            "action": "mark_visits_cancelled",
            "select_across": "1",
            "_selected_action": [old[0].pk],
        })
        self.assertEqual(Visit.objects.filter(pk__in=[v.pk for v in old], status="CANCELLED").count(), 3)
        new.refresh_from_db()
        self.assertEqual(new.status, "ASSIGNED")

    def test_staff_without_change_permission_gets_no_actions(self):
        viewer = User.objects.create_user("viewer", password="test", is_staff=True)
        from django.contrib.auth.models import Permission
        viewer.user_permissions.add(Permission.objects.get(codename="view_visit"))
        self.client.force_login(viewer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context.get("action_form"))
