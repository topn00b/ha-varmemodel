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
    "cop_steady": None,
    "cop_loss": "%",
    "emission": "kW/K",
    "max_power": "kW",
    "min_power": "kW",
    "flow_learned": "°C",
    "defrost": None,
    "cooling": "h",
    "wind_effect": "%",
}

# Øges når selve regnemetoden ændres: så regnes tallene forfra uden ±10 %-grænsen
CALIB_VERSION = 5

CALIBRATION_HOUR = 3
CALIBRATION_MINUTE = 17

# Rum (underpunkter)
SUBENTRY_ROOM = "room"
CONF_ROOM_NAME = "name"
CONF_ROOM_TEMP = "temperature"
CONF_ROOM_MIN_ENTITY = "minimum_entity"
CONF_ROOM_MIN = "minimum"
CONF_ROOM_MAX = "maximum"  # loft: luft-luft-planen varmer aldrig rummet over dette
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
)

# Planlægger (huset)
CONF_PRICE_ENTITY = "price_entity"
CONF_PRICE_ATTR = "price_attribute"
CONF_BLOCK_ENTITY = "block_entity"
CONF_BLOCK_CODES = "block_codes"
CONF_BLOCK_NOW_ENTITY = "block_now_entity"  # blokering lige nu (fx et prisloft pr. kvarter) ud over timeplanen
CONF_BLOCK_NOW_STATES = "block_now_states"  # entitetens tilstande, der blokerer (kommasepareret)
DEFAULT_BLOCK_NOW_STATES = "rød, red, r, on"
CONF_BLOCK_QUARTERS_ENTITY = "block_quarters_entity"  # blokerede kvarterer (attribut »kvarterer«: liste af starttider)
BLOCK_QUARTERS_MIN = 2
LEARN_IGNORE_WORDS = ("ignorer", "ignore")  # kalender-begivenheder med disse ord springes over i læringen (og er ikke tiltag)
AIR_EFF_BACKFILL_DAYS = 14  # stigens virkning pr. trin læres også fra så mange dages historik
AIR_EFF_BF_VERSION = 2  # tilbagefyldningen køres igen automatisk, når metoden ændres
AIR_EFF_AIRING_DROP = 0.3  # et rum, der falder mere end så mange °C på et kvarter, er luftet ud – kvarteret bruges ikke  # en time med så mange blokerede kvarterer planlægges som blokeret (ellers klippes kørslen over)
# Søg laveste fremløb (»lavest«): hold målet lavt og ændr det stille og roligt efter rummenes margen og tendens
LOW_URGENT_HOLD = 1800  # et rum under gulvet og ikke på vej op: +2 K højst hver halve time
LOW_UP_HOLD = 3600  # et rum på vej under gulvet inden for 3 timer: +1 K højst hver time
LOW_DOWN_HOLD = 7200  # alle rum med god margen og stabile: −1 K højst hver anden time
LOW_DOWN_MARGIN = 0.5  # °C over gulvet, alle rum skal have, før der sænkes
LOW_RESERVE = 0.1  # °C over gulvet, rummet skal holde om 3 timer (målt tendens)
LOW_LEARN_HOLD = 10800  # målet har holdt rummene i 3 timer uden at hæve: lært som laveste fungerende fremløb
# Startløft: nogle varmepumper kører lavt flow de første minutter efter kompressorstart (fx fast flow-tilstand), så
# fremløbet skyder over målet + varmepumpens egen margen, og kompressoren stopper efter få minutter. Løftet over returen,
# som starten skal have, læres af de korte kørsler (målet − returen ved kompressorstart + 1 K)
START_PHASE_S = 1200  # et stop inden for så mange sekunder efter kompressorstart er en kort kørsel (opstarten)
START_K_MAX = 10.0  # højst så meget lært startløft over returen
START_K_DECAY = 0.25  # en start, der kommer igennem opstarten, sænker det lærte løft så meget (prøver lavere igen)
# Varmemodel bestemmer start og stop – varmepumpens egen termostat skal ikke nå at stoppe kompressoren (start/stop slider)
ANTI_STOP_GAP = 2.0  # stopværn: mens kompressoren kører, holdes målet højst så meget under fremløbet (stop ved ca. målet + 4 °C)
DF_VALVE_S = 120  # et stop så kort efter en afrimning er 4-vejsventilens skift – intet rigtigt stop
DHW_AFTER_S = 600  # efter varmt vand er kredsen varm og fremløbet ustabilt: intet opstartsløft eller stopværn så længe
# Besked forud: hændelsen »varmemodel_varsel«, når planen forudser et rum under sin grænse (fx en lang blokering)
EVENT_VARSEL = "varmemodel_varsel"
VARSEL_MIN = 0.2  # varsles, når et rum forudsiges mindst så meget under grænsen
VARSEL_WORSE = 0.5  # og igen, hvis det bliver så meget værre end sidst varslet (planen bliver ofte lidt værre, når horisonten vokser)
VARSEL_COOLDOWN = 6 * 3600  # højst én samlet besked så ofte (brugeren: "2 x 2 beskeder indenfor 30 min – det virker af meget")
VARSEL_URGENT = 3 * 3600  # undtagen et nyt problem, der begynder inden for så kort tid
# Lang rød dag (brugerens regel): over så mange røde timer i et døgn (røde kvarterer / 4 – rød zone og prisloft) er der noget at
# være opmærksom på – én besked pr. døgn, så snart det er kendt. Rum-beskeden er derefter kun en nødbesked (nyt problem snart)
RED_DAY_H = 8.0
VARSEL_CLEAR = 0.1  # under så lidt (eller ikke under) er problemet væk – et nyt varsles igen
# Kun det usædvanlige: rummets sædvanlige laveste (en almindelig dårlig dag de seneste VARSEL_DAYS døgn) – besked kun, når planen
# forudser mindst VARSEL_UNUSUAL °C koldere end det (brugeren: "ikke beskeder hver dag på noget jeg godt ved")
VARSEL_DAYS = 14
VARSEL_UNUSUAL = 0.3
VARSEL_PCT = 0.2  # fraktilen af døgnenes laveste margen over gulvet, der regnes som »sædvanlig dårlig dag«
CONF_MAX_PAUSE = "max_pause"
CONF_COMFORT_OFFSET = "comfort_offset"
CONF_BOOST_ENTITY = "boost_entity"
CONF_BOOST_DELTA = "boost_delta"
PLAN_HORIZON_H = 48
CONF_BOOST_HOURS = "boost_hours"
CONF_RED_ALLOWANCE = "red_allowance"
CONF_ECONOMY = "economy"
CONF_COLD_CAUTION = "cold_caution"
CONF_PRELOAD = "preload"
CONF_FLOW_MARGIN = "flow_margin"
CONF_MEASURES_CALENDAR = "measures_calendar"
CONF_PLANT_ROOM = "plant_room"
CONF_PLANT_LOSS = "plant_room_loss"
CONF_GROUND_TEMP = "ground_temp"
DEFAULT_PLANT_LOSS = 30.0
DEFAULT_GROUND_TEMP = 9.0
# Planens indstillinger (tal-entiteter): nøgle, min, maks, trin, enhed, standard
SETTINGS = (
    ("max_pause", 0, 24, 1, "h", 0),
    ("comfort_offset", 0, 3, 0.5, "°C", 1.0),
    ("boost_delta", 0, 5, 0.5, "°C", 1.0),
    ("boost_hours", 1, 12, 1, "h", 5),
    ("red_allowance", 0, 3, 0.5, "°C", 0.0),
    # Økonomi: rummene må altid ligge op til så meget under gulvet – planen køber mindre varme og finder et lavere fremløb
    ("economy", 0, 3, 0.25, "°C", 0.0),
    # Koldere end varmetabet er målt ved: + så mange % varmetab (og +0,2 °C margen i rummene); 0 = slået fra
    ("cold_caution", 0, 30, 1, "%", 10),
    # Lavt og jævnt: højst så mange °C over det fremløb, modellen har lært huset bruger ved timens udetemperatur
    ("flow_margin", -5, 5, 0.5, "°C", 0.0),
)

# Luft-luft-styring (pr. varmekilde, kun når der er en klimaentitet): tal, valg, tider og kontakten
CONF_CLIMATE = "climate"
CONF_EL_POWER = "el_power"  # luft-luft: eleffekt (W) – til forbrug pr. trin (energimålere er for grove pr. kvarter)
CONF_ASSIST = "assist_fan"  # luft-luft: ventilation der fordeler varmen (til/fra-tilstand)
CONF_ASSIST_SPEED = "assist_fan_speed"  # valgfri: dens hastighed (0 = står)
AIR_SELECTS = ("air_fan_day", "air_fan_night", "air_swing_v", "air_swing_h", "air_start")
AIR_START_OPTIONS = ("rolig", "hurtig")  # rolig: ét trin pr. halve time og loft ved det effektive trin (30–60 min. længere opvarmning)
AIR_SOFT_UP_EVERY = 1800  # rolig opstart: højst ét trin op pr. halve time
AIR_SOFT_SHARE = 0.5  # rolig opstart: start højst midt på stigen (fx trin 6 af 0–11)
AIR_SOFT_MIN_RISE = 0.2  # rolig opstart: et rum under gulvet, der stiger mindst så meget (°C/t) på trinnet, får ikke et trin mere
AIR_TIMES = (("air_night_start", "22:00"), ("air_night_end", "07:00"))
AIR_SWITCH = "air_control"
AIR_BOOST = "air_boost"
CONF_FLOW_TARGET = "flow_target"  # vandbåren: entiteten Varmemodel skriver fremløbsmålet til (climate eller number)
FLOW_SWITCH = "flow_control"  # vandbåren: Varmemodel styrer fremløbet (tænd/sluk og termostater styres stadig andetsteds)
FLOW_MAX_WRITES = 96  # sikring mod en løbsk løkke; justering af fremløbet skåner ikke noget – tænd/sluk gør
CONF_ONOFF_TARGET = "onoff_target"  # vandbåren: entiteten Varmemodel tænder (varme) og slukker (fx varmepumpens climate)
CONF_ONOFF_FALLBACK = "onoff_fallback"  # vandbåren: reserve for tænd/sluk (fx producentens sky, når den primære er lokal – eller omvendt)
HYDRO_SWITCH = "hydro_control"  # vandbåren: Varmemodel styrer tænd/sluk og fremløb
HYDRO_MAX_FLIPS = 10
SOFT_START = "soft_start"  # vandbåren: blød start – fremløbsmålet løftes gradvist efter tænding (fra / til / test)
SOFT_OPTIONS = ("fra", "til", "test", "lavest")  # lavest: start lavt og søg det laveste fremløb, der holder rummene over gulvet
SOFT_STEP_K = 2.0  # fremløbet hæves så meget pr. kvarter under blød start
SOFT_OVER_RETURN_K = 3.0  # blød start begynder så meget over returen (brugeren: 4 var for hårdt)
SOFT_FLOOR = 20.0  # og aldrig under dette fremløb
SOFT_EVAL_S = 5400  # hver start vurderes over de første 90 min.  # højst så mange tænd/sluk pr. døgn (sluk i blokerede timer går altid igennem)  # højst så mange skrivninger af fremløbsmålet pr. døgn  # »jeg fryser« for luft-luft: øverste trin (powerful), også i blokerede timer, i højst »jeg fryser gælder højst« timer
UNCHANGED = "uændret"
