# SalonOps production server handover

Ubuntu Linux x64 · **main** branch · **operation.hairship.in**

Company Nginx must already provide HTTPS and proxy to `127.0.0.1:8000`.
Do not expose 8000/5432. GitHub Actions currently runs checks only; it does not
deploy to this server or require a production environment or runner.

## 1 Create the deployment account

As the server administrator:

```bash
sudo adduser --disabled-password --gecos "" salonops-deploy
sudo install -d -o salonops-deploy -g salonops-deploy -m 700 \
  /etc/salonops /var/lib/salonops-prod/backups
sudo install -d -o salonops-deploy -g salonops-deploy -m 750 /opt/salonops-prod
sudo -iu salonops-deploy
```

GitHub Actions does not use a self-hosted runner. Do not register this account
as a runner for this repository.

## 2 Install Docker and log in

Install **Docker Engine and Compose plugin** using the
[official Ubuntu commands](https://docs.docker.com/engine/install/ubuntu/).
Review existing installations first. Then, as the administrator:

```bash
sudo systemctl enable --now docker
sudo groupadd --force docker
sudo usermod -aG docker salonops-deploy
sudo -iu salonops-deploy
docker version
docker compose version
docker login ghcr.io --username COMPANY_GITHUB_USERNAME
```

At the password prompt, use a **GitHub PAT classic with read:packages** and Read
access to the GHCR package; authorize organization SSO if needed. Protect the
credentials. Docker group access is effectively root access and needs approval.

## 3 Create the files before deployment

As **salonops-deploy**, extract the developer's approved **main source ZIP** into
`~/release`. Replace the folder below. **Fresh setup only: never overwrite existing secrets.**

```bash
RELEASE_ROOT=/home/salonops-deploy/release/REPLACE_RELEASE_FOLDER
cp "$RELEASE_ROOT"/compose.production.yaml "$RELEASE_ROOT"/compose.caddy.yaml /opt/salonops-prod/
mkdir -p /opt/salonops-prod/{deploy,scripts}
cp -r "$RELEASE_ROOT"/deploy/postgres /opt/salonops-prod/deploy/
cp "$RELEASE_ROOT"/deploy/Caddyfile /opt/salonops-prod/deploy/
cp "$RELEASE_ROOT"/scripts/*.sh /opt/salonops-prod/scripts/
chmod 750 /opt/salonops-prod/scripts/*.sh
chmod 644 /opt/salonops-prod/deploy/postgres/*.sh
install -m 600 "$RELEASE_ROOT"/deploy/production.env.example /etc/salonops/prod.env
install -m 600 "$RELEASE_ROOT"/deploy/restic.env.example /etc/salonops/prod.restic.env
install -m 600 "$RELEASE_ROOT"/deploy/ops.env.example /etc/salonops/prod.ops.env
```

Edit securely and replace all placeholders:

- **prod.env:** independent random `SECRET_KEY`, `POSTGRES_PASSWORD` and
  `POSTGRES_ADMIN_PASSWORD`; approved release image digest in `SALONOPS_IMAGE`;
  `ALLOWED_HOSTS=operation.hairship.in,localhost,127.0.0.1`;
  `CSRF_TRUSTED_ORIGINS=https://operation.hairship.in`.
  Keep production/secure-cookie settings and volume **salonops-prod-pgdata** unchanged.
  For AWS review ingestion, set `REVIEW_INTEGRATION_KEY_PRODUCTION` privately to
  the same `X-Integration-Key` value held in the production AWS Secret. Configure
  AWS `APP_REVIEW_RESULT_API_URL` as
  `https://operation.hairship.in/api/integrations/google-reviews/results`.
  See [review integration](google-review-integration.md) for key transfer and
  authenticated receipt verification; never copy a sandbox key into production.
- **prod.restic.env:** company **off-server HTTPS S3/Restic repository**, encryption
  password and scoped storage credentials. Local-only backups cannot deploy production.
- **prod.ops.env:** keep the supplied production paths.

Keep files mode **600**, owned by salonops-deploy; save protected copies in the
company vault. Never commit secrets or DB backups. Set
`SALONOPS_USE_CADDY=false` in the deployment shell unless the approved Caddy
configuration is being used.

## 4 Initialize the backup and deploy

Continue as **salonops-deploy**. Replace the digest with the approved published release image:

```bash
OPS=(/etc/salonops/prod.env salonops-prod /opt/salonops-prod/compose.production.yaml
     /var/lib/salonops-prod/backups /etc/salonops/prod.restic.env)
REGISTRY=ghcr.io/bizla-my-coach/salon-operation
IMAGE="$REGISTRY@sha256:REPLACE_WITH_APPROVED_64_HEX_DIGEST"
export SALONOPS_USE_CADDY=false
bash /opt/salonops-prod/scripts/restic_admin.sh "${OPS[@]}" init
bash /opt/salonops-prod/scripts/deploy.sh "${OPS[@]}" "$IMAGE" "$REGISTRY"
```

Use `init` only for a new backup repository; use `snapshots` for an approved existing
one. Deployment backs up **before migrations**; investigate failures, never bypass them.

## 5 Create the superuser and verify recovery

Continue in the same shell:

```bash
export SALONOPS_IMAGE="$(cat /var/lib/salonops-prod/backups/salonops-prod.last-successful-image)"
dc() { docker compose --env-file /etc/salonops/prod.env -p salonops-prod \
  -f /opt/salonops-prod/compose.production.yaml "$@"; }
dc exec web python manage.py createsuperuser
dc exec -T web python manage.py check_production_security
curl --fail https://operation.hairship.in/health/
bash /opt/salonops-prod/scripts/backup.sh "${OPS[@]}"
bash /opt/salonops-prod/scripts/rehearse_offsite.sh "${OPS[@]}"
```

Choose a new website username/password, **not the DB password**. Verify HTTPS login,
backup and isolated restore success.

## 6 Enable scheduled backups

Return to the administrator using `exit`; set `RELEASE_ROOT` again to the approved folder:

```bash
sudo install -m 644 "$RELEASE_ROOT"/deploy/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start salonops-backup@prod.service salonops-rehearsal@prod.service
sudo systemctl enable --now salonops-backup@prod.timer salonops-rehearsal@prod.timer
systemctl list-timers 'salonops-*'
```

After verification, pull requests and `dev` pushes run CI only. A push to
**main** runs checks, publishes an immutable application image, and deploys on
`salonops-production-01` through the `production` GitHub environment. Any required
environment reviewers must approve before deployment. See
[production deployment](production-deployment.md#5-github-checks-and-automatic-production-releases)
for runner permissions, registry access, backup ordering, and failure handling.
Check the Actions deployment result and website after each release.

**Never run `docker compose down -v` or prune the production DB volume.**
The company manages certificate renewal, credentials, monitoring and backup failures.
