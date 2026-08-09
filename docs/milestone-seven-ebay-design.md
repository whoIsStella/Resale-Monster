# Milestone Seven marketplace design: eBay

## Selection

The first production adapter is **eBay**, using eBay's supported REST Sell APIs. This is the
strongest fit because the repository already models eBay accounts, approved marketplace
variants, remote listings, orders, fulfillment, idempotent operations, and durable polling.
The implementation does not use the eBay browser skeleton.

## Integration profile

| Concern | Decision |
| --- | --- |
| Supported method | Official eBay REST APIs: Identity, Account, Inventory, and Fulfillment |
| Test environment | Official eBay Sandbox, with a separate sandbox keyset and endpoints |
| Authentication | OAuth 2.0 authorization-code grant for user access; state is single-use and callback-bound; eBay does not document PKCE for this seller flow, so the confidential-client secret and exact RuName redirect are required |
| Token lifecycle | A short-lived access token and long-lived refresh token are stored together under authenticated encryption; refresh occurs inside the broker boundary; rotation increments a credential version; expiry, revocation, corruption, and identity mismatch fail closed |
| Rate limits | Local per-account/per-operation gates plus eBay response headers and `Retry-After`; throttled jobs are deferred by the durable worker rather than sleeping |
| Webhooks/polling | Durable incremental polling is the baseline. eBay Notification API ingestion is accepted only after ECC signature verification is configured; unknown events are quarantined and never execute writes inline |
| Operational risks | Seller/business-policy prerequisites, category-specific required fields, account selling limits, eventual consistency after publish/update, OAuth revocation, per-API quotas, and eBay account suspension |
| Policy risks | Inventory API listings must continue to be managed through that API; production writes require approval, canary limits, emergency-stop checks, idempotency, and verification |

## Declared capabilities

Supported account reads are authentication validation, account identity, seller privileges/limits,
and payment/fulfillment/return policy discovery. Supported listing operations are fixed-price
listing creation through inventory-item + offer + publish, offer update (including price), quantity
update, withdrawal/end, single/list reads, and reconciliation. Supported order operations are
incremental order listing, single-order retrieval, paid-sale detection, fee projection from order
line totals, fulfillment-state reads, and tracking upload.

The first adapter deliberately declares offer negotiation, buyer messaging, promotion/share,
refresh/relist, label purchase, refunds, payout/settlement retrieval, and raw notification payload
access as unsupported or manual. eBay may expose some of these through other APIs or restricted
programs, but they are outside this adapter's initial production-capable surface. Unsupported
operations return typed non-retryable results.

## Publication mapping

An approved local variant maps to a deterministic seller SKU, an Inventory API inventory item,
and an offer referencing explicitly configured merchant location and payment, fulfillment, and
return policy identifiers. The adapter checks for the deterministic SKU/offer before creating,
publishes the offer, then reads the offer and inventory item back. It never invents missing
category, condition, location, or policy values and never modifies approved title, description,
price, or quantity.
