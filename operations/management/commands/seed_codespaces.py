"""Idempotent, fictional data for the dedicated Codespaces database only."""
import os

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from operations.models import Branch, Chair, FeedbackQuestion, OperationalTask, Service, SOPTask, SubService
from operations.review_workflow import record_review


def validate_environment(config, environ):
    db = config.DATABASES['default']
    if (environ.get('SALONOPS_CODESPACES') != 'true' or config.APP_ENV != 'development'
            or not config.DEBUG or db['ENGINE'] != 'django.db.backends.postgresql'
            or db['NAME'] != 'salonops_codespaces' or db['HOST'] != 'codespaces-db'):
        raise CommandError('This command is only available in the isolated SalonOps Codespaces development database.')


class Command(BaseCommand):
    help = 'Create admin/admin and fictional sample data in the dedicated Codespaces database.'

    def add_arguments(self, parser):
        parser.add_argument('--check-environment', action='store_true', help='Validate the target without modifying data.')

    @transaction.atomic
    def handle(self, *args, **options):
        validate_environment(settings, os.environ)
        if options['check_environment']:
            self.stdout.write('Isolated Codespaces environment confirmed.')
            return
        branch, _ = Branch.objects.get_or_create(code='CS-DEMO', defaults={'name': 'Codespaces Demo Salon'})
        other, _ = Branch.objects.get_or_create(code='CS-OTHER', defaults={'name': 'Codespaces Second Branch'})
        Chair.objects.get_or_create(branch=branch, code='CS01', defaults={'name': 'Demo chair'})
        people = {}
        for username, role, name in [('admin', 'ADMIN', 'Test Administrator'), ('manager', 'MANAGER', 'Test Manager'),
                                     ('stylist', 'EMPLOYEE', 'Test Stylist'), ('gm', 'GENERAL_MANAGER', 'Test General Manager')]:
            person, created = User.objects.get_or_create(username=username, defaults={'first_name': name})
            if created:
                person.set_password('admin')
                person.is_staff = person.is_superuser = role == 'ADMIN'
                person.save()
                person.profile.role = role
                person.profile.branch = None if role == 'GENERAL_MANAGER' else branch
                person.profile.job_title = name
                person.profile.save()
            elif username == 'admin' and not (person.is_superuser and person.is_staff and person.profile.role == 'ADMIN'):
                raise CommandError('An unrelated admin account already exists; no test data was changed.')
            people[username] = person
        service, _ = Service.objects.get_or_create(code='CS-HAIRCUT', defaults={
            'name': 'Demo Haircut & Styling', 'category': 'Hair', 'standard_duration_minutes': 45, 'base_price': 500})
        for code, name, action in [
            ('SUB025', 'Sanitisation', 'Sanitize tools and prepare the workstation'),
            ('SUB001', 'Consultation', 'Consult the customer and agree the requested style'),
        ]:
            opening, created = SubService.objects.get_or_create(code=code, defaults={'name': name})
            if created:
                OperationalTask.objects.create(external_id='CS-OPEN-' + code, sub_service=opening,
                    sequence=10, name=action, active_labour_minutes=2)
        for seq, phase, typ, title in [
            (10, 'BEFORE', 'CONSULT', 'Confirm the requested style'),
            (20, 'BEFORE', 'HYGIENE', 'Check tools are sanitized'),
            (30, 'DURING', 'SERVICE', 'Complete the haircut and styling'),
            (40, 'FINISHING', 'QUALITY', 'Check the finished result'),
            (50, 'AFTER', 'HYGIENE', 'Clean the tools and workstation'),
        ]:
            SOPTask.objects.get_or_create(service=service, sequence=seq, defaults={
                'phase': phase, 'task_type': typ, 'title': title, 'required': True, 'can_skip': False})
        for number, text in enumerate([
            'How satisfied are you with the result?', 'How professional was the team?',
            'How clean was the salon?', 'How well did we understand your needs?', 'Would you visit again?',
        ], 1):
            FeedbackQuestion.objects.get_or_create(sequence=number * 10, defaults={'text': text, 'active': True})
        for key, target, subject, text, issues in [
            ('waiting', branch, 'Demo: appointment wait and reception follow-up', 'The haircut was good, but I waited and needed an update.',
             [{'category': 'WAITING', 'reported_issue': 'Fictional customer reports a delayed appointment.', 'ai_priority': 'HIGH'},
              {'category': 'STAFF', 'reported_issue': 'Fictional customer asks for clearer reception updates.', 'ai_priority': 'NORMAL'}]),
            ('positive', branch, 'Demo: a great visit', 'Friendly team and a great haircut.', []),
            ('other', other, 'Demo: second branch follow-up', 'Please check the styling finish.',
             [{'category': 'SERVICE', 'reported_issue': 'Fictional customer asks for a finish check.', 'ai_priority': 'NORMAL'}]),
        ]:
            record_review(branch=target, actor=people['admin'], subject=subject, customer_label='Fictional Codespaces customer',
                text=text, rating=5 if not issues else 3, service_description='Fictional haircut appointment.',
                issues=issues, source_key='codespaces-demo-' + key)
        self.stdout.write(self.style.SUCCESS('Codespaces test data ready. Admin: admin / admin. Other test users: manager, stylist, gm (password: admin).'))
