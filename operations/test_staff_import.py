import csv
import io
import string
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import StopUpload
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Branch, BranchDuty, Customer, Profile, StaffCSVImport, Visit
from .admin import ProfileAdmin
from .staff_imports import (
    MAX_STAFF_ROWS, MAX_STAFF_UPLOAD, StaffImportProblem, apply_staff_import,
    credentials_csv, generate_staff_password, parse_staff_csv, stage_staff_import,
)
from .upload_limits import StaffCSVUploadLimit


class GeneratedStaffPasswordTests(SimpleTestCase):
    def test_passwords_are_eight_random_characters_with_all_character_types(self):
        passwords = {generate_staff_password() for _ in range(100)}
        self.assertEqual(len(passwords), 100)
        for password in passwords:
            self.assertEqual(len(password), 8)
            for group in (string.ascii_uppercase, string.ascii_lowercase, string.digits, '!#$%&*?'):
                self.assertTrue(any(char in group for char in password))


def staff_csv(rows=None, headers=None, delimiter=',', bom=True):
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=delimiter)
    writer.writerow(headers or ['BRANCH', 'NAME', 'PH.NO', 'SECTION', 'EXPERIENCE', 'SALARY'])
    writer.writerows(rows if rows is not None else [
        ['TEST-A', 'Alex Example', '9000000001', 'MANAGER/UNISEX HAIR STYLIST', '4 YEARS', '25000.50'],
        ['Example Branch B', 'Blair Example', 'null', 'LADIES', '6 YEARS', 'null'],
    ])
    return (('\ufeff' if bom else '') + output.getvalue()).encode('utf-8')


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}},
)
class StaffCSVImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = Branch.objects.create(code='TEST-A', name='Example Branch A')
        cls.b = Branch.objects.create(code='TEST-B', name='Example Branch B')
        for attr, role in [('admin', 'ADMIN'), ('other_admin', 'ADMIN'), ('gm', 'GENERAL_MANAGER'),
                           ('manager', 'MANAGER'), ('employee', 'EMPLOYEE')]:
            person = User.objects.create_user('csv_' + attr, password='Operator-test-pass!52')
            person.profile.role = role
            person.profile.branch = cls.a if role in ['MANAGER', 'EMPLOYEE'] else None
            person.profile.save()
            setattr(cls, attr, person)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('staff_csv_import')

    def upload(self, payload=None, filename='staff.csv'):
        return self.client.post(self.url, {'action': 'validate', 'csv_file': SimpleUploadedFile(
            filename, payload if payload is not None else staff_csv(), content_type='text/csv',
        )})

    def stage(self, payload=None):
        return stage_staff_import(self.admin, 'staff.csv', payload or staff_csv())

    def confirm(self, stage, **changes):
        values = dict(action='confirm', stage=str(stage.pk), confirmed='on', actor_password='Operator-test-pass!52')
        values.update(changes)
        return self.client.post(self.url, values)

    def test_validate_only_creates_private_preview_not_accounts(self):
        before = User.objects.count()
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        stage = StaffCSVImport.objects.get()
        self.assertEqual(User.objects.count(), before)
        self.assertEqual(stage.created_by, self.admin)
        self.assertEqual(stage.row_count, 2)
        self.assertEqual(stage.status, 'PENDING')
        self.assertNotIn('password', str(stage.rows).lower())
        response = self.client.get(response.url)
        self.assertContains(response, '25000.50')
        self.assertContains(response, 'MANAGER')
        self.assertContains(response, 'EMPLOYEE')
        self.assertContains(response, 'Your current password')
        self.assertContains(response, 'one-time')
        self.assertIn('no-store', response['Cache-Control'])

    def test_confirmation_creates_accounts_pay_details_and_private_credentials_once(self):
        stage = self.stage()
        response = self.confirm(stage)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Disposition'], 'attachment; filename="staff-login-credentials.csv"')
        self.assertIn('no-store', response['Cache-Control'])
        rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]['PASSWORD'], rows[1]['PASSWORD'])
        for row, expected_role, expected_branch in zip(rows, ['MANAGER', 'EMPLOYEE'], [self.a, self.b]):
            person = User.objects.get(username=row['USERNAME'])
            self.assertEqual(len(row['PASSWORD']), 8)
            self.assertTrue(person.check_password(row['PASSWORD']))
            self.assertNotEqual(person.password, row['PASSWORD'])
            self.assertEqual(person.profile.role, expected_role)
            self.assertEqual(person.profile.branch, expected_branch)
            self.assertFalse(person.is_staff)
            self.assertFalse(person.is_superuser)
            self.assertTrue(Client().login(username=person.username, password=row['PASSWORD']))
            self.assertTrue(person.profile.employee_code.startswith('STF-'))
            self.assertNotIn(row['PASSWORD'], str(StaffCSVImport.objects.get(pk=stage.pk).rows))
            self.assertNotIn(row['PASSWORD'], str(list(LogEntry.objects.values('change_message'))))
        manager = User.objects.get(username=rows[0]['USERNAME'])
        self.assertEqual(manager.profile.mobile, '9000000001')
        self.assertEqual(manager.profile.salary, Decimal('25000.50'))
        self.assertEqual(manager.profile.experience, '4 YEARS')
        employee = User.objects.get(username=rows[1]['USERNAME'])
        self.assertEqual(employee.profile.mobile, '')
        self.assertIsNone(employee.profile.salary)
        stage.refresh_from_db()
        self.assertEqual(stage.status, 'APPLIED')
        self.assertEqual(stage.rows, [])
        self.assertFalse(BranchDuty.objects.exists())
        self.assertEqual(LogEntry.objects.count(), 2)
        response = self.confirm(stage)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Content-Disposition', response)
        self.assertContains(response, 'already used')
        self.assertEqual(LogEntry.objects.count(), 2)
        response = self.client.post(self.url, {'action': 'cancel', 'stage': stage.pk}, follow=True)
        self.assertContains(response, 'no longer pending')
        self.assertNotContains(response, 'No accounts were created.')

    def test_confirm_requires_checkbox_and_operators_password(self):
        stage = self.stage()
        before = User.objects.count()
        for extra in [dict(confirmed=''), dict(actor_password='wrong')]:
            response = self.confirm(stage, **extra)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['confirm_form'].errors)
            self.assertEqual(User.objects.count(), before)
            self.assertNotContains(response, 'value="Operator-test-pass!52"')
        self.assertFalse(LogEntry.objects.exists())

    def test_admin_and_unrostered_general_manager_have_import_access_not_branch_staff(self):
        for actor in [self.admin, self.gm]:
            self.client.force_login(actor)
            self.assertEqual(self.client.get(self.url).status_code, 200)
            self.assertEqual(self.client.get(reverse('staff_csv_template')).status_code, 200)
            directory = 'admin_staff' if actor == self.admin else 'general_manager_staff'
            response = self.client.get(reverse(directory))
            self.assertContains(response, 'Upload staff CSV')
        for actor in [self.manager, self.employee]:
            self.client.force_login(actor)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.upload().status_code, 403)
            self.assertEqual(self.client.get(reverse('staff_csv_template')).status_code, 403)
        self.client.force_login(self.manager)
        self.assertNotContains(self.client.get(reverse('manager_staff')), 'Upload staff CSV')
        self.gm.profile.active = False
        self.gm.profile.save()
        self.client.force_login(self.gm)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_stage_is_owner_bound_and_csrf_protected(self):
        stage = self.stage()
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.get(self.url, {'stage': stage.pk}).status_code, 404)
        self.assertEqual(self.confirm(stage).status_code, 404)
        self.assertEqual(self.client.post(self.url, {'action': 'cancel', 'stage': stage.pk}).status_code, 404)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url, {'stage': 'bad-uuid'}).status_code, 400)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.admin)
        self.assertEqual(protected.post(self.url, {'action': 'confirm', 'stage': stage.pk}).status_code, 403)

    def test_salary_is_visible_only_to_admin_and_general_manager(self):
        self.employee.profile.salary = Decimal('17345.67')
        self.employee.profile.experience = '9 YEARS'
        self.employee.profile.save()
        for actor, page in [(self.admin, 'admin_staff'), (self.gm, 'general_manager_staff')]:
            self.client.force_login(actor)
            response = self.client.get(reverse(page))
            self.assertContains(response, '17345.67')
            self.assertContains(response, '<dt>Salary</dt>')
            self.assertIn('no-store', response['Cache-Control'])
        self.client.force_login(self.manager)
        response = self.client.get(reverse('manager_staff'))
        self.assertNotContains(response, '17345.67')
        self.assertNotContains(response, '<dt>Salary</dt>')
        self.assertContains(response, '9 YEARS')
        self.assertEqual(self.client.get(reverse('admin_staff')).status_code, 403)
        self.client.force_login(self.employee)
        self.assertNotContains(self.client.get(reverse('my_profile')), '17345.67')
        response = self.client.post(reverse('my_profile'), {
            'action': 'details', 'first_name': 'Updated', 'last_name': '', 'email': '', 'mobile': '',
            'salary': '99999', 'experience': '50 YEARS',
        })
        self.assertEqual(response.status_code, 302)
        self.employee.profile.refresh_from_db()
        self.assertEqual(self.employee.profile.salary, Decimal('17345.67'))
        self.assertEqual(self.employee.profile.experience, '9 YEARS')

    def test_expired_cancelled_and_superseded_previews_are_cleared(self):
        stage = self.stage()
        stage.expires_at = timezone.now() - timedelta(seconds=1)
        stage.save()
        self.assertContains(self.confirm(stage), 'expired')
        stage.refresh_from_db()
        self.assertEqual(stage.status, 'EXPIRED')
        self.assertEqual(stage.rows, [])
        stage = self.stage()
        self.client.post(self.url, {'action': 'cancel', 'stage': stage.pk})
        stage.refresh_from_db()
        self.assertEqual(stage.status, 'CANCELLED')
        self.assertEqual(stage.rows, [])
        first = self.stage()
        second = self.stage()
        first.refresh_from_db()
        self.assertEqual(first.rows, [])
        self.assertEqual(first.status, 'CANCELLED')
        self.assertEqual(second.status, 'PENDING')

    def test_django_admin_profile_form_hides_salary_from_other_roles(self):
        model_admin = ProfileAdmin(Profile, django_admin.site)
        request = RequestFactory().get('/admin/operations/profile/')
        for actor, allowed in [(self.admin, True), (self.gm, True), (self.manager, False), (self.employee, False)]:
            request.user = actor
            fields = model_admin.get_form(request, self.employee.profile).base_fields
            self.assertEqual('salary' in fields, allowed)
        self.gm.profile.active = False
        self.gm.profile.save()
        request.user = self.gm
        self.assertNotIn('salary', model_admin.get_form(request).base_fields)

    def test_changed_branch_and_new_username_collision_block_confirmation_atomically(self):
        stage = self.stage()
        self.a.name = 'Renamed branch'
        self.a.save()
        before = User.objects.count()
        with self.assertRaises(StaffImportProblem):
            apply_staff_import(stage.pk, self.admin)
        self.assertEqual(User.objects.count(), before)
        self.a.name = 'Example Branch A'
        self.a.save()
        stage = self.stage()
        User.objects.create_user(stage.rows[1]['username'])
        before = User.objects.count()
        with self.assertRaises(StaffImportProblem):
            apply_staff_import(stage.pk, self.admin)
        self.assertEqual(User.objects.count(), before)
        self.assertFalse(LogEntry.objects.exists())

    def test_database_failure_rolls_back_all_users_profiles_and_audit_entries(self):
        stage = self.stage()
        before = User.objects.count()
        with patch('operations.staff_imports.LogEntry.objects.create', side_effect=RuntimeError('Unavailable')):
            response = self.confirm(stage)
        self.assertContains(response, 'rolled back')
        self.assertEqual(User.objects.count(), before)
        self.assertFalse(LogEntry.objects.exists())
        stage.refresh_from_db()
        self.assertEqual(stage.status, 'PENDING')

    def test_repeat_upload_and_duplicate_names_never_overwrite_staff(self):
        stage = self.stage()
        credentials = apply_staff_import(stage.pk, self.admin)
        person = User.objects.get(username=credentials[0][2])
        password_hash = person.password
        person.first_name = 'Renamed afterwards'
        person.save()
        with self.assertRaises(StaffImportProblem):
            stage_staff_import(self.admin, 'again.csv', staff_csv())
        other_payload = staff_csv([['TEST-A', 'Renamed afterwards', '', 'LADIES', '', '100']])
        with self.assertRaises(StaffImportProblem):
            parse_staff_csv(other_payload)
        person.refresh_from_db()
        self.assertEqual(person.password, password_hash)
        self.assertEqual(person.profile.salary, Decimal('25000.50'))

    def test_invalid_rows_are_reported_together_and_create_nothing(self):
        payload = staff_csv([
            ['UNKNOWN', 'Bad branch', '', 'LADIES', '', ''],
            ['TEST-A', 'Bad phone', '9.1E+09', 'LADIES', '', ''],
            ['TEST-A', 'Bad salary', '', 'LADIES', '', '-100'],
        ])
        response = self.upload(payload)
        self.assertEqual(response.status_code, 200)
        for text in ['Row 2', 'Row 3', 'Row 4']:
            self.assertContains(response, text)
        self.assertFalse(StaffCSVImport.objects.exists())

    def test_invalid_headers_formats_limits_and_values(self):
        bad = [
            b'BRANCH,NAME\nTEST-A,Example\n',
            b'BRANCH,NAME,SECTION,ROLE\nTEST-A,Example,LADIES,ADMIN\n',
            b'BRANCH,NAME,SECTION,PH.NO,PHONE\nTEST-A,Example,LADIES,,\n',
            b'BRANCH,NAME,SECTION\nTEST-A,"unterminated,LADIES\n',
            b'\xff\xfe\x00', b'', b'X' * (MAX_STAFF_UPLOAD + 1),
            staff_csv([['TEST-A', 'X' * 151, '', 'LADIES', '', '']]),
            staff_csv([['TEST-A', 'Example', '', 'LADIES', 'X' * 81, '']]),
            staff_csv([['TEST-A', 'Example', '', 'LADIES', '', 'Infinity']]),
            staff_csv([['TEST-A', 'Example', '', 'LADIES', '', '1.001']]),
            staff_csv([['TEST-A', 'Example', '', 'LADIES', '', '10000000000']]),
            staff_csv([['TEST-A', f'Example {i}', '', 'LADIES', '', ''] for i in range(MAX_STAFF_ROWS + 1)]),
            staff_csv([['TEST-A', 'Same Name', '', 'LADIES', '', ''], ['test-a', 'same   name', '', 'LADIES', '', '']]),
        ]
        for payload in bad:
            with self.subTest(payload_length=len(payload)):
                with self.assertRaises(StaffImportProblem):
                    parse_staff_csv(payload)
        self.assertTrue(self.upload(filename='staff.xlsx').context['form'].errors)

    def test_null_optional_fields_semicolon_csv_and_numeric_phone_formats(self):
        rows = parse_staff_csv(staff_csv([
            [' example branch a ', 'Example One', '9000000001.0', 'MANAGER', 'null', '₹25,000'],
            ['TEST-B', 'Example Two', '+91 9000000002', 'HOUSE KEEPING', '', '0'],
        ], delimiter=';'))
        self.assertEqual(rows[0]['mobile'], '9000000001')
        self.assertEqual(rows[0]['experience'], '')
        self.assertEqual(rows[0]['salary'], '25000.00')
        self.assertEqual(rows[1]['mobile'], '+919000000002')
        self.assertEqual(rows[1]['role'], 'EMPLOYEE')
        self.assertEqual(rows[1]['salary'], '0.00')

    def test_inactive_and_ambiguous_branches_are_rejected(self):
        self.a.active = False
        self.a.save()
        with self.assertRaises(StaffImportProblem):
            parse_staff_csv(staff_csv())
        self.a.active = True
        self.a.save()
        Branch.objects.create(code='AMBIGUOUS', name=self.a.name)
        with self.assertRaises(StaffImportProblem):
            parse_staff_csv(staff_csv([[self.a.name, 'Example', '', 'LADIES', '', '']]))
        self.assertEqual(len(parse_staff_csv(staff_csv())), 2)  # A unique branch code still works.

    def test_generated_username_collision_is_previewed_not_existing_account_update(self):
        User.objects.create_user('test.a.alex.example')
        rows = parse_staff_csv(staff_csv())
        self.assertEqual(rows[0]['username'], 'test.a.alex.example.2')

    def test_credentials_export_guards_excel_formulas_and_template_is_synthetic(self):
        exported = list(csv.reader(io.StringIO(credentials_csv([['=1+1', '@name', 'safe.user', 'Hs!safe', 'EMPLOYEE']]))))
        self.assertEqual(exported[1][0], '\t=1+1')
        self.assertEqual(exported[1][1], '\t@name')
        for value in ['  +1', '-2', '＝1+1', '＠name', '\tvalue', '\rvalue', '\nvalue', '=1,2"3']:
            encoded = credentials_csv([[value]])
            self.assertEqual(list(csv.reader(io.StringIO(encoded)))[1][0], '\t' + value)
            self.assertIn('"\t', encoded)
        self.assertEqual(exported[1][2:4], ['safe.user', 'Hs!safe'])
        response = self.client.get(reverse('staff_csv_template'))
        text = response.content.decode('utf-8-sig')
        self.assertIn('BRANCH,NAME,PH.NO,SECTION,EXPERIENCE,SALARY', text)
        self.assertIn('Example Staff', text)
        self.assertNotIn(self.employee.username, text)

    def test_upload_limit_is_enforced_before_parsing(self):
        response = self.client.generic('POST', self.url, b'X', content_type='multipart/form-data; boundary=x',
                                       CONTENT_LENGTH=str(MAX_STAFF_UPLOAD + 1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
        handler = StaffCSVUploadLimit()
        with self.assertRaises(StopUpload):
            handler.receive_data_chunk(b'X', MAX_STAFF_UPLOAD)

    def test_database_disallows_negative_salary(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Profile.objects.filter(pk=self.employee.profile.pk).update(salary=-1)

    def test_import_preserves_existing_rosters_and_visit_records(self):
        BranchDuty.objects.create(user=self.manager, branch=self.b, status='WORK',
                                 date=timezone.localdate(), updated_by=self.gm)
        Visit.objects.create(branch=self.a, customer=Customer.objects.create(name='Synthetic customer'),
                             created_by=self.manager)
        before = list(BranchDuty.objects.values()), list(Visit.objects.values())
        apply_staff_import(self.stage().pk, self.admin)
        self.assertEqual(before, (list(BranchDuty.objects.values()), list(Visit.objects.values())))
