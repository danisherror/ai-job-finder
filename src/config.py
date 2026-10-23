"""Load and validate the YAML config files."""

import os
from pathlib import Path

import yaml

from src.models import Profile, Source

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "config" / "sources.yaml"
PROFILES_DIR = ROOT / "config" / "profiles"
ENV_FILE = ROOT / ".env"


def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    raw = yaml.safe_load(path.read_text()) or []
    sources = [Source(**item) for item in raw]
    names = [s.name for s in sources]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"Duplicate source names in {path.name}: {sorted(dupes)}")
    return sources


def load_profiles(directory: Path = PROFILES_DIR) -> list[Profile]:
    """Every profile except the committed template. A bad file stops the run with a clear error."""
    profiles = []
    for path in sorted(directory.glob("*.yaml")):
        if path.stem == "example":
            continue
        raw = yaml.safe_load(path.read_text()) or {}
        try:
            profiles.append(Profile(id=path.stem, **raw))
        except Exception as e:
            raise ValueError(f"Bad profile {path.name}: {e}") from e
    return profiles


def read_resume(profile: Profile) -> str:
    """Resume as plain text (a .txt or .md file), or "" if none is set."""
    if not profile.resume_file:
        return ""
    path = Path(profile.resume_file)
    if not path.is_absolute():
        path = ROOT / path
    return path.read_text()


def load_env(path: Path = ENV_FILE) -> None:
    """Read KEY=value lines from .env into os.environ (values already set win)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
