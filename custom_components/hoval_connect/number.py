"""Number platform for Hoval Connect (weather-based control Eco/Comfort sliders).

Exposes the two "Weather based control" weighting sliders added to the Hoval
Connect app in 2026-07 (see docs/reverse-engineering-2026-05-23.md and
CircuitSettingsDTO.weatherImpact in docs/openapi-v3.json):

    - "by outside temperature"  -> weatherImpact.outsideTemperature (0..100)
    - "by solar radiation"      -> weatherImpact.solarRadiation    (-10..0)

For both fields the minimum of the API's documented range is the app's "Eco"
end and the maximum is "Comfort", so a plain min->max HA number slider
reproduces the app control 1:1 without any extra UI-side rescaling.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .api import HovalApiError
from .const import (
    SUPPORTS_WEATHER_IMPACT,
    WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MAX,
    WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MIN,
    WEATHER_IMPACT_SOLAR_RADIATION_MAX,
    WEATHER_IMPACT_SOLAR_RADIATION_MIN,
)
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

# Sliders fire many intermediate values while being dragged in the HA frontend;
# debounce so only the settled value is actually sent to the cloud. Mirrors
# fan.py's DEBOUNCE_SECONDS for the same reason (HV speed slider).
DEBOUNCE_SECONDS = 1.5


@dataclass(frozen=True, kw_only=True)
class HovalWeatherImpactNumberDescription(NumberEntityDescription):
    """Describe a weather-impact weighting number entity."""

    value_fn: Callable[[HovalCircuitData], float | None]
    # Keyword name accepted by HovalDataCoordinator.async_set_weather_impact
    # / resolve_weather_impact_update() ("outside_temperature" or "solar_radiation").
    api_field: str = ""
    # Matching key in the raw weatherImpact dict / optimistic-override dict
    # ("outsideTemperature" or "solarRadiation").
    override_key: str = ""


WEATHER_IMPACT_NUMBER_DESCRIPTIONS: tuple[HovalWeatherImpactNumberDescription, ...] = (
    HovalWeatherImpactNumberDescription(
        key="weather_impact_outside_temperature",
        translation_key="weather_impact_outside_temperature",
        icon="mdi:thermometer",
        entity_category=EntityCategory.CONFIG,
        native_min_value=WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MIN,
        native_max_value=WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MAX,
        native_step=1,
        mode=NumberMode.SLIDER,
        api_field="outside_temperature",
        override_key="outsideTemperature",
        value_fn=lambda c: c.weather_impact_outside_temperature,
    ),
    HovalWeatherImpactNumberDescription(
        key="weather_impact_solar_radiation",
        translation_key="weather_impact_solar_radiation",
        icon="mdi:weather-sunny",
        entity_category=EntityCategory.CONFIG,
        native_min_value=WEATHER_IMPACT_SOLAR_RADIATION_MIN,
        native_max_value=WEATHER_IMPACT_SOLAR_RADIATION_MAX,
        native_step=1,
        mode=NumberMode.SLIDER,
        api_field="solar_radiation",
        override_key="solarRadiation",
        value_fn=lambda c: c.weather_impact_solar_radiation,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval weather-impact number entities."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[HovalWeatherImpactNumber] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            for path, circuit in plant_data.circuits.items():
                if circuit.circuit_type not in SUPPORTS_WEATHER_IMPACT:
                    continue
                for description in WEATHER_IMPACT_NUMBER_DESCRIPTIONS:
                    uid = f"{plant_id}_{path}_{description.key}"
                    if uid in known:
                        continue
                    known.add(uid)
                    entities.append(
                        HovalWeatherImpactNumber(coordinator, plant_id, path, circuit, description)
                    )
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalWeatherImpactNumber(CoordinatorEntity[HovalDataCoordinator], NumberEntity):
    """Number entity for one weather-based-control Eco/Comfort weighting slider."""

    _attr_has_entity_name = True
    entity_description: HovalWeatherImpactNumberDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
        description: HovalWeatherImpactNumberDescription,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_{description.key}"
        self._attr_device_info = circuit_device_info(plant_id, circuit_data)
        self._debounce_task: asyncio.Task | None = None
        self._pending_value: float | None = None

    def _cancel_debounce(self) -> None:
        """Cancel any pending debounce task safely."""
        task = self._debounce_task
        if task is not None and not task.done():
            task.cancel()
        self._debounce_task = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending debounce task on removal."""
        self._cancel_debounce()
        await super().async_will_remove_from_hass()

    @property
    def _circuit(self) -> HovalCircuitData | None:
        """Get current circuit data from coordinator."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.circuits.get(self._circuit_path)

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Requires the circuit to still be present AND either the last poll to
        have confirmed this circuit reports weatherImpact, or a still-fresh
        optimistic override to exist (covers the edge case of a control
        action landing before the very first settings poll completes).
        """
        circuit = self._circuit
        if not super().available or circuit is None:
            return False
        if circuit.weather_impact_supported:
            return True
        return self.coordinator.get_weather_impact_override(self._circuit_path) is not None

    @property
    def native_value(self) -> float | None:
        """Return the current weighting value.

        Priority: an in-flight pending drag value > a still-fresh optimistic
        override from a very recent write > the last polled coordinator data.
        Consulting the override directly here (rather than relying solely on
        the next poll to have folded it into HovalCircuitData) avoids a
        multi-second flicker back to the pre-update value between a
        successful write and the background refresh landing.
        """
        if self._pending_value is not None:
            return self._pending_value
        override = self.coordinator.get_weather_impact_override(self._circuit_path)
        if override is not None:
            value = override.get(self.entity_description.override_key)
            if value is not None:
                return value
        circuit = self._circuit
        if circuit is None:
            return None
        return self.entity_description.value_fn(circuit)

    async def _send_value(self, value: float) -> None:
        """Actually send the value to the API (called after debounce)."""
        self._pending_value = None
        kwargs = {self.entity_description.api_field: value}
        try:
            await self.coordinator.async_set_weather_impact(
                self._plant_id,
                self._circuit_path,
                **kwargs,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set {self.entity_description.key}: {err}") from err
        finally:
            self.async_write_ha_state()

    async def _debounced_set(self, value: float) -> None:
        """Wait for debounce period, then send the latest value."""
        await asyncio.sleep(DEBOUNCE_SECONDS)
        _LOGGER.debug("Debounce complete, sending %s=%s", self.entity_description.key, value)
        await self._send_value(value)

    async def async_set_native_value(self, value: float) -> None:
        """Set the weighting value (debounced)."""
        _LOGGER.debug("async_set_native_value called: %s=%s", self.entity_description.key, value)
        # Store pending value and update UI immediately.
        self._pending_value = value
        self.async_write_ha_state()
        # Cancel previous debounce timer and start a new one.
        self._cancel_debounce()
        self._debounce_task = self.hass.async_create_task(self._debounced_set(value))
