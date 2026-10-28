from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.admin.models import CHANGE, LogEntry
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Branch, BranchDuty, Customer, Service, Visit, VisitService, VisitTask


@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class AccountTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(code='A', name='Branch A')
        self.user = User.objects.create_user('self_user', password='Old-test-pass!27', first_name='Original')
        self.user.profile.branch = self.branch
        self.user.profile.save()
        self.other = User.objects.create_user('other_user', password='Other-test-pass!27')
        self.client.force_login(self.user)

    def details(self, **extra):
        return dict(action='details', first_name='Updated', last_name='Person',
                    email='updated@example.com', mobile='+91 98765 43210', **extra)

    def password(self, old='Old-test-pass!27', new='Fresh-test-pass!92', confirmation=None):
        return dict(action='password', old_password=old, new_password1=new,
                    new_password2=confirmation if confirmation is not None else new)

    def test_profile_and_icon_menu_available_for_every_role_without_branch_duty(self):
        for role in ['ADMIN', 'GENERAL_MANAGER', 'MANAGER', 'EMPLOYEE']:
            self.user.profile.role = role
            self.user.profile.save()
            response = self.client.get(reverse('my_profile'))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'aria-label="Open profile menu"')
            self.assertContains(response, f'href="{reverse("my_profile")}"')
            self.assertContains(response, 'role="switch" aria-label="Dark mode"')
            self.assertContains(response, 'data-theme-key="hairship-theme-')
            self.assertContains(response, 'Save details')
            self.assertContains(response, 'Change password')
        self.user.is_superuser = True
        self.user.save()
        self.assertEqual(self.client.get(reverse('my_profile')).status_code, 200)

    def test_theme_is_saved_for_only_current_account_and_restored_across_logins(self):
        url = reverse('save_theme_preference')
        for theme in ['dark', 'light']:
            response = self.client.post(url, {'theme': theme, 'user_id': self.other.pk, 'role': 'ADMIN'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'theme': theme})
            self.user.refresh_from_db()
            self.other.refresh_from_db()
            self.assertEqual(self.user.profile.theme, theme)
            self.assertEqual(self.user.profile.role, 'EMPLOYEE')
            self.assertEqual(self.other.profile.theme, '')
            self.client.logout()
            self.assertTrue(self.client.login(username=self.user.username, password='Old-test-pass!27'))
            other_device = Client()
            other_device.force_login(self.user)
            for client in [self.client, other_device]:
                page = client.get(reverse('my_profile'))
                self.assertContains(page, f'<html lang="en" data-theme="{theme}">')
            other_device.force_login(self.other)
            self.assertContains(other_device.get(reverse('my_profile')), '<html lang="en" data-theme="light">')

    def test_theme_save_is_available_for_all_roles_without_branch_duty(self):
        for role in ['ADMIN', 'GENERAL_MANAGER', 'MANAGER', 'EMPLOYEE']:
            self.user.profile.role = role
            self.user.profile.branch = None
            self.user.profile.save()
            response = self.client.post(reverse('save_theme_preference'), {'theme': 'dark'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'theme': 'dark'})
            self.user.refresh_from_db()

    def test_legacy_browser_theme_does_not_overwrite_an_existing_account_choice(self):
        url = reverse('save_theme_preference')
        self.assertEqual(self.client.post(url, {'theme': 'dark', 'only_if_unset': '1'}).json(), {'theme': 'dark'})
        self.assertEqual(self.client.post(url, {'theme': 'light', 'only_if_unset': '1'}).json(), {'theme': 'dark'})
        self.assertEqual(self.client.post(url, {'theme': 'light'}).json(), {'theme': 'light'})

    def test_theme_save_rejects_invalid_choices_and_requires_post_login_active_profile_and_csrf(self):
        url = reverse('save_theme_preference')
        for theme in ['', 'system', '<script>', None]:
            response = self.client.post(url, {} if theme is None else {'theme': theme})
            self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile.theme, '')
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(url, {'theme': 'dark'}).status_code, 302)
        self.client.force_login(self.user)
        self.user.profile.active = False
        self.user.profile.save()
        self.assertEqual(self.client.post(url, {'theme': 'dark'}).status_code, 403)
        self.user.profile.active = True
        self.user.profile.save()
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.user)
        self.assertEqual(protected.post(url, {'theme': 'dark'}).status_code, 403)
        protected.get(reverse('my_profile'))
        response = protected.post(url, {'theme': 'dark'}, HTTP_X_CSRFTOKEN=protected.cookies['csrftoken'].value)
        self.assertEqual(response.status_code, 200)

    def test_self_details_update_cannot_target_another_user_or_change_authorization(self):
        response = self.client.post(reverse('my_profile'), self.details(
            user_id=self.other.pk, username='other_user', role='ADMIN',
            branch='', is_superuser='true', is_staff='true', active='false',
            employee_code='OVERRIDE', job_title='Director', password='unsafe',
        ))
        self.assertRedirects(response, reverse('my_profile'))
        self.user.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Updated')
        self.assertEqual(self.user.last_name, 'Person')
        self.assertEqual(self.user.email, 'updated@example.com')
        self.assertEqual(self.user.profile.mobile, '+91 98765 43210')
        self.assertEqual(self.user.username, 'self_user')
        self.assertEqual(self.user.profile.role, 'EMPLOYEE')
        self.assertEqual(self.user.profile.branch, self.branch)
        self.assertEqual(self.user.profile.employee_code, '')
        self.assertEqual(self.user.profile.job_title, '')
        self.assertTrue(self.user.profile.active)
        self.assertFalse(self.user.is_superuser)
        self.assertFalse(self.user.is_staff)
        self.assertTrue(self.user.check_password('Old-test-pass!27'))
        self.assertEqual(self.other.first_name, '')

    def test_invalid_details_do_not_save(self):
        data = self.details()
        data.update(email='invalid', mobile='not a phone')
        response = self.client.post(reverse('my_profile'), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['details_form'].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Original')
        self.assertEqual(self.user.profile.mobile, '')

    def test_password_change_checks_current_password_and_keeps_current_session(self):
        response = self.client.post(reverse('my_profile'), self.password())
        self.assertRedirects(response, reverse('my_profile'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Fresh-test-pass!92'))
        self.assertFalse(self.user.check_password('Old-test-pass!27'))
        self.assertEqual(int(self.client.session['_auth_user_id']), self.user.pk)
        self.assertFalse(Client().login(username=self.user.username, password='Old-test-pass!27'))
        self.assertTrue(Client().login(username=self.user.username, password='Fresh-test-pass!92'))

    def test_password_errors_do_not_change_password_or_details(self):
        for data in [self.password(old='wrong'), self.password(confirmation='different'),
                     self.password(new='short'), self.password(new='1234567890')]:
            data['first_name'] = 'Must not change'
            response = self.client.post(reverse('my_profile'), data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['password_form'].errors)
            self.user.refresh_from_db()
            self.assertTrue(self.user.check_password('Old-test-pass!27'))
            self.assertEqual(self.user.first_name, 'Original')

    def test_profile_requires_authentication_active_profile_and_csrf(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('my_profile')).status_code, 302)
        self.assertEqual(self.client.post(reverse('my_profile'), self.details()).status_code, 302)
        self.client.force_login(self.user)
        self.user.profile.active = False
        self.user.profile.save()
        self.assertEqual(self.client.get(reverse('my_profile')).status_code, 403)
        self.user.profile.active = True
        self.user.profile.save()
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.user)
        self.assertEqual(protected.post(reverse('my_profile'), self.password()).status_code, 403)


    def test_change_password_page_and_menu_for_all_roles_without_branch_duty(self):
        for role in ['ADMIN', 'GENERAL_MANAGER', 'MANAGER', 'EMPLOYEE']:
            self.user.profile.role = role
            self.user.profile.save()
            response = self.client.get(reverse('change_password'))
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, 'operations/change_password.html')
            # Admin/manager layouts have separate desktop and mobile profile menus.
            self.assertContains(response, f'href="{reverse("change_password")}"',
                                count=1 if role == 'EMPLOYEE' else 2)
            self.assertContains(response, 'autocomplete="current-password"')
            self.assertContains(response, 'autocomplete="new-password"', count=2)
            self.assertIn('no-store', response['Cache-Control'])

    def test_dedicated_password_page_changes_only_self_and_preserves_session(self):
        other_session = Client()
        other_session.force_login(self.user)
        data = self.password(new='R7!mQ2#x')
        data.update(user_id=self.other.pk, first_name='Must not change', role='ADMIN')
        response = self.client.post(reverse('change_password'), data)
        self.assertRedirects(response, reverse('change_password'))
        self.user.refresh_from_db()
        self.other.refresh_from_db()
        self.assertTrue(self.user.check_password('R7!mQ2#x'))
        self.assertEqual(self.user.first_name, 'Original')
        self.assertEqual(self.user.profile.role, 'EMPLOYEE')
        self.assertTrue(self.other.check_password('Other-test-pass!27'))
        self.assertEqual(self.client.get(reverse('my_profile')).status_code, 200)
        self.assertEqual(other_session.get(reverse('my_profile')).status_code, 302)
        self.assertFalse(Client().login(username=self.user.username, password='Old-test-pass!27'))
        self.assertTrue(Client().login(username=self.user.username, password='R7!mQ2#x'))

    def test_dedicated_password_page_rejects_invalid_passwords(self):
        for data in [self.password(old='wrong'), self.password(confirmation='different'),
                     self.password(new='short'), self.password(new='12345678')]:
            response = self.client.post(reverse('change_password'), data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['password_form'].errors)
            self.user.refresh_from_db()
            self.assertTrue(self.user.check_password('Old-test-pass!27'))

    def test_dedicated_password_page_requires_active_login_and_csrf(self):
        url = reverse('change_password')
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.post(url, self.password()).status_code, 302)
        self.client.force_login(self.user)
        self.user.profile.active = False
        self.user.profile.save()
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.password()).status_code, 403)
        self.user.profile.active = True
        self.user.profile.save()
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.user)
        self.assertEqual(protected.post(url, self.password()).status_code, 403)


@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ManagerStaffTests(TestCase):
    def setUp(self):
        self.a = Branch.objects.create(code='A', name='Branch A')
        self.b = Branch.objects.create(code='B', name='Branch B')
        self.manager = self.person('manager', 'MANAGER', self.a)
        self.local = self.person('local', 'EMPLOYEE', self.a)
        self.away = self.person('away', 'EMPLOYEE', self.a)
        self.leave = self.person('leave', 'EMPLOYEE', self.a)
        self.visitor = self.person('cover', 'EMPLOYEE', self.b)
        self.foreign = self.person('foreign', 'EMPLOYEE', self.b)
        self.gm = self.person('gm', 'GENERAL_MANAGER', None)
        self.duty(self.away, self.b)
        self.duty(self.leave, status='LEAVE')
        self.duty(self.visitor, self.a)

    def person(self, username, role, branch):
        user = User.objects.create_user(username, password='Account-test-pass!41')
        user.profile.role = role
        user.profile.branch = branch
        user.profile.save()
        return user

    def duty(self, user, branch=None, status='WORK'):
        return BranchDuty.objects.create(user=user, branch=branch, status=status,
                                        date=timezone.localdate(), updated_by=self.manager)

    def test_manager_sees_branch_staff_leave_and_cover_not_other_branches(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('manager_staff') + '?branch=' + str(self.b.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual({p.pk for p in response.context['staff']},
                         {self.local.pk, self.away.pk, self.leave.pk, self.visitor.pk})
        self.assertContains(response, 'On leave')
        self.assertContains(response, 'Working at another branch')
        self.assertContains(response, f'href="{reverse("manager_staff")}"')
        self.assertNotContains(response, self.foreign.username)
        self.assertEqual(self.client.post(reverse('manager_staff'), {}).status_code, 405)

    def test_compact_staff_layout_preserves_contact_and_work_details(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('manager_staff'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="page-head staff-page-head"')
        self.assertContains(response, '<h1>All staff</h1>')
        self.assertContains(response, 'class="staff-card-head"', count=4)
        for label in ['Username', 'Email', 'Mobile', 'Open services here']:
            self.assertContains(response, f'<dt>{label}</dt>', count=4)
        self.assertContains(response, 'On leave')
        self.assertContains(response, 'Working at another branch')

    def test_employee_and_unrostered_general_manager_cannot_access_staff_list(self):
        for user in [self.local, self.gm]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('manager_staff')).status_code, 403)
        self.duty(self.gm, self.a)
        self.client.force_login(self.gm)
        self.assertEqual(self.client.get(reverse('manager_staff')).status_code, 200)

    def test_roster_changes_manager_scope_and_leave_blocks_staff_access(self):
        duty = self.duty(self.manager, self.b)
        self.client.force_login(self.manager)
        response = self.client.get(reverse('manager_staff'))
        self.assertEqual(response.context['branch'], self.b)
        self.assertNotIn(self.local.pk, {p.pk for p in response.context['staff']})
        duty.status = 'LEAVE'
        duty.branch = None
        duty.save()
        self.assertEqual(self.client.get(reverse('manager_staff')).status_code, 403)

    def reset_url(self, user):
        return reverse('staff_password_reset', args=[user.pk])

    def reset_data(self, **changes):
        data = dict(actor_password='Account-test-pass!41',
                    new_password1='Recovered-staff-pass!82', new_password2='Recovered-staff-pass!82')
        data.update(changes)
        return data

    def test_general_manager_directory_shows_all_branches_without_acting_duty(self):
        self.client.force_login(self.gm)
        response = self.client.get(reverse('general_manager_staff'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual({p.pk for p in response.context['staff']}, {
            self.manager.pk, self.gm.pk, self.local.pk, self.away.pk,
            self.leave.pk, self.visitor.pk, self.foreign.pk,
        })
        self.assertContains(response, 'Home branch')
        self.assertContains(response, 'Working branch today')
        self.assertContains(response, 'Branch A')
        self.assertContains(response, 'Branch B')
        self.assertContains(response, 'On leave')
        self.assertContains(response, 'Not rostered today')
        self.assertContains(response, f'href="{reverse("general_manager_staff")}"')
        self.duty(self.gm, self.a)
        response = self.client.get(reverse('general_manager_staff'))
        self.assertIn(self.foreign.pk, {p.pk for p in response.context['staff']})
        self.assertEqual(self.client.post(reverse('general_manager_staff'), {}).status_code, 405)

    def test_directory_search_pagination_and_role_guards(self):
        self.client.force_login(self.gm)
        response = self.client.get(reverse('general_manager_staff'), {'q': 'foreign'})
        self.assertEqual([p.pk for p in response.context['staff']], [self.foreign.pk])
        for i in range(26):
            self.person('extra_%02d' % i, 'EMPLOYEE', self.b)
        response = self.client.get(reverse('general_manager_staff'))
        self.assertEqual(len(response.context['staff']), 24)
        self.assertContains(response, 'Next')
        response = self.client.get(reverse('general_manager_staff'), {'page': 2})
        self.assertEqual(len(response.context['staff']), 9)
        for user in [self.manager, self.local]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('general_manager_staff')).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('general_manager_staff')).status_code, 302)

    def test_manager_resets_employee_password_only_and_invalidates_employee_session(self):
        employee_client = Client()
        employee_client.force_login(self.local)
        self.client.force_login(self.manager)
        response = self.client.get(self.reset_url(self.local))
        self.assertContains(response, 'Your current password')
        self.assertContains(response, 'Back to staff')
        self.assertFalse(LogEntry.objects.exists())
        response = self.client.post(self.reset_url(self.local), self.reset_data(
            role='ADMIN', is_staff='true', user_id=self.foreign.pk,
        ))
        self.assertRedirects(response, reverse('manager_staff'))
        self.local.refresh_from_db()
        self.assertTrue(self.local.check_password('Recovered-staff-pass!82'))
        self.assertEqual(self.local.profile.branch, self.a)
        self.assertEqual(self.local.profile.role, 'EMPLOYEE')
        self.assertFalse(self.local.is_staff)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.manager.pk)
        self.assertEqual(employee_client.get(reverse('my_profile')).status_code, 302)
        self.assertFalse(Client().login(username='local', password='Account-test-pass!41'))
        self.assertTrue(Client().login(username='local', password='Recovered-staff-pass!82'))
        entry = LogEntry.objects.get()
        self.assertEqual(entry.user, self.manager)
        self.assertEqual(entry.object_id, str(self.local.pk))
        self.assertEqual(entry.action_flag, CHANGE)
        self.assertNotIn('pass!', entry.change_message)

    def test_manager_password_reset_follows_employee_cover_not_home_membership(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('manager_staff'))
        by_id = {p.pk: p for p in response.context['staff']}
        self.assertTrue(by_id[self.local.pk].can_reset_password)
        self.assertTrue(by_id[self.visitor.pk].can_reset_password)
        self.assertFalse(by_id[self.away.pk].can_reset_password)
        self.assertFalse(by_id[self.leave.pk].can_reset_password)
        self.assertEqual(self.client.get(self.reset_url(self.visitor)).status_code, 200)
        for target in [self.away, self.leave, self.foreign]:
            self.assertEqual(self.client.get(self.reset_url(target)).status_code, 404)
            self.assertEqual(self.client.post(self.reset_url(target), self.reset_data()).status_code, 404)
            target.refresh_from_db()
            self.assertTrue(target.check_password('Account-test-pass!41'))
        self.assertFalse(LogEntry.objects.exists())

    def test_general_manager_can_reset_employee_on_any_branch_or_leave_not_managers(self):
        self.client.force_login(self.gm)
        for target in [self.foreign, self.leave]:
            response = self.client.post(self.reset_url(target), self.reset_data())
            self.assertRedirects(response, reverse('general_manager_staff'))
            target.refresh_from_db()
            self.assertTrue(target.check_password('Recovered-staff-pass!82'))
        for target in [self.manager, self.gm, self.person('admin', 'ADMIN', self.a)]:
            self.assertEqual(self.client.post(self.reset_url(target), self.reset_data()).status_code, 404)
        self.assertEqual(LogEntry.objects.count(), 2)

    def test_reset_rechecks_duty_at_submission_and_manager_leave_blocks_recovery(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.reset_url(self.local)).status_code, 200)
        duty = self.duty(self.manager, self.b)
        self.assertEqual(self.client.post(self.reset_url(self.local), self.reset_data()).status_code, 404)
        self.assertEqual(self.client.get(self.reset_url(self.foreign)).status_code, 200)
        duty.status, duty.branch = 'LEAVE', None
        duty.save()
        self.assertEqual(self.client.post(self.reset_url(self.foreign), self.reset_data()).status_code, 403)
        self.assertFalse(LogEntry.objects.exists())

    def test_reset_password_validation_reauthentication_and_csrf(self):
        self.client.force_login(self.manager)
        for data in [self.reset_data(actor_password='wrong'),
                     self.reset_data(new_password2='different'),
                     self.reset_data(new_password1='short', new_password2='short'),
                     self.reset_data(new_password1='1234567890', new_password2='1234567890'),
                     self.reset_data(new_password1='Account-test-pass!41', new_password2='Account-test-pass!41')]:
            response = self.client.post(self.reset_url(self.local), data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
            self.assertNotContains(response, 'value="Account-test-pass!41"')
            self.local.refresh_from_db()
            self.assertTrue(self.local.check_password('Account-test-pass!41'))
        self.assertFalse(LogEntry.objects.exists())
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.manager)
        self.assertEqual(protected.post(self.reset_url(self.local), self.reset_data()).status_code, 403)

    def test_reset_denies_inactive_and_privileged_targets_and_non_manager_actors(self):
        self.client.force_login(self.gm)
        self.foreign.is_staff = True
        self.foreign.save()
        self.local.profile.active = False
        self.local.profile.save()
        self.away.is_active = False
        self.away.save()
        superuser = User.objects.create_superuser('root_account', '', 'Root-test-pass!41')
        for target in [self.foreign, self.local, self.away, superuser]:
            self.assertEqual(self.client.post(self.reset_url(target), self.reset_data()).status_code, 404)
        self.gm.profile.active = False
        self.gm.profile.save()
        self.assertEqual(self.client.get(reverse('general_manager_staff')).status_code, 403)
        self.assertEqual(self.client.post(self.reset_url(self.visitor), self.reset_data()).status_code, 403)
        self.client.force_login(self.visitor)
        self.assertEqual(self.client.post(self.reset_url(self.leave), self.reset_data()).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(self.reset_url(self.leave), self.reset_data()).status_code, 302)
        self.assertFalse(LogEntry.objects.exists())

    def test_cover_staff_reset_preserves_roster_assignments_and_completed_snapshots(self):
        visit = Visit.objects.create(
            branch=self.a, customer=Customer.objects.create(name='Recovery test customer'),
            created_by=self.manager,
        )
        assigned = VisitService.objects.create(
            visit=visit, service=Service.objects.create(code='RECOVERY', name='Recovery test service'),
            employee=self.visitor, assigned_by=self.manager,
        )
        VisitTask.objects.create(
            visit_service=assigned, sequence=1, title='Completed snapshot',
            phase='DURING', task_type='SERVICE', status='COMPLETED',
            performed_by=self.visitor, completed_at=timezone.now(),
        )
        before = {
            'duties': list(BranchDuty.objects.values()),
            'visits': list(Visit.objects.values()),
            'services': list(VisitService.objects.values()),
            'tasks': list(VisitTask.objects.values()),
        }
        self.client.force_login(self.manager)
        response = self.client.post(self.reset_url(self.visitor), self.reset_data())
        self.assertRedirects(response, reverse('manager_staff'))
        self.visitor.refresh_from_db()
        self.assertTrue(self.visitor.check_password('Recovered-staff-pass!82'))
        self.assertEqual(before, {
            'duties': list(BranchDuty.objects.values()),
            'visits': list(Visit.objects.values()),
            'services': list(VisitService.objects.values()),
            'tasks': list(VisitTask.objects.values()),
        })

    def test_failed_audit_rolls_back_password_reset(self):
        self.client.force_login(self.manager)
        with patch('operations.views.LogEntry.objects.create', side_effect=RuntimeError('Audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.reset_url(self.local), self.reset_data())
        self.local.refresh_from_db()
        self.assertTrue(self.local.check_password('Account-test-pass!41'))
        self.assertFalse(LogEntry.objects.exists())
