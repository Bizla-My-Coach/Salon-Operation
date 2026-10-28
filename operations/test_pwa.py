import json
import struct
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse


@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class InstallableAppTests(TestCase):
    def test_sign_in_page_exposes_manifest_and_phone_metadata(self):
        response = self.client.get(reverse('login'))
        self.assertContains(response, f'rel="manifest" href="{reverse("pwa_manifest")}"')
        self.assertContains(response, 'rel="apple-touch-icon"')
        self.assertContains(response, 'js/pwa.js')
        self.assertNotContains(response, 'data-install-app')

    def test_all_staff_roles_have_a_conditional_install_button(self):
        user = User.objects.create_user('pwa_staff')
        self.client.force_login(user)
        for role in ['EMPLOYEE', 'MANAGER', 'GENERAL_MANAGER', 'ADMIN']:
            user.profile.role = role
            user.profile.save()
            response = self.client.get(reverse('my_profile'))
            self.assertContains(response, 'data-install-app hidden>Install Hairship</button>')


class PublicAppResourceTests(SimpleTestCase):
    def read_response(self, name):
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200)
        return response, b''.join(response.streaming_content)

    def test_manifest_supports_standalone_installation_with_valid_square_icons(self):
        response, body = self.read_response('pwa_manifest')
        self.assertEqual(response['Content-Type'], 'application/manifest+json')
        manifest = json.loads(body)
        self.assertEqual(manifest['id'], '/')
        self.assertEqual(manifest['start_url'], '/')
        self.assertEqual(manifest['scope'], '/')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertFalse(manifest['prefer_related_applications'])
        self.assertTrue({'192x192', '512x512'}.issubset({icon['sizes'] for icon in manifest['icons']}))
        self.assertTrue(any(icon['purpose'] == 'maskable' for icon in manifest['icons']))
        for icon in manifest['icons']:
            path = Path(settings.BASE_DIR) / icon['src'].lstrip('/')
            data = path.read_bytes()
            self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
            width, height = struct.unpack('>II', data[16:24])
            self.assertEqual(f'{width}x{height}', icon['sizes'])
            self.assertEqual(width, height)

    def test_service_worker_is_public_at_root_and_can_update_without_stale_caching(self):
        response, body = self.read_response('pwa_service_worker')
        self.assertEqual(reverse('pwa_service_worker'), '/service-worker.js')
        self.assertEqual(response['Content-Type'], 'application/javascript')
        self.assertEqual(response['Service-Worker-Allowed'], '/')
        self.assertEqual(response['Cache-Control'], 'no-cache')
        self.assertIn(b'addEventListener', body)

    def test_offline_fallback_is_public_and_independent_of_staff_data(self):
        response, body = self.read_response('pwa_offline')
        self.assertEqual(response['Content-Type'], 'text/html; charset=utf-8')
        self.assertIn(b'Connect to Wi-Fi or mobile data', body)
        self.assertIn(b'href="/"', body)
        self.assertNotIn(b'csrfmiddlewaretoken', body)
        self.assertNotIn(b'localStorage', body)

    def test_public_resources_reject_mutation_requests(self):
        for name in ['pwa_manifest', 'pwa_service_worker', 'pwa_offline']:
            self.assertEqual(self.client.post(reverse(name)).status_code, 405)
