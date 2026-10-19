"""Create isolated fictional review cases for interactive verification."""
import sys
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from operations.models import Branch
from operations import review_workflow as workflow


class Command(BaseCommand):
    help = 'Seed fictional review tickets, only in an isolated *_review_preview development database.'

    @transaction.atomic
    def handle(self, *args, **options):
        if (not settings.DEBUG or settings.APP_ENV != 'development'
                or not settings.DATABASES['default']['NAME'].endswith('_review_preview')):
            raise CommandError('Use a separate *_review_preview development database. This command is disabled elsewhere.')
        password = sys.stdin.readline().rstrip('\r\n')
        if len(password) < 8:
            raise CommandError('Provide a demo-only password of at least 8 characters on stdin.')
        harbour, _ = Branch.objects.get_or_create(code='DEMO-RV-A', defaults={'name': 'Harbour · Demo'})
        riverside, _ = Branch.objects.get_or_create(code='DEMO-RV-B', defaults={'name': 'Riverside · Demo'})
        unstaffed, _ = Branch.objects.get_or_create(code='DEMO-RV-C', defaults={'name': 'Parkside · Demo'})
        people = {}
        for username, name, role, branch in [
            ('review.manager', 'Maya (demo manager)', 'MANAGER', harbour),
            ('review.admin', 'Alex (demo admin)', 'ADMIN', None),
            ('review.other', 'Sam (demo manager)', 'MANAGER', riverside),
        ]:
            person, created = User.objects.get_or_create(username=username, defaults={'first_name': name})
            if created:
                person.set_password(password)
                person.save(update_fields=['password'])
                person.profile.role, person.profile.branch = role, branch
                person.profile.save()
            people[username] = person
        manager = people['review.manager']

        def record(key, subject, text, rating, issues, branch=harbour, customer='Fictional customer'):
            return workflow.record_review(branch=branch, actor=None, source_key='review-demo-v1-' + key,
                subject=subject, customer_label=customer, text=text, rating=rating,
                service_description='Cut and styling appointment · fictional verification example.',
                issues=[dict(category=category, reported_issue=report, ai_priority=priority)
                        for category, report, priority in issues], synthetic=True,
                ai_requires_human_review=any(category in ['HEALTH', 'CONDUCT'] for category, _, _ in issues))

        def advance(case, status, action, outcome='', **extra):
            for issue in case.issues.all():
                if issue.status == 'NEW':
                    case = workflow.update_issue(manager, case.pk, issue.pk, case.version, {
                        'status': status, 'confirmed_priority': 'NORMAL', 'assignee': manager,
                        'action_summary': action, 'outcome': outcome, 'evidence_reference': '',
                        'customer_confirmation': 'UNKNOWN', 'waiting_reason': '', 'next_review_at': None, **extra,
                    }, synthetic=True)
            return case

        mixed = record('mixed', 'Great haircut, but the visit started late',
            'I loved the haircut, but waited a long time after my appointment time. The welcome at the desk felt rushed.', 5,
            [('WAITING', 'Customer reports a 40-minute wait after their appointment time.', 'HIGH'),
             ('STAFF', 'Customer reports a rushed welcome and unclear updates at reception.', 'NORMAL')], customer='Aarav · fictional')
        if mixed.reply.status == 'NONE':
            workflow.update_reply(manager, mixed.pk, mixed.version, 'submit_reply',
                'Thank you for your kind words about the haircut. We are sorry about the wait and the reception experience. Our branch manager will review both concerns with the team.', synthetic=True)
        active = record('active', 'Follow-up on styling finish', 'The styling finish was uneven after I got home.', 3,
                        [('SERVICE', 'Customer reports an uneven styling finish.', 'NORMAL')], customer='Nila · fictional')
        advance(active, 'IN_PROGRESS', 'Reviewed the service checklist with the stylist. Offering a follow-up appointment.')
        waiting = record('waiting', 'Payment clarification requested', 'I think the card payment appeared twice.', 2,
                         [('BILLING', 'Customer reports a possible duplicate charge.', 'HIGH')], customer='Dev · fictional')
        advance(waiting, 'WAITING', 'Checked the till summary; requested a payment reference through the approved contact channel.',
                waiting_reason='CUSTOMER', next_review_at=timezone.now() + timedelta(days=1))
        record('sensitive', 'Customer reports discomfort after colour service',
               'My scalp felt irritated after the colour appointment. I want someone to follow up.', 2,
               [('HEALTH', 'Customer reports scalp discomfort after a colour appointment. Human investigation is required.', 'URGENT')], customer='Customer · fictional')
        ready = record('ready', 'Reception and wait-time follow-up completed',
            'The visit was delayed and I did not receive an update.', 2,
            [('WAITING', 'Customer reports a delayed appointment.', 'HIGH'),
             ('STAFF', 'Customer reports missing progress updates.', 'NORMAL')], customer='Isha · fictional')
        advance(ready, 'RESOLVED', 'Reviewed appointment spacing and agreed an update procedure with reception.',
                'Appointment reminders and reception handover updated; corrective service offered.')
        closed = record('closed', 'Corrective styling completed', 'I would like the styling finish checked.', 3,
                        [('SERVICE', 'Customer requests a styling follow-up.', 'NORMAL')], customer='Riya · fictional')
        if closed.status != 'CLOSED':
            closed = advance(closed, 'RESOLVED', 'Completed the agreed corrective styling session.',
                             'The finish was reviewed at the appointment.', customer_confirmation='CONFIRMED')
            workflow.close_case(manager, closed.pk, closed.version, 'Corrective styling completed and outcome recorded.',
                                'Follow-up wash and corrective styling, with a final finish check.', synthetic=True)
        record('positive', 'A lovely visit with the team', 'Friendly, careful team. No waiting, and a great haircut.', 5, [], customer='Leena · fictional')
        record('no-text', 'Low rating · clarification needed', '', 1, [], customer='Reviewer · fictional')
        record('other-branch', 'Riverside service follow-up', 'Please follow up on the finishing details.', 3,
               [('SERVICE', 'Customer asks for a finishing review.', 'NORMAL')], branch=riverside)
        record('unassigned', 'Manager assignment needed', 'I would like to speak to the branch manager.', 2,
               [('OTHER', 'Customer requests a manager follow-up.', 'NORMAL')], branch=unstaffed)
        self.stdout.write(self.style.SUCCESS('Fictional review workspace ready. No external notifications or Google writes were sent.'))
        self.stdout.write('Manager: review.manager | HQ: review.admin | Other branch: review.other')
