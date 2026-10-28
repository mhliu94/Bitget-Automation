# GigaMoney behavior parity audit

Audit date: 2026-10-25. Reference: `/home/ubuntu/GigaMoney-Automation-Linux`.
Compared with the delivered `/home/ubuntu/broker-api-scripts` and its workspace
copy. Only tracked-style source, documentation, examples, and tests were read.
No private configuration, sessions, certificates, runtime state, services, or
broker/Kafka writes were used. Findings below describe the state before the
current parity implementation; they are a work checklist, not a completion claim.

## Highest priority contract defects

1. **Legacy command timestamps are ignored.** The reference requires and parses
   `ts` (`gigamoney/runner.py:144-156`) before execution (`:446-451`). The delivered
   `common/commands.py:197-218` checks several aliases but omits `ts`. An old
   command republished into Kafka with a fresh record timestamp is therefore
   accepted. A direct offline reproduction accepted
   `{"ts":"2000-01-01T00:00:00Z"}` with a current record timestamp. Add `ts` to
   recognized timestamps, reject malformed/future/stale values, and apply age
   checks to financial and trading-state commands before state changes.

2. **`cancel_unfilled: true` silently loses its meaning.** The reference maps it
   to its FOK workflow (`gigamoney/runner.py:49-60`), alongside
   `LIMIT_ORDER_FOK` and `time_in_force: FOK`. Delivered normalization
   (`common/commands.py:125-172`) ignores this field: an offline test normalized a
   LIMIT_ORDER carrying it to `time_in_force: day`. Treat the field as a strict
   alias for the requested fill-or-kill/cancel-remainder behavior, reject
   contradictory or malformed combinations, and retain explicit native versus
   synthetic capability differences. Never let this instruction silently create
   a resting order.

## Missing core runner behavior

| Requirement | Reference evidence | Delivered evidence and impact |
| --- | --- | --- |
| `CANCEL_OPEN_ORDERS` control and CLI cancel-all | `gigamoney/runner.py:62-63`, `:193`; `gigamoney/api_trading.py:542` | `common/commands.py:125-126` rejects the action; `common/runner.py:50-77` has no cancellation branch. Existing cancellation recovery is Zhuorui-only, not an account cancel-all control. |
| Persistent `SET_TRADING_ENABLED` | `gigamoney/runner.py:64-76`, `:176-186`, `:455-462` | Delivered parser rejects the action. `common/runner.py:36-37` only reads fixed configuration; the running consumer cannot be disabled/enabled by the existing control. Reference cancellation remains available while new orders are disabled. |
| Account snapshots on startup, periodic refresh, and after financial actions | `gigamoney/runner.py:312-317`, `:344-417`, `:467-493` | `common/consumer.py:37-54` only iterates command records. No timed refresh or post-action snapshot exists. `docs/kafka.md:42` explicitly excludes these snapshots. |
| KTrader `account-details` payload | `gigamoney/runner.py:95-141`, `:385-389` | `common/runner.py:56-57` returns native positions inside a command-result envelope. Required account identity, cash/currencies, normalized position quantities/costs, observation time, and trading state are absent as a separate snapshot. Some adapters will need balance/account reads in addition to positions. |
| KTrader `order-status` full snapshots for today's orders, including externally placed and completed orders | `gigamoney/runner.py:376-383`; `gigamoney/order_status.py:58-119`; `docs/order-status.md:10-23` | `orders_today` only returns native query data (`common/runner.py:72-79`). There is no mapper, snapshot producer, exact order ID key, normalized lifecycle, cumulative fills, or source-command correlation. |
| Durable acknowledged order publication with restart replay | `gigamoney/order_status.py:127-253`; tests `test_order_status.py:102-207` | Only a command result journal exists (`common/journal.py:13-56`). There is no per-order pending outbox, sequence, stable full snapshot, retry, or one-time restart republish. |
| Refresh failure must not replay completed financial actions | `gigamoney/runner.py:473-481`, `:380-383`, `:412-414`; tests `test_runner.py:304-355` | Current result publication already waits before offset commit, with stored order results. New account/order publication must preserve that protection, commit the action before post-action refresh, and independently retry failed reporting. |

Reference account payload fields are `account_id`, optional `account_num_id`,
`cash`, `cash_by_currency`, `positions`, `ts`, and `trading_enabled`. Position
fields include `symbol`, `qty`, `avg_price` and optional `marketPrice`,
`marketValue`, `dailyPL`, `dailyPLPercent`. Unknown financial values should not
be invented merely to satisfy a schema.

Reference `account-details` publication uses a zero-length UTF-8 key because a
true Kafka null key was rejected by the topic (`gigamoney/runner.py:238-245`).
Order keys are compact JSON `[account_id,order_id]`
(`gigamoney/runner.py:267-271`). The authoritative v1 schema fixture is
`tests/fixtures/order-status.v1.schema.json`; mapping must preserve exact string
IDs, original creation times, cumulative partial fills, and separate NEW,
PARTIALLY_FILLED, FILLED, CANCELED, REJECTED, EXPIRED, pending and unknown states.
Missing creation times cannot be replaced with poll time.

The reference schedules every next periodic refresh from completion of the last
query/delivery attempt, including failures (`gigamoney/runner.py:412-417`). It
commits financial commands before post-action publication and performs that
publication before polling the next command (`:473-481`). An accepted order with
failed confirmation/cancellation and partially completed cancel-all also trigger
refresh (`:483-493`). These behaviors need explicit fake-client regression tests.

## Recovery and operational parity

- **Authentication stops intake:** the reference persists an
  `authentication_required` condition and stops (`gigamoney/runner.py:293-302`),
  leaving pre-submission authentication failures uncommitted (`:499-503`). The
  delivered runner turns generic write errors into `unknown`
  (`common/runner.py:84-91`) and consumer commits every returned/rejected result
  (`common/consumer.py:40-51`). Introduce a distinct pre-submission authentication
  outcome where the broker can prove that condition; do not conflate an expired
  credential with an uncertain accepted submission.
- **Account-wide recovery before intake:** reference startup calls
  `recover_pending` before Kafka intake (`gigamoney/runner.py:276-286`). Its
  API executor restores already-known order confirmation/cancellation work
  (`gigamoney/api_trading.py:506-540`). Delivered SQLite prevents replay only
  for the same command ID (`common/journal.py:23-48`) and offers no general
  inspect/reconcile flow or account-wide pending-operation check. An unresolved
  ID does not prevent a new unrelated ID from submitting. Preserve journal
  intent and use exact broker/client IDs for reconciliation; never match only
  symbol, quantity, or time.
- **One runner and serialized account workflows:** reference uses a runner lock
  (`gigamoney/runner.py:276-280`) and API operation lock
  (`gigamoney/api_trading.py:128`, `:204`, `:430`, `:512`, `:543`). Delivered
  SQLite serializes reservation briefly but does not hold an account operation
  lock across broker requests (`common/journal.py:34-48`). Safe cancellation,
  shared session renewal, and snapshot sequencing need explicit coordination.
- **Runner lifecycle and health:** reference provides signal/stop-file handling,
  runner-status/runner-stop/service commands and workflow timing logs. Delivered
  CLI only exposes individual scripts/consumer (`common/cli.py:16-74`). These are
  useful operational parity work. The reference's full emulator/dashboard UI is
  not necessary for the user's broker-script deliverable unless separately
  requested.

## Behavior needing broker-aware treatment

The GigaMoney API backend converts MARKET_ORDER during verified premarket and
after-hours sessions to a limit using cached Kafka `price-books`; regular-hours
market orders need no price. The source contract is documented at
`docs/api.md:143-188`: buy offer side/sell bid side, depth accumulation, 300-second
maximum source age, no order-time Kafka read, and rejection of missing/stale or
unknown-session inputs. The delivered common suite has no such cache or
conversion pipeline. This matters for literal operational equivalence, but the
implementation should use an explicit per-broker capability/option and respect
native session behavior. Do not make a global silent market-to-limit conversion.

Likewise, GigaMoney's synthetic FOK, universal directional cent rounding, and
five-second live-action delay are particular to that reference implementation.
Native broker FOK and instrument-specific price ticks should stay accurate; copy
the requested intent and reporting contract, not an unsuitable broker-specific
restriction. The existing suite's explicit unsupported order/session/borrow
results, exact decimal request construction, per-account routing, preview mode,
and duplicate suppression are valuable existing behavior.

## Verification boundary

This audit establishes source-level gaps and four offline parser reproductions.
It does not prove broker endpoint compatibility, authenticated balances/orders,
Kafka producer permissions, live publication acceptance, or execution/fills.
Those claims require separate evidence. The source files and regression suites
above supply concrete expected behavior for implementation and offline tests.

## Publication implementation follow-up

`common/publication.py` now provides the shared `SnapshotPublisher` infrastructure,
tested independently in `tests/test_publication.py`. Broker mapping and consumer
integration remain separate work. It accepts the Kafka producer's
`send(...).get(timeout)` interface, keeps pending payloads and acknowledged
snapshots in a distinct SQLite publication database, and serializes cooperating
publishers sharing that database. Each order's key is compact JSON
`[account_id,order_id]`; account snapshots use the reference's empty byte key.

Delivery failure preserves the exact pending payload and sequence. Pending
snapshots for **today in the configured broker timezone** are retried before new
observations, including when the broker no longer returns the order. Older and
future-date pending snapshots are retained for inspection but **never replayed**.
Unchanged orders are republished once after publisher restart only when observed
again. This deliberately follows the GigaMoney today-only policy; it does not
promise Zhuorui-style replay of earlier creation dates. Creation time is immutable,
publication update time cannot decrease, and observation-time changes alone do
not increment sequence. A corrupt database record is reported without resetting
its sequence. Keep this database with the account across restarts and migrations.

The actual KTrader account consumer casts `cash` to a number and rejects a null
cash value. Accordingly, account publication requires known finite numeric cash;
unknown cash cannot be substituted with zero. Broker mapping may use a clearly
labelled, explicitly configured alternative such as Robinhood's buying power,
but must not imply that buying power is a broker cash balance. Unknown average
position cost remains nullable, as supported by the consumer. Unknown order fill
quantities are rejected instead of inventing zero executions. Independent order
publication failures are returned individually and do not prevent a caller from
publishing valid account data.


## Current implementation evidence

The original gap table above is retained as the before-state audit. Current code/tests now cover:

| Audit item | Current evidence |
| --- | --- |
| Legacy timestamp/cancel-unfilled semantics | `common/commands.py`, `tests/test_controls.py` |
| Cancel-open and persistent controls | `common/cancellation.py`, `common/journal.py`, `tests/test_cancellation.py`, `tests/test_controls.py` |
| Startup/idle/post-action reporting | `common/consumer.py`, `common/reporting.py`, `tests/test_consumer_runtime.py` |
| Canonical account/order mapping | `common/snapshots.py`, `common/snapshot_asia.py`, `tests/test_snapshots.py`, `tests/test_snapshot_asia.py`, `tests/test_snapshot_isolation.py` |
| Durable snapshots and replay | `common/publication.py`, `tests/test_publication.py` |
| Auth refusal and uncertain-write distinction | `common/errors.py`, broker transports, `tests/test_authentication.py`, `tests/test_controls.py` |
| Optional timed cancellation and account-wide recovery | `common/timed_cancel.py`, `common/runner.py`, `tests/test_timed_cancel.py`, `tests/test_timed_integration.py` |
| Serialization and local lifecycle | `common/locking.py`, `common/health.py`, `tools/runner_control.py` |

Native broker market/session behavior remains explicit. A universal Kafka price-book market-to-limit conversion, a new web dashboard, and unsupported native broker products are not claimed. Broker-specific native support and verified scope still govern which scenarios can execute. In particular, Robinhood US-stock parity remains an open scope question; the existing official adapter is crypto-only.
