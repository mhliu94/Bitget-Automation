# Webull

This adapter uses the official `webull-openapi-python-sdk` US Trading API. It supports market/limit equity, futures and crypto orders, and limit event-contract orders. Native FOK is available **only for event contracts**; stock/futures/crypto FOK requests fail explicitly. Equities and futures support DAY/GTC. Crypto market orders require IOC; crypto limit orders require DAY/GTC. Event orders support DAY/GTC/IOC/FOK here; the broker's GTD expiry instructions are not implemented. Option strategies are outside this adapter's order schema; position/order queries still return all instruments in the configured account.

Install with `pip install -r webull/requirements.txt`. The supported SDK range is `>=3.0.4,<4`, which includes the current cursor-based query methods. Do not install the unrelated unofficial `webull` package. Keep this directory without `__init__.py` so it does not hide the official SDK.

Place `config.example.json` under `brokers.webull` in the shared configuration. Required values are `app_key`, `app_secret`, and `account_id`. `api_endpoint` is a hostname: `api.webull.com` for production, or `api.sandbox.webull.com` with appropriate sandbox credentials. `region` must be `us`. `timezone` controls “today.” Optional `token_dir` selects the SDK's 2FA cache directory; optional `access_token` supplies a current token. The SDK manages Webull's required authorization/2FA flow. Each account type uses its own account ID; configure the account that owns the desired assets.

Set `journal_account` to a permanent identifier for this account and environment. Preserve it across restarts, credential rotation and routing changes; use distinct values for production and sandbox.

Optional equity field `options.support_trading_session` is `CORE` by default, or `ALL`/`NIGHT` for eligible limit orders. Event orders require `asset_class:"event"`, the actual event market symbol, `options.event_outcome:"yes"` or `"no"`, a price below 1, and quantity with at most two decimal places. Futures quantities must be integral.

The shared `extended_hours` boolean maps to `ALL`/`CORE`; conflicting session options are rejected. LULD intent also rejects explicit non-CORE session options.

```json
{"id":"wb-fok-001","broker":"webull","action":"place_order","asset_class":"event","symbol":"REPLACE-WITH-ACTUAL-EVENT-MARKET","side":"buy","quantity":"1","order_type":"limit","time_in_force":"fok","limit_price":"0.25","options":{"event_outcome":"yes"}}
```

The root README documents the per-folder command scripts and Kafka worker. Dry runs validate and build payloads without importing the SDK or authenticating. Stable 32-character client IDs identify submissions; automatic SDK retries are disabled. An uncertain submission raises an explicit error and must be reconciled before replay.

The account publication mapper uses each `account_currency_assets[].cash_balance` and the native positions list; buying power is not cash. Missing cash values, including empty values returned for some `EVENTS_CASH` accounts, fail the snapshot rather than publishing zero. Contract market values are not inferred from quantity and price because multipliers and strategy legs can differ. Order snapshots use `total_quantity`, `filled_quantity`, and `filled_price`; missing fill information triggers a read-only exact-order detail query. The broker's list/detail schema has no general update timestamp, so order publication uses observation time instead of mislabeling the last-fill time as a cancellation time.

Positions use the account-wide position endpoint. Today's orders combine all pages from `list_order_history` and `list_order_open`, retain all statuses, flatten order groups while retaining their group ID, deduplicate by order ID, and filter placement time to `[start,end)`. `query_interval` defaults to 1.1 seconds between query pages. Repeated cursors or reaching `max_query_pages` cause an error, never an apparently complete partial result. The broker documents processing delays in both order lists; use its order-detail API for time-critical reconciliation.

`capabilities` reports the asset-specific order types and limitations. `tradability` queries the stock-instrument profile: `OC` means broker-listed tradable, `NT` nontradable, and `CO` close-only. Live halt status and account-specific permission remain unknown; existence alone is not treated as tradability. This check is currently for stocks/ETFs, not event/crypto/futures discovery.

No documented retail API field guarantees US LULD reopening-auction participation. The common `auction_intent=luld_reopening` path submits an ordinary CORE DAY limit order, subject to broker acceptance and routing. Institutional opening/closing instructions are separate capabilities; this adapter does not reinterpret them as LULD orders. Equity FOK is unsupported.

`borrow_availability` reads the stock profile's `shortable` and `easy_to_borrow` flags. Missing flags remain null. `available_quantity`, `fee_rate`, and `locate_required` remain null because these values are not exposed by this US retail endpoint; HTB is not converted into an invented locate requirement. A shortable flag is not a share reservation, live inventory guarantee or account-specific approval. The documented Webull HK institutional Broker API stock-locate endpoint is a different service and is not called with US retail credentials.

Official references checked 2026-10-25:

- [Order schema and TIF by asset](https://developer.webull.com/apis/docs/reference/common-order-place/), [event contracts](https://developer.webull.com/apis/docs/trade-api/event-contract/), [SDK/environments](https://developer.webull.com/apis/docs/sdk/).
- [Official query implementation](https://github.com/webull-inc/webull-openapi-python-sdk/blob/main/webull/trade/trade/v3/order_operation_v3.py), [September 2026 cursor migration](https://developer.webull.com/apis/docs/changelog/), [order history](https://developer.webull.com/apis/docs/reference/order-history/), [stock instruments](https://developer.webull.com/apis/docs/reference/instrument-list/).
- [Webull LULD explanation](https://www.webull.com/learn/courseware/CNsYOv/Limit-Up-Limit-Down).
