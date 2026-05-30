"""Binary sensor platform for Hoval Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info, plant_device_info
from .const import CIRCUIT_TYPE_BL, CIRCUIT_TYPE_WEZ
from .coordinator import (
    SIGNAL_NEW_CIRCUITS,
    HovalCircuitData,
    HovalDataCoordinator,
    HovalPlantData,
)

# Values returned by circuitStatus that mean the generator is NOT running.
# Any other non-None value is treated as "active / running".
_INACTIVE_STATUSES: frozenset[str] = frozenset({"inactive", "standby", "off", "error"})


@dataclass(frozen=True, kw_only=True)
class HovalCircuitBinarySensorDescription(BinarySensorEntityDescription):
    """Describe a Hoval circuit-level binary sensor."""

    value_fn: Callable[[HovalCircuitData], bool | None]
    circuit_types: frozenset[str] | None = None


# ---------------------------------------------------------------------------
# Circuit-level binary sensor descriptions
# ---------------------------------------------------------------------------

CIRCUIT_BINARY_SENSOR_DESCRIPTIONS: tuple[HovalCircuitBinarySensorDescription, ...] = (
    HovalCircuitBinarySensorDescription(
        key="wez_active",
        translation_key="wez_active",
        device_class=BinarySensorDeviceClass.RUNNING,
        circuit_types=frozenset({CIRCUIT_TYPE_BL, CIRCUIT_TYPE_WEZ}),
        # True  → heat generator is running (circuitStatus not in inactive set)
        # False → heat generator is idle / standby
        # None  → status unknown (circuitStatus missing from API response)
        value_fn=lambda c: (
            c.circuit_status.lower() not in _INACTIVE_STATUSES
            if c.circuit_status is not None
            else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hoval binary sensor entities."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[BinarySensorEntity] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            # Plant-level binary sensors (online / error)
            uid_online = f"{plant_id}_online"
            uid_error = f"{plant_id}_error"
            if uid_online not in known:
                known.add(uid_online)
                entities.append(HovalPlantOnline(coordinator, plant_id, plant_data))
            if uid_error not in known:
                known.add(uid_error)
                entities.append(HovalPlantError(coordinator, plant_id, plant_data))

            # Circuit-level binary sensors (heat generator active, etc.)
            for path, circuit in plant_data.circuits.items():
                for description in CIRCUIT_BINARY_SENSOR_DESCRIPTIONS:
                    if (
                        description.circuit_types is not None
                        and circuit.circuit_type not in description.circuit_types
                    ):
                        continue
                    uid = f"{plant_id}_{path}_{description.key}"
                    if uid in known:
                        continue
                    known.add(uid)
                    entities.append(
                        HovalCircuitBinarySensor(
                            coordinator, plant_id, path, circuit, description
                        )
                    )

        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


# ---------------------------------------------------------------------------
# Plant-level binary sensors
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Circuit-level binary sensors
# ---------------------------------------------------------------------------


class HovalCircuitBinarySensor(CoordinatorEntity[HovalDataCoordinator], BinarySensorEntity):
    """Binary sensor for a circuit-level boolean state.

    Used for: heat generator active status (BL / WEZ circuits).
    The sensor maps the circuit's ``circuitStatus`` field to a running/idle
    binary state and uses ``BinarySensorDeviceClass.RUNNING`` so HA renders
    a meaningful "Running" / "Idle" label on the entity card.
    """

    _attr_has_entity_name = True
    entity_description: HovalCircuitBinarySensorDescription

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
        description: HovalCircuitBinarySensorDescription,
    ) -> None:
        """Initialize the circuit binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_{description.key}"
        self._attr_device_info = circuit_device_info(plant_id, circuit_data)

    @property
    def _circuit(self) -> HovalCircuitData | None:
        """Get current circuit data from the coordinator."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.circuits.get(self._circuit_path)

    @property
    def available(self) -> bool:
        """Return True when the circuit is present in coordinator data."""
        return super().available and self._circuit is not None

    @property
    def is_on(self) -> bool | None:
        """Return the binary state for this circuit."""
        circuit = self._circuit
        if circuit is None:
            return None
        return self.entity_description.value_fn(circuit)
