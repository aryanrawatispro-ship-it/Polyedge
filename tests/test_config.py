import pytest

from favorite_hunter.config import Settings, load_settings


def test_example_config_equals_defaults():
    loaded = load_settings("config.example.yaml")
    assert loaded.model_dump() == Settings().model_dump()


def test_invalid_band_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("scanner:\n  price_min: 0.95\n  price_max: 0.90\n")
    with pytest.raises(ValueError):
        load_settings(path)


def test_paper_mode_only():
    with pytest.raises(ValueError):
        Settings.model_validate({"mode": "live"})
