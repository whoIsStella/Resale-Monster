# Resale Goliath

A safe, local-first resale operations foundation with persistent orchestration for Codex CLI,
Hermes, and configurable command-line agents.

Agents can inspect approved files, draft code inside approved engineering workspaces, run approved
commands, and return structured results. They cannot publish listings, change live prices, accept
offers, issue refunds, contact buyers, access payouts, receive marketplace credentials, or execute
unrestricted production SQL.

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
- No live marketplace mutation, buyer messaging, payout handling, or marketplace credential code
  exists in this milestone.

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

Example systemd units for API, worker, and scheduler are provided under `systemd/`; they are
examples only and are never installed automatically.

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
