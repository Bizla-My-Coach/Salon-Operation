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

Open a review to see:

1. The reviewer details, followed by the customer review and system reply in two
   collapsible cards. Both start collapsed; on narrow screens the cards stack.
   A prepared reply is clearly marked when publication is not
   confirmed; an internal status change never posts or approves a Google reply.
2. Always-visible **Issues** and **Appreciation** sections from the latest saved
   Lambda analysis. Issues show the supplied category, description and priority.
   Previous issues remain in history but are not mixed into the latest findings.
3. One form: **Customer follow-up status**, **Staff follow-up status** and
   **Justification**, followed by **Save review** and **Close review**.

The current Lambda example supplies `analysis.issues` but no appreciation points.
In that case the page says **No separate appreciation points were supplied**.
It never guesses praise from a star rating, sentiment or the review text. An
optional `analysis.appreciations` array may contain strings or objects with
`reported_appreciation` and an optional `category`.

Customer status is Not contacted / Awaiting reply / Contacted. Staff status is
Not discussed / In progress / Awaiting follow-up / Resolved / Unresolved /
Not actionable. Both dropdowns are optional and default to **Not contacted** and
**Not discussed**. They do not control whether a review is open or closed.
Justification is required for **1–2 stars** and optional for **3–5 stars**, both
when saving and closing. A five-star review can be closed with its defaults and
no justification. **Close review** closes the review; **Reopen review** reopens it.
**Save review** updates the notes and statuses while keeping it open or closed.
Closing without a staff outcome records it as closed without claiming the issue
was resolved. Explicit Unresolved remains distinct from Resolved. Existing
sensitive-review closure permissions still apply.

The same form works for appreciations, complaints, mixed feedback and rating-only
reviews. There is no separate Follow-up tab, assignment step, acceptance step,
per-issue form in this workflow. The bell opens the same
simple review page. Submission time, actor and audit history are recorded by the
server, and stale edits are rejected rather than overwriting another person's work.
Existing sensitive-review closure policy remains enforced on the server.

Migration **0023_simple_google_review_updates** adds review-level status and
justification fields without deleting earlier cases, issues, notes or history.
Migration **0026_google_review_optional_followup** renames the default staff label
to Not discussed while preserving existing status values and saved follow-ups.
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
