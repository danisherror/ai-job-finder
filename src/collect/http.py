"""Polite JSON fetching shared by all adapters."""

import time

import httpx

HEADERS = {"User-Agent": "ai-job-finder/0.1 (private job digest)"}  # Ashby returns 403 without one
TIMEOUT = 30
RETRIES = 3


class SourceError(Exception):
    """A source failed. `reason` is short and goes into the run records."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def get_json(url: str, params: dict | None = None):
    """GET a URL and return parsed JSON, retrying temporary failures with backoff."""
    return _request("GET", url, params=params)


def post_json(url: str, body: dict):
    """POST a JSON body and return parsed JSON (Workday's search works this way)."""
    return _request("POST", url, body=body)


def _request(method: str, url: str, params: dict | None = None, body: dict | None = None):
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = httpx.request(method, url, params=params, json=body, headers=HEADERS, timeout=TIMEOUT,
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
            if resp.status_code == 429 or resp.status_code >= 500:
                last = SourceError("http", f"HTTP {resp.status_code}")
            elif resp.status_code != 200:
                raise SourceError("http", f"HTTP {resp.status_code}")
            else:
                try:
                    return resp.json()
                except ValueError as e:
                    raise SourceError("bad_response", "not JSON") from e
        time.sleep(2 ** (attempt + 1))
    assert last is not None
    raise last
