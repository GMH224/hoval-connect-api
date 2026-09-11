"""Constants for the Hoval Connect integration."""

from datetime import timedelta
from math import isfinite

DOMAIN = "hoval_connect"

# API endpoints
BASE_URL = "https://azure-iot-prod.hoval.com/core"
IDP_URL = "https://akwc5scsc.accounts.ondemand.com/oauth2/token"
# Public OAuth2 client_id for the Hoval Connect mobile app (same for all users).
# Extracted from the official Android/iOS app; required by the SAP IAS identity provider.
CLIENT_ID = "991b54b2-7e67-47ef-81fe-572e21c59899"

# Custom User-Agent sent on every outbound request (IDP token calls and all
# BASE_URL calls).
#
# ⚠️ DO NOT CHANGE THIS STRING WITHOUT RE-VALIDATING AGAINST THE LIVE API. ⚠️
# This exact value is empirically proven (not guessed) to get past Hoval's
# Azure Application Gateway. See docs/audit-v0.24.0.md for the full
# investigation; summary below.
#
# History: v0.23.0 first added a User-Agent here (a different string,
# "HovalConnectHomeAssistant/1.0 ...") as a fix for a blanket HTTP 403 on
# every endpoint. That release shipped without live confirmation it worked —
# it didn't. The 403 persisted. Root-causing it properly (v0.24.0) required
# isolating one variable at a time directly against the live API and found
# TWO independent causes stacked on top of each other:
#
#   1. aiohttp's TLS connection fingerprint is blocked outright, regardless
#      of any header content — confirmed across default aiohttp, aiohttp +
#      this same custom User-Agent, aiohttp + more headers, and aiohttp with
#      its cipher suite list rebuilt to exactly match urllib3's. All HTTP 403.
#      This is why v0.23.0's User-Agent fix alone did not work: it was aimed
#      at aiohttp, which was blocked for an unrelated, lower-level reason.
#   2. `requests`' own DEFAULT User-Agent string, "python-requests/X.Y.Z", is
#      SEPARATELY blocked — almost certainly a WAF rule against well-known
#      scripting-tool identities. Confirmed by changing only this one string,
#      nothing else, on an otherwise byte-identical plain-requests script:
#      HTTP 403 with the default UA, HTTP 200 with a custom one.
#
# v0.24.0 therefore does two things together: switches the transport from
# aiohttp to requests-in-executor (api.py), AND keeps sending an explicit,
# non-default User-Agent, because #2 above still applies to requests too.
# The specific string below is the exact one used in the successful test —
# not a stylistic choice. If you want a different, more "branded" string,
# it must be validated against the live API first (see docs/audit-v0.24.0.md
# for the minimal test-script pattern used) before it replaces this one.
USER_AGENT = "hoval-connect-forensic-crawler/1.0 (+https://github.com/; diagnostic tool)"

# Token TTLs (with safety margins)
ID_TOKEN_TTL = timedelta(minutes=25)
PLANT_TOKEN_TTL = timedelta(minutes=12)

# v1.0.0 — "lighter" architecture: this integration no longer polls
# telemetry (live values, weather, events) on any schedule. All telemetry
# now comes from a separate, CAN-bus-based HACS integration the user runs
# alongside this one; this integration's job is control (writes) plus one
# lightweight scheduled check that the cloud API is still reachable at all.
# See docs/audit-v1.0.0.md for the full design rationale.
#
# History: v1.0.0 initially removed the old user-configurable scan_interval
# entirely (CONF_SCAN_INTERVAL / SCAN_INTERVAL_OPTIONS / DEFAULT_SCAN_INTERVAL),
# reasoning that there was no longer a meaningful "poll rate" to tune once
# circuit/program/settings data moved to fetch-once-at-startup-and-after-
# writes rather than a schedule. Reinstated as CONF_HEALTH_CHECK_INTERVAL,
# still in this same v1.0.0 release (before deployment — see CHANGELOG.md),
# at the user's explicit request: even a minimal health check's cadence is
# still worth tuning, since it directly controls how quickly the
# "Cloud API problem" diagnostic can react (see CLOUD_API_PROBLEM_THRESHOLD
# below) and how much standing cloud traffic the integration generates.
# This is a narrower, differently-scoped option than the old one — it only
# affects the one lightweight scheduled call this integration still makes,
# never circuit/program/settings data (which is unaffected regardless of
# this setting, per the design above).
CONF_HEALTH_CHECK_INTERVAL = "health_check_interval"
# Options given in seconds, matching the old SCAN_INTERVAL_OPTIONS
# convention. Deliberately coarser granularity than that option had (this
# is a reachability heartbeat, not a telemetry poll — second-level
# precision was never meaningful here): 10/15/30/60/120 minutes.
HEALTH_CHECK_INTERVAL_OPTIONS = [600, 900, 1800, 3600, 7200]
DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 1800  # 30 minutes — the original v1.0.0 default
# Used as the fallback wherever a timedelta (not raw seconds) is needed —
# e.g. before a config entry's options have ever been read, or in contexts
# without a config entry at all (some tests construct a coordinator this way).
HEALTH_CHECK_INTERVAL = timedelta(seconds=DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS)

# How long since the last successful cloud contact (a health check OR an
# actual successful write, whichever is more recent) before the diagnostic
# "Cloud API problem" binary sensor turns on. Deliberately patient — a
# single missed health check should not trip it; several in a row should.
# NOTE: this stays fixed regardless of the user's chosen
# CONF_HEALTH_CHECK_INTERVAL. Choosing a long health-check interval (e.g.
# 2 hours) means this threshold gets little to no "several in a row"
# margin before tripping on a single missed check — an accepted
# consequence of that choice, not a bug; not scaled automatically since
# the user did not ask for that and it would make this constant's
# behavior surprising/implicit rather than a fixed, documented promise.
CLOUD_API_PROBLEM_THRESHOLD = timedelta(hours=2)

# Program cache TTL — programs change rarely, no need to fetch every poll
PROGRAM_CACHE_TTL = timedelta(minutes=5)

# Circuit settings (weather-based control weighting) cache TTL — these values
# are configuration, not telemetry; they only change when a user drags a
# slider, so there is no value in re-fetching them every poll cycle.
CIRCUIT_SETTINGS_CACHE_TTL = timedelta(minutes=10)

# Circuit types
CIRCUIT_TYPE_HV = "HV"
CIRCUIT_TYPE_HK = "HK"
CIRCUIT_TYPE_BL = "BL"
CIRCUIT_TYPE_WW = "WW"
CIRCUIT_TYPE_FRIWA = "FRIWA"
CIRCUIT_TYPE_SOL = "SOL"
CIRCUIT_TYPE_SOLB = "SOLB"
CIRCUIT_TYPE_PS = "PS"
CIRCUIT_TYPE_GW = "GW"

# Supported circuit types for this integration
SUPPORTED_CIRCUIT_TYPES = {CIRCUIT_TYPE_HV, CIRCUIT_TYPE_HK, CIRCUIT_TYPE_BL, CIRCUIT_TYPE_WW}

# Circuit types that expose the "weather based control" weighting sliders
# (Eco <-> Comfort, by outside temperature / by solar radiation) added to the
# Hoval Connect app in 2026-07. Confirmed against HK (heating circuit) via the
# app screenshot; other circuit types are not confirmed to support the
# `CircuitSettingsDTO.weatherImpact` sub-object and are deliberately excluded
# to avoid firing an unnecessary (and possibly erroring) request against an
# endpoint the circuit type doesn't implement.
SUPPORTS_WEATHER_IMPACT = frozenset({CIRCUIT_TYPE_HK})

# Circuit types whose cloud endpoint actually has a time-program to fetch.
# BL (boiler/heat source) is a supported, always-polled circuit type (see
# _NON_SELECTABLE_TYPES in coordinator.py) but has no schedule of its own —
# confirmed via forensic crawl (2026-09): GET .../circuits/{path}/programs
# returns HTTP 417 for BL every time, never 200. Excluding it from
# SUPPORTS_PROGRAMS stops the coordinator from making a call that is
# guaranteed to fail on every cache-refresh cycle; the existing exception
# handling around get_programs() means this was never a crash, only wasted
# round-trips and noise in the debug log.
SUPPORTS_PROGRAMS = frozenset({CIRCUIT_TYPE_HV, CIRCUIT_TYPE_HK, CIRCUIT_TYPE_WW})

# Human-readable names for circuit types
CIRCUIT_TYPE_NAMES = {
    CIRCUIT_TYPE_HV: "HomeVent",
    CIRCUIT_TYPE_HK: "Heating Circuit",
    CIRCUIT_TYPE_BL: "Boiler",
    CIRCUIT_TYPE_WW: "Hot Water",
    CIRCUIT_TYPE_FRIWA: "Fresh Water",
    CIRCUIT_TYPE_SOL: "Solar",
    CIRCUIT_TYPE_SOLB: "Solar Buffer",
    CIRCUIT_TYPE_PS: "Pool",
    CIRCUIT_TYPE_GW: "Gateway",
}

# Hoval operation modes
OPERATION_MODE_REGULAR = "REGULAR"
OPERATION_MODE_STANDBY = "standby"

# Temporary change duration options (API enum)
DURATION_FOUR_HOURS = "FOUR"
DURATION_MIDNIGHT = "MIDNIGHT"
CONF_OVERRIDE_DURATION = "override_duration"
DEFAULT_OVERRIDE_DURATION = DURATION_FOUR_HOURS
# Independent audit finding (2026-09, fourth round, HVC-007): the options
# form only ever writes one of these two values, but persisted config-entry
# options are read directly at use time with no re-validation — an
# out-of-band value (hand-edited storage, a future migration bug) would
# otherwise reach the API unchanged. See fan.py's _override_duration.
VALID_OVERRIDE_DURATIONS = frozenset({DURATION_FOUR_HOURS, DURATION_MIDNIGHT})

# Turn-on mode options (what happens when fan is turned on from standby)
TURN_ON_RESUME = "resume"
TURN_ON_WEEK1 = "week1"
TURN_ON_WEEK2 = "week2"
CONF_TURN_ON_MODE = "turn_on_mode"
DEFAULT_TURN_ON_MODE = TURN_ON_RESUME
# Same rationale as VALID_OVERRIDE_DURATIONS above. See fan.py's _turn_on_mode.
VALID_TURN_ON_MODES = frozenset({TURN_ON_RESUME, TURN_ON_WEEK1, TURN_ON_WEEK2})

# HV (HomeVent) air-volume operating bounds, in percent.
# The Hoval cloud/firmware rejects or undefined-behaves on values below the
# device minimum; the fan entity clamps user/automation requests into this band
# before sending a temporary-change command.
HV_AIR_VOLUME_MIN = 15
HV_AIR_VOLUME_MAX = 100


def clamp_temperature(value: float, minimum: float, maximum: float) -> float:
    """Clamp a requested temperature into a climate/water-heater entity's declared band.

    Independent audit finding (2026-09, HVC-007): climate.py and
    water_heater.py declared _attr_min_temp/_attr_max_temp but never
    actually enforced them on async_set_temperature() — a service call or
    automation could send any float straight to the API. Generic (not one
    bespoke function per entity, since climate and water heater have
    different bounds) so both call sites share one tested implementation.

    Raises ValueError for non-finite input (NaN, +-inf): clamping those
    against a numeric range is not well-defined (Python's min/max do not
    reliably reject NaN), and a service call passing one is almost always a
    caller bug that should surface as a rejected call, not a silently
    "clamped" result that doesn't correspond to what was asked for.
    """
    if not isfinite(value):
        raise ValueError(f"Temperature must be a finite number, got {value!r}")
    return max(minimum, min(maximum, value))


def clamp_hv_air_volume(percentage: float) -> int:
    """Clamp a requested HV air-volume percentage into the device's valid band.

    Pure helper (no HA imports) so it is directly unit-testable. Returns an int
    in [HV_AIR_VOLUME_MIN, HV_AIR_VOLUME_MAX].

    Raises ValueError for non-finite input (independent audit finding,
    2026-09, HVC-ICS-004): without this guard, Python's min/max silently
    turn NaN into a valid-looking endpoint value (e.g.
    `min(HV_AIR_VOLUME_MAX, float("nan"))` returns HV_AIR_VOLUME_MAX, not
    an error) — an invalid automation/service value would silently become
    a legitimate-looking API setting instead of being rejected. Same fix
    as clamp_temperature() above, applied here for consistency.
    """
    if not isfinite(percentage):
        raise ValueError(f"Air volume percentage must be a finite number, got {percentage!r}")
    return int(max(HV_AIR_VOLUME_MIN, min(HV_AIR_VOLUME_MAX, percentage)))


# Weather-based control weighting bounds ("Eco" <-> "Comfort" sliders), per
# CircuitSettingsDTO.weatherImpact in docs/openapi-v3.json:
#   outsideTemperature: integer, 0..100   (0 = full Eco, 100 = full Comfort)
#   solarRadiation:     double,  -10..0   (-10 = full Eco, 0 = full Comfort)
# In both cases the minimum of the documented range is the "Eco" end of the
# app's slider and the maximum is the "Comfort" end, so a plain min->max
# HA number slider reproduces the app's Eco/Comfort control without any extra
# UI-side rescaling that could silently send a different physical value than
# what the slider position implies.
WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MIN = 0
WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MAX = 100
WEATHER_IMPACT_SOLAR_RADIATION_MIN = -10.0
WEATHER_IMPACT_SOLAR_RADIATION_MAX = 0.0


def clamp_weather_impact_outside_temperature(value: float) -> int:
    """Clamp a requested outside-temperature weighting into the API's valid band.

    Pure helper (no HA imports) so it is directly unit-testable.

    Raises ValueError for non-finite input — see clamp_hv_air_volume's
    docstring (HVC-ICS-004) for why this guard exists.
    """
    if not isfinite(value):
        raise ValueError(f"Outside-temperature weighting must be a finite number, got {value!r}")
    return int(
        max(
            WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MIN,
            min(WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MAX, value),
        )
    )


def clamp_weather_impact_solar_radiation(value: float) -> float:
    """Clamp a requested solar-radiation weighting into the API's valid band.

    Pure helper (no HA imports) so it is directly unit-testable.

    Raises ValueError for non-finite input — see clamp_hv_air_volume's
    docstring (HVC-ICS-004) for why this guard exists.
    """
    if not isfinite(value):
        raise ValueError(f"Solar-radiation weighting must be a finite number, got {value!r}")
    return float(
        max(
            WEATHER_IMPACT_SOLAR_RADIATION_MIN,
            min(WEATHER_IMPACT_SOLAR_RADIATION_MAX, value),
        )
    )


# Service names
SERVICE_RESET_WW_BOOST = "reset_ww_boost"

# Persistent health storage
# Increment HEALTH_STORAGE_VERSION whenever the stored schema changes in a
# backwards-incompatible way.
#
# IMPORTANT, verified directly against HA's Store source
# (homeassistant/helpers/storage.py) rather than assumed: HA does NOT
# silently discard a version-mismatched file on its own. Without an
# overridden _async_migrate_func (which this integration has never
# provided), Store.async_load() raises — UnsupportedStorageVersionError if
# the file's version is NEWER than requested (e.g. after rolling back to an
# older release), or a re-raised NotImplementedError if OLDER (e.g. right
# after upgrading past a version bump like this one). Either way, an
# uncaught raise here would fail the whole integration's setup, not just
# lose historical counters. __init__.py's async_setup_entry wraps the
# health_store.async_load() call in a broad try/except specifically because
# of this — see the comment there before removing it.
#
# Bumped 1 -> 2 in v1.0.0: per-circuit health tracking (tied to the
# now-removed live-values polling) was dropped, and a new
# last_successful_contact_at field was added. Thanks to the try/except in
# __init__.py, a version mismatch in either direction now degrades to
# "start fresh" instead of blocking setup — losing a few days of cumulative
# counters on upgrade or rollback is an acceptable, one-time cost.
HEALTH_STORAGE_KEY = f"{DOMAIN}_health"
HEALTH_STORAGE_VERSION = 2
