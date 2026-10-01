# Resale Goliath

I built this around a resale workflow question: how can software agents help with inventory, pricing, and listings without receiving the marketplace's credentials or unrestricted execution power? Goliath puts typed services, durable jobs, and account policy between a proposal and an external action.

**Working backend, still under development.** FastAPI, PostgreSQL, SQLAlchemy/Alembic, a Typer CLI, and MCP are implemented. Offline tests exercise the workflow and failure paths; live marketplace behavior is a separate, opt-in test layer.

## The interesting engineering

- Jobs survive process restarts: workers claim leases, heartbeat, retry, cancel, and recover work through versioned state transitions.
- Inventory, comparables, pricing, drafts, and approvals use typed workflows rather than arbitrary SQL or generic account mutation.
- Human API principals, MCP service principals, workers, and marketplace accounts have distinct identities and scopes.
- Marketplace writes pass through policy, idempotency, account health, circuit breakers, emergency stops, and read-back verification.
- Marketplace sessions stay behind the broker and adapters. Agent subprocesses receive an allowlisted environment.

## Boundaries

```mermaid
flowchart TB
    ENTRY["CLI / API / MCP"] --> SERVICE["Typed services and policy"]
    SERVICE --> DB["PostgreSQL and audit events"]
    SERVICE --> JOB["Durable jobs and leased workers"]
    JOB --> AGENT["Configured agent subprocess"]
    SERVICE --> GATE["Marketplace gateway"]
    GATE --> ADAPTER["Session broker and typed adapter"]
```

The MCP interface has no generic shell, SQL, cookie, or browser-profile tool.
Configured coding agents can run commands inside their allowed workspace;
permissions passed to an agent are not an OS sandbox. The adapter, host
permissions, and deployment isolation still matter.

Leases coordinate claims and completion; they are not an exactly-once guarantee
for external effects. Idempotency and verification address that separate problem.

## Run locally

Python 3.11+ and PostgreSQL are required for the normal application. SQLite is used only by the isolated test layer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pip install 'psycopg[binary]>=3,<4'

cp config/agents.example.yaml config/agents.yaml
export GOLIATH_CONFIG="$PWD/config/agents.yaml"
export GOLIATH_DATABASE_URL='postgresql+psycopg://user:password@localhost/resale_goliath'
# Create that database and edit workspace_roots/agent commands before proceeding.
alembic upgrade head
goliath agent list
goliath agent doctor
```

Keep credentials in the application environment, outside agent workspaces.
Start the API with `python -m goliath.api_runner`. Example API, worker, and
scheduler service units live in [systemd/](systemd/).

## Tests and code worth reading

```bash
pytest
ruff check .
```

Default tests exclude sandbox and live marketplace operations. Offline SQLite
coverage does not establish PostgreSQL locking behavior or live adapter reliability.

| Path | What to inspect |
| --- | --- |
| [goliath/orchestration/](goliath/orchestration/) | Worker leases, job lifecycle, retries, and recovery |
| [goliath/marketplace/gateway.py](goliath/marketplace/gateway.py) | Policy, idempotency, breakers, and receipts |
| [goliath/marketplace/broker.py](goliath/marketplace/broker.py) | Session boundary |
| [goliath/mcp/](goliath/mcp/) | Bounded tools and scoped principals |
| [tests/](tests/) | Lifecycle, supervisor, API, domain, and marketplace tests |
| [docs/ebay-adapter-design.md](docs/ebay-adapter-design.md) | eBay adapter design and limitations |

Browser-backed adapters need `pip install -e '.[browser]'` and
`playwright install chromium`. They remain subject to account policy and
marketplace-specific behavior. This is not a claim of production readiness
across every marketplace.

The repository's older name is `Resale-Monster`; the application/package is
`resale-goliath` / `goliath`.
