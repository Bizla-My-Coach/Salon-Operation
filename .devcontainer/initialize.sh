#!/usr/bin/env bash
# Runs on the Codespaces host before Compose reads its env_file.
set -Eeuo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$root/.devcontainer/.env" <<'PY'
import os
import re
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.exists():
    print('Keeping the existing isolated Codespaces configuration.')
    sys.exit(0)
name = os.environ.get('CODESPACE_NAME', '')
domain = os.environ.get('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN', 'app.github.dev')
if name and (not re.fullmatch(r'[a-zA-Z0-9-]+', name) or not re.fullmatch(r'[a-zA-Z0-9.-]+', domain)):
    sys.exit('Invalid Codespaces forwarding hostname.')
host = f'{name}-8000.{domain}' if name else ''
values = {
    'SALONOPS_CODESPACES': 'true', 'APP_ENV': 'development', 'DEBUG': 'True',
    'SECRET_KEY': secrets.token_urlsafe(64),
    'POSTGRES_DB': 'salonops_codespaces', 'POSTGRES_USER': 'salonops_codespaces',
    'POSTGRES_PASSWORD': secrets.token_urlsafe(32), 'POSTGRES_HOST': 'codespaces-db', 'POSTGRES_PORT': '5432',
    'ALLOWED_HOSTS': 'localhost,127.0.0.1,testserver' + (',' + host if host else ''),
    'CSRF_TRUSTED_ORIGINS': ','.join((['https://' + host] if host else []) +
                                    ['http://localhost:8000', 'http://127.0.0.1:8000']),
    'TRUST_PROXY_HEADERS': str(bool(host)), 'SECURE_SSL_REDIRECT': 'False',
    'SESSION_COOKIE_SECURE': str(bool(host)), 'CSRF_COOKIE_SECURE': str(bool(host)),
    'SALONOPS_CODESPACES_ORIGIN': 'https://' + host if host else 'http://127.0.0.1:8000',
}
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
    output.write(''.join(f'{key}={value}\n' for key, value in values.items()))
print('Created private configuration for the isolated Codespaces database.')
PY
