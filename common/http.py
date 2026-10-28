import requests
from urllib.parse import urlsplit
from .errors import AuthenticationError, BrokerError, ConfigurationError, OutcomeUnknown


class HttpClient:
    """HTTPS JSON transport. No automatic write retries and no redirects."""
    def __init__(self, base_url, headers=None, timeout=20):
        if urlsplit(base_url).scheme != "https":
            raise ConfigurationError("Broker base_url must use HTTPS")
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout
        self.session = requests.Session()

    def request(self, method, path, params=None, json=None, headers=None):
        url = path if path.startswith("https://") else self.base_url + "/" + path.lstrip("/")
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc:
            raise BrokerError("Cross-origin request refused")
        write = method.upper() not in {"GET", "HEAD"}
        try:
            response = self.session.request(method, url, params=params, json=json,
                headers={**self.headers, **(headers or {})}, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:
            raise (OutcomeUnknown if write else BrokerError)("HTTP request failed; no automatic retry") from None
        if response.status_code in (401, 403):
            raise AuthenticationError(f"Broker denied authentication or access (HTTP {response.status_code})", no_submission=True)
        if not 200 <= response.status_code < 300:
            error = OutcomeUnknown if write and response.status_code >= 500 else BrokerError
            raise error(f"Broker returned HTTP {response.status_code}; no automatic retry")
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise (OutcomeUnknown if write else BrokerError)("Broker returned invalid JSON") from None
