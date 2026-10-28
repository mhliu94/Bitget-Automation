# Delivery verification

Verified on 2026-10-25 in the build workspace:

- 419 automated tests passed, with 32 additional unittest subtests passing.
- 103 standalone CLI/JSON preview checks passed across all seven broker folders, including native FOK rejection, explicit timed cancellation, cancellation controls, trading controls, recovery and all example messages.
- All twelve example Kafka/control JSON commands were accepted by the Alpaca offline command handler.
- All project Python sources parsed successfully, and runtime modules were imported by the offline tests.
- Official Webull SDK 3.0.4 imports and request method signatures were checked offline; no authenticated network call was made.
- The public Bitget BTCUSDT symbol-information endpoint returned a successful online-market result.
- The public Nasdaq halt feed returned HTTP 200 and its RSS fields were inspected; parsing tests preserve unknown live halt/auction eligibility.
- Kafka metadata and bounded read-only inspection succeeded at `45.32.121.19:9092`. Metadata was refreshed after runtime integration, confirming both snapshot topics exist. `trading-commands` partition 0 had no retained records (`begin=end=9238`). No producer operation, consumer-group commit, topic creation, or command execution was performed during inspection.

Dependencies used for validation: Python 3.14, requests 2.34.2, kafka-python 3.0.11, PyNaCl 1.6.2, pytest 9.1.1, and optional webull-openapi-python-sdk 3.0.4. The main requirements pin the tested HTTP/Kafka/signing versions; the optional Webull requirements constrain its major API version.

Broker request/response, pagination, signing and error paths were tested with mocks. Credentials, authenticated borrow permissions, real positions/order history, fills, live auction routing, and the external Windows Zhuorui session were not available for end-to-end execution tests. No live or paper orders were placed. API contracts were checked against the linked official references, with Zhuorui checked against the explicitly supplied pinned repository.

The deliverable is source plus configuration examples, not a running trading service. Populate private configuration, preserve per-account journals, and explicitly enable execution when deploying it.


The expanded verification includes native broker cancellation transport contracts, exact-ticket timed cancellation, durable pending/restart recovery, no cancellation replay after uncertain dispatch, scoped authentication failures, startup/idle/post-action publication scheduling, account controls, immutable publication identity and sequencing, per-order mapping failure isolation, signed/diluted costs, and conditional cash-basis mappings. Tests use broker and Kafka mocks; passing them does not establish live account permission or actual auction routing.

The initial source audit is in `gigamoney-parity-audit.md`. Implemented runtime behavior is documented in `runtime.md`. Core parity additions are present in the working source and the delivered folder: `ts`/`cancel_unfilled` interpretation, account-scoped cancel controls, persistent trading control, periodic/post-action account and order snapshots, durable outbox, local operation/runner locks, health/stop controls, typed authentication handling, and optional timed cancellation with recovery.

Open scope clarification: Robinhood remains the official Crypto API adapter. US stock execution and LULD participation are not implemented through that API; the user has been asked whether stock automation is required. This limitation must not be represented as stock-trading parity.
