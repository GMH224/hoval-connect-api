"""Tests for the v1.0.2 fixes to custom_components/hoval_connect/climate.py.

Entities are constructed by bypassing __init__ (object.__new__) and setting
only the instance attributes the methods under test actually touch — the
same lightweight pattern used for pure-logic testing elsewhere in this
suite, avoiding the need to stand up a full config-entry/device-registry
stack just to exercise `available` and `async_set_hvac_mode`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.hoval_connect.climate import HovalClimate


def _make_climate(plant=None, circuit=None) -> HovalClimate:
    entity = object.__new__(HovalClimate)
    entity._plant_id = "p1"
    entity._circuit_path = "hv-1"
    coordinator = MagicMock()
    plants = {}
    if plant is not None:
        plants["p1"] = plant
    coordinator.data.plants = plants
    coordinator.api = MagicMock()
    coordinator.async_control_and_refresh = AsyncMock()
    entity.coordinator = coordinator
    return entity


def _make_plant(is_online=True, circuit=None):
    plant = MagicMock()
    plant.is_online = is_online
    plant.circuits = {"hv-1": circuit} if circuit is not None else {}
    return plant


class TestCrit008PlantOnlineGatesAvailability:
    def test_available_when_plant_online_and_circuit_present(self):
        circuit = MagicMock()
        entity = _make_climate(plant=_make_plant(is_online=True, circuit=circuit))
        assert entity.available is True

    def test_unavailable_when_plant_offline(self):
        circuit = MagicMock()
        entity = _make_climate(plant=_make_plant(is_online=False, circuit=circuit))
        assert entity.available is False

    def test_unavailable_when_plant_missing(self):
        entity = _make_climate(plant=None)
        assert entity.available is False

    def test_unavailable_when_circuit_missing_even_if_plant_online(self):
        entity = _make_climate(plant=_make_plant(is_online=True, circuit=None))
        assert entity.available is False


class TestHigh019RejectsUnsupportedHvacMode:
    @pytest.mark.asyncio
    async def test_unsupported_mode_raises(self):
        entity = _make_climate(plant=_make_plant())
        entity._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]
        with pytest.raises(HomeAssistantError, match="Unsupported HVAC mode"):
            await entity.async_set_hvac_mode(HVACMode.DRY)
        entity.coordinator.async_control_and_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_off_mode_still_calls_control_and_refresh(self):
        entity = _make_climate(plant=_make_plant())
        entity._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]
        await entity.async_set_hvac_mode(HVACMode.OFF)
        entity.coordinator.async_control_and_refresh.assert_awaited_once()
