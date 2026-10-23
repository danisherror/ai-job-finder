"""Load and validate the YAML config files."""

from pathlib import Path

import yaml

from src.models import Source

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "config" / "sources.yaml"


def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    raw = yaml.safe_load(path.read_text()) or []
    sources = [Source(**item) for item in raw]
    names = [s.name for s in sources]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"Duplicate source names in {path.name}: {sorted(dupes)}")
    return sources
