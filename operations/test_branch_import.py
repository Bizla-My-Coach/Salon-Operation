import csv
import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import StopUpload
from django.db import IntegrityError, transaction
from django.forms import modelform_factory
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import branch_imports
from .branch_imports import MAX_BRANCH_ROWS, MAX_BRANCH_UPLOAD, BranchImportProblem, apply_branch_import, parse_branch_csv, stage_branch_import
from .models import Branch, BranchCSVImport, BranchDuty, Chair, Customer, Profile, Service, Visit, VisitService, VisitTask
from .upload_limits import BranchCSVUploadLimit


def branch_csv(rows=None, headers=None, delimiter=','):
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=delimiter)
    writer.writerow(headers or ['BRANCH_CODE', 'BRANCH_NAME', 'LOCATION', 'PHONE', 'CHAIR_CODE', 'CHAIR_NAME'])
    writer.writerows(rows if rows is not None else [
        ['NEW-A', 'New example A', 'Example location A', '', 'C1', 'First chair'],
        ['NEW-A', 'New example A', 'Example location A', '', 'C2', 'Second chair'],
        ['NEW-B', 'New example B', '', '', 'C1', 'First chair'],
    ])
    return ('\ufeff' + output.getvalue()).encode('utf-8')


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}},
)
class BranchImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(code='TEST-A', name='Existing example', address='Existing location', phone='9000000001')
        cls.chair = Chair.objects.create(branch=cls.branch, code='C1', name='Existing chair')
        for attr, role in [('admin', 'ADMIN'), ('other_admin', 'ADMIN'), ('gm', 'GENERAL_MANAGER'), ('manager', 'MANAGER'), ('employee', 'EMPLOYEE')]:
            actor = User.objects.create_user('branch_' + attr, password='Operator-test-pass!52')
            actor.profile.role, actor.profile.branch = role, cls.branch
            actor.profile.save()
            setattr(cls, attr, actor)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('branch_csv_import')

    def upload(self, payload=None, filename='branches.csv'):
        return self.client.post(self.url, {'action': 'validate', 'csv_file': SimpleUploadedFile(filename, payload or branch_csv())})

    def stage(self, payload=None):
        return stage_branch_import(self.admin, 'branches.csv', payload or branch_csv())

    def confirm(self, stage, **changes):
        values = {'action': 'confirm', 'stage': str(stage.pk), 'confirmed': 'on', 'actor_password': 'Operator-test-pass!52'}
        values.update(changes)
        return self.client.post(self.url, values)

    def test_hub_groups_exactly_three_import_workflows_and_subpages_link_back(self):
        response = self.client.get(reverse('import_data'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="panel import-card"', count=3)
        for target in ['branch_csv_import', 'staff_csv_import', 'workbook_import']:
            self.assertContains(response, f'href="{reverse(target)}"')
            self.assertContains(self.client.get(reverse(target)), 'Back to import data')
        sidebar = response.content.decode().split('<aside', 1)[1].split('</aside>', 1)[0]
        self.assertIn(f'href="{reverse("import_data")}"', sidebar)
        self.assertNotIn(f'href="{reverse("workbook_import")}"', sidebar)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.post(reverse('import_data')).status_code, 405)

    def test_general_manager_retains_staff_only_master_imports_are_admin_only(self):
        self.client.force_login(self.gm)
        response = self.client.get(reverse('import_data'))
        self.assertContains(response, 'Administrator access required', count=2)
        self.assertContains(response, f'href="{reverse("staff_csv_import")}"')
        self.assertNotContains(response, f'href="{self.url}"')
        self.assertEqual(self.client.get(reverse('staff_csv_import')).status_code, 200)
        for actor in [self.gm, self.manager, self.employee]:
            self.client.force_login(actor)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.upload().status_code, 403)
            self.assertEqual(self.client.get(reverse('branch_csv_template')).status_code, 403)
            self.assertEqual(self.client.get(reverse('workbook_import')).status_code, 403)
        for actor in [self.manager, self.employee]:
            self.client.force_login(actor)
            self.assertEqual(self.client.get(reverse('import_data')).status_code, 403)
        self.admin.profile.active = False
        self.admin.profile.save()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.get(reverse('import_data')).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_superuser_can_import_without_an_admin_role(self):
        superuser = User.objects.create_superuser('branch_superuser', '', 'Test-only-password!')
        self.client.force_login(superuser)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertContains(self.client.get(reverse('import_data')), f'href="{self.url}"')

    def test_legacy_bookmark_redirects_and_direct_posts_cannot_bypass_confirmation(self):
        self.assertRedirects(self.client.get(reverse('csv_import')), reverse('import_data'))
        response = self.client.post(reverse('csv_import'), {'kind': 'branches', 'csv_file': SimpleUploadedFile('direct.csv', branch_csv())})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Branch.objects.count(), 1)
        self.assertFalse(BranchCSVImport.objects.exists())

    def test_validation_previews_two_branches_and_three_chairs_without_saving_master_data(self):
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Branch.objects.count(), 1)
        self.assertEqual(Chair.objects.count(), 1)
        stage = BranchCSVImport.objects.get()
        self.assertEqual(len(stage.plan['branches']), 2)
        self.assertEqual(len(stage.plan['chairs']), 3)
        self.assertEqual(stage.summary['branches']['create'], 2)
        self.assertEqual(stage.summary['chairs']['create'], 3)
        response = self.client.get(response.url)
        self.assertContains(response, 'Example location A')
        self.assertContains(response, 'Your current password')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertFalse(LogEntry.objects.exists())

    def test_confirmation_creates_active_records_and_audit_and_is_replay_safe(self):
        stage = self.stage()
        self.assertEqual(self.confirm(stage).status_code, 302)
        a, b = Branch.objects.get(code='NEW-A'), Branch.objects.get(code='NEW-B')
        self.assertEqual(a.address, 'Example location A')
        self.assertEqual(a.chairs.count(), 2)
        self.assertEqual(b.chairs.get().code, 'C1')
        self.assertTrue(a.active)
        self.assertTrue(a.chairs.get(code='C1').active)
        self.assertEqual(LogEntry.objects.count(), 5)
        stage.refresh_from_db()
        self.assertEqual(stage.status, 'APPLIED')
        self.assertEqual(stage.plan, {})
        self.assertIsNotNone(stage.applied_at)
        self.assertContains(self.confirm(stage), 'already used')
        self.assertEqual(LogEntry.objects.count(), 5)
        response = self.client.post(self.url, {'action': 'cancel', 'stage': stage.pk}, follow=True)
        self.assertContains(response, 'no longer pending')

    def test_code_matching_updates_in_place_preserves_blanks_and_missing_chairs(self):
        payload = branch_csv([['test-a', 'Renamed example', '', 'null', 'c1', 'Renamed chair']])
        stage = self.stage(payload)
        self.assertEqual(stage.plan['branches'][0]['changed_fields'], ['name'])
        apply_branch_import(stage.pk, self.admin)
        self.branch.refresh_from_db()
        self.chair.refresh_from_db()
        self.assertEqual(self.branch.name, 'Renamed example')
        self.assertEqual(self.branch.address, 'Existing location')
        self.assertEqual(self.branch.phone, '9000000001')
        self.assertEqual(self.chair.name, 'Renamed chair')
        self.assertEqual(Branch.objects.count(), 1)
        self.assertEqual(Chair.objects.count(), 1)
        payload = branch_csv([['TEST-A', 'Renamed example', 'Updated location', '+91 9000000001', '', '']])
        apply_branch_import(self.stage(payload).pk, self.admin)
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.address, 'Updated location')
        self.assertEqual(self.branch.phone, '+919000000001')
        self.assertEqual(Chair.objects.count(), 1)

    def test_same_data_reimport_is_idempotent_without_new_audit_entries(self):
        apply_branch_import(self.stage().pk, self.admin)
        stage = self.stage()
        self.assertEqual(stage.summary['branches']['unchanged'], 2)
        self.assertEqual(stage.summary['chairs']['unchanged'], 3)
        apply_branch_import(stage.pk, self.admin)
        self.assertEqual(Branch.objects.count(), 3)
        self.assertEqual(Chair.objects.count(), 4)
        self.assertEqual(LogEntry.objects.count(), 5)

    def test_confirm_requires_password_checkbox_and_csrf(self):
        stage = self.stage()
        for changes in [{'confirmed': ''}, {'actor_password': 'wrong'}]:
            response = self.confirm(stage, **changes)
            self.assertTrue(response.context['confirm_form'].errors)
            self.assertEqual(Branch.objects.count(), 1)
            self.assertNotContains(response, 'value="Operator-test-pass!52"')
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.admin)
        self.assertEqual(protected.post(self.url, {'action': 'confirm', 'stage': stage.pk}).status_code, 403)

    def test_stage_preview_confirmation_and_cancel_are_owner_bound(self):
        stage = self.stage()
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.get(self.url, {'stage': stage.pk}).status_code, 404)
        self.assertEqual(self.confirm(stage).status_code, 404)
        self.assertEqual(self.client.post(self.url, {'action': 'cancel', 'stage': stage.pk}).status_code, 404)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url, {'stage': 'bad-uuid'}).status_code, 400)

    def test_expired_cancelled_and_superseded_previews_clear_their_plan(self):
        stage = self.stage()
        stage.expires_at = timezone.now() - timedelta(seconds=1)
        stage.save()
        self.assertContains(self.confirm(stage), 'expired')
        stage.refresh_from_db()
        self.assertEqual((stage.status, stage.plan), ('EXPIRED', {}))
        first = self.stage()
        second = self.stage()
        first.refresh_from_db()
        self.assertEqual((first.status, first.plan), ('CANCELLED', {}))
        self.client.post(self.url, {'action': 'cancel', 'stage': second.pk})
        second.refresh_from_db()
        self.assertEqual((second.status, second.plan), ('CANCELLED', {}))
        self.assertEqual(Branch.objects.count(), 1)

    def test_branch_and_chair_changes_after_preview_reject_stale_plans(self):
        for model, pk in [(Branch, self.branch.pk), (Chair, self.chair.pk)]:
            stage = self.stage()
            model.objects.filter(pk=pk).update(name='Changed elsewhere')
            with self.assertRaises(BranchImportProblem):
                apply_branch_import(stage.pk, self.admin)
            self.assertEqual(Branch.objects.count(), 1)
            self.assertEqual(Chair.objects.count(), 1)
        stage = self.stage()
        Branch.objects.create(code='OTHER', name='Added elsewhere')
        self.assertContains(self.confirm(stage), 'changed after preview')
        self.assertFalse(Branch.objects.filter(code='NEW-A').exists())

    def test_audit_failure_rolls_back_updates_creates_and_all_audit_records(self):
        payload = branch_csv([
            ['TEST-A', 'Updated example', 'New location', '', 'C1', 'Updated chair'],
            ['NEW-A', 'New example A', '', '', 'C1', 'New chair'],
        ])
        stage = self.stage(payload)
        original_audit = branch_imports.audit
        def fail_on_chair(actor, obj, action):
            if isinstance(obj, Chair):
                raise RuntimeError('Simulated audit failure')
            return original_audit(actor, obj, action)
        with patch('operations.branch_imports.audit', side_effect=fail_on_chair):
            self.assertContains(self.confirm(stage), 'rolled back')
        self.branch.refresh_from_db()
        self.chair.refresh_from_db()
        self.assertEqual(self.branch.name, 'Existing example')
        self.assertEqual(self.chair.name, 'Existing chair')
        self.assertEqual(Branch.objects.count(), 1)
        self.assertEqual(Chair.objects.count(), 1)
        self.assertFalse(LogEntry.objects.exists())
        stage.refresh_from_db()
        self.assertEqual(stage.status, 'PENDING')

    def test_invalid_rows_report_multiple_errors_without_partial_import(self):
        payload = branch_csv([
            ['bad code', 'Bad', '', '', '', ''],
            ['GOOD', 'Good', '', 'bad phone', '', ''],
            ['PARTIAL', 'Partial', '', '', 'C1', ''],
        ])
        response = self.upload(payload)
        for text in ['Row 2', 'Row 3', 'Row 4']:
            self.assertContains(response, text)
        self.assertFalse(BranchCSVImport.objects.exists())
        self.assertEqual(Branch.objects.count(), 1)

    def test_conflicting_branch_details_duplicate_chairs_and_new_name_duplicates_rejected(self):
        for rows in [
            [['NEW', 'New', 'One', '', 'C1', 'One'], ['new', 'New', 'Two', '', 'C2', 'Two']],
            [['NEW', 'New', '', '', 'C1', 'One'], ['NEW', 'New', '', '', 'c1', 'Two']],
            [['NEW', 'Existing example', '', '', '', '']],
            [['NEW', 'New same name', '', '', '', ''], ['OTHER', 'new same name', '', '', '', '']],
        ]:
            with self.subTest(rows=rows), self.assertRaises(BranchImportProblem):
                parse_branch_csv(branch_csv(rows))

    def test_inactive_and_case_ambiguous_master_records_are_rejected(self):
        payload = branch_csv([['TEST-A', 'Existing example', '', '', 'C1', 'Existing chair']])
        self.chair.active = False
        self.chair.save()
        with self.assertRaises(BranchImportProblem):
            parse_branch_csv(payload)
        self.chair.active = True
        self.chair.save()
        self.branch.active = False
        self.branch.save()
        with self.assertRaises(BranchImportProblem):
            parse_branch_csv(payload)
        self.branch.active = True
        self.branch.save()
        Branch.objects.create(code='test-a', name='Ambiguous code')
        with self.assertRaises(BranchImportProblem):
            parse_branch_csv(payload)

    def test_bad_csv_headers_encoding_limits_and_values_are_rejected(self):
        bad = [
            b'', b'\xff\xfe', b'BRANCH_CODE,BRANCH_NAME\nABC,"unterminated', b'\x00',
            branch_csv(headers=['code', 'name']), branch_csv(headers=['BRANCH_CODE', 'BRANCH_NAME', 'LOCATION', 'PHONE', 'ACTIVE', 'CHAIR_NAME']),
            branch_csv(headers=['BRANCH_CODE', 'BRANCH_NAME', 'LOCATION', 'ADDRESS', 'CHAIR_CODE', 'CHAIR_NAME']),
            branch_csv([['A', '', '', '', '', '']]), branch_csv([['A', 'X' * 121, '', '', '', '']]),
            branch_csv([['A', 'Example', 'X' * 1001, '', '', '']]), branch_csv([['A', 'Example', '', '', 'X' * 21, 'Chair']]),
            branch_csv([['A', 'Example', '', '', 'C1', 'X' * 81]]), b'X' * (MAX_BRANCH_UPLOAD + 1),
            branch_csv([[f'A{i}', f'Example {i}', '', '', '', ''] for i in range(MAX_BRANCH_ROWS + 1)]),
        ]
        for payload in bad:
            with self.subTest(length=len(payload)), self.assertRaises(BranchImportProblem):
                parse_branch_csv(payload)
        self.assertTrue(self.upload(filename='branches.xlsx').context['form'].errors)

    def test_semicolon_utf8_aliases_and_branch_only_rows_are_supported(self):
        plan = parse_branch_csv(branch_csv([['NEW', 'ഉദാഹരണ ശാഖ', 'Example, location', '', '', '']], delimiter=';'))
        self.assertEqual(plan['branches'][0]['name'], 'ഉദാഹരണ ശാഖ')
        self.assertEqual(plan['chairs'], [])
        plan = parse_branch_csv(b'BRANCH_CODE,NAME,ADDRESS\nNEW,Example,Location\n')
        self.assertEqual(plan['branches'][0]['address'], 'Location')
        stage = self.stage(branch_csv([['NEW', 'Example', '', '', '', '']]))
        apply_branch_import(stage.pk, self.admin)
        self.assertEqual(Branch.objects.get(code='NEW').chairs.count(), 0)

    def test_store_codes_are_previewed_and_imported_without_changing_branch_identity(self):
        payload = branch_csv([
            ['TEST-A', 'Existing example', '', '', 'C1', 'Existing chair', 'HS-001'],
            ['NEW', 'New store', '', '', '', '', 'HS-002'],
        ], headers=['BRANCH_CODE', 'BRANCH_NAME', 'LOCATION', 'PHONE', 'CHAIR_CODE', 'CHAIR_NAME', 'STORE_CODE'])
        stage = self.stage(payload)
        self.assertEqual(stage.plan['branches'][0]['changed_fields'], ['store_code'])
        self.assertContains(self.client.get(self.url, {'stage': stage.pk}), 'HS-001')
        self.branch.refresh_from_db()
        self.assertIsNone(self.branch.store_code)
        apply_branch_import(stage.pk, self.admin)
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.store_code, 'HS-001')
        self.assertEqual(self.branch.code, 'TEST-A')
        self.assertEqual(Branch.objects.get(code='NEW').store_code, 'HS-002')
        repeat = self.stage(payload)
        self.assertEqual(repeat.summary['branches']['unchanged'], 2)
        apply_branch_import(repeat.pk, self.admin)

    def test_blank_or_omitted_csv_store_codes_preserve_existing_value(self):
        self.branch.store_code = 'HS-001'
        self.branch.save()
        for payload in [
            branch_csv([['TEST-A', 'Existing example', '', '', '', '']]),
            b'BRANCH_CODE,BRANCH_NAME,STORE_CODE\nTEST-A,Existing example,\n',
            b'BRANCH_CODE,BRANCH_NAME,STORE_CODE\nTEST-A,Existing example,null\n',
        ]:
            with self.subTest(payload=payload):
                apply_branch_import(self.stage(payload).pk, self.admin)
                self.branch.refresh_from_db()
                self.assertEqual(self.branch.store_code, 'HS-001')

    def test_duplicate_invalid_and_conflicting_csv_store_codes_reject_entire_upload(self):
        self.branch.store_code = 'HS-001'
        self.branch.save()
        for rows in [
            [['NEW', 'New', 'HS-001']],
            [['NEW', 'New', 'HS-002'], ['OTHER', 'Other', 'HS-002']],
            [['NEW', 'New', 'HS-002'], ['NEW', 'New', 'HS-003']],
            [['NEW', 'New', 'X' * 121]],
            [['NEW', 'New', 'HS-\x1f001']],
        ]:
            with self.subTest(rows=rows), self.assertRaises(BranchImportProblem):
                parse_branch_csv(branch_csv(rows, headers=['BRANCH_CODE', 'BRANCH_NAME', 'STORE_CODE']))
        self.assertEqual(Branch.objects.count(), 1)

    def test_store_code_change_after_preview_rejects_stale_plan(self):
        stage = self.stage()
        Branch.objects.filter(pk=self.branch.pk).update(store_code='HS-001')
        with self.assertRaisesMessage(BranchImportProblem, 'changed after preview'):
            apply_branch_import(stage.pk, self.admin)
        self.assertFalse(Branch.objects.filter(code='NEW-A').exists())

    def test_store_codes_are_unique_in_database_and_optional_in_branch_admin_form(self):
        self.branch.store_code = 'HS-001'
        self.branch.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Branch.objects.create(code='DUPLICATE', name='Duplicate store', store_code='HS-001')
        duplicate = Branch(code='DUPLICATE', name='Duplicate store', store_code='HS-001')
        with self.assertRaises(ValidationError):
            duplicate.full_clean()
        form_class = modelform_factory(Branch, fields=['code', 'name', 'store_code'])
        for code in ['BLANK-A', 'BLANK-B']:
            form = form_class(data={'code': code, 'name': code, 'store_code': ''})
            self.assertTrue(form.is_valid(), form.errors)
            self.assertIsNone(form.save().store_code)

    def test_template_is_synthetic_and_preview_escapes_untrusted_values(self):
        response = self.client.get(reverse('branch_csv_template'))
        self.assertIn('no-store', response['Cache-Control'])
        rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['BRANCH_CODE'], rows[1]['BRANCH_CODE'])
        self.assertNotContains(response, 'Existing example')
        stage = self.stage(branch_csv([['NEW', '<script>alert(1)</script>', '', '', '', '']]))
        response = self.client.get(self.url, {'stage': stage.pk})
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')

    def test_upload_limits_are_enforced_before_parsing(self):
        response = self.client.generic('POST', self.url, b'X', content_type='multipart/form-data; boundary=x', CONTENT_LENGTH=str(MAX_BRANCH_UPLOAD + 1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
        with self.assertRaises(StopUpload):
            BranchCSVUploadLimit().receive_data_chunk(b'X', MAX_BRANCH_UPLOAD)

    def test_import_preserves_staff_rosters_services_visits_and_task_snapshots(self):
        duty = BranchDuty.objects.create(user=self.employee, branch=self.branch, status='WORK', date=timezone.localdate(), updated_by=self.admin)
        visit = Visit.objects.create(branch=self.branch, customer=Customer.objects.create(name='Synthetic example'), created_by=self.manager)
        service = VisitService.objects.create(visit=visit, service=Service.objects.create(code='EXAMPLE', name='Example service'), employee=self.employee, chair=self.chair, assigned_by=self.manager)
        VisitTask.objects.create(visit_service=service, title='Recorded task snapshot', sequence=1, phase='DURING', task_type='SERVICE', status='COMPLETED', completed_at=timezone.now())
        models = [Profile, BranchDuty, Service, Visit, VisitService, VisitTask]
        before = [list(model.objects.order_by('pk').values()) for model in models]
        apply_branch_import(self.stage(branch_csv([['TEST-A', 'Updated label', 'Updated location', '', 'C1', 'Updated chair label']])).pk, self.admin)
        self.assertEqual(before, [list(model.objects.order_by('pk').values()) for model in models])
        service.refresh_from_db()
        duty.refresh_from_db()
        self.assertEqual(service.chair_id, self.chair.pk)
        self.assertEqual(duty.branch_id, self.branch.pk)
