import pytest

from src import normalize as n


@pytest.mark.parametrize("location, mode, desc, country, expected", [
    ("Bengaluru-VTP, India", "onsite", "", None, "yes"),
    ("Bangalore - WF", "onsite", "", None, "yes"),
    ("Surat, Gujarat", "onsite", "", "IN", "yes"),            # Lever country code
    ("Remote - India", "remote", "", None, "yes"),
    ("Remote Globally", "remote", "", None, "yes"),
    ("Remote, APAC", "remote", "", None, "yes"),
    ("Remote, Global", "remote", "Must be located in the US.", None, "unclear"),
    ("United States | Distributed, Global", "remote", "", None, "unclear"),
    ("Remote - US", "remote", "", None, "no"),
    ("Sweden (Remote)", "remote", "", None, "no"),
    ("Remote, AMER", "remote", "", None, "no"),
    ("U.S. Anywhere | United States", "remote", "", None, "no"),   # anywhere, but only in the US
    ("Anywhere in Canada", "remote", "", None, "no"),
    ("Remote - EMEA", "remote", "", None, "no"),
    ("Bangalore | Remote - US", "remote", "", None, "yes"),
    ("Indianapolis, IN", "remote", "", None, "no"),            # "IN" here is Indiana
    ("Remote", "remote", "", None, "unclear"),
    ("Remote", "remote", "This role is US-only.", None, "no"),
    ("San Francisco, CA", "onsite", "", None, "no"),
    ("Hybrid in Santa Clara, CA", "hybrid", "", None, "no"),
])
def test_india_eligibility(location, mode, desc, country, expected):
    assert n.india_eligibility(location, mode, desc, country) == expected


@pytest.mark.parametrize("location, expected", [
    ("Bengaluru-VTP, India", ["Bengaluru"]),
    ("Gurgaon | New Delhi", ["Gurugram", "Delhi"]),
    ("Bombay", ["Mumbai"]),
    ("Remote - US", []),
])
def test_indian_cities(location, expected):
    assert n.indian_cities(location) == expected


@pytest.mark.parametrize("location, ats, is_remote, expected", [
    ("Delhi", "OnSite", None, "onsite"),
    ("Anywhere", None, None, "remote"),
    ("Bangalore", "hybrid", None, "hybrid"),
    ("Pune", None, True, "remote"),
    ("Hybrid in Santa Clara, CA", None, None, "hybrid"),
    ("", None, None, "unknown"),
])
def test_work_mode(location, ats, is_remote, expected):
    assert n.work_mode(location, ats, is_remote) == expected


@pytest.mark.parametrize("text, expected", [
    ("Have 5-8 years of platform engineering", (5, 8)),
    ("3 to 5 yrs of experience", (3, 5)),
    ("5+ years building infrastructure", (5, None)),
    ("Minimum 2 years of full sales cycle experience", (2, None)),
    ("at least 4 years in backend", (4, None)),
    ("3 years of professional experience", (3, None)),
    ("Founded 25 years ago, we are a team of 300", (None, None)),
    ("", (None, None)),
])
def test_experience_range(text, expected):
    assert n.experience_range(text) == expected


def test_html_to_text_unescapes_greenhouse_content():
    raw = "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"
    text = n.html_to_text(raw)
    assert "Hello & welcome" in text
    assert "Python" in text
    assert "<" not in text
