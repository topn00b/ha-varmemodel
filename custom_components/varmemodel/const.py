"""Konstanter for Varmemodel."""

from __future__ import annotations

DOMAIN = "varmemodel"

CONF_NAME = "name"
CONF_HEAT = "heat_energy"
CONF_ELECTRICITY = "electricity_energy"
CONF_ELECTRICITY_BACKUP = "electricity_backup"  # supplerende/ældre elmåler, bruges hvor hovedmåleren mangler data
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
CONF_WEATHER = "weather"

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
    "cop_steady": None,
    "cop_loss": "%",
    "emission": "kW/K",
    "max_power": "kW",
    "min_power": "kW",
    "curve": "°C",
    "defrost": None,
    "cooling": "h",
    "wind_effect": "%",
}

# Øges når selve regnemetoden ændres: så regnes tallene forfra uden ±10 %-grænsen
CALIB_VERSION = 4

CALIBRATION_HOUR = 3
CALIBRATION_MINUTE = 17

# Rum (underpunkter)
SUBENTRY_ROOM = "room"
CONF_ROOM_NAME = "name"
CONF_ROOM_TEMP = "temperature"
CONF_ROOM_MIN_ENTITY = "minimum_entity"
CONF_ROOM_MIN = "minimum"
CONF_ROOM_VALVE = "valve"
CONF_ROOM_ALWAYS_OPEN = "always_open"
CONF_ROOM_WEIGHT = "loop_weight"
CONF_ROOM_IN_PLAN = "in_plan"
CONF_ROOM_SOURCE = "heat_source"  # varmekilden som rummets termostat/telestat styrer (underpunkt-id)
CONF_ROOM_EXTRA_SOURCES = "extra_sources"  # andre varmekilder, der også kan varme rummet direkte
CONF_ROOM_NEIGHBOURS = "neighbours"  # nabozoner (temperaturfølere), fx kælder eller trappeopgang

# Varmekilder (underpunkter)
SUBENTRY_SOURCE = "source"
CONF_SOURCE_NAME = "name"
CONF_SOURCE_TYPE = "source_type"
SOURCE_HYDRONIC = "hydronic"  # luft-vand / vandbåren
SOURCE_AIR_AIR = "air_air"  # luft-luft
CONF_UNIT_TEMP = "unit_temp"  # luft-luft: enhedens egen føler (entitet)
CONF_UNIT_TEMP_ATTR = "unit_temp_attribute"
CONF_LEGACY_IDS = "legacy_ids"  # den varmekilde, der kom fra version 1, beholder sine entitets-id'er
CONF_MIGRATED_SOURCE = "migrated_source"

# Felter der hører til en vandbåren varmekilde (flyttes fra huset ved opgradering fra version 1)
HYDRONIC_KEYS = (
    "electricity_energy", "heat_energy", "flow_temp", "return_temp", "compressor", "defrost", "dhw",
    "flow_setpoint", "flow_setpoint_attribute", "curve_offset", "curve_offset_attribute", "curve_offset_zero",
)

# Planlægger (huset)
CONF_PRICE_ENTITY = "price_entity"
CONF_PRICE_ATTR = "price_attribute"
CONF_BLOCK_ENTITY = "block_entity"
CONF_BLOCK_CODES = "block_codes"
CONF_MAX_PAUSE = "max_pause"
CONF_COMFORT_OFFSET = "comfort_offset"
CONF_BOOST_ENTITY = "boost_entity"
CONF_BOOST_DELTA = "boost_delta"
PLAN_HORIZON_H = 36
