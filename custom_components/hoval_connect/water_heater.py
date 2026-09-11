"""Water heater platform for Hoval Connect (WW hot water circuits)."""

from __future__ import annotations

import logging

from homeassistant.components.water_heater import (
    STATE_HEAT_PUMP,
    STATE_HIGH_DEMAND,
    STATE_OFF,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    async_get_current_platform,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .api import HovalApiError
from .const import (
    CIRCUIT_TYPE_WW,
    OPERATION_MODE_REGULAR,
    OPERATION_MODE_STANDBY,
    SERVICE_RESET_WW_BOOST,
    clamp_temperature,
)
from .coordinator import (
    SIGNAL_NEW_CIRCUITS,
    HovalCircuitData,
    HovalDataCoordinator,
    resolve_resume_program,
)

_LOGGER = logging.getLogger(__name__)

# Temperature limits for WW circuits (°C).
# The Hoval app allows 10–65 °C; we use a safe operational range.
WW_MIN_TEMP = 10.0
WW_MAX_TEMP = 65.0
WW_TEMP_STEP = 0.5

# Operation modes exposed to HA
_OP_HEAT_PUMP = STATE_HEAT_PUMP  # "heat_pump"  — normal week-program operation
_OP_HIGH_DEMAND = STATE_HIGH_DEMAND  # "high_demand" — temporary boost override active
_OP_OFF = STATE_OFF  # "off"         — circuit in standby


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Hoval water heater entities for WW circuits."""
    coordinator = entry.runtime_data.coordinator
    plant_devices = entry.runtime_data.plant_devices
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[HovalWaterHeater] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            plant_device_id = plant_devices.async_get_device_id(plant_id, plant_data)
            for path, circuit in plant_data.circuits.items():
                uid = f"{plant_id}_{path}_water_heater"
                if circuit.circuit_type != CIRCUIT_TYPE_WW or uid in known:
                    continue
                known.add(uid)
                entities.append(
                    HovalWaterHeater(coordinator, entry, plant_id, plant_device_id, path, circuit)
                )
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))

    # Register the reset_ww_boost entity service so automations can call it
    # by targeting one or more HovalWaterHeater entities.
    platform = async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_RESET_WW_BOOST,
        {},  # no extra fields — the entity already knows its plant/circuit
        "async_reset_temporary_change",
    )


class HovalWaterHeater(CoordinatorEntity[HovalDataCoordinator], WaterHeaterEntity):
    """Hoval hot water circuit entity.

    Exposes:
    - current_temperature  — live top-of-tank sensor (tempSf1Actual)
    - target_temperature   — active setpoint (tempTarget from live values)
    - operation_mode       — heat_pump (normal) / high_demand (boost override) / off (standby)
    - set_temperature()    — posts a temporary-change override until midnight
    - set_operation_mode() — switches between heat_pump (reset to week program) and off (standby)
    """

    _attr_has_entity_name = True
    _attr_translation_key = "hot_water"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = WW_MIN_TEMP
    _attr_max_temp = WW_MAX_TEMP
    _attr_target_temperature_step = WW_TEMP_STEP
    _attr_operation_list = [_OP_HEAT_PUMP, _OP_OFF]
    # Independent audit finding (2026-09, HVC-ICS-003): _OP_HIGH_DEMAND is
    # deliberately NOT in this list. current_operation (below) can still
    # report it — a temporary boost genuinely can be active, and that's a
    # real, observable state — but it was never actually selectable: the
    # old async_set_operation_mode() branch treated `heat_pump` and
    # `high_demand` identically, both resetting to the normal schedule,
    # which is the opposite of what selecting "high_demand" implies. The
    # correct way to start a boost is already implemented correctly:
    # async_set_temperature() (a real WaterHeaterEntityFeature.TARGET_
    # TEMPERATURE call, which callers already use) sends a genuine
    # temporary-change command with an actual target value — something
    # async_set_operation_mode() never receives (it only gets a mode
    # string, no numeric value to boost to). See docs/audit-v1.0.0.md.
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.OPERATION_MODE
    )

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        entry: HovalConnectConfigEntry,
        plant_id: str,
        plant_device_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the water heater entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_water_heater"
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
        """Return current water temperature (top-of-tank sensor).

        Independent audit finding (2026-09, HVC-003): `circuit.actual_value`
        (free, from the circuits-list response) is checked first.
        """
        circuit = self._circuit
        if circuit is None:
            return None
        if circuit.actual_value is not None:
            return circuit.actual_value
        val = circuit.live_values.get("tempSf1Actual") or circuit.live_values.get("tempActual")
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return target water temperature.

        Independent audit finding (2026-09, HVC-003): `circuit.target_value`
        (already fetched for the write path anyway) is checked first.
        """
        circuit = self._circuit
        if circuit is None:
            return None
        if circuit.target_value is not None:
            return circuit.target_value
        val = circuit.live_values.get("tempTarget")
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def current_operation(self) -> str | None:
        """Return current operation mode.

        Independent audit finding (2026-09, HVC-006 and HVC-003):
        - Missing/None operationMode previously fell through to
          _OP_HEAT_PUMP, reporting an active state with no actual basis.
          Returns None (WaterHeaterEntity supports this) instead.
        - `circuit.temporary_change_active` (free, derived from the
          circuits-list response's `temporaryChange` object being non-null)
          replaces the old `live_values.get("temporaryChangeActive") ==
          "true"` check, which was permanently False since v1.0.0 stopped
          fetching live values at all.
        """
        circuit = self._circuit
        if circuit is None:
            return None
        override = self.coordinator.get_mode_override(self._plant_id, self._circuit_path)
        mode = override if override is not None else circuit.operation_mode
        if mode is None:
            return None
        if mode == OPERATION_MODE_STANDBY:
            return _OP_OFF
        # If a temporary change is active, show as high_demand
        if circuit.temporary_change_active:
            return _OP_HIGH_DEMAND
        return _OP_HEAT_PUMP

    async def async_set_temperature(self, **kwargs) -> None:
        """Apply a temporary temperature override until midnight.

        The Hoval API's 'midnight' duration means the override automatically
        expires at 00:00, so the regular week program resumes the next day
        without any cleanup automation.

        Independent audit finding (2026-09, HVC-007): _attr_min_temp/
        _attr_max_temp were declared but never enforced — see the matching
        fix and rationale in climate.py's async_set_temperature.
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
        _LOGGER.debug(
            "WW set_temperature: circuit=%s temp=%s (override until midnight)",
            self._circuit_path,
            temperature,
        )
        try:
            await self.coordinator.async_control_and_refresh(
                self.coordinator.api.set_temporary_change(
                    self._plant_id,
                    self._circuit_path,
                    value=float(temperature),
                    duration="midnight",
                ),
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set hot water temperature: {err}") from err

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Switch operation mode.

        Independent audit finding (2026-09, HVC-ICS-003): `high_demand` was
        removed from `_attr_operation_list` (see that attribute's comment)
        because there was never a real implementation for selecting it —
        this method treated it identically to `heat_pump`. It's rejected
        explicitly here too (raising, not silently resetting to normal)
        in case anything calls this service directly with a value outside
        the currently-advertised list.
        """
        try:
            if operation_mode == _OP_OFF:
                await self.coordinator.async_control_and_refresh(
                    self.coordinator.api.set_program(
                        self._plant_id,
                        self._circuit_path,
                        "standby",
                    ),
                    plant_id=self._plant_id,
                    circuit_path=self._circuit_path,
                    mode_override=OPERATION_MODE_STANDBY,
                )
            elif operation_mode == _OP_HEAT_PUMP:
                # Reset to the normal week program. Independent audit
                # finding (2026-09, HVC-ICS-008 + "more" report finding
                # #8): preserve week2 if that's actually (freshly-
                # confirmed) active — see resolve_resume_program().
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
            else:
                raise HomeAssistantError(
                    f"Unsupported operation mode: {operation_mode!r}. To start a "
                    "temporary boost, set a target temperature instead — that's "
                    "what actually activates one; there is no separate "
                    "'high_demand' operation-mode command."
                )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set operation mode: {err}") from err

    async def async_reset_temporary_change(self) -> None:
        """Cancel any active temporary WW temperature boost and resume the week program.

        This mirrors the 'reset' action in the Hoval app: it issues a DELETE to
        the temporary-change endpoint, which immediately removes the manual override
        and hands control back to the active week/day program — without touching the
        program itself or switching to standby.

        Safe to call even when no temporary change is active; the API treats it as
        a no-op in that case.
        """
        _LOGGER.debug(
            "reset_ww_boost: cancelling temporary change for circuit=%s",
            self._circuit_path,
        )
        try:
            await self.coordinator.async_control_and_refresh(
                self.coordinator.api.reset_temporary_change(
                    self._plant_id,
                    self._circuit_path,
                ),
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to reset hot water temporary boost: {err}") from err
