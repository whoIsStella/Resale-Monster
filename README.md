# Resale Goliath

A safe, local-first resale operations foundation with persistent orchestration for Codex CLI,
Hermes, and configurable command-line agents.

General coding agents can inspect approved engineering files, draft code, run approved commands,
and return structured results. Separately authorized operations agents can request typed marketplace
reads and writes through Goliath's deterministic policy gateway. No agent can receive marketplace
credentials, browser profiles, cookies, payout access, unrestricted SQL, or unrestricted browser
controls.

## Linux setup

```bash
cd resale-goliath-starter
./scripts/setup_linux.sh
cp config/agents.example.yaml config/agents.yaml
```

Edit `workspace_roots` in `config/agents.yaml` to name the engineering directories agents may use.
Production database commands require PostgreSQL:

```bash
export GOLIATH_CONFIG="$PWD/config/agents.yaml"
export GOLIATH_DATABASE_URL='postgresql+psycopg://user:password@localhost/resale_goliath'
alembic upgrade head
```

Database credentials belong only in the application environment. Do not put them in the agent
configuration or an agent environment allowlist.

## Architecture

The orchestration service validates and persists a submission before routing it. The router honors
an explicit agent without fallback; automatic routing filters enabled agents by availability, task
type, capability, permissions, and configured priority. A worker atomically claims one queued job,
runs its adapter, persists output and structured results, and commits each transition together with
its append-only audit event.

```text
CLI -> typed submission -> orchestration service -> bounded repositories -> PostgreSQL
                                  |
                                  +-> router -> adapter -> process supervisor -> CLI agent
```

Repositories expose task-specific methods only. There is no generic SQL execution method. SQLite
is isolated to tests; `create_production_engine` rejects non-PostgreSQL URLs.

### Job lifecycle

```text
pending -> queued -> running -> succeeded
   |          |         |-----> failed
   |          |         |-----> timed_out
   +----------+---------+-----> cancelled
```

Routing failures may move `pending` or `queued` jobs to `failed`. Terminal states cannot restart.
Every update uses a versioned compare-and-swap. A worker can claim a job only when both its observed
status and version still match, preventing duplicate execution by competing workers.

Created, queued, started, completed, and cancelled timestamps are persisted. Captured stdout and
stderr are bounded independently while both streams continue to be drained. When a limit is
reached, the record stores the retained text, original character counts, and explicit truncation
flags; truncation is never silent.

## Agent configuration

See `config/agents.example.yaml`. Each agent supports:

- enable/disable state and routing priority;
- an argument-sequence command template using `{prompt}`, `{workspace}`, and optional `{profile}`;
- capabilities, task types, and permissions;
- a minimal environment allowlist; and
- an optional Hermes profile.

Command templates are passed directly to `asyncio.create_subprocess_exec`; no shell is used.
Sensitive environment names such as API keys, tokens, passwords, payout variables, and database
URLs are rejected from configuration.

### Codex CLI on Linux

Install Codex CLI using OpenAI's current standalone Linux installer:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex
```

The first interactive launch authenticates the CLI. Resale Goliath then uses non-interactive
`codex exec` through the configured command template. The Codex desktop GUI is not required and is
not available natively on Linux; this integration uses Codex CLI only. Credentials are not copied
into job records or prompts, and API-key environment variables are not forwarded.

### Hermes

Install Hermes separately and set its executable path and arguments in the configuration. An
optional profile can be inserted with `{profile}`. Resale Goliath does not install Hermes at
runtime. A missing or disabled Hermes executable is reported as unavailable without crashing.

## CLI

```bash
goliath agent list
goliath agent doctor

goliath job submit \
  --agent codex \
  --task-type code_change \
  --workspace . \
  --permission read_files \
  --permission write_files \
  --permission run_commands \
  --capability coding \
  --objective "Add inventory search endpoints and tests"

goliath job list
goliath job status JOB_ID
goliath job run-next
goliath job cancel JOB_ID --reason "Operator requested cancellation"
```

Add `--json` for machine-readable output. Expected errors produce clear messages and nonzero exit
codes without application stack traces. Job submission always requires an explicit workspace.

## Cancellation and process supervision

Pending and queued jobs cancel immediately. For a running job, the service records the request,
uses the process-group leader PID persisted at spawn time, sends `SIGTERM` to the entire process
group on Linux, waits the configured grace period, and sends `SIGKILL` if processes remain. This
works when cancellation comes from a separate CLI process. Stdout and stderr are drained
concurrently throughout shutdown, preventing child commands spawned by an agent from surviving
cancellation or timeout.

If execution completion races with cancellation, optimistic locking permits only the first valid
terminal transition. The rejected transition is reported rather than overwriting terminal state.

## Security boundaries

- Workspace and context paths are resolved and must remain within configured workspace roots.
- Permissions and supported task types are closed typed sets.
- High-risk commerce actions remain explicitly forbidden in every job.
- Agent processes receive only allowlisted, non-secret environment values.
- Prompts and audit metadata never contain full environments or credentials.
- Production data and engineering workspaces remain separate.
- Live commerce mutations are available only through the Milestone Six marketplace gateway below;
  ordinary engineering jobs still have no such authority.

## Development and migrations

```bash
pytest
ruff check .
alembic upgrade head
alembic downgrade base
alembic upgrade head
alembic upgrade head --sql  # PostgreSQL offline SQL
```

Tests use fake Python subprocesses and make no network calls. Codex and Hermes do not need to be
installed.

## Durable workers, leases, and recovery

Milestone three adds a durable worker registry and lease table. A worker claims a queued job with a
random lease token whose hash is persisted, records its expiry and attempt number, and heartbeats
before the lease expires. Completion and heartbeat both validate worker ID and token. Claims use a
compare-and-swap job version plus the lease row, so competing workers cannot execute the same job.

`goliath worker start` continuously polls, respects configured concurrency, emits structured worker
events, heartbeats, and drains gracefully on SIGTERM/SIGINT. `goliath worker reap` finds expired
leases and applies `fail`, `requeue`, or `retry` recovery policy. Retry attempts honor maximum
attempts, delay/backoff, retryable exit codes/categories, and next-eligible timestamps. Explicit
cancellation, invalid submissions, forbidden actions, permission failures, unsupported agents, and
path failures are never retried.

```bash
goliath worker start
goliath worker status
goliath worker heartbeat WORKER_ID
goliath worker reap
```

## Scheduling

Schedules create ordinary queued jobs; they never invoke an agent directly. Safe internal task types
include maintenance, tests, review, research, analysis, and operational placeholders. Interval,
one-time, and the supported cron subset (`@hourly`, `@daily`, `*/N * * * *`) are timezone-aware.
Schedules can be enabled/disabled and use skip or bounded catch-up behavior.

```bash
goliath schedule create nightly-tests --task-type test --objective "Run tests" --workspace .
goliath schedule list
goliath schedule enable SCHEDULE_ID
goliath schedule disable SCHEDULE_ID
goliath schedule tick
```

Marketplace automation uses the same schedule, job, lease, retry, audit, and worker tables. Install
the account-scoped schedules after provisioning marketplace accounts; repeated installation is safe.
The scheduler only queues jobs, and `goliath worker start` is the sole production execution path.

```bash
goliath marketplace schedules-install --json
goliath marketplace schedules-list --json
goliath marketplace schedules-disable SCHEDULE_ID
goliath marketplace schedules-enable SCHEDULE_ID
goliath marketplace schedules-run-now SCHEDULE_ID --json
```

## Authenticated API

`goliath.api.create_app` builds the FastAPI orchestration application. `/health` is public;
control, audit, agent, worker, schedule, and metrics operations require a Bearer API key when
authentication is enabled. Keys are stored only as SHA-256 hashes, shown once at creation, scoped,
revocable, expirable, and tracked by last use. `Idempotency-Key` on job creation is scoped to the
authenticated principal and rejects conflicting payloads.

The application exposes health/readiness, agent, paginated job, audit, worker, schedule, and metrics
routes with request IDs and OpenAPI documentation. Metrics remain disabled unless explicitly enabled.
Use `goliath auth key-create`, `goliath auth key-list`, and `goliath auth key-revoke KEY_ID` for
local key administration. No OAuth or external identity provider is used.

Example systemd units for API and worker plus a scheduler oneshot/timer pair are provided under
`systemd/`; they are examples only and are never installed automatically. Run marketplace schedule
installation once after account provisioning, then enable both `goliath-worker.service` and
`goliath-scheduler.timer`. The timer invokes only `schedule tick`; marketplace calls occur in jobs
claimed by the durable worker.

## Milestone four: resale-domain operations

Milestone four adds the first real resale-domain capabilities while preserving strict separation
between read-only access, agent recommendations, human-approved proposals, and deterministic
execution. **No live marketplace publishing, repricing, delisting, refunds, buyer messaging,
payouts, or credential access exists in this milestone.**

### Inventory model

`InventoryItem` carries typed fields: SKU, title, brand, model/style name, category, subcategory,
department, size label and normalized size, colors, materials, pattern, condition grade and notes,
defects, acquisition date/source, cost basis, estimated and packed weight, package dimensions,
storage location, status, research/identification confidence, timestamps, and an optimistic
`version`. Supported statuses: `draft`, `research_needed`, `ready_for_listing`, `listed`,
`reserved`, `sold`, `archived`, `donated`, `lost`.

Agents may only write draft fields and may never move an item directly into `listed`, `sold`,
`donated`, or `archived`; those require a deterministic service or explicit human authorization.
Draft updates use a versioned compare-and-swap (`update_draft(expected_version=…)`); a stale
version raises `VersionConflictError`. See `docs/examples/inventory.json`.

### Image metadata workflow

`InventoryMedia` stores only file metadata — identifier, media type, checksum, size, image
dimensions, role (`original`, `processed`, `label`, `defect`, `measurement`, `receipt`,
`document`), original filename, storage key, processing status/error. Bytes live on the filesystem
or object storage, never in the database. Checksum-based duplicate detection is enforced per item,
and media paths are validated to stay within the configured `media_storage_root`.

### Measurement model

Measurements record type, numeric value, unit (`cm`/`in`), method, confidence, source, and notes.
The submitted unit is preserved and a normalized `value_cm` is stored alongside. Negative, zero, or
physically impossible values are rejected.

### Research workflow

1. An item enters `research_needed` and a `ResearchRecord` is created.
2. An eligible agent reads inventory and image metadata over MCP.
3. The agent records `ResearchSource` rows (source type, title, URL, excerpt, relevance,
   reliability, content hash, duplicate flag) and `IdentificationCandidate` rows.
4. The agent submits a structured result; exact identification requires at least one source-backed
   evidence record.
5. Confidence is compared against `identification_auto_threshold`. A proposal is always created —
   agents never write final identification fields directly. Below-threshold identification is
   marked `high` risk.
6. A human approves the proposal, which deterministically applies the identification to the item.

See `docs/examples/research_result.json`.

### Pricing calculations

The pricing engine (`goliath/domain/pricing.py`) is pure and deterministic. It weights sold vs.
active comparables, applies condition/size/stale adjustments, and computes recommended, fast-sale,
and minimum prices, expected net proceeds, profit, margin, confidence, a full breakdown, and
warnings. Marketplace fees come only from versioned configuration (`fee_versions`); nothing is
hard-coded. Agents supply inputs and observations but can never override the arithmetic. See
`docs/examples/pricing_breakdown.json`.

### Listing drafts and marketplace variants

Master drafts are marketplace-neutral and versioned; approved content is never silently
overwritten (`set_status` refuses to un-approve). Variants are generated for eBay, Poshmark, Depop,
Mercari, Grailed, Facebook Marketplace, and a generic marketplace, each validated against
configuration-driven, versioned constraints (title length, required fields, allowed conditions).
Drafts never create live listings. See `docs/examples/listing_draft.json`.

### Approval queue

Typed `DomainProposal`s (`inventory_update`, `identification_selection`, `pricing_change`,
`listing_draft_approval`, `listing_variant_approval`, `archive_recommendation`,
`research_resolution`) capture the proposed payload, justification, evidence, risk tier, and the
resource version at request time. Approval never triggers live marketplace actions; for
inventory-only safe changes, deterministic execution applies the approved payload with a version
check. A proposal whose resource version has advanced is rejected as stale
(`execution_failed`). See `docs/examples/approval_proposal.json`.

### MCP architecture

`goliath/mcp/server.py` exposes a bounded, typed tool surface: `inventory.*`, `research.*`,
`pricing.*`, `listing.*`, and `approval.*`. Every call authenticates a service principal, enforces
a scope, appends an audit event, and returns controlled errors. The server exposes **no** raw
database sessions, unrestricted SQL, shell execution, credentials, marketplace tokens, or live
mutations. The forbidden set — `execute_sql`, `run_shell`, `get_secret`, `get_marketplace_token`,
`publish_listing`, `change_live_price`, `send_buyer_message`, `issue_refund`, `purchase_label` — is
never registered and is asserted absent at construction and in tests.

### MCP authentication and scopes

Service principals are separate from human API principals and store only a hashed credential, with
expiration and revocation. Scopes: `inventory:read`, `inventory:write_draft`, `research:read`,
`research:write`, `pricing:calculate`, `listing:read`, `listing:write_draft`, `approval:read`,
`approval:request`. Raw credentials are shown once at creation and never logged.

Example service-principal creation workflow (issued programmatically; store the returned secret in
the Hermes environment, never in the repo):

```python
from goliath.db.domain_repositories import McpPrincipalRepository
principal, secret = McpPrincipalRepository(session).create(
    name="hermes", scopes=["inventory:read", "research:read", "research:write", "approval:request"]
)
# `secret` is shown once; Goliath persists only its hash.
```

### Hermes MCP setup

See `config/mcp-hermes.example.json`. Hermes is the configurable preferred agent for product
identification, comparable research, stale-inventory review, and inventory quality review; Codex
remains preferred for code changes, tests, migrations, and refactoring. Commerce-domain tasks are
never silently routed to an agent lacking the required MCP scopes
(`goliath/agents/domain_routing.py`).

### Domain API examples

```bash
curl -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"sku":"VJ-1","title":"Levis Jacket","acquisition_cost":18,"category":"clothing"}' \
  http://127.0.0.1:8000/inventory

curl -H "Authorization: Bearer $KEY" \
  -X PATCH http://127.0.0.1:8000/inventory/$ID/draft \
  -d '{"expected_version":1,"changes":{"brand":"Levis"}}'

curl -H "Authorization: Bearer $KEY" \
  -d '{"item_id":"'$ID'","cost_basis":18,"comparables":[{"price":80,"is_sold":true}]}' \
  http://127.0.0.1:8000/pricing/calculate
```

Endpoints support pagination, filtering, optimistic version checks (409 on conflict),
`Idempotency-Key` on creation, scoped authorization, structured errors, and `X-Request-ID`.

### Domain CLI examples

```bash
goliath inventory create --sku VJ-1 --title "Levis Jacket" --cost 18 --category clothing
goliath inventory completeness ITEM_ID --json
goliath research create ITEM_ID --question "identify model"
goliath pricing calculate ITEM_ID --cost-basis 18
goliath listing draft-create ITEM_ID
goliath listing variant-create DRAFT_ID --marketplace ebay
goliath approval list
goliath approval approve APPROVAL_ID
```

Add `--json` for machine-readable output; expected user errors print a message and exit non-zero
without a stack trace.

### Security restrictions (milestone four)

- Agents inspect, research, draft, price, and propose; humans approve; deterministic code applies
  approved inventory-safe changes only.
- No live marketplace mutation, buyer messaging, refunds, payouts, label purchase, or marketplace
  credential access exists.
- Repositories expose only domain methods — no raw sessions, generic access, arbitrary filters,
  or SQL.
- MCP and human credentials are stored only as hashes; media paths are confined to the configured
  root; audit events never contain credentials, tokens, buyer data, or file contents.

### Troubleshooting (milestone four)

- **MCP scope denied:** confirm the service principal's scopes and that the tool's required scope is
  granted; check `goliath_mcp_authorization_failures_total`.
- **Stale proposal:** the resource version advanced after the proposal was created; regenerate the
  proposal against the current version.
- **Media path rejected:** the storage path resolved outside `media_storage_root`.
- **No eligible domain agent:** the preferred agent is disabled or missing a required MCP scope.

## Milestone five: ingestion, imaging, and review dashboard

Milestone five turns the domain layer into a usable ingestion and review system.
It never publishes listings, changes live prices, contacts buyers, accepts offers, issues refunds,
purchases labels, handles payouts, exposes marketplace credentials, or scrapes marketplaces.

### MCP protocol setup

`goliath/mcp/protocol.py` implements a standards-compliant MCP JSON-RPC 2.0 layer:
`initialize` with protocol-version negotiation and capability reporting, `tools/list`, `tools/call`,
`resources/list`, `resources/read`, `prompts/list`, `prompts/get`, `ping`, `notifications/cancelled`,
and graceful `shutdown`. It preserves the bounded tool surface, service-principal authentication,
scope enforcement, audit events, resource-version checks, and forbidden-tool absence. Run it over
stdio:

```bash
GOLIATH_MCP_CREDENTIAL=mcp_... python -m goliath.mcp --transport stdio
python -m goliath.mcp --list-tools    # manifest only
```

Read-only MCP **resources** expose non-sensitive summaries (`goliath://inventory/{id}/summary`,
`goliath://research/{id}`, `goliath://listing-drafts/approved`, `goliath://pricing/{id}/recommendations`,
`goliath://completeness/{id}`, `goliath://approvals/queue`), each scope-enforced and audited.
Bounded **prompts** (`product_identification`, `comparable_research`, `listing_draft_generation`,
`stale_inventory_review`, `inventory_completeness_review`) contain no secrets and escape arguments.

### Hermes MCP configuration

See `config/mcp-hermes.example.json` (stdio) and `config/mcp-http.example.json` (authenticated HTTP).
HTTP binding stays loopback unless authentication is enabled and the non-loopback override is set.

### Media ingestion and storage layout

Uploads (multipart API, CLI, filesystem, or pre-existing registration) are validated without trusting
client MIME/filenames: magic-byte sniffing, MIME/extension allowlists, size and dimension limits, and
filename sanitization. Bytes are written **atomically** (temp file + `os.replace`) under a generated,
non-user-controlled storage key `{{item}}/{{ab}}/{{checksum}}.{{ext}}`; derivations live under
`derived/`. Checksum duplicates are collapsed. Roots (`media_root`, `quarantine_root`,
`temp_upload_root`) must be distinct and non-overlapping. No cloud credentials are used. See
`config/media.example.yaml`.

### Quarantine behavior

Files that fail validation (unrecognized/disallowed MIME, declared-MIME mismatch, disallowed
extension, or corrupt bytes) are written to the quarantine root, marked `quarantined`, and raise a
`media_validation` review task. They never enter processing.

### Image processing

Deterministic, Pillow-backed operations run through durable, leased jobs (retries, exponential
backoff, crash recovery via lease reaping, idempotent derivation naming): EXIF-orientation
normalization, metadata stripping, format normalization, thumbnail and marketplace-preview generation
(aspect-preserving, optional square padding), average perceptual hashing (duplicate detection), and a
quality gate (min dimensions, blur variance, exposure placeholders, extreme aspect ratio, corrupt
files, unsupported color modes). Every generated image keeps a parent-child derivation record.
Processing runs out of band — never synchronously inside API request handlers.

### Optional analysis providers

`goliath/domain/providers.py` defines an `ImageAnalysisProvider` interface with a disabled default and
a deterministic fake for tests. Output is treated as suggestions with confidence and requires human
review; it never mutates final inventory fields. No external vendor is hard-coded and no network is
required.

### Comparable import and review

CSV/JSON import validates every row (marketplace, title, sold/active state, prices, currency, date,
URL, size, condition) and records a status for each — nothing is silently discarded. Supports dry run,
partial import, all-or-nothing rollback, duplicate detection, idempotency, and audit events. Imported
comparables enter a review queue (`pending_review` → `accepted`/`rejected`/`duplicate`/`invalidated`);
reviewers adjust similarity/reliability and add notes, individually or in bulk. See
`docs/examples/comparables.csv` and `docs/examples/comparables.json`.

### Pricing-source policies

`recommend_price_with_policy` applies a versioned, configuration-driven policy (`reviewed-only`,
minimum count, maximum age, marketplace/sold-vs-active weighting, reliability and similarity
thresholds, z-score outlier handling, currency restriction, fallback). By default only reviewed
comparables feed pricing. Every recommendation persists the policy version and lists included and
excluded comparables with reasons. See `docs/examples/pricing_source_policy.json`.

### Review tasks and workflow

A generalized `ReviewTask` (types: inventory_completion, media_validation, image_quality,
research_resolution, comparable_review, pricing_review, listing_review, proposal_review) is created
idempotently when completeness blocks readiness, media is quarantined, image processing fails,
research is inconclusive, comparables need review, pricing confidence is low, listing validation has
blocking errors, or a proposal needs approval. Tasks are claimed with an atomic compare-and-swap so
two reviewers cannot claim the same task. See `docs/examples/review_task.json`.

### Dashboard endpoints

```text
GET /dashboard/summary | /tasks | /inventory-needing-review | /research-needing-review
GET /dashboard/comparables-needing-review | /listings-needing-review | /approvals
GET /dashboard/media-failures | /worker-health
```

All are `dashboard:read`-scoped, paginated where applicable, with stable schemas and request IDs. See
`docs/examples/dashboard_summary.json`.

### API examples

```bash
curl -H "Authorization: Bearer $KEY" -F file=@photo.jpg -F role=original \
  http://127.0.0.1:8000/media/$ITEM/upload
curl -H "Authorization: Bearer $KEY" -X POST http://127.0.0.1:8000/media/$MEDIA/process
curl -H "Authorization: Bearer $KEY" -d '{"item_id":"'$ITEM'","source_format":"csv","content":"..."}' \
  http://127.0.0.1:8000/comparables/import
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/dashboard/summary
```

### CLI examples

```bash
goliath media ingest ITEM_ID photo.jpg
goliath media process           # claim and process one queued media job
goliath media quarantine-list
goliath comparables import ITEM_ID comps.csv --dry-run
goliath comparables review-list
goliath comparables accept COMPARABLE_ID
goliath review list
goliath review claim TASK_ID
goliath review complete TASK_ID
goliath dashboard summary
```

### API scopes (milestone five)

`media:read`, `media:write`, `media:process`, `comparables:read`, `comparables:write`,
`comparables:review`, `dashboard:read`, `reviews:read`, `reviews:write`. Human principals, MCP service
principals, and worker identities stay separate; service principals may **never** hold the human-only
scopes `reviews:write` or `comparables:review` (enforced at principal creation).

### systemd integration

The example units under `systemd/` cover the API, worker, and scheduler. A media-processing loop can
reuse the worker pattern by invoking `goliath media process` on a timer; the MCP server runs as a
stdio subprocess of the agent host (Hermes), not as a system service.

### Security boundaries (milestone five)

- Uploaded filenames and MIME declarations are never trusted; paths cannot traverse configured roots.
- No marketplace scraping, buyer messaging, refunds, payouts, label purchase, live mutation, or
  marketplace credentials exist.
- Analysis and comparable providers are suggestions only; humans review and approve.
- Audit/log events never contain raw file bytes, credentials, tokens, buyer data, full EXIF, or
  private prompts.

### Troubleshooting (milestone five)

- **Upload quarantined:** check `validation_result.findings`; the true MIME did not match the
  allowlist, the declared MIME, or the extension, or the bytes were corrupt.
- **Media job stuck running:** run the reaper (`ImageProcessingService.reap`) to requeue expired
  leases; check `goliath_media_jobs`.
- **Comparable excluded from pricing:** inspect the recommendation's `excluded_comparables` reasons
  (e.g. `not_reviewed`, `too_old`, `reliability_below_threshold`, `price_outlier`).
- **Review task won't claim:** it was already claimed or is no longer open; refetch its version.
- **MCP HTTP rejected:** non-loopback binding requires authentication and the explicit override.

## Troubleshooting

- **Agent unavailable:** run `goliath agent doctor`; verify the executable path and `PATH` allowlist.
- **Workspace rejected:** add the resolved engineering directory to `workspace_roots`; symlink and
  `..` traversal cannot escape a configured root.
- **No eligible agent:** check enabled state, task types, capabilities, permissions, and priority.
- **Database error:** confirm `GOLIATH_DATABASE_URL` is a PostgreSQL URL and migrations are current.
- **Job remains queued:** run a worker with `goliath job run-next`; milestone two provides a durable
  single-job worker command, not a continuously running daemon.
- **Output marked truncated:** raise configured and per-job limits within the configured maximum;
  original character counts identify how much output the process produced.

## Milestone Six: zero-touch marketplace operations

Milestone Six adds a separate operational control plane. An authorized Hermes operations principal
may request a typed operation, but deterministic application code remains responsible for policy,
state, numeric, idempotency, health, rate-limit, circuit-breaker, emergency-stop, execution, and
verification decisions.

```text
Hermes operations principal -> typed MCP tool -> marketplace gateway -> session broker
                                                            -> isolated adapter -> marketplace
                                                            -> verification -> persistence/audit
```

New accounts are `disabled`. `observe` permits synchronization only, `shadow` records proposed
writes, `autonomous_conservative` uses stricter limits, `autonomous_normal` executes enabled routine
work, and `paused` preserves safe reads while blocking writes. Modes are stored with optimistic
versions and append-only audit events. Global and per-account emergency stops and scoped circuit
breakers block writes without stopping reads.

### Adapters and isolated authentication

The async adapter contract covers health, authentication, account/listing/order/offer/message reads,
listing creation and engagement, offer responses, messaging, tracking, fee/shipping queries, label
purchase, and synchronization. Every adapter declares capabilities and returns classified typed
results. Unsupported operations fail closed. The complete in-memory fake and manual export adapter
make tests and offline workflows deterministic. eBay, Poshmark, Depop, Mercari, Grailed, and
Facebook each have a distinct Playwright subclass; selectors and workflows remain marketplace-local.

The session broker encrypts Playwright storage state with Fernet. Each account receives a generated
`0700` directory below the configured session root, outside every agent workspace. Decryption occurs
only while provisioning the adapter; the Playwright foundation passes decoded storage state to an
isolated in-memory browser context and never writes plaintext cookies to disk. Navigation is domain
allowlisted. Goliath never uses a normal browser profile, bypasses CAPTCHA/2FA/challenges, or exports
session data through API, MCP, audit, logs, or metrics.

```bash
pip install -e '.[browser]'
playwright install chromium
goliath marketplace authenticate ACCOUNT_ID --session-state /secure/operator/storage-state.json
```

Remove the operator-controlled input file from its temporary secure location after import. Never put
it in this repository or an agent workspace. See `config/marketplace.example.yaml` for fake,
Playwright, conservative, normal, publishing, cross-listing, offer, message, pricing, shipping,
refund, breaker, emergency-stop, and Hermes examples.

### Publishing, cross-posting, and engagement

Publishing starts only from `ready_for_listing` inventory with approved content, usable images,
current deterministic pricing, sufficient expected profit, a healthy capable account, no active
reservation or duplicate, and no stop or breaker. Inventory is the source of truth. Each account has
an independent idempotency key and listing row, so cross-posting can partially succeed and retry only
failed targets. `single`, `preferred`, `all_eligible`, and category strategies are bounded by an
active-listing maximum. A publish succeeds only after reading back the exact idempotent listing.

Refresh, share, promotion, watcher offers, price updates, and end operations are capability-gated.
Engagement and markdown clocks are persisted per listing. Price reductions use Decimal arithmetic,
minimum profit, daily and total movement caps, cooldowns, stale-age thresholds, marketplace rounding,
and reservation checks. Relisting must end and verify the old listing before creating a replacement;
donation, disposal, and destructive archival remain exception actions.

### Offers, messages, sales, and duplicate prevention

Agents may initiate offer actions, but the deterministic engine owns minimum proceeds, profit,
margin, maximum discount, and counter boundaries. High-value, reserved, disputed, or unmatched cases
escalate. Routine buyer messages use allowed factual categories and body checksums; threats, legal or
counterfeit claims, fraud, chargebacks, off-platform payment, personal-contact requests, tracking
disputes, and unusual refunds create exceptions. Responses never include costs, internal notes,
credentials, or another buyer's data.

Order synchronization uses `(account_id, remote_order_id)` idempotent upserts and buyer-safe hashes.
A paid sale atomically reserves inventory, stops incompatible automation, creates a shipping task,
ends every active copy, verifies each end, retries a failed end once, and marks inventory sold only
after all copies are inactive. Exceeding the delisting target opens an item breaker and an urgent
duplicate-sale exception while reconciliation continues.

### Shipping, refunds, and reconciliation

Shipping tasks preserve location, package profile, weights, dimensions, ship-by time, label reference,
tracking, carrier, and optimistic version. Automatic label purchase requires a paid order, complete
package data, adapter support, idempotency, and a maximum cost carried into the adapter request before
purchase. Higher-weight packages require confirmed packed weight. Tracking is verified by read-back.

Refund policy evaluation is deterministic and limited to explicitly configured routine reasons and
an amount ceiling; disputes, legal/counterfeit/chargeback cases and larger amounts remain exceptions.
Financial reconciliation uses Decimal arithmetic for sale, tax, shipping, fees, cost, refunds, net
proceeds, realized profit, and margin. Missing fees, unexpected shipping costs, and refund mismatches
create discrepancy records and exceptions.

### Agent authority, API, and CLI

Marketplace MCP scopes are separate from engineering permissions. A scope can be limited to one
account as `marketplace:listing:create@ACCOUNT_UUID`; both MCP and the gateway enforce it. Coding
adapters are rejected if configured with marketplace scopes. The `marketplace.*` tools never return a
session reference, browser object, or buyer/session secret.

```bash
goliath automation status --json
goliath automation stop --reason "Account challenge"
goliath automation start --reason "Challenge resolved"
goliath marketplace account-list --json
goliath marketplace mode ACCOUNT_ID autonomous-normal
goliath marketplace sync ACCOUNT_ID
goliath listing publish APPROVED_DRAFT_ID
goliath listing refresh LISTING_ID
goliath listing promote LISTING_ID
goliath listing end LISTING_ID
goliath listing sync LISTING_ID --json
goliath order sync
goliath offer list
goliath message list
goliath shipping purchase-label TASK_ID
goliath reconcile run
goliath breaker list
```

API routes mirror these commands under `/automation`, `/marketplace-accounts`,
`/marketplace-listings`, `/orders`, `/offers`, `/messages`, `/shipping/tasks`, `/reconciliation`, and
`/circuit-breakers`. Operational writes pass authentication, scope, policy, mode, health,
idempotency, stop, breaker, capability, version, audit, and verification gates and return typed
receipts.

### Deployment, renewal, and troubleshooting

Use the API, worker, and scheduler examples under `systemd/`. Set `GOLIATH_SESSION_KEY` in a root-owned
service environment file, use PostgreSQL in production, place sessions outside the checkout, and
grant that root only to the Goliath service user. Database-persisted stops are visible before every
write; read-only synchronization continues.

- `authentication_required`: renew isolated storage state; do not retry writes.
- `challenged` or `suspended`: resolve the challenge manually; no bypass is attempted.
- `verification_failed`: inspect remote state and idempotency before retrying.
- `rate_limited`: honor reset state and never increase request pressure.
- open breaker: resolve and reconcile the cause, then use `goliath breaker reset`.
- duplicate-sale risk: verify every remote listing is inactive before resetting the item breaker.
- label cost exception: select another service or explicitly adjust the bounded ceiling.
- session-root validation: move sessions outside all agent workspaces and restrict permissions.
