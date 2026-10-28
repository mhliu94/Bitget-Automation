# Longbridge

This adapter uses the official Longbridge REST protocol for HK/SG (AP data-center) accounts, including trading US-listed securities. It supports market (`MO`) and limit (`LO`) orders with DAY/GTC, all stock and fund positions, and all orders submitted during the configured calendar day. Native FOK and IOC are unavailable and produce `UnsupportedOrder`; no cancel-after-submit approximation is used. US-domiciled Longbridge accounts require different account APIs and are explicitly rejected.

Install the shared requirements or `pip install -r longbridge/requirements.txt`. Place the values from `config.example.json` under `brokers.longbridge` in the shared configuration. The API key, secret and access token are all required for network calls. Tokens must be valid; the scripts do not rotate them. `http_url` supports the official `.com`/`.cn` access points; this does not change account data-center scope. `timezone` defines “today”; use `America/New_York` for US orders or `Asia/Hong_Kong` for HK day boundaries.

Set `journal_account` to a permanent identifier for this account and environment. Preserve it across restarts, credential rotation and routing changes; use a distinct value for each account/environment.

Use canonical symbols such as `AAPL.US` and `700.HK`. `asset_class` supports `equity`/`stock`, single `option`, and `warrant`, subject to account permissions. Optional order fields are `options.outside_rth` (`RTH_ONLY` by default; `ANY_TIME`, `OVERNIGHT`, `OPTION_PRE_MARKET`) and `options.remark` (up to 255 characters). Extended sessions require a limit order. The broker validates instrument eligibility, sessions, price increments and lot sizes.

The shared `extended_hours` boolean maps to `ANY_TIME`/`RTH_ONLY`; conflicting session options are rejected. LULD intent also rejects explicit nonregular session options.

```json
{"id":"lb-limit-001","broker":"longbridge","action":"place_order","symbol":"AAPL.US","side":"buy","quantity":"1","order_type":"limit","time_in_force":"day","limit_price":"100.00"}
```

The root README documents the per-folder command scripts and Kafka worker. `build_order` needs no credentials or connection. Real submissions sign the exact prepared request bytes; they have no automatic retries or redirects. Stable UUID request IDs provide the broker's ten-minute idempotency protection; the shared persistent ledger is also required to guard later replay.

Positions preserve the complete stock/fund response, including account channels. Order queries combine the history window and today's endpoint, keep every status, deduplicate by order ID, and filter submission time to `[start,end)`. History returns at most 1,000 orders; the adapter recursively splits the window when `has_more` is true. If more than 1,000 orders occur in a single second it fails explicitly because the documented endpoint has no cursor.

The account publication mapper reads `/v1/asset/account` and both position categories. It reports `cash_by_currency` from each currency's `available_cash`, with `cash_basis: "available_cash"`; frozen and settling amounts and consolidated reporting-currency totals are retained separately. It does not add converted `total_cash` values to individual-currency balances. US publication symbols omit `.US`, with the native symbol retained in position metadata. Fund positions preserve ISINs and NAV prices; stock costs follow the account's configured average/diluted-cost basis. Missing cash or position cost fails the snapshot rather than becoming zero. Order publication uses cumulative `executed_quantity` and `executed_price`; a missing fill field triggers an exact-order detail query.

`capabilities` lists implemented and other broker order types. `tradability` queries static information and the live quote status through the HTTP equivalents in the official SDK. A halt/Fuse status is reported; a normal quote leaves account tradability unknown. Missing status stays unknown. Quotes require the corresponding OpenAPI market-data permissions and may be delayed; this is a snapshot, not a halt monitor.

No documented API instruction guarantees US LULD reopening-auction participation. The common `auction_intent=luld_reopening` path submits an ordinary US DAY limit order, subject to broker acceptance and routing. HK `AO`/`ALO` auction types, `ELO`, and `SLO` are distinct HK instructions and are not implemented as US reopening orders. FOK cannot wait for an auction.

`borrow_availability` queries margin ratios and, for `.US` symbols, a market-order sell-capacity estimate. The result preserves `cash_max_qty`/`margin_max_qty` under `raw.sell_capacity_estimate`; these are **not lendable inventory**. `shortable`, `easy_to_borrow`, `available_quantity`, `fee_rate`, and `locate_required` remain null because the documented retail OpenAPI does not expose those borrow-specific values. No shares are located, reserved or borrowed by this query. Longport Whale's stock-lending APIs are a separate product/authentication contract and are not called with retail keys.

Official references checked 2026-10-25:

- [Order submission](https://open.longbridge.com/docs/trade/order/submit) and [order definitions](https://open.longbridge.com/docs/trade/trade-definition).
- [Today's orders](https://open.longbridge.com/docs/trade/order/today_orders), [history and the 1,000-order limit](https://open.longbridge.com/docs/trade/order/history_orders), [stock positions](https://open.longbridge.com/docs/trade/asset/stock), [fund positions](https://open.longbridge.com/docs/trade/asset/fund).
- [Signature implementation](https://github.com/longbridge/openapi/blob/main/rust/crates/httpclient/src/signature.rs), [quote HTTP paths](https://github.com/longbridge/openapi/blob/main/rust/src/quote/cmd_code.rs), [security status codes](https://open.longbridge.com/docs/quote/objects), [US halt information](https://longbridge.com/sg/investment-products/trade-us-stocks).
- [Margin ratios](https://open.longbridge.com/docs/trade/asset/margin_ratio) and [account purchase/sell estimates](https://open.longbridge.com/docs/trade/order/estimate_available_buy_limit).
