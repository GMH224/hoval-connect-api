"""Fan platform for Hoval Connect (HV ventilation speed control)."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .api import HovalApiError
from .const import (
    CIRCUIT_TYPE_HV,
    CONF_OVERRIDE_DURATION,
    CONF_TURN_ON_MODE,
    DEFAULT_OVERRIDE_DURATION,
    DEFAULT_TURN_ON_MODE,
    HV_AIR_VOLUME_MAX,
    HV_AIR_VOLUME_MIN,
    OPERATION_MODE_REGULAR,
    OPERATION_MODE_STANDBY,
    TURN_ON_RESUME,
    VALID_OVERRIDE_DURATIONS,
    VALID_TURN_ON_MODES,
    clamp_hv_air_volume,
)
from .coordinator import (
    SIGNAL_NEW_CIRCUITS,
    HovalCircuitData,
    HovalDataCoordinator,
    resolve_resume_program,
)

_LOGGER = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Hoval fan entities."""
    coordinator = entry.runtime_data.coordinator
    plant_devices = entry.runtime_data.plant_devices
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[HovalFan] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            plant_device_id = plant_devices.async_get_device_id(plant_id, plant_data)
            for path, circuit in plant_data.circuits.items():
                uid = f"{plant_id}_{path}_fan"
                if circuit.circuit_type != CIRCUIT_TYPE_HV or uid in known:
                    continue
                known.add(uid)
                entities.append(
                    HovalFan(coordinator, entry, plant_id, plant_device_id, path, circuit)
                )
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalFan(CoordinatorEntity[HovalDataCoordinator], FanEntity):
    """Hoval ventilation fan entity with percentage speed control."""

    _attr_has_entity_name = True
    _attr_translation_key = "ventilation"
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )
    _attr_speed_count = 100

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        entry: HovalConnectConfigEntry,
        plant_id: str,
        plant_device_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the fan entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_fan"
        self._attr_device_info = circuit_device_info(plant_id, plant_device_id, circuit_data)
        self._debounce_task: asyncio.Task | None = None
        # ICS-001: the task (if any) that has passed the debounce sleep and
        # committed to its API call — see _cancel_debounce().
        self._committed_task: asyncio.Task | None = None
        self._pending_percentage: int | None = None

    def _cancel_debounce(self) -> None:
        """Cancel any pending debounce task — unless it has already committed.

        ICS-001 (independent audit, 2026-09, v1.0.1 round). Cancelling a
        task that has already begun its HTTP request does NOT stop that
        request: it was handed to Home Assistant's executor thread pool
        (see api.py's requests-in-executor transport), and
        concurrent.futures cancellation only works before a worker picks
        the job up. What cancellation DOES do is unwind the coroutine,
        releasing the coordinator's control_lock — which lets the newer
        write acquire it, run, and finish while the older request is still
        in flight. The older value can then land LAST and win.

        Fix: cancellation stays effective during the debounce sleep, where
        it belongs and where nothing has been sent yet. Once a task has
        committed to the API call it is left alone to complete. The newer
        write then queues behind it on control_lock and lands after it, so
        ordering is preserved by the lock rather than by a cancellation
        that cannot deliver it.

        Cost: in that narrow window both writes are sent instead of one.
        An extra write with the correct final state is strictly better
        than one write with the wrong one.

        Tracked per-task (not a bare boolean) so that a task still sleeping
        remains cancellable even while a different, older task is mid-send.
        """
        task = self._debounce_task
        if task is not None and not task.done() and task is not self._committed_task:
            task.cancel()
        self._debounce_task = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending debounce task on removal."""
        self._cancel_debounce()
        await super().async_will_remove_from_hass()

    @property
    def _override_duration(self) -> str:
        """Get override duration enum from options (FOUR or MIDNIGHT).

        Independent audit finding (2026-09, fourth round, HVC-007): the
        raw persisted value is now validated against VALID_OVERRIDE_DURATIONS
        before use — an out-of-band value would otherwise reach the API
        unchanged, producing a confusing failure there instead of a
        graceful local fallback.
        """
        value = self._entry.options.get(CONF_OVERRIDE_DURATION, DEFAULT_OVERRIDE_DURATION)
        return value if value in VALID_OVERRIDE_DURATIONS else DEFAULT_OVERRIDE_DURATION

    @property
    def _turn_on_mode(self) -> str:
        """Get turn-on mode from options (resume, week1, week2).

        Independent audit finding (2026-09, fourth round, HVC-007): same
        validation rationale as _override_duration above.
        """
        value = self._entry.options.get(CONF_TURN_ON_MODE, DEFAULT_TURN_ON_MODE)
        return value if value in VALID_TURN_ON_MODES else DEFAULT_TURN_ON_MODE

    @property
    def _plant(self):
        """Get current plant data from coordinator."""
        return self.coordinator.data.plants.get(self._plant_id)

    @property
    def _circuit(self) -> HovalCircuitData | None:
        """Get current circuit data from coordinator."""
        plant = self._plant
        if plant is None:
            return None
        return plant.circuits.get(self._circuit_path)

    @property
    def available(self) -> bool:
        """Return if entity is available.

        ICS-CRIT-008 (audit v1.0.1): now also requires the plant itself to
        be online — see the identical fix/rationale in climate.py.
        """
        plant = self._plant
        return (
            super().available
            and plant is not None
            and plant.is_online
            and self._circuit is not None
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if fan is on (not in standby)."""
        circuit = self._circuit
        if circuit is None:
            return None
        override = self.coordinator.get_mode_override(self._plant_id, self._circuit_path)
        mode = override if override is not None else circuit.operation_mode
        # Independent audit finding (2026-09, HVC-006): operationMode is not
        # a required API field. Missing/None previously made
        # `mode != OPERATION_MODE_STANDBY` evaluate True, reporting the fan
        # as ON with no actual basis for it. Report unknown instead.
        if mode is None:
            return None
        return mode != OPERATION_MODE_STANDBY

    @property
    def percentage(self) -> int | None:
        """Return the current speed percentage (0-100).

        Independent audit finding (2026-09, HVC-003): checks
        `circuit.actual_value` (free, from the circuits-list response)
        before falling back to the now-permanently-empty `live_values`,
        then `target_value` as the last resort.
        """
        # Show pending value immediately for responsive UI
        if self._pending_percentage is not None:
            return self._pending_percentage
        circuit = self._circuit
        if circuit is None:
            return None
        val = circuit.actual_value
        if val is None:
            val = circuit.live_values.get("airVolume")
        if val is None:
            val = circuit.target_value
        if val is None:
            return None
        return max(0, min(100, int(float(val))))

    async def _send_percentage(self, percentage: int) -> None:
        """Actually send the percentage to the API (called after debounce).

        The requested percentage is clamped into the HV device's valid
        air-volume band [HV_AIR_VOLUME_MIN, HV_AIR_VOLUME_MAX] before being
        sent. HA already constrains percentages to 0–100, but values between 1
        and the device minimum would otherwise be forwarded verbatim and
        rejected (or undefined-behaved) by the cloud/firmware. 0 is handled as
        turn-off earlier in async_set_percentage and never reaches here.
        """
        # ICS-MED-001 (audit v1.0.1): only clear the pending-value display if
        # it still matches what THIS call is about to send. An OLDER
        # in-flight write reaching this point after a NEWER
        # async_set_percentage() call has already stored a different
        # pending value (and restarted the debounce timer for it) must not
        # clobber that newer value — doing so briefly reverted the slider
        # to the coordinator's stale state until the newer write's own
        # debounce eventually fired.
        if self._pending_percentage == percentage:
            self._pending_percentage = None
        try:
            clamped = clamp_hv_air_volume(percentage)
        except ValueError as err:
            # Independent audit finding (2026-09, HVC-ICS-004): clamp_hv_air_volume
            # now rejects non-finite input instead of silently returning a
            # boundary value. Converted to HomeAssistantError here (this method
            # runs inside a fire-and-forget background task — see
            # _debounced_set's docstring — so an uncaught ValueError would
            # otherwise be invisible to the user).
            raise HomeAssistantError(f"Invalid fan speed: {err}") from err
        if clamped != percentage:
            _LOGGER.debug(
                "Clamped requested air volume %d%% to device band %d-%d%% → %d%%",
                percentage,
                HV_AIR_VOLUME_MIN,
                HV_AIR_VOLUME_MAX,
                clamped,
            )
        try:
            await self.coordinator.async_control_and_refresh(
                lambda: self.coordinator.api.set_temporary_change(
                    self._plant_id,
                    self._circuit_path,
                    value=clamped,
                    duration=self._override_duration,
                ),
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set fan speed: {err}") from err

    async def _debounced_set(self, percentage: int) -> None:
        """Wait for debounce period, then send the latest percentage.

        Runs as a fire-and-forget task, so an exception raised here would only
        reach the event loop's unhandled-task logger — invisible to the user
        (audit finding F5, v0.21.1). A failed actuation on a control path must
        be observable: log it at WARNING and rewrite entity state so the slider
        visibly reverts instead of silently showing a value the device never
        accepted.
        """
        await asyncio.sleep(DEBOUNCE_SECONDS)
        _LOGGER.debug("Debounce complete, sending %d%%", percentage)
        # ICS-001: past this point the write is committed — cancelling it
        # could no longer stop the HTTP request, only corrupt the ordering.
        # See _cancel_debounce() for the full reasoning.
        self._committed_task = asyncio.current_task()
        try:
            await self._send_percentage(percentage)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Setting fan speed to %d%% failed for circuit %s: %s — "
                "the slider will revert to the device's actual value",
                percentage,
                self._circuit_path,
                err,
            )
            self.async_write_ha_state()
        finally:
            if self._committed_task is asyncio.current_task():
                self._committed_task = None

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed percentage of the fan (debounced)."""
        _LOGGER.debug("async_set_percentage called: %d%%", percentage)
        # ICS-MED-002 (audit v1.0.1): validated before being written to the
        # pending display value / pushed to the UI via
        # async_write_ha_state(). HA's fan service schema already
        # constrains percentage to 0-100 under normal use, but this method
        # must not trust that unconditionally — a direct entity-service
        # call (or a future HA change) bypassing that schema must fail
        # loudly here rather than display/forward a bogus value.
        if (
            not isinstance(percentage, int)
            or isinstance(percentage, bool)
            or not (0 <= percentage <= 100)
        ):
            raise HomeAssistantError(
                f"Invalid fan percentage: {percentage!r}; expected an integer 0-100"
            )
        if percentage == 0:
            self._cancel_debounce()
            self._pending_percentage = None
            await self.async_turn_off()
            return
        # Store pending value and update UI immediately
        self._pending_percentage = percentage
        self.async_write_ha_state()
        # Cancel previous debounce timer
        self._cancel_debounce()
        # Start new debounce timer
        # Independent audit finding (2026-09, fourth round, HVC-003):
        # routed through the coordinator's shared task tracking instead of
        # a bare hass.async_create_task() call, so async_shutdown() (called
        # before the API session closes on unload/reload) cancels AND
        # awaits this too — not just the coordinator's own post-write
        # refresh tasks. Without this, a debounce task still asleep at
        # reload time could wake up and try to use an already-closed
        # requests.Session, since this entity's own
        # async_will_remove_from_hass() (which also cancels it) only runs
        # during platform unload, which happens after the session closes.
        self._debounce_task = self.coordinator.create_tracked_task(self._debounced_set(percentage))

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs,
    ) -> None:
        """Turn on the fan.

        Independent audit finding (2026-09, HVC-ICS-002): cancels any
        pending debounced percentage write first. Without this, a stale
        queued speed write (from a slider drag shortly before this call)
        could fire ~1.5s later and override the state this call just set.
        """
        self._cancel_debounce()
        self._pending_percentage = None
        if percentage is not None:
            await self.async_set_percentage(percentage)
            return
        mode = self._turn_on_mode

        # ICS-HIGH-018 (audit v1.0.1): resolve_resume_program()'s fresh read
        # (for TURN_ON_RESUME) now happens inside this factory, called only
        # once async_control_and_refresh holds this circuit's lock — see
        # the identical fix/rationale in climate.py's AUTO branch.
        async def _turn_on_coro():
            if mode == TURN_ON_RESUME:
                # Independent audit finding (2026-09, HVC-ICS-008 + "more"
                # report finding #8): preserve week2 if that's the circuit's
                # actual, freshly-confirmed active program, instead of always
                # forcing week1 or trusting a possibly-stale cached snapshot —
                # see resolve_resume_program().
                resume_program = await resolve_resume_program(
                    self.coordinator.api, self._plant_id, self._circuit_path, self._circuit
                )
                return await self.coordinator.api.reset_circuit(
                    self._plant_id,
                    self._circuit_path,
                    program=resume_program,
                )
            return await self.coordinator.api.set_program(
                self._plant_id,
                self._circuit_path,
                mode,
            )

        try:
            await self.coordinator.async_control_and_refresh(
                _turn_on_coro,
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to turn on fan: {err}") from err

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off the fan (standby mode).

        Independent audit finding (2026-09, HVC-ICS-002): cancels any
        pending debounced percentage write first — without this, a queued
        speed write from just before this call could fire ~1.5s later and
        turn the fan back on immediately after this explicit turn-off.
        """
        self._cancel_debounce()
        self._pending_percentage = None
        try:
            await self.coordinator.async_control_and_refresh(
                lambda: self.coordinator.api.set_circuit_mode(
                    self._plant_id,
                    self._circuit_path,
                    OPERATION_MODE_STANDBY,
                ),
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_STANDBY,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to turn off fan: {err}") from err
