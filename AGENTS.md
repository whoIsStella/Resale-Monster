# Resale Goliath agent instructions

## Mission
Build a safe, local-first resale operations platform with interchangeable CLI agents.

## Hard boundaries
- Agents may inspect, draft, test, and recommend.
- Deterministic application code performs live marketplace mutations.
- Never expose marketplace tokens, payout credentials, unrestricted SQL, or buyer messaging to an agent.
- Require human approval for publishing, price changes, offer acceptance, refunds, and buyer contact.
- Keep production data and engineering workspaces separate.

## Engineering expectations
- Python 3.11 or newer.
- Add tests for every behavioral change.
- Prefer typed schemas and explicit interfaces.
- Do not silently weaken sandbox or approval settings.
- Keep commits small and reversible.
