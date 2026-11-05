# Download, edit and re-upload the SOP workbook without SSH

## Bulk changes: download, edit, upload

For bulk changes (add a step to every facial, add many services at once,
change prices) start from the live data instead of an old workbook:

1. On `/admin-panel/import/workbook/` choose **Download SOP workbook**. The file
   holds every master row as it is now, including steps, task order and skips
   changed in the service catalogue, plus `base_price`, `can_skip`, `active` and
   an extra **Service Task Order** sheet (per-service task order and task skips).
2. Edit it in Excel. Grey `(info)` columns (service name, category, step name)
   are only for filtering and are ignored on upload. Keep IDs of existing rows;
   give new rows new unique IDs. Set `active` to FALSE to switch a row off (a
   blank `active` cell counts as active). Steps run in `sequence` order, so 15
   goes between 10 and 20. The **Read me** sheet repeats these rules.
3. Upload it below. The preview lists every service that will change in plain
   words (added/removed/moved steps, price, minutes, skips) and how many rows
   per sheet are new, changed or removed. Nothing is saved until you confirm.
4. On confirm, each changed service gets a new `WORKBOOK` version in its SOP
   history, so any single service can still be restored.

The download carries a catalogue reference on its **Read me** sheet. If the
master data changed after the download (another admin edited the catalogue or
imported a workbook), the upload is refused; download again and redo the change.
Workbooks not downloaded from this page (no reference) still upload as before,
with a warning in the preview.

When the **Service Task Order** sheet is present it is the complete list: a row
removed there makes that service follow the shared task order again. Leave the
sheet out of a hand-built workbook to keep current task settings unchanged.
Older workbooks without `base_price` or `can_skip` columns keep current values.

## Import steps

An application administrator can use `/admin-panel/import/workbook/` on the
deployed site. The change is rolled out on `dev` first; company production must
receive the tested code/migration separately through its normal release process.

1. Sign in as an active **ADMIN** or superuser. Branch managers, general managers
   and employees cannot access this global master-data operation.
2. Select **Import SOP workbook** in the sidebar or Business overview > Setup.
3. Choose the approved `.xlsx` file from your computer; click **Validate workbook**.
4. Review the per-service changes and row counts. Validation uses the same
   `import_sop_workbook` command as the CLI, but rolls its transaction back.
5. Tick the approval checkbox and choose **Confirm import**. Uploading or deploying
   code alone never applies a workbook. A preview lasts one hour, belongs only to
   its uploader, and may be confirmed only once. If master data changes meanwhile,
   upload again and review a fresh preview.
6. Download the pre-import master-data snapshot from **Your recent imports** and
   store it securely outside the server if required. This is an administrative
   recovery fixture, **not a full PostgreSQL backup**. Do not commit it to Git or
   upload it to a public file store.

The import updates services, sub-services, tasks, inventory, equipment and their
mappings by stable workbook identifiers. Missing rows are not automatically
deleted. Staff/accounts, branches, chairs, customers, visits, invoices, feedback
and historical VisitTask snapshots are not imported or modified. Existing pending
visit plans also keep their snapshots; rebuilding an unstarted plan is a separate
manager action.

## Safety and limits

- Workbook limit: 10 MB compressed, 64 MB expanded, 1,000 ZIP entries, 10,000
  worksheet rows across the eight required sheets and the optional Service Task
  Order sheet (including blank rows).
- Only `.xlsx`; encrypted files/macros, missing sheets/columns, duplicate/blank
  identifiers, invalid/negative numbers and broken references are rejected.
- Active sanitisation (`SUB025`) and consultation (`SUB001`) tasks are required.
- Formula cells use the saved Excel cached value; the importer does not calculate
  formulas. Recalculate and save your approved workbook in Excel before upload.
- Auth/CSRF protection, private database staging, no public media directory, no
  source workbook included in images/Git/CI artifacts/logs.
- Parsing is bounded before Django's CSRF multipart parsing. The test Caddy proxy
  additionally limits request bodies to 11 MB. Set an equivalent body limit in
  another company's reverse proxy when releasing to production.
- Workbooks are cleared on confirmation/cancellation; expired pending uploads
  are cleared on the next use of this page. Audit metadata/snapshots remain in
  PostgreSQL and are covered by normal DB backups; they are not automatically
  pruned. Monitor storage and apply a company retention policy separately.
- Master tables are locked during preview/confirmation. A short lock timeout
  prevents indefinite waiting. Changes plus the compressed pre-import snapshot
  commit atomically; errors roll back all master changes. No automatic rollback
  touches later customer work.
- Gunicorn permits up to 180 seconds for bounded administrative requests. If an
  exceptionally large import times out, the DB transaction rolls back; revalidate
  before retrying. For workbooks beyond these limits, ask a server administrator
  to use the CLI during maintenance.

## Recovery is not automatic

The downloadable `.json.gz` snapshot contains only the master tables (the eight
workbook tables plus per-service task settings) as
they were immediately before import, with original primary keys. It does not
contain visit/customer/user data and cannot recover a lost server on its own.
An administrator should inspect/rehearse recovery on an isolated restored test
database; do not blindly run `loaddata` on a live server. It updates existing
snapshot IDs and does not remove newly imported rows. Later imports, dependencies
and newly created visits must be considered when planning a forward fix.

Keep the existing full PostgreSQL backups as well. The current test deployment
takes local full backups before/after deployments; it does **not** take a new
full `pg_dump` for each web import, and web workers deliberately have no Docker
socket or bootstrap DB password. The pre-import master snapshot replaces neither
those dumps nor production's encrypted off-server backup/restore procedures.
