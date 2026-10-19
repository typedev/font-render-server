import pytest

from font_render import config
from font_render.stats import stats


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Admin setting overrides go to a temp file and are undone after each test."""
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    saved = {name: getattr(config, name) for name in config.EDITABLE}
    overrides = dict(config.OVERRIDES)
    yield
    for name, value in saved.items():
        setattr(config, name, value)
    config.OVERRIDES.clear()
    config.OVERRIDES.update(overrides)


@pytest.fixture(autouse=True)
def _isolated_stats(tmp_path, monkeypatch):
    """Keep data/stats.json of the checkout out of tests; start each test with empty stats."""
    monkeypatch.setattr(config, "STATS_FILE", tmp_path / "stats.json")
    stats.reset()
    yield
    if stats.episode is not None:
        stats.episode = None
