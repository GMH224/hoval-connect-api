"""Binary sensor platform for Hoval Connect."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import HovalConnectConfigEntry, plant_device_info
from .const import CLOUD_API_PROBLEM_THRESHOLD
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalDataCoordinator, HovalPlantData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Hoval binary sensor entities."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[BinarySensorEntity] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            uid_online = f"{plant_id}_online"
            uid_error = f"{plant_id}_error"
            uid_cloud_problem = f"{plant_id}_cloud_api_problem"
            if uid_online not in known:
                known.add(uid_online)
                entities.append(HovalPlantOnline(coordinator, plant_id, plant_data))
            if uid_error not in known:
                known.add(uid_error)
                entities.append(HovalPlantError(coordinator, plant_id, plant_data))
            if uid_cloud_problem not in known:
                known.add(uid_cloud_problem)
                entities.append(HovalCloudApiProblem(coordinator, plant_id, plant_data))
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalPlantOnline(CoordinatorEntity[HovalDataCoordinator], BinarySensorEntity):
    """Binary sensor for plant online status."""

    _attr_has_entity_name = True
    _attr_translation_key = "plant_online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_online"
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def is_on(self) -> bool | None:
        """Return true if the plant is online."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.is_online


class HovalPlantError(CoordinatorEntity[HovalDataCoordinator], BinarySensorEntity):
    """Binary sensor for plant error status."""

    _attr_has_entity_name = True
    _attr_translation_key = "plant_error"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_error"
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def is_on(self) -> bool | None:
        """Return true if the plant has an active error."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.has_error


class HovalCloudApiProblem(CoordinatorEntity[HovalDataCoordinator], BinarySensorEntity):
    """Diagnostic: has the cloud API gone unreachable for a while?

    v1.0.0. Since this integration no longer polls telemetry, its only
    remaining regular cloud traffic is a minimal health check every
    HEALTH_CHECK_INTERVAL (30 min by default) plus whatever writes the user
    actually makes. This sensor answers the question the user actually
    cares about — "do I need to go look at this?" — rather than reporting
    every transient blip: it turns on only once CLOUD_API_PROBLEM_THRESHOLD
    (2 hours) has passed with no successful contact of ANY kind (a health
    check OR a write), and off again the moment either one succeeds.

    Always available, like the rest of the connection-health diagnostics —
    CoordinatorEntity's default availability (tied to the last update's
    success) would make this sensor go unavailable exactly when it's most
    useful.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "cloud_api_problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_data: HovalPlantData,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{plant_id}_cloud_api_problem"
        self._attr_device_info = plant_device_info(plant_data)

    @property
    def available(self) -> bool:
        """Always available — tracking cloud reachability is the whole point."""
        return True

    @property
    def is_on(self) -> bool:
        """True once too long has passed since the last successful contact.

        No successful contact ever recorded (e.g. right after a fresh
        install, before the first health check has even run) is treated as
        a problem rather than "unknown", since there's nothing to report
        with confidence otherwise.
        """
        last = self.coordinator.connection_health.last_successful_contact_at
        if last is None:
            return True
        return dt_util.utcnow() - last > CLOUD_API_PROBLEM_THRESHOLD

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose the raw timestamp for automations/dashboards that want it."""
        last = self.coordinator.connection_health.last_successful_contact_at
        return {"last_successful_contact": last.isoformat() if last else None}
