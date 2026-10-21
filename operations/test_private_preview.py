from copy import deepcopy
import importlib.util

from django.conf import settings
from django.test import SimpleTestCase


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, settings.BASE_DIR / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate = load_module('preview_validator', 'scripts/validate_preview.py').validate
preview_config = load_module('preview_fixture', 'tests/preview_config_fixture.py').preview_config


class PrivatePreviewConfigTests(SimpleTestCase):
    def test_only_https_web_receives_review_integration_credentials(self):
        config = preview_config(https=True)
        config['services']['web']['environment'].update(REVIEW_INTEGRATION_KEY_SANDBOX='synthetic-test-key')
        validate(config, https=True)
        config['services']['web']['environment']['POSTGRES_ADMIN_PASSWORD'] = 'not-allowed'
        with self.assertRaises(ValueError):
            validate(config, https=True)
        config = preview_config()
        config['services']['web']['environment']['REVIEW_INTEGRATION_KEY_SANDBOX'] = 'synthetic-test-key'
        with self.assertRaises(ValueError):
            validate(config)

    def test_https_local_backup_test_mode_requires_explicit_selection(self):
        config = preview_config(https=True)
        validate(config, https=True)
        with self.assertRaises(ValueError):
            validate(config)
        with self.assertRaises(ValueError):
            validate(preview_config(), https=True)

    def test_https_test_never_accepts_insecure_cookies_or_production_targets(self):
        changes = [
            lambda c: c.update(name='salonops-prod'),
            lambda c: c['volumes']['postgres_data'].update(name='salonops-prod-pgdata'),
            lambda c: c['services']['web']['environment'].update(SESSION_COOKIE_SECURE='False'),
            lambda c: c['services']['web']['environment'].update(CSRF_COOKIE_SECURE='False'),
            lambda c: c['services']['web']['environment'].update(SECURE_SSL_REDIRECT='False'),
            lambda c: c['services']['web']['environment'].update(ALLOWED_HOSTS='operations.hairship.in'),
            lambda c: c['services']['web']['ports'][0].update(host_ip='0.0.0.0'),
            lambda c: c['services']['db'].update(ports=[{'published': '5432'}]),
            lambda c: c['services']['proxy']['ports'].append({'target': 2019, 'published': '2019'}),
            lambda c: c['services']['proxy']['volumes'][0].update(source='/etc/private.env'),
        ]
        for change in changes:
            with self.subTest(change=change):
                config = preview_config(https=True)
                change(config)
                with self.assertRaises(ValueError):
                    validate(config, https=True)

    def test_https_switch_preserves_fixed_test_database_and_volume(self):
        private, public = preview_config(), preview_config(https=True)
        self.assertEqual(private['services']['db'], public['services']['db'])
        self.assertEqual(private['volumes']['postgres_data'], public['volumes']['postgres_data'])

    def test_fixed_private_test_configuration_is_accepted(self):
        validate(preview_config())

    def test_empty_compose_v5_ipam_is_accepted(self):
        config = preview_config()
        config['networks']['default']['ipam'] = {}
        validate(config)

    def test_production_resources_or_public_network_access_are_rejected(self):
        changes = [
            lambda c: c.update(name='salonops-prod'),
            lambda c: c['volumes']['postgres_data'].update(name='salonops-prod-pgdata'),
            lambda c: c['volumes']['postgres_data'].update(external=True),
            lambda c: c['services']['web']['ports'][0].update(host_ip='0.0.0.0'),
            lambda c: c['services']['db'].update(ports=[{'published': '5432'}]),
            lambda c: c['services']['web'].update(network_mode='host'),
            lambda c: c['services']['db'].update(privileged=True),
            lambda c: c['services']['db']['environment'].update(POSTGRES_DB='salon_operations'),
            lambda c: c['services']['web']['environment'].update(POSTGRES_HOST='production-db'),
            lambda c: c['services']['web']['environment'].update(DEBUG='True'),
            lambda c: c['services']['web']['environment'].update(POSTGRES_ADMIN_PASSWORD='secret'),
            lambda c: c['services'].update(caddy={}),
            lambda c: c['networks']['default'].update(driver_opts={'gateway_mode_ipv4': 'routed'}),
            lambda c: c['services']['web'].update(volumes=[{'source': '/etc'}]),
        ]
        for change in changes:
            with self.subTest(change=change):
                config = deepcopy(preview_config())
                change(config)
                with self.assertRaises(ValueError):
                    validate(config)
