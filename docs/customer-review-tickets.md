# Customer review tickets — verification release

Open **Customer experience → Tickets** in the sidebar. Managers see only
their current working branch, including dated cover/leave rules. Admin and General
Manager can see all branches and reassign work to currently eligible branch managers.
Employees have no access. Opening a page does not acknowledge work.

The daily page is deliberately short: **Accept review**, one card per reported
issue, and **Update follow-up**. Managers' inboxes contain only reviews with issues.
Admin and General Manager see every review, including feedback with no reported
issue, and can combine branch, Open / Closed and priority filters. Priority uses
staff-confirmed values when available, otherwise the existing suggestion.

The inbox uses compact cards with a one-line review excerpt and up to two issue
categories; any additional issues are counted and remain fully visible on the
ticket. Larger text and bold headings are retained.

A notification bell appears beside the profile menu on desktop and mobile.
Assignment alerts are available in-app immediately after intake commits. The bell
checks for new alerts every 30 seconds while the app is visible, and on focus or
opening the bell. Only the recipient's currently assigned, accessible branch
reviews with issues appear; positive/no-issue reviews stay outside this queue.
Unread counts persist per manager. Opening an alert or choosing **Mark all as
read** records read time through a CSRF-protected POST, without accepting or
closing the review. Reassignment, current branch duty and demo/live isolation are
rechecked on every feed and read action. The dropdown shows up to 20 alerts, with
unread alerts first; its badge counts every unread alert. No browser permission is
required. These are in-app alerts; email, SMS and device push are not connected.

The update form contains customer follow-up (Not contacted / Awaiting reply /
Contacted), staff follow-up (Not started / In progress / Completed), and one short
action/service note. Customer contact does not imply confirmed satisfaction. Every
submission records server time and the actor automatically; there is no date input.
Existing outcomes, notes, priorities and confirmation history are retained.

Once all staff follow-ups are completed, **Close ticket** saves the issue notes as
the closure/service record without asking staff to type them again. The server
still blocks closure if any issue is open, preserves unresolved/not-actionable
outcomes from existing records, and checks optional company senior-closure policy.
Closed records can be reopened with a reason. Stale edits return 409.

The manager-facing page omits automatic acknowledgement, ownership-management and
substantive-response panels. Those backend records remain separate from operational
work; hiding a panel does not send or delete an external response. Follow-up history
is collapsed by default. Reviews without issues have no empty issue cards; HQ may
mark them reviewed with a short note, without counting them as solved complaints.

Automatic acknowledgements and substantive follow-ups are separate. A persisted
manager assignment prepares a fixed, generic acknowledgement, including for
sensitive complaints, without a manual approval wait. A missing manager holds the
intent and no referral text is prepared. Eligibility rechecks current owner and
source freshness. AI human-review provenance remains unchanged. The intent has a
stable ID and template/policy version; acknowledgement-sent time is separate from
manager-first-acknowledged time. Updating issue status never rewrites public text.

Assignment and an idempotent notification outbox record commit with intake;
delivery is not claimed. Synthetic entries are preview-only. Live pending entries
need a configured dispatcher before external delivery. In-app read status is
separate from external delivery status; no external notifications are sent.

Substantive public follow-ups are editable controlled drafts. Editing resets their
approval. Neither an acknowledgement nor a follow-up closes operational work.
No Google credentials, publisher, Lambda calls or machine ingestion are enabled.
The updated handoff is a reference specification, not a deployed API. The companion
event and actual integration repository were not attached. The intake page is for
staff-recorded feedback, not arbitrary Google event JSON. Preview intents are not
dispatched, and the browser cannot turn on publishing.

Reports show current case and issue counts, separate closure outcomes, current
owner workload and branch summaries. No historical backlog or rating-trend claims
are made. Synthetic records are excluded unless the explicitly labelled demo workspace
is selected. SLA deadlines are not invented; unset targets show “SLA not configured”.

## Local verification

Use an isolated development PostgreSQL database whose name ends in
`_review_preview`. Run migrations there and pipe a demo-only password to:

```sh
python manage.py seed_review_demo
```

The command refuses other databases / nondevelopment environments, creates fictional
branches and three demo accounts, and preserves existing users and case progress on
repeat runs. Open `/reviews/?demo=1` after signing in as `review.manager` or
`review.admin`. `review.other` verifies branch isolation. The fixtures include:

- A five-star review with two issues and a reply awaiting approval.
- In-progress and waiting work, a sensitive urgent suggestion, a ready-to-close
  ticket, and a closed ticket with service notes.
- Positive feedback without a complaint, a one-star review without text,
  a different branch's case, and a branch needing manager assignment.

Verification: update one of two issues and confirm the parent stays open; complete
both and close; the action/service notes are retained; reopen with a reason. Try saving an
old tab after another update and expect a conflict. Confirm a reply approval does
not close the ticket. Confirm that a manager can close a completed sensitive case
under the default policy, the generic acknowledgement contains no sensitive details,
and another branch's guessed URL is denied. Compare demo reporting to the empty live workspace.

## Integration still to connect

Connect trusted Google location → internal branch mapping and machine authentication
before accepting worker events. Validate every identity/version, deduplicate event
IDs/payload fingerprints, preserve issue IDs and staff actions through reanalysis,
and quarantine unknown/stale events. No model may select branch or manager identity.
Connect the persisted outbox to an approved dispatcher and configure SLA/escalation
policy before delivery. Connect the fixed-template acknowledgement intent only after
trusted assignment persistence. Failed assignment must not produce a referral claim.

Source snapshots are separate and expire after 30 days in this initial staff/demo
workflow. Pages stop exposing expired source text/rating immediately; run
`python manage.py expire_review_snapshots` to clear stored expired snapshots.
This is not a claim of approved Google retention. Before live API intake, confirm
permitted storage/use, apply expiry to every source-derived field (including AI
issue text), drafts, logs, exports and backups, and add a permitted refresh path.
Publishing requires a separate approved backend with source/draft freshness checks,
idempotent intents and reconciliation of unknown writes. The UI cannot enable it.
