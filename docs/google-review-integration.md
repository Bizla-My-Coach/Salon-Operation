# Google Business Profile integration (v1)

The application **receives and stores** AWS results. AWS fetches reviews, runs AI,
and publishes Google replies independently. App downtime, ingestion receipts,
assignment results, and manager approval must never gate public publication.
`published: true` and `requires_human_review: true` can coexist: a public
acknowledgement can be published while the internal investigation remains open.
The application has no Google publisher or Google credentials.

## Endpoints and authentication

After deploying this revision, configure these URLs using your deployed HTTPS origin:

| Purpose | Method and path | Example using the documented test domain |
| --- | --- | --- |
| Review ingestion | `POST /api/v1/google-reviews/ingest/` | `https://operations.shahinanalytics.com/api/v1/google-reviews/ingest/` |
| Publication outcome | `POST /api/v1/google-reviews/publications/` | `https://operations.shahinanalytics.com/api/v1/google-reviews/publications/` |

These are **deployment targets**, not a claim that this revision or its secrets
are already installed on that server. Local development uses the same paths at
`http://127.0.0.1:8000`. The static verification site cannot receive these APIs.

Both endpoints require `Content-Type: application/json` and
`Authorization: Bearer <token>`. Set independent `REVIEW_API_TOKEN_SANDBOX` and
`REVIEW_API_TOKEN_PRODUCTION` server secrets, each at least 32 random characters.
The credential must match the payload's `environment`. Blank credentials disable
that environment's endpoints (503). Browser sessions do not authenticate machine
requests. Set `REVIEW_API_REQUIRE_HTTPS=True` for deployed servers; trusted proxy
settings must reflect your actual HTTPS proxy. Only isolated local HTTP testing
should use False. Requests are limited to 512 KiB. Machine endpoints are CSRF
exempt; all human mutations retain session authentication and CSRF protection.

### AWS configuration names

- `APP_PROCESSING_API_URL`: the review ingestion URL above. Send the complete result
  envelope once, after obtaining an AWS result, through an actual HTTP POST.
- `APP_REVIEW_CASE_API_URL`: no separate case-creation POST is needed. Ingestion
  atomically creates applicable cases/issues. Remove the old secondary call; if a
  legacy worker requires this variable, it can reference the same ingestion URL
  only when sending the **identical event ID and payload**. Do not send a
  case-only payload to it.
- `APP_PUBLICATION_STATUS_API_URL`: the publication-outcome URL above, for later
  publication changes and outcomes arriving before ingestion.
- `APP_AUTH_SECRET_ARN`: ARN of the AWS Secrets Manager secret containing
  `{"token":"<same token installed on application server>"}`. This is an ARN
  returned by your AWS account, not an app-generated ARN. Use separate secrets
  for sandbox and production and grant the worker `secretsmanager:GetSecretValue`
  on the matching secret (plus KMS decrypt if required by that secret's key).

### Secure token creation and transfer

The command below generates a private JSON file (0600), refuses Git checkouts,
and never prints the token. Run in the application Python environment, using a
private directory outside the repository:

```sh
python manage.py create_review_api_secret --output /private/path/review-api-sandbox.json
aws secretsmanager create-secret --name hairship/sandbox/app-review-api \
  --secret-string file:///private/path/review-api-sandbox.json \
  --query ARN --output text
```

In Docker, run the management command inside the web container with an output
under `/tmp`, then use `docker compose cp` to copy that private file into your
server's private secret directory. Transfer it to the configured AWS CLI host
through your secure administration channel if needed. Install the JSON `token`
value as the matching server environment secret without printing it. Restart or
recreate web workers so they load it. Never put secret values in Git, URLs,
workflow logs, screenshots, or chat. Repeat independently for production. This
change does not create a secret, assign an ARN, or transfer any live credentials.

## Review result request

[Full sensitive example](examples/google-review-result.json) matches the proposed
AWS worker payload exactly, including `source.review_snapshot`, `processing`,
`analysis`, and `publication`. Additional executable examples:

- [Positive appreciation](examples/google-review-positive.json): saved, no complaint case.
- [Five-star mixed feedback](examples/google-review-mixed.json): two independent issue tasks.
- [Rating only](examples/google-review-rating-only.json): clarification case, zero invented issues.
- [Edited review](examples/google-review-edited.json): updates the sensitive example's existing review.

Use `schema_version: "hairship.review-result.v1"`. Required identity fields are
`source.provider` (`google_business_profile`), full `review_name`, matching
`account_name`, `location_name` and `review_id`. `location_name` may be
`locations/<id>` as in the example, or the full account/location resource name.
The app normalizes it to `accounts/<account>/locations/<location>` for mapping.
`profile_key` is stored for tracing but cannot choose a branch or manager.

Require `event_id`, `environment` (`sandbox` or `production`), Google create/update
times with timezones, `review_snapshot.star_rating` (integer 1–5),
`processing.analysis_run_id`, and timezone-aware `processing.analyzed_at`.
`review_snapshot.text` can be empty for rating-only reviews. Optional
`review_snapshot.reviewer_display_name` is supported. `processing` and the full
original `analysis` JSON are retained, including prompt/model versions, scope,
reason and any extra analysis fields. The complete original event is retained too.

Analysis supports the supplied `sentiment`, `complaint_detected`, `issues`,
`requires_human_review`, and `case_followup_requires_human` fields. `issues` and
optional `complaints` contain issue objects with `category`, `reported_issue`
and optional `priority` (low/normal/high/urgent). Give each issue an optional stable
`issue_id` across edits/reanalysis for the best continuity. Without it, identity
is derived from category and normalized issue description; a changed description
creates a new task while retaining the earlier task and its staff actions. Arrays
are never matched by their position. At most 100 distinct issues are accepted.
Unknown categories map to Other; the original taxonomy remains in source JSON.
`injury_or_health` always becomes an urgent Health task.

A case is created for any reported issue, complaint flag, human follow-up flag,
or rating of 1–2 without an explanation. Human follow-up alone creates a case
with zero issue tasks. No complaint is invented from a rating. A positive review
without follow-up creates no complaint ticket; staff can mark it closed from
All reviews without an investigation or a manager-assignment requirement.

### Committed receipt

Success is HTTP 200 only after the transaction commits the review, applicable
case, issue tasks, history and original receipt. IDs below are illustrative UUIDs:

```json
{
  "event_id": "example-delivery-001",
  "status": "committed",
  "committed": true,
  "review_record_id": "e5c195a1-2596-4709-893c-bd247ef7e89f",
  "case_id": "ba75d2d4-7b1e-4697-b97d-4b11efb942e6",
  "issues_saved_count": 1,
  "assignment_confirmed": true
}
```

An appreciation returns `case_id: null`, count 0, assignment false. An unassigned
case returns its actual ID and assignment false. `issues_saved_count` includes
retained tasks from earlier analyses. Assignment true means an actual saved owner
was eligible for the branch when the receipt was committed; it does not claim
notification delivery or Google publication. Retrying the same event returns the
original successful receipt even if staff subsequently change assignments.

## Publication outcomes

Use a **new event ID for each distinct publication outcome**, with
`schema_version: "hairship.review-publication.v1"`. Send the same source identity
and processing metadata, plus `publication`. No analysis or snapshot is needed.
[Published example](examples/google-review-publication.json) and
[failed example](examples/google-review-publication-failed.json) are complete requests.

Supported statuses: `pending`, `published`, `failed`, `skipped`, `human_review`.
`published` must agree with status. A published result requires actual `reply_text`
and `published_at`; a failure requires `error_code`. Include
`publication.review_update_time` to identify the exact Google version. Always send
`publication.occurred_at` for later changes/retries, particularly failed/skipped
outcomes; it represents when the outcome happened, not HTTP retry time. For the
supplied initial result example it defaults to `published_at`, or to
`processing.analyzed_at` if unpublished. `reply_kind` is retained.

A pre-ingestion publication is durably saved and returns:

```json
{
  "event_id": "demo-publication-before-review",
  "status": "committed",
  "committed": true,
  "review_record_id": null,
  "reconciliation_pending": true
}
```

It attaches automatically when the review arrives. This original receipt remains
unchanged on retries. For an existing review, its real ID is returned and
`reconciliation_pending` is false. Publication never creates a complaint case or
changes a manager's issue status. The UI displays the latest outcome for the
current Google version separately from internal resolution. Actual published
text, timestamp, error code and every prior outcome remain in publication history.

## Idempotency, edits and retries

- Google identity is the full `review_name`, unique within each isolated environment.
  `review_id` alone is insufficient across locations/accounts.
- `(environment, event_id)` deduplicates all endpoints. Exact retries return the
  original receipt; reusing an ID for different content or endpoint kind returns
  HTTP 409. PostgreSQL transaction locks cover concurrent first deliveries.
- Source ordering is `(review_update_time, processing.analyzed_at)`. A later analysis
  of the same Google version can update the source analysis. Older events remain
  in history but cannot overwrite current source fields. A different source/analysis
  at the same ordering tuple is a conflict; send a later analysis time.
- Issue tasks, manager notes, confirmations, priorities, assignments and audit logs
  survive source edits. Removed source issues are retained, labelled historical,
  and require an explicit staff outcome. New issues reopen a closed investigation
  and audit its prior closure. Appreciation closes are tied to the Google version;
  a customer edit makes the new version open for review again.
- Publication reduction uses matching environment, review identity and Google
  version, then the newest `occurred_at` (event ID breaks equal-time ties). Old
  versions never replace the current version's publication state. Deliver later
  state transitions with a strictly later occurrence time.
- Unknown/inactive locations are durably quarantined: HTTP 422,
  `{"event_id":"...","status":"unmapped_location","committed":false}`.
  Configure the mapping and retry the exact event; Admin/GM can also retry it
  from Needs assignment. No successful receipt is claimed before review commit.
- Invalid JSON/schema: 400; wrong credential: 401; HTTP when HTTPS required: 403;
  conflicting event/version: 409; oversized body: 413; wrong content type: 415;
  missing server credential: 503. Transaction failure rolls back all records.
  AWS should persist undelivered events and retry transient/network failures with
  backoff using the same event ID and body; investigate validation/conflict errors.
  This delivery/retry path must remain independent of Google reply publication.

A Lambda **return value does not deliver an event**. The worker must load its
secret and make an HTTP POST, for example:

```python
import json
from urllib.request import Request, urlopen
import boto3

secret = json.loads(boto3.client("secretsmanager").get_secret_value(
    SecretId=APP_AUTH_SECRET_ARN)["SecretString"])
request = Request(APP_PROCESSING_API_URL, data=json.dumps(result_event).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + secret["token"]}, method="POST")
with urlopen(request, timeout=15) as response:
    receipt = json.load(response)
# Persist/dedupe/retry delivery in the worker. Do not make Google publication wait
# for this HTTP call, receipt or assignment_confirmed.
```

## Mapping, manager workflow and analytics

In Django Admin, add **Google review locations** with the environment, normalized
full account/location name, and existing branch. For the supplied example:
`accounts/101013083477743854199/locations/12925644274742814696`.
Create sandbox and production mappings independently. The application's existing
branch-manager roster/duty rules select exactly one eligible manager; missing or
ambiguous managers go to Admin/GM **Needs assignment**. Senior staff can assign an
eligible manager from the investigation page. AWS/AI cannot supply assignments.

**Tickets → All reviews** stores all feedback. **Investigations** shows actionable
work, including rating-only clarification cases. Managers are branch scoped;
Admin/GM see all branches and can filter by branch, handling state and priority.
The existing bell notifies the assigned manager after committed intake. Synthetic
cases use preview notifications visible only in the demo workspace.

Accept the review, update each issue's customer follow-up and staff follow-up,
and record action/service notes. Staff statuses support Not started, In progress,
Awaiting follow-up, Resolved, Unresolved and Not actionable. Server time and actor
are saved automatically. Every issue must have an outcome before closure.
Unresolved/partial outcomes remain visibly **Unresolved** and never count as
resolved cases or successful resolution times. Reopening requires a reason and is
audited. Public reply controls are absent and rejected for AWS-linked cases.
Company sensitive-case closure policy still applies to internal investigation only.

**Branch analytics** includes volume, rating distribution/average, sentiment,
positive feedback, mixed complaints, complaint categories, urgent open cases,
assignment backlog, unresolved issues, fully resolved case duration and current
reply outcomes. It uses one current record per Google review, not delivery counts.
Mixed high-star reviews count in complaint reporting. Positive feedback has no
invented appreciation categories. Retained historical issue tasks are explicitly
included in complaint/unresolved reporting. Sandbox (`?demo=1`) and production
analytics, review records, credentials and notification feeds are separate.

## Migrations, deployment and verification

Migration **0019_google_review_ingestion** adds Google location mappings, current
reviews, immutable source events/receipts, publication outcomes, administrative
close history and stable AWS issue identity. It preserves existing staff cases.
Source events and complete analysis are retained to preserve review-edit history.
The older staff-intake snapshot expiry job only clears legacy snapshots; it is
not an expiry policy for these new Google source/event/history records.

1. Deploy the reviewed `dev` build through the existing test workflow; use the
   established backup/migration deployment scripts for the server. For local Docker:
   `docker compose up --build -d`, then `docker compose exec -T web python manage.py migrate --noinput`.
   Normal web startup also applies pending migrations.
2. Install environment-specific credentials securely and recreate workers. Configure
   trusted HTTPS and location mappings. Keep the AWS worker in sandbox initially.
3. Check `/health/`, API authentication, All reviews, Investigations, Needs assignment
   and Branch analytics using manager and GM accounts. Verify another branch is denied.
4. Run the PostgreSQL integration suite:
   `docker compose exec -T web python manage.py test operations.test_google_reviews operations.test_reviews --noinput`.
   It demonstrates positive, mixed, sensitive, duplicate/concurrent, edited/older,
   unassigned, rollback and publication-before-ingestion/failure cases.
5. In Codespaces, `bash scripts/codespaces.sh` now also seeds isolated Google
   examples. Sign in with the existing **admin/admin**, **manager/admin** or
   **gm/admin**, then open `/reviews/google/?demo=1`. Repeated startup preserves
   staff work. In a dedicated development `*_review_preview` database, run
   `seed_review_demo` first, then `python manage.py seed_google_review_demo`.
   The seed command refuses other environments/databases and sends no external requests.

Production enablement requires the reviewed revision, migrations, matching token,
location mapping and an AWS worker that actually POSTs the documented envelope.
The older draft-only worker ZIP does not provide that delivery automatically.
