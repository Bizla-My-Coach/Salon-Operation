"""Validate resolved Compose JSON without printing its credentials (Linux host)."""
import json
import sys


def validate(config, https=False):
    def require(condition):
        if not condition:
            raise ValueError('Unsafe local-backup test configuration; use the reviewed standalone Compose file.')

    require(config.get('name') == 'salonops-dev')
    services = config.get('services', {})
    require(set(services) == ({'web', 'db', 'proxy'} if https else {'web', 'db'}))
    web, db = services['web'], services['db']
    for service in services.values():
        require(not service.get('network_mode') and not service.get('privileged'))
        require(not service.get('container_name'))
        require(service.get('networks') == {'default': None} or
                service.get('networks') == {'default': {}})
    fixed = {
        'APP_ENV': 'private-preview', 'DEBUG': 'False',
        'ALLOWED_HOSTS': 'localhost,127.0.0.1', 'CSRF_TRUSTED_ORIGINS': '',
        'TRUST_PROXY_HEADERS': 'False', 'SECURE_SSL_REDIRECT': 'False',
        'SESSION_COOKIE_SECURE': 'False', 'CSRF_COOKIE_SECURE': 'False',
        'SECURE_HSTS_SECONDS': '0', 'SECURE_HSTS_INCLUDE_SUBDOMAINS': 'False',
        'SECURE_HSTS_PRELOAD': 'False', 'POSTGRES_HOST': 'db',
        'POSTGRES_PORT': '5432', 'POSTGRES_DB': 'salonops_dev',
        'POSTGRES_USER': 'salonops_dev_app', 'AUTO_MIGRATE': 'false',
        'CHECK_DEPLOY': 'false',
    }
    if https:
        fixed.update({
            'APP_ENV': 'production',
            'ALLOWED_HOSTS': 'operations.shahinanalytics.com,localhost,127.0.0.1',
            'CSRF_TRUSTED_ORIGINS': 'https://operations.shahinanalytics.com',
            'TRUST_PROXY_HEADERS': 'True', 'SECURE_SSL_REDIRECT': 'True',
            'SESSION_COOKIE_SECURE': 'True', 'CSRF_COOKIE_SECURE': 'True',
            'SECURE_HSTS_SECONDS': '300', 'CHECK_DEPLOY': 'true',
        })
    environment = web.get('environment', {})
    require(all(environment.get(key) == value for key, value in fixed.items()))
    required = set(fixed) | {'SECRET_KEY', 'POSTGRES_PASSWORD', 'TIME_ZONE'}
    integration = {'REVIEW_INTEGRATION_KEY_SANDBOX', 'REVIEW_INTEGRATION_KEY_PRODUCTION'} if https else set()
    require(required <= set(environment) <= required | integration)
    ports = web.get('ports', [])
    require(len(ports) == 1 and ports[0].get('host_ip') == '127.0.0.1' and
            str(ports[0].get('published')) == '8000' and
            ports[0].get('target') == 8000 and ports[0].get('protocol') == 'tcp')
    require(not web.get('volumes') and not db.get('ports'))
    require(db.get('image') == 'postgres:17-alpine')
    db_env = db.get('environment', {})
    require(db_env.get('POSTGRES_DB') == 'salonops_dev' and
            db_env.get('POSTGRES_USER') == 'salonops_admin' and
            db_env.get('APP_DB_USER') == 'salonops_dev_app')
    require(set(db_env) == {'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD',
                            'APP_DB_USER', 'APP_DB_PASSWORD'})
    require(db_env.get('APP_DB_PASSWORD') == environment.get('POSTGRES_PASSWORD'))
    volumes = config.get('volumes', {})
    require(set(volumes) == ({'postgres_data', 'caddy_data', 'caddy_config'} if https else {'postgres_data'}))
    require(volumes['postgres_data'] == {'name': 'salonops-dev-pgdata'})
    mounts = db.get('volumes', [])
    require(len(mounts) == 2)
    data, init = mounts
    require(data.get('type') == 'volume' and data.get('source') == 'postgres_data' and
            data.get('target') == '/var/lib/postgresql/data')
    require(init.get('type') == 'bind' and init.get('read_only') is True and
            init.get('source', '').replace('\\', '/').endswith('/deploy/postgres/10-create-app-user.sh') and
            init.get('target') == '/docker-entrypoint-initdb.d/10-create-app-user.sh')
    networks = config.get('networks', {})
    require(set(networks) == {'default'})
    network = dict(networks['default'])
    # Compose v5 emits an empty ipam object; v2 usually omits it.
    require(network.pop('ipam', {}) == {})
    require(network == {'name': 'salonops-dev_default'})
    if https:
        proxy = services['proxy']
        require(proxy.get('image') == 'caddy:2-alpine' and not proxy.get('environment'))
        ports = proxy.get('ports', [])
        require(len(ports) == 2)
        require({(p.get('target'), str(p.get('published')), p.get('protocol'),
                  p.get('host_ip', '0.0.0.0')) for p in ports} ==
                {(80, '80', 'tcp', '0.0.0.0'), (443, '443', 'tcp', '0.0.0.0')})
        require(volumes['caddy_data'] == {'name': 'salonops-dev-caddy-data'})
        require(volumes['caddy_config'] == {'name': 'salonops-dev-caddy-config'})
        mounts = proxy.get('volumes', [])
        require(len(mounts) == 3)
        config_mount, data_mount, cache_mount = mounts
        require(config_mount.get('type') == 'bind' and config_mount.get('read_only') is True and
                config_mount.get('source', '').replace('\\', '/').endswith('/deploy/Caddyfile.test') and
                config_mount.get('target') == '/etc/caddy/Caddyfile')
        require(data_mount.get('type') == 'volume' and data_mount.get('source') == 'caddy_data' and
                data_mount.get('target') == '/data')
        require(cache_mount.get('type') == 'volume' and cache_mount.get('source') == 'caddy_config' and
                cache_mount.get('target') == '/config')


if __name__ == '__main__':
    try:
        # PowerShell's native pipeline can prepend a UTF-8 BOM.
        if sys.argv[1:] not in ([], ['--https']):
            raise ValueError('Unknown test mode.')
        validate(json.loads(sys.stdin.read().removeprefix('\ufeff')), https=bool(sys.argv[1:]))
    except (ValueError, KeyError, TypeError, AttributeError):
        # Never dump Compose JSON: it contains secrets.
        print('ERROR: Invalid or unsafe local-backup test Compose configuration.', file=sys.stderr)
        sys.exit(1)
