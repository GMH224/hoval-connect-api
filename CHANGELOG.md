# Changelog

All notable changes to the `hoval_connect` integration are documented here.
This project follows a loose [Semantic Versioning](https://semver.org/) scheme
while pre-1.0 (minor = behavioural/feature change, patch = internal fix).

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
