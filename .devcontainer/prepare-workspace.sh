#!/usr/bin/env bash
set -Eeuo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Existing Codespaces may have collected these generated files as root.
# Keep their contents, but let the new terminal user update them on startup.
if [[ -d "$root/staticfiles" ]]; then
    sudo chown -R "$(id -u):$(id -g)" "$root/staticfiles"
fi
cd "$root"
python manage.py seed_codespaces --check-environment
python manage.py migrate --noinput
python manage.py seed_codespaces
python manage.py seed_google_review_demo
