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
CONF_DHW_TEMP = "dhw_temp"  # varmtvandstankens temperatur (°C) – med den lærer Varmemodel tanken og lægger en plan for varmt vand
CONF_DHW_SWITCH = "dhw_switch"  # varmt vand til/fra på varmepumpen (til sammenligning med planen)
CONF_DHW_TARGET = "dhw_target"  # varmt vands måltemperatur på varmepumpen (til sammenligning og til varmepumpens startgrænse)
CONF_DHW_BOOST = "dhw_boost"  # »bad nu«: varmt vand til »højt« med det samme, også i en blokeret time
CONF_DHW_FALLBACK = "dhw_fallback"  # reserve for varmt vand til/fra og mål (fx producentens sky, når den primære er lokal)
DHW_CONTROL = "dhw_control"  # kontakt: Varmemodel styrer varmt vand (fra = kun anbefaling)
DHW_PRIORITY = "dhw_priority"  # varmt vands egen prioritet: økonomi – balance – komfort
DHW_TIMES = (("dhw_use_start", "00:00"), ("dhw_use_end", "00:00"))  # brugstiden: fra–til (ens = hele døgnet / det lærte)
DHW_MAX_WRITES = 48  # sikring mod en løbsk løkke: højst så mange skrivninger til varmt vand pr. døgn
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
CONF_PRICE_CAP_ENTITY = "price_cap_entity"  # prisloftet (tal-hjælper, kr pr. kWh el), som de blokerede kvarterer regnes efter – vises og ændres i fanen
BLOCK_QUARTERS_MIN = 2
# En sådan time er delvist fri for vandbårne kilder, når de blokerede kvarterer ligger samlet i den ene ende og mindst så
# mange kvarterer er frie: den frie del bruges som fortsættelse af nabotimen (fx rødt fra 06:30 → varme til 06:30)
PARTIAL_FREE_MIN = 2
LEARN_IGNORE_WORDS = ("ignorer", "ignore")  # kalender-begivenheder med disse ord springes over i læringen (og er ikke tiltag)
AIR_EFF_BACKFILL_DAYS = 14  # stigens virkning pr. trin læres også fra så mange dages historik
AIR_EFF_BF_VERSION = 3  # tilbagefyldningen køres igen automatisk, når metoden ændres
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
RESTART_S = 600  # en genstart så kort efter et stop, der ikke tæller (ventilskift, afrimning, varmt vand), er samme kørsel
DHW_SHIFT_S = 120  # et stop højst så længe før varmt vand melder tændt er skiftet til varmt vand – intet rigtigt stop
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
PRIORITY = "priority"  # husets prioritet: økonomi – balance – komfort (valg-entitet; trinnene står i planner.PRIORITIES)
PRIORITY_DEFAULT = "balance"
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
)

# Varmt vand (tal-entiteter, kun når en vandbåren kilde har en tankføler): brugerens tre temperaturer – alt andet om tanken
# læres af målinger (dhw.learn_tank)
DHW_SETTINGS = (
    ("dhw_min", 25, 55, 0.5, "°C", 40.0),  # mindst: så varm skal tanken være, når en time begynder
    ("dhw_normal", 30, 60, 0.5, "°C", 45.0),  # normalt: målet for en almindelig opvarmning
    ("dhw_high", 35, 65, 0.5, "°C", 50.0),  # højt: »bad nu« og opfyldning før en lang blokering
)

# Luft-luft-styring (pr. varmekilde, kun når der er en klimaentitet): tal, valg, tider og kontakten
CONF_CLIMATE = "climate"
CONF_EL_POWER = "el_power"  # luft-luft: eleffekt (W) – til forbrug pr. trin (energimålere er for grove pr. kvarter)
CONF_ASSIST = "assist_fan"  # luft-luft: ventilation der fordeler varmen (til/fra-tilstand)
CONF_ASSIST_SPEED = "assist_fan_speed"  # valgfri: dens hastighed (0 = står)
AIR_SELECTS = ("air_priority", "air_fan_min", "air_fan_day", "air_fan_night", "air_swing_v", "air_swing_h", "air_ref", "air_sp_low", "air_sp_top")
AIR_REF = "air_ref"  # føleren ved enheden, som stigens temperaturer måles fra: enhedens egen eller et af kildens rum
AIR_REF_UNIT = "enheden"  # valget »enhedens egen føler«
AIR_SP_TOP = "air_sp_top"  # stigens højeste setpunkt (°C) – standard: rummenes højeste gulv + AIR_SPAN
AIR_SP_LOW = "air_sp_low"  # stigens laveste setpunkt (°C) – aldrig under rummenes højeste gulv: der skal mindst være så varmt ved enheden, som de rum, den betjener, skal holde
AIR_ANCHOR_BELOW = 1.5  # stigens laveste trin ligger så langt under føleren ved enheden: varmepumpen kører på sin laveste effekt (længere nede stopper og starter den selv)
AIR_ANCHOR_HYST = 0.1  # ankeret flytter først, når føleren er så langt forbi afrundingens grænse (ellers skifter setpunktet frem og tilbage)
AIR_SPAN = 3.0  # stigens temperaturer: fra laveste trin og så mange °C op (højst til stigens top)
AIR_LADDER_VERSION = 2  # stigens opbygning: ændres den, skifter trinenes navne betydning, og det lærte samles forfra
AIR_PRIORITY = "air_priority"  # luft-luft-kildens prioritet: økonomi – balance – komfort (afløser »Opstart« rolig/hurtig)
AIR_START_MAP = {"rolig": "balance", "hurtig": "comfort"}  # den gamle »Opstart« (til 0.28) oversat til prioritet
AIR_SOFT_UP_EVERY = 1800  # økonomi og balance: højst ét trin op pr. halve time
AIR_SOFT_SHARE = 0.5  # økonomi og balance: start højst midt på stigen (fx trin 6 af 0–11), til virkningen pr. trin er lært
AIR_SOFT_MIN_RISE = 0.2  # balance: et rum under gulvet, der stiger mindst så meget (°C/t) på trinnet, får ikke et trin mere (økonomi: planens egen stigning, højst dette)
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
# Opvarmningen (prioriteten bestemmer, hvor hårdt varmepumpen presses på vej mod planens fremløb – se calibration.lift_levels)
APPR_EVAL_S = 300  # mens loftet holder fremløbet nede, følges returen så tit
APPR_TRIM_S = 840  # effekten måles over så lang tid, før løftet rettes (og så længe mellem to rettelser)
APPR_TRIM_MAX = 2  # højst så mange grader lægges på løftet, når den målte effekt ligger under varmepumpens laveste stabile
APPR_NEED = 1.25  # trinnets effekt skal dække husets behov nu med så meget, før loftet også gælder rummenes egne hævninger
# Oversigten »Varmemodel mod jævn drift«
CMP_MIN_HOURS = 18  # så mange timer af et døgn skal have pris og måling (uden varmt vand), før døgnet regnes
CMP_KEEP_DAYS = 62  # så mange døgn gemmes
UNCHANGED = "uændret"
