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

### v0.15.8
Comprehensive defect fix release. Addresses all 15 items raised in the post-0.15.7 review.

#### API reliability (api.py)
- **Fix #1 — Retry jitter**: `_jittered_delay()` now multiplies base delay by `uniform(0.8, 1.2)` (formula corrected from the broken `1 ± 0.4*(r-0.5)*2` that produced `[0.6, 1.4]`). Prevents thundering-herd retries when multiple HA instances recover simultaneously after a Hoval cloud incident.
- **Fix #5 — `content_length` check**: Replaced fragile `resp.content_length == 0` with an explicit two-step check: `if resp.status == 204: return None` then `cl = resp.content_length; if cl is not None and cl == 0: return None`. `content_length` is `None` when the server omits the Content-Length header — the old check would silently return `None` and drop valid JSON bodies.
- **Fix #11 — Auth backoff after IDP rejection**: `_auth_cooldown_until: float` added to `HovalConnectApi`. After HTTP 400/401/403 from the IDP, `_auth_cooldown_until = time.monotonic() + 60.0`. Subsequent `_get_id_token()` calls within the cooldown raise `HovalApiError("Auth cooldown active…")` rather than hitting the IDP again. Cleared on successful auth and on `invalidate_tokens()`. Prevents hammering the identity provider when credentials are simply wrong.

#### Connection health & circuit breaker (coordinator.py)
- **Fix #2 — Circuit breaker**: After `_CB_THRESHOLD = 5` consecutive failures, `_maybe_open_circuit_breaker()` sets `_cb_open = True` and schedules a probe after `_CB_PROBE_INTERVAL = 300 s`. While open, `_async_update_data` raises `UpdateFailed` immediately without making any network requests. CB-skipped polls are NOT counted in `total_polls` or recorded in `_poll_records`. A successful probe closes the breaker; a failed probe resets the interval. `circuit_breaker_open` property exposed for diagnostics.
- **Fix #3 — Per-endpoint partial failure tracking**: `HovalCircuitData` gains `failed_sub_fetches: list[str]`. `_fetch_circuit_data` appends `"live_values"` or `"programs"` when those sub-fetches fail inside `gather(return_exceptions=True)`. `_fetch_all_data` aggregates these plus `latest_event`, `events`, `weather` failures into `HovalConnectionHealth.partial_failures_last_poll`, `total_partial_failures`, and `partial_failure_endpoints` (comma-separated list). Previously these failures were completely silent.
- **Fix #6 — Program cache stale-data fallback**: When programs fetch fails and a cached entry exists, the stale cached value is used and the cache timestamp is NOT updated (so the next poll retries). Previously the stale entry was abandoned mid-TTL, leaving circuits with no program data until the endpoint recovered.
- **Fix #10 — `_fetch_circuit` promoted to proper method**: `_fetch_circuit` was an inner closure relying on a default-arg trick (`_plant_id: str = plant_id`) to avoid closure-over-loop-variable. Now a proper `async def _fetch_circuit_data(self, plant_id, path, ctype, circuit)` coordinator method. Independently testable and no scoping risk.

#### Rolling-window telemetry (coordinator.py)
- **Fix #12 — `_poll_history` stores failure type, not bool**: `_PollRecord(ts, error_type, latency_ms)` NamedTuple replaces the old `(float, bool)` tuple. `error_type` is `None` for success or `"timeout"/"auth"/"api"/"unknown"` for failures. `latency_ms` populated only for successes. This unlocks per-type rate computation.
- **Fix #13 — `api_auth_failure_rate_1h` sensor**: New `auth_failure_rate_pct_1h` rolling-window property on `HovalConnectionHealth`, counting only `error_type == "auth"` records. Exposed as `api_auth_failure_rate_1h` sensor.
- **Fix #15 — Rate sensors return `0.0` not `None` before first poll**: `failure_rate_pct_1h` and `auth_failure_rate_pct_1h` now return `0.0` on an empty window, so sensors show `0%` rather than `unknown` immediately after HA restart.

#### Sensor fixes (sensor.py)
- **Fix #14 — Counter state_class corrected**: `api_total_polls`, `api_total_failures`, `api_auth_failures` changed from `TOTAL_INCREASING` to `MEASUREMENT`. Dimensionless counters without a physical unit are stored inconsistently by HA's recorder with `TOTAL_INCREASING` (some HA versions show `unknown` until the first non-zero value). `MEASUREMENT` stores every value reliably.
- **Fix #7 — P95 latency sensor**: New `api_p95_latency_1h` sensor backed by `p95_latency_ms_1h` property. Computed from successful-poll latencies in the 1-hour window; requires ≥5 samples (returns `None` otherwise). Useful for detecting gradual latency degradation before hard timeouts begin.
- **Fix #8 — `api_seconds_since_success` sensor**: New sensor computing `(dt_util.utcnow() - last_success).total_seconds()` live in `native_value`. Updates every coordinator poll. Unit `s`, state_class `MEASUREMENT`. Enables simple automations like "alert if no successful poll in 10 minutes" without template sensors.
- **New — `api_partial_failures_last_poll`**: Exposes `HovalConnectionHealth.partial_failures_last_poll` — count of sub-tasks that failed silently in the most recent poll. Non-zero while the overall poll succeeds means some entities have stale data.

#### Diagnostics (diagnostics.py)
- **Fix #9 — Connection health in diagnostics export**: `async_get_config_entry_diagnostics` now includes a `connection_health` section with all telemetry fields (timestamps as ISO-8601, all counters, rolling-window rates, P95 latency, partial-failure details, circuit-breaker state, and rolling-window sample count). Raw `_poll_records` are omitted (monotonic timestamps are meaningless outside the process); derived metrics are included instead.

#### Summary of new/changed sensors
| Key | Change |
|---|---|
| `api_total_polls` | state_class: `TOTAL_INCREASING` → `MEASUREMENT` |
| `api_total_failures` | state_class: `TOTAL_INCREASING` → `MEASUREMENT` |
| `api_auth_failures` | state_class: `TOTAL_INCREASING` → `MEASUREMENT` |
| `api_failure_rate_1h` | now returns `0.0` (not `None`) before first poll |
| `api_auth_failure_rate_1h` | **NEW** — auth errors as % of polls in last hour |
| `api_p95_latency_1h` | **NEW** — P95 successful poll latency over last hour (ms) |
| `api_seconds_since_success` | **NEW** — seconds elapsed since last good poll |
| `api_partial_failures_last_poll` | **NEW** — silent sub-task failures in last poll |

### v0.15.7
- **API connection health telemetry** (`coordinator.py`, `sensor.py`, `strings.json`, `translations/en.json`): Adds a comprehensive set of diagnostic sensors to surface Hoval cloud API connection quality directly in Home Assistant. Motivated by persistent server-side instability causing silent failures that are hard to diagnose without log diving.

  **New `HovalConnectionHealth` dataclass** (`coordinator.py`): Persists across coordinator poll cycles on the coordinator instance (not inside `HovalData` which is regenerated each poll). Fields tracked:
  - `last_success: datetime | None` — UTC timestamp of last successful full poll
  - `last_error_time: datetime | None` — UTC timestamp of last poll failure
  - `last_error_msg: str | None` — Error message string (truncated to 200 chars)
  - `last_error_type: str | None` — Category string: `"timeout"` | `"auth"` | `"api"` | `"unknown"`
  - `consecutive_failures: int` — Count of consecutive failed polls; reset to 0 on any success
  - `total_failures: int` — Cumulative failed polls since HA startup
  - `total_polls: int` — Cumulative poll attempts since HA startup
  - `auth_failures: int` — Cumulative `HovalAuthError` occurrences since HA startup
  - `poll_latency_ms: float | None` — Wall-clock duration of last successful full poll in milliseconds (measured with `time.monotonic()`)

  **Instrumented `_async_update_data`** (`coordinator.py`): Health counters updated in every branch (success and all exception paths) before exceptions are re-raised. On success: resets `consecutive_failures`, records `last_success` and `poll_latency_ms`. On each failure type: increments `consecutive_failures`, `total_failures`, sets `last_error_time`, `last_error_msg`, `last_error_type`. `HovalAuthError` additionally increments `auth_failures`.

  **New `HovalConnectionSensorDescription` dataclass** (`sensor.py`): Like `HovalPlantSensorEntityDescription` but `value_fn` takes a `HovalConnectionHealth` instead of `HovalPlantData`.

  **New `CONNECTION_SENSOR_DESCRIPTIONS` tuple** (`sensor.py`): Nine diagnostic sensors, all `EntityCategory.DIAGNOSTIC` (hidden by default, visible under Developer Tools / Entities). Attached to the plant device:
  | Entity key | Device class | State class | Description |
  |---|---|---|---|
  | `api_last_success` | `timestamp` | — | When last poll completed OK |
  | `api_last_error_time` | `timestamp` | — | When last poll failed |
  | `api_last_error` | — | — | Error message string |
  | `api_last_error_type` | — | — | `timeout` / `auth` / `api` / `unknown` |
  | `api_consecutive_failures` | — | `measurement` | Failures in a row (resets on success) |
  | `api_total_failures` | — | `total_increasing` | Cumulative failures since startup |
  | `api_auth_failures` | — | `total_increasing` | Cumulative auth errors since startup |
  | `api_total_polls` | — | `total_increasing` | Cumulative polls since startup |
  | `api_poll_latency` | — | `measurement` | Last successful poll duration (ms) |

  **New `HovalConnectionSensor` entity class** (`sensor.py`): Overrides `available` to always return `True` — the whole point is to report failures, so marking unavailable on poll error would defeat the purpose. `native_value` handles `datetime` (returned as-is for TIMESTAMP sensors), `int`/`float`, and `str` (truncated to 255 chars).

  **Suggested automations** using the new sensors:
  - Alert when `api_consecutive_failures` ≥ 3 (Hoval cloud is likely down)
  - Alert when `api_last_error_type` = `"auth"` (credentials expired / rotated)
  - Monitor `api_poll_latency` for gradual degradation (rising p50 predicts timeouts)
  - Long-term statistics on `api_total_failures` / `api_total_polls` for reliability dashboards

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
