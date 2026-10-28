# Longbridge US stocks

Uses the HK/SG (AP) account OpenAPI to trade **US stocks only**. This is a distinction between account API region and instrument market. US-domiciled Longbridge accounts use a different API and are not silently routed through AP. Configure app_key, app_secret and access_token; default HTTP origin is `https://openapi.longbridge.com`.

Plain tickers normalize to `.US`, for example AAPL to AAPL.US. Market/limit orders support DAY/GTC here. Native FOK/IOC is unavailable; the shared explicit timed-cancellation policy can be selected separately. Options, warrants, funds and other-market symbols reject. Position reads use stock channels only and do not query fund holdings.

`options.outside_rth` supports `RTH_ONLY`, `ANY_TIME`, or `OVERNIGHT` for eligible orders; extended sessions require limits. The shared boolean extended_hours maps to ANY_TIME/RTH_ONLY, and conflicting settings reject. LULD intent requires RTH_ONLY and an ordinary DAY limit. Neither HK auction instructions nor a special US auction route is substituted.

Positions/order history filter verified US-stock symbols. Historical has_more windows are subdivided; a one-second overflow or unresolvable classification raises an incomplete-result error. Canonical snapshots preserve native cash basis, signed diluted cost, and available_qty; account cash reflects native-currency availability, with reported consolidated totals separate.

Tradability combines static instrument information and quote trade-status fields, while leaving account entitlement/auction routing unknown. Borrow queries expose margin ratios and a sell-capacity estimate separately; these are not lendable inventory. Shortability, ETB, borrow quantity/rate and locate approval stay null where not provided.

Sources: [trade definitions](https://open.longbridge.com/docs/trade/trade-definition), [submission](https://open.longbridge.com/docs/trade/order/submit), [account](https://open.longbridge.com/docs/trade/asset/account), [margin ratios](https://open.longbridge.com/docs/trade/asset/margin_ratio), [capacity estimate](https://open.longbridge.com/docs/trade/order/estimate_available_buy_limit), [quote objects](https://open.longbridge.com/docs/quote/objects).
