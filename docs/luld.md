# LULD and reopening auctions

The scripts support explicit reopening **intent**, not a universal broker auction order type. Use `auction_intent: "luld_reopening"` on a US equity limit DAY order within the enforced `asset_class: "us_equity"` scope; other asset scopes, market orders, FOK/IOC, and extended-hours flags are rejected for this intent. Bitget Stock+ and Longbridge US symbols use `.US`. Webull requires CORE and Longbridge requires regular-session handling. Zhuorui limits are submitted with pre/post disabled. This preserves the requested limit and permits the broker to decide whether to accept/hold/route the order.

```bash
.venv/bin/python alpaca/limit_order.py --config config.example.json \
  --id reopening-example-001 --symbol AAPL --side buy --quantity 1 \
  --limit-price 100 --time-in-force day --asset-class us_equity --auction-intent luld_reopening
```

The example is an offline request preview. To send it, configure the actual account, enable trading, and add `--execute`. Results retain `auction_participation_confirmed: false`: a successful ordinary-order acknowledgement does not confirm participation in a reopening auction. Current broker APIs do not establish a cross-broker guarantee of such participation.

Native FOK requires immediate complete execution or cancellation. It does not wait through a halt for reopening. Zhuorui's optional timed cancellation similarly cannot provide all-or-none auction participation. Opening-auction instructions such as OPG/MOO and Longbridge's HK AO/ALO types are different instructions and are not substituted.

Broker tradability checks expose what their instrument and quote APIs report. Some provide instrument eligibility but no current halt field; some quote status feeds require entitlements or may be delayed. A missing/unknown halt flag is not an all-clear. These checks do not block explicit DAY limit orders merely because continuous trading is halted, since broker-supported reopening participation may be intended.

The additional read-only helper reads Nasdaq's public announcements:

```bash
.venv/bin/python tools/halts.py --symbol AAPL
```

It reports reason code, LULD indicator, halt time, announced resumption, feed age and freshness. A missing record is not proof that a security is unhalted. A scheduled resumption is not proof that trading actually resumed, and a stale feed is explicitly marked. The helper is informational; it does not automatically select broker routes, order prices, auction timing, or retry rejected orders.

Exchange behavior differs by primary listing venue and by the time of the pause. For example, Nasdaq's rules distinguish its own halt-cross securities from those listed elsewhere, and a late-day LULD pause may transition into closing procedures. See the [Nasdaq equity rules](https://listingcenter.nasdaq.com/rulebook/nasdaq/rules/nasdaq-equity-4), [Nasdaq LULD FAQ](https://nasdaqtrader.com/content/MarketRegulation/LULD_FAQ.pdf), and [Cboe LULD FAQ](https://www.cboe.com/document/tech-spec/document/technical-specifications/cboe-limit-updown-faq). Confirm the configured account's route/session entitlements and inspect broker order status; the adapter does not infer auction eligibility from these exchange rules alone.
