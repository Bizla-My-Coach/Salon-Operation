from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class PublicPagesTests(TestCase):
    def test_pages_open_without_sign_in_for_both_spellings(self):
        for path in ['/about', '/about/', '/privacy', '/privacy/', '/terms', '/terms/']:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertContains(response, 'info@hairship.in')

    def test_about_page_covers_google_branding_requirements(self):
        response = self.client.get('/about')
        for text in ['Hairship Operations', 'Google review retrieval', 'Manager follow-up', 'Automated replies',
                     'href="/privacy"', 'href="/terms"', 'mailto:info@hairship.in']:
            self.assertContains(response, text)

    def test_privacy_policy_states_google_limited_use(self):
        response = self.client.get('/privacy')
        self.assertContains(response, 'Google API Services User Data Policy')
        self.assertContains(response, 'Limited Use')

    def test_signed_in_staff_can_also_open_pages(self):
        self.client.force_login(User.objects.create_user('public_pages_staff'))
        self.assertEqual(self.client.get('/about').status_code, 200)

    def test_dashboard_still_requires_sign_in(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])
