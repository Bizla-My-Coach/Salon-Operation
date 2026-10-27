"""Lambda findings remain visible outside the collapsible source/reply cards."""
from copy import deepcopy
from html.parser import HTMLParser
import json

from django.test import TestCase, override_settings
from django.urls import reverse

from . import test_google_reviews as base
from .models import GoogleReview, GoogleReviewAction


class ReviewLayoutParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.details = []
        self.visible_findings = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'details':
            self.depth += 1
            if 'review-foldable' in attrs.get('class', '').split():
                self.details.append(attrs)
        if tag == 'section' and 'review-findings' in attrs.get('class', '').split():
            self.visible_findings.append(self.depth == 0)

    def handle_endtag(self, tag):
        if tag == 'details':
            self.depth -= 1


@override_settings(**base.CONFIG)
class GoogleReviewFindingsTests(TestCase):
    def setUp(self):
        base.GoogleReviewTests.setUp(self)
        self.payload.update(environment='production', synthetic=False)
        self.payload['source']['review_snapshot'].update(star_rating=5,
            text='Great haircut and friendly staff, but I waited 40 minutes.')
        self.payload['analysis'].update(sentiment='mixed', requires_human_review=False,
            issues=[{'category': 'waiting_time', 'reported_issue': 'Customer reported waiting 40 minutes.', 'priority': 'normal'}])
        self.send()
        self.review = GoogleReview.objects.get()
        self.client.force_login(self.people['manager'])

    def send(self, payload=None):
        return self.api.post(reverse('google_review_results'), data=json.dumps(payload or self.payload),
            content_type='application/json', HTTP_X_INTEGRATION_KEY=base.PRODUCTION_TOKEN)

    def detail(self):
        return self.client.get(reverse('google_review_detail', args=[self.review.pk]))

    def update(self, **extra):
        self.review.refresh_from_db()
        data = {'action': 'update', 'version': self.review.edit_token, 'intent': 'save',
            'customer_followup': 'CONTACTED', 'staff_followup': 'IN_PROGRESS', 'justification': 'Checked with the branch team.'}
        data.update(extra)
        return self.client.post(reverse('google_review_action', args=[self.review.pk]), data)

    def next_result(self):
        payload = deepcopy(self.payload)
        payload['event_id'] = 'refreshed-findings'
        payload['processing']['analyzed_at'] = '2026-10-20T10:00:00Z'
        return payload

    def test_lambda_issue_is_visible_for_all_authorized_roles_and_sources_are_collapsible(self):
        for role in ('manager', 'gm', 'admin'):
            with self.subTest(role=role):
                self.client.force_login(self.people[role])
                response = self.detail()
                for text in ('Customer reported waiting 40 minutes.', 'Waiting time', 'Normal priority', 'Issues', 'Appreciation'):
                    self.assertContains(response, text)
                parser = ReviewLayoutParser()
                parser.feed(response.content.decode())
                self.assertEqual(len(parser.details), 2)
                self.assertTrue(all('open' not in attrs for attrs in parser.details))
                self.assertEqual(parser.visible_findings, [True, True])
                html = response.content.decode()
                self.assertLess(html.index('id="review-issues-heading"'), html.index('name="justification"'))
                self.assertContains(response, '<select', count=2)
                self.assertContains(response, '<textarea', count=1)
                self.assertNotContains(response, 'amazon.nova')

    def test_sample_without_appreciation_points_does_not_invent_them(self):
        response = self.detail()
        self.assertEqual(response.context['review_appreciations'], [])
        self.assertContains(response, 'No separate appreciation points were supplied.')
        section = response.content.decode().split('id="review-appreciation-heading"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn('Great haircut', section)
        self.assertNotIn('friendly staff', section)

    def test_optional_explicit_appreciations_are_saved_and_displayed_with_html_escaped(self):
        payload = self.next_result()
        payload['analysis']['appreciations'] = ['Customer praised the haircut.',
            {'category': 'staff_friendliness', 'reported_appreciation': 'Customer praised <script>alert(1)</script> friendly staff.'}]
        self.assertEqual(self.send(payload).status_code, 200)
        response = self.detail()
        self.assertContains(response, 'Customer praised the haircut.')
        self.assertContains(response, 'Staff friendliness')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertEqual(len(response.context['review_appreciations']), 2)
        self.review.refresh_from_db()
        self.assertEqual(self.review.analysis['appreciations'], payload['analysis']['appreciations'])

    def test_only_latest_lambda_findings_are_shown_and_earlier_issue_history_remains(self):
        payload = self.next_result()
        payload['analysis']['issues'] = [{'category': 'billing', 'reported_issue': 'New billing concern from Lambda.', 'priority': 'high'}]
        self.assertEqual(self.send(payload).status_code, 200)
        response = self.detail()
        self.assertContains(response, 'New billing concern from Lambda.')
        self.assertNotContains(response, 'Customer reported waiting 40 minutes.')
        self.assertEqual(self.review.case.issues.count(), 2)
        self.assertTrue(self.review.case.issues.filter(reported_issue='Customer reported waiting 40 minutes.', source_active=False).exists())

    def test_duplicate_issues_and_legacy_complaints_do_not_duplicate_cards(self):
        payload = self.next_result()
        payload['analysis']['complaints'] = deepcopy(payload['analysis']['issues'])
        self.assertEqual(self.send(payload).status_code, 200)
        response = self.detail()
        self.assertEqual(len(response.context['review_issues']), 1)
        self.assertContains(response, 'Customer reported waiting 40 minutes.', count=1)

    def test_findings_remain_on_invalid_and_stale_submissions(self):
        response = self.update(justification='')
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Customer reported waiting 40 minutes.', status_code=400)
        token = self.review.edit_token
        self.assertEqual(self.update().status_code, 302)
        response = self.update(version=token)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'Customer reported waiting 40 minutes.', status_code=409)

    def test_explicit_close_requires_terminal_staff_status_and_preserves_unresolved_outcome(self):
        response = self.update(intent='close')
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Choose Resolved, Unresolved or Not actionable', status_code=400)
        self.assertFalse(GoogleReviewAction.objects.exists())
        self.assertEqual(self.update(intent='close', staff_followup='CLOSED_UNRESOLVED').status_code, 302)
        self.review.refresh_from_db()
        self.assertTrue(self.review.is_closed)
        self.assertEqual(self.review.case.closure_outcome, 'UNRESOLVED')
        self.assertNotContains(self.detail(), '>Close review</button>')

    def test_optional_missing_or_malformed_findings_are_backward_compatible(self):
        for analysis in ({}, {'issues': None, 'appreciations': {}},
                         {'issues': [None, {}], 'appreciations': [None, {}, '', {'reported_appreciation': 123}]}, []):
            with self.subTest(analysis=analysis):
                GoogleReview.objects.filter(pk=self.review.pk).update(analysis=analysis)
                response = self.detail()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['review_issues'], [])
                self.assertEqual(response.context['review_appreciations'], [])

    def test_other_branches_and_employees_cannot_view_findings_or_submit_close(self):
        for role, expected in [('other', 404), ('employee', 403)]:
            self.client.force_login(self.people[role])
            self.assertEqual(self.detail().status_code, expected)
            self.assertEqual(self.update(intent='close', staff_followup='RESOLVED').status_code, expected)
