# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Reverse-engineered API documentation and **Home Assistant custom integration** for the Hoval Connect IoT platform. Hoval Connect is a cloud platform connecting Hoval HVAC systems (heating, ventilation, hot water) via IoT gateways to Azure IoT Hub.

## Repository Structure

- `README.md` — API documentation + HA integration install instructions
- `examples/` — Standalone Python and Bash API client examples
- `custom_components/hoval_connect/` — Home Assistant integration (HACS-compatible)
- `docs/openapi-v3.json` — Full OpenAPI 3.1 spec (~450KB, fetched from `/v3/api-docs`)
- `tests/` — Unit tests (pure function tests, run without HA installed)
- `hacs.json` — HACS repository metadata
- `.github/workflows/` — CI: HACS/Hassfest validation, Ruff linting, automated releases on tags
- `pyproject.toml` — Ruff linter config (Python 3.12+, 100-char lines)

## Home Assistant Integration

The integration lives in `custom_components/hoval_connect/`. User setup is email + password only — plants and circuits are discovered automatically from the Hoval account at runtime.

### Key files

- `api.py` — Async aiohttp client: 2-step auth, auto-refresh, token retry on 401, handles 204 and empty-body (content_length==0) responses
- `coordinator.py` — DataUpdateCoordinator: parallel fetch of circuits/events/weather, offline plant skip, program cache (5min TTL), `control_lock`, `_V1_PROGRAM_MAP`, `SIGNAL_NEW_CIRCUITS`
- `config_flow.py` — Config + reauth + options flow (turn-on mode, override duration, polling interval)
- `climate.py` — HK heating: target temp, HVAC modes (heat/auto/off)
- `fan.py` — HV ventilation: speed slider 0–100%, on/off (standby ↔ temporary-change), debounced 1.5s
- `select.py` — Program selection (week1/week2/ecoMode/standby/constant) with user-defined names; applies to HV, HK, **and WW** circuits
- `sensor.py` — Circuit-type-filtered sensors (HV/HK/BL/WW) + 6 plant-level sensors (events, weather); includes `circuit_status` diagnostic sensor for BL, HK, and WW (sourced from `HovalCircuitData.circuit_status`, populated from `CircuitV3DTO.circuitStatus` in the circuit list response)
- `water_heater.py` — WW hot water entity (`WaterHeaterEntity`); exposes current/target temperature and operation modes heat_pump / high_demand / off; `set_temperature` posts a `midnight`-duration temporary-change override; `set_operation_mode` switches between week-program and standby; registers the `reset_ww_boost` entity service via `async_get_current_platform()` → `async_register_entity_service`, which calls `async_reset_temporary_change` (DELETEs the temporary-change endpoint without touching the week program)
- `binary_sensor.py` — Plant online status + error/warning status
- `diagnostics.py` — Diagnostic export with PII redaction
- `const.py` — API URLs, OAuth client ID, token TTLs, polling interval, circuit types, duration enums, `SERVICE_RESET_WW_BOOST`
- `__init__.py` — Entry setup, platform forwarding, `plant_device_info`/`circuit_device_info` helpers

### Entity architecture

- Entities use `CoordinatorEntity` — no direct API calls, all data comes from the coordinator
- Device hierarchy: one parent device per plant, one child device per plant+circuit (linked via `via_device`)
- Circuit devices identified by `{plantId}_{circuitPath}`
- Supports HV (ventilation), HK (heating), BL (boiler), and WW (warm water) circuit types (`SUPPORTED_CIRCUIT_TYPES` in `const.py`)
- Sensor descriptions use `circuit_types: frozenset[str] | None` to filter which sensors appear on which circuit types (`None` = all types)
- Fan speed resolution uses smart fallback chain: live airVolume → `targetValue` (HV percentage from circuit list) → program air volume → default 40% (API rejects value=0)
- All entity platforms use `translation_key` for entity names (not hardcoded `_attr_name`)
- Dynamic entity discovery: all platforms listen to `SIGNAL_NEW_CIRCUITS` dispatcher signal to add entities at runtime without restart. The coordinator must dispatch this signal whenever `_known_circuits` grows — *including* the first time circuits appear. Earlier the coordinator gated the dispatch on `if self._known_circuits and new_circuits`, which silently stranded all circuit-level entities if the very first refresh after `async_setup_entry` came back without circuits (e.g. transient `_fetch_circuit` failure swallowed by `gather(return_exceptions=True)`); they stayed `restored=true`/`unavailable` until HA was restarted. Each platform's `_add_new()` already deduplicates via its `known` set, so unconditional dispatch on any new circuit is safe.

## Running Tests

```bash
python -m pytest tests/ -v
```

## Linting

```bash
ruff check custom_components/ tests/
ruff format --check custom_components/ tests/
```

## Running Examples

```bash
python examples/hoval_client.py <email> <password>
./examples/get-live-values.sh <email> <password> <plantId> <circuitPath> <circuitType>
```

## Live Testing & Release Workflow

- `homeassistant.reload_config_entry` does NOT re-import Python modules — `custom_components/hoval_connect/` code changes only take effect after a full HA core restart (`POST http://supervisor/core/restart`). Clear `__pycache__/` first.
- HA core logs on HAOS are not in `/config/home-assistant.log` (that file usually doesn't exist). Fetch via `GET http://supervisor/core/logs?tail=N` with `Authorization: Bearer <SUPERVISOR_TOKEN>`. The token isn't exposed in the SSH addon's shell env but is in another addon process: `sudo sh -c 'for p in /proc/[0-9]*/environ; do tr "\0" "\n" <$p 2>/dev/null | grep -m1 SUPERVISOR_TOKEN; done | head -1'`.
- Release CI (`.github/workflows/release.yml`) triggers on `v*` tag pushes only. Bumping `manifest.json` does nothing on its own — also `git tag vX.Y.Z && git push origin vX.Y.Z`.
- Live API probes: the SSH addon's `python3` is stdlib-only, but `urllib.request` is enough for the OAuth + Plant-Access-Token + JSON flow. Write the probe locally, `pscp` it to `/tmp/`, run via plink.

## Authentication Architecture (2-step)

1. **ID Token**: OAuth2 password grant to SAP IAS. Use `id_token` from response, NOT `access_token`. Lifetime: 30min.
2. **Plant Access Token (PAT)**: Fetch via `GET /v1/plants/{plantId}/settings`. Send as `X-Plant-Access-Token` header. Lifetime: ~15min.

## API Base URL

`https://azure-iot-prod.hoval.com/core`

## Key Endpoint Patterns

- `/api/` endpoints need only the id_token (`Authorization: Bearer`)
- `/v1/plants/`, `/v2/api/`, `/v3/` endpoints also require `X-Plant-Access-Token`
- `/business/` endpoints require elevated (partner) access — regular users get 403

## Circuit Types

HK (heating), BL (boiler), WW (warm water), FRIWA (fresh water), HV (ventilation), SOL (solar), SOLB (solar buffer), PS (pool), GW (gateway)

## API Behavior Notes

- Control endpoints return HTTP 204 No Content on success — no response body
- Some GET endpoints (e.g. `/v1/plant-events/latest/`) return HTTP 200 with Content-Length: 0 (empty body) instead of 204 or empty JSON when no data exists — `_request` handles this via `content_length == 0` check
- **Around 2026-04-21 Hoval removed every `/v1/plants/{id}/circuits/...` endpoint** (list, mode setters, `temporary-change`, `reset`). The integration uses `/v3/plants/{id}/circuits` everywhere now. The cloud responds to v1 paths with HTTP 404 `{"detail":"No static resource ..."}`. Restoring those paths is not expected.
- `temporary-change` (v3): `POST /v3/.../temporary-change` with JSON body `{"value": <float>, "duration": "fourHours"|"midnight"}`. The HV value is a percentage; the HK value is degrees Celsius (no tenths). Stored option values `FOUR`/`MIDNIGHT` from older configs are translated to the v3 camelCase form inside `set_temporary_change`.
- `temporary-change/reset` (v3): `DELETE /v3/.../temporary-change` (no body). Replaces the removed v1 POST `/temporary-change/reset`.
- Mode endpoints `/v1/.../{standby|manual|constant|reset|cooling|time-programs}` are gone. Use `POST /v3/.../programs/{program}` where program ∈ {`constant`,`ecoMode`,`standby`,`week1`,`week2`,`manual`,`externalConstant`}.
- v1 had a separate `/reset` endpoint that auto-resumed the configured time program. v3 has no such auto-pick — `reset_circuit()` defaults to `week1`; pass `program="week2"` for the second weekly schedule.
- API always reports `operationMode='REGULAR'` regardless of actual device state — optimistic override needed for standby tracking
- v1 `activeProgram` enum (legacy, only relevant if Hoval rolls back): `constant`, `nightReduction`, `dayCooling`, `timePrograms`, `standby`, `manual`, `externalConstant`, `tteControlled`
- v3 `activeProgram` enum: `constant`, `ecoMode`, `standby`, `week1`, `week2`, `manual`, `externalConstant`
- v3 circuit list field renames vs the old v1 shape: `targetAirVolume` → `targetValue` (now `float`, percentage for HV / degrees for HK), `isAirQualityGuided` is now nested under `airQuality.isAirQualityGuided`, `targetAirHumidity` is no longer in the list (humidity comes from `live-values`).
- Weather forecast available via `get_weather()` — returns condition + temperature
- `PlantEventDTO` fields: `eventType`, `description`, `timeOccurred`, `timeResolved`, `sourcePath`, `code`, `module`, `functionGroup`, `function`, `category` — event is active when `timeResolved` is null
- Event types: `locking`, `blocking`, `warning`, `info`, `offline`, `ok` — the error binary sensor triggers on active `blocking`, `locking`, or `warning` events

## HA Compatibility Notes

- `OptionsFlow.config_entry` is a **read-only property** in modern HA — do NOT assign it in `__init__`. The base class sets it automatically.
- `async_get_options_flow()` should return the flow instance without passing `config_entry`.

## Known Pitfalls

- `aiohttp.resp.json()` on empty body throws `ContentTypeError` (subclass of `ClientError`) — easily misidentified as connection error in generic exception handlers
- A coordinator refresh can return `success=True` while `plant_data.circuits` is empty — `_fetch_circuit` exceptions are captured per-circuit by `gather(return_exceptions=True)`, plant-level fetches still succeed. Anything keying off "did the coordinator refresh" rather than "did this specific circuit appear" can drift; the `SIGNAL_NEW_CIRCUITS` dispatcher pitfall above is one consequence.

## Known Gaps

- Temperature history (`/v3/api/statistics/temperature/`) requires `datapoints` param — valid IDs not yet discovered
- Energy stats return empty for HV circuit (likely only relevant for HK/WW/SOL)
- `business/plants/{id}/plant-structure` needs business role
- Full OpenAPI 3.1 spec saved at `docs/openapi-v3.json` (also available live at `/v3/api-docs`, no auth required)
- Non-supported circuit types (FRIWA, SOL, SOLB, PS) have endpoint support in the API but no HA entities yet
- HK climate entity: `set_temperature` sends value as integer — may need adjustment for different HK circuit models (some use tenths of degree)

## Changelog

### v0.15.9
Focus: API communication monitoring sensors + production-quality documentation pass.

#### New: API communication sensors
**New file `api_stats.py`** — ``HovalApiStats`` rolling-window statistics collector.
Maintains four monotonic-timestamp deques (calls, timeouts, errors, retries) as a
1-hour sliding window.  All rate metrics (calls/hour, timeouts/hour, etc.) are
automatically up-to-date without any external reset.  Lifetime counters (total_*)
accumulate for the full HA session.  UTC ``datetime`` objects are stored for the
two timestamp sensors so HA can render them directly without parsing.  An
``as_dict()`` method is used by the diagnostics platform.

**12 new diagnostic sensor entities** added to the plant device
(``entity_category=DIAGNOSTIC``, do not appear in the main dashboard by default):

| Entity key | What it shows |
|---|---|
| `api_calls_hour` | HTTP requests made in the last 60 minutes |
| `api_timeouts_hour` | Timed-out requests in the last 60 minutes |
| `api_errors_hour` | Terminal request failures in the last 60 minutes |
| `api_retries_hour` | Retry attempts triggered in the last 60 minutes |
| `api_failure_ratio` | Error rate as % of calls in the last 60 minutes |
| `api_total_calls` | All HTTP requests since HA started (lifetime) |
| `api_total_errors` | All terminal errors since HA started (lifetime) |
| `api_last_success` | UTC timestamp of last successful API response |
| `api_last_error` | UTC timestamp of last terminal error |
| `api_last_error_message` | Human-readable description of last error |
| `api_consecutive_failures` | Current coordinator consecutive-failure count |
| `api_poll_interval` | Current poll interval in seconds (reflects adaptive backoff) |

`api_poll_interval` and `api_consecutive_failures` read from the coordinator
directly (not from ``HovalApiStats``), making backoff transparent in the HA UI.

**`api.py`** — instrumented with ``stats.record_*()`` calls at every HTTP event:
``record_call()`` at the start of every outbound attempt (in ``_request``,
``_get_id_token``, ``_get_plant_access_token``); ``record_timeout()`` on every
``TimeoutError`` catch including mid-retry; ``record_retry()`` before each retry
sleep; ``record_error()`` on terminal failure; ``record_success()`` on a valid 2xx
response.  The stats object is stored as ``api.stats`` and is accessible via
``entry.runtime_data.stats``.

**`__init__.py`** — ``HovalApiStats()`` created in ``async_setup_entry`` and passed
to ``HovalConnectApi`` at construction time.  ``HovalRuntimeData`` extended with a
``stats: HovalApiStats`` field.  Full docstring added to ``HovalRuntimeData``.

**`sensor.py`** — new ``HovalCommsSensorEntityDescription`` dataclass (``value_fn``
accepts both stats and coordinator).  ``COMMS_SENSOR_DESCRIPTIONS`` tuple with all
12 entries.  ``HovalApiStatsSensor`` entity class — extends ``CoordinatorEntity``
for lifecycle management but reads from ``_stats`` in ``native_value``.
``async_setup_entry`` updated to create comms sensors per plant.

**`diagnostics.py`** — diagnostics payload extended with ``api_stats`` (from
``stats.as_dict()``) and ``coordinator_health`` (consecutive failures, current and
base poll intervals).  Module-level docstring added listing all payload sections.

#### Documentation pass (all files)
Every module, class, method, and non-obvious constant now has a Google-style
docstring or inline comment.  Key additions:
- ``api.py``: class-level docstring covering auth flow, resilience, and stats.
  Method docstrings for all public methods describing endpoint, auth requirements,
  and edge cases.  ``_request`` docstring describes the full retry flow and every
  stats recording point.
- ``coordinator.py``: ``_fetch_circuit_data`` and ``_async_update_data`` docstrings
  explain the timeout hierarchy and adaptive backoff.
- ``api_stats.py``: module docstring explaining the design (asyncio-safe, monotonic
  pruning, wall-clock timestamps).  Property docstrings for all computed attributes.
- ``__init__.py``: ``HovalRuntimeData`` dataclass fully documented.

### v0.15.8
Focus: timeout resilience and reduction of crash/noise during Hoval cloud instability.

- **Retry jitter** (`api.py`): All retry delays in `_request`, `_get_id_token`, and `_get_plant_access_token` now use `_jittered_delay(attempt)` — `base × 2^attempt × uniform(0.5, 1.0)` — rather than a fixed value. This prevents synchronised retry bursts (thundering-herd) when multiple circuit requests fail at the same moment. Extracted into a module-level helper `_jittered_delay()` for consistency and testability. Added `import random`.

- **Auth call retry loops** (`api.py`): `_get_id_token` and `_get_plant_access_token` previously made a single HTTP call with no retry. A momentary blip on the SAP IAS identity provider or the PAT endpoint caused an immediate `HovalApiError`, failing the entire coordinator refresh before the 90 s timeout could help. Both methods now have a `for attempt in range(_MAX_RETRIES)` loop identical to `_request`, with jittered backoff on `aiohttp.ClientError` and `TimeoutError`. `HovalAuthError` (4xx — bad credentials) is still raised immediately without retry since credentials don't fix themselves. Comments added to both `get_events` and `get_latest_event` explaining why `plant_id=` is intentionally omitted (those paths do not require `X-Plant-Access-Token`; adding it would cause 401s — fix #6 from recommendations).

- **Per-circuit `asyncio.timeout`** (`coordinator.py`): `_fetch_circuit_data` wraps its entire gather block in `async with asyncio.timeout(_CIRCUIT_TIMEOUT)` (35 s). Previously, one stuck circuit could occupy up to ~57 s of the coordinator's 90 s global budget; now it times out at 35 s, logs a WARNING, and returns `circuit_data` with empty `live_values` so the entity shows its previous state. The remaining circuits and plant-level fetches get the full remaining budget. Added `_CIRCUIT_TIMEOUT = 35` module constant with explanation.

- **Sleep moved inside background refresh task** (`coordinator.py`): The 2-second pre-refresh pause in `async_control_and_refresh` was previously awaited by the caller (the entity action method, e.g. `async_set_hvac_mode`), blocking it for 2 s before returning to HA. Moved inside `_do_refresh()` so the entity action returns to HA immediately after scheduling the task. The Hoval API still gets the same 2 s commit window before the read-back; only the caller is no longer blocked.

- **Adaptive poll backoff** (`coordinator.py`, `__init__.py`): On sustained API failures the coordinator now backs off its poll interval exponentially rather than hammering a struggling server at the configured rate. New state: `_consecutive_failures` counter and `_base_update_interval` (the user-configured rate). Logic: first `_BACKOFF_THRESHOLD=2` failures keep normal cadence (transient blips), from the third failure onward the interval doubles each time up to `_MAX_BACKOFF_SECONDS=900` (15 min). On any successful refresh the counter resets to 0 and the interval reverts to `_base_update_interval`. `HovalAuthError` does not increment the counter (HA's ConfigEntryAuthFailed machinery stops retries anyway). New `set_base_update_interval(interval)` method replaces direct `coordinator.update_interval =` assignment; it updates `_base_update_interval`, resets the failure counter, and sets `update_interval`. Called from both `async_setup_entry` and `_async_options_updated` in `__init__.py` so a user-changed scan interval also clears any in-progress backoff.

### v0.15.7
- **`latest_event_time` sensor fixed** (`sensor.py`): The `TIMESTAMP` device class requires a timezone-aware `datetime` object — not a raw string. Previously the sensor always showed `unknown` because the ISO-8601 string from the API (e.g. `"2026-02-17T10:30:00Z"`) was returned as-is. Now parsed via `dt_util.parse_datetime()`. Added `logging` import and `_LOGGER` to `sensor.py` (previously had no logger) so the debug line on parse failure can emit.
- **`_resolve_active_program_value` week selection fixed** (`coordinator.py`): The function previously always read from `week1` regardless of the circuit's `active_program`. Circuits running `week2` were getting the wrong schedule for `active_week_name`, `active_day_program_name`, and `program_air_volume`. Added `active_program: str | None = None` parameter; uses `week2` key when `active_program == "week2"`, falls back to `week1` for all other values (constant, ecoMode, standby, None) since those modes are not schedule-driven. `_fetch_circuit_data` passes `circuit_data.active_program` to the resolver.
- **Fan debounce task error handling** (`fan.py`): `_debounced_set` is scheduled as a fire-and-forget background task; previously any `HomeAssistantError` from `_send_percentage` was silently swallowed, leaving the UI stuck on a stale pending percentage. Now caught, logged at ERROR level, and `_pending_percentage` is cleared + `async_write_ha_state()` called so the UI reverts to the last confirmed state.
- **Background refresh task lifecycle** (`coordinator.py`, `__init__.py`): `async_control_and_refresh` now stores each background `_do_refresh` task handle in `_pending_tasks: set[asyncio.Task]` and registers a `discard` done-callback for automatic cleanup. New `cancel_pending_tasks()` method cancels all in-flight tasks. Called from `async_unload_entry` in `__init__.py` so that post-control refreshes cannot call `async_request_refresh()` on a torn-down coordinator after entry unload.
- **Fragile index arithmetic refactored** (`coordinator.py`): The single large `asyncio.gather(*all_tasks)` that mixed circuit coroutines with plant-level tasks (events, weather) and used manually tracked integer indices to extract results has been split into two explicit `gather` calls: one for circuits (`circuit_results`) and one for plant-level data (`latest_event_result, events_result, weather_result = await asyncio.gather(...)`). Eliminates the silent-breakage risk from reordering tasks.
- **`_fetch_circuit` extracted as coordinator method** (`coordinator.py`): The inner async function `_fetch_circuit` was redefined on every plant loop iteration and was untestable in isolation. Replaced by `_fetch_circuit_data(self, plant_id, path, ctype, circuit)` — a proper instance method on `HovalDataCoordinator`. Loop-variable capture no longer relies on default-arg tricks.
- **`_api_key_from_display` whitelist guard** (`select.py`): Previously fell through to return the raw display string when the reverse lookup failed (e.g. after a program rename on the API side), silently sending an invalid value to the cloud which would return an opaque 4xx. Now raises `HomeAssistantError` with an actionable message directing the user to reload the integration.
- **`resp.content_length == 0` → `not resp.content_length`** (`api.py`): When the Hoval server returns a 200 with an empty body but no `Content-Length` header, `aiohttp` sets `content_length=None`, bypassing the old `== 0` check and then crashing in `resp.json()` with `ContentTypeError`. `not resp.content_length` catches both `None` and `0`, fully closing the gap documented in CLAUDE.md's Known Pitfalls section.
- **`test_coordinator.py` module mock ordering fixed** (`tests/test_coordinator.py`): Replaced direct `sys.modules[...] = ha_mock` with `sys.modules.setdefault(...)` so that when both test files run in the same pytest process, the second file reuses the mocks registered by the first instead of overwriting them (which could invalidate already-imported modules and cause order-dependent test failures). Updated `_resolve_active_program_value` test suite: all existing calls now pass `active_program` explicitly; added two new tests (`test_week2_selected_when_active_program_is_week2`, `test_non_schedule_program_falls_back_to_week1`) covering the new week-selection logic; `_make_programs` extended with a `week2` schedule and a third day configuration.

### v0.15.6
- **`manifest.json` version corrected**: Version string was stuck at `0.15.2` instead of the current release version. Bumped to `0.15.6`.
- **Dead constant removed** (`const.py`): `REQUEST_TIMEOUT = 30` was left in `const.py` after v0.15.5 removed it from `api.py`'s imports. No file referenced it; now removed entirely.
- **`water_heater.py` constant consistency fix**: Three occurrences of the bare string literal `"REGULAR"` used as `mode_override` were replaced with `OPERATION_MODE_REGULAR` (imported from `const.py`). All other platforms (climate, fan, select) already used the constant; `water_heater.py` was the only exception. `OPERATION_MODE_REGULAR` is now included in the module's import from `const`.
- **`examples/hoval_client.py` dead endpoint fixed**: `get_circuits()` was still calling the removed v1 endpoint (`/v1/plants/{plantId}/circuits`), which returns HTTP 404 since 2026-04-21. Updated to `/v3/plants/{plantId}/circuits`, consistent with the main integration.

### v0.15.5
- **API timeout hardening** (`api.py`): Reduced `_MAX_RETRIES` from 3 → 2 and `_RETRY_BASE_DELAY` from 1.0 → 0.5 s to prevent startup hangs when the Hoval cloud is slow. Replaced single `total=30 s` `ClientTimeout` with split `connect=8 s / sock_read=20 s` timeouts on all requests including auth calls — dead connections are now detected in 8 s instead of 30 s. Removed unused `REQUEST_TIMEOUT` import.
- **Coordinator global timeout guard** (`coordinator.py`): Refactored `_async_update_data` into a thin `asyncio.timeout(90)` wrapper calling a new inner `_fetch_all_data` method. All `HovalAuthError` / `HovalApiError` exceptions are caught in one place and converted to `ConfigEntryAuthFailed` / `UpdateFailed`. A raw `TimeoutError` (API hung > 90 s) now surfaces as a clear `UpdateFailed` log instead of blocking HA's event loop.
- **`async_control_and_refresh` lock fix** (`coordinator.py`): The `control_lock` was previously held for the API call + sleep(2) + full coordinator refresh (up to ~149 s worst case), blocking all concurrent control actions (automations, button presses) for that entire window. Fixed: lock now releases immediately after the API call succeeds; the sleep(2) and refresh run outside the lock. The refresh itself is scheduled as a **fire-and-forget background task** via `hass.async_create_task`, so the entity action method returns to HA promptly even when the cloud is slow. Refresh failures are silently discarded (coordinator retries on normal poll schedule).
- **Optimistic state survives failed refreshes** (`coordinator.py`): `_mode_override.clear()` was called at the *start* of each refresh, meaning a timeout or API error mid-cycle would cause entities to snap back to stale state immediately. Fixed: overrides are now cleared only at the *end* of a successful fetch, so the optimistic state (e.g. "boost cancelled") remains visible until confirmed by real data.
- **`services.yaml` removed**: Entity services registered via `async_register_entity_service` are purely programmatic — a `services.yaml` file is only valid for domain-level services and caused HA's integration validator to raise an initialization error on startup. The `reset_ww_boost` service continues to work correctly in automations.

### v0.15.4
- **`reset_ww_boost` entity service** (`water_heater.py`, `const.py`, `strings.json`, `translations/en.json`): Adds a dedicated HA service to cancel an active temporary WW temperature override and immediately resume the week program — identical to pressing "reset" in the Hoval app.
  - API call: `DELETE /v3/plants/{plantId}/circuits/{circuitPath}/temporary-change` (already present as `api.reset_temporary_change()`; no API changes needed).
  - Registered as an entity service via `async_get_current_platform().async_register_entity_service(SERVICE_RESET_WW_BOOST, {}, "async_reset_temporary_change")` in `async_setup_entry`; targets `WaterHeaterEntity` instances only.
  - Service name constant `SERVICE_RESET_WW_BOOST = "reset_ww_boost"` added to `const.py`.
  - `strings.json` and `translations/en.json` extended with a `"services"` block containing the human-readable name and description.
  - Safe to call when no temporary change is active — the API treats it as a no-op.
  - Does **not** switch the circuit to standby or modify any week program; only removes the temporary override layer.
  - Example automation usage:
    ```yaml
    action:
      - service: hoval_connect.reset_ww_boost
        target:
          entity_id: water_heater.hoval_hot_water
    ```

### v0.15.3
- **Electric auxiliary heater sensors** (`sensor.py`): Adds 5 sensors for BL circuits covering the auxiliary electric heating element (distinct from the heat pump compressor):
  - `operating_hours_el_heater` — cumulative runtime (`operatingHoursElHeater`, hours, `TOTAL_INCREASING`)
  - `operation_cycles_el_heater` — start/stop cycle count (`operationCyclesElHeater`, `TOTAL_INCREASING`)
  - `heat_amount_el_heater` — thermal energy produced (`heatAmountElHeater`, MWh, `TOTAL_INCREASING`)
  - `energy_el_heater` — electrical energy consumed (`energyElHeater`, MWh, `TOTAL_INCREASING`)
  - `el_heater_active` — current active status (`elHeaterActive`, diagnostic string/bool)
  - All scoped to `CIRCUIT_TYPE_BL`. Live-value keys follow Hoval's established camelCase pattern but are unconfirmed — verify against the `Circuit <path> live_values: {...}` debug log line and adjust `value_fn` lambdas if needed.

### v0.15.2
- **WaterHeater entity for WW circuits** (`water_heater.py`, new file): Adds a proper `WaterHeaterEntity` for each WW circuit. Exposes current temperature (`tempSf1Actual`), target temperature (`tempTarget`), and three operation modes: `heat_pump` (normal week-program), `high_demand` (temporary override active), `off` (standby). `set_temperature()` calls `set_temporary_change` with `duration=midnight` so the override auto-expires at 00:00 with no cleanup needed. `set_operation_mode()` maps heat_pump/high_demand → `reset_circuit` and off → standby program.
- **Solar boost automation** (`automations.yaml`): Two automations — one fires at `input_datetime.max_solar_start_time` and calls `water_heater.set_temperature` with the value from `input_number.ww_boost_temperature`; the second fires at 11:00 and calls `water_heater.set_operation_mode: heat_pump` to cancel the override.
- **Boost temperature helper** (`helpers.yaml`): `input_number.ww_boost_temperature` (40–65 °C, default 50 °C) for setting the boost target from the HA UI.
- Also includes all changes from v0.15.0 and v0.15.1.

### v0.15.1
- **Bugfix — circuit status sensors**: `circuitStatus` is a field on the circuit list response (`CircuitV3DTO`), not in the live-values key-value array. Added `circuit_status: str | None` to `HovalCircuitData`, populated from `circuit.get("circuitStatus")` in coordinator. Sensor `value_fn` lambdas now read `c.circuit_status`.

### v0.15.0
- **WW program control**: Hot water circuits now expose a `Program` select entity.
- **Circuit status diagnostics**: Diagnostic `circuit_status` sensor for BL, HK, WW.
- **Translation fix**: `translations/en.json` synced with `strings.json`.
