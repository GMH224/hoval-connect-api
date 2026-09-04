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
)
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator

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

        Live-values key for HK circuits is 'roomTempActual'.  The fallback
        keys cover future circuit types that may use different field names.
        """
        circuit = self._circuit
        if circuit is None:
            return None
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

        Live-values key for HK circuits is 'roomTempTarget'.
        """
        circuit = self._circuit
        if circuit is None:
            return None
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
        """Return the current HVAC mode."""
        circuit = self._circuit
        if circuit is None:
            return None
        override = self.coordinator.get_mode_override(self._circuit_path)
        mode = override if override is not None else circuit.operation_mode
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
        override = self.coordinator.get_mode_override(self._circuit_path)
        mode = override if override is not None else circuit.operation_mode
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
        """Set HVAC mode."""
        try:
            if hvac_mode == HVACMode.OFF:
                await self.coordinator.async_control_and_refresh(
                    self.coordinator.api.set_circuit_mode(
                        self._plant_id,
                        self._circuit_path,
                        OPERATION_MODE_STANDBY,
                    ),
                    circuit_path=self._circuit_path,
                    mode_override=OPERATION_MODE_STANDBY,
                )
            elif hvac_mode in (HVACMode.AUTO, HVACMode.HEAT):
                await self.coordinator.async_control_and_refresh(
                    self.coordinator.api.reset_circuit(
                        self._plant_id,
                        self._circuit_path,
                    ),
                    circuit_path=self._circuit_path,
                    mode_override=OPERATION_MODE_REGULAR,
                )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set HVAC mode: {err}") from err

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature via temporary change."""
        temperature = kwargs.get("temperature")
        if temperature is None:
            return
        duration = self._entry.options.get(
            CONF_OVERRIDE_DURATION,
            DEFAULT_OVERRIDE_DURATION,
        )
        try:
            await self.coordinator.async_control_and_refresh(
                self.coordinator.api.set_temporary_change(
                    self._plant_id,
                    self._circuit_path,
                    value=float(temperature),
                    duration=duration,
                ),
                circuit_path=self._circuit_path,
                mode_override=OPERATION_MODE_REGULAR,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set temperature: {err}") from err
