# Google review result endpoint — queue worker V3

The existing Django/PostgreSQL backend receives `hairship.review-result.v1` directly.
This implements [APP_CONTRACT_V3](contracts/APP_CONTRACT_V3.md) and the common
[V2 receipt](contracts/APP_CONTRACT_V2.md). AWS owns Google fetching, analysis,
publication, queue retries and DLQ redrive. The app never approves, republishes or
regenerates Google replies. No processing lease, Google token, shared database,
DynamoDB table, EventBridge schedule or app-side AWS integration is required.

## Endpoint and private authentication

| Setting | Value |
| --- | --- |
| Method/path | `POST /api/integrations/google-reviews/results` |
| AWS URL variable | `APP_REVIEW_RESULT_API_URL` |
| Authentication header | `X-Integration-Key` |
| Request headers | `Content-Type: application/json`, `Idempotency-Key: <event_id>` |
| Sandbox server variable | `REVIEW_INTEGRATION_KEY_SANDBOX` |
| Production server variable | `REVIEW_INTEGRATION_KEY_PRODUCTION` |

The production server is **operation.hairship.in**. Configure the AWS worker's
`APP_REVIEW_RESULT_API_URL` with this HTTPS POST URL:

```text
https://operation.hairship.in/api/integrations/google-reviews/results
```

This is the production target; the installed release, live endpoint and matching
key must be verified on that server. Repository tests do not confirm deployment.
Local verification uses `http://127.0.0.1:8000` with the same path. Codespaces and
the retired `operations.shahinanalytics.com` test server are not the production
AWS endpoint. After checks pass on a push to `main`, GitHub Actions publishes
and deploys the production image through the self-hosted production runner.

The API route contains no hardcoded domain. Production reads its allowed hostname
and proxy settings from `/etc/salonops/prod.env` through `compose.production.yaml`:

```dotenv
ALLOWED_HOSTS=operation.hairship.in,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://operation.hairship.in
TRUST_PROXY_HEADERS=True
```

The company HTTPS proxy must preserve the request Host and set
`X-Forwarded-Proto: https`. If using the optional Caddy proxy, also set
`SITE_DOMAIN=operation.hairship.in`. The API must receive POST directly without a
proxy login redirect. See [production deployment](production-deployment.md).

The canonical route accepts POST directly with no login redirect, session-cookie
requirement or CSRF token. Both slash spellings are registered directly. Wrong or
missing authentication returns JSON 401; unsupported methods return 405. Public
HTTPS is required (`REVIEW_API_REQUIRE_HTTPS=True`, also the default when DEBUG is
False). Only isolated local HTTP development may disable that setting. Keep TLS
verification enabled and configure only the actual trusted proxy.

Use independent random keys of at least 32 characters. The key is selected using
the validated payload environment; sandbox credentials cannot authenticate
production requests. Blank keys disable that environment (503). Deployed Compose
files explicitly pass only these app credentials to web, never to the database.
Do not run `docker compose config` in shared logs: resolved output includes secrets.

### Secure key preparation and transfer

Generate a private JSON file outside any Git checkout in the application Python
runtime. The command creates a new file with mode 0600, refuses overwrites, and
prints the path only:

```sh
python manage.py create_review_api_secret --output /private/path/review-integration.json
```

It writes this **shape**, with a generated value that must stay private:

```json
{"headers":{"X-Integration-Key":"<private-generated-value>"}}
```

In Docker, generate under `/tmp` inside the web container and copy it into your
private server secret directory with `docker compose cp`. Use the company's
secret-injection/password-manager process to install the JSON header value as
`REVIEW_INTEGRATION_KEY_PRODUCTION` in `/etc/salonops/prod.env` (0600), matching
AWS payloads with `environment: production`. Sandbox payloads use an independent
`REVIEW_INTEGRATION_KEY_SANDBOX` value. Recreate the application container and
restart the AWS worker after changing their environment; existing processes will
not reload it. Never overwrite an existing live key merely to update the hostname.

Transfer that same private JSON through the approved secret channel to AWS Secrets
Manager `hairship/app/review-integration`, for the selected worker environment.
For example, on a securely configured AWS CLI host:

```sh
aws secretsmanager create-secret --name hairship/app/review-integration \
  --secret-string file:///private/path/review-integration.json \
  --query ARN --output text
```

Use the worker's existing secret configuration and scoped access to that ARN.
Use separate secrets/accounts or environment-specific names for sandbox and
production. This repository does not create or read live AWS secrets. Never put
the value in Git, chat, screenshots, command-line headers or workflow logs.
No live key was installed or transferred as part of local implementation.

The old `/api/v1/google-reviews/ingest/` and `/publications/` routes remain compatible
for existing callers. Only those legacy routes also accept Bearer auth. Legacy
`REVIEW_API_TOKEN_*` server variables remain fallbacks; use the new key names for
this deployment. V3 needs only the canonical result URL. Do not configure separate
processing/case/publication POSTs for the queue worker.

## Envelope and committed receipt

### Finding a committed sandbox review

Production reviews are shown by default. Admin can use **Google Review → Sandbox reviews** or
`/reviews/google/?demo=1` to see sandbox events, including positive reviews with
no complaint ticket. Detail URLs also need `?demo=1`. Sandbox records remain
excluded from production analytics and live manager alerts. Sandbox routes, branch
analytics, issue reporting and integration diagnostics are Admin-only; General
Manager retains production review access and assignment tools. Employees keep
their existing access permissions.

`python manage.py inspect_google_review --review-id <receipt-review-id>
--event-id <aws-event-id> --environment sandbox` traces the receipt, saved review,
database identity and trusted branch mapping without changing data. Run it in
the same application deployment that handled the POST. See the
[Codespaces diagnostic example](codespaces.md) for the Koppam location check.
Google identity is normalized to the full account/location path; `profile_key`
does not choose a branch. Positive reviews without issues or human follow-up
appear under All reviews and intentionally have `case_id: null`.

### Stored envelope

The optional reviewer fields live inside `source.review_snapshot` in the existing
result envelope:

```json
{
  "reviewer_display_name": "Example Reviewer",
  "reviewer_is_anonymous": false,
  "star_rating": 5,
  "text": "Great service and friendly staff.",
  "existing_reply": null
}
```

Both reviewer fields may be omitted by older senders. A missing, null, empty or
whitespace-only display name, or `reviewer_is_anonymous: true`, displays
**Anonymous reviewer**. A supplied anonymity flag must be a boolean. Names are
display metadata, never customer identities: ingestion neither creates nor links
a customer. Cards show name, rating, text, mapped branch, `review_create_time` and
`review_id`. Deduplication continues to use `(environment, full review_name)`;
neither names nor short review IDs replace that identity. The rest of the result
envelope, trusted store/location mapping and version validation remain required.

The API stores the complete original event, full analysis, processing diagnostics,
Google reply state, existing reply and unknown optional fields. Dashboard fields
are normalized separately. `analysis.status` describes draft validation only;
public state comes from `publication`. Submitted/published and human-investigation
requirements can coexist without closing internal work.

Accept Google account/location/review IDs matching the **full** `source.review_name`.
Normalize `locations/<id>` to the full account/location identity. When supplied,
`source.store_code` selects the active branch with that configured store code;
otherwise the existing environment/location mapping selects it. `profile_key` cannot
choose an internal branch or manager. Review text may be empty for rating-only
feedback. Require timezone-aware Google and processing timestamps. Queue worker
V2/V3 requests require integer `processing.result_revision` (0 through signed
64-bit maximum); it is an ordering value, not a consecutive counter.

All publication states are supported:

| State | Meaning |
| --- | --- |
| `pending` | Confirmed queued rate-limit retry; not published or resolved. |
| `submitted` | Google accepted submission; visibility is not confirmed. |
| `published` | Google reports approval; not proof of the exact customer-visible instant. |
| `failed` | Definite API/moderation failure; retain the error code. |
| `uncertain` | Unconfirmed write; app never retries Google publication. |
| `superseded` | Source/reply changed or disappeared; retain prior history. |
| `skipped` | Existing reply preserved or automatic publication disabled. |
| `human_review` | No valid automatic reply; human handling remains available. |

`published` must agree with status. Published outcomes require actual text and a
publication timestamp; failures require an error code. `submitted_at`,
`google_reply.updateTime`, `reviewReplyState`, visibility, confirmation and future
optional diagnostics are retained. A moderation timeout can remain `submitted`
with `moderation_not_confirmed_within_retry_window`; it is never marked resolved.
The generated draft is stored from `analysis.reply_text` independently of actual
published text. Actual public text is shown as published only for that state.

HTTP 200 is returned **after** the review, applicable case, incoming issue
reconciliation, actual assignment, notification outbox and original receipt commit:

```json
{
  "event_id": "same-event-id-as-request",
  "status": "committed",
  "committed": true,
  "review_record_id": "e5c195a1-2596-4709-893c-bd247ef7e89f",
  "case_id": "ba75d2d4-7b1e-4697-b97d-4b11efb942e6",
  "issues_saved_count": 1,
  "assignment_confirmed": true
}
```

IDs above are illustrative; actual saved UUIDs are returned as strings.
`issues_saved_count` counts the incoming `analysis.issues` reconciled for this
event, excluding historical and manually added tasks. A positive review without
follow-up returns null case, count 0 and assignment false. A saved unassigned case
returns its actual case ID and assignment false. Assignment means a persisted,
currently eligible owner, not external notification delivery. A low rating without
explanation creates a clarification case with zero invented complaint tasks.

A durably ignored older version/revision can return:

```json
{"event_id":"older-event-id","status":"stale","committed":true}
```

No HTTP 202 is used as proof of persistence. Responses stay far below 128 KiB;
request bodies are limited to 512 KiB. Transaction failure rolls back partial work.

## Idempotency, review versions and Google evidence

- `(environment, event_id)` is database-unique. Exact retries return the original
  successful receipt and repeat no side effects. Reusing an ID with changed JSON
  returns 409. A supplied `Idempotency-Key` must equal the body event ID.
  PostgreSQL locks serialize concurrent first deliveries.
- Business identity is full `review_name` within the isolated environment.
  Google `review_update_time` determines customer-review versions. Old versions
  retain raw history but cannot overwrite current source or manager work.
- Within each analysis run and Google version, only the greatest result revision
  controls its publication disposition. Revision gaps and higher-first arrival
  are valid. Lower/equal revisions are durably acknowledged as stale.
- Across independent analysis runs of the same Google version, compare affirmative
  Google evidence and timestamps, not UUID ordering. Confirmed approval, observed
  supersession or explicit Google moderation rejection outrank unconfirmed
  uncertain/skipped/pending observations. Definite failure outranks weaker results;
  confirmed submission outranks unconfirmed outcomes. Google update timestamps
  are used where available; fetch observation time handles state changes/removal.
  Equal evidence/time ties preserve approval before an arbitrary transport-ID tie.
- Another analysis of an unchanged review reuses existing tasks: stable issue IDs
  and exact normalized issue descriptions match first, then unmatched derived
  tasks reconcile one-to-one within their category. Exact matches are reserved
  before reworded matches. Additional distinct concerns get separate tasks.
  Across actual Google review edits, prior task identities/history remain intact.
- Manager notes, assignees, confirmations, resolutions, administrative closes and
  actor/time audit records survive edits/reanalysis. New issue tasks can reopen a
  closed case with its old closure in history; a pure moderation update cannot.
  Administrative appreciation closes are tied to the Google review version.
- Alerts deduplicate by review/version/recipient, independently of analysis/run IDs
  and case edit counters. They become visible only after transaction commit.
  Sandbox outbox entries remain preview-only and never enter live alert feeds.

Unknown/inactive store codes are durably quarantined with HTTP 422,
`status:unmapped_store_code` and `committed:false`. They never fall back to a
location mapping. Unknown/inactive Google locations on legacy events return
`status:unmapped_location` instead. An administrator configures the branch store
code or location mapping and retries the same event. An existing inactive location
mapping also blocks ingestion via store code. A store code conflicting with an
existing location mapping returns 409 without saving a review or moving staff work.
Invalid payloads return 400; missing/wrong auth 401; HTTP when HTTPS required 403;
conflicting IDs/versions 409; oversized body 413; wrong content type 415; missing
server key 503. No failure response claims a committed review.

V3's SQS delivery buffer is **14 days**, superseding V2's historical 28-day note.
AWS retries only application delivery; recovery/redrive must not regenerate
analysis or publish another Google reply. Keep the queue trigger disabled until
live URL/authentication and committed receipts have been verified.

## Mapping and staff pages

Set **Django Admin → Branches → Store code** to the exact Google store code, such as
`HS-001` for the Koppam branch. The optional `STORE_CODE` column in the confirmed
branch CSV import also sets this value. Store codes are unique and case-sensitive,
up to 120 characters. Existing branches can leave it blank until configured; the
migration does not guess or backfill Google store codes from internal branch codes.
The branch's existing primary key and internal `code` remain unchanged.

The [store-code positive example](examples/google-review-store-code-positive.json)
can be ingested with no location mapping once an active branch has `HS-001`.
It produces a published appreciation with no complaint case. A supplied store
code must be a nonblank string; surrounding whitespace is trimmed, control
characters are rejected, and case is preserved. Do not reuse one store code for
different branches in sandbox and production. Review records, idempotency and
manager access remain isolated by environment and branch as before.

For older events that omit `source.store_code`, continue using
Django Admin → Google review locations: map the environment and full
`accounts/<account>/locations/<location>` to the existing branch. For the supplied
fixtures use `accounts/101013083477743854199/locations/12925644274742814696`.
No mapping is created automatically for real branches. The existing manager roster
selects exactly one eligible owner; missing/ambiguous owners go to Needs assignment.

Google Review → All reviews stores appreciations, mixed/negative and rating-only reviews.
Appreciations can be closed without investigation. Complaints/human follow-up use
one case with separate issue cards; health issues are urgent. Managers remain
branch scoped; Admin/GM can inspect/filter all branches and assignments. Analytics,
issue reporting, sandbox views, sentiment filters and integration diagnostics are
available only to Admin.
Unresolved outcomes remain unresolved; public reply success never resolves a case.
No appreciation topics are invented from positive sentiment. Sandbox views use
`?demo=1` and are excluded from production analytics and notifications.

## Local verification and deployment pending

The original synthetic worker examples are in [worker-v2](examples/worker-v2/):
positive, mixed, rating-only and sensitive. They share one Google identity/version,
so test them independently or assign distinct synthetic review IDs for batch demos.
The V3 suite derives queue/moderation scenarios without changing these originals.
Codespaces still starts with `bash scripts/codespace.sh`; sample review pages use
`/reviews/google/?demo=1`. Do not give AWS a private Codespaces port URL.

```sh
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py makemigrations --check --dry-run
docker compose exec -T web python manage.py test operations.test_google_review_contract_v3 operations.test_google_reviews operations.test_reviews --noinput
```

Migration **0020_review_contract_v3** extends publication states and adds revision
and evidence ordering. It backfills existing publication history so an old approved
reply cannot lose to an unconfirmed result after upgrade, and reuses the existing
current-version notification. Migration **0019** creates the ingestion schema.
Neither deletes manager actions, source events or operational data.
Migration **0022_google_review_reviewer_anonymity** adds the optional anonymity flag
with a false default for existing records. Apply it before running the updated app.

The current [Actions workflow](../.github/workflows/salonops.yml) runs tests on
GitHub-hosted machines for pull requests and pushes to `dev` and `main`; it does
not deploy to a test server. Successful `main` pushes publish an image and deploy
production through its dedicated self-hosted runner. Legacy test-server releases
remain separate manual operations. See [test HTTPS deployment](test-https-deployment.md)
only when maintaining an existing test VPS.

For production, use the reviewed **main** release, `compose.production.yaml`,
`/etc/salonops/prod.env` and `scripts/deploy.sh` as documented in the
[production handover](SalonOps_Production_Server_Handover.md). The production
Compose file passes the integration keys to web explicitly. Install the matching
private key and configure Google location-to-branch mappings before enabling AWS
deliveries. The legacy `scripts/deploy_test_https.sh` and `compose.test.yaml` target
the retired test hostname and must not be used to deploy operation.hairship.in.

After deployment, verify `/health/` and POST a sandbox fixture directly to the full
HTTPS URL. Supply the private key through a protected client header file/secret
store; never put it in the command line or chat. Do not follow redirects. Check
HTTP 200/201, no Location header, event ID echo, `committed:true`, real record/case
IDs, exact incoming issue count and actual Boolean assignment. Retry the identical
request and confirm its original receipt and unchanged task/alert counts. A bad
key must return JSON 401, not a login page. Only then provide the verified full
HTTPS URL and `X-Integration-Key` header name to the worker operator; transfer the
actual key through the agreed private channel.
