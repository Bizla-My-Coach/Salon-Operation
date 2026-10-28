# Salon Operation project guidance

This repository is the Django application for salon SOP execution and timing analysis.

## Runtime and layout

- `salonops/` contains Django project configuration.
- `operations/` contains the domain models, workflows, views, forms, admin, commands, and tests.
- `templates/` and `static/` contain the server-rendered interface.
- Run the supported environment with `docker compose up --build -d`.
- Run checks with `docker compose exec -T web python manage.py check` and tests with `docker compose exec -T web python manage.py test`.
- PostgreSQL is the authoritative runtime database. Do not add SQLite files to Git.

## Domain invariants

- A service expands to ordered sub-services and operational tasks.
- A combined visit can contain multiple ordered services.
- `order_number` controls service display order. Employees can run and submit assigned services independently, including multiple active tasks, after the visit opening checks; preserve task sequence rules within each service.
- Build exactly one sanitisation at the beginning and one Consultation at the Second of a combined visit.
- Inventory requirements may vary by service; equipment requirements belong to operational tasks.
- Never commit customer data, database dumps, workbooks, exports, `.env`, or credentials.
- Treat `data/` workbooks as external import sources and use `import_sop_workbook` to update master data.

## Completion checks

- Generate and commit migrations for model changes.
- Run Django checks and tests inside Docker.
- Verify `/health/` and any changed role-specific page.
- Preserve branch isolation and role authorization in all new views.
