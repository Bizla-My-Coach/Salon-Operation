# SalonOps — Django Salon SOP & Service Workflow

A responsive Django application for salon operations with separate admin, manager, employee, and customer-feedback experiences.

## Included

- Multi-branch master data and date-based branch-isolated manager/employee access
- General-manager accounts and a branch duty roster for temporary cover and leave
- Admin CRUD through Django Admin
- CSV insert/update import for branches, chairs, services, and SOP tasks
- Admin creation of managers and employees by branch
- Manager visit creation, service assignment, chair allocation, live progress, verification, invoicing, and feedback handoff
- Employee mobile workflow showing one current task with large Start, Complete, and Skip controls
- Flexible SOP phases: Before, During, Finishing, and After service
- SOP task categories for consultation, service steps, towel/laundry, cleaning/sanitization, quality checks, and recommendations
- Required, optional, and skippable tasks with skip reasons
- Five-question emoji feedback form with optional suggestion
- PostgreSQL-only database, Docker deployment and encrypted off-server backup tooling

## Quick start

```bash
cp .env.example .env
# Set your local SECRET_KEY and PostgreSQL credentials in .env first.
docker compose up --build -d
docker compose exec -T web python manage.py createsuperuser
```

Run `seed_demo` only when setting up development demo data. Do not run it during
normal application startup or production deployments because it resets the demo
accounts and demo SOP configuration.

Open `http://127.0.0.1:8000/`.

## Optional development demo accounts

For one-command testing on the **dev** branch in GitHub Codespaces, see the
[Codespaces setup](docs/codespaces.md). Run `bash scripts/codespaces.sh` inside
that dev container, then sign in with **admin / admin**. It uses an isolated
PostgreSQL database and fictional data.

On a disposable development database only, run
`docker compose exec -T web python manage.py seed_demo` to create these accounts.

All demo passwords are `Admin@123`.

| Role | Username |
|---|---|
| Admin | `admin` |
| Manager | `manager` |
| Employee | `stylist` |

Change all passwords before real use.

## Main URLs

- `/` — role-aware dashboard
- `/admin/` — full Django CRUD administration
- `/admin-panel/` — simplified admin landing page
- `/manager/` — manager live operations
- `/employee/` — employee work list
- `/admin-panel/roster/` — branch duty roster (admin and general manager)
- `/general-manager/` — general-management overview

## Branch cover and leave

Create a separate account for each person; do not share a branch-manager login.
An admin can create a `GENERAL_MANAGER` account from **Add team member**. The
general manager can view business-wide visits and reports, maintain the roster,
and use manager operations only for the branch where they have a working duty
for the current local calendar day. They do not receive Django admin or user
creation rights.

Use **Branch roster** to record a working branch or leave for one person for up
to 31 consecutive whole days. A dated duty overrides the person's home branch.
Without a duty, managers and employees remain at their home branch; general
managers have no acting branch. A person cannot be assigned to two branches on
the same day. Reassign an employee's open services before moving them or
recording leave for today. Existing visit and task history remains at its
original branch and retains the individual actor's identity.

## CSV import

Use **Admin panel → CSV master-data import**. Import in this order:

1. `branches.csv`
2. `chairs.csv`
3. `services.csv`
4. `sop_tasks.csv`

Sample files are in `sample_csv/`. Imports use update-or-create behaviour, so matching codes/sequences are updated rather than duplicated.

### Accepted SOP values

- `phase`: `BEFORE`, `DURING`, `FINISHING`, `AFTER`
- `task_type`: `SERVICE`, `CONSULT`, `HYGIENE`, `TOWEL`, `QUALITY`, `RECOMMEND`
- Boolean values: `true/false`, `yes/no`, or `1/0`

## Employee-effort design

The employee sees only their assigned jobs. Inside a job, the page shows one current SOP task and large action buttons. Notes are optional except when a configured skipped task requires a reason. The full checklist is collapsed by default.

## Production deployment

Invoice entry accepts the numeric suffix only: entering `977` saves
`INV/26-27/977`. A number is reserved within its branch across all invoice
statuses; another branch may use the same number. Leading zeroes are normalized.
Migration `0012_invoice_branch_number` assigns existing invoices to their visit's
branch and preserves their saved invoice numbers and amounts.

Follow [the deployment and recovery guide](docs/production-deployment.md) for
`dev` / `main` branches, GitHub Actions runners, dedicated Docker PostgreSQL,
HTTPS via the company proxy or optional Caddy, encrypted off-server backups and
isolated restore rehearsals. Automatic VPS deployment is disabled until enabled
explicitly after company setup. Never use the local Compose file for production.

## Useful commands

```bash
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py makemigrations --check --dry-run
docker compose exec -T web python manage.py test
node --test tests/*.test.cjs
```

## Docker and PostgreSQL

Create a local `.env` from `.env.example`, then run:

```powershell
docker compose up --build -d
docker compose ps
```

Open <http://localhost:8000/>. The database-aware health endpoint is
<http://localhost:8000/health/>.

The web container runs migrations and collects static files at startup. PostgreSQL
data is kept in the named `postgres_data` volume. `docker compose down` preserves
it; `docker compose down -v` permanently removes it.

## SOP workbook import

For remote servers without SSH access, administrators can [upload, validate and
confirm a workbook in the website](docs/workbook-web-import.md) through **Import
SOP workbook**. Deploying code never automatically imports a workbook.

Business workbooks stay in the ignored `data/` directory and are never copied
into the Docker image. To import or update the master data:

```powershell
docker compose exec -T web python manage.py import_sop_workbook
```

The import uses update-or-create behaviour for services, sub-services, tasks,
inventory, equipment, and their mappings. Re-importing an updated workbook does
not duplicate matching codes.

## Combined service orders

Managers can choose multiple services and arrange their execution order. The
employee sees that order and cannot start a later service before the earlier one
is complete. The generated execution plan always starts with one sanitisation,
then one consultation, followed by procedures in the manager-defined service
order. Before work starts, the manager can add services at the beginning or end,
reorder them, or change the employee and chair assignment.

Employees can run services from different visits at the same time and use
**Back to my services** to switch visits without stopping task timers. Each
staff card shows the customer, service, visit number, and service order. Later
services in the same visit stay locked until earlier services are submitted;
only one hands-on task per visit can run at a time. Processing-enabled tasks
still support separate hands-on and waiting intervals.

Admins, general managers, and managers can open the read-only **Service catalogue** from the application sidebar, choose a
service, and inspect its sub-services, tasks, inventory quantities, active/passive
time, equipment time, and utility time.

## Workflow safeguards and measured timing

Confirming a visit opening check locks that service's assignment and position;
upcoming services can still be edited after the locked prefix. A permitted skip,
with a reason when required, counts as resolved during submission and verification.
Verification cannot reopen invoiced, closed or cancelled visits.

An employee may finish their own carried-over assignments from an expired cover
duty when the assignment date and branch match that recorded duty. Previously
assigned home-branch work also remains accessible when starting a cover duty. This does not
grant access to other branch records, and an explicit leave day still blocks work.

Managers see total task time, hands-on working time and waiting time on the live
overview, review and detail pages. Total task time sums working and waiting
intervals; it is not visit wall-clock time. Staff see workflow controls and progress,
but no measured time or stopwatch. Admin/general-manager detail pages are read-only
application pages and do not require Django Admin access; the native management
link appears only for accounts with Django staff/module permissions.

The manager live overview uses compact cards: customer and status, a short
Total / Working / Waiting strip, ordered service/staff/chair rows, and the next
action in the header. There is no visit-details button or repeated action footer.
Cancelled assignments are collapsed, and Edit is shown only for upcoming work
that can still be changed.

## Personal account and appearance

Managers, Admin and General Manager have **Customer experience → Tickets**.
All Google feedback is stored, including appreciations that can be closed without
an investigation. Complaints and human follow-up use branch-scoped cases with
separate issue tasks, automatic submission timestamps and audited outcomes.
AWS ingestion and publication-status endpoints receive the proposed v1 worker
envelope; AWS owns Google publication independently of the app and managers.
Branch analytics, unassigned work and sandbox separation are included. See the
[Google review integration and deployment guide](docs/google-review-integration.md)
for API URLs, authentication, examples, migrations and Codespaces verification.
The [ticket guide](docs/customer-review-tickets.md) describes staff handling.

Every signed-in role has a profile icon in the navigation bar. Open it to see
the account name, open **Profile**, switch between light and dark mode, or sign
out. Light is the default; an explicitly chosen theme is remembered per account
in that browser. Sign out is available in the profile menu, not the sidebar. The profile
page edits first/last name, email and mobile; roles and branch assignments remain
administrator-controlled. **Change password** opens a dedicated page directly
from the profile icon on desktop and mobile. Password changes require the current
password and keep the current session signed in.

Managers have an **All staff** sidebar link for their current branch's employees,
including home-branch staff on leave or working elsewhere and today's visiting
cover staff. General managers can use it while rostered as acting branch manager.

Admin and General Manager can onboard staff through **All staff → Upload staff
CSV**, including experience and salary. Validate and review before confirming;
each new account gets an individual random 8-character password (uppercase,
lowercase, numbers and symbols) in a one-time download. See the
[staff CSV guide](docs/staff-csv-import.md) for the format and safeguards.

The **Import data** sidebar section brings branch/chair CSV, staff CSV and SOP
workbook imports together. See the [branch import guide](docs/branch-csv-import.md)
for the downloadable CSV format, preview/confirmation steps and update rules.

The account-menu JavaScript unit tests can also be run with
`node --test tests/accounts.test.cjs`.

## Git workflow

For a test VPS without a domain or S3, see the optional
[private dev deployment guide](docs/private-test-deployment.md). It uses local
PostgreSQL backups and SSH-tunnel access only; production protections are unchanged.
For `operations.shahinanalytics.com`, use the
[test HTTPS/Caddy and local-backup guide](docs/test-https-deployment.md) instead.

The canonical remote is `https://github.com/Bizla-My-Coach/Salon-Operation.git`.
The two permanent branches are `dev` (your test server) and `main` (company
production and GitHub's default branch). Promote tested changes from `dev` into
`main` through a reviewed pull request. Optional temporary feature branches can
be reviewed into `dev` and removed after merging:

```powershell
git switch -c feature/short-description
# Stage reviewed source files explicitly; never stage data or credentials.
git add path/to/changed-source
git commit -m "Describe the change"
git push -u origin feature/short-description
```

Do not commit `.env`, workbooks, customer data, exports, backups, SQLite files,
or database dumps. Branches contain code only; test and production databases
remain completely separate.
