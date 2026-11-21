"""Konstanter for Varmemodel."""

from __future__ import annotations

DOMAIN = "varmemodel"

CONF_NAME = "name"
CONF_HEAT = "heat_energy"
CONF_ELECTRICITY = "electricity_energy"
CONF_OUTDOOR = "outdoor_temp"
CONF_INDOOR = "indoor_temps"
CONF_FLOW = "flow_temp"
CONF_RETURN = "return_temp"
CONF_COMPRESSOR = "compressor"
CONF_DEFROST = "defrost"
CONF_DHW = "dhw"
CONF_SETPOINT = "flow_setpoint"
CONF_SETPOINT_ATTR = "flow_setpoint_attribute"
CONF_OFFSET = "curve_offset"
CONF_OFFSET_ATTR = "curve_offset_attribute"
CONF_OFFSET_ZERO = "curve_offset_zero"
CONF_WINDOW_DAYS = "window_days"

DEFAULT_NAME = "Varmepumpe"
DEFAULT_WINDOW_DAYS = 60
COLLECT_KEEP_DAYS = 120
BACKFILL_DAYS = 14

MODE_CALIBRATED = "calibrated"
MODE_FROZEN = "frozen"

# Rækkefølge og enheder for kalibreringstallene (nøgle → enhed)
FIT_KEYS: dict[str, str | None] = {
    "heat_loss": "W/K",
    "cop": None,
    "emission": "kW/K",
    "max_power": "kW",
    "min_power": "kW",
    "curve": "°C",
    "defrost": None,
    "cooling": "h",
}

CALIBRATION_HOUR = 3
CALIBRATION_MINUTE = 17
