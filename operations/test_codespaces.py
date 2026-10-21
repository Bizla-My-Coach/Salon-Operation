from copy import deepcopy
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, TestCase, override_settings

from .management.commands.seed_codespaces import validate_environment
from .models import Branch, Customer, ReviewCase, Service, SOPTask, Visit, VisitService, VisitTask
from .workflow import build_visit_tasks


class CodespacesGuardTests(SimpleTestCase):
    def test_only_the_explicit_isolated_development_target_is_allowed(self):
        config = SimpleNamespace(APP_ENV='development', DEBUG=True, DATABASES={'default': {
            'ENGINE': 'django.db.backends.postgresql', 'NAME': 'salonops_codespaces', 'HOST': 'codespaces-db'}})
        validate_environment(config, {'SALONOPS_CODESPACES': 'true'})
        for attr, value in [('APP_ENV', 'production'), ('DEBUG', False), ('NAME', 'company_production'),
                            ('HOST', 'company-db'), ('ENGINE', 'django.db.backends.sqlite3')]:
            bad = deepcopy(config)
            if attr in ('APP_ENV', 'DEBUG'):
                setattr(bad, attr, value)
            else:
                bad.DATABASES['default'][attr] = value
            with self.subTest(attr=attr), self.assertRaises(CommandError):
                validate_environment(bad, {'SALONOPS_CODESPACES': 'true'})
        with self.assertRaises(CommandError):
            validate_environment(config, {})


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class CodespacesSeedTests(TestCase):
    def seed(self, **options):
        with patch('operations.management.commands.seed_codespaces.validate_environment'):
            call_command('seed_codespaces', stdout=StringIO(), **options)

    def test_admin_login_roles_sample_workflow_and_manager_notification(self):
        self.seed()
        admin = authenticate(username='admin', password='admin')
        self.assertTrue(admin.is_superuser and admin.is_staff)
        self.assertEqual(admin.profile.role, 'ADMIN')
        self.assertEqual(User.objects.get(username='gm').profile.role, 'GENERAL_MANAGER')
        manager = User.objects.get(username='manager')
        self.assertEqual(manager.profile.role, 'MANAGER')
        self.assertEqual(User.objects.get(username='stylist').profile.branch, manager.profile.branch)
        ticket = ReviewCase.objects.get(source_key='codespaces-demo-waiting')
        self.assertEqual(ticket.issues.count(), 2)
        self.assertEqual(ticket.owner, manager)
        self.assertEqual(ticket.notifications.get().recipient, manager)
        self.assertEqual(ReviewCase.objects.count(), 3)
        self.assertEqual(SOPTask.objects.count(), 5)
        visit = Visit.objects.create(branch=manager.profile.branch,
            customer=Customer.objects.create(name='Fictional workflow test'), created_by=manager)
        for order in (1, 2):
            VisitService.objects.create(visit=visit, service=Service.objects.get(code='CS-HAIRCUT'),
                order_number=order, employee=User.objects.get(username='stylist'), assigned_by=manager)
        build_visit_tasks(visit)
        types = list(VisitTask.objects.filter(visit_service__visit=visit).order_by(
            'visit_service__order_number', 'sequence').values_list('task_type', flat=True))
        self.assertEqual(types[:2], ['HYGIENE', 'CONSULT'])
        self.assertEqual(types.count('HYGIENE'), 1)
        self.assertEqual(types.count('CONSULT'), 1)

    def test_repeat_start_preserves_passwords_ticket_progress_and_master_data(self):
        self.seed()
        ids = set(ReviewCase.objects.values_list('pk', flat=True))
        admin = User.objects.get(username='admin')
        admin.set_password('Changed-test-password!')
        admin.save()
        branch = Branch.objects.get(code='CS-DEMO')
        branch.name = 'Edited test salon'
        branch.save()
        issue = ReviewCase.objects.get(source_key='codespaces-demo-waiting').issues.first()
        issue.status, issue.action_summary = 'IN_PROGRESS', 'Test follow-up already underway.'
        issue.save()
        self.seed()
        self.assertIsNotNone(authenticate(username='admin', password='Changed-test-password!'))
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'IN_PROGRESS')
        self.assertEqual(issue.action_summary, 'Test follow-up already underway.')
        self.assertEqual(Branch.objects.get(pk=branch.pk).name, 'Edited test salon')
        self.assertEqual(set(ReviewCase.objects.values_list('pk', flat=True)), ids)
        self.assertEqual(SOPTask.objects.count(), 5)

    @override_settings(ALLOWED_HOSTS=['fictional-8000.app.github.dev'],
        CSRF_TRUSTED_ORIGINS=['https://fictional-8000.app.github.dev'],
        SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO', 'https'),
        SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
    def test_admin_login_through_codespaces_https_forwarding_keeps_csrf_protection(self):
        self.seed()
        client = Client(enforce_csrf_checks=True)
        headers = {'HTTP_HOST': 'fictional-8000.app.github.dev', 'HTTP_X_FORWARDED_PROTO': 'https'}
        response = client.get('/login/', **headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.cookies['csrftoken']['secure'])
        response = client.post('/login/', {'username': 'admin', 'password': 'admin',
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value},
            HTTP_ORIGIN='https://fictional-8000.app.github.dev', **headers)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.cookies['sessionid']['secure'])
        self.assertEqual(client.get('/admin/', **headers).status_code, 200)

    def test_environment_check_creates_no_demo_data(self):
        self.seed(check_environment=True)
        self.assertFalse(User.objects.exists())
        self.assertFalse(Branch.objects.exists())

    def test_an_unrelated_admin_is_preserved_and_seed_changes_roll_back(self):
        user = User.objects.create_user('admin', password='Existing-password!')
        with self.assertRaises(CommandError):
            self.seed()
        self.assertFalse(Branch.objects.exists())
        user.refresh_from_db()
        self.assertTrue(user.check_password('Existing-password!'))
        self.assertFalse(user.is_superuser)
