"""Select platform for Hoval Connect (program selection)."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HovalConnectConfigEntry, circuit_device_info
from .api import HovalApiError
from .const import CIRCUIT_TYPE_HK, CIRCUIT_TYPE_HV, CIRCUIT_TYPE_WW, OPERATION_MODE_REGULAR
from .coordinator import SIGNAL_NEW_CIRCUITS, HovalCircuitData, HovalDataCoordinator

_LOGGER = logging.getLogger(__name__)

# API program keys in display order
API_PROGRAMS = ["week1", "week2", "ecoMode", "standby", "constant"]

# Full set of program identifiers the cloud accepts on the programs endpoint.
# Used to validate a resolved key before sending so an unmapped display string
# cannot be forwarded verbatim to the API (which would 400 on the round-trip).
VALID_API_PROGRAMS = frozenset(
    {"week1", "week2", "ecoMode", "standby", "constant", "manual", "externalConstant"}
)

# Fallback display names when API doesn't provide custom names
DEFAULT_NAMES: dict[str, str] = {
    "week1": "Week 1",
    "week2": "Week 2",
    "ecoMode": "Eco mode",
    "standby": "Standby",
    "constant": "Constant",
}


def resolve_program_display_names(program_names: dict[str, str]) -> dict[str, str]:
    """Map every API program key to a UNIQUE display name.

    Independent audit finding (2026-09, "more" report, finding #10): a
    naive per-key lookup can't detect that two DIFFERENT keys produced the
    SAME display name — if a user names both week1 and week2 identically
    in the Hoval app (e.g. both "Summer"), the select's option list would
    contain a literal duplicate, and the reverse lookup (display name ->
    API key) would always resolve to whichever key happened to be checked
    first, regardless of which duplicate entry the user actually clicked.
    Computing every key's name at once (rather than one key in isolation)
    is what makes the collision detectable at all.

    Any name that collides across more than one key gets disambiguated by
    appending the API key itself (e.g. "Summer (week1)" / "Summer
    (week2)"); a name that's already unique across all programs is
    returned completely unchanged, so the common case (no duplicates)
    looks exactly as before.

    A standalone pure function (no entity/coordinator dependency) so this
    algorithm is directly unit-testable — see tests/test_select.py.
    """
    raw_names = {key: program_names.get(key, DEFAULT_NAMES.get(key, key)) for key in API_PROGRAMS}
    counts: dict[str, int] = {}
    for name in raw_names.values():
        counts[name] = counts.get(name, 0) + 1
    return {
        key: (f"{name} ({key})" if counts[name] > 1 else name) for key, name in raw_names.items()
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HovalConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Hoval select entities."""
    coordinator = entry.runtime_data.coordinator
    plant_devices = entry.runtime_data.plant_devices
    known: set[str] = set()

    def _add_new() -> None:
        entities: list[HovalProgramSelect] = []
        for plant_id, plant_data in coordinator.data.plants.items():
            plant_device_id = plant_devices.async_get_device_id(plant_id, plant_data)
            for path, circuit in plant_data.circuits.items():
                uid = f"{plant_id}_{path}_program"
                if (
                    circuit.circuit_type not in (CIRCUIT_TYPE_HV, CIRCUIT_TYPE_HK, CIRCUIT_TYPE_WW)
                    or uid in known
                ):
                    continue
                known.add(uid)
                entities.append(
                    HovalProgramSelect(coordinator, plant_id, plant_device_id, path, circuit)
                )
        if entities:
            async_add_entities(entities)

    _add_new()

    @callback
    def _on_new_circuits() -> None:
        _add_new()

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_CIRCUITS, _on_new_circuits))


class HovalProgramSelect(CoordinatorEntity[HovalDataCoordinator], SelectEntity):
    """Select entity for choosing the active program on a circuit."""

    _attr_has_entity_name = True
    _attr_translation_key = "program"
    _attr_icon = "mdi:format-list-bulleted"

    def __init__(
        self,
        coordinator: HovalDataCoordinator,
        plant_id: str,
        plant_device_id: str,
        circuit_path: str,
        circuit_data: HovalCircuitData,
    ) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._circuit_path = circuit_path
        self._attr_unique_id = f"{plant_id}_{circuit_path}_program"
        self._attr_device_info = circuit_device_info(plant_id, plant_device_id, circuit_data)

    @property
    def _circuit(self) -> HovalCircuitData | None:
        """Get current circuit data from coordinator."""
        plant = self.coordinator.data.plants.get(self._plant_id)
        if plant is None:
            return None
        return plant.circuits.get(self._circuit_path)

    def _all_display_names(self) -> dict[str, str]:
        """Map every API program key to a UNIQUE display name for this circuit.

        See resolve_program_display_names() for the disambiguation logic
        itself (independent audit finding, 2026-09, "more" report,
        finding #10) — this just supplies this entity's current
        program_names.
        """
        circuit = self._circuit
        return resolve_program_display_names(circuit.program_names if circuit else {})

    @property
    def options(self) -> list[str]:
        """Return list of program display names."""
        return list(self._all_display_names().values())

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._circuit is not None

    @property
    def current_option(self) -> str | None:
        """Return the currently active program's display name.

        Independent audit finding (2026-09, fourth round, HVC-015): the
        API permits `activeProgram` values ("manual", "externalConstant")
        that were never included in `options` (API_PROGRAMS only lists the
        programs this integration lets a user actively select). Returning
        the raw string for one of those violated HA's own SelectEntity
        contract — `current_option` is supposed to always be a member of
        `options`, or None — which could confuse automations checking
        membership, or render oddly in the frontend. Returns None instead,
        the same choice already made for water_heater.py's `high_demand`
        (a real, observable state with no real "select this to activate
        it" action, not something to force into the selectable list).
        """
        circuit = self._circuit
        if circuit is None or circuit.active_program is None:
            return None
        display_names = self._all_display_names()
        if circuit.active_program not in display_names:
            return None
        return display_names[circuit.active_program]

    async def async_select_option(self, option: str) -> None:
        """Set the active program."""
        display_names = self._all_display_names()
        api_program = next((k for k, name in display_names.items() if name == option), option)
        if api_program not in VALID_API_PROGRAMS:
            raise HomeAssistantError(
                f"Unknown program '{option}' (resolved to '{api_program}'); "
                f"valid programs: {', '.join(sorted(VALID_API_PROGRAMS))}"
            )
        _LOGGER.debug(
            "Setting program to %s (%s) for %s",
            option,
            api_program,
            self._circuit_path,
        )
        mode = OPERATION_MODE_REGULAR if api_program != "standby" else "standby"
        try:
            await self.coordinator.async_control_and_refresh(
                self.coordinator.api.set_program(
                    self._plant_id,
                    self._circuit_path,
                    api_program,
                ),
                plant_id=self._plant_id,
                circuit_path=self._circuit_path,
                mode_override=mode,
            )
        except HovalApiError as err:
            raise HomeAssistantError(f"Failed to set program: {err}") from err
