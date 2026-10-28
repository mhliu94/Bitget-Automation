# Alpaca

This group uses the official Trading API v2 with API-key authentication. The default endpoint is **paper trading**. Set `base_url: https://api.alpaca.markets` explicitly for a live account and use its matching keys.

Configuration under `brokers.alpaca`:

```yaml
api_key: ${ALPACA_API_KEY}
api_secret: ${ALPACA_API_SECRET}
base_url: https://paper-api.alpaca.markets
timeout: 20
```

Use the root README for installation, Kafka consumption, and command-line invocation. No Alpaca SDK is required; the shared HTTP transport uses `requests`.

The example above shows fields for readability; the application loads JSON. Copy the object in [config.example.json](config.example.json) into your root configuration's `brokers.alpaca` entry.

Before submitting, set `journal_account` to a stable label for this exact account and paper/live environment, such as `alpaca-paper-my-account`. Preserve it across key rotations and routing changes, and retain the SQLite journal. Keep separate labels for different accounts/environments.

Example normalized command:

```json
{"id":"alpaca-demo-001","broker":"alpaca","action":"place_order","symbol":"AAPL","side":"buy","quantity":"2","order_type":"limit","time_in_force":"fok","limit_price":"150.25","asset_class":"us_equity"}
```

Market orders use `order_type: market` without `limit_price`. `fok` is a time-in-force applied to a market or limit order. The adapter sends native `time_in_force: fok`; it never substitutes IOC or a cancellation strategy.

| Asset | Market/limit | Time in force |
| --- | --- | --- |
| Whole-share US equity | Yes | day, gtc, ioc, fok |
| Fractional US equity | Yes | day |
| Crypto | Yes | gtc, ioc |
| Single-leg US option | Yes | day, gtc; whole contracts |

Alpaca documents IOC/FOK equity availability as requiring sales contact for relevant accounts. API enablement alone does not guarantee that entitlement. The broker checks symbol tradability and route/session restrictions when submitting. OTC FOK is unavailable. For crypto, specify `asset_class: crypto` and a symbol such as `BTC/USD`; crypto FOK is rejected before any request. The option class is `us_option`.

Supported `options`: boolean `extended_hours` (equity limit with day/gtc only), and `position_intent` (`buy_to_open`, `buy_to_close`, `sell_to_open`, `sell_to_close`, consistent with side). Unknown options fail validation. Quantities and prices remain decimal strings; unsupported equity price precision is rejected instead of rounded.

`positions` returns every current position in the key's account. `orders_today` includes all statuses for orders **submitted during the configured calendar day**; it does not select older orders solely because they filled today. It uses 500-record pages with ID cursors, preserving orders sharing a submission timestamp. `max_pages` defaults to 10000; reaching it or a repeated cursor raises an incomplete-result error.

Order IDs are deterministic from command IDs. A submission is never automatically retried after a timeout. Reconcile an uncertain result with the broker before any new submission.

## Capabilities, symbol checks, and LULD

`action: capabilities` returns an offline matrix. `order_types` identifies the implemented market/limit types; `broker_order_types` also reports the broker's documented stop variants, which these scripts do not submit. `action: tradability` with a symbol fetches the official asset or option-contract record. `instrument_tradable` reflects broker eligibility, while `tradable` remains `null` when current execution availability cannot be established. An inactive or explicitly nontradable instrument returns `false`. `halted` is `null`: asset eligibility is not a real-time regular-session halt check. Missing instruments and API errors are explicit failures, not assumed tradable.

Alpaca provides stock-data WebSocket `statuses`, `lulds`, and `imbalances` channels subject to data entitlements. This REST adapter does not maintain that live stream or claim a current LULD state. Its ordinary equity limit DAY order can be tagged with `auction_intent: luld_reopening` in the shared CLI to validate the intent; that tag does not create a dedicated broker auction instruction. Actual acceptance and participation depend on Alpaca's routing and the primary exchange. FOK cannot rest waiting for reopening. OPG refers to the opening-session auction and is not an LULD reopening instruction. No broker-confirmed guarantee of halt-auction access was found in the cited API documentation.

## Borrow availability

`action: borrow_availability` with an equity symbol reads its asset record. The adapter prefers the current `borrow_status` field (`easy_to_borrow` or `hard_to_borrow`); the deprecated boolean `easy_to_borrow` is only a fallback for older responses. `shortable` reports the broker's instrument flag, not proof of account approval or reserved inventory.

For HTB assets on the live endpoint, it also reads `GET /v1/locates/quotes`. When returned, `quantity` is advisory `available_qty`, `locate_fee_per_share` is the quote's price, and `quoted_at` identifies the observation. A locate fee is distinct from a daily borrow rate, so `fee_rate` stays `null`. Quotes do not reserve shares and the scripts never request a paid locate. HTB classification sets `locate_required: true`; ETB sets it to `false` for the separate customer locate workflow. An approved locate is still necessary before opening an HTB short.

The locates API is unavailable in paper trading; paper queries return classification with unknown quantity and price. Missing live quote permission or a per-symbol error also leaves unavailable fields `null` with an explanation and raw evidence. ETB does not imply unlimited stock inventory, and quantity is never invented. Option contracts and crypto do not use this equity locate workflow.

Official references checked on 2026-10-25:

- [Create order](https://docs.alpaca.markets/us/reference/postorder)
- [Order types, time in force, and restrictions](https://docs.alpaca.markets/us/docs/orders-at-alpaca)
- [List orders and ID pagination](https://docs.alpaca.markets/us/reference/getallorders-1)
- [Positions](https://docs.alpaca.markets/us/reference/getallopenpositions)
- [Asset lookup](https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id)
- [Option-contract lookup](https://docs.alpaca.markets/us/reference/get-option-contract-symbol_or_id)
- [Trading-status, LULD, and imbalance streams](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data)
- [NYSE auction eligibility and mechanics](https://www.nyse.com/trade/auctions)
- [Current short-selling and locate workflow](https://docs.alpaca.markets/us/docs/margin-and-short-selling)
- [Locate quote endpoint](https://docs.alpaca.markets/us/reference/listlocatequotes)
- [Borrow-status migration](https://docs.alpaca.markets/us/changelog/2026-06-05-borrow-status-6b96a5a)
- [Locates are unavailable in paper trading](https://docs.alpaca.markets/us/changelog/2026-09-10-locates-paper-trading-5658e38)
