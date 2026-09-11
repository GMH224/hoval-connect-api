"""Climate platform for Hoval Connect (HK heating circuits)."""

from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .api import HovalApiError
from .const import (
    CIRCUIT_TYPE_HK,
    CONF_OVERRIDE_DURATION,
    DEFAULT_OVERRIDE_DURATION,
    OPERATION_MODE_REGULAR,
    OPERATION_MODE_STANDBY,
    VALID_OVERRIDE_DURATIONS,
    clamp_temperature,
)
from .coordinator import (
    SIGNAL_NEW_CIRCUITS,
    HovalCircuitData,
    HovalDataCoordinator,
    resolve_resume_program,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Hoval climate entities for heating circuits."""
    coordinator = entry.runtime_data.coordinator
    plant_devices = entry.runtime_data.plant_devices
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[HovalClimate] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            plant_device_id = plant_devices.async_get_device_id(plant_id, plant_data)
            for path, circuit in plant_data.circuits.items():
                uid = f"{plant_id}_{path}_climate"
                if circuit.circuit_type != CIRCUIT_TYPE_HK or uid in known:
                    continue
                known.add(uid)
                entities.append(
                    HovalClimate(coordinator, entry, plant_id, plant_device_id, path, circuit)
                )
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalClimate(CoordinatorEntity[HovalDataCoordinator], ClimateEntity):
    """Hoval heating circuit climate entity."""

    _attr_has_entity_name = True
    _attr_translation_key = "heating"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 5.0
    _attr_max_temp = 30.0
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        entry: HovalConnectConfigEntry,
        plant_id: str,
        plant_device_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_climate"
        self._attr_device_info = circuit_device_info(plant_id, plant_device_id, circuit_data)

    @property
    def _circuit(self) -> HovalCircuitData | None:
        """Get current circuit data from coordinator."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.circuits.get(self._circuit_path)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._circuit is not None

    @property
    def current_temperature(self) -> float | None:
        """Return the current room temperature.

        Independent audit finding (2026-09, HVC-003): `circuit.actual_value`
        (from the circuits-list response, already fetched — no extra call)
        is checked first. `live_values` is permanently empty since v1.0.0
        (see HovalCircuitData's field comment) but the loop is left in place
        as a harmless no-op fallback rather than touched for this release.
        """
        circuit = self._circuit
        if circuit is None:
            return None
        if circuit.actual_value is not None:
            return circuit.actual_value
        for key in ("roomTempActual", "actualTemperature", "roomTemperature"):
            val = circuit.live_values.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the target room temperature.

        Independent audit finding (2026-09, HVC-003): `circuit.target_value`
        (from the circuits-list response, already fetched for the write
        path anyway) is checked first, instead of only ever reading the
        now-permanently-empty `live_values`.
        """
        circuit = self._circuit
        if circuit is None:
            return None
        if circuit.target_value is not None:
            return circuit.target_value
        for key in ("roomTempTarget", "targetTemperature"):
            val = circuit.live_values.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the current HVAC mode.

        HEAT (independent audit finding, 2026-09, fourth round, HVC-004)
        specifically means "not on a week/eco schedule" — constant,
        manual, externalConstant, or anything else not in the AUTO set
        below. This is intentionally the mirror image of what selecting
        HEAT now does (async_set_hvac_mode activates "constant"
        specifically): a circuit already on "constant" reports back as
        HEAT, and selecting HEAT puts it on "constant", so read and write
        agree on what HEAT actually means instead of write silently
        landing in whatever AUTO would have produced.
        """
        circuit = self._circuit
        if circuit is None:
            return None
        override = self.coordinator.get_mode_override(self._plant_id, self._circuit_path)
        mode = override if override is not None else circuit.operation_mode
        # Independent audit finding (2026-09, HVC-006): operationMode is not
        # a required API field. Missing/None used to fall through silently
        # to HEAT below, reporting an active state with no actual basis for
        # it. Report unknown instead — ClimateEntity's hvac_mode explicitly
        # allows None.
        if mode is None:
            return None
        if mode == OPERATION_MODE_STANDBY:
            return HVACMode.OFF
        # If a time program is active, show as AUTO
        prog = circuit.active_program
        if prog in ("week1", "week2", "ecoMode"):
            return HVACMode.AUTO
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action.

        The 'status' key in live values reflects the circuit's operating state
        (e.g. 'heating', 'off').  'circuitStatus' from the circuit-list
        endpoint is a fallback for when live values are unavailable.
        """
        circuit = self._circuit
        if circuit is None:
            return None
        override = self.coordinator.get_mode_override(self._plant_id, self._circuit_path)
        mode = override if override is not None else circuit.operation_mode
        # See the identical guard in hvac_mode above (HVC-006).
        if mode is None:
            return None
        if mode == OPERATION_MODE_STANDBY:
            return HVACAction.OFF
        # Prefer the live-values 'status' key; fall back to circuit-list field.
        status = (circuit.live_values.get("status") or circuit.circuit_status or "").upper()
        if status == "HEATING":
            return HVACAction.HEATING
        if status == "COOLING":
            return HVACAction.COOLING
        return HVACAction.IDLE

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode.

        Independent audit finding (2026-09, fourth round, HVC-004): HEAT
        and AUTO used to perform the IDENTICAL operation (resume the week
        program) despite being advertised as two distinct, independently
        selectable modes — selecting HEAT did not produce any different
        outcome from selecting AUTO, which is misleading given HA's
        climate entity contract implies each advertised mode is a genuine,
        distinct target state. Fixed by giving HEAT a real, different
        target: the circuit's "constant" program (a fixed target
        temperature, no schedule) — reusing api.set_program(), an already
        proven, already-used mechanism (select.py's Constant option calls
        this same thing), not a new unvalidated API interaction. AUTO
        keeps its original meaning: resume the underlying week schedule.
        This also makes the write side consistent with the read side
        (hvac_mode below already reports HEAT specifically when the
        circuit is NOT on a week/eco program, i.e. exactly the "constant"-
        like states) — selecting HEAT now actually produces the state
        hvac_mode would report back as HEAT, instead of silently
        resuming the schedule and often landing back in AUTO.
        """
        try:
            if hvac_mode == HVACMode.OFF:
                await self.coordinator.async_control_and_refresh(
                    self.coordinator.api.set_circuit_mode(
                        self._plant_id,
                        self._circuit_path,
                        OPERATION_MODE_STANDBY,
                    ),
                    plant_id=self._plant_id,
                    circuit_path=self._circuit_path,
                    mode_override=OPERATION_MODE_STANDBY,
                )
            elif hvac_mode == HVACMode.HEAT:
                await self.coordinator.async_control_and_refresh(
                    self.coordinator.api.set_program(
                        self._plant_id,
                        self._circuit_path,
                        "constant",
                    ),
                    plant_id=self._plant_id,
                    circuit_path=self._circuit_path,
                    mode_override=OPERATION_MODE_REGULAR,
                )
            elif hvac_mode == HVACMode.AUTO:
                # Independent audit finding (2026-09, HVC-ICS-008 + "more"
                # report finding #8): preserve week2 if that's actually
                # (freshly-confirmed) active — see resolve_resume_program().
                resume_program = await resolve_resume_program(
                    self.coordinator.api, self._plant_id, self._circuit_path, self._circuit
                )
                await self.coordinator.async_control_and_refresh(
                    self.coordinator.api.reset_circuit(
                        self._plant_id,
                        self._circuit_path,
                        program=resume_program,
                    ),
                    plant_id=self._plant_id,
                    circuit_path=self._circuit_path,
                    mode_override=OPERATION_MODE_REGULAR,
                )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set HVAC mode: {err}") from err

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature via temporary change.

        Independent audit finding (2026-09, HVC-007): _attr_min_temp/
        _attr_max_temp were declared but never enforced here — any value
        (including out-of-range or non-finite) went straight to the API.
        Clamped, not rejected, to match this codebase's existing convention
        for out-of-range input (see clamp_hv_air_volume in fan.py,
        clamp_weather_impact_* in number.py) rather than introducing a
        different failure mode just for this entity.
        """
        temperature = kwargs.get("temperature")
        if temperature is None:
            return
        try:
            temperature = clamp_temperature(
                float(temperature), self._attr_min_temp, self._attr_max_temp
            )
        except (ValueError, TypeError) as err:
            raise HomeAssistantError(f"Invalid target temperature: {temperature!r}") from err
        duration = self._entry.options.get(
            CONF_OVERRIDE_DURATION,
            DEFAULT_OVERRIDE_DURATION,
        )
        # Independent audit finding (2026-09, fourth round, HVC-007): found
        # a second occurrence of the same gap fixed in fan.py's
        # _override_duration — validated here too, rather than trusting an
        # out-of-band persisted value to reach the API unchanged.
        if duration not in VALID_OVERRIDE_DURATIONS:
            duration = DEFAULT_OVERRIDE_DURATION
        try:
            await self.coordinator.async_control_and_refresh(
                self.coordinator.api.set_temporary_change(
                    self._plant_id,
                    self._circuit_path,
                    value=float(temperature),
                    duration=duration,
                ),
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set temperature: {err}") from err
