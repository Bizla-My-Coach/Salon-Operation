# Legacy private test VPS: no domain or S3

Routine testing now runs in Codespaces and GitHub Actions. Actions runs the test
suite on pull requests and pushes targeting `dev` or `main`; it does not deploy
to this VPS. The remaining instructions are for operators maintaining an
existing private test server and are not required for normal development.

If using the new public test domain, follow
[Caddy HTTPS with local backups](test-https-deployment.md) instead. The old
`local-preview` mode remains SSH-only; `https-local` is the explicit new option.

This is an **optional dev-only preview**, not public production hosting.
`main`, its HTTPS/secure-cookie requirements and mandatory encrypted off-server
backup workflow are unchanged. Use synthetic test records, not company/customer
data. Local backups survive container replacement but **not loss of the VPS or
its disk**. Copy important backups to another machine until off-site storage is ready.

## 1. Your existing runner

You already installed the Ubuntu runner service and Docker. No second runner or
manual repository clone is needed. In GitHub **Settings → Actions → Runners**,
select this test runner and add the custom label **`salonops-test`**. It must also
have the normal `self-hosted`, `Linux`, `X64` labels. Never label it
`salonops-production`.

On Ubuntu, as `ubuntu`:

```bash
cd ~/actions-runner
sudo ./svc.sh status
docker version
docker compose version
sudo install -d -o ubuntu -g ubuntu -m 700 /etc/salonops
sudo install -d -o ubuntu -g ubuntu -m 750 /opt/salonops-dev
sudo install -d -o ubuntu -g ubuntu -m 700 /var/lib/salonops-dev/backups
```

This path needs Bash, coreutils, `flock` and host `python3` (normally present on
Ubuntu). Keep Docker current; Docker releases before 28 have a localhost-publish
security caveat. Do not enable custom Docker direct-routing/routed networks.
Restrict inbound SSH to your IP in the VPS security group/firewall. **Do not open
8000 or 5432 publicly** and do not add a public reverse proxy for this preview.
See [Docker port-publishing guidance](https://docs.docker.com/engine/network/port-publishing/).

## 2. Generate the private configuration once

Run this on **Ubuntu**, not local Windows. It creates fresh secrets without
printing them. It refuses to overwrite an existing file. If the file already
exists, investigate first; changing DB passwords in `.env` does not change a
role password in an already-initialized PostgreSQL volume.

```bash
python3 - <<'PY'
import os
import secrets

path = '/etc/salonops/dev.env'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as config:
    config.write('SECRET_KEY=' + secrets.token_hex(64) + '\n')
    config.write('POSTGRES_PASSWORD=' + secrets.token_hex(32) + '\n')
    config.write('POSTGRES_ADMIN_PASSWORD=' + secrets.token_hex(32) + '\n')
print('Created /etc/salonops/dev.env; secret values were not printed.')
PY
stat -c '%a %U %n' /etc/salonops/dev.env
```

Expected permissions: `600 ubuntu /etc/salonops/dev.env`. **Do not paste the file
contents into chat, Git, Actions variables, logs or screenshots.** The example
`deploy/preview.env.example` documents the three secrets, but all runtime settings
are forced in standalone `compose.preview.yaml`: `DEBUG=False`, localhost hosts,
private HTTP, no proxy-header trust, restricted DB user, and no public database
port. Bootstrap-admin credentials are never passed to the web container.

The fixed Compose project is `salonops-dev`, DB `salonops_dev`, app role
`salonops_dev_app`, persistent volume **`salonops-dev-pgdata`**. Never reuse an
existing production volume or rename this volume after first deployment.
The HTTP cookies deliberately cannot use the browser's `Secure` flag for
`http://localhost`; traffic between your computer and the VPS is encrypted by
SSH. This must never be exposed directly through the Elastic IP.

## 3. Allow the runner to pull the private image

Create a GitHub personal access token **classic**, with **`read:packages`**,
for an account that can read this repository's GHCR package. Authorize company
SSO if required. On Ubuntu as the runner account (`ubuntu`), **without sudo**:

```bash
docker login ghcr.io --username YOUR_GITHUB_USERNAME
```

At the password prompt enter the token, not your GitHub password. Do not put it
in a command-line argument or send it to chat. The workflow supplies the tested
image's immutable digest, so no image tag/password needs to go in `dev.env`.
See [GitHub's container-registry authentication instructions](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## 4. GitHub Actions behavior

Actions runs checks on GitHub-hosted runners for pull requests and pushes
targeting `dev` and `main`. There is no test-server deployment job or
`SALONOPS_TEST_AUTODEPLOY` setting. Use Codespaces for interactive testing; the
test VPS instructions below are only for maintaining an existing server.

## 5. Open the site through an SSH tunnel

After a successful deployment, open **Windows PowerShell on your own computer**:

```powershell
ssh -i "C:\path\to\your-key.pem" -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8000:127.0.0.1:8000 ubuntu@YOUR_ELASTIC_IP
```

Replace the key path and IP. Keep this terminal running; then open
**http://127.0.0.1:8000/login/** in your browser. `localhost:8000` also works.
The tunnel's local bind is deliberately `127.0.0.1`, not `0.0.0.0`. If local port
8000 is busy, use `127.0.0.1:8001:127.0.0.1:8000` and browse port 8001.
Verify the server's SSH host-key fingerprint through a trusted source before
accepting it. Do not bypass SSH host-key checks. Phone access requires its own
secure tunnel/VPN; this mode does not provide a public mobile URL.

Create the first account interactively on **Ubuntu** (no password in logs):

```bash
SALONOPS_IMAGE=$(cat /var/lib/salonops-dev/backups/salonops-dev.last-successful-image) \
docker compose --env-file /etc/salonops/dev.env -p salonops-dev \
  -f /opt/salonops-dev/compose.preview.yaml exec web python manage.py createsuperuser
```

Use application administration to create test branches/staff and assign roles.
SOP workbook imports are separate from deployment; no real workbook or customer
database is uploaded automatically. No account password is stored in GitHub.

## 6. Local backups and restore practice

Every deployment validates its effective private Compose settings, checks the
restricted DB role, makes a PostgreSQL custom-format dump and validates it
**before migration**. It stops the old web app for schema changes and checks
migrations, `/health/` and `/login/` afterward. A second backup captures the
post-migration schema (important on the first, empty-database install).

Archives, SHA-256 checksums and release metadata stay in
`/var/lib/salonops-dev/backups/salonops-dev/<UTC timestamp>/`, outside the runner
checkout and Docker containers. No exports are uploaded to GitHub artifacts.
All local backups are retained: monitor disk use and deliberately remove old
ones only after retaining verified copies elsewhere. Deployment fails if the
disk is full or dump/archive validation fails. There is no automatic destructive
database rollback. If migration fails, the old web app may remain stopped;
investigate, don't delete the DB volume to fix it.

Manual backup after the first successful release, on Ubuntu:

```bash
bash /opt/salonops-dev/scripts/backup_preview.sh \
  /etc/salonops/dev.env salonops-dev /opt/salonops-dev/compose.preview.yaml \
  /var/lib/salonops-dev/backups
```

For an **isolated rehearsal**, choose a completed **post-migration** dump:

```bash
find /var/lib/salonops-dev/backups/salonops-dev -name database.dump
bash /opt/salonops-dev/scripts/restore_rehearsal.sh \
  /var/lib/salonops-dev/backups/salonops-dev/CHOSEN_TIMESTAMP/database.dump
```

It checks the checksum, restores into a temporary PostgreSQL container with
`--network none`, checks the application schema and removes **only that temporary
container**. It does not modify your live database. Restoring a live database
requires a separately reviewed downtime/cutover procedure.

Backups happen at deployment/manual execution, not every hour automatically.
If you add test data between releases, take a manual backup before important
tests or ask for a scheduled backup. To copy a completed dump and checksum to
your PC (Windows PowerShell, replace both placeholders):

```powershell
scp -i "C:\path\to\your-key.pem" ubuntu@YOUR_ELASTIC_IP:/var/lib/salonops-dev/backups/salonops-dev/CHOSEN_TIMESTAMP/database.dump* "$HOME\Downloads\"
```

Treat copies as confidential, store them on encrypted storage, and never inside
Git. Copying to another machine is a simple temporary alternative to S3, not a
managed encrypted off-site backup service with monitored retention.

## Troubleshooting

- Job skipped: verify the push was to `dev` and test opt-in is exactly `true`.
- Job queued: runner online and labeled `salonops-test`? Service using `ubuntu`?
- Registry denied: recheck package access, token `read:packages`, SSO and login
  as `ubuntu`, not root.
- Env permissions rejected: owner `ubuntu`, mode 600, outside Git.
- Browser cannot connect: successful deployment, SSH terminal still open, correct
  local port, security group permits your SSH source? Do **not** open port 8000.
- PostgreSQL auth failure: preserve the volume; investigate existing credentials.
- No disk space: safely archive verified old backups elsewhere; never run
  `docker compose down -v`, `docker volume prune` or `docker system prune --volumes`
  on this server.
