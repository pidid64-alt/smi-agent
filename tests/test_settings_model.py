import pytest
from pydantic import ValidationError

from smi_agent.settings_model import DEFAULT_WEIGHTS, load_project_settings


def test_defaults_match_spec():
    cfg = load_project_settings({})
    f = cfg.funnel
    assert (f.stage1, f.stage2, f.stage3, f.final) == (50, 15, 10, 5)
    assert cfg.geo.kz_target == 0.6 and cfg.geo.world_target == 0.4
    assert cfg.mode.value == "learning"  # автопилот выключен по умолчанию
    assert cfg.content.platforms == ["telegram", "instagram", "facebook"]  # WhatsApp исключён
    assert len(DEFAULT_WEIGHTS) == 13


def test_funnel_must_narrow():
    with pytest.raises(ValidationError):
        load_project_settings({"funnel": {"stage1": 10, "stage2": 15, "stage3": 10, "final": 5}})


def test_unknown_mode_rejected():
    with pytest.raises(ValidationError):
        load_project_settings({"mode": "yolo"})
