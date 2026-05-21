"""Data coordinator for Hoval Connect.

Responsibilities
----------------
* Polls the Hoval Connect cloud API on a configurable interval.
* Fetches plant list, circuits, live values, time programs, events and weather
  in parallel using ``asyncio.gather`` with per-request and global timeouts.
* Converts raw API payloads to typed dataclasses consumed by platform entities.
* Serialises control commands (mode changes, overrides) through ``control_lock``
  and schedules a fast post-control refresh as a background task.
* Implements adaptive poll-interval back-off when the cloud is persistently
  unavailable, reducing noise in the HA log and server load.

Timeout hierarchy
-----------------
1. ``_CONNECT_TIMEOUT`` / ``_READ_TIMEOUT`` on every aiohttp call (in api.py).
2. ``_CIRCUIT_TIMEOUT`` caps the entire live-values + programs fetch for one
   circuit so a single stuck circuit cannot consume the coordinator's budget.
3. Global 90 s ``asyncio.timeout`` in ``_async_update_data`` as a hard backstop.
"""

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

# Per-circuit data-fetch cap.  With _MAX_RETRIES=2 and worst-case 28 s per
# attempt, a single endpoint could theoretically block for ~57 s.  35 s gives
# each circuit at least one full attempt plus breathing room while keeping it
# well within the 90 s global cap.
_CIRCUIT_TIMEOUT = 35  # seconds

# Adaptive back-off: how many consecutive failures before widening the interval.
# First two failures keep the normal cadence (may be transient blips); back-off
# starts on the third failure.
_BACKOFF_THRESHOLD = 2
_MAX_BACKOFF_SECONDS = 900  # 15 minutes

# v1 API returns different activeProgram values than v3.
# Normalise so entities always see v3 enum keys.
_V1_PROGRAM_MAP: dict[str, str] = {
    "tteControlled": "week1",
    "timePrograms": "week1",
    "nightReduction": "week1",
    "dayCooling": "week1",
}


def _resolve_active_program_value(
    programs: dict[str, Any],
    now: datetime,
    active_program: str | None = None,
) -> tuple[str | None, str | None, float | None]:
    """Resolve the currently active week, day-program name, and air volume.

    Uses *active_program* to select the correct week schedule.  Any value
    other than ``"week2"`` (constant, ecoMode, standby, None …) falls back to
    ``week1`` because those modes are not schedule-driven.

    Returns:
        (week_name, day_program_name, current_phase_value)
    """
    day_programs = programs.get("dayPrograms", {})
    day_configs = day_programs.get("dayConfigurations", [])
    if not day_configs:
        return None, None, None

    config_by_id: dict[int, dict] = {d["id"]: d for d in day_configs}

    week_key = "week2" if active_program == "week2" else "week1"
    week = programs.get(week_key) or programs.get("week1", {})
    week_name = week.get("name")
    day_program_ids = week.get("dayProgramIds", [])

    weekday = now.weekday()  # 0 = Monday
    if weekday >= len(day_program_ids):
        return week_name, None, None

    day_config = config_by_id.get(day_program_ids[weekday])
    if day_config is None:
        return week_name, None, None

    day_name = day_config.get("name")
    current_minutes = now.hour * 60 + now.minute
    for phase in day_config.get("phases", []):
        start = phase["start"]
        end = phase["end"]
        if start["hours"] * 60 + start["minutes"] <= current_minutes < end["hours"] * 60 + end["minutes"]:
            return week_name, day_name, phase.get("value")

    return week_name, day_name, None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

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
    target_value: float | None = None
    is_air_quality_guided: bool = False
    has_error: bool = False
    circuit_status: str | None = None
    live_values: dict[str, str] = field(default_factory=dict)
    active_week_name: str | None = None
    active_day_program_name: str | None = None
    program_air_volume: float | None = None
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    """Return True if the event is active and represents a fault."""
    return bool(
        event
        and event.is_active
        and event.event_type in ("blocking", "locking", "warning")
    )


DEFAULT_FAN_SPEED = 40


def resolve_fan_speed(circuit: HovalCircuitData | None) -> int:
    """Resolve the best fan speed for constant mode.

    Fallback chain: live airVolume → targetValue → program air volume → default.
    Always returns ≥ 1 (the API rejects 0).
    """
    if circuit is None:
        return DEFAULT_FAN_SPEED
    for val_raw in (
        circuit.live_values.get("airVolume"),
        circuit.target_value,
        circuit.program_air_volume,
    ):
        if val_raw is not None:
            speed = int(float(val_raw))
            if speed >= 1:
                return speed
    return DEFAULT_FAN_SPEED


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class HovalDataCoordinator(DataUpdateCoordinator[HovalData]):
    """Coordinator to fetch and cache data from the Hoval Connect API."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, api: HovalConnectApi) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self.control_lock = asyncio.Lock()
        self._mode_override: dict[str, str] = {}
        self._program_cache: dict[str, tuple[Any, float]] = {}
        self._program_cache_ttl = PROGRAM_CACHE_TTL.total_seconds()
        self._known_circuits: set[str] = set()
        # Background-task tracking — tasks are cancelled on config-entry unload
        # so they cannot call async_request_refresh() after teardown.
        self._pending_tasks: set[asyncio.Task] = set()
        # Adaptive back-off state
        self._consecutive_failures: int = 0
        self._base_update_interval: timedelta = DEFAULT_SCAN_INTERVAL

    # ------------------------------------------------------------------
    # Interval management
    # ------------------------------------------------------------------

    def set_base_update_interval(self, interval: timedelta) -> None:
        """Set the configured poll interval and reset any active back-off.

        Called from ``async_setup_entry`` and ``_async_options_updated`` so
        that a user-changed scan interval takes effect immediately and clears
        any back-off that was in progress.

        Also primes ``api.stats._poll_interval_seconds`` so the diagnostic
        sensor shows the correct value from the moment of setup rather than
        waiting for the first successful refresh to update it.
        """
        self._base_update_interval = interval
        self._consecutive_failures = 0
        self.update_interval = interval
        self.api.stats._poll_interval_seconds = int(interval.total_seconds())

    def _apply_backoff(self) -> None:
        """Widen the poll interval exponentially after repeated failures.

        Kicks in after ``_BACKOFF_THRESHOLD`` consecutive failures.  The
        interval doubles each time up to ``_MAX_BACKOFF_SECONDS``.
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
                "backing off poll interval to %ds",
                self._consecutive_failures,
                int(backed_off),
            )
            self.update_interval = new_interval

    # ------------------------------------------------------------------
    # Mode override helpers
    # ------------------------------------------------------------------

    def set_mode_override(self, circuit_path: str, mode: str) -> None:
        """Store an optimistic mode override for a circuit after a control action."""
        self._mode_override[circuit_path] = mode

    def get_mode_override(self, circuit_path: str) -> str | None:
        """Return the optimistic override for a circuit, or None."""
        return self._mode_override.get(circuit_path)

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def cancel_pending_tasks(self) -> None:
        """Cancel all in-flight background refresh tasks.

        Called by ``async_unload_entry`` to prevent post-control refresh tasks
        from calling ``async_request_refresh()`` on a torn-down coordinator.
        """
        for task in list(self._pending_tasks):
            task.cancel()
        self._pending_tasks.clear()

    # ------------------------------------------------------------------
    # Control + refresh
    # ------------------------------------------------------------------

    async def async_control_and_refresh(
        self,
        coro: Any,
        circuit_path: str,
        mode_override: str,
    ) -> None:
        """Execute a control command, set optimistic state, then refresh.

        The API call and override are serialised inside ``control_lock``.
        The post-control refresh runs as a fire-and-forget background task so
        the entity action method returns to HA immediately (responsive UI even
        when the cloud is slow).

        The 2-second pre-refresh pause lives *inside* the background task so
        the caller is not blocked.  The task handle is registered in
        ``_pending_tasks`` so it is cancelled cleanly on entry unload.
        """
        async with self.control_lock:
            await coro
            self.set_mode_override(circuit_path, mode_override)

        async def _do_refresh() -> None:
            # Give the API time to commit the change before reading back.
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

    # ------------------------------------------------------------------
    # Per-circuit data fetch
    # ------------------------------------------------------------------

    async def _fetch_circuit_data(
        self,
        plant_id: str,
        path: str,
        ctype: str,
        circuit: dict,
    ) -> HovalCircuitData:
        """Fetch live values and (cached) programs for one circuit.

        Wrapped in ``asyncio.timeout(_CIRCUIT_TIMEOUT)`` so a single stuck
        circuit cannot exhaust the coordinator's 90 s global budget.  On
        timeout the circuit is returned with empty ``live_values``; the entity
        continues to show its last known state until the next successful poll.
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

        cached_prog = self._program_cache.get(path)
        need_programs = (
            cached_prog is None
            or time.time() - cached_prog[1] > self._program_cache_ttl
        )

        try:
            async with asyncio.timeout(_CIRCUIT_TIMEOUT):
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
                "Circuit %s data fetch timed out after %ds; keeping previous state",
                path,
                _CIRCUIT_TIMEOUT,
            )
            return circuit_data

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
            w1 = prog_result.get("week1", {})
            w2 = prog_result.get("week2", {})
            if w1.get("name"):
                circuit_data.program_names["week1"] = w1["name"]
            if w2.get("name"):
                circuit_data.program_names["week2"] = w2["name"]
        else:
            _LOGGER.debug("Programs not available for %s: %s", path, prog_result)

        return circuit_data

    # ------------------------------------------------------------------
    # Main update entry points
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> HovalData:
        """Fetch data from the API with adaptive back-off on sustained failures.

        Success path: reset failure counter and restore the configured interval.
        Failure path: increment counter; widen interval after _BACKOFF_THRESHOLD.
        Auth errors: do not back off — HA's ConfigEntryAuthFailed stops retries.

        Also updates ``api.stats._poll_interval_seconds`` so the diagnostic
        poll-interval sensor always reflects the current setting.
        """
        try:
            async with asyncio.timeout(90):
                data = await self._fetch_all_data()

            if self._consecutive_failures > 0:
                _LOGGER.info(
                    "Hoval API recovered after %d consecutive failure(s); "
                    "restoring poll interval to %ds",
                    self._consecutive_failures,
                    int(self._base_update_interval.total_seconds()),
                )
            self._consecutive_failures = 0
            self.update_interval = self._base_update_interval
            # Keep the poll-interval stat current for the diagnostic sensor.
            self.api.stats._poll_interval_seconds = int(
                self.update_interval.total_seconds()
            )
            return data

        except TimeoutError as err:
            self._consecutive_failures += 1
            self._apply_backoff()
            raise UpdateFailed(
                "Hoval API refresh timed out after 90 s — cloud may be unresponsive."
            ) from err
        except HovalAuthError as err:
            # Auth failures are permanent; don't increment the back-off counter.
            self._consecutive_failures = 0
            self.update_interval = self._base_update_interval
            raise ConfigEntryAuthFailed("Authentication failed — check credentials") from err
        except HovalApiError as err:
            self._consecutive_failures += 1
            self._apply_backoff()
            _LOGGER.warning("Hoval API error during refresh: %s", err)
            raise UpdateFailed(f"Error fetching Hoval data: {err}") from err

    async def _fetch_all_data(self) -> HovalData:
        """Fetch all plants, circuits, events and weather from the API.

        Called inside the 90 s asyncio.timeout guard in ``_async_update_data``.
        ``_mode_override`` is cleared only after a *successful* fetch so that
        optimistic entity state survives transient failures.
        """
        data = HovalData()
        # Guard against None: the API can return an empty 204 body which
        # _request translates to None.  Iterating over None would raise
        # "NoneType is not iterable" and bring down the entire refresh.
        plants = await self.api.get_plants() or []

        for plant in plants:
            plant_id = plant.get("plantExternalId")
            if not plant_id:
                _LOGGER.debug("Skipping plant with missing plantExternalId")
                continue

            plant_data = HovalPlantData(
                plant_id=plant_id,
                name=plant.get("description", plant_id),
                is_online=plant.get("isOnline", True),
            )

            if not plant_data.is_online:
                self.api.invalidate_plant_token(plant_id)
                data.plants[plant_id] = plant_data
                continue

            try:
                # Guard against None for the same reason as get_plants() above.
                circuits_raw = await self.api.get_circuits(plant_id) or []
            except HovalApiError as err:
                _LOGGER.error(
                    "Circuits endpoint failed for plant %s: %s — entities will be "
                    "unavailable until the API recovers.",
                    plant_id, err,
                )
                raise

            _non_selectable_types = {CIRCUIT_TYPE_BL, CIRCUIT_TYPE_WW}
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

            # --- Fetch all circuits in parallel (each with its own timeout cap) ---
            circuit_results = await asyncio.gather(
                *[
                    self._fetch_circuit_data(plant_id, path, ctype, circ)
                    for path, ctype, circ in supported_circuits
                ],
                return_exceptions=True,
            )

            # --- Fetch plant-level data in parallel (separate gather for clarity) ---
            latest_event_result, events_result, weather_result = await asyncio.gather(
                self.api.get_latest_event(plant_id),
                self.api.get_events(plant_id),
                self.api.get_weather(plant_id),
                return_exceptions=True,
            )

            # Process circuits
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
                    "Latest event: type=%s active=%s",
                    plant_data.latest_event.event_type,
                    plant_data.latest_event.is_active,
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

            # Process weather
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

        # Dynamic entity discovery — fire signal on any newly seen circuit.
        # Firing on the *first* discovery is intentional: it ensures entities
        # added in _add_new() catch up even if the first poll returned after
        # platform setup ran against an empty circuits dict.
        current_circuits = {
            f"{pid}_{path}"
            for pid, plant in data.plants.items()
            for path in plant.circuits
        }
        if new_circuits := current_circuits - self._known_circuits:
            _LOGGER.info("New circuits discovered: %s", new_circuits)
            async_dispatcher_send(self.hass, SIGNAL_NEW_CIRCUITS)
        self._known_circuits = current_circuits

        # Clear overrides only after a successful fetch so optimistic state
        # survives transient failures.
        self._mode_override.clear()
        return data
