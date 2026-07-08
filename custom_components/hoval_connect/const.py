"""Constants for the Hoval Connect integration."""

from datetime import timedelta

DOMAIN = "hoval_connect"

# API endpoints
BASE_URL = "https://azure-iot-prod.hoval.com/core"
IDP_URL = "https://akwc5scsc.accounts.ondemand.com/oauth2/token"
# Public OAuth2 client_id for the Hoval Connect mobile app (same for all users).
# Extracted from the official Android/iOS app; required by the SAP IAS identity provider.
CLIENT_ID = "991b54b2-7e67-47ef-81fe-572e21c59899"

# Token TTLs (with safety margins)
ID_TOKEN_TTL = timedelta(minutes=25)
PLANT_TOKEN_TTL = timedelta(minutes=12)

# Polling interval
DEFAULT_SCAN_INTERVAL = timedelta(seconds=60)
CONF_SCAN_INTERVAL = "scan_interval"
SCAN_INTERVAL_OPTIONS = {30: "30 seconds", 60: "60 seconds", 120: "2 minutes", 300: "5 minutes"}

# Program cache TTL — programs change rarely, no need to fetch every poll
PROGRAM_CACHE_TTL = timedelta(minutes=5)

# Circuit settings (weather-based control weighting) cache TTL — these values
# are configuration, not telemetry; they only change when a user drags a
# slider, so there is no value in re-fetching them every poll cycle.
CIRCUIT_SETTINGS_CACHE_TTL = timedelta(minutes=10)

# Plant-level cache TTLs — weather and events are slow-changing and plant-scoped,
# so fetching them on every (default 60 s) poll wastes round-trips against the
# cloud and risks rate-limiting. They are refreshed on their own cadence and the
# last good value is reused in between.
WEATHER_CACHE_TTL = timedelta(minutes=15)
EVENTS_CACHE_TTL = timedelta(minutes=3)

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

# Turn-on mode options (what happens when fan is turned on from standby)
TURN_ON_RESUME = "resume"
TURN_ON_WEEK1 = "week1"
TURN_ON_WEEK2 = "week2"
CONF_TURN_ON_MODE = "turn_on_mode"
DEFAULT_TURN_ON_MODE = TURN_ON_RESUME

# HV (HomeVent) air-volume operating bounds, in percent.
# The Hoval cloud/firmware rejects or undefined-behaves on values below the
# device minimum; the fan entity clamps user/automation requests into this band
# before sending a temporary-change command.
HV_AIR_VOLUME_MIN = 15
HV_AIR_VOLUME_MAX = 100


def clamp_hv_air_volume(percentage: float) -> int:
    """Clamp a requested HV air-volume percentage into the device's valid band.

    Pure helper (no HA imports) so it is directly unit-testable. Returns an int
    in [HV_AIR_VOLUME_MIN, HV_AIR_VOLUME_MAX].
    """
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
    """
    return int(
        max(
            WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MIN,
            min(WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MAX, value),
        )
    )


def clamp_weather_impact_solar_radiation(value: float) -> float:
    """Clamp a requested solar-radiation weighting into the API's valid band.

    Pure helper (no HA imports) so it is directly unit-testable.
    """
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
# backwards-incompatible way; HA will discard the stale file automatically.
HEALTH_STORAGE_KEY = f"{DOMAIN}_health"
HEALTH_STORAGE_VERSION = 1
