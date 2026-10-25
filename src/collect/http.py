"""Polite JSON fetching shared by all adapters."""

import time

import httpx

HEADERS = {"User-Agent": "ai-job-finder/0.1 (private job digest)"}  # Ashby returns 403 without one
# Many job portals and career sites block unknown user agents but answer a normal browser.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 30
RETRIES = 3
RATE_LIMIT_RETRIES = 5      # HTTP 429 gets more, longer waits: the site asked us to slow down
RATE_LIMIT_WAIT = 30        # seconds, times the attempt number, unless the site sends Retry-After
MAX_WAIT = 120


class SourceError(Exception):
    """A source failed. `reason` is short and goes into the run records."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def get_json(url: str, params: dict | None = None, headers: dict | None = None):
    """GET a URL and return parsed JSON, retrying temporary failures with backoff."""
    return _request("GET", url, params=params, headers=headers)


def get_text(url: str, params: dict | None = None, headers: dict | None = None,
             rate_limit_retries: int = RATE_LIMIT_RETRIES) -> str:
    """GET a URL and return its body as text (RSS feeds, HTML pages).

    rate_limit_retries=1 gives up at the first HTTP 429 instead of waiting, for optional calls."""
    return _request("GET", url, params=params, as_text=True, headers=headers, rate_limit_retries=rate_limit_retries)


def post_json(url: str, body: dict, headers: dict | None = None):
    """POST a JSON body and return parsed JSON (Workday's search works this way)."""
    return _request("POST", url, body=body, headers=headers)


def post_form(url: str, form: dict, headers: dict | None = None):
    """POST form fields and return parsed JSON (RippleHire's search works this way)."""
    return _request("POST", url, form=form, headers=headers)


def _request(method: str, url: str, params: dict | None = None, body: dict | None = None, as_text: bool = False,
             headers: dict | None = None, form: dict | None = None, rate_limit_retries: int = RATE_LIMIT_RETRIES):
    last: Exception | None = None
    attempt = limited = 0
    while attempt < RETRIES and limited < rate_limit_retries:
        try:
            resp = httpx.request(method, url, params=params, json=body, data=form, headers={**HEADERS, **(headers or {})}, timeout=TIMEOUT,
                                 follow_redirects=True)
        except httpx.TimeoutException as e:
            last = SourceError("timeout", str(e))
        except httpx.HTTPError as e:
            last = SourceError("network", str(e))
        else:
            if "maintenance" in resp.url.path.lower():
                raise SourceError("maintenance", str(resp.url))  # e.g. Workday's weekly window; try next run
            if resp.status_code == 404:
                raise SourceError("not_found", url)          # wrong board name; retrying won't help
            if resp.status_code in (401, 403):
                raise SourceError("blocked", f"HTTP {resp.status_code}")
            if resp.status_code == 429:
                last = SourceError("rate_limited", "HTTP 429")
                limited += 1
                if limited < rate_limit_retries:
                    time.sleep(_retry_after(resp, limited))
                continue
            if resp.status_code >= 500:
                last = SourceError("http", f"HTTP {resp.status_code}")
            elif resp.status_code != 200:
                raise SourceError("http", f"HTTP {resp.status_code}")
            elif as_text:
                return resp.text
            else:
                try:
                    return resp.json()
                except ValueError as e:
                    raise SourceError("bad_response", "not JSON") from e
        attempt += 1
        time.sleep(2 ** attempt)
    assert last is not None
    raise last


def _retry_after(resp: httpx.Response, limited: int) -> float:
    try:
        wait = float(resp.headers.get("Retry-After", ""))
    except ValueError:
        wait = RATE_LIMIT_WAIT * limited
    return min(max(wait, 1), MAX_WAIT)
