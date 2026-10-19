# Bulk staff onboarding

Admin or General Manager: **All staff → Upload staff CSV**.
The same uploader is also available through **Import data → Staff CSV**. Use
**Import data → Branches and chairs** first if your branches do not exist yet.

1. Download the template, or save your staff sheet as **CSV UTF-8** in Excel.
2. Use columns `BRANCH, NAME, PH.NO, SECTION, EXPERIENCE, SALARY`.
   - Branch must match an existing active branch name or code. Create branches first.
   - Name and section are required. Phone, experience and salary can be blank or `null`.
   - Salary is a nonnegative amount with at most two decimal places. Do not include formulas.
   - Format phones as text to avoid scientific notation.
3. **Validate CSV**, then review every branch, role, username and pay detail.
   A section beginning with `MANAGER` (including `MANAGER/UNISEX HAIR STYLIST`)
   creates a manager. Other sections create employees, never Admin/General Manager.
4. Enter your own current password, tick the confirmation and create the accounts.
   Save the **one-time credentials CSV** securely. Give each person only their own
   username and individual random 8-character password (uppercase, lowercase,
   numbers and symbols); ask them to open **Change password** from the profile icon.
   Keep the page open until the download starts; secure password hashing takes
   longer for large batches. A company HTTPS proxy should allow up to 180 seconds
   for the confirmation response (Nginx: `proxy_read_timeout 180s;`).

Salary is visible only to Admin/General Manager. Experience appears in staff
directories. Uploads are limited to 200 rows / 512 KB; previews expire after 30
minutes. Confirming, discarding or expiring a preview clears its staged rows.

This is **create-only**: duplicate names within the same home branch, existing
accounts and previously imported files are rejected. All rows must pass before
anything is created. Existing passwords, rosters, SOPs and visits are unchanged.
Manager accounts still follow the normal roster rules.

Plaintext passwords are not retained on the server. If the download is lost, use
employee password recovery in All staff; manager passwords need administrator
recovery. Do not commit either upload files or credentials to Git.

The credentials CSV quotes fields and guards formula-leading display values for
Excel. Treat CSVs as sensitive data, not trusted executable content; spreadsheet
applications differ in their handling of CSV formula injection.
