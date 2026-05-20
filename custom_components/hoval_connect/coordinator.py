"""Data coordinator for Hoval Connect."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import HovalApiError, HovalAuthError, HovalConnectApi
from .const import (
    CIRCUIT_TYPE_BL,
    CIRCUIT_TYPE_WW,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PROGRAM_CACHE_TTL,
    SUPPORTED_CIRCUIT_TYPES,
)

SIGNAL_NEW_CIRCUITS = f"{DOMAIN}_new_circuits"

_LOGGER = logging.getLogger(__name__)

# Per-circuit data fetch hard cap.  Each _fetch_circuit_data call runs two API
# requests in parallel (live values + programs).  Worst-case for a single
# request is (_CONNECT_TIMEOUT + _READ_TIMEOUT) × _MAX_RETRIES ≈ 57 s, but
# in practice both succeed in <5 s.  35 s is generous for one full attempt with
# some recovery room, and keeps the circuit well inside the 90 s global cap so
# a single stuck circuit cannot consume the entire coordinator budget.
_CIRCUIT_TIMEOUT = 35  # seconds

# Adaptive backoff: how many consecutive failures before we start backing off
# the poll interval.  Value of 2 means first two failures keep normal cadence
# (they may be transient blips), backoff starts on the third.
_BACKOFF_THRESHOLD = 2
# Maximum backed-off interval regardless of failure count (15 minutes).
_MAX_BACKOFF_SECONDS = 900


# v1 API returns different activeProgram values than v3.
# Normalize so entities always see v3 enum keys.
_V1_PROGRAM_MAP: dict[str, str] = {
    "tteControlled": "week1",  # time program active (v1 doesn't say which week)
    "timePrograms": "week1",
    "nightReduction": "week1",
    "dayCooling": "week1",
}


def _resolve_active_program_value(
    programs: dict[str, Any], now: datetime, active_program: str | None = None
) -> tuple[str | None, str | None, float | None]:
    """Resolve the currently active week, day program name, and air volume.

    Uses active_program to select the correct week schedule (week1 or week2).
    Falls back to week1 for programs that are not schedule-driven (constant,
    ecoMode, standby, etc.).

    Returns (week_name, day_program_name, current_phase_value).
    """
    day_programs = programs.get("dayPrograms", {})
    day_configs = day_programs.get("dayConfigurations", [])
    if not day_configs:
        return None, None, None

    # Build lookup: id -> day config
    config_by_id: dict[int, dict] = {d["id"]: d for d in day_configs}

    # Select the week schedule that matches the active program.  Any value
    # other than "week2" (constant, ecoMode, standby, None, …) falls back to
    # week1 because those modes do not correspond to a named schedule.
    week_key = "week2" if active_program == "week2" else "week1"
    week = programs.get(week_key) or programs.get("week1", {})
    week_name = week.get("name")
    day_program_ids = week.get("dayProgramIds", [])

    # weekday: 0=Monday in Python, dayProgramIds[0]=Monday in Hoval
    weekday = now.weekday()
    if weekday >= len(day_program_ids):
        return week_name, None, None

    day_prog_id = day_program_ids[weekday]
    day_config = config_by_id.get(day_prog_id)
    if day_config is None:
        return week_name, None, None

    day_name = day_config.get("name")

    # Find active phase based on current time
    current_minutes = now.hour * 60 + now.minute
    for phase in day_config.get("phases", []):
        start = phase["start"]
        end = phase["end"]
        start_min = start["hours"] * 60 + start["minutes"]
        end_min = end["hours"] * 60 + end["minutes"]
        if start_min <= current_minutes < end_min:
            return week_name, day_name, phase.get("value")

    return week_name, day_name, None


@dataclass
class HovalEventData:
    """Parsed data for a plant event."""

    event_type: str | None = None
    description: str | None = None
    time_occurred: str | None = None
    time_resolved: str | None = None
    source_path: str | None = None
    code: int | None = None

    @property
    def is_active(self) -> bool:
        """Event is active when it has not been resolved."""
        return self.time_resolved is None


@dataclass
class HovalCircuitData:
    """Parsed data for a single circuit."""

    circuit_type: str
    path: str
    name: str
    operation_mode: str | None = None
    active_program: str | None = None
    # HV: air-volume percentage; HK: target temperature in °C. Coming from the
    # circuit list endpoint's `targetValue` (renamed from v1 `targetAirVolume`).
    target_value: float | None = None
    is_air_quality_guided: bool = False
    has_error: bool = False
    circuit_status: str | None = None
    live_values: dict[str, str] = field(default_factory=dict)
    active_week_name: str | None = None
    active_day_program_name: str | None = None
    program_air_volume: float | None = None
    # User-defined program names: API key → display name (e.g. "week1" → "Normal")
    program_names: dict[str, str] = field(default_factory=dict)


@dataclass
class HovalWeatherData:
    """Parsed weather forecast data for a plant."""

    weather_type: str | None = None
    outside_temperature: float | None = None
    outside_temperature_min: float | None = None


@dataclass
class HovalPlantData:
    """Parsed data for a single plant."""

    plant_id: str
    name: str
    is_online: bool = True
    has_error: bool = False
    circuits: dict[str, HovalCircuitData] = field(default_factory=dict)
    latest_event: HovalEventData | None = None
    events: list[HovalEventData] = field(default_factory=list)
    weather: HovalWeatherData | None = None


@dataclass
class HovalData:
    """Top-level data returned by the coordinator."""

    plants: dict[str, HovalPlantData] = field(default_factory=dict)


def _parse_event(raw: dict) -> HovalEventData:
    """Parse a PlantEventDTO dict into HovalEventData."""
    return HovalEventData(
        event_type=raw.get("eventType"),
        description=raw.get("description"),
        time_occurred=raw.get("timeOccurred"),
        time_resolved=raw.get("timeResolved"),
        source_path=raw.get("sourcePath"),
        code=raw.get("code"),
    )


def _is_problem_event(event: HovalEventData | None) -> bool:
    """Return True if event is active and represents a fault (blocking/locking/warning)."""
    return bool(
        event
        and event.is_active
        and event.event_type
        in (
            "blocking",
            "locking",
            "warning",
        )
    )


DEFAULT_FAN_SPEED = 40


def resolve_fan_speed(circuit: HovalCircuitData | None) -> int:
    """Resolve the best fan speed value for constant mode.

    Fallback chain: live airVolume → targetValue → program air volume → default.
    Always returns at least 1 (API rejects value=0).
    """
    if circuit is None:
        return DEFAULT_FAN_SPEED
    # Try live sensor value first
    val = circuit.live_values.get("airVolume")
    if val is not None:
        speed = int(float(val))
        if speed >= 1:
            return speed
    # Try target from circuit config
    if circuit.target_value is not None:
        speed = int(circuit.target_value)
        if speed >= 1:
            return speed
    # Try the currently active time program phase value
    if circuit.program_air_volume is not None:
        speed = int(circuit.program_air_volume)
        if speed >= 1:
            return speed
    return DEFAULT_FAN_SPEED


class HovalDataCoordinator(DataUpdateCoordinator[HovalData]):
    """Coordinator to fetch data from Hoval Connect API."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: HovalConnectApi,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self.control_lock = asyncio.Lock()
        # Optimistic mode override per circuit (set by control actions,
        # cleared on next poll). Key: circuit_path, value: operation mode string.
        self._mode_override: dict[str, str] = {}
        # Program cache: key=circuit_path, value=(programs_data, timestamp)
        self._program_cache: dict[str, tuple[Any, float]] = {}
        self._program_cache_ttl = PROGRAM_CACHE_TTL.total_seconds()
        # Track known circuits for dynamic entity discovery
        self._known_circuits: set[str] = set()
        # Track background refresh tasks so they can be cancelled on entry unload
        self._pending_tasks: set[asyncio.Task] = set()
        # Adaptive backoff: track consecutive failures to widen the poll interval
        # during sustained outages, and restore it when the API recovers.
        self._consecutive_failures: int = 0
        self._base_update_interval: timedelta = DEFAULT_SCAN_INTERVAL

    def set_base_update_interval(self, interval: timedelta) -> None:
        """Set the configured poll interval and reset any active backoff.

        Called from async_setup_entry and _async_options_updated so that a
        user-changed scan interval takes effect immediately and clears any
        backoff that was in progress.
        """
        self._base_update_interval = interval
        self._consecutive_failures = 0
        self.update_interval = interval

    def set_mode_override(self, circuit_path: str, mode: str) -> None:
        """Set optimistic mode override after a control action."""
        self._mode_override[circuit_path] = mode

    def get_mode_override(self, circuit_path: str) -> str | None:
        """Get the optimistic mode override for a circuit."""
        return self._mode_override.get(circuit_path)

    def cancel_pending_tasks(self) -> None:
        """Cancel all pending background refresh tasks.

        Called by async_unload_entry so that in-flight post-control refreshes
        do not call async_request_refresh() on a torn-down coordinator after
        the config entry has been unloaded.
        """
        for task in list(self._pending_tasks):
            task.cancel()
        self._pending_tasks.clear()

    def _apply_backoff(self) -> None:
        """Widen the poll interval exponentially after repeated failures.

        Kicks in after _BACKOFF_THRESHOLD consecutive failures, doubling the
        interval each time up to _MAX_BACKOFF_SECONDS.  Logged at INFO so the
        user can see in the HA log that the integration is deliberately slowing
        down rather than hammering a struggling server.
        """
        if self._consecutive_failures <= _BACKOFF_THRESHOLD:
            return
        base_s = self._base_update_interval.total_seconds()
        backed_off = min(
            base_s * (2 ** (self._consecutive_failures - _BACKOFF_THRESHOLD)),
            _MAX_BACKOFF_SECONDS,
        )
        new_interval = timedelta(seconds=backed_off)
        if new_interval != self.update_interval:
            _LOGGER.info(
                "Hoval API unavailable (%d consecutive failures); "
                "backing off poll interval to %ds (base: %ds, max: %ds)",
                self._consecutive_failures,
                int(backed_off),
                int(base_s),
                _MAX_BACKOFF_SECONDS,
            )
            self.update_interval = new_interval

    async def async_control_and_refresh(
        self,
        coro: Any,
        circuit_path: str,
        mode_override: str,
    ) -> None:
        """Execute a control command with lock, optimistic state, and refresh.

        The API call and optimistic override are serialised inside control_lock
        so concurrent control actions don't race each other.

        The coordinator refresh is scheduled as a fire-and-forget background
        task that returns to the caller immediately, keeping the UI responsive
        even when the Hoval cloud is slow.

        The 2-second pre-refresh pause (giving the API time to commit the
        change before we read back) is inside the background task so the
        caller is not blocked.

        The task handle is stored in _pending_tasks so it can be cancelled
        cleanly if the config entry is unloaded before the refresh completes.
        """
        async with self.control_lock:
            await coro
            self.set_mode_override(circuit_path, mode_override)

        # Schedule refresh as background task — do not await it here.
        # This keeps the caller (entity action method) fast and prevents the
        # lock from being starved during a slow/timeout refresh.
        async def _do_refresh() -> None:
            # Brief pause inside the task so the API has time to commit the
            # change before we read back.  Kept here (not in the caller) so
            # the entity action method returns to HA immediately.
            await asyncio.sleep(2)
            try:
                await self.async_request_refresh()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Post-control refresh failed for %s; coordinator will retry on next poll",
                    circuit_path,
                )

        task = self.hass.async_create_task(_do_refresh())
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _fetch_circuit_data(
        self,
        plant_id: str,
        path: str,
        ctype: str,
        circuit: dict,
    ) -> HovalCircuitData:
        """Fetch live values and programs for a single circuit.

        Live values are always fetched. Programs are fetched only when the cache
        has expired (PROGRAM_CACHE_TTL, default 5 min), reducing API load.

        Both fetches run in parallel via asyncio.gather with return_exceptions=True
        so a failure on one does not block the other.

        The entire fetch is wrapped in asyncio.timeout(_CIRCUIT_TIMEOUT) so that
        a single stuck circuit cannot hold up the coordinator's global 90 s cap.
        On timeout the circuit is returned with empty live_values; entities show
        their previous state until the next successful poll.
        """
        raw_program = circuit.get("activeProgram")
        air_quality = circuit.get("airQuality") or {}
        circuit_data = HovalCircuitData(
            circuit_type=ctype,
            path=path,
            name=circuit.get("name") or ctype,
            operation_mode=circuit.get("operationMode"),
            active_program=_V1_PROGRAM_MAP.get(raw_program, raw_program),
            target_value=circuit.get("targetValue"),
            is_air_quality_guided=bool(air_quality.get("isAirQualityGuided")),
            has_error=circuit.get("hasError", False),
            circuit_status=circuit.get("circuitStatus"),
        )

        # Check program cache
        cached_prog = self._program_cache.get(path)
        need_programs = (
            cached_prog is None
            or time.time() - cached_prog[1] > self._program_cache_ttl
        )

        try:
            async with asyncio.timeout(_CIRCUIT_TIMEOUT):
                # Fetch live values (always) + programs (only if cache expired) in parallel
                if need_programs:
                    results = await asyncio.gather(
                        self.api.get_live_values(plant_id, path, ctype),
                        self.api.get_programs(plant_id, path),
                        return_exceptions=True,
                    )
                    live_result: Any = results[0]
                    prog_result: Any = results[1]
                else:
                    results = await asyncio.gather(
                        self.api.get_live_values(plant_id, path, ctype),
                        return_exceptions=True,
                    )
                    live_result = results[0]
                    prog_result = cached_prog[0]
        except TimeoutError:
            _LOGGER.warning(
                "Circuit %s data fetch timed out after %ds; "
                "entities will show previous state until next poll",
                path,
                _CIRCUIT_TIMEOUT,
            )
            return circuit_data  # empty live_values — entity stays at last state

        if not isinstance(live_result, BaseException):
            circuit_data.live_values = {v["key"]: v["value"] for v in live_result}
            _LOGGER.debug("Circuit %s live_values: %s", path, circuit_data.live_values)
        else:
            _LOGGER.debug("Live values not available for %s: %s", path, live_result)

        if not isinstance(prog_result, BaseException):
            if need_programs:
                self._program_cache[path] = (prog_result, time.time())
            now = dt_util.now()
            week_name, day_name, phase_value = _resolve_active_program_value(
                prog_result, now, circuit_data.active_program
            )
            circuit_data.active_week_name = week_name
            circuit_data.active_day_program_name = day_name
            circuit_data.program_air_volume = phase_value
            # Extract user-defined program names
            w1 = prog_result.get("week1", {})
            w2 = prog_result.get("week2", {})
            if w1.get("name"):
                circuit_data.program_names["week1"] = w1["name"]
            if w2.get("name"):
                circuit_data.program_names["week2"] = w2["name"]
        else:
            _LOGGER.debug("Programs not available for %s: %s", path, prog_result)

        return circuit_data

    async def _async_update_data(self) -> HovalData:
        """Fetch data from the API with adaptive backoff on sustained failures.

        On success: reset failure counter and restore the configured poll interval.
        On TimeoutError / HovalApiError: increment counter and apply exponential
          backoff after _BACKOFF_THRESHOLD consecutive failures (up to 15 min).
        On HovalAuthError: does not back off — auth failures require user action
          and HA's ConfigEntryAuthFailed machinery handles the notification.

        The 90 s asyncio.timeout is a hard cap that prevents the coordinator from
        blocking HA's event loop indefinitely when the cloud is unresponsive.
        """
        try:
            async with asyncio.timeout(90):
                data = await self._fetch_all_data()

            # Success path: reset backoff and restore normal poll cadence
            if self._consecutive_failures > 0:
                _LOGGER.info(
                    "Hoval API recovered after %d consecutive failure(s); "
                    "restoring poll interval to %ds",
                    self._consecutive_failures,
                    int(self._base_update_interval.total_seconds()),
                )
            self._consecutive_failures = 0
            self.update_interval = self._base_update_interval
            return data

        except TimeoutError as err:
            self._consecutive_failures += 1
            self._apply_backoff()
            raise UpdateFailed(
                "Hoval API refresh timed out after 90 s — cloud may be unresponsive. "
                "HA will retry automatically."
            ) from err
        except HovalAuthError as err:
            # Auth errors are not transient — do not increment failure counter
            # or back off, as ConfigEntryAuthFailed stops retries until the user
            # re-enters credentials anyway.
            self._consecutive_failures = 0
            self.update_interval = self._base_update_interval
            raise ConfigEntryAuthFailed("Authentication failed — check credentials") from err
        except HovalApiError as err:
            self._consecutive_failures += 1
            self._apply_backoff()
            _LOGGER.warning("Hoval API error during refresh: %s", err)
            raise UpdateFailed(f"Error fetching Hoval data: {err}") from err

    async def _fetch_all_data(self) -> HovalData:
        """Inner fetch — called inside the asyncio.timeout guard.

        HovalAuthError and HovalApiError propagate up to _async_update_data
        which converts them to ConfigEntryAuthFailed / UpdateFailed respectively.

        _mode_override is cleared only at the END of a successful fetch so
        that optimistic entity state survives failed/timed-out refreshes.
        """
        data = HovalData()

        plants = await self.api.get_plants()

        for plant in plants:
            plant_id = plant.get("plantExternalId")
            if not plant_id:
                _LOGGER.debug("Skipping plant with missing plantExternalId")
                continue

            plant_name = plant.get("description", plant_id)

            plant_data = HovalPlantData(
                plant_id=plant_id,
                name=plant_name,
                is_online=plant.get("isOnline", True),
            )

            # Skip all API calls when plant is offline
            if not plant_data.is_online:
                # Invalidate cached PAT so we get a fresh token when back
                self.api.invalidate_plant_token(plant_id)
                data.plants[plant_id] = plant_data
                continue

            # Fetch circuits. A persistent failure here is the most common
            # symptom of an upstream API change (the v1 endpoint removal in
            # April 2026 was masked for days because we used to swallow this
            # error). Log loudly and let DataUpdateCoordinator surface the
            # failure to the user as `unavailable` entities.
            try:
                circuits_raw = await self.api.get_circuits(plant_id)
            except HovalApiError as err:
                _LOGGER.error(
                    "Circuits endpoint failed for plant %s: %s — entities will go "
                    "unavailable until the cloud API recovers or the integration is "
                    "updated.",
                    plant_id,
                    err,
                )
                raise

            # BL/WW circuits have selectable=False but still provide live values
            _non_selectable_types = {CIRCUIT_TYPE_BL, CIRCUIT_TYPE_WW}

            _LOGGER.debug(
                "Fetched %d circuits (%d supported)",
                len(circuits_raw),
                sum(
                    1
                    for c in circuits_raw
                    if c.get("type") in SUPPORTED_CIRCUIT_TYPES
                    and (c.get("selectable") or c.get("type") in _non_selectable_types)
                ),
            )

            # Build list of supported circuits
            supported_circuits: list[tuple[str, str, dict]] = []
            for circuit in circuits_raw:
                ctype = circuit.get("type", "")
                if ctype not in SUPPORTED_CIRCUIT_TYPES:
                    continue
                if not circuit.get("selectable", False) and ctype not in _non_selectable_types:
                    continue
                path = circuit["path"]
                _LOGGER.debug(
                    "Circuit %s raw: %s",
                    path,
                    {k: v for k, v in circuit.items() if k != "name"},
                )
                supported_circuits.append((path, ctype, circuit))

            # Fetch all circuits in parallel. Each circuit has its own
            # _CIRCUIT_TIMEOUT cap so a single slow/stuck circuit cannot
            # exhaust the coordinator's 90 s global budget.
            # return_exceptions=True means one failed circuit does not
            # block the others.
            circuit_results = await asyncio.gather(
                *[
                    self._fetch_circuit_data(plant_id, path, ctype, circ)
                    for path, ctype, circ in supported_circuits
                ],
                return_exceptions=True,
            )

            # Fetch plant-level data (events + weather) in parallel.
            # Kept separate from circuit fetches for clarity; these calls are
            # fast and do not benefit meaningfully from being fused with the
            # circuit gather above.
            latest_event_result, events_result, weather_result = await asyncio.gather(
                self.api.get_latest_event(plant_id),
                self.api.get_events(plant_id),
                self.api.get_weather(plant_id),
                return_exceptions=True,
            )

            # Process circuit results
            for result in circuit_results:
                if isinstance(result, BaseException):
                    _LOGGER.debug("Circuit fetch failed: %s", result)
                    continue
                if result.has_error:
                    plant_data.has_error = True
                plant_data.circuits[result.path] = result

            # Process latest event
            if not isinstance(latest_event_result, BaseException) and latest_event_result:
                plant_data.latest_event = _parse_event(latest_event_result)
                if _is_problem_event(plant_data.latest_event):
                    plant_data.has_error = True
                _LOGGER.debug(
                    "Latest event: type=%s active=%s desc=%s",
                    plant_data.latest_event.event_type,
                    plant_data.latest_event.is_active,
                    plant_data.latest_event.description,
                )
            elif isinstance(latest_event_result, BaseException):
                _LOGGER.debug("Events endpoint not available for %s", plant_id)

            # Process events list
            if not isinstance(events_result, BaseException) and events_result:
                for ev in events_result[:10]:
                    plant_data.events.append(_parse_event(ev))
                for ev in plant_data.events:
                    if _is_problem_event(ev):
                        plant_data.has_error = True
                        break
            elif isinstance(events_result, BaseException):
                _LOGGER.debug("Events list not available for %s", plant_id)

            # Process weather forecast
            if not isinstance(weather_result, BaseException) and weather_result:
                if isinstance(weather_result, list) and weather_result:
                    w = weather_result[0]
                    plant_data.weather = HovalWeatherData(
                        weather_type=w.get("weatherType"),
                        outside_temperature=w.get("outsideTemperature"),
                        outside_temperature_min=w.get("outsideTemperatureMin"),
                    )
            elif isinstance(weather_result, BaseException):
                _LOGGER.debug("Weather not available for %s", plant_id)

            data.plants[plant_id] = plant_data

        # Detect new circuits for dynamic entity discovery.
        # Fire on any newly seen circuit, including the first one. Skipping the
        # initial set (when `_known_circuits` was still empty) used to leave
        # circuits stranded if the very first refresh came back without them
        # — async_setup_entry's _add_new() ran against an empty circuits dict
        # and the dispatcher then suppressed the catch-up signal. Each platform
        # already deduplicates via its `known` set, so firing on the first
        # discovery is a no-op when entities are already present.
        current_circuits = {
            f"{pid}_{path}" for pid, plant in data.plants.items() for path in plant.circuits
        }
        new_circuits = current_circuits - self._known_circuits
        if new_circuits:
            _LOGGER.info("New circuits discovered: %s", new_circuits)
            async_dispatcher_send(self.hass, SIGNAL_NEW_CIRCUITS)
        self._known_circuits = current_circuits

        # Clear optimistic overrides only after a SUCCESSFUL fetch so that if
        # the refresh fails (API timeout, transient error), entities continue to
        # show their optimistic state rather than snapping back to stale data
        # mid-cycle.  Fresh coordinator data takes over on the next good refresh.
        self._mode_override.clear()

        return data
