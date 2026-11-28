from datetime import date, datetime, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .admin import ReviewTargetForm
from .models import Branch, GoogleReview, ReviewTarget
from .review_targets import month_targets, progress


class ReviewTargetTests(TestCase):
    def setUp(self):
        self.north = Branch.objects.create(code='N', name='North')
        self.south = Branch.objects.create(code='S', name='South')

    def person(self, username, role, branch=None):
        user = User.objects.create_user(username)
        user.profile.role, user.profile.branch = role, branch
        user.profile.save()
        return user

    def review(self, branch, when, n):
        aware = timezone.make_aware(datetime.combine(when, datetime.min.time()) + timedelta(hours=12))
        return GoogleReview.objects.create(environment='production', review_name=f'r/{n}', account_name='a',
            location_name='l', google_review_id=str(n), branch=branch, star_rating=5, google_created_at=aware,
            google_updated_at=aware, analysis_version='v', analyzed_at=aware, version_fingerprint='f', sentiment='positive')

    def test_month_split_weights_weekends_and_adds_up(self):
        # November 2026 has 30 days: 9 weekend days (Sat/Sun) and 21 weekdays.
        target = ReviewTarget.objects.create(branch=self.north, month=date(2026, 11, 17), target=78,
            weekday_weight=1, weekend_weight=2)
        self.assertEqual(target.month, date(2026, 11, 1))
        daily = target.daily_targets()
        self.assertEqual(len(daily), 30)
        self.assertAlmostEqual(sum(daily.values()), 78)
        self.assertAlmostEqual(daily[date(2026, 11, 2)], 2.0)   # Monday
        self.assertAlmostEqual(daily[date(2026, 11, 1)], 4.0)   # Sunday
        target.weekend_days = '4,5,6'  # Friday also counts as weekend
        self.assertAlmostEqual(target.daily_targets()[date(2026, 11, 6)], target.daily_targets()[date(2026, 11, 7)])

    def test_progress_counts_this_branch_and_month_and_compares_with_expected(self):
        target = ReviewTarget.objects.create(branch=self.north, month=date(2026, 11, 1), target=78)
        for n, day in enumerate([date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3), date(2026, 10, 31)]):
            self.review(self.north, day, n)
        self.review(self.south, date(2026, 11, 2), 99)
        row = progress(target, today=date(2026, 11, 3))
        self.assertEqual(row['done'], 3)
        self.assertEqual(row['expected'], 8)  # Sun 4 + Mon 2 + Tue 2
        self.assertEqual(row['behind'], 5)
        self.assertFalse(row['on_track'])
        self.assertEqual(row['today_done'], 1)
        self.assertEqual(row['today_goal'], 2)
        self.assertEqual(row['days_left'], 27)
        self.assertEqual([r['branch'] for r in month_targets(today=date(2026, 11, 3))], [self.north])

    def test_admin_form_saves_weekend_days(self):
        form = ReviewTargetForm(data={'branch': self.north.pk, 'month': '2026-12-05', 'target': 60,
            'weekday_weight': '1', 'weekend_weight': '1.5', 'weekend_days': ['5', '6']})
        self.assertTrue(form.is_valid(), form.errors)
        target = form.save()
        self.assertEqual(target.weekend_days, '5,6')
        self.assertEqual(target.month, date(2026, 12, 1))
        bad = ReviewTargetForm(data={'branch': self.south.pk, 'month': '2026-12-01', 'target': 10,
            'weekday_weight': '0', 'weekend_weight': '0', 'weekend_days': ['6']})
        self.assertFalse(bad.is_valid())

    def test_dashboards_show_the_bar(self):
        today = timezone.localdate()
        ReviewTarget.objects.create(branch=self.north, month=today, target=30)
        ReviewTarget.objects.create(branch=self.south, month=today, target=20)
        self.review(self.north, today, 1)
        manager = self.person('manager', 'MANAGER', self.north)
        self.client.force_login(manager)
        page = self.client.get(reverse('manager_dashboard'))
        self.assertContains(page, 'Google review target')
        self.assertContains(page, '1 of 30 reviews')
        self.assertNotContains(page, 'South')
        admin = self.person('admin', 'ADMIN')
        self.client.force_login(admin)
        page = self.client.get(reverse('admin_dashboard'))
        self.assertContains(page, 'North')
        self.assertContains(page, '0/20')
        gm = self.person('gm', 'GENERAL_MANAGER')
        self.client.force_login(gm)
        self.assertContains(self.client.get(reverse('general_manager_dashboard')), '1/30')
        self.assertContains(self.client.get(reverse('general_manager_dashboard'), {'branch': self.south.pk}), '0 of 20 reviews')
