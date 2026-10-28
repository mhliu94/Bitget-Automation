# Robinhood US stocks through the official Trading MCP

This folder handles **US-listed equities** using Robinhood's official endpoint, `https://agent.robinhood.com/mcp/trading`. It replaces the former crypto implementation. Market and limit orders, DAY/GTC, account positions, all today's equity orders, cancellation, tradability and account/order snapshots use the official equity tools. Native FOK/IOC, short opening orders and dedicated LULD auction routing are unsupported and rejected, without substituting another order type. Crypto and options are outside this adapter's scope.

## Authentication and schema discovery

Configure `brokers.robinhood` in the shared config:

```json
{
  "access_token": "${ROBINHOOD_ACCESS_TOKEN}",
  "account_number": "${ROBINHOOD_ACCOUNT_NUMBER}",
  "timezone": "America/New_York",
  "review_before_order": true,
  "trading_enabled": false
}
```

The bearer must be an **OAuth token for Robinhood Trading MCP**. A crypto API key or Ed25519 secret will not work. Connect through the official onboarding flow and choose the dedicated Agentic/MCP account. Only an account which Robinhood reports accessible to this agent can submit or cancel orders. Reads may cover other accounts accessible through the same connection.

The public OAuth metadata advertises authorization-code and refresh-token grants with PKCE S256: authorization `https://robinhood.com/oauth`, token `https://api.robinhood.com/oauth2/token/`, registration `https://agent.robinhood.com/oauth/trading/register`, resource `https://agent.robinhood.com/mcp/trading`, scope `internal`. Obtain and refresh tokens through your authorized OAuth client. These scripts do not register a client, open an account, refresh tokens or reuse private mobile-app credentials. An expired bearer stops execution; update the environment/configuration and reconcile unresolved commands before resuming. Do not paste credentials into Kafka commands.

Install the shared dependencies, including `requests` and `jsonschema>=4.18,<5`. Before enabling trading, export the live contract:

```bash
python robinhood/discover_tools.py --config config.json --execute --output robinhood-tools.json
```

This invokes only MCP initialization and paginated `tools/list`, and exports schemas rather than credentials or account data. Without `--execute` it is an offline plan. Every adapter instance discovers the live tools before use, checks argument names and JSON schemas, and validates response schemas. Changed/missing schemas fail explicitly. No HTTP redirects or automatic retries are permitted; bearer credentials are sent only to the official endpoint.

## Orders and queries

Quantity means shares. Whole-share market/limit and fractional market quantities of up to six decimal places are accepted locally, subject to live account/symbol checks. Fractional limits are rejected. The adapter currently uses the regular session. DAY maps to `gfd`, GTC to `gtc`. A deterministic UUID in `ref_id` preserves identity across retries of the same command. A SELL requires sufficient available long shares and cannot intentionally open a short.

By default `review_before_order=true` runs `review_equity_order` and requires an empty `order_checks` object plus matching ticket fields before submission. A warning, missing result or mismatch blocks the submission. Set this option to `false` only when intentionally delegating pre-trade checks to native order placement; the script then performs account and tradability checks but skips the optional preview. Broker-side in-app trade approvals remain effective. A placement response without a matching order/reference is reported as uncertain, including a proposal awaiting approval; it is never reported as filled or automatically resubmitted.

Positions and order history fetch all pages, passing the server's `next` cursor back verbatim. Today's orders are **created** within the configured timezone's `[midnight, next midnight)` window, across all statuses. Orders created earlier remain included in open-order discovery. Cancellation acknowledgement means accepted, not finally cancelled; subsequent status reads establish the result. Account snapshots publish actual portfolio `cash`, with buying power reported separately.

Tradability reports US listing identity, general trading availability, account-type restrictions, fractional/session flags and any returned internal halt details. Absent halt details mean unknown, not proof the market is open. An account-type restriction such as closing-only is separate from listing availability.

Borrow queries call stock tradability and expose a returned `short_selling_tradability` flag. That is eligibility, **not available borrow inventory**: quantity, fee, easy-to-borrow and locate status remain unknown. Robinhood offers retail short selling in other account workflows, but its current agent trading documentation limits equity orders to long equities and provides no borrow-inventory/rate/locate tool.

LULD reopening intent may submit an ordinary regular-session DAY limit after checks; there is no verified auction-specific route. The default review may block a halted instrument. Neither broker acceptance nor a tradability flag establishes participation in a reopening auction.

## Verification limits and sources

The official catalog confirms the tool names and broad capabilities. Exact arguments were provisionally derived from a developer's recorded server schemas, so authenticated runtime discovery is mandatory. No authenticated requests or trades were performed during development; unit tests use protocol fixtures. Run read-only discovery and positions/tradability checks with your account before enabling order execution.

- [Official external-agent onboarding and OAuth endpoint](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
- [Official equity tool catalog and supported agent order types](https://robinhood.com/us/en/support/articles/trading-with-your-agent/)
- [Official retail short-selling documentation](https://robinhood.com/us/en/support/articles/short-selling/)
- [Public schema capture, recorded October 12, 2026; independent, not broker-published](https://github.com/kevin1chun/robinhood-for-agents/blob/main/docs/official-mcp-tools.json)

Sources inspected October 25, 2026. The public MCP endpoint returned OAuth-required HTTP 401; public discovery metadata was verified. Live account schemas and execution remain account-dependent.
