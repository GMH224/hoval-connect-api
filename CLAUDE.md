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

### v0.17.0 — Production audit & bug fixes

Full code audit across all platforms following the v0.16.x BL-circuit debugging
session. No new features; all changes are correctness fixes and hardening.

#### Bug fixes

**`climate.py` — HK current/target temperature was always `None`**
The `current_temperature` property looked for `"actualTemperature"` and
`"roomTemperature"` in live values; the actual HK field is `"roomTempActual"`.
`target_temperature` looked for `"targetTemperature"`; the actual field is
`"roomTempTarget"`.  Both properties have been updated with the correct primary
key and the old names as fallbacks for future circuit types.

**`climate.py` — `hvac_action` was always `HVACAction.IDLE`**
The property read `circuit.live_values.get("circuitStatus", "")`, but the
`circuitStatus` key never appears in live values — it comes from the circuit-list
response and is stored in `circuit.circuit_status`.  The live-values equivalent is
the `"status"` key (e.g. `"heating"`, `"off"`).  The property now reads
`circuit.live_values.get("status") or circuit.circuit_status` so both sources
are consulted.

**`sensor.py` — missing `room_temp_actual` sensor for HK circuits**
`roomTempActual` is present in HK live values and is used by the fixed climate
entity, but was not exposed as a standalone sensor.  Added
`HovalSensorEntityDescription(key="room_temp_actual", ...)` with
`circuit_types=frozenset({CIRCUIT_TYPE_HK})`.

**`coordinator.py` — `restore_from_store` crashes on corrupt storage data**
`HovalCircuitHealth.restore_from_store` and
`HovalConnectionHealth.restore_from_store` both called `int(data.get(field, 0))`
without a try/except.  A corrupt `.storage` file (e.g. type coercion from a
previous version) would raise `ValueError` and prevent the integration from
loading.  All integer conversions are now wrapped in `try/except (TypeError, ValueError)`.

**`coordinator.py` — `ch` variable could be unbound (UnboundLocalError)**
The `HovalCircuitHealth` object was assigned inside the `if/else` block for live
values.  If an unexpected exception occurred after the `if` branch was entered
but before the assignment, `circuit_data.circuit_failure_rate_1h = ch.failure_rate_1h`
would raise `UnboundLocalError`.  `ch` is now fetched before the if/else.

**`coordinator.py` — `lv_raw` type guard**
Added `if not isinstance(lv_raw, list): lv_raw = []` after the paginated-wrapper
unwrap, so an unexpected non-list (e.g. `None` from a future `api.py` regression)
is handled gracefully rather than crashing the comprehension.

#### Other changes
- `coordinator.py` — trimmed the 25-line inline bug-history comment from the
  programs block to 6 lines; full history remains in CLAUDE.md (v0.16.2 entry).
- `hacs.json` — added `"render_readme": true`.
- `manifest.json` — version `0.17.0`.
- `strings.json` / `translations/en.json` — added `room_temp_actual` entry
  ("Room temperature").
- `tests/test_coordinator.py` — comprehensive rewrite: all existing tests
  preserved, new tests added for every fix above plus defensive edge cases.

### v0.16.2
**Bug fix — BL (boiler) circuit STILL loses all entities (v0.16.1 fix was incomplete)**

#### Root cause of v0.16.1 regression

v0.16.1 guarded against `programs is None` in `_fetch_circuit`.  This handled
the case where Hoval's programs endpoint returned HTTP 204 (empty body), which
`_request()` converts to Python `None`.

However, Hoval's API update (late May 2026) made the endpoint return
**HTTP 200 with body `[]`** (an empty JSON array) for non-programmable circuits
such as BL/boiler (those with `operationMode=None`).

`[]` is **not** `None` and **not** a `BaseException`, so the v0.16.1 guard
`if programs is not None and not isinstance(programs, BaseException)` evaluated
to **True**, entering the processing block.  Two things then happened:

1. `self._program_cache[path] = ([], time.time())` — the empty list was cached,
   poisoning the cache for subsequent polls within the TTL window.
2. `_resolve_active_program_value([], now, ...)` was called.  The function's
   v0.16.1 guard was `if programs is None`, which is False for `[]`, so execution
   continued to `[].get("dayPrograms", {})` →
   **`AttributeError: 'list' object has no attribute 'get'`**.

This `AttributeError` propagated out of `_fetch_circuit`, was captured by the
outer `asyncio.gather(*all_tasks, return_exceptions=True)` in `_fetch_all_data`,
and caused BL to be logged as `"Circuit fetch failed"` and silently dropped from
`plant_data.circuits`.  With no BL circuit in coordinator data, no sensor or
diagnostic entities were created for the Hoval Heatgenerator device.

The cached `[]` made it self-perpetuating: on subsequent polls within the 5-minute
TTL, `results[1]` was read from cache as `[]`, re-entered the block, and crashed
again — even after HA restart (the cache is in-memory, so the poisoned entry did
not persist across restarts, but the API kept returning `[]` each fresh poll).

#### Fixes (`coordinator.py`)

1. **`_fetch_circuit` programs condition** — Changed from
   `if programs is not None and not isinstance(programs, BaseException)` to
   **`if isinstance(programs, dict)`**.  Only a proper dict is a valid programs
   payload; anything else (`None`, `[]`, an int, a string, …) falls through to
   the `else` branch which logs the type and value at DEBUG level and leaves all
   program fields at their `None` defaults.  Non-dict values are **not cached**,
   so the cache is never poisoned.

2. **`_resolve_active_program_value` guard** — Changed from
   `if programs is None` to **`if not isinstance(programs, dict)`**.  This makes
   the function safe against every non-dict input (safety net; the primary fix is
   #1 above).

3. **Live-values comprehension** — Added `"key" in v and "value" in v` to the
   filter so items missing the expected fields are silently skipped rather than
   raising `KeyError`.

### v0.16.1
**Bug fix — BL (boiler) circuit loses all entities after Hoval May 2026 API change**

#### Root cause
Hoval updated their API backend in late May 2026 to return HTTP 204 (no content)
from `GET /v3/plants/{plantId}/circuits/{circuitPath}/programs` for non-programmable
circuits such as BL (boiler). Previously this endpoint returned HTTP 404, which the
integration correctly treated as an exception via `asyncio.gather(return_exceptions=True)`.

The `_request` helper maps any response with `status == 204` or `content_length == 0`
to a Python `None` return value. `None` is not a `BaseException`, so the guard
`if not isinstance(programs, BaseException)` was True, and `_fetch_circuit` called
`_resolve_active_program_value(None, ...)`. That function immediately executed
`programs.get("dayPrograms", {})` — raising `AttributeError: 'NoneType' has no
attribute 'get'`.

This `AttributeError` propagated out of `_fetch_circuit` and was captured by the
outer `asyncio.gather(*all_tasks, return_exceptions=True)` in `_fetch_all_data`,
causing the BL circuit to be silently skipped (`"Circuit fetch failed"` debug log)
and never added to `plant_data.circuits`. With no circuit in the coordinator data
no entities were created for the Hoval Heatgenerator device.

#### Fixes (`coordinator.py`, `api.py`)
1. **`_resolve_active_program_value`** — Added early-exit guard:
   `if programs is None: return None, None, None`. Prevents the `AttributeError`
   even if `None` reaches the function through any path (safety net).

2. **`_fetch_circuit` programs block** — Changed condition from
   `if not isinstance(programs, BaseException)` to
   `if programs is not None and not isinstance(programs, BaseException)`.
   `None` now falls through to the new `elif programs is None` branch, which
   logs a debug message and leaves all program fields at their `None` defaults.
   This is the primary fix: `None` programs are handled gracefully, `_fetch_circuit`
   returns normally, and the BL circuit is added to `plant_data.circuits` → entities
   are created.

3. **`_fetch_circuit` live-values block** — Added `isinstance(lv_raw, dict)` guard
   that extracts `lv_raw.get("content", [])` before building `live_values`. This
   defends against the live-values endpoint also adopting the paginated wrapper shape.
   The comprehension also adds `if isinstance(v, dict)` to skip any non-dict items.

4. **`api.get_circuits`** — Now returns a normalised list regardless of whether the
   endpoint sends a plain list or a paginated wrapper `{"content": [...], ...}`.

5. **`api.get_live_values`** — Same normalisation: plain list or paginated wrapper
   both produce a plain list of `{"key": ..., "value": ...}` objects.

6. **`api.get_plants`** — Full pagination loop: fetches all pages (12 items each)
   and returns a flat list. Handles both old (plain list) and new (Spring Page wrapper)
   API shapes. Previously only page 0 was fetched; users with >12 plants would have
   had plants silently missing.

### v0.16.0
Implements all four improvements suggested at the end of the v0.15.9 release notes.

#### Persistence across HA restarts (`const.py`, `coordinator.py`, `__init__.py`)
Cumulative health counters now survive HA restarts via HA's built-in `Store` helper (`homeassistant/helpers/storage`). A store keyed `hoval_connect_health` (version 1) is created in `async_setup_entry` and loaded *before* the first coordinator refresh, so sensors show historical context immediately — not a misleading "0 failures since startup" after every reboot.

**Persisted fields**: `total_polls`, `total_failures`, `auth_failures`, `error_counts`, `ema_latency_ms`, and per-circuit `total_polls` / `total_failures`.

**Intentionally ephemeral** (reset on restart): rolling deques (`_poll_times`, `_failure_times`, etc.), `consecutive_failures`, `last_success`, `last_error_*`. Rolling rates reflect the current HA session; mixing sessions would give misleading 1-hour rates.

Saves are *debounced* via `Store.async_delay_save(fn, 30s)` so busy poll cycles don't hammer I/O. A final immediate `async_save` is called from `async_unload_entry` to guard against data loss on a clean HA shutdown before the debounce fires.

#### Exponential Moving Average latency (`coordinator.py`, `sensor.py`, `strings.json`, `translations/en.json`)
Adds `api_ema_latency` sensor (α = 0.1). The EMA weights the most-recent poll at 10 % and the prior EMA at 90 %, giving a smooth trend line that responds to sustained degradation without being dominated by one-off spikes. A single 10 s spike on a 100 ms baseline produces EMA ≈ 1 090 ms — a clear signal but not an alarm. The EMA is persisted across restarts so its trend carries meaningful history; it's never reset to `None` unless the storage file is deleted.

The EMA is calculated without storing any additional history — a single scalar field updated on each successful poll.

#### Finer-grained error bucketing (`coordinator.py`)
`last_error_type` and the new `error_counts` dict now use five distinct string constants instead of four blended categories:

| Constant | When raised |
|---|---|
| `"timeout"` | The 90 s overall coordinator asyncio timeout fires |
| `"auth"` | `HovalAuthError` — credentials problem |
| `"circuit_list"` | `get_circuits()` specifically fails (most impactful single endpoint) |
| `"api"` | Any other `HovalApiError` |
| `"unknown"` | Unexpected exception (schema change, bug) |

`"circuit_list"` is a new sentinel raised by the private `_CircuitListError` exception class, which wraps `HovalApiError` from `get_circuits()` before it propagates to `_async_update_data`. This lets `error_counts["circuit_list"]` distinguish a circuits-endpoint failure from a generic API failure on a less-critical endpoint (e.g. weather). `error_counts` is persisted and included in the `counters_since_startup` diagnostics group.

#### Per-circuit reliability tracking (`coordinator.py`, `sensor.py`, `strings.json`, `translations/en.json`)
Adds `HovalCircuitHealth` — a dedicated dataclass per circuit path (keyed in `HovalConnectionHealth._circuit_health`) that tracks:

- `total_polls` / `total_failures` — cumulative; persisted
- `consecutive_failures` — session-only; resets on success
- `last_success` / `last_failure` (UTC datetimes) — session-only
- `last_error` (str, truncated to 200 chars) — session-only
- `failure_rate_1h` / `availability_1h` — computed from rolling deques, same pattern as the coordinator-level rates

`record_success(ts)` / `record_failure(ts, error)` are called inside `_fetch_circuit` after the live-values fetch result is known. Programs-fetch failures are *not* counted because they fall back to a stale cache and are far less impactful on entity availability.

Two new **circuit-level diagnostic sensors** are added to `CIRCUIT_SENSOR_DESCRIPTIONS`:

| Entity key | Unit | Description |
|---|---|---|
| `circuit_failure_rate_1h` | `%` | Live-values fetch failure rate for this specific circuit in the last hour |
| `circuit_availability_1h` | `%` | Inverse of failure rate |

These surfaces a partial outage (e.g. Hoval rolling out a change that breaks one circuit type's endpoint) that the plant-level sensors would mask.

The full per-circuit health snapshot (`total_polls`, `total_failures`, `consecutive_failures`, `failure_rate_1h_pct`, `availability_1h_pct`, `last_success`, `last_failure`, `last_error`) is included in the `connection_health.circuits` section of the HA diagnostics export, sorted alphabetically by circuit path.

#### Suggested automations using new sensors
- Alert when `circuit_failure_rate_1h` > 50 % on any circuit → partial API outage
- Alert when `api_ema_latency` > 8 000 ms → sustained cloud slowdown, timeouts likely soon
- Trend `api_ema_latency` in a Grafana/InfluxDB dashboard for long-term reliability history
- Alert when `error_counts["circuit_list"]` increases → most severe single-endpoint failure

### v0.15.9
- **Telemetry: rolling 1-hour rate sensors** (`coordinator.py`, `sensor.py`, `strings.json`, `translations/en.json`): Adds three new diagnostic sensors that answer the most actionable question — *"how reliable is the Hoval cloud right now?"* — without requiring custom template helpers:
  | Entity key | Unit | Description |
  |---|---|---|
  | `api_failure_rate_1h` | `%` | Percentage of polls that failed in the last hour |
  | `api_auth_failure_rate_1h` | `%` | Percentage of polls with an auth error in the last hour |
  | `api_availability_1h` | `%` | Inverse of failure rate — API uptime over the last hour |

  All three return `None` (unavailable) until at least one poll is recorded inside the rolling window, so they don't show a misleading `0 %` immediately after startup.

- **Telemetry: rolling latency statistics** (`coordinator.py`, `sensor.py`): Replaces the single "last poll latency" scalar with three sensors covering the last 60 successful polls:
  | Entity key | Description |
  |---|---|
  | `api_poll_latency` (renamed) | Last successful poll — `(last)` suffix added for clarity |
  | `api_avg_latency` | Rolling arithmetic mean — useful for spotting gradual slowdowns |
  | `api_p95_latency` | 95th-percentile — catches tail latency that the mean hides; a rising p95 reliably predicts imminent timeouts |

- **Telemetry: rolling-history internals** (`coordinator.py`): `HovalConnectionHealth` now maintains four in-memory deques (not dataclass fields, so they never appear in `asdict()` or corrupt serialisation):
  - `_poll_times` — UTC timestamp of every poll attempt (maxlen=180, ~90 min at 30 s polling)
  - `_failure_times` — UTC timestamp of every failed poll
  - `_auth_failure_times` — UTC timestamp of every auth failure
  - `_latency_samples` — wall-clock duration (ms) of every successful poll (maxlen=60)
  All computed properties (`failure_rate_1h`, `auth_failure_rate_1h`, `availability_1h`, `avg_latency_ms`, `p95_latency_ms`) use `datetime.now(timezone.utc)` for the rolling cutoff so they are always accurate regardless of when HA started or how long it ran. Each failure branch in `_async_update_data` now appends a single shared timestamp variable (`_ts = dt_util.utcnow()`) rather than calling `dt_util.utcnow()` multiple times — ensures all counter fields for the same failure are timestamped identically.

- **Diagnostics: `connection_health` section** (`diagnostics.py`): The HA diagnostics export (`Developer Tools → Diagnostics`) now includes a structured `connection_health` block alongside `config_entry` and `coordinator_data`. The block is produced by the new `HovalConnectionHealth.as_diagnostic_dict()` method and contains four groups:
  ```
  connection_health
  ├── last_success              ISO-8601 UTC string
  ├── last_error
  │   ├── time                  ISO-8601 UTC string
  │   ├── type                  "timeout" | "auth" | "api" | "unknown"
  │   └── message               truncated error string
  ├── counters_since_startup
  │   ├── total_polls
  │   ├── total_failures
  │   ├── auth_failures
  │   ├── consecutive_failures
  │   └── overall_failure_rate_pct
  ├── rolling_1h_window
  │   ├── polls                 raw count in the window
  │   ├── failures
  │   ├── auth_failures
  │   ├── failure_rate_pct
  │   ├── auth_failure_rate_pct
  │   └── availability_pct
  └── latency_ms
      ├── last
      ├── avg
      ├── p95
      └── sample_count
  ```

- **Suggested automations** using the new sensors:
  - Alert when `api_availability_1h` < 80 % (sustained outage, not just a blip)
  - Alert when `api_auth_failure_rate_1h` > 0 % (credentials may have rotated)
  - Alert when `api_p95_latency` > 15 000 ms (cloud degradation likely to cause timeouts soon)
  - Long-term statistics on `api_failure_rate_1h` for a reliability trend dashboard

### v0.15.8
- **Bug fix — `async_control_and_refresh` blocked entity actions for 2 s** (`coordinator.py`): `await asyncio.sleep(2)` was placed *before* `hass.async_create_task(_do_refresh())`, so every control action (fan speed change, set temperature, HVAC mode switch) blocked the calling entity method for 2 s before returning to HA. Despite the docstring saying the caller returns promptly, it did not. Fixed by moving the `asyncio.sleep(2)` inside `_do_refresh()`, which runs as a background task. Entity action methods now return to HA immediately; the 2 s settle delay happens off the critical path.
- **Bug fix — `_resolve_active_program_value` always used `week1`, breaking week2 users** (`coordinator.py`): The function hard-coded `programs.get("week1", {})` regardless of which weekly schedule was active. Users running on `week2` got the wrong `active_week_name`, wrong `active_day_program_name`, and wrong `program_air_volume` — the last of which feeds into `resolve_fan_speed()`'s fallback chain, silently sending incorrect airflow targets to the API. Fixed by adding an `active_program: str | None = None` parameter; the function now selects `"week2"` when `active_program == "week2"`, otherwise defaults to `"week1"`. The call-site in `_fetch_circuit` passes `circuit_data.active_program`.
- **Bug fix — `latest_event_time` TIMESTAMP sensor returned a raw ISO string** (`sensor.py`): `HovalPlantSensor.native_value` returned `str(val)` for any string value with no `native_unit_of_measurement`. The `latest_event_time` sensor carries `device_class=SensorDeviceClass.TIMESTAMP` — HA requires a `datetime` object for TIMESTAMP sensors; a plain string causes state errors. Fixed by importing `dt_util` and calling `dt_util.parse_datetime(val)` when `device_class == SensorDeviceClass.TIMESTAMP` and the value is a string. The return-type annotation is updated to `datetime | float | str | None`.
- **Bug fix — `get_events` / `get_latest_event` did not send Plant Access Token** (`api.py`): Both methods called `self._request(...)` without the `plant_id=` keyword argument, so `_headers()` only attached the `Authorization: Bearer` id-token and omitted `X-Plant-Access-Token`. All v1 endpoints require the PAT; the missing header caused silent failures (swallowed by `gather(return_exceptions=True)`) that made event sensors always show `None`. Fixed by passing `plant_id=plant_id` in both calls.
- **Bug fix — unguarded `circuit["path"]` KeyError** (`coordinator.py`): A circuit dict returned without a `"path"` key raised `KeyError`, failing the entire plant refresh rather than skipping the malformed entry. Fixed by using `circuit.get("path")` with an explicit `None` check and a `_LOGGER.warning` before `continue`.
- **Bug fix — auth headers built once before retry loop** (`api.py`): `headers = await self._headers(plant_id)` was computed once before the `for attempt in range(_MAX_RETRIES)` loop. On a long retry cycle where the id-token expires between attempts, the stale bearer token was reused and would be rejected with 401. Fixed by moving the `headers` call inside the loop so tokens are always fresh at the start of each attempt.
- **Bug fix — `select.py` display-name reverse-lookup collision** (`select.py`): `_api_key_from_display` fell through to `DEFAULT_NAMES` for all keys, including those already overridden by the user. If a user named their `week2` program `"Week 1"` (the default for `week1`), the lookup returned `"week1"` and activated the wrong program. Fixed by skipping `DEFAULT_NAMES` for any key already present in `circuit.program_names`.

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
