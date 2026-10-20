# Customer review tickets

Open **Customer experience → Tickets**. All reviews stores Google appreciations,
complaints, mixed feedback and rating-only reviews. Appreciations have a simple
**Close ticket** action without an investigation. Investigations contains reported
issues and cases needing human follow-up, including low ratings without an
explanation; a rating alone does not invent a complaint.

Managers see their current working branch, including dated cover/leave rules.
Admin/GM see all branches, branch analytics and Needs assignment. Employees have
no access. Filters cover branch, handling state, sentiment and priority. Cards
remain compact with larger text and bold headings.

The daily investigation page uses **Accept review**, one card per reported issue,
and **Update follow-up**. Customer status is Not contacted / Awaiting reply /
Contacted. Staff status is Not started / In progress / Awaiting follow-up /
Resolved / Unresolved / Not actionable. Add a short action/service note.
Submission time and actor are saved automatically. Customer contact does not imply
confirmed satisfaction. Existing notes, outcomes, confirmations and staff priority
choices survive AWS edits; health issues always remain urgent.

Every issue needs an explicit outcome and action note before **Close ticket**.
The notes become the closure/service record without asking staff to retype them.
Unresolved and partial outcomes remain visibly Unresolved and are excluded from
successful resolution counts/timing. Closed work can be reopened with a reason.
Stale edits return 409. Low-rating cases with zero issues use **Record clarification
outcome** instead of empty issue cards. Company sensitive-closure policy still
applies to internal work. Admin/GM can expand Assign manager when needed.

The bell beside the profile menu works on desktop and mobile. It polls while the
app is visible and on focus/open, showing the recipient's accessible assigned
follow-up cases. Reading an alert does not accept or close the review. Read state
persists; reassignment and branch scope are rechecked. Appreciations create no
complaint alert. These are in-app alerts; external SMS/email/push are not connected.

AWS-linked tickets show public reply status separately. The app does not publish
or approve Google replies. Publication can succeed while an investigation stays
open, and app downtime or missing assignments must not delay AWS publication.
Legacy staff-recorded acknowledgement/draft records remain historical internal
records, without a Google publisher. They do not control the new integration.

See [Google integration](google-review-integration.md) for exact endpoints, secrets,
request/receipt examples, migrations, retry/version rules and deployment steps.
Sandbox data is isolated with `?demo=1` and excluded from live analytics and alerts.
Codespaces seeds the sandbox examples automatically. An isolated development
`*_review_preview` database can run `seed_review_demo` followed by
`seed_google_review_demo`; other environments are refused. Demo progress survives
repeated seeding. No seed command publishes to Google.
