# Alpaca US stocks

Uses Trading API v2 with API key/secret. The default endpoint is `https://paper-api.alpaca.markets`; live uses `https://api.alpaca.markets` with matching keys. Copy this folder's config into `brokers.alpaca` and follow the root README.

Only `us_equity` is supported. Market and limit orders accept DAY/GTC/IOC/FOK where the broker permits; fractional shares require DAY. Native FOK requires eligible whole shares and may need account entitlement. OTC FOK rejects locally. Submission verifies symbol metadata before POST; crypto and option instruments cannot be relabeled as stocks.

Order options: boolean `extended_hours` for eligible equity limits, and side-consistent `position_intent`. Price/quantity remain decimal strings; unsupported equity price precision rejects rather than rounds. Positions and all-status order history filter US equity records; missing classification resolves through asset metadata or fails explicitly. Order pages use ID cursors so tied timestamps are preserved.

`tradability` returns asset eligibility separately from unknown live halt state. `borrow_availability` uses current `borrow_status` and `shortable`, plus authorized live HTB locate quotes when applicable. Quote inventory is advisory, per-share locate cost is not a daily borrow rate, and no locate is purchased. Paper trading does not expose the locates API.

LULD intent sends an ordinary regular-session DAY limit; no auction route is promised. IOC/FOK cannot wait through a halt. Shared optional timed cancellation is distinct from native FOK.

Sources: [orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca), [create order](https://docs.alpaca.markets/us/reference/postorder), [assets](https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id), [order pagination](https://docs.alpaca.markets/us/reference/getallorders-1), [short selling](https://docs.alpaca.markets/us/docs/margin-and-short-selling), [locate quotes](https://docs.alpaca.markets/us/reference/listlocatequotes).
