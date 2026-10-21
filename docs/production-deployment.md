# SalonOps: test and production deployment

For the optional **dev-only private preview** without a domain or off-server
storage, use [private-test-deployment.md](private-test-deployment.md). It is not
public production hosting. The secure workflow described below remains the
default and the only production mode.

## What is ready, and what still needs company access

The application now requires PostgreSQL; there is no SQLite fallback. The
standalone production Compose stack runs the app and its **own PostgreSQL
container**, with a permanent named volume and a restricted application login.
They are separate containers, not a database baked into the app image.

GitHub Actions runs checks on GitHub-hosted machines for pull requests and
pushes to `dev` and `main`. It does not publish images or deploy to a server.
Production releases described in this guide are performed separately by an
authorized operator after CI passes. Routine interactive testing uses Codespaces.

The two permanent branches are `dev` and `main`; `dev` is used for integration
and Codespaces testing, while `main` is GitHub's default branch and contains
approved production releases. Production releases are performed manually;
GitHub Actions does not use a production environment or runner. Existing `/opt/salonops-prod`,
`/etc/salonops/prod.env`, backup paths, timer instance names and DB volume names
do **not** change when the branch changes. Never rename a DB volume to match a
Git branch. The retired `prod` branch is not needed.

This repository does not install a runner, issue a certificate, create a backup
bucket, configure GitHub protection rules, or change an existing server. The
company must approve and supply those first. This guide describes a separate
manual release process; Actions does not connect to the production server.

## 1. Company checklist (one-time administrator work)

- Dedicated Linux x86-64 VPS with systemd, Docker Engine, Docker Compose plugin,
  Bash, coreutils and `flock`. This workflow expects Linux/X64, not Windows/ARM.
- A domain pointing to the VPS; decide who owns HTTPS and ports 80/443.
- Outbound access to GitHub, GHCR, Docker Hub and the backup storage endpoint.
- Company-controlled GHCR read access and encrypted off-server object storage.
- A deployment user (`salonops-deploy`) and permission to manage this stack.
- Firewall: expose only HTTPS/HTTP as appropriate; never publish PostgreSQL or
  port 8000 to the Internet. Restrict SSH using the company's policy.

**Important:** Docker socket/group access is effectively privileged server
access. Limit it to the authorized deployment operator.
See [Docker's security warning](https://docs.docker.com/engine/install/linux-postinstall/).
Keep the repo private; untrusted PR code must never run on the production VPS.

Company administrator (after approving the deployment user's privileges):

```bash
sudo install -d -o salonops-deploy -g salonops-deploy -m 700 /etc/salonops
sudo install -d -o salonops-deploy -g salonops-deploy -m 750 /opt/salonops-prod
sudo install -d -o salonops-deploy -g salonops-deploy -m 700 /var/lib/salonops-prod/backups
```

Use separate `salonops-dev` paths on the test server. Never reuse production
credentials, volumes, backup repository or real customer data on the test server.

## 2. Configuration and passwords

Copy `deploy/production.env.example` to `/etc/salonops/prod.env`, outside the
checkout. Replace **every placeholder**, especially the key, domain and three
database settings/passwords. Generate independent random values, for example:

```bash
python3 -c 'import secrets; print(secrets.token_hex(64))'
```

- `SECRET_KEY`: unique to this environment; keep stable across normal releases.
- `POSTGRES_USER=salon_app`: restricted Django login, not the administrator.
- `POSTGRES_PASSWORD`: application DB password.
- `POSTGRES_ADMIN_PASSWORD`: different bootstrap administrator password.
- `POSTGRES_VOLUME_NAME=salonops-prod-pgdata`: permanent name; never change it
  during ordinary deployments or you will appear to have an empty database.
- `ALLOWED_HOSTS`: actual hostname plus `localhost,127.0.0.1` for health checks.
- `CSRF_TRUSTED_ORIGINS`: actual `https://` origin, no wildcards.
- `SALONOPS_IMAGE`: approved GHCR `@sha256:...` digest from the manual release process.
- `SALONOPS_ENV_FILE`: supplied by scripts, not stored inside this file.

Protect configuration with mode 600, readable by the deployment user:

```bash
sudo chown salonops-deploy:salonops-deploy /etc/salonops/prod.env
sudo chmod 600 /etc/salonops/prod.env
```

The production web container receives only an explicit application-settings
allowlist; it does not receive the bootstrap admin or backup credentials.

DB initialization runs **only on an empty volume**. Changing passwords in the
env file does not update existing PostgreSQL roles. Coordinate a real password
rotation with a database administrator. Existing local development uses an
administrator DB role; do not reuse that role/volume for production. The new
stack creates `salonops_admin` for bootstrap and a non-superuser app role with
no CREATEDB, CREATEROLE or replication rights. It owns its application database
so it can run migrations. Only disposable CI uses CREATEDB for Django tests.

## 3. HTTPS: choose ONE option

### A. Company manages Nginx / another reverse proxy (default)

The company terminates TLS with a valid certificate and forwards to
`http://127.0.0.1:8000` on this VPS. It must preserve Host and **overwrite**, not
trust an incoming client-supplied `X-Forwarded-Proto` with its own TLS scheme.
Set `TRUST_PROXY_HEADERS=True` only after this is guaranteed. A proxy on a
different machine needs a company-designed private transport; do not simply
expose the backend publicly. Example when Nginx is the direct TLS terminator:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

The company still configures its TLS server block, HTTP-to-HTTPS redirect and
certificate renewal. Leave `SALONOPS_USE_CADDY=false` in the deployment shell.

### B. Optional Caddy

If the company has **no** existing proxy and approves using ports 80/443, set
`SITE_DOMAIN`, `ACME_EMAIL`, and `SALONOPS_USE_CADDY=true` in the deployment
shell. The deploy script adds `compose.caddy.yaml` and Caddy handles TLS. DNS
and public certificate validation must work first. Do not run Caddy on ports
already owned by another company service.

Both options use HTTPS redirects, secure session/CSRF cookies and SameSite Lax.
The private `/health/` endpoint alone permits HTTP for container checks. Start
with HSTS 300 seconds; increase after verifying HTTPS. Subdomain HSTS and browser
preload remain opt-in to avoid breaking other company sites. Their two Django
warnings are intentionally reported; `check_production_security` fails on other
deployment warnings. [Django proxy guidance](https://docs.djangoproject.com/en/5.2/ref/settings/#secure-proxy-ssl-header).

## 4. Off-server backups: what they mean and setup

A Docker volume or a backup on the same VPS is **not** enough: both can disappear
with the server. `backup.sh` creates a consistent PostgreSQL custom-format dump,
validates its archive, writes a checksum and uploads it through encrypted Restic
to a **different** server/provider. Failed uploads fail the backup and block
deployment; the local dump remains available for diagnosis.

Create an HTTPS S3-compatible bucket (or HTTPS Restic REST server). Use a dedicated
repository/prefix per environment, bucket-scoped keys and a unique encryption
password. Copy `deploy/restic.env.example` to `/etc/salonops/prod.restic.env`.
For Docker env files use plain values, no shell quoting. chmod 600, same owner.
The password must also live in the company password manager: without it, encrypted
backups cannot be recovered. Store securely outside the VPS copies of deployment
configuration, domains, secrets and storage access instructions as well. DB dumps
do not include OS configuration or database role passwords. There are currently
no persistent customer-upload files; add a separate media backup if that changes.

Install reviewed operational files from the selected release into
`/opt/salonops-prod` with this structure: `compose.production.yaml`,
`compose.caddy.yaml`, `scripts/*.sh`, `deploy/Caddyfile`, `deploy/postgres/*.sh`.
All shell files must have Linux LF line endings (enforced by `.gitattributes`).
As the deployment user, initialize the new remote repository **once**:

```bash
bash /opt/salonops-prod/scripts/restic_admin.sh \
  /etc/salonops/prod.env salonops-prod /opt/salonops-prod/compose.production.yaml \
  /var/lib/salonops-prod/backups /etc/salonops/prod.restic.env init
```

Only HTTPS S3/REST endpoints are accepted by these scripts; the operator must
ensure the endpoint really is off-server. They never silently fall back to local
storage. Remote retention: last 5, daily 14, weekly 8, monthly 6; local successful
backups older than 7 days are removed. Failed uploads are retained locally and
need investigation, including disk-space monitoring.

## 5. GitHub checks and manual releases

Protect `dev` and `main` with required reviews and CI checks. The Actions
workflow runs only on GitHub-hosted runners; it does not use deployment
environments or self-hosted runners, publish images, or deploy. An authorized
operator must separately publish the reviewed image and follow the manual
deployment procedure below. Keep deployment credentials outside the workflow.

## 6. First release and normal updates

After CI passes, an authorized release operator must build and publish the
approved image through the separately controlled release process. Obtain its
immutable digest and manually deploy it as the deployment user (optionally
`export SALONOPS_USE_CADDY=true` first):

```bash
bash /opt/salonops-prod/scripts/deploy.sh \
  /etc/salonops/prod.env salonops-prod /opt/salonops-prod/compose.production.yaml \
  /var/lib/salonops-prod/backups /etc/salonops/prod.restic.env \
  ghcr.io/bizla-my-coach/salon-operation@sha256:REPLACE_WITH_64_HEX_DIGEST \
  ghcr.io/bizla-my-coach/salon-operation
```

Sequence: pull approved image → start DB → verify restricted role → successful
off-site backup → explicit migrations → start app → health/migration checks.
On the very first installation the pre-migration backup is an **empty database**.
Immediately take another backup after migrations and account setup, then rehearse
that backup before enabling scheduled jobs. Create a real administrator
with `manage.py createsuperuser` through the production Compose configuration;
never run `seed_demo` on production. Verify HTTPS login, all role dashboards,
service ordering, verification/invoice/feedback and secure cookies in a browser.

Once configured:

- Work in Codespaces or a feature branch → PR into `dev` → checks run on GitHub-hosted
   runners.
- After testing, open a PR from `dev` into `main` → checks run again. Merging to
   `main` runs checks but does not deploy; production release remains a separate
   manual operation. Branches transfer **code**, not databases.
- Commit Django migrations alongside model changes. Use backwards-compatible
  expand/contract migrations; plan a write-maintenance window for incompatible
  changes. A backup does not make destructive migrations automatically safe.

No destructive automatic DB rollback is attempted. A previous app digest can
be redeployed only if compatible with the current schema; otherwise have the
company conduct a planned DB restore. Failed deployment does not mean DB
migrations have been undone. Never run `docker compose down -v` on either VPS.

## 7. Scheduled backups and restore rehearsal

Copy `deploy/ops.env.example` to `/etc/salonops/prod.ops.env`, adjust paths and
ownership as above. Install the four reviewed `deploy/systemd/*` files into
`/etc/systemd/system/`. As administrator, after the first post-migration backup:

```bash
sudo systemctl daemon-reload
sudo systemctl start salonops-backup@prod.service
sudo systemctl start salonops-rehearsal@prod.service
sudo systemctl enable --now salonops-backup@prod.timer salonops-rehearsal@prod.timer
systemctl list-timers 'salonops-*'
journalctl -u salonops-backup@prod.service -u salonops-rehearsal@prod.service
```

Repeat separately for `dev` if desired. Hourly backups give a target of roughly
one hour of possible data loss when jobs succeed; delays/failures can increase
that. Weekly Sunday UTC rehearsal downloads the latest encrypted remote snapshot,
verifies files, restores into a newly generated isolated PostgreSQL container,
checks core tables, and removes only that temporary container/anonymous volume.
It never connects to or mounts the live DB. Repository check and prune follow a
successful rehearsal. This proves archive recoverability, not a full end-user
disaster recovery exercise; also test app login/workflows on an isolated rebuilt
server periodically and measure how long recovery takes.

**Monitoring is required:** timers/journal are not email/SMS alerts. Company
monitoring should alert on failed units, missing recent snapshots, disk usage,
HTTPS expiry and `/health/` failures. Test the alert path; investigate any failed
weekly rehearsal. Keep independent recovery access if GitHub is unavailable.

Manual download-and-rehearse (same five arguments as `backup.sh`):

```bash
bash /opt/salonops-prod/scripts/rehearse_offsite.sh \
  /etc/salonops/prod.env salonops-prod /opt/salonops-prod/compose.production.yaml \
  /var/lib/salonops-prod/backups /etc/salonops/prod.restic.env
```

## 8. Moving to a new VPS / importing an existing PostgreSQL database

1. Provision new Docker/TLS/configuration and a **new** permanent DB volume with
   the restricted app account. Use the same application release/schema version.
2. Rehearse recovery from off-site first. For cutover, stop customer/staff writes
   on the old server and take a final successful dump + encrypted upload. Copy
   backups only through approved secure paths outside Git; never CI artifacts.
3. Restore only into the verified **empty new target**. PostgreSQL `pg_restore
   --exit-on-error --no-owner --no-acl` as the target app owner avoids importing
   old superuser ownership/privileges. Retain production secrets securely. Do not
   restore over a running live database or use `--clean` casually.
4. Run migrations/security/health checks, compare aggregate counts, validate
   visits/tasks/timing/invoices/feedback and role isolation. Change DNS/proxy
   routing after approval, re-enable writes on only the new server, then enable
   its runner and backup timers. Keep the old server read-only for a rollback
   window; preserve backups before decommissioning it.

No workbook is needed for daily operations or restoring historical work. Import
master-data changes deliberately; existing visit task snapshots must remain
unchanged. Never put workbooks, DB dumps, customers or `.env` into Git.

## Validation boundary

Local automated tests exercise PostgreSQL, production settings, proxy redirects,
secure cookies, backup failure ordering and rehearsal isolation. Real remote
upload, VPS certificates, runner registration, GitHub approvals and disaster
cutover require the actual company infrastructure and credentials; do not treat
them as completed just because scripts are present.
