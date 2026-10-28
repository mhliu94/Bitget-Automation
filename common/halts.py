"""Supplementary public Nasdaq halt observations, never broker order eligibility."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree
from zoneinfo import ZoneInfo
import requests
from .errors import BrokerError
from .utils import iso_utc

URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
NS = "{http://www.nasdaqtrader.com/}"
FIELDS = ("HaltDate", "HaltTime", "IssueSymbol", "IssueName", "Market", "ReasonCode", "PauseThresholdPrice",
          "ResumptionDate", "ResumptionQuoteTime", "ResumptionTradeTime")


def parse_feed(content, symbol=None, now=None, max_age_seconds=180):
    now = now or datetime.now(timezone.utc)
    if len(content) > 5_000_000 or b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise BrokerError("Unexpected halt feed format/size")
    try:
        root = ElementTree.fromstring(content)
        channel = root.find("channel")
        published = parsedate_to_datetime(channel.findtext("pubDate"))
        age = (now - published).total_seconds()
        fresh = -60 <= age <= max_age_seconds
        observations = []
        for item in channel.findall("item"):
            row = {key: (item.findtext(NS+key) or "").strip() for key in FIELDS}
            if symbol and row["IssueSymbol"].upper() != symbol.upper():
                continue
            halt_time = datetime.strptime(row["HaltDate"]+" "+row["HaltTime"].split(".")[0], "%m/%d/%Y %H:%M:%S").replace(tzinfo=ZoneInfo("America/New_York"))
            trade_time = None
            if row["ResumptionDate"] and row["ResumptionTradeTime"]:
                trade_time = datetime.strptime(row["ResumptionDate"]+" "+row["ResumptionTradeTime"].split(".")[0], "%m/%d/%Y %H:%M:%S").replace(tzinfo=ZoneInfo("America/New_York"))
            # Feed observations are historical announcements, not an executable
            # real-time market status. Even a scheduled resumption is no proof.
            row.update(halt_at=iso_utc(halt_time), announced_resumption_at=iso_utc(trade_time) if trade_time else None,
                       is_luld=row["ReasonCode"] == "LUDP",
                       no_resumption_announced=trade_time is None,
                       live_halted=None)
            observations.append(row)
        observations.sort(key=lambda item: item["halt_at"], reverse=True)
        return {"source": URL, "feed_published_at": iso_utc(published), "checked_at": iso_utc(now),
                "feed_is_fresh": fresh, "feed_age_seconds": age, "symbol": symbol,
                "observations": observations, "live_halted": None, "auction_eligible": None,
                "note": "Public announcements only. Missing symbols do not establish unhalted status. Scheduled resumptions can change. Verify broker/venue status and routing for auction participation."}
    except (ElementTree.ParseError, ValueError, TypeError, AttributeError):
        raise BrokerError("Could not parse the Nasdaq halt feed; status remains unknown") from None


def query_halts(symbol=None):
    try:
        response = requests.get(URL, timeout=20, allow_redirects=False)
        if response.status_code != 200:
            raise BrokerError(f"Nasdaq halt feed returned HTTP {response.status_code}")
        return parse_feed(response.content, symbol)
    except requests.RequestException:
        raise BrokerError("Nasdaq halt feed unavailable; status remains unknown") from None
