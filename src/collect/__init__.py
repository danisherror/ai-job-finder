"""One adapter per source type. Each exposes fetch(source, known) -> list[Job].

`known` holds the ats_ids already stored for that source. Adapters that need an extra call
per job for its description (SmartRecruiters, Workday, Oracle, Eightfold, Rippling, YC) skip
that call for known jobs and return them without a description; the store then keeps the saved one.
"""

from src.collect import (amazon, ashby, eightfold, greenhouse, himalayas, jibe, lever, oracle, remoteboards,
                         rippling, smartrecruiters, weworkremotely, workable, workday, ycombinator)

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
    **remoteboards.FETCHERS,
}
