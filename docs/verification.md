# US-stock delivery verification

Verified on 2026-10-25:

- **736 automated tests passed, plus 35 unittest subtests.** The final full run explicitly disabled socket connections, so tests could not contact brokers or publish Kafka messages.
- **132 offline CLI/JSON checks passed** across all seven broker folders: native order previews, native FOK rejection where appropriate, opt-in timed cancellation, queries, controls, recovery, all example commands and Robinhood read-only discovery preview. Every broker rejected crypto, option, futures and event-contract command scopes. No `--execute` flag was used in this CLI run.
- All project Python sources parsed successfully. Runtime modules and the installed Webull SDK request interfaces were exercised offline.
- Actual US-stock adapter classes were used in mocked request/response, signing, pagination, classification, canonical snapshot, cancellation, timed recovery, auth, and acknowledgement tests. Malformed acknowledgements never establish successful submission.
- Bitget uses the official Stock+ catalog. Tests validate signed Stock+ requests and KTrader v1 snapshots; correlation remarks are not advertised as server idempotency.
- Robinhood uses the official OAuth Trading MCP equity tools. Tests cover authenticated tool-schema discovery/validation, bounded incremental JSON/SSE transport, immediate return at the matching SSE response, account/listing identity checks, ADRs, orders, pagination and no-write retries. Exact schemas seeded from an independently recorded server contract are validated against live authenticated schemas before use.
- A public Robinhood MCP initialization check returned OAuth-required HTTP 401; its public OAuth discovery metadata was verified. No bearer or private account data was used.
- The last read-only Kafka metadata check confirmed `account-details` and `order-status` exist on `45.32.121.19:9092`; `trading-commands` partition 0 was empty (`begin=end=9238`). No topic was created, input offset committed, or record published by inspection.

Tested environment: Python 3.14, requests 2.34.2, kafka-python 3.0.11, jsonschema 4.26.0, pytest 9.1.1, optional webull-openapi-python-sdk 3.0.4. Credentials are not included. The delivered scope is US stocks; the former crypto adapters and crypto-borrow substitutions are no longer active paths.

The tests establish local implementation and mocked contract behavior. Authenticated account permissions, live Stock+ POST acceptance (whose generated cURL examples differ from its general JSON POST guidance), current authenticated Robinhood schemas, actual fills, borrow entitlements, LULD auction routing and the external Windows Zhuorui session have not been exercised here. No live or paper trades were placed. Unsupported native FOK/borrow capabilities are explicit rather than fabricated.

The deliverable is source plus configuration and operations documentation, not a running trading service. Populate the correct stock credentials/account, preserve journals and explicitly enable execution for deployment. See `runtime.md`, per-broker READMEs, `borrow-availability.md`, and `luld.md` for the exact boundaries.
