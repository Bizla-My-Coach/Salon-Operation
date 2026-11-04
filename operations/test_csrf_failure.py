from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse


@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class CsrfFailureTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        User.objects.create_user('phone_user', password='pw-12345-abc')

    def stale_login_post(self, path=None):
        self.client.get(reverse('login'))  # sets the CSRF cookie
        with self.assertLogs('salonops.csrf', 'WARNING'):
            return self.client.post(path or reverse('login'), {
                'username': 'phone_user', 'password': 'pw-12345-abc', 'csrfmiddlewaretoken': 'stale' * 8,
            })

    def test_stale_login_form_redirects_to_fresh_sign_in_with_message(self):
        response = self.stale_login_post()
        self.assertRedirects(response, reverse('login') + '?expired=1')
        page = self.client.get(reverse('login') + '?expired=1')
        self.assertContains(page, 'Your sign-in page had expired. Please sign in again.')
        self.assertContains(page, 'csrfmiddlewaretoken')

    def test_stale_login_keeps_safe_next_and_drops_external_next(self):
        response = self.stale_login_post(reverse('login') + '?next=/profile/')
        self.assertRedirects(response, reverse('login') + '?expired=1&next=%2Fprofile%2F', fetch_redirect_response=False)
        response = self.stale_login_post(reverse('login') + '?next=https://evil.example/')
        self.assertRedirects(response, reverse('login') + '?expired=1')
        self.assertNotContains(self.client.get(reverse('login')), 'had expired')

    def test_fresh_form_after_redirect_signs_in(self):
        self.stale_login_post()
        token = self.client.cookies['csrftoken'].value
        response = self.client.post(reverse('login'), {
            'username': 'phone_user', 'password': 'pw-12345-abc', 'csrfmiddlewaretoken': token})
        self.assertRedirects(response, reverse('dashboard'), fetch_redirect_response=False)

    def test_blocked_cookies_show_friendly_page_not_a_loop(self):
        with self.assertLogs('salonops.csrf', 'WARNING') as logs:
            response = Client(enforce_csrf_checks=True).post(reverse('login'), {'username': 'x', 'password': 'y'})
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'Cookies are blocked', status_code=403)
        self.assertIn('CSRF cookie not set', logs.output[0])

    def test_other_pages_show_friendly_expired_page(self):
        self.client.get(reverse('login'))  # CSRF cookie first, then sign in
        self.client.force_login(User.objects.get(username='phone_user'))
        with self.assertLogs('salonops.csrf', 'WARNING'):
            response = self.client.post(reverse('save_theme_preference'), {'csrfmiddlewaretoken': 'stale' * 8})
        self.assertContains(response, 'This page has expired', status_code=403)
