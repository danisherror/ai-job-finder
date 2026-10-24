import pytest

from src import digest


@pytest.fixture(autouse=True)
def _temp_exports(monkeypatch, tmp_path):
    """Keep every test's CSV exports out of the real private/exports folder."""
    monkeypatch.setattr(digest, "EXPORTS", tmp_path / "exports")
