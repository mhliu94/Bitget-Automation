# Webull US stocks

Uses the official `webull-openapi-python-sdk` US Trading API. Install `webull/requirements.txt`. Configure app_key, app_secret, stock account_id and region us. The endpoint is a hostname such as `api.webull.com`; sandbox credentials can use the appropriate sandbox hostname. Optional token_dir/access_token follow the SDK's authorization flow. No private password-login endpoint is used.

All submissions force instrument_type EQUITY and market US. Market/limit stock orders support DAY/GTC. Native stock FOK/IOC rejects; explicit shared timed cancellation remains available as a different execution policy. Crypto, futures, options and event contracts are excluded.

`options.support_trading_session` defaults to CORE; eligible limits may select ALL/NIGHT. The shared extended_hours boolean maps to ALL/CORE. LULD intent requires CORE and a DAY limit; no special reopening route is documented or guaranteed.

Positions and paginated open/history orders filter US stock records using instrument metadata. Unknown classifications resolve through the US stock profile API or fail explicitly. Cancel requests use the correct child client_order_id inside the configured account; they do not cancel other asset types. Snapshot detail reads preserve cumulative executed shares and actual fill prices.

Tradability uses stock profile status, including open/tradable, nontradable and closing-only. These flags do not establish current halt state or account permissions. Borrow queries return shortable/easy_to_borrow flags where supplied; available inventory, fees and locate confirmation remain null. Institutional or other-region locate APIs are not mixed with US retail keys.

Sources: [stock orders](https://developer.webull.com/apis/docs/reference/common-order-place/), [US stock profiles](https://developer.webull.com/apis/docs/reference/instrument-list/), [cancel](https://developer.webull.com/apis/docs/reference/common-order-cancel/), [SDK](https://developer.webull.com/apis/docs/sdk/).
