#!/usr/bin/env bash
# Run inside the repository's Codespaces dev container.
set -Eeuo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
# Codespaces also injects these variables into the terminal/container. Resolve
# them here in case they were not available to the host initialization hook.
if [[ -n "${CODESPACE_NAME:-}" ]]; then
    domain=${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}
    [[ "$CODESPACE_NAME" =~ ^[a-zA-Z0-9-]+$ && "$domain" =~ ^[a-zA-Z0-9.-]+$ ]] || { echo 'Invalid Codespaces hostname.' >&2; exit 1; }
    codespaces_host="${CODESPACE_NAME}-8000.${domain}"
    export ALLOWED_HOSTS="localhost,127.0.0.1,testserver,$codespaces_host"
    export CSRF_TRUSTED_ORIGINS="https://$codespaces_host"
    export TRUST_PROXY_HEADERS=True SESSION_COOKIE_SECURE=True CSRF_COOKIE_SECURE=True
    export SALONOPS_CODESPACES_ORIGIN="https://$codespaces_host"
fi
python manage.py seed_codespaces --check-environment
python manage.py migrate --noinput
python manage.py seed_codespaces
python manage.py collectstatic --noinput
printf '\nSalonOps test is starting: %s\nAdmin login: admin / admin\nStop with Ctrl+C; run this command again to resume.\n\n' "${SALONOPS_CODESPACES_ORIGIN:-http://127.0.0.1:8000}"
exec python manage.py runserver 0.0.0.0:8000 --noreload
