"""Tests for the v1.0.2 fixes to custom_components/hoval_connect/fan.py.

See test_climate_v1_0_2_fixes.py for the object.__new__ construction
pattern used here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.hoval_connect.fan import HovalFan


def _make_fan(plant=None) -> HovalFan:
    entity = object.__new__(HovalFan)
    entity._plant_id = "p1"
    entity._circuit_path = "hv-1"
    entity._debounce_task = None
    entity._committed_task = None
    entity._pending_percentage = None
    entity._entry = MagicMock()
    entity._entry.options = {}
    coordinator = MagicMock()
    plants = {"p1": plant} if plant is not None else {}
    coordinator.data.plants = plants
    coordinator.api = MagicMock()
    coordinator.async_control_and_refresh = AsyncMock()
    entity.coordinator = coordinator
    entity.async_write_ha_state = MagicMock()
    return entity


def _make_plant(circuit=None):
    plant = MagicMock()
    plant.is_online = True
    plant.circuits = {"hv-1": circuit} if circuit is not None else {}
    return plant


class TestMed001PendingValueNotClobberedByOlderWrite:
    @pytest.mark.asyncio
    async def test_older_write_does_not_clear_a_newer_pending_value(self):
        entity = _make_fan(plant=_make_plant(circuit=MagicMock()))
        # Simulate: an older debounced write for 30% is now actually
        # sending, but the user has since dragged the slider to 60% (a
        # newer async_set_percentage call already overwrote the pending
        # value and would have restarted the debounce timer for it).
        entity._pending_percentage = 60
        await entity._send_percentage(30)
        # The older write (30%) must NOT have cleared the newer pending
        # value (60%) — ICS-MED-001.
        assert entity._pending_percentage == 60

    @pytest.mark.asyncio
    async def test_matching_write_does_clear_the_pending_value(self):
        entity = _make_fan(plant=_make_plant(circuit=MagicMock()))
        entity._pending_percentage = 45
        await entity._send_percentage(45)
        assert entity._pending_percentage is None


class TestMed002PercentageValidatedBeforeDisplay:
    @pytest.mark.asyncio
    async def test_out_of_range_percentage_rejected(self):
        entity = _make_fan(plant=_make_plant(circuit=MagicMock()))
        with pytest.raises(HomeAssistantError, match="Invalid fan percentage"):
            await entity.async_set_percentage(150)
        # Must not have been written to the pending display value.
        assert entity._pending_percentage is None

    @pytest.mark.asyncio
    async def test_negative_percentage_rejected(self):
        entity = _make_fan(plant=_make_plant(circuit=MagicMock()))
        with pytest.raises(HomeAssistantError, match="Invalid fan percentage"):
            await entity.async_set_percentage(-5)

    @pytest.mark.asyncio
    async def test_non_integer_percentage_rejected(self):
        entity = _make_fan(plant=_make_plant(circuit=MagicMock()))
        with pytest.raises(HomeAssistantError, match="Invalid fan percentage"):
            await entity.async_set_percentage(50.5)

    @pytest.mark.asyncio
    async def test_valid_percentage_is_accepted_and_stored(self):
        entity = _make_fan(plant=_make_plant(circuit=MagicMock()))
        entity._cancel_debounce = MagicMock()
        entity.hass = MagicMock()

        def _discard_coroutine(coro):
            coro.close()
            return MagicMock()

        entity.coordinator.create_tracked_task = MagicMock(side_effect=_discard_coroutine)
        await entity.async_set_percentage(50)
        assert entity._pending_percentage == 50
