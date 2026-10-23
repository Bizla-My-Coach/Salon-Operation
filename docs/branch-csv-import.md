# Import data

Admin uses **Import data** in the sidebar for all three workflows:

1. **Branches and chairs** — CSV, administrator-only.
2. **Staff CSV** — Admin or General Manager; creates individual login accounts.
3. **Excel SOP workbook** — administrator-only; updates SOP master data.

General Managers see the same hub, but cannot open branch or SOP imports. Branch
managers and employees cannot access imports. Existing staff-directory upload
shortcuts still work. The old direct CSV import URL redirects to this hub; direct
POSTs there are disabled so no upload can bypass preview and confirmation.

## Branch and chair CSV

Download the template in **Import data → Import branches and chairs**. Replace
the example rows with your own information and export **CSV UTF-8** from Excel:

`BRANCH_CODE,BRANCH_NAME,LOCATION,PHONE,CHAIR_CODE,CHAIR_NAME,STORE_CODE`

- Use one row per chair and repeat identical branch details on every row.
- To create a branch without chairs, leave both chair fields blank.
- Branch code and name are required. Location is stored in the existing branch
  address field. Location and phone are optional.
- `STORE_CODE` is optional, up to 120 characters, and matches the Google Business
  Profile store code exactly (case-sensitive), for example `HS-001`. Each nonblank
  store code belongs to one branch. Repeat it on each chair row. Existing six-column
  uploads still work; omitted or blank store codes preserve the current value.
  Store codes identify branches for incoming reviews and do not replace branch codes.
- Codes contain 1–20 letters/numbers, underscores or hyphens. Keep branch codes
  permanent; reusing the same code updates that branch instead of creating a new
  one. A chair code identifies a chair **within its branch**.
- Branch names have a 120-character limit; chair names have an 80-character limit.
- Phone accepts 7–15 digits with an optional `+` prefix. Export phones as text.
- Blank/null location or phone preserves existing values. No blank-field clearing,
  deletions, moves or reactivation are performed by an import. An administrator
  must review inactive records separately.

**Validate CSV → Review the Create/Update/Unchanged preview → Enter your current
password and confirm.** Validation changes no branches or chairs. Any invalid row
rejects the entire upload. Confirmation is transactional: all changes succeed,
or all are rolled back. The preview expires after 30 minutes and is private to
the administrator who uploaded it. Staged rows are cleared when used/discarded
or when expired previews are cleaned on a subsequent importer visit.

Maximum size: 500 rows / 512 KB. Comma and semicolon exports are supported. If
branch/chair data changes after preview, validate again rather than applying a
stale plan. Reimporting the same data is safe and produces Unchanged rows.

Employee home branches, rosters, visit/service IDs and task snapshots are not
rewritten. Renamed master records may show their updated labels in existing
screens. Keep all real import files outside Git.
