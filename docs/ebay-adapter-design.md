# eBay adapter design

## Scope

The first production adapter targets eBay using the supported REST Sell APIs.

The repository already models eBay accounts, approved marketplace variants, remote listings, orders, fulfillment, idempotent operations, and durable polling. The adapter uses the API integration rather than the older browser skeleton.

## Integration profile

| Concern | Decision |
| --- | --- |
| Supported method | Official eBay REST APIs: Identity, Account, Inventory, and Fulfillment |
| Test environment | Official eBay Sandbox with a separate sandbox keyset and endpoints |
| Authentication | OAuth 2.0 authorization-code grant for user access. State is single-use and callback-bound. The seller flow requires a confidential-client secret and exact RuName redirect. |
| Token lifecycle | Access and refresh tokens are stored under authenticated encryption. Refresh occurs inside the broker boundary. Expiry, revocation, corruption, and identity mismatch fail closed. |
| Rate limits | Local per-account and per-operation gates plus eBay response headers and `Retry-After`. Throttled jobs are deferred by the durable worker. |
| Webhooks and polling | Durable incremental polling is the baseline. Notification API ingestion is accepted only after ECC signature verification is configured. Unknown events are quarantined and do not execute writes inline. |
| Operational risks | Seller policy prerequisites, category-specific fields, account selling limits, eventual consistency after updates, OAuth revocation, API quotas, and account suspension |
| Policy constraints | Inventory API listings remain managed through that API. Production writes require approval, canary limits, emergency-stop checks, idempotency, and verification. |

## Declared capabilities

Supported reads include authentication validation, account identity, seller privileges and limits, and payment, fulfillment, and return-policy discovery.

Supported listing operations include fixed-price listing creation through inventory item, offer, and publish calls; offer and price updates; quantity updates; withdrawal; listing reads; and reconciliation.

Supported order operations include incremental order listing, single-order retrieval, paid-sale detection, fee projection from order totals, fulfillment-state reads, and tracking upload.

Offer negotiation, buyer messaging, promotion, sharing, refresh/relist, label purchase, refunds, payout retrieval, and raw notification payload access are outside the initial adapter surface. Unsupported operations return typed non-retryable results.

## Publication mapping

An approved local variant maps to a deterministic seller SKU, an Inventory API inventory item, and an offer that references configured merchant location, payment, fulfillment, and return-policy identifiers.

Before creation, the adapter checks for the deterministic SKU and offer. After publish, it reads the offer and inventory item back for verification.

The adapter does not invent missing category, condition, location, or policy values, and it does not modify approved title, description, price, or quantity.
