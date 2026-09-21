# Resale Goliath

Resale Goliath is a resale operations platform built around typed workflows, durable job execution, marketplace adapters, and tightly scoped agent access.

The system separates engineering work, resale-domain logic, and live marketplace operations. Agents can research, draft, analyze, and request actions, but credentials and privileged marketplace operations stay behind application-controlled interfaces.

## What it includes

- FastAPI API and Typer CLI
- PostgreSQL, SQLAlchemy, and Alembic migrations
- durable jobs, workers, leases, retries, cancellation, and recovery
- typed inventory, research, pricing, listing, approval, and review workflows
- MCP tools with scoped service principals
- image ingestion and metadata processing
- configurable marketplace adapters
- audit events, idempotency, circuit breakers, and emergency stops
- unit and integration test layers

## Architecture

```text
CLI / API / MCP
      |
      v
typed services and policy checks
      |
      +--> PostgreSQL
      |
      +--> durable job queue --> worker --> supervised agent process
      |
      +--> marketplace gateway --> isolated adapter --> marketplace
```

The application does not expose generic SQL, shell access, browser profiles, marketplace credentials, or unrestricted browser controls to agents.

## Job lifecycle

```text
pending -> queued -> running -> succeeded
   |          |         |-----> failed
   |          |         |-----> timed_out
   +----------+---------+-----> cancelled
```

Jobs use versioned compare-and-swap updates. Workers claim queued jobs with leases so competing workers cannot execute the same job at the same time.

## Setup

Requirements:

- Python 3.11+
- PostgreSQL for normal development and production use
- optional Playwright/Chromium support for marketplace adapters

Create a virtual environment and install the project:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Create the agent configuration:

```bash
cp config/agents.example.yaml config/agents.yaml
export GOLIATH_CONFIG="$PWD/config/agents.yaml"
export GOLIATH_DATABASE_URL='postgresql+psycopg://user:password@localhost/resale_goliath'
alembic upgrade head
```

Edit `workspace_roots` and agent settings in `config/agents.yaml` for the machine running Goliath.

Database credentials belong in the application environment, not in agent configuration or prompts.

## Basic CLI use

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

Most CLI commands also support `--json`.

## Resale-domain workflows

The domain layer covers inventory, measurements, media processing, product research, comparable review, pricing, listing drafts, approvals, marketplace synchronization, offers, messages, orders, shipping, refunds, and reconciliation.

Inventory and draft updates are versioned. Agent-generated proposals do not bypass the service layer.

## Marketplace operations

Marketplace access is isolated behind typed adapters and account policy.

Accounts can operate in observe, shadow, paused, or explicitly enabled autonomous modes. Writes pass through scope, policy, health, idempotency, breaker, and verification checks before execution.

Session state is kept outside agent workspaces. Agents do not receive browser cookies, marketplace tokens, payout access, or raw session data.

## MCP

The MCP layer exposes bounded tools for inventory, research, pricing, listings, approvals, reviews, comparables, and enabled marketplace operations.

Human API principals, MCP service principals, workers, and marketplace accounts use separate identities and scopes.

Example configuration files are under `config/`.

## Development

```bash
pytest
ruff check .
alembic upgrade head
```

The default pytest configuration skips opt-in sandbox and live marketplace tests.

Test markers include:

- `unit`
- `integration_fake`
- `integration_sandbox`
- `integration_live_read`
- `integration_live_write`

Live and sandbox tests are opt-in.

## Deployment

Example systemd units are in `systemd/` for the API, worker, and scheduler.

For browser-backed adapters:

```bash
pip install -e '.[browser]'
playwright install chromium
```

Use PostgreSQL, keep session storage outside the repository and agent workspaces, and keep secrets in the service environment.

## Security boundaries

- no unrestricted SQL or shell surface is exposed to agents
- secrets are rejected from agent environment allowlists
- credentials stay behind dedicated application boundaries
- session state is not returned through API, MCP, logs, or audit events
- privileged marketplace writes require explicit policy and scope
- emergency stops and circuit breakers block writes while allowing safe reads
- destructive or ambiguous resale actions are escalated instead of guessed

## Repository layout

```text
goliath/      application code
config/       example configuration
migrations/   Alembic migrations
systemd/      example service units
tests/        unit and integration tests
docs/         design notes and examples
```

The repository name predates the current project name. The Python package and application are named `resale-goliath` and `goliath`.
