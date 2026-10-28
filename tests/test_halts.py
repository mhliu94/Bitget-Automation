from datetime import datetime, timezone
import pytest
from common.halts import parse_feed
from common.errors import BrokerError

FEED = b'''<rss xmlns:ndaq="http://www.nasdaqtrader.com/"><channel><pubDate>Fri, 23 Oct 2026 15:00:00 GMT</pubDate><item><ndaq:HaltDate>10/23/2026</ndaq:HaltDate><ndaq:HaltTime>10:58:00.000</ndaq:HaltTime><ndaq:IssueSymbol>TEST</ndaq:IssueSymbol><ndaq:ReasonCode>LUDP</ndaq:ReasonCode></item></channel></rss>'''


def test_feed_is_observation_not_eligibility():
    result = parse_feed(FEED, "TEST", datetime(2026, 10, 23, 15, 1, tzinfo=timezone.utc))
    assert result["feed_is_fresh"] is True
    assert result["observations"][0]["is_luld"] is True
    assert result["observations"][0]["halt_at"] == "2026-10-23T14:58:00Z"
    assert result["live_halted"] is None
    assert result["auction_eligible"] is None


def test_stale_or_missing_symbol_remains_unknown():
    result = parse_feed(FEED, "OTHER", datetime(2026, 10, 23, 16, tzinfo=timezone.utc))
    assert result["feed_is_fresh"] is False
    assert result["observations"] == []
    assert result["live_halted"] is None


def test_reject_invalid_xml():
    with pytest.raises(BrokerError):
        parse_feed(b"not xml")
