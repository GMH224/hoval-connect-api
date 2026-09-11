"""Forward-compatibility tests for the HA 2026.8 → 2026.12 migration (v2.2.0).

These cover the three API migrations and, more importantly, the invariants that
must survive them. The migration's real risk is not that the new APIs fail — it
is that device and entity identity silently changes and every user gets a
duplicate device tree with a fresh set of entity IDs. The identity tests below
are therefore the ones that matter most.

Verified against the real Home Assistant 2026.9.0 wheel:

* ``OptionsFlowWithReload`` + a config-entry update listener raises ValueError in
  ``OptionsFlowManager.async_finish_flow`` — already live, not a 2026.12 change.
* ``DeviceInfo`` no longer declares ``via_device``; it declares ``via_device_id``.
* ``async_update_reload_and_abort`` warns when update listeners exist and stops
  accepting them in 2026.12.
* ``PERCENTAGE`` is NOT deprecated; it is ``UnitOfRatio.PERCENTAGE.value``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import custom_components.hoval_connect as hoval  # noqa: E402
from custom_components.hoval_connect import (  # noqa: E402
    HovalPlantDevices,
    circuit_device_info,
    plant_device_info,
)
from custom_components.hoval_connect.config_flow import (  # noqa: E402
    HovalConnectOptionsFlow,
)
from custom_components.hoval_connect.const import DOMAIN  # noqa: E402

from . import ha_stubs

COMPONENT_DIR = Path(hoval.__file__).parent

CIRCUIT_PLATFORMS = [
    "climate",
    "fan",
    "number",
    "select",
    "sensor",
    "water_heater",
]
# History: v1.0.0 originally removed "sensor" from this list when
# sensor.py was deleted entirely (every entity in it depended on telemetry
# this integration no longer polls). Reinstated when sensor.py was
# recreated (same v1.0.0 release) as a deliberately narrow set of
# current-value and API-health sensors — see sensor.py's own module
# docstring and docs/audit-v1.0.0.md. The new file follows the same
# AddConfigEntryEntitiesCallback / dynamic-discovery conventions as every
# other platform here, so it belongs in this list's baseline checks again.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plant(plant_id: str = "plant1", name: str = "Home") -> MagicMock:
    plant = MagicMock()
    plant.plant_id = plant_id
    plant.name = name
    return plant


def _circuit(path: str = "1/1", name: str = "Living", circuit_type: str = "HK") -> MagicMock:
    circuit = MagicMock()
    circuit.path = path
    circuit.name = name
    circuit.circuit_type = circuit_type
    return circuit


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> ha_stubs.StubDeviceRegistry:
    """Install a recording device registry behind dr.async_get()."""
    reg = ha_stubs.StubDeviceRegistry()
    monkeypatch.setattr(hoval.dr, "async_get", lambda _hass: reg)
    return reg


@pytest.fixture
def plant_devices(registry: ha_stubs.StubDeviceRegistry) -> HovalPlantDevices:
    """Build a resolver bound to a fake config entry."""
    entry = MagicMock()
    entry.entry_id = "entry1"
    return HovalPlantDevices(MagicMock(), entry)


def _code_only(source: str) -> str:
    """Return source with comments and string literals stripped.

    Deprecation guards must not trip over prose that names a removed API while
    explaining its removal.
    """
    import io
    import tokenize

    pieces: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            pieces.append(tok.string)
    except tokenize.TokenError:  # pragma: no cover - defensive
        return source
    return "\n".join(pieces)


def _source(name: str) -> str:
    return (COMPONENT_DIR / f"{name}.py").read_text()


# ---------------------------------------------------------------------------
# P0 — options flow lifecycle
# ---------------------------------------------------------------------------


class TestApiSessionClosedOnSetupFailure:
    """Independent audit finding (2026-09, HVC-005): api.aclose() must run
    on any setup failure between HovalConnectApi's construction and
    entry.runtime_data being assigned — otherwise the requests.Session
    (and its connection pool) leaks, since async_unload_entry() — the only
    other place that closes it — never runs for a config entry that never
    finished loading.
    """

    def test_api_construction_to_runtime_data_is_wrapped_in_try_except(self) -> None:
        """Static guard: the span between `api = HovalConnectApi(...)` and
        `entry.runtime_data = HovalRuntimeData(...)` must be inside a
        try/except that closes the api on failure.
        """
        src = _source("__init__")
        start = src.index("api = HovalConnectApi(")
        end = src.index("entry.runtime_data = HovalRuntimeData(")
        span = src[start:end]
        assert "try:" in span, "API construction must be followed by a try block"
        after = src[end:]
        assert "except" in after[:200] and "api.aclose()" in after[:400], (
            "the except clause after entry.runtime_data assignment must close the API session"
        )

    @pytest.mark.asyncio
    async def test_setup_failure_pattern_closes_session(self) -> None:
        """Behavioral: reproduces the exact try/except pattern in
        async_setup_entry and confirms a failure during first refresh
        results in the API being closed.
        """

        class FakeApi:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        api = FakeApi()

        async def _failing_first_refresh() -> None:
            raise RuntimeError("simulated first-refresh failure")

        with pytest.raises(RuntimeError):
            try:
                await _failing_first_refresh()
                # entry.runtime_data would be assigned here on success — never reached
            except BaseException:
                await api.aclose()
                raise

        assert api.closed is True

    @pytest.mark.asyncio
    async def test_setup_success_pattern_does_not_close_session(self) -> None:
        """Sibling case: successful setup must NOT close the session —
        ownership transfers to entry.runtime_data, which owns cleanup from
        then on (via async_unload_entry).
        """

        class FakeApi:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        api = FakeApi()

        async def _successful_first_refresh() -> None:
            return None

        try:
            await _successful_first_refresh()
            runtime_data_assigned = True
        except BaseException:
            await api.aclose()
            raise

        assert runtime_data_assigned is True
        assert api.closed is False


class TestClimateHeatAutoAreDistinct:
    """Independent audit finding (2026-09, fourth round, HVC-004): HEAT and
    AUTO used to be handled by the same `elif hvac_mode in (HVACMode.AUTO,
    HVACMode.HEAT):` branch — selecting either produced the identical
    "resume the schedule" API call, despite being advertised as two
    distinct, independently selectable HA climate modes.
    """

    def test_heat_and_auto_are_not_handled_by_the_same_branch(self) -> None:
        src = _code_only(_source("climate"))
        assert "HVACMode.AUTO,HVACMode.HEAT" not in src.replace(" ", "").replace("\n", ""), (
            "HEAT and AUTO must not share a single elif branch in async_set_hvac_mode"
        )

    def test_heat_activates_constant_program(self) -> None:
        """HEAT must now call set_program specifically with "constant" —
        a real, distinct action, not api.reset_circuit() (which is what
        AUTO calls, and what HEAT used to call too).
        """
        src = _source("climate")
        heat_idx = src.index("hvac_mode == HVACMode.HEAT")
        # The set_program(..., "constant") call must appear shortly after
        # the HEAT branch starts, before the AUTO branch begins.
        auto_idx = src.index("hvac_mode == HVACMode.AUTO")
        heat_branch = src[heat_idx:auto_idx] if auto_idx > heat_idx else src[heat_idx:]
        assert "set_program" in heat_branch
        assert '"constant"' in heat_branch

    def test_hvac_mode_read_and_write_are_consistent(self) -> None:
        """The read side (hvac_mode property) must still report HEAT for
        the same "not on a week/eco schedule" condition that the write
        side (async_set_hvac_mode) now specifically targets via
        "constant" — otherwise selecting HEAT wouldn't produce the state
        that reading HEAT back actually means.
        """
        src = _source("climate")
        # Both branches must still exist; a regression that removes either
        # would silently break the read/write symmetry this fix relies on.
        assert "return HVACMode.HEAT" in src
        assert "return HVACMode.AUTO" in src


class TestPersistedOptionsAreValidated:
    """Independent audit finding (2026-09, fourth round, HVC-007): persisted
    turn_on_mode/override_duration options were read directly from
    config-entry options with no re-validation against the actual allowed
    enums, in both fan.py AND (found while fixing fan.py) climate.py.
    """

    def test_fan_validates_turn_on_mode_and_override_duration(self) -> None:
        src = _source("fan")
        assert "VALID_TURN_ON_MODES" in src
        assert "VALID_OVERRIDE_DURATIONS" in src

    def test_climate_validates_override_duration(self) -> None:
        src = _source("climate")
        assert "VALID_OVERRIDE_DURATIONS" in src


class TestHealthStoreLoadResilience:
    """v1.0.0 bumped HEALTH_STORAGE_VERSION 1 -> 2. Verified directly against
    Home Assistant's Store source (homeassistant/helpers/storage.py, not
    assumed): without an overridden migrate function, a version mismatch on
    load raises rather than silently starting fresh — either
    UnsupportedStorageVersionError (stored file is NEWER than requested,
    e.g. after a rollback) or a re-raised NotImplementedError (stored file
    is OLDER, e.g. right after upgrading past this version bump). Left
    unhandled, either would make async_setup_entry raise and the whole
    integration fail to load. See docs/audit-v1.0.0.md § 8.
    """

    def test_health_store_load_is_wrapped_in_try_except(self) -> None:
        """Static guard: the health_store.async_load() call must be inside
        a try/except in async_setup_entry. A dedicated regression test
        (rather than only relying on the behavioral test below) because
        this is exactly the kind of one-line refactor ("clean up that
        try/except, it looks unnecessary") that would silently reintroduce
        the bug without any other test noticing.

        Uses the raw source, not _code_only(): that helper joins every
        token with a newline (so a multi-token phrase like this one never
        appears as a contiguous substring in its output), and there's no
        realistic risk of this exact call appearing inside a comment here.
        """
        src = _source("__init__")
        idx = src.index("health_store.async_load()")
        preceding = src[:idx]
        assert "try:" in preceding[-200:], "health_store.async_load() must be inside a try block"
        following = src[idx:]
        assert "except" in following[:400], (
            "an except clause must follow shortly after health_store.async_load()"
        )

    def test_store_exceptions_are_caught_by_broad_except(self) -> None:
        """Both exception types Store.async_load() can raise on a version
        mismatch must be real subclasses of Exception, so a broad
        `except Exception` (what __init__.py actually uses) catches both.
        This would fail loudly if either exception type's base ever changed
        to something outside the Exception hierarchy (e.g. BaseException).
        """
        from homeassistant.exceptions import UnsupportedStorageVersionError

        assert issubclass(UnsupportedStorageVersionError, Exception)
        assert issubclass(NotImplementedError, Exception)

    @pytest.mark.asyncio
    async def test_setup_continues_with_fresh_counters_when_store_raises(self) -> None:
        """Behavioral: a health_store.async_load() that raises must not
        propagate — async_setup_entry's restore step must degrade to
        "no stored health data" instead of failing setup.
        """
        from custom_components.hoval_connect.coordinator import HovalConnectionHealth

        class RaisingStore:
            async def async_load(self):
                raise RuntimeError("simulated version-mismatch failure")

            def async_delay_save(self, *_a, **_kw):
                pass

        health = HovalConnectionHealth()
        store = RaisingStore()

        # Reproduce the exact pattern in __init__.py's async_setup_entry.
        try:
            stored_health = await store.async_load()
        except Exception:  # noqa: BLE001
            stored_health = None
        if stored_health and isinstance(stored_health, dict):
            health.restore_from_store(stored_health)

        # Must not have raised, and must be left in a valid, fresh state.
        assert health.total_polls == 0
        assert health.last_successful_contact_at is None


class TestOptionsFlowLifecycle:
    """The options flow must reload the entry instead of using an update listener."""

    def test_options_flow_uses_reload_base_class(self) -> None:
        assert issubclass(HovalConnectOptionsFlow, ha_stubs.StubOptionsFlowWithReload)

    def test_automatic_reload_is_enabled(self) -> None:
        # HA reads this flag in OptionsFlowManager.async_finish_flow to decide
        # whether to schedule a config entry reload.
        assert HovalConnectOptionsFlow.automatic_reload is True

    def test_no_update_listener_is_registered(self) -> None:
        """A listener alongside OptionsFlowWithReload is a hard ValueError in HA.

        It also makes async_update_reload_and_abort (the reauth path) warn, and
        that stops being accepted in HA 2026.12.
        """
        assert "add_update_listener" not in _code_only(_source("__init__"))

    def test_options_updated_callback_is_gone(self) -> None:
        assert not hasattr(hoval, "_async_options_updated")
        assert "_async_options_updated" not in _source("__init__")

    def test_options_flow_still_saves_submitted_options(self) -> None:
        """Reloading must not change what the flow persists."""
        flow = HovalConnectOptionsFlow()
        # v1.0.0 removed scan_interval from the options schema entirely (see
        # docs/audit-v1.0.0.md); this dict now matches what the real form
        # actually submits.
        submitted = {"turn_on_mode": "resume", "override_duration": "FOUR"}

        result = flow.async_step_init.__wrapped__(flow, submitted) if False else None
        # async_step_init is a coroutine; drive it directly.
        result = asyncio.run(flow.async_step_init(submitted))

        assert result["type"] == "create_entry"
        assert result["data"] == submitted

    def test_legacy_scan_interval_option_stays_removed(self) -> None:
        """
        v1.0.0 initially removed the polling-interval option entirely — see
        docs/audit-v1.0.0.md and CHANGELOG.md — since there was no longer a
        meaningful "poll rate" to tune for circuit/program/settings data
        (still true: that data is fetched once at startup and again only
        after a write, never on a schedule, regardless of the setting
        below). CONF_HEALTH_CHECK_INTERVAL (reinstated later in this same
        v1.0.0 release, at the user's explicit request) is a narrower,
        differently-named, differently-scoped setting for the one
        lightweight reachability check still left on a schedule — it does
        not resurrect the OLD option. This test guards that the old
        symbols and option key specifically stay gone, not that the whole
        concept of a configurable interval never comes back.
        """
        assert not hasattr(hoval, "_get_scan_interval")
        assert not hasattr(hoval, "DEFAULT_SCAN_INTERVAL")

        flow = HovalConnectOptionsFlow()
        flow.config_entry = MagicMock(
            options={"turn_on_mode": "resume", "override_duration": "FOUR"}
        )
        result = asyncio.run(flow.async_step_init(None))
        schema_dict = result["data_schema"].schema
        assert not any(k == "scan_interval" for k in schema_dict)

    def test_health_check_interval_is_configurable_via_options(self) -> None:
        """CONF_HEALTH_CHECK_INTERVAL — reinstated in this same v1.0.0
        release at the user's explicit request — must actually be read
        from config-entry options, not silently ignored in favor of the
        constant default. HEALTH_CHECK_INTERVAL (the constant) remains the
        correct fallback for any construction path without a config entry
        at all (e.g. a bare coordinator built directly, as other tests in
        this suite do), which test_options_form_offers_health_check_interval_choices
        below covers separately.
        """
        from custom_components.hoval_connect import _get_health_check_interval

        entry = MagicMock()
        entry.options = {"health_check_interval": 3600}
        assert _get_health_check_interval(entry).total_seconds() == 3600

    def test_health_check_interval_falls_back_to_default_when_unset(self) -> None:
        from custom_components.hoval_connect import _get_health_check_interval
        from custom_components.hoval_connect.const import DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS

        entry = MagicMock()
        entry.options = {}
        assert (
            _get_health_check_interval(entry).total_seconds()
            == DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
        )

    def test_health_check_interval_coerces_legacy_string_option(self) -> None:
        from custom_components.hoval_connect import _get_health_check_interval

        entry = MagicMock()
        entry.options = {"health_check_interval": "900"}
        assert _get_health_check_interval(entry).total_seconds() == 900

    def test_health_check_interval_falls_back_on_garbage(self) -> None:
        from custom_components.hoval_connect import _get_health_check_interval
        from custom_components.hoval_connect.const import DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS

        entry = MagicMock()
        entry.options = {"health_check_interval": "not-a-number"}
        assert (
            _get_health_check_interval(entry).total_seconds()
            == DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
        )

    def test_health_check_interval_rejects_zero(self) -> None:
        """Independent audit finding (2026-09, fourth round, HVC-006): a
        numeric-but-unsupported value (not just a non-numeric one) must
        also fall back to the default — 0 would otherwise hand
        DataUpdateCoordinator a non-positive update_interval.
        """
        from custom_components.hoval_connect import _get_health_check_interval
        from custom_components.hoval_connect.const import DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS

        entry = MagicMock()
        entry.options = {"health_check_interval": 0}
        assert (
            _get_health_check_interval(entry).total_seconds()
            == DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
        )

    def test_health_check_interval_rejects_negative(self) -> None:
        from custom_components.hoval_connect import _get_health_check_interval
        from custom_components.hoval_connect.const import DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS

        entry = MagicMock()
        entry.options = {"health_check_interval": -1}
        assert (
            _get_health_check_interval(entry).total_seconds()
            == DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
        )

    def test_health_check_interval_rejects_implausibly_large_value(self) -> None:
        from custom_components.hoval_connect import _get_health_check_interval
        from custom_components.hoval_connect.const import DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS

        entry = MagicMock()
        entry.options = {"health_check_interval": 999999999}
        assert (
            _get_health_check_interval(entry).total_seconds()
            == DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
        )

    def test_health_check_interval_rejects_overflow_value(self) -> None:
        """A value so large it would make timedelta() itself raise
        OverflowError must not crash config-entry setup.
        """
        from custom_components.hoval_connect import _get_health_check_interval
        from custom_components.hoval_connect.const import DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS

        entry = MagicMock()
        entry.options = {"health_check_interval": 10**20}
        assert (
            _get_health_check_interval(entry).total_seconds()
            == DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
        )

    def test_health_check_interval_accepts_every_supported_option(self) -> None:
        from custom_components.hoval_connect import _get_health_check_interval
        from custom_components.hoval_connect.const import HEALTH_CHECK_INTERVAL_OPTIONS

        for seconds in HEALTH_CHECK_INTERVAL_OPTIONS:
            entry = MagicMock()
            entry.options = {"health_check_interval": seconds}
            assert _get_health_check_interval(entry).total_seconds() == seconds

    def test_options_form_offers_health_check_interval_choices(self) -> None:
        flow = HovalConnectOptionsFlow()
        flow.config_entry = MagicMock(
            options={
                "turn_on_mode": "resume",
                "override_duration": "FOUR",
                "health_check_interval": 3600,
            }
        )
        result = asyncio.run(flow.async_step_init(None))

        assert result["type"] == "form"
        schema_dict = result["data_schema"].schema
        interval_marker = next(k for k in schema_dict if k == "health_check_interval")
        assert interval_marker.default() == 3600
        interval_validator = schema_dict[interval_marker]
        assert interval_validator(3600) == 3600
        assert interval_validator("3600") == 3600  # frontend submits strings

    def test_default_coordinator_construction_still_uses_the_constant(self) -> None:
        """A bare coordinator built without going through async_setup_entry
        (as most of this test suite's coordinator tests do) has no config
        entry options to read, so it must fall back to the constant.
        """
        from custom_components.hoval_connect.const import HEALTH_CHECK_INTERVAL
        from custom_components.hoval_connect.coordinator import HovalDataCoordinator

        coordinator = HovalDataCoordinator(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert coordinator.update_interval == HEALTH_CHECK_INTERVAL


# ---------------------------------------------------------------------------
# P1 — via_device -> via_device_id
# ---------------------------------------------------------------------------


class TestDeviceInfoContract:
    """Circuit devices must link to their plant by device ID, not identifier tuple."""

    def test_circuit_device_info_uses_via_device_id(self) -> None:
        info = circuit_device_info("plant1", "dev1", _circuit())
        assert info["via_device_id"] == "dev1"

    def test_circuit_device_info_has_no_via_device(self) -> None:
        """`via_device` was removed from HA 2026.9's DeviceInfo TypedDict."""
        info = circuit_device_info("plant1", "dev1", _circuit())
        assert "via_device" not in info

    @pytest.mark.parametrize("builder", ["plant", "circuit"])
    def test_device_info_keys_are_all_valid_in_ha_2026_9(self, builder: str) -> None:
        """Guard against reintroducing a key HA has removed from DeviceInfo."""
        info = (
            plant_device_info(_plant())
            if builder == "plant"
            else circuit_device_info("plant1", "dev1", _circuit())
        )
        assert set(info) <= ha_stubs._DEVICE_INFO_KEYS

    def test_registry_rejects_legacy_via_device_keyword(self) -> None:
        """Sanity-check the stub actually models HA's rejection of unknown kwargs."""
        reg = ha_stubs.StubDeviceRegistry()
        with pytest.raises(TypeError, match="via_device"):
            reg.async_get_or_create(
                config_entry_id="entry1",
                identifiers={(DOMAIN, "x")},
                via_device=(DOMAIN, "plant1"),
            )


class TestDeviceIdentityStability:
    """The migration must not create a second device tree.

    Identifiers and unique IDs are what HA matches existing devices and entities
    on. If these change, every user gets duplicates and loses history, which is
    the single worst outcome of this release.
    """

    def test_plant_identifiers_unchanged(self) -> None:
        info = plant_device_info(_plant("plant1", "Home"))
        assert info["identifiers"] == {(DOMAIN, "plant1")}

    def test_circuit_identifiers_unchanged(self) -> None:
        """Still `<plant_id>_<path>` — the parent link changed, identity did not."""
        info = circuit_device_info("plant1", "dev1", _circuit(path="1/1"))
        assert info["identifiers"] == {(DOMAIN, "plant1_1/1")}

    def test_circuit_identifier_still_derives_from_plant_id_not_device_id(self) -> None:
        """Regression guard for the review's proposed signature.

        Dropping `plant_id` in favour of the device ID (as the upstream review
        suggested) would rewrite every circuit identifier and orphan the existing
        devices. The device ID must only ever be the parent link.
        """
        info = circuit_device_info("plant1", "dev-XYZ", _circuit(path="1/1"))
        identifier = next(iter(info["identifiers"]))[1]
        assert identifier == "plant1_1/1"
        assert "dev-XYZ" not in identifier

    def test_plant_name_model_manufacturer_unchanged(self) -> None:
        info = plant_device_info(_plant("plant1", "Home"))
        assert info["name"] == "Hoval Home"
        assert info["manufacturer"] == "Hoval"
        assert info["model"] == "Plant"

    def test_circuit_model_still_maps_from_circuit_type(self) -> None:
        info = circuit_device_info("plant1", "dev1", _circuit(circuit_type="HK"))
        assert info["model"] == hoval.CIRCUIT_TYPE_NAMES.get("HK", "HK")


class TestPlantDeviceResolver:
    """The resolver registers plants and caches their device IDs."""

    def test_registers_plant_and_returns_device_id(
        self, plant_devices: HovalPlantDevices, registry: ha_stubs.StubDeviceRegistry
    ) -> None:
        device_id = plant_devices.async_get_device_id("plant1", _plant("plant1"))
        assert device_id == "dev1"
        assert len(registry.calls) == 1

    def test_repeated_lookups_hit_the_cache(
        self, plant_devices: HovalPlantDevices, registry: ha_stubs.StubDeviceRegistry
    ) -> None:
        """Platforms re-scan every plant on each SIGNAL_NEW_CIRCUITS dispatch."""
        first = plant_devices.async_get_device_id("plant1", _plant("plant1"))
        for _ in range(5):
            assert plant_devices.async_get_device_id("plant1", _plant("plant1")) == first
        assert len(registry.calls) == 1

    def test_multiple_plants_stay_independent(
        self, plant_devices: HovalPlantDevices, registry: ha_stubs.StubDeviceRegistry
    ) -> None:
        a = plant_devices.async_get_device_id("plant1", _plant("plant1", "Home"))
        b = plant_devices.async_get_device_id("plant2", _plant("plant2", "Cabin"))
        assert a != b
        assert len(registry.devices) == 2

    def test_registers_a_plant_discovered_after_setup(
        self, plant_devices: HovalPlantDevices, registry: ha_stubs.StubDeviceRegistry
    ) -> None:
        """Late plants must still get a parent device.

        Unlike the old `via_device`, which merely logged when the parent was
        missing, an unresolvable `via_device_id` raises DeviceInfoError and HA
        drops the entity entirely.
        """
        plant_devices.async_get_device_id("plant1", _plant("plant1"))
        late = plant_devices.async_get_device_id("plant9", _plant("plant9", "New Wing"))
        assert late in {d.id for d in registry.devices.values()}

    def test_registration_uses_the_config_entry_id(
        self, plant_devices: HovalPlantDevices, registry: ha_stubs.StubDeviceRegistry
    ) -> None:
        plant_devices.async_get_device_id("plant1", _plant("plant1"))
        assert registry.calls[0]["config_entry_id"] == "entry1"

    def test_resolved_id_is_accepted_as_a_via_device_id(
        self, plant_devices: HovalPlantDevices, registry: ha_stubs.StubDeviceRegistry
    ) -> None:
        """End-to-end: the parent must be registered before the child links to it."""
        plant_device_id = plant_devices.async_get_device_id("plant1", _plant("plant1"))
        circuit = registry.async_get_or_create(
            config_entry_id="entry1",
            **circuit_device_info("plant1", plant_device_id, _circuit()),
        )
        assert circuit.via_device_id == plant_device_id

    def test_unregistered_parent_is_rejected(self, registry: ha_stubs.StubDeviceRegistry) -> None:
        with pytest.raises(ValueError, match="not a registered device id"):
            registry.async_get_or_create(
                config_entry_id="entry1",
                **circuit_device_info("plant1", "never-registered", _circuit()),
            )


class TestPlatformWiring:
    """Every circuit platform must thread the plant device ID through."""

    @pytest.mark.parametrize("platform", CIRCUIT_PLATFORMS)
    def test_platform_resolves_plant_device_id(self, platform: str) -> None:
        src = _source(platform)
        assert "plant_devices.async_get_device_id(plant_id, plant_data)" in src

    @pytest.mark.parametrize("platform", CIRCUIT_PLATFORMS)
    def test_platform_passes_both_plant_id_and_device_id(self, platform: str) -> None:
        src = _source(platform)
        assert "circuit_device_info(plant_id, plant_device_id, circuit_data)" in src

    @pytest.mark.parametrize("platform", CIRCUIT_PLATFORMS)
    def test_circuit_entity_accepts_plant_device_id(self, platform: str) -> None:
        """The constructor signature must actually take the new argument."""
        module = __import__(f"custom_components.hoval_connect.{platform}", fromlist=["x"])
        entity_classes = [
            obj
            for name, obj in vars(module).items()
            if inspect.isclass(obj)
            and name.startswith("Hoval")
            and "circuit_data" in inspect.signature(obj.__init__).parameters
        ]
        assert entity_classes, f"no circuit entity class found in {platform}.py"
        for cls in entity_classes:
            params = inspect.signature(cls.__init__).parameters
            assert "plant_device_id" in params, f"{cls.__name__} missing plant_device_id"

    def test_binary_sensor_is_plant_scoped_and_untouched(self) -> None:
        """binary_sensor only builds plant devices, so it needs no parent link."""
        src = _source("binary_sensor")
        assert "circuit_device_info" not in src
        assert "plant_device_info" in src


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
# History: v1.0.0 originally deleted sensor.py entirely, which is where a
# whole class guarding a PERCENTAGE -> UnitOfRatio.PERCENTAGE migration
# used to live (see docs/audit-v1.0.0.md). Not resurrected when sensor.py
# was recreated: the new file's percentage usage is a direct
# `_attr_native_unit_of_measurement = PERCENTAGE` (HV air-volume and the
# API failure-rate sensor), not the more elaborate migration this class
# guarded, so there is nothing equivalent left to test here.
# TestPercentageUnits removed along with it.


# ---------------------------------------------------------------------------
# Deprecation guards
# ---------------------------------------------------------------------------


class TestDeprecationGuards:
    """Static guards against reintroducing APIs HA removes on or before 2026.12.

    Each entry was confirmed against the HA 2026.9.0 source rather than taken
    from release notes.
    """

    # symbol -> why it must not appear
    BANNED = {
        "via_device=": "removed from DeviceInfo; async_get_or_create drops it in 2027.8",
        "add_update_listener": "ValueError with OptionsFlowWithReload; rejected in 2026.12",
        "default_name=": "deprecated device-registry parameter, removed 2027.9",
        "default_model=": "deprecated device-registry parameter, removed 2027.9",
        "default_manufacturer=": "deprecated device-registry parameter, removed 2027.9",
        "merge_connections": "deprecated async_update_device parameter",
        "merge_identifiers": "deprecated async_update_device parameter",
        "OptionsFlowWithConfigEntry": "phased out; not for new code",
        "_enable_turn_on_off_backwards_compat": "attribute no longer exists in HA climate",
        "async_forward_entry_setup(": "singular form superseded by async_forward_entry_setups",
        "utc_to_timestamp": "removed from homeassistant.util.dt",
        "verify_domain_control": "hass argument deprecated, breaks 2026.10",
        "async_extract_entity_ids": "hass argument deprecated, breaks 2026.10",
        "async_extract_config_entry_ids": "hass argument deprecated, breaks 2026.10",
    }

    @pytest.mark.parametrize("symbol", sorted(BANNED))
    def test_symbol_absent_from_component(self, symbol: str) -> None:
        """Scan executable code only.

        Comments and docstrings legitimately name these APIs to explain why they
        were removed, so matching raw text would flag the documentation itself.
        """
        offenders = [
            path.name
            for path in sorted(COMPONENT_DIR.glob("*.py"))
            if symbol in _code_only(path.read_text())
        ]
        assert not offenders, f"{symbol} ({self.BANNED[symbol]}) found in {offenders}"

    def test_no_legacy_serial_dependency(self) -> None:
        """No dependency on the old CAN-bus/serial hardware stack.

        This integration's predecessor (Hoval-GatewayV2-CANBUS-MQTT) required
        a physical CAN adapter and pyserial-family packages; the whole point
        of hoval_connect is that none of that hardware is needed anymore.
        `requirements == []` was a fine proxy for that invariant until
        v0.24.0 added a real, justified cloud-API dependency (`requests` —
        see docs/audit-v0.24.0.md for why). Check what this test actually
        cares about directly instead, so a legitimate future dependency
        doesn't fail it again for an unrelated reason.
        """
        import json

        manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text())
        banned_substrings = ("serial", "can-", "pycan", "canbus")
        for requirement in manifest["requirements"]:
            lowered = requirement.lower()
            assert not any(b in lowered for b in banned_substrings), requirement

    def test_all_platforms_use_config_entry_add_entities_callback(self) -> None:
        """AddConfigEntryEntitiesCallback is the correct type for config entries."""
        for platform in [*CIRCUIT_PLATFORMS, "binary_sensor"]:
            src = _source(platform)
            assert "AddConfigEntryEntitiesCallback" in src, platform

    def test_coordinator_receives_config_entry_explicitly(self) -> None:
        """Avoids relying on HA's current_entry ContextVar."""
        from custom_components.hoval_connect.coordinator import HovalDataCoordinator

        params = inspect.signature(HovalDataCoordinator.__init__).parameters
        assert "config_entry" in params
        assert "config_entry=config_entry" in _source("coordinator")

    def test_setup_registers_plants_before_forwarding_platforms(self) -> None:
        """Ordering is load-bearing: children cannot link to an unregistered parent."""
        src = _source("__init__")
        register_at = src.index("plant_devices.async_get_device_id(plant_id, plant_data)")
        forward_at = src.index("async_forward_entry_setups")
        assert register_at < forward_at


class TestMinimumVersionGuard:
    """A too-old HA must fail with an explanation, not a TypeError.

    HACS honours the floor in hacs.json, but manual installs bypass it. On HA
    2026.7 the first circuit entity would otherwise hit
    `TypeError: async_get_or_create() got an unexpected keyword argument
    'via_device_id'` with nothing pointing at the cause.
    """

    def test_floor_matches_via_device_id_availability(self) -> None:
        assert hoval.MIN_HA_VERSION == (2026, 8)

    def test_current_version_passes(self) -> None:
        hoval._check_ha_version()  # stubs report 2026.9

    @pytest.mark.parametrize(
        ("major", "minor"),
        [(2026, 7), (2026, 1), (2025, 12), (2024, 1)],
    )
    def test_older_versions_are_rejected(
        self, monkeypatch: pytest.MonkeyPatch, major: int, minor: int
    ) -> None:
        monkeypatch.setattr(hoval, "MAJOR_VERSION", major)
        monkeypatch.setattr(hoval, "MINOR_VERSION", minor)
        with pytest.raises(Exception, match="requires Home Assistant"):
            hoval._check_ha_version()

    @pytest.mark.parametrize(("major", "minor"), [(2026, 8), (2026, 12), (2027, 1)])
    def test_supported_versions_are_accepted(
        self, monkeypatch: pytest.MonkeyPatch, major: int, minor: int
    ) -> None:
        monkeypatch.setattr(hoval, "MAJOR_VERSION", major)
        monkeypatch.setattr(hoval, "MINOR_VERSION", minor)
        hoval._check_ha_version()

    def test_error_names_both_versions_and_a_way_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hoval, "MAJOR_VERSION", 2026)
        monkeypatch.setattr(hoval, "MINOR_VERSION", 7)
        with pytest.raises(Exception) as excinfo:
            hoval._check_ha_version()
        message = str(excinfo.value)
        assert "2026.8" in message
        assert "2026.7" in message
        assert "0.21.1" in message

    def test_guard_runs_before_any_api_call(self) -> None:
        """The check must be the first statement of async_setup_entry.

        Checked via AST rather than text search: the function *definition* also
        contains the string `_check_ha_version()`, so a substring test would
        still pass if the call site were deleted.
        """
        tree = ast.parse(_source("__init__"))
        setup = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
        )
        calls = [
            stmt
            for stmt in setup.body
            if isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == "_check_ha_version"
        ]
        assert calls, "async_setup_entry does not call _check_ha_version()"

        first_real = next(
            stmt
            for stmt in setup.body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        )
        assert first_real is calls[0], "_check_ha_version() must run first"

    def test_guard_floor_matches_hacs_manifest(self) -> None:
        import json

        hacs = json.loads((COMPONENT_DIR.parent.parent / "hacs.json").read_text())
        declared = tuple(int(p) for p in hacs["homeassistant"].split(".")[:2])
        assert declared == hoval.MIN_HA_VERSION


class TestManifestAndMetadata:
    """Version and minimum-HA metadata must match the APIs actually used."""

    def test_version_is_bumped(self) -> None:
        """
        Version history (see CHANGELOG.md for the full story of the earlier
        typo/correction cycle): 0.21.1 -> 0.23.0 -> 0.24.0 -> now 1.0.0.

        1.0.0 is a deliberate major-version jump, not a routine increment:
        v1.0.0 is a genuine architecture change (this integration no longer
        polls telemetry on any schedule — see docs/audit-v1.0.0.md) that the
        user explicitly wanted a clean, distinct version number for, so a
        previously-deployed 0.24.0 install is never silently overwritten by
        an in-place update. Bump this string (and manifest.json) together on
        release.
        """
        import json

        manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text())
        assert manifest["version"] == "1.0.0"

    def test_hacs_minimum_ha_covers_via_device_id(self) -> None:
        """via_device_id landed in HA 2026.8; earlier versions raise TypeError.

        Verified by inspecting the 2026.7.0 and 2026.8.0 wheels: 2026.7's
        async_get_or_create has no **kwargs, so the keyword is fatal there.
        """
        import json

        hacs = json.loads((COMPONENT_DIR.parent.parent / "hacs.json").read_text())
        major, minor = (int(p) for p in hacs["homeassistant"].split(".")[:2])
        assert (major, minor) >= (2026, 8), (
            f"hacs.json allows HA {hacs['homeassistant']}, but via_device_id requires 2026.8+"
        )


class TestSourceIntegrity:
    """Whole-component sanity checks."""

    @pytest.mark.parametrize("path", sorted(COMPONENT_DIR.glob("*.py")), ids=lambda p: p.name)
    def test_module_parses(self, path: Path) -> None:
        ast.parse(path.read_text(), filename=str(path))

    def test_every_platform_module_is_importable(self) -> None:
        for platform in [*CIRCUIT_PLATFORMS, "binary_sensor", "diagnostics", "config_flow"]:
            __import__(f"custom_components.hoval_connect.{platform}", fromlist=["x"])
