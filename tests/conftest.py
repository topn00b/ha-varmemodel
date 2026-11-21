"""Fælles fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
def ha(recorder_mock, enable_custom_integrations, hass):
    """Home Assistant med database (recorder skal startes før hass)."""
    return hass
