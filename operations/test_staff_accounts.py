from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Branch


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}},
)
class StaffAccountTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(code='HS01', name='Synthetic branch')
        self.admin = self.person('administrator', 'ADMIN')
        self.gm = self.person('generalmanager', 'GENERAL_MANAGER')
        self.manager = self.person('manager', 'MANAGER')
        self.employee = self.person('employee', 'EMPLOYEE')

    def person(self, username, role):
        user = User.objects.create_user(username)
        user.profile.role = role
        user.profile.branch = self.branch
        user.profile.save()
        return user

    def payload(self, **overrides):
        return {'branch': self.branch.pk, 'first_name': 'Alex', 'last_name': 'Smith',
                'role': 'EMPLOYEE', **overrides}

    def test_admin_and_gm_create_accounts_with_copyable_generated_credentials(self):
        for actor, expected in ((self.admin, 'hs01.alex.smith'), (self.gm, 'hs01.alex.smith.2')):
            self.client.force_login(actor)
            response = self.client.post(reverse('create_user'), self.payload())
            self.assertEqual(response.status_code, 200)
            user = response.context['created_account']
            password = response.context['generated_password']
            self.assertEqual(user.username, expected)
            self.assertEqual(user.last_name, 'Smith')
            self.assertEqual(user.profile.branch, self.branch)
            self.assertFalse(user.is_staff)
            self.assertFalse(user.is_superuser)
            self.assertEqual(len(password), 8)
            self.assertTrue(user.check_password(password))
            self.assertContains(response, 'Copy username')
            self.assertContains(response, 'Copy password')
            self.assertIn('no-store', response['Cache-Control'])
            audit = LogEntry.objects.get(object_id=str(user.pk), action_flag=ADDITION)
            self.assertEqual(audit.user, actor)
            self.assertNotIn(password, audit.change_message)
            # A new GET never re-exposes the temporary password.
            self.assertNotContains(self.client.get(reverse('create_user')), password)

    def test_preview_is_read_only_and_handles_case_insensitive_collisions(self):
        User.objects.create_user('HS01.ALEX')
        User.objects.create_user('hs01.alex.2')
        self.client.force_login(self.gm)
        before = User.objects.count()
        response = self.client.get(reverse('create_user'), {
            'preview': '1', **self.payload(last_name=''),
        })
        self.assertEqual(response.json()['username'], 'hs01.alex.3')
        self.assertEqual(User.objects.count(), before)
        response = self.client.post(reverse('create_user'), self.payload(last_name='', username='tampered'))
        self.assertEqual(response.context['created_account'].username, 'hs01.alex.3')
        self.assertEqual(response.context['created_account'].last_name, '')

    def test_gm_cannot_create_admin_or_general_manager(self):
        self.client.force_login(self.gm)
        for role in ('ADMIN', 'GENERAL_MANAGER'):
            response = self.client.post(reverse('create_user'), self.payload(role=role))
            self.assertIn('role', response.context['form'].errors)
        self.assertFalse(User.objects.filter(first_name='Alex').exists())
        response = self.client.post(reverse('create_user'), self.payload(role='MANAGER'))
        self.assertEqual(response.context['created_account'].profile.role, 'MANAGER')

    def test_unauthorized_roles_cannot_create_or_preview_accounts(self):
        for user in (self.manager, self.employee):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('create_user')).status_code, 403)
            self.assertEqual(self.client.get(reverse('create_user'), {'preview': '1'}).status_code, 403)
            self.assertEqual(self.client.post(reverse('create_user'), self.payload()).status_code, 403)

    def test_validation_rejects_missing_inactive_branch_and_invalid_names(self):
        self.client.force_login(self.admin)
        for branch in ('', '999999'):
            response = self.client.post(reverse('create_user'), self.payload(branch=branch))
            self.assertIn('branch', response.context['form'].errors)
        response = self.client.post(reverse('create_user'), self.payload(first_name='!!!'))
        self.assertIn('first_name', response.context['form'].errors)
        self.branch.active = False
        self.branch.save()
        response = self.client.post(reverse('create_user'), self.payload())
        self.assertIn('branch', response.context['form'].errors)
        self.assertFalse(User.objects.filter(first_name='Alex').exists())

    def test_form_and_dashboard_show_creation_flow(self):
        self.client.force_login(self.gm)
        response = self.client.get(reverse('create_user'))
        fields = list(response.context['form'].fields)
        self.assertEqual(fields[:4], ['branch', 'first_name', 'last_name', 'username'])
        self.assertContains(response, 'disabled')
        self.assertContains(response, 'staff-account.js')
        self.assertContains(self.client.get(reverse('general_manager_dashboard')), 'Add team member')
        self.assertContains(self.client.get(reverse('general_manager_staff')), 'Add team member')
