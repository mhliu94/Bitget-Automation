# Borrow availability API coverage

All seven groups expose `borrow_availability.py --symbol SYMBOL --execute` and the Kafka action `QUERY_BORROW_AVAILABILITY`. These are read-only checks. `available_quantity`, `shortable`, `easy_to_borrow`, `fee_rate`, and `locate_required` are common response fields. Unexposed or unverified values are JSON `null`. Native evidence and limitations are retained alongside those fields. No query buys a locate, borrows coins, opens a short, or reserves inventory.

| Broker | Verified interface used | Returned information | Not established |
| --- | --- | --- | --- |
| Bitget | Optional UTA `GET /api/v3/account/max-borrowable?coin=...` | Account's maximum manual coin borrowing in coin units | Equity stock borrow, recurring fee, or inventory reservation; default Classic spot scope is not applicable |
| Robinhood | Current official Crypto API reviewed | Explicit unsupported-in-crypto-API result | Stock/coin borrowing or locate inventory through this adapter; Robinhood's other products are distinct |
| Alpaca | Asset record, live HTB `GET /v1/locates/quotes` | Shortable/borrow classification; available HTB quote shares, per-share locate price and quote timestamp when authorized | Daily stock-borrow rate, account approval or reservation; paper has no locates API |
| TradeStation | Quote `MarketFlags`, selected account `AccountDetail` | Nondelayed HTB flag and account stock-locate eligibility | Available shares, confirmed symbol shortability, borrow fee or locate approval |
| Longbridge | Margin ratio and US account sell-capacity estimate | Raw margin ratios and sell-capacity estimates | Actual borrow inventory, fees, ETB/shortable flag or locate approval; sell capacity is not lendable inventory |
| Webull | US retail stock profiles | `shortable` and `easy_to_borrow` flags | Available shares, fee rate or locate confirmation; institutional/HK APIs are different products |
| Zhuorui | Pinned reference client reviewed | Explicit unverified-API result | No verified borrow/locate endpoint in that reference; owned holdings are not borrowed inventory |

Alpaca's implementation uses its current `borrow_status` field, with the old ETB boolean only as a compatibility fallback. An HTB quote's `price` is returned as `locate_fee_per_share`, never as a daily `fee_rate`. Advisory quote quantities are not reservations. Failed/unauthorized quote retrieval leaves quantity/price unknown while retaining classification and the error. See [Alpaca's current short-selling workflow](https://docs.alpaca.markets/us/docs/margin-and-short-selling), [locate quotes](https://docs.alpaca.markets/us/reference/listlocatequotes), and [paper-trading limitation](https://docs.alpaca.markets/us/changelog/2026-09-10-locates-paper-trading-5658e38).

For Bitget, set `borrow_scope: "uta"` only for credentials authorized to query that separate account product, and use a coin such as `BTC`, not a pair such as `BTCUSDT`. Optional `borrow_coin_map` can explicitly map your control's symbols to coin codes. This setting does not convert the Classic spot trading adapter into a UTA/margin execution adapter. See [Bitget's max-borrowable API](https://www.bitget.com/docs/catalog/account/margin-borrow).

Other references: [TradeStation v3 specification](https://api.tradestation.com/docs/specification/), [Longbridge margin ratios](https://open.longbridge.com/docs/trade/asset/margin_ratio), [Longbridge capacity estimates](https://open.longbridge.com/docs/trade/order/estimate_available_buy_limit), [Webull US profiles](https://developer.webull.com/apis/docs/reference/instrument-list/), [Robinhood Crypto API](https://docs.robinhood.com/crypto/trading/), and the [pinned Zhuorui client](https://github.com/mhliu94/Zhuorui-Securities-Automation/blob/002aa6866b246dbc4c1f5e1f647019abc170080d/zhuorui/api/client.py). Broker-specific READMEs contain further setup and scope details.

Example control message:

```json
{"id":"borrow-check-001","account_num_id":3,"action":"QUERY_BORROW_AVAILABILITY","symbol":"AAPL"}
```

The response envelope contains `action: "borrow_availability"`, `status: "ok"` for a completed query, and a `data` object. A successful query with null quantity is not a failed query and is not zero availability. A broker access/API failure yields an error, except for explicitly documented partial results such as a classification obtained before a locate quote fails. Controls should show the explanation and observation timestamp rather than collapsing unknown, unsupported, false and zero into the same state.
