# Broker API scripts

Seven separate broker groups: [Bitget](bitget/README.md), [Robinhood](robinhood/README.md), [Alpaca](alpaca/README.md), [TradeStation](tradestation/README.md), [Longbridge](longbridge/README.md), [Webull](webull/README.md), and [Zhuorui](zhuorui/README.md).

Each folder contains these standalone Python entry points:

| Script | Operation |
| --- | --- |
| `market_order.py` | Submit/preview a market order |
| `limit_order.py` | Submit/preview a limit order |
| `fok_order.py` | Submit/preview a limit order with native FOK where supported |
| `positions.py` | Query all positions/holdings in the configured account scope |
| `orders_today.py` | Query orders created today, including open and terminal states |
| `capabilities.py` | Report implemented order types, time-in-force, and broker limitations |
| `tradability.py` | Check a symbol using broker instrument/status data, without submitting an order |
| `borrow_availability.py` | Query available shortability/borrow information, without borrowing or requesting a locate |
| `command.py` | Handle one JSON command from a file or stdin |
| `cancel_order.py` | Cancel a specific broker order in the configured account |
| `cancel_open_orders.py` | Request cancellation of open orders, including older GTC orders where the API exposes them |
| `set_trading_enabled.py` | Persist an account trading control; cancellation remains available while disabled |
| `recover_cancellations.py` | Resume confirmed pending cancellation work without resubmitting orders |
| `consume.py` | Run the Kafka command worker and periodic account/order publisher |

The trading, cancellation, state-control, and query commands are available through Kafka. Recovery is a local maintenance operation and also runs before live worker intake. Shared infrastructure lives in `common/`; each broker owns its request mapping, authentication, capabilities, and query logic. Results are JSON. A broker acknowledgement is reported as `submitted`, not as a fill.

## Setup

Use Python 3.11 or newer. The main suite was tested on Python 3.14. All broker calls use supplied configuration; no credentials are included.

```bash
cd ~/broker-api-scripts
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
# Only needed for Webull:
.venv/bin/python -m pip install -r webull/requirements.txt
cp config.example.json config.json
```

Edit the selected entry under `brokers` in `config.json`, or copy its folder's config example into that object. `${ENV_VARIABLE}` placeholders are resolved from the process environment. Only the selected broker's placeholders are resolved. Use a separate config for each account if you run multiple accounts at one broker.

Set `routing.account_id` and/or `routing.account_num_id` to the IDs your Kafka producer uses; these are application routing labels, not necessarily the broker's real account ID. `routing.server_id` is optional. Every selector provided in a command must match. Credentials and real broker account IDs remain in the broker-specific fields described in each README.

Set `journal_account` to a permanent label identifying the exact account **and environment**, for example `alpaca-paper-my-account`. Preserve that label when rotating credentials, changing server names, or restarting. Use another label for live versus paper and for each actual account. Preserve `.state/` as well: removing/changing the journal or its account label removes duplicate protection. Run only one logical executor per account; processes on one host must use the same journal file. Separate machines do not share SQLite protection.

Scripts default to an offline preview. Add `--execute` to perform a broker query or submission. Order submissions additionally require `trading_enabled: true` in the selected broker config. Alpaca defaults to paper; TradeStation defaults to simulation. Other adapters use the configured live API unless their own documentation specifies otherwise. The delivered configuration has order submission disabled throughout.

## Examples

```bash
# No credentials or network needed for capabilities/order previews.
.venv/bin/python alpaca/capabilities.py --config config.example.json
.venv/bin/python alpaca/limit_order.py --config config.example.json \
  --id my-limit-001 --symbol AAPL --side buy --quantity 1 --limit-price 100

# These read real configured account/instrument data; they place no orders.
.venv/bin/python alpaca/positions.py --execute
.venv/bin/python alpaca/orders_today.py --execute
.venv/bin/python alpaca/tradability.py --symbol AAPL --execute
.venv/bin/python alpaca/borrow_availability.py --symbol AAPL --execute

# After configuring credentials and deliberately enabling trading:
.venv/bin/python alpaca/fok_order.py --id my-fok-001 \
  --symbol AAPL --side buy --quantity 1 --limit-price 100 --execute

# Preview the commands addressed to this account, with a separate preview group:
.venv/bin/python alpaca/consume.py --config config.json
# Execute new addressed commands using the live consumer group:
.venv/bin/python alpaca/consume.py --config config.json --execute
```

Always keep the same `--id` after an uncertain submission. A completed duplicate returns the stored result; a crashed/pending or unknown submission is never automatically resubmitted. New orders for that account stop until known cancellation work is recovered or uncertain actions are reconciled against broker evidence. Inspect the broker's orders before creating any replacement command. Query commands may use fresh IDs for fresh data.

Today's window is `[local midnight, next local midnight)` in the configured IANA timezone, including DST transitions. Default examples use `America/New_York` for equity accounts and UTC for Bitget. “Today's orders” means creation/submission today, including pending, filled, canceled and rejected records available from the broker. Older GTC orders are not today's orders. Responses retain native broker fields to avoid misrepresenting state. Query failures or unresolved pagination raise an error rather than silently returning partial results.

## Implemented scopes and native FOK

| Broker | Implemented trading scope | Market / limit | Native FOK |
| --- | --- | --- | --- |
| Bitget | Classic spot crypto | Yes | Limit only; market buys use quote currency |
| Robinhood | Official Crypto Trading API v2 | Yes | Not exposed by this API; equities not implemented |
| Alpaca | US equities, single-leg options, crypto | Yes | Whole-share eligible US equities; account/venue restrictions apply |
| TradeStation | US equities, single-leg options, futures | Yes | Native duration; availability depends on asset, route and session |
| Longbridge | AP HK/SG account APIs, US/HK securities | Yes | Not exposed by the supported API |
| Webull | US API equities, crypto, futures, event contracts | Yes; events limit only | Event contracts only; not stocks |
| Zhuorui | US equities via the referenced Windows session runtime | Yes | No verified native FOK; optional timed cancellation explicitly identified |

See each broker's `capabilities.py` and README for exact asset/TIF/session combinations and current source links. A broker may support other order types that these scripts do not implement. Unsupported native combinations are rejected before order submission. The explicitly configured timed-cancellation policy below is separate from native FOK and reports its actual execution semantics.

Zhuorui requires the pinned external checkout and a valid Windows DPAPI session, described in [its setup instructions](zhuorui/README.md). Its optional one-second limit cancellation is **not native FOK** and can partially fill. It is disabled by default. Zhuorui recovery uses its upstream journal; the other six recovery scripts use the shared durable timed-cancellation journal.

## Reference-runner workflows

The live Kafka worker publishes account snapshots to `account-details` and full v1 order snapshots to `order-status` at startup, every 30 seconds, and after order/cancellation actions. It queries externally placed and completed orders as well as local submissions. An acknowledged order snapshot is durably sequenced; delivery retries reuse the same payload and sequence. Reporting failures do not replay trades. Read [runtime operations](docs/runtime.md) for scheduling, process controls, recovery, and account-field semantics.

`SET_TRADING_ENABLED` persists across restarts. Remote enable cannot override `trading_enabled:false` in the local configuration. A proven pre-acceptance authentication failure stops intake without committing that command; ambiguous accepted operations remain reserved. All processes using one account must share the same journals and execution lock.

Native FOK remains the default. For the six direct API adapters, explicitly setting `fok_policy:"timed_cancel"` enables a limit-then-cancel workflow comparable to the references. Its one-second target starts at dispatch. It can partially fill, and network delays can miss the target. The script verifies the exact ticket before requesting cancellation and reads its final state; acknowledgement alone does not finish the workflow. This policy is excluded from LULD reopening intent. Zhuorui retains its own `allow_timed_cancel_fok` setting.

```bash
# Preview these operations first; --execute performs them.
.venv/bin/python alpaca/cancel_order.py --id cancel-001 --order-id BROKER_ORDER_ID
.venv/bin/python alpaca/cancel_open_orders.py --id cancel-all-001
.venv/bin/python alpaca/set_trading_enabled.py --id disable-001 --enabled false
.venv/bin/python alpaca/recover_cancellations.py
.venv/bin/python tools/runner_control.py status --broker alpaca
```

## Tradability, borrow availability, and halts

Tradability reports separate symbol visibility, instrument eligibility, session/halt observations, and unknown account permissions where the API permits. A positive instrument flag does not promise immediate execution or acceptance of every order. `null` means unavailable/unverified, not false. Checks never submit probe orders.

Borrow responses distinguish shortability, easy-to-borrow status, reported available quantity, locate quote cost, and recurring borrow rates. A stock flag is not a borrow reservation. An estimated maximum sell quantity is not broker lendable inventory. Crypto cash/spot borrowing is not equity stock borrow. [Borrow API coverage](docs/borrow-availability.md) documents what each adapter can establish. These scripts never acquire a locate, borrow assets, or enroll shares in lending merely to answer a query.

[LULD support notes](docs/luld.md) explain reopening intent and the public halt helper. An explicit `auction_intent: "luld_reopening"` accepts only a regular-session US equity limit DAY order. It submits that ordinary order without claiming special auction routing. FOK/IOC orders cannot rest for a future reopening. Broker and venue acceptance/routing still determine participation. `OPG`, MOO, and HK auction types are not silently used as LULD instructions.

## Kafka and verification

The requested broker `45.32.121.19:9092` was reachable. At inspection, `trading-commands` partition 0 had beginning offset **9238** and end offset **9238**, with no retained records. `actions` and `client-requests` were also empty. No existing consumer offset was committed and no command was published or executed. The supported legacy field mapping therefore follows the supplied Zhuorui repository's documented KTrader contract, with explicit query extensions. See [Kafka protocol](docs/kafka.md) and [inspection evidence](docs/kafka-sample.jsonl).

```bash
.venv/bin/python -m pytest -q
.venv/bin/python tools/inspect_kafka.py --count 20
.venv/bin/python tools/halts.py --symbol AAPL
```

Automated validation covers request construction, signing, pagination, broker errors, unsupported order types, capability/tradability/borrow responses, timezone windows, routing, crash recovery, and duplicate suppression. Authenticated broker integration and real fills require your credentials and account permissions and were not exercised during delivery. No live orders were placed.
