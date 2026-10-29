import re

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Branch, BranchDuty


@override_settings(STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class AdminNavigationTests(TestCase):
    def test_admin_has_desktop_and_mobile_profile_menus_with_sign_out(self):
        user = User.objects.create_user("admin_nav", password="test")
        user.profile.role = "ADMIN"
        user.profile.save()
        self.client.force_login(user)

        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="desktop-session-bar"')
        self.assertContains(response, 'class="account-menu"', count=2)
        self.assertContains(response, 'aria-label="Open profile menu"', count=2)
        self.assertContains(response, 'class="account-signout"', count=2)
        self.assertContains(response, f'href="{reverse("logout")}"')
        sidebar = response.content.decode().split('<aside', 1)[1].split('</aside>', 1)[0]
        self.assertNotIn(f'href="{reverse("logout")}"', sidebar)
        self.assertContains(response, '<html lang="en" data-theme="light">')

        self.assertRedirects(self.client.get(reverse("logout")), reverse("login"))


@override_settings(STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class SidebarIconTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(code='ICONS', name='Icon test branch')

    def person(self, role):
        user = User.objects.create_user('icons_' + role)
        user.profile.role = role
        user.profile.branch = self.branch
        user.profile.save()
        return user

    def check_sidebar(self, user, page_name, expected):
        expected = expected + ['reviews']
        self.client.force_login(user)
        response = self.client.get(reverse(page_name))
        self.assertEqual(response.status_code, 200)
        sidebar = response.content.decode().split('<aside', 1)[1].split('</aside>', 1)[0]
        nav = sidebar.split('<nav>', 1)[1].split('</nav>', 1)[0]
        links = re.findall(r'<a\b[^>]*>(.*?)</a>', nav, flags=re.DOTALL)
        self.assertEqual(re.findall(r'data-icon="([^"]+)"', nav), expected)
        self.assertEqual(len(links), len(expected))
        for link in links:
            self.assertIn('class="nav-icon"', link)
            self.assertIn('aria-hidden="true" focusable="false"', link)
            self.assertIn('<path ', link)
            self.assertIn('<span>', link)
        self.assertNotIn('&#', nav)
        self.assertNotIn(reverse('logout'), sidebar)
        if 'settings' not in expected:
            self.assertNotIn('href="/admin/"', sidebar)
        self.assertContains(response, 'data-icon="menu"', count=1)
        self.assertContains(response, 'data-icon="close"', count=1)
        return response

    def test_all_existing_role_sidebars_use_consistent_decorative_svg_icons(self):
        for role, page, icons in [
            ('ADMIN', 'admin_dashboard', ['home', 'visits', 'reports', 'catalogue', 'user-add', 'staff', 'calendar', 'upload']),
            ('GENERAL_MANAGER', 'general_manager_dashboard', ['home', 'reports', 'user-add', 'calendar', 'staff', 'upload', 'visits', 'reports', 'catalogue']),
            ('MANAGER', 'manager_dashboard', ['home', 'reports', 'new', 'catalogue', 'staff']),
        ]:
            with self.subTest(role=role):
                self.check_sidebar(self.person(role), page, icons)

    def test_rostered_general_manager_receives_acting_branch_icons_only(self):
        user = self.person('GENERAL_MANAGER')
        BranchDuty.objects.create(user=user, date=timezone.localdate(), status='WORK',
                                  branch=self.branch, updated_by=user)
        self.check_sidebar(user, 'general_manager_dashboard',
                           ['home', 'reports', 'user-add', 'calendar', 'staff', 'upload', 'visits', 'reports', 'catalogue', 'new', 'staff'])

    def test_superuser_records_icon_and_staff_minimal_navigation_are_preserved(self):
        admin = User.objects.create_superuser('icons_superuser', '', 'test')
        response = self.check_sidebar(admin, 'admin_dashboard',
                                      ['home', 'visits', 'reports', 'catalogue', 'user-add', 'staff', 'calendar', 'upload', 'settings'])
        self.assertContains(response, 'href="/admin/"')
        employee = self.person('EMPLOYEE')
        self.client.force_login(employee)
        response = self.client.get(reverse('employee_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<aside')
        self.assertContains(response, 'aria-label="Open profile menu"')
