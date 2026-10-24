"""One adapter per source type. Each exposes fetch(source, known) -> list[Job].

`known` holds the ats_ids already stored for that source. Adapters that need an extra call
per job for its description (SmartRecruiters, Workday, Oracle, Eightfold, Rippling, YC, RippleHire,
Phenom, Avature, and the sitemap readers in jobpages) skip that call for known jobs and return them
without a description; the store then keeps the saved one (and the saved title, if none is given).
"""

from src.collect import (amazon, ashby, atlassian, avature, eightfold, greenhouse, hackernews, himalayas, jibe,
                         jobpages, lever, oracle, phenom, portals, remoteboards, ripplehire, rippling, smartrecruiters,
                         weworkremotely, workable, workday, ycombinator)

ADAPTERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "workday": workday.fetch,
    "workable": workable.fetch,
    "himalayas": himalayas.fetch,
    "amazon": amazon.fetch,
    "oracle": oracle.fetch,
    "eightfold": eightfold.fetch,
    "rippling": rippling.fetch,
    "weworkremotely": weworkremotely.fetch,
    "ycombinator": ycombinator.fetch,
    "jibe": jibe.fetch,
    "hackernews": hackernews.fetch,
    "atlassian": atlassian.fetch,
    "ripplehire": ripplehire.fetch,
    "phenom": phenom.fetch,
    "avature": avature.fetch,
    **jobpages.FETCHERS,
    **remoteboards.FETCHERS,
    **portals.FETCHERS,
}
