"""Turn messy ATS fields into clean values: text, cities, work mode, India eligibility, experience."""

import html
import re
from html.parser import HTMLParser

# ---------------------------------------------------------------- text


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(raw: str | None) -> str:
    """HTML (possibly entity-escaped, as Greenhouse sends it) to plain text."""
    if not raw:
        return ""
    if "&lt;" in raw:
        raw = html.unescape(raw)
    parser = _TextExtractor()
    parser.feed(raw)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# ---------------------------------------------------------------- cities

CITY_ALIASES = {
    "Bengaluru": ["bengaluru", "bangalore", "blr"],
    "Hyderabad": ["hyderabad"],
    "Pune": ["pune"],
    "Mumbai": ["mumbai", "bombay", "navi mumbai"],
    "Gurugram": ["gurugram", "gurgaon"],
    "Noida": ["noida"],
    "Delhi": ["new delhi", "delhi", "ncr"],
    "Chennai": ["chennai", "madras"],
    "Kolkata": ["kolkata", "calcutta"],
    "Ahmedabad": ["ahmedabad"],
    "Jaipur": ["jaipur"],
    "Kochi": ["kochi", "cochin"],
    "Coimbatore": ["coimbatore"],
    "Indore": ["indore"],
    "Chandigarh": ["chandigarh"],
    "Mohali": ["mohali"],
    "Thiruvananthapuram": ["thiruvananthapuram", "trivandrum"],
    "Surat": ["surat"],
    "Mysuru": ["mysuru", "mysore"],
    "Mangaluru": ["mangaluru", "mangalore"],
    "Bhubaneswar": ["bhubaneswar"],
    "Vadodara": ["vadodara", "baroda"],
    "Nagpur": ["nagpur"],
    "Visakhapatnam": ["visakhapatnam", "vizag"],
    "Lucknow": ["lucknow"],
}
_CITY_RE = {city: re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b", re.I)
            for city, names in CITY_ALIASES.items()}
_INDIA_RE = re.compile(r"\bindia\b", re.I)  # not "IN": that is also Indiana's state code


def indian_cities(location: str) -> list[str]:
    return [city for city, rx in _CITY_RE.items() if rx.search(location)]


def mentions_india(location: str, country_code: str | None = None) -> bool:
    return bool(country_code == "IN" or _INDIA_RE.search(location) or indian_cities(location))


# ---------------------------------------------------------------- work mode

def work_mode(location: str, ats_value: str | None = None, is_remote: bool | None = None) -> str:
    """remote / hybrid / onsite / unknown, from the ATS field first, then location text."""
    v = (ats_value or "").lower().replace("-", "").replace(" ", "")
    if v in ("remote",) or is_remote:
        return "remote"
    if v == "hybrid":
        return "hybrid"
    if v in ("onsite", "inoffice"):
        return "onsite"
    low = location.lower()
    if "hybrid" in low:
        return "hybrid"
    if re.search(r"\bremote\b|\banywhere\b|\bwfh\b|work from home|distributed", low):
        return "remote"
    if location.strip():
        return "onsite"
    return "unknown"


# ---------------------------------------------------------------- India eligibility

_GLOBAL_RE = re.compile(r"\b(anywhere|worldwide|global|globally|apac|asia|asia[- ]pacific|any location)\b", re.I)
# Words that describe *how* someone works, not *where*. What's left after removing these is a place.
_NOT_A_PLACE_RE = re.compile(
    r"\b(remote|distributed|fully|flexible|wfh|work from home|home[- ]based|virtual|zone \d+|"
    r"job requisitions only|anywhere|worldwide|global|globally|any location|in|of|the)\b",
    re.I,
)
# Phrases in descriptions that usually mean "you must live somewhere else".
_RESTRICTION_RE = re.compile(
    r"(must (be )?(located|based|reside|live) in (the )?(us|u\.s\.|united states|usa|canada|uk|europe|eu)"
    r"|(us|u\.s\.|usa|united states)[- ]only"
    r"|only (open to|hiring|considering) (candidates|applicants|people) (in|from|based in) (the )?(us|u\.s\.|united states|usa|canada|uk|europe|eu)"
    r"|authori[sz]ed to work in the (us|u\.s\.|united states)"
    r"|(est|pst|pt|et|cet) (time ?zone|hours)"
    r"|overlap with (us|u\.s\.|eastern|pacific|european) (time|hours|business))",
    re.I,
)


def india_eligibility(location: str, mode: str, description: str = "", country_code: str | None = None) -> str:
    """yes / no / unclear: can someone living in India take this job?

    India location                           -> yes
    Onsite/hybrid outside India              -> no
    Remote, global/APAC, no country named    -> yes (unclear if the description restricts it)
    Remote, global + specific countries      -> unclear (e.g. "United States | Distributed, Global")
    Remote, only non-India places named      -> no  (e.g. "Remote - US", "Sweden (Remote)")
    Plain "Remote", nothing else             -> unclear (no if the description restricts it)
    """
    if mentions_india(location, country_code):
        return "yes"
    if mode != "remote":
        return "no"
    restricted = bool(_RESTRICTION_RE.search(description))
    is_global = bool(_GLOBAL_RE.search(location))
    place_text = _GLOBAL_RE.sub(" ", _NOT_A_PLACE_RE.sub(" ", location))
    names_place = bool(re.search(r"[a-z]", place_text, re.I))
    if is_global and not names_place:
        return "unclear" if restricted else "yes"
    if is_global:
        return "unclear"
    if names_place:
        return "no"
    return "no" if restricted else "unclear"


# ---------------------------------------------------------------- experience

_RANGE_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_MIN_RE = re.compile(
    r"(?:(\d{1,2})\s*\+\s*(?:years?|yrs?))"
    r"|(?:(?:at least|minimum(?: of)?|min\.?|over|more than)\s*(\d{1,2})\s*(?:years?|yrs?))"
    r"|(?:(\d{1,2})\s*(?:years?|yrs?)\s*(?:of\s+)?(?:\w+\s+){0,4}experience)",
    re.I,
)


def experience_range(text: str) -> tuple[int | None, int | None]:
    """Years of experience asked for, e.g. '3-5 years' -> (3, 5), '4+ years' -> (4, None)."""
    if not text:
        return None, None
    m = _RANGE_RE.search(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 <= lo <= hi <= 30:
            return lo, hi
    for m in _MIN_RE.finditer(text):
        n = next(int(g) for g in m.groups() if g)
        if 0 <= n <= 30:
            return n, None
    return None, None
