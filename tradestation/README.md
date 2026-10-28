# TradeStation US stocks

Uses official v3 and defaults to `https://sim-api.tradestation.com/v3`. Live uses `https://api.tradestation.com/v3`. Configure the selected stock `account_id`, plus an access token or OAuth client/refresh credentials. PKCE clients can omit `client_secret`; remove unused placeholder fields. Rotating refreshed credentials are retained only in process memory, so a current token must be provisioned after restart.

Only US stocks are supported. Market/limit orders use DAY/GTC/IOC/FOK durations subject to broker route/session/account restrictions. Quantities are whole shares. `options.trade_action` can explicitly select `SELLSHORT` or `BUYTOCOVER`; plain BUY/SELL retains its usual meaning. `options.route` maps to the documented Route field. Submission verifies symbol AssetType STOCK before sending.

Positions and current/history order reads retain only stock records. Both order endpoints use pageSize and NextToken. All today's creation timestamps are filtered to the configured calendar window, with older GTC orders discovered separately for cancellation. Queries can select `account_ids`, but a single account is required for each KTrader account snapshot and submission scope.

Tradability reports symbol metadata and a nondelayed halt flag where available; instrument existence alone does not prove account permission. Borrow queries expose the nondelayed HTB quote flag and account stock-locate eligibility. Available share inventory, borrow rate and locate approval are not exposed by the verified public v3 schema and remain null.

LULD intent sends a regular-session DAY limit. Use only a broker-confirmed route; no dedicated LULD instruction or participation guarantee is inferred. Native FOK and optional timed cancellation cannot rest for reopening.

Sources: [v3 specification](https://api.tradestation.com/docs/specification/), [SIM/live](https://api.tradestation.com/docs/fundamentals/sim-vs-live/), [OAuth refresh](https://api.tradestation.com/docs/fundamentals/authentication/refresh-tokens/).
