"""One adapter per source type. Each exposes fetch(source) -> list[Job]."""

from src.collect import ashby, greenhouse, lever

ADAPTERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
}
