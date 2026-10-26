# Google Review

Open **Customer experience → Google Review**. Managers see all Google reviews for
their current working branch, including dated cover/leave rules. General Managers
and Admins see every branch. Employee permissions are unchanged.

The three tabs are **All reviews**, **Open reviews** and **Closed reviews**.
General Managers and Admins can combine branch, star rating, start date, end date
and search filters. Search matches reviewer name, review text or Google review ID.
Tab links and pagination retain the selected filters.

Each card shows reviewer name, rating, complete review text, branch, Google
creation date and Google review ID. Anonymous, missing or blank names display
**Anonymous reviewer**. Older payloads may omit reviewer metadata. The app never
creates or links a customer from reviewer data; deduplication still uses the full
Google review resource name within its environment.

Open a review to see only:

1. The customer review and its details.
2. The system reply. A prepared reply is clearly marked when publication is not
   confirmed; an internal status change never posts or approves a Google reply.
3. One form: **Customer follow-up status**, **Staff follow-up status** and
   **Justification**, followed by **Save review**.

Customer status is Not contacted / Awaiting reply / Contacted. Staff status is
Not started / In progress / Awaiting follow-up / Resolved / Unresolved /
Not actionable. Justification is required. Resolved, Unresolved and Not actionable
move a review into Closed. The other staff statuses keep it Open. Change the
staff status back to In progress and explain why to reopen a review using the same
form. Contacting a customer does not automatically close the review or establish
customer satisfaction. Unresolved remains a separate outcome from Resolved.

The same form works for appreciations, complaints, mixed feedback and rating-only
reviews. There is no separate Follow-up tab, assignment step, acceptance step,
per-issue form or extra close button in this workflow. The bell opens the same
simple review page. Submission time, actor and audit history are recorded by the
server, and stale edits are rejected rather than overwriting another person's work.
Existing sensitive-review closure policy remains enforced on the server.

Migration **0023_simple_google_review_updates** adds review-level status and
justification fields without deleting earlier cases, issues, notes or history.
Older notes populate the form until a review-level update is saved. The old
issue records remain historical; the daily form does not rewrite their notes.
New Google review versions or newly reported concerns continue to preserve staff
justifications and can reopen work under the existing ingestion rules.

Legacy reporting and integration routes remain available under their existing
Admin permissions, but are not part of the daily review navigation. Sandbox data
remains Admin-only, separate from production reviews and manager alerts. In
Codespaces an Admin can use `/reviews/google/?demo=1` for test reviews.

See [Google integration](google-review-integration.md) for endpoint authentication,
receipt examples, migrations and retry/version rules.
