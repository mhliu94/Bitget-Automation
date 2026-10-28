# Bitget Stock+ US stocks

This group uses the official **Stock+ securities API** at `/api/v3/stockplus`. It supports US stock market and limit orders, position queries, today's orders, cancellation, account/order snapshots and instrument checks. Crypto, Reality/rToken, CFDs, options and foreign-listed securities are excluded. Older `account_type:classic_spot` and crypto-borrow settings fail configuration validation.

Use an API key, secret and passphrase with Stock+ trading/account read permissions, trade-write permission for orders, and Stock+ market-data access for instrument checks. Complete the Stock+ account onboarding and any market-data entitlement requirements with Bitget. `config.example.json` shows the settings; `base_url` defaults to `https://api.bitget.com` and request timeout to 20 seconds.

Supply `AAPL` or `AAPL.US`; the broker payload always uses `AAPL.US`. Quantities are shares for buys and sells, including market buys. Fractional quantities remain decimal strings. A live placement first verifies the requested instrument using Stock+ basic information. `build_order` itself is offline. Quotes/instrument metadata do not prove account-specific trading permission or order acceptance.

Market and limit orders support `day`, `gtc` and `gtd`. GTD requires `options.expire_date` in `YYYY-MM-DD` format. **Native FOK and IOC are not documented and are rejected**; timed cancellation, when separately selected through the shared runtime, allows partial fills and is not true FOK. Other broker-documented conditional order types are reported in capabilities but not submitted by these scripts.

Optional `outside_rth` is `RTHOnly`, `AnyTime` or `Overnight`; the equivalent uppercase underscore forms are accepted. `extended_hours:true` selects `AnyTime`. Extended-session orders must be limits. LULD intent uses regular hours and the shared halt controls. No dedicated Stock+ LULD reopening auction route or guaranteed participation is documented; ordinary eligible limits must not be described as an auction guarantee.

A deterministic `remark` (`cmd:` plus a command-ID hash) correlates submissions. It is **not broker idempotency**. The docs mention `clientOid` in responses/cancellation but omit it from the placement request, so this adapter does not invent one. The local journal provides duplicate protection. Unknown write outcomes require reconciliation; writes are never automatically retried.

Orders for a requested day combine `today-orders` and `history-orders`, deduplicate by exact ID and filter `submittedAt` to the configured day's UTC bounds. Historical `hasMore` splits the inclusive integer-second interval recursively. Live empty history responses may omit `hasMore`; that omission is accepted only with an empty `orders` list. Nonempty pages still require a boolean flag. A single second that still overflows, malformed pagination metadata, excessive windows, or rejected history range fails explicitly. Open-order cancellation queries history from Unix epoch through now as well as today's endpoint, preserving older GTC orders. Broker-side retention cannot be enlarged by this script; a server rejection is not silently replaced with a shorter range.

`positions` reads every account channel from `stock-position` and includes only US stock symbols; snapshots retain the channel, signed share quantity and reported cost. Cancellation resolves an exact order detail, checks the requested symbol and stock instrument, and only then sends a cancel. The acknowledgement alone does not prove cancellation or fills.

The borrow control returns `supported:false`, `status:unavailable` and unknown quantity/rate/shortability. Stock+ supports ETB short selling, but its published API has no stock borrow-availability endpoint. Crypto loan APIs do not answer this query. Tradability reads security metadata and quote status; unknown statuses remain unknown.

Transport uses the documented Bitget HMAC headers and signed JSON POST bodies. The generated Stock+ cURL examples inconsistently put POST parameters in the query string; JSON follows Bitget's general REST contract. Mocked contract tests cover the transmitted bytes, but authenticated live Stock+ acceptance remains to be verified with the user's configured account. No test trades have been placed.

Official sources checked 2026-10-25: [Stock+ orders](https://www.bitget.com/docs/catalog/stock-plus/orders), [Stock+ assets](https://www.bitget.com/docs/catalog/stock-plus/assets), [Stock+ instrument/quotes](https://www.bitget.com/docs/catalog/stock-plus/stock-quotes), [REST authentication](https://www.bitget.com/docs/uta/rest-api), [stock short-selling announcement](https://www.bitget.com/support/articles/12560603892050).

## BITGET_LYJ deployment

The local `config.json` routes account `BITGET_LYJ`, numeric ID `1`, through
Kafka `45.32.121.19:9092`. Commands use `trading-commands`; snapshots use
`account-details` and `order-status`. The configuration enables live trading,
uses `latest` for a new consumer group, and preserves the journal identity
`BITGET_LYJ-stock-plus-live`. Keep `.state/` across restarts.

`bitget/start.py` loads `client_id`, `client_secret`, and `passphrase` from
`/root/bitget-configs/config.properties` into the API environment variables.
Credentials stay out of `config.json` and command-line arguments.

```bash
cd /root/broker-api-scripts
.venv/bin/python bitget/start.py --check
systemctl start bitget-lyj
systemctl status bitget-lyj --no-pager
journalctl -u bitget-lyj -n 50 --no-pager
systemctl stop bitget-lyj
```

The service definition is `deploy/bitget-lyj.service`. It runs read-only Kafka,
account, order, and instrument checks before starting the consumer. It does not
automatically restart after an authentication or reconciliation failure and is
not enabled at boot. A graceful stop permits the current operation to finish.

Bitget error `40084` means the supplied account is in Classic Account mode and
the requested API requires Unified Account access. Resolve account eligibility
and Stock+ permissions in Bitget, then run the check and start commands above.
Changing the local `account_type` cannot resolve this server-side rejection.
