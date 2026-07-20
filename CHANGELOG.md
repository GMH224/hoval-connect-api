# Changelog

All notable changes to the `hoval_connect` integration are documented here.
This project follows a loose [Semantic Versioning](https://semver.org/) scheme
while pre-1.0 (minor = behavioural/feature change, patch = internal fix).

## [0.21.1] - 2026-07-20

Hardening release from a full ICS-style code audit (findings F1–F9; the
complete report with reproduction evidence, severity ratings, and residual
risks is in `docs/audit-v0.21.1.md`). No new features, no config changes, no
entity changes. Restart Home Assistant after updating.

### Fixed
- **F1 — Schema-drift crash paths in program resolution** (`coordinator.py`):
  `_resolve_active_program_value()` raised on plausible nested API drift (a
  day configuration missing `id`, a `week1`/`week2` entry that isn't a dict,
  a phase missing `start`/`end`, non-numeric phase times). Because the
  exception escaped `_fetch_circuit` inside `gather(return_exceptions=True)`,
  the **whole circuit — including already-fetched live values — was silently
  dropped** for that poll, with only a debug log. The resolver is now fully
  defensive (every nested level type-checked, malformed entries skipped), and
  the call site carries a second isolation barrier so any residual parsing
  exception degrades program *fields* only, logged at WARNING.
- **F2 — Events path not hardened against response-shape drift**
  (`api.py`, `coordinator.py`): `get_events()`/`get_latest_event()` were the
  only list-shaped endpoints without the May-2026 pagination-wrapper
  normalisation that `get_circuits()`/`get_live_values()`/`get_plants()`
  already had. A wrapped events response reached list slicing in the plant
  loop — *outside* per-circuit exception isolation — and **failed the entire
  poll** (every entity unavailable, `ERROR_TYPE_UNKNOWN`). Both endpoints now
  normalise the wrapper in the client; `_parse_event()` tolerates non-dict
  payloads; the coordinator's events block gained isinstance guards plus a
  try/except that falls back to cached events; the weather block validates
  its first forecast element.
- **F3 — Unbounded pagination / unbounded config-flow validation**
  (`api.py`, `config_flow.py`): `get_plants()` looped for as long as the
  server reported `"last": false`; a misbehaving upstream could loop forever
  with unbounded memory growth. Now capped at `_MAX_PLANT_PAGES` (50 pages =
  600 plants) with a WARNING on truncation. The config-flow credential
  validation (setup **and** reauth) — which unlike the coordinator had no
  outer timeout at all — is now bounded by a 30 s `asyncio.timeout`, mapped
  to the existing `cannot_connect` error.
- **F5 — Silent failure of debounced slider writes** (`fan.py`,
  `number.py`): the debounced fan-speed and weather-impact writes run as
  fire-and-forget tasks, so their `HomeAssistantError` never reached the UI —
  a failed actuation was only visible in the event loop's unhandled-task log.
  Failures are now caught in `_debounced_set`, logged at **WARNING** with the
  circuit and requested value, and entity state is rewritten so the slider
  visibly reverts to the device's actual value.

### Changed
- **F4 — Comment/behaviour drift on optimistic weather-impact overrides**
  (`coordinator.py`): comments and docstrings claimed the weather-impact
  override is "cleared on the next successful poll"; the code has always been
  TTL-only. Documentation now states the actual (and intentional) semantics —
  TTL expiry plus the settings-cache update keep entity state consistent
  because circuit settings are cache-tiered and not re-fetched every poll.
- **F9 — Health tracker encapsulation** (`coordinator.py`):
  `_async_update_data` no longer reaches into `HovalConnectionHealth`'s
  private deques. New public recording API: `record_poll_attempt()`,
  `record_poll_success()`, and `record_error()` (renamed from
  `_record_error()`; counter semantics unchanged, persisted storage schema
  unchanged).

### Tests / CI
- **Coordinator async core is now behaviourally tested** (new
  `tests/test_coordinator_fetch.py`, 28 tests): `_fetch_all_data` and
  `_async_update_data` run for real against a scripted fake API — happy path
  (circuit filtering, v1 program mapping, live values, program/event/weather
  caches, discovery signal), the F1/F2 degradation guarantees, offline-plant
  handling, error classification, and HK weather-impact settings with
  cache fallback. Enabled by rewriting `tests/conftest.py` to install
  minimal **real** stub classes for `DataUpdateCoordinator`, `UpdateFailed`,
  `ConfigEntryAuthFailed`, `HomeAssistantError`, dispatcher, and
  `homeassistant.util.dt` (a MagicMock base class silently turns the subclass
  into a mock, which is why this code was untestable before).
- Removed `tests/test_coordinator.py`'s legacy module-level shim that
  **hard-overwrote** `sys.modules` (making results import-order-dependent);
  the suite now passes in any test-selection order.
- Replaced four grep-the-source pseudo-tests with behavioural equivalents
  (resolver robustness ×12, `_parse_event` guard, live-values type guard,
  store-corruption recovery was already covered). The remaining source-text
  checks are consolidated under `TestSourceContracts` with an explicit
  docstring on why they can't be behavioural without a full HA test harness.
- New API-client tests: events/latest-event shape normalisation (×8) and the
  pagination cap (×2); new public health-API tests (×3).
- **191 tests pass** (was 141), `ruff check` / `ruff format --check` clean.
  Coverage **44 %** (was 31 %): `coordinator.py` 48 % → **85 %**, `api.py`
  83 % → **85 %**. Coverage gate raised `fail_under = 30` → **40**.
- CI: added the missing `voluptuous` dependency to the test-install step in
  `.github/workflows/lint.yml` (test_api.py imports it directly).

### Not in this release (deferred, see audit report §Residual risks)
- Climate `HEAT` mode mapping (currently identical to `AUTO`), honouring the
  IDP's `expires_in`, JSON-decode-error retry classification, redaction of
  the `connection_health` diagnostics section, and 0 %-covered entity
  platforms (requires `pytest-homeassistant-custom-component`).

---

## [0.21.0] - 2026-07-08

### Added
- **Weather based control sliders** (`number.py`, new platform file): The
  Hoval Connect app added a "Weather based control" screen in 2026-07 with two
  Eco↔Comfort sliders — *by outside temperature* and *by solar radiation* —
  that were previously only settable on the heat pump itself. These are now
  exposed as HA `number` entities on each HK heating circuit's device:
  - **Weather based control: outside temperature** — slider, 0–100
  - **Weather based control: solar radiation** — slider, −10–0
  Both are `Config` category entities (hidden from the default dashboard,
  same visibility tier as other configuration-style entities) and use the
  same 1.5s debounce as the existing fan-speed slider so dragging doesn't
  spam the API.
- New API methods `get_circuit_settings()` / `update_circuit_settings()`
  (`GET`/`PATCH /v3/plants/{id}/circuits/{path}/settings`).
- New coordinator method `async_set_weather_impact()` with the same
  lock + optimistic-update + background-refresh pattern used by existing
  control actions (fan speed, program select, etc.), so the slider reflects
  your change immediately rather than waiting for the next poll.

### Notes
- Only enabled for HK (heating) circuits, matching the app screenshot this was
  built from. Not yet empirically verified against a live plant — if the
  cloud rejects the request shape, it will show up as a `HomeAssistantError`
  when moving the slider; please open an issue with the log line so the
  request shape/bounds can be corrected.
- 141 tests pass (22 new for this feature); `ruff check` / `ruff format
  --check` clean; coverage 30.70 % (gate: 30 %).
- No configuration changes required; after updating, restart Home Assistant
  (not just "reload integration" — see `CLAUDE.md` Live Testing section) and
  the two new entities will appear on each HK circuit's device.

---

## [0.20.0] - 2026-06-27

### Changed
- **WW water heater target-temperature step is now `0.5 °C` (was `1.0 °C`).**
  `WW_TEMP_STEP` in `water_heater.py` was lowered from `1.0` to `0.5`, which
  surfaces as `target_temp_step: 0.5` on the
  `water_heater.hoval_warmwasser_hot_water` entity.

  Rationale: the API already transmits the setpoint as a raw float
  (`api.set_temporary_change` → `body = {"value": <float>, ...}`), and per
  Hoval the WW circuit accepts half-degree resolution. The previous declared
  step of `1.0` mismatched that reality: any half-degree setpoint written by an
  automation (e.g. `47.5`) was quantised by the cloud and read back on the
  `temperature` attribute as a whole degree, which broke exact-equality
  "did the setpoint change?" guards in downstream automations (perpetual
  re-write / setpoint never settling). Declaring `0.5` makes the entity's
  contract match the device.

### Migration
- After updating, **restart Home Assistant** (not just "reload integrations")
  so the entity re-publishes its capabilities. Confirm the entity attribute
  reads `target_temp_step: 0.5` before relying on half-degree setpoints.
- No configuration changes are required. No entities are added or removed.
- If empirical testing shows the WW cloud still rounds half-degree values for
  your plant (read the `temperature` attribute back after writing `47.5` — it
  should report `47.5`, not `47`/`48`), then your plant's WW circuit is
  integer-only; in that case keep automations on whole-degree targets. This
  declaration change is safe either way.

### Notes
- No test or service-schema in the repository asserted the old `1.0` value, so
  this is an isolated, low-risk change. `tests/` and `services.yaml` were
  reviewed and require no updates.

## [0.19.0] - 2026-06-01
- Baseline reviewed for this audit. See repository history for prior changes.
