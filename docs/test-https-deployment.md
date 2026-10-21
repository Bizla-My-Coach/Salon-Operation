# Test VPS: Caddy HTTPS and local PostgreSQL backups

This is the **dev-only** `https-local` mode for
**https://operations.shahinanalytics.com**, currently pointed at test Elastic IP
`13.126.12.164`. Production remains on `main` and uses its separate, mandatory
off-server backup workflow. Do not use real customer/company data on this test VPS.

## What runs on the test server

Standalone `compose.test.yaml` starts three containers:

- `web`: tested Django image, `DEBUG=False`, HTTPS redirects and secure cookies.
- `db`: PostgreSQL 17 with restricted app user; no public database port.
- `proxy`: Caddy on ports 80/443, redirects HTTP, obtains and renews the origin
  certificate, forwards to `web:8000` on the private Compose network.

The Caddy code is checked in at `deploy/Caddyfile.test`:

```caddyfile
{
    admin off
}
operations.shahinanalytics.com {
    reverse_proxy web:8000 {
        header_up X-Forwarded-Proto {scheme}
    }
}
```

No host-level `apt install caddy` is needed. Caddy runs in Docker. Its `/data`
and `/config` volumes persist across image/container replacement; never delete
them to redeploy, as that can cause certificate issuance/rate-limit problems.
See [Caddy's automatic HTTPS requirements](https://caddyserver.com/docs/automatic-https).

## 1. One-time Ubuntu preparation

Run as the existing runner account, **ubuntu**:

```bash
sudo install -d -o ubuntu -g ubuntu -m 700 /etc/salonops
sudo install -d -o ubuntu -g ubuntu -m 750 /opt/salonops-dev
sudo install -d -o ubuntu -g ubuntu -m 700 /var/lib/salonops-dev/backups
sudo systemctl enable --now docker
cd ~/actions-runner
sudo ./svc.sh start
sudo ./svc.sh status
docker version
docker compose version
python3 --version
curl --version
stat -c '%a %U %n' /etc/salonops/dev.env
```

Expected env permissions: `600 ubuntu /etc/salonops/dev.env`. If you already
generated this file, **keep the same secrets**. Both private preview and HTTPS
use exactly the three existing keys: `SECRET_KEY`, `POSTGRES_PASSWORD`,
`POSTGRES_ADMIN_PASSWORD`. No domain or backup-storage credentials need to be
added to that file for this fixed test stack. Do not upload it to GitHub.

The fixed test DB name, app role and volume remain `salonops_dev`,
`salonops_dev_app` and **`salonops-dev-pgdata`**. Switching from private preview
to HTTPS does not import, replace or erase records. It does not rename the volume.
If the volume is already initialized, changing passwords in env will NOT reset
PostgreSQL role passwords; investigate instead of deleting the volume.

In AWS, allow inbound HTTP 80 and HTTPS 443; keep 8000/5432 private. The user has
temporarily retained SSH 22 from `0.0.0.0/0`: keep key-only authentication and
plan to restrict it or use Session Manager later. If Ubuntu's firewall is active,
allow 80/443 there too; do not enable/reset a firewall in a way that locks out SSH.
Ensure no existing host service is occupying ports 80/443.

For initial setup, Cloudflare's `operations` A record should have Content
`13.126.12.164`, temporarily **DNS only**. Remove/correct any stale AAAA record
for this same test hostname if the VPS does not support that IPv6 address.
DNS-only temporarily exposes the origin IP and bypasses Cloudflare's proxy.
Do not change production's DNS or disable SSL.

## 2. Runner label and private-registry access

GitHub **Settings → Actions → Runners**: this server needs the custom label
**`salonops-test`**, alongside `self-hosted`, `Linux`, `X64`. Never assign the
production label to this server. Installing a runner alone does not deploy apps.

As `ubuntu` on the VPS (not root):

```bash
docker login ghcr.io --username YOUR_GITHUB_USERNAME
```

At the password prompt enter a GitHub **classic PAT with `read:packages`**, for
an account with access to this private GHCR package. Authorize organization SSO
if required. Do not paste the token into a command argument or chat. The runner
and subsequent image pulls must run as the same account that logged in.
See [GitHub container-registry authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## 3. GitHub variables and first deployment

Repository **Settings → Secrets and variables → Actions → Variables**:

| Name | Ready-to-deploy value |
| --- | --- |
| `SALONOPS_TEST_DEPLOY_MODE` | `https-local` |
| `SALONOPS_TEST_AUTODEPLOY` | `true`, ONLY after preparation above |
| `SALONOPS_PROD_AUTODEPLOY` | **`false`** |

These are repository variables, not Secrets or server env entries. The `testing`
environment must allow deployment branch `dev`. `SALONOPS_USE_CADDY` is not used
by `https-local`; Caddy is already in its standalone Compose file.

A subsequent **dev push** runs hosted checks, publishes the tested immutable
image and sends deployment to `salonops-test`. The job copies reviewed Compose,
Caddy and scripts into `/opt/salonops-dev`; no manual repository clone is needed.

To deliberately trigger the first deployment from local Windows PowerShell:

```powershell
git switch dev
git pull --ff-only origin dev
git commit --allow-empty -m "Trigger test HTTPS deployment"
git push origin dev
```

Do not push this trigger to `main`. Main targets the company runner and remains
disabled until its separate setup is complete. Watch the run in **GitHub →
Actions → SalonOps checks and deployment**.

The deploy script checks fixed test-only resources, image digest and DB role,
validates a local dump **before migration**, stops old web/proxy for schema
changes, migrates, starts all services, checks secure settings and validates the
origin HTTPS certificate/login. It retries certificate readiness for several
minutes without disabling TLS verification. A second local backup captures the
post-migration schema. No schema rollback/data restore is automatic.

## 4. Verify and create the website login

After the deployment job succeeds, on Ubuntu:

```bash
curl -I https://operations.shahinanalytics.com/login/
curl -I https://operations.shahinanalytics.com/health/
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Expect HTTP 200 and containers `salonops-dev-web-1`, `salonops-dev-db-1`,
`salonops-dev-proxy-1`. The web backend is loopback-only; proxy publishes 80/443;
PostgreSQL has no published host port. Test the browser at
**https://operations.shahinanalytics.com/login/**, without an SSH tunnel.

Create the first website account interactively; the database passwords are not
website login passwords:

```bash
SALONOPS_IMAGE=$(cat /var/lib/salonops-dev/backups/salonops-dev.last-successful-image) \
docker compose --env-file /etc/salonops/dev.env -p salonops-dev \
  -f /opt/salonops-dev/compose.test.yaml exec web python manage.py createsuperuser
```

After origin HTTPS works, you may re-enable Cloudflare's orange cloud for this
test hostname and use **Full (strict)** encryption, never Flexible. Do not enable
"Cache Everything" or other rules caching authenticated HTML for this app.
Changing a zone-wide SSL mode affects other sites in that zone: check them first
or use a hostname-specific configuration rule. See
[Cloudflare Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/).

## 5. Local backup and restore rehearsal commands

The script is named `backup_preview.sh` for compatibility, but accepts both
reviewed DEV-only Compose files. It never accepts production project names.

Manual backup on Ubuntu, **after the first successful deployment**:

```bash
bash /opt/salonops-dev/scripts/backup_preview.sh \
  /etc/salonops/dev.env salonops-dev /opt/salonops-dev/compose.test.yaml \
  /var/lib/salonops-dev/backups
```

List completed dumps:

```bash
find /var/lib/salonops-dev/backups/salonops-dev -name database.dump
```

Each timestamp directory contains `database.dump`, `database.dump.sha256` and
non-secret release metadata. Nothing is committed or uploaded as a CI artifact.
All backups are retained, so monitor disk space. Deployment and manual backups
are implemented; hourly schedules are not enabled automatically.

Pick a completed **post-migration** dump for an isolated restore rehearsal:

```bash
bash /opt/salonops-dev/scripts/restore_rehearsal.sh \
  /var/lib/salonops-dev/backups/salonops-dev/CHOSEN_TIMESTAMP/database.dump
```

This validates the checksum, restores into a temporary PostgreSQL container on
`--network none`, checks schema and removes only that temporary container. It
does **not** overwrite the live test DB. Preserve pre-migration backups even if
TLS setup fails. A first-install pre-migration archive has no application tables;
use the post-migration archive for schema rehearsal.

Local backups do **not** protect against VPS/disk loss. Keep verified copies on
encrypted storage off the server. DB dumps do not contain your env credentials
or Caddy private keys; protect the env separately in a password manager/encrypted
backup. Never delete volumes, run `down -v`, or prune volumes on the VPS.

## Troubleshooting

- Queued job: check runner service and `salonops-test` label.
- Skipped deploy: correct `dev` push and test opt-in exactly `true`?
- GHCR denied: account/package access, PAT scope/SSO and login as `ubuntu`.
- Port in use: inspect `sudo ss -ltnp`; do not kill unrelated services blindly.
- TLS not ready: DNS/AAAA, inbound 80/443, host firewall and Caddy logs:
  `docker logs --tail 100 salonops-dev-proxy-1`. Never hide this with `curl -k`.
- Failed migration: preserve DB/backup volumes and investigate; app may remain
  stopped, and there is no destructive automatic rollback.

## Google review receiver

The reviewed V3 release adds `POST /api/integrations/google-reviews/results`
with `X-Integration-Key` authentication. Install
`REVIEW_INTEGRATION_KEY_SANDBOX` privately in `/etc/salonops/dev.env`; the Compose
file passes it explicitly to web. Blank keys keep ingestion disabled. Run the
existing backup/migration deployment and verify a sandbox committed receipt before
enabling AWS delivery. See [the integration guide](google-review-integration.md)
for the exact contract, secret format, mapping and pending deployment status.
