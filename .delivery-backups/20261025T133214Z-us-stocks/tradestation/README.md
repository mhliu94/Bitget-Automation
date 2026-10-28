# TradeStation

This group uses the official v3 API and defaults to the **SIM** endpoint. Set `base_url: https://api.tradestation.com/v3` explicitly for live trading. Account IDs and tokens must belong to the selected environment.

Configuration under `brokers.tradestation`:

```yaml
account_id: ${TRADESTATION_ACCOUNT_ID}
base_url: https://sim-api.tradestation.com/v3
client_id: ${TRADESTATION_CLIENT_ID}
client_secret: ${TRADESTATION_CLIENT_SECRET}
refresh_token: ${TRADESTATION_REFRESH_TOKEN}
timeout: 20
```

Alternatively configure `access_token` alone for short-lived sessions. TradeStation requires OAuth authorization in addition to an API key. Obtain the refresh token through its authorization-code flow with appropriate trading/account scopes and `offline_access`; PKCE applications omit `client_secret`. The adapter refreshes before expiry and caches tokens in memory. Optional `access_token_expires_at` is a Unix timestamp. A rotating refresh token is retained only for the current process; installations using token rotation must provision a current token when restarting, or integrate their own secure token store. Standard nonrotating refresh tokens avoid that restart issue.

The application loads JSON. Copy [config.example.json](config.example.json) into `brokers.tradestation`; the YAML above only illustrates the fields. For access-token-only authentication, remove `client_id`, `client_secret`, and `refresh_token` placeholders rather than leaving unrelated missing environment variables.

Before submitting, set `journal_account` to a stable label for this exact account and SIM/live environment. Keep it unchanged across token rotations or worker-routing changes, and preserve the SQLite journal. Different accounts/environments need different labels.

Queries use `account_ids` (a list or comma-separated string) if configured, otherwise `account_id`. Submissions always use the single `account_id`. Queries are batched across all configured accounts. Optional `route` chooses a broker route; otherwise TradeStation chooses its default. `requests` is the only broker-specific runtime dependency.

Use the root README for installation and CLI/Kafka commands. A normalized FOK command is:

```json
{"id":"tradestation-demo-001","broker":"tradestation","action":"place_order","symbol":"AAPL","side":"buy","quantity":"2","order_type":"limit","time_in_force":"fok","limit_price":"150.25","asset_class":"equity"}
```

The adapter sends `OrderType: Limit` and `TimeInForce.Duration: FOK`. Market orders omit `limit_price`. Supported durations are day, gtc, ioc, and fok; the broker determines availability for each asset, route, and session. Unsupported FOK is never converted to another execution policy.

Asset classes are `equity` (default), `futures`, and `option`; quantities must be whole units. For equity shorts specify `options.trade_action: SELLSHORT`, and for covering specify `BUYTOCOVER`. Options require explicit `BUYTOOPEN`, `BUYTOCLOSE`, `SELLTOOPEN`, or `SELLTOCLOSE`, matching the command side. This adapter supports single-leg orders. `options.route` overrides the configured route. Other options fail validation.

`positions` returns every position in the configured accounts. `orders_today` combines current and historical endpoints, including open and completed orders **opened during the configured calendar day**. Older GTC orders filled today are outside this definition. Both endpoints use explicit `pageSize: 600` and `NextToken` pagination, then merge by account/order ID. The history date filter is widened across timezone boundaries before precise local-day filtering. Errors embedded in HTTP-200 responses fail the query, avoiding a misleading partial result. `max_pages` defaults to 10000; exceeding it raises an error.

The deterministic `OrderConfirmID` enables broker duplicate checks. Submissions are never retried automatically; reconcile uncertain acknowledgements before resubmitting.

## Capabilities, symbol checks, and LULD

`action: capabilities` reports the implemented market/limit order types and durations. The separate `broker_order_types` field also lists the documented stop variants that this adapter does not implement. `action: tradability` with a symbol fetches symbol metadata and a quote. It reports `MarketFlags.IsHalted`, `IsDelayed`, and raw records. An explicitly nondelayed `IsHalted: true` makes `tradable: false`; otherwise tradability remains unknown because symbol existence and quotes do not establish account or order eligibility. Delayed or missing flags produce `halted: null`. Market-data permissions are required; errors are reported explicitly.

For a proposed LULD reopening order, the shared command accepts `auction_intent: luld_reopening` only with an ordinary US equity limit DAY order. `options.route` maps directly to the documented `Route` field; use a broker-confirmed route. No dedicated LULD instruction is sent and no specific auction participation is guaranteed. FOK cannot rest waiting for reopening. The quote's halt flag does not identify the halt reason or ensure primary-exchange auction eligibility. Verify that the broker accepts and routes the chosen order into the intended reopening auction before relying on it; neither SIM fills nor an unhalted quote establish that behavior.

## Borrow availability

`action: borrow_availability` with a stock symbol reads quote `MarketFlags.IsHardToBorrow` and the configured accounts' `AccountDetail.IsStockLocateEligible` from `GET /brokerage/accounts`. The public v3 specification checked on 2026-10-25 does not document borrow-share inventory, stock-borrow fee rates, or a locate reservation endpoint. Those fields are returned as `null` with the limitation stated explicitly.

An HTB flag is not a locate approval. A false HTB flag does not establish ETB inventory or shortability. Accordingly, `shortable`, `quantity`, `fee_rate`, and `locate_required` remain unknown; `easy_to_borrow` is false only when a nondelayed quote affirmatively reports HTB, otherwise unknown. Account stock-locate eligibility is reported separately and does not establish whether a specific short order can be placed. Delayed quote flags remain available as raw evidence but are not promoted to current availability. Account lookup failure preserves the quote result with an explicit error. The query creates no locate and incurs no locate commitment.

Official references checked on 2026-10-25:

- [Current v3 specification: orders, positions, durations, pagination](https://api.tradestation.com/docs/specification/)
- [SIM and live endpoints](https://api.tradestation.com/docs/fundamentals/sim-vs-live/)
- [OAuth refresh tokens](https://api.tradestation.com/docs/fundamentals/authentication/refresh-tokens/)
- [NYSE auction eligibility and mechanics](https://www.nyse.com/trade/auctions)
