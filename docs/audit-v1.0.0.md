# Hoval Connect Integration — Architecture Change Audit (v1.0.0)

| | |
|---|---|
| **Subject** | `custom_components/hoval_connect` v0.24.0 → v1.0.0 |
| **Audit date** | 2026-09-10 |
| **Trigger** | User request, arrived at over several conversational turns rather than as a single spec: reduce this integration's standing cloud-API footprint, since a separate CAN-bus-based HACS integration already covers all telemetry this integration used to poll for |
| **Scope** | `coordinator.py` (major reduction), `api.py` (four dead methods removed), `sensor.py` (deleted), `binary_sensor.py` (new diagnostic entity), `const.py`, `__init__.py`, `config_flow.py`, full test suite |
| **Method** | Design worked out interactively with the user before any code was written (see §1); implementation then traced field-by-field against every entity platform to confirm the user's five must-not-break automation entities were structurally unaffected, before removing anything |

## 1. How the design was actually arrived at

Worth recording precisely, since the final design is narrower and more
specific than the first framing of the request:

1. **Initial idea**: randomize the polling interval (20–120 min) as "white
   noise" to make automated-traffic detection harder, in case that was
   contributing to a previous, unrelated 403 investigation (see
   `docs/audit-v0.24.0.md`).
2. **Reframed once the actual need was stated plainly**: the user doesn't
   need most of what this integration polls for at all — they read all
   telemetry via a separate CAN-bus HACS integration, and only need this
   integration for writes (heating program, DHW setpoint) plus knowing if
   the write path itself is still working.
3. **Further narrowed via direct questions** (see conversation): (a) no
   dynamic circuit discovery is needed — the heating hardware is static,
   and a reboot after a hardware change is acceptable; (b) the user already
   has (or believed they had) a manual "did the write take effect" check,
   so nothing new was needed there; (c) the actual want for the diagnostic
   is specifically "tell me if the cloud API has been down for more than
   2 hours."
4. Only after those four constraints were pinned down was the mechanism
   below designed. The random-jitter idea from step 1 was **not** carried
   into the final design — a fixed 30-minute interval was judged sufficient
   once the health check itself became this minimal (see §3).

## 2. What "telemetry" vs. "control" meant in practice

The dividing line that shaped every removal below:

| Data | Source call | Kept? | Why |
|---|---|---|---|
| Circuit existence, type, path, name | `get_circuits()` | Yes | Needed to know what to write to at all |
| Active program, target value, operation mode, per-circuit `hasError` | `get_circuits()` (same call) | Yes | Free — already in the circuits-list response |
| Program display names (`week1`/`week2` → "Winter"/"Sommer") | `get_programs()` | Yes | Feeds `select.py`'s displayed option names |
| weatherImpact Eco/Comfort current values | `get_circuit_settings()` | Yes | Feeds `number.py`'s slider current-value display, and is itself the thing being written |
| Live sensor values (temps, humidity, energy, hours) | `get_live_values()` | **No** | Pure display, zero write dependency; covered by the user's CAN-bus integration |
| Event history / latest event | `get_latest_event()`, `get_events()` | **No** | Same — display-only |
| Weather forecast | `get_weather()` | **No** | Same |
| Plant online/offline | `get_plants()` | Yes | Needed anyway for the health check; free |

Everything in the "No" rows was removed from `api.py` outright, not just
stopped being called — see §4.

## 3. The new mechanism

### 3.1 One-time startup discovery, refreshed only after a write

`_fetch_all_data()` still exists, does the same circuits/programs/settings
fetch it always did, minus the four telemetry calls above. What changed is
*when* it runs:

- Once, unconditionally, on the coordinator's first refresh
  (`_did_initial_discovery` flag).
- Again, whenever a write sets `_pending_full_refresh = True` immediately
  before calling `async_request_refresh()` (both `async_control_and_refresh()`
  and `async_set_weather_impact()` do this now).
- Never on a plain scheduled tick.

`_async_update_data()` is the dispatcher: it checks those two flags and
calls either `_fetch_all_data()` (clearing both on success) or the new
`_health_check()`. Both flags are deliberately **not** cleared on failure —
a failed full refresh must be retried as a full refresh next time, not
silently downgraded to a health check just because the previous attempt
didn't complete.

### 3.2 The health check itself

```python
async def _health_check(self) -> HovalData:
    plants_raw = await self.api.get_plants()
    # ...builds HovalData, carrying each plant's existing `circuits` dict
    # forward unchanged, refreshing only is_online and has_error.
```

One `get_plants()` call. Nothing else. `HEALTH_CHECK_INTERVAL = timedelta(minutes=30)`
is a fixed constant, not a config option — see §5 for why the old
user-configurable scan interval was removed entirely rather than just
given a new default.

### 3.3 The diagnostic

`HovalConnectionHealth.last_successful_contact_at` is updated by two call
sites: `record_poll_success()` (fires on either a successful health check
*or* a successful full refresh — anything that makes it through
`_async_update_data()` without raising) and directly inside
`async_control_and_refresh()` / `async_set_weather_impact()`, immediately
after their respective write calls succeed — deliberately *before* the
2-second-delayed post-write refresh, since the write succeeding is itself
proof of contact and shouldn't wait on a second round trip to be recorded.

`binary_sensor.*_cloud_api_problem` (device_class `problem`, diagnostic
category, always-available) is on when
`utcnow() - last_successful_contact_at > CLOUD_API_PROBLEM_THRESHOLD`
(2 hours, fixed). No contact ever recorded (e.g. a fresh install before
the first health check has run) is treated as a problem, not "unknown" —
there is nothing to report with confidence otherwise.

## 4. What was actually deleted, and why that's safe

- **`api.py`**: `get_live_values()`, `get_events()`, `get_latest_event()`,
  `get_weather()`. Confirmed via full-repo grep that nothing else called
  them before removal.
- **`coordinator.py`**: `HovalCircuitHealth` (per-circuit reliability
  tracking — existed only to track *live-values* fetch health, which no
  longer happens), `HovalEventData`, `HovalWeatherData`, `_parse_event()`,
  `_is_problem_event()`, and `resolve_fan_speed()`. That last one is worth
  flagging specifically: it was **dead code already**, before this
  release — a full-repo grep found it defined but never called anywhere,
  its only reference being a comment. It was removed here because its one
  dependency (`HovalCircuitData.program_air_volume`) was being removed
  anyway; it would have been equally correct to leave it as unreferenced
  dead code, but there was no reason to.
- **`sensor.py`**: deleted in full. Every entity in it read fields sourced
  from the four removed API calls, so keeping the file would have meant a
  platform that creates entities showing "unknown" forever. `Platform.SENSOR`
  removed from `PLATFORMS` in `__init__.py` accordingly.
- **`CONF_SCAN_INTERVAL` / `SCAN_INTERVAL_OPTIONS` / `DEFAULT_SCAN_INTERVAL`**
  (`const.py`), and the corresponding options-flow dropdown
  (`config_flow.py`) and `_get_scan_interval()` helper (`__init__.py`).
  See §5.

### 4.1 Confirming the user's five entities were unaffected — before removing anything

Before any deletion happened, the user gave the exact, authoritative list
of entities their automations depend on:

- `select.hoval_bodenheizung_program`
- `number.heating_hoval_bodenheizung_weather_based_control_outside_temperature`
- `number.heating_hoval_bodenheizung_weather_based_control_solar_radiation`
- `binary_sensor.hoval_e_604961729200133_error`
- `select.hoval_warmwasser_program`
- `water_heater.hoval_warmwasser_hot_water` (including its `reset_ww_boost`
  action)

Each was traced field-by-field before touching any code:

- `select.py` reads `circuit.active_program` and `circuit.program_names` —
  both still populated (from `get_circuits()` and `get_programs()`
  respectively, both retained). **No changes made to `select.py` at all.**
- `number.py` (the weatherImpact sliders) reads
  `circuit.weather_impact_supported` /
  `circuit.weather_impact_outside_temperature` /
  `circuit.weather_impact_solar_radiation` — all still populated from
  `get_circuit_settings()`, retained. **No changes made to `number.py`.**
- `water_heater.py` reads `circuit.operation_mode` and
  `circuit.live_values` (the latter degrades to empty gracefully — see
  below). Its `reset_ww_boost` action calls
  `coordinator.async_control_and_refresh()`, which was extended (contact
  recording, `_pending_full_refresh`) but not changed in its externally
  visible behavior. **No changes made to `water_heater.py`.**
- `binary_sensor.py`'s `HovalPlantError` unique_id is `f"{plant_id}_error"`,
  built from `plant_id` alone — unaffected by anything in this release. Its
  `is_on` property reads `plant.has_error`, whose *derivation* changed
  (circuits' `hasError` instead of event history) but whose *presence* and
  the entity's identity did not.
- `climate.py` and `fan.py` — not in the user's list, but traced anyway
  since they're the other write-capable platforms. Both read
  `circuit.live_values` for auxiliary display (current temperature,
  `hvac_action`), never for the actual write path (`async_set_temperature`,
  `async_set_hvac_mode`, etc., all call the API directly). With
  `live_values` now permanently `{}`, every `.get(...)` call against it
  returns `None`, which each of these files already has a fallback for
  (e.g. `climate.py`'s status line already falls back to
  `circuit.circuit_status` when `live_values.get("status")` is absent).
  **No changes made to either file.**

This tracing is why `HovalCircuitData.live_values` was kept as a field
(always empty) rather than removed outright: removing it would have forced
edits to `climate.py`/`fan.py`/`water_heater.py` for no functional gain,
each edit being a new place to introduce a mistake in files that otherwise
needed zero changes.

## 5. Why the polling-interval option was removed, not just given a new default

`SCAN_INTERVAL_OPTIONS` had real bug-fix history behind it (v0.19.0's
string-coercion fix, v0.23.0's missing-10-minutes fix). Simply pointing it
at `HEALTH_CHECK_INTERVAL` and leaving it user-configurable was considered
and rejected: there is no longer a meaningful trade-off for a user to make
by tuning this value. It doesn't affect how fresh telemetry is (there is
none polled), and it doesn't affect how quickly a write's effect shows up
(that's the immediate post-write refresh, unaffected by this interval). The
only thing a shorter interval would buy is faster detection of an outage —
which is a fixed design trade-off (§1, point 3: "more than 2 hours"), not
a per-user preference. Removing the option entirely, rather than
relabeling it, avoids a config UI element that implies a choice matters
when it no longer does.

## 6. Known limitations / residual risk

- **`plant.has_error`'s new, narrower definition.** Previously: any active
  blocking/locking/warning event in the plant's recent history (from
  `get_events()`/`get_latest_event()`). Now: any circuit reporting
  `hasError=true` in the circuits-list response. These are not guaranteed
  equivalent — an error condition that manifests only in event history and
  never sets a circuit's `hasError` flag would no longer be caught. This
  was an accepted trade-off (the user did not ask for event-level fault
  detail, only "do I need to take action"), not a verified non-issue.
- **No live end-to-end verification against the production API for this
  release.** Unlike the v0.24.0 investigation (which was validated live,
  interactively, step by step), this release's correctness rests on the
  offline test suite and manual field-by-field tracing described in §4.1.
  A real smoke test — confirming the health check fires on schedule, the
  diagnostic sensor's timing is correct, and a real write still updates
  the right entities — is recommended before treating this as fully
  proven in production.
- **Orphaned `sensor.*` entities are not cleaned up automatically.** Home
  Assistant does not remove entities from the registry just because a
  platform stops creating them; they will show as unavailable / "not
  provided by the integration" until the user removes them manually. No
  migration code was written to do this automatically, since silently
  deleting entities (and any history/statistics attached to them) without
  explicit user action was judged riskier than leaving them for manual
  cleanup.
- **`HEALTH_CHECK_INTERVAL` (30 min) and `CLOUD_API_PROBLEM_THRESHOLD`
  (2 h) are fixed constants, not validated against real-world outage
  patterns** — they were chosen for internal consistency (30 min gives
  the 2 h threshold four data points before tripping) rather than any
  empirical outage-frequency data, since none exists for this specific
  integration/account.

## 7. Test coverage

Every test that exercised removed behavior was removed alongside it
(`TestResolveFanSpeed`, `TestParseEvent`, `TestIsProblemEvent`,
`TestHovalCircuitHealth`, `TestParseEventGuard`, `TestEventEndpointNormalisation`,
the three `get_live_values` tests, `TestPercentageUnits`, and the
`scan_interval`-specific tests in `test_ha_compat.py`), each replaced with
a short comment pointing here rather than silently disappearing from the
diff. New coverage added specifically for this release:

- `TestHealthCheckDispatch` (`tests/test_coordinator_fetch.py`, 10 tests) —
  the core new mechanism: first call does full discovery; second call is a
  health-check-only (`api.calls == ["plants"]`); health check preserves
  existing circuits and only refreshes `is_online`/`has_error`;
  `_pending_full_refresh` correctly forces (and, on failure, keeps forcing)
  a real resync; a failed startup discovery is retried in full next time,
  not silently treated as done; a successful health check records contact.
- `TestHovalConnectionHealth`'s `last_successful_contact_at` coverage
  (`tests/test_coordinator.py`) — set, overwritten, persisted, restored,
  and included in the diagnostics dict.
- `TestScanIntervalRemoved` / `test_health_check_interval_is_fixed_not_user_configurable`
  (`tests/test_api.py`, `tests/test_ha_compat.py`) — guard that the old
  option's constants are actually gone and the new interval genuinely
  isn't read from config-entry options.

**Result at initial draft: 255 tests pass, ruff clean (lint + format), 60%
overall coverage.** Grew to 272 tests after the pre-deployment audit
response and post-audit bug report fix (§§9-10) added their own dedicated
coverage — see those sections for what's new. Final: 272 tests, ruff
clean, 60.4% overall coverage.
(`api.py` 85%, `coordinator.py` 83%; the entity-platform files remain in
the 30–40% range they were already at, since exercising them fully needs a
real Home Assistant test harness — an acknowledged, pre-existing gap, not
introduced by this release).

## 8. Rollback

This is the reason for the 1.0.0 version jump rather than 0.25.0: rollback
is simply not upgrading past v0.24.0, or reinstalling it, rather than
needing a patch release reverted. v0.24.0's `sensor.py` and scan-interval
option are fully intact there. No config-entry data or entity unique_ids
changed in a way that would make downgrading unsafe.

**One real caveat, found and fixed only after this release was first
written up (see the git-equivalent note in CHANGELOG.md): the persisted
health-counter store is not automatically forward/backward compatible on
its own.** `HEALTH_STORAGE_VERSION` was bumped 1 → 2 in this release.
Verified directly against Home Assistant's `Store` helper source
(`homeassistant/helpers/storage.py`), a version mismatch on load — in
*either* direction — raises rather than silently starting fresh, unless the
integration provides its own migration function (this one never has).
Left unhandled, that would have made the *whole integration* fail to load
after either upgrading into this release or rolling back out of it, not
merely lose historical counters. `async_setup_entry()` in `__init__.py` now
wraps the health-store load in a broad try/except specifically to guard
against this; on any load failure it logs a warning and starts with fresh
counters rather than blocking setup.

**This fix exists in v1.0.0. It does not exist in the currently-deployed
v0.24.0.** Practically: if v1.0.0 runs (writing a v2-format health file)
and is then rolled back to v0.24.0, that specific file could make v0.24.0
fail to load, since v0.24.0's `__init__.py` has no equivalent try/except.
The safe rollback procedure, until/unless v0.24.0 is repatched with the
same fix, is: downgrade the integration files as normal, and additionally
delete the single file `.storage/hoval_connect_health` from the Home
Assistant config directory before restarting. This does not touch config
entry data, options, or entity registrations — only the persisted health
counters, which restart from zero regardless (that was always the accepted
cost of this version bump, as noted above; this just makes the *mechanism*
of resetting them explicit and reliable instead of assumed).

**Update:** a separate v0.24.1 patch release was produced with the
identical try/except fix, specifically so v0.24.0 users have a safe
rollback target without needing the manual file-deletion workaround above.
See that release's own changelog entry.

## 9. Pre-deployment audit response (before first deployment)

An independent code audit (`hoval-connect-api-v1_0_0-ICS-test-report.md`,
10 findings, HVC-001 through HVC-010) was run against this release before
it was ever deployed. Per the reviewer's explicit instruction, every
finding was independently re-verified against the actual code — reading
the real source, and in one case (HVC-003) the actual OpenAPI spec — rather
than accepted on the audit's word. All 10 were confirmed real. The audit's
own severity framing needed correction in several cases: HVC-001, HVC-004,
HVC-006, and HVC-007 turned out to be pre-existing bugs already present in
v0.24.0, not introduced by this release, and HVC-003 was substantially the
user's own explicitly-agreed design tradeoff (reduced telemetry from
climate/fan/water-heater, in exchange for a much lighter integration) rather
than an oversight — though its specific fix suggestion turned out to be
genuinely valuable and free once checked against the spec. HVC-002 and
HVC-008 were confirmed as new, genuinely introduced by this release's
health-check/optimistic-update redesign.

### 9.1 HVC-004 — `get_circuits()` failed open on unrecognised shapes

Confirmed by reading `api.py`: a dict response without a list `content` key
became `[]` via `.get("content", [])`; any other non-list response also
became `[]`. Pre-existing in v0.24.0 (identical code), but materially more
impactful under this release's architecture: v0.24.0's frequent polling
would self-correct within the next cycle; this release fetches circuits
once at startup, so a single malformed response there could leave every
circuit entity missing until a manual restart, since there would be no
entities left to trigger the write-based refresh that would otherwise fix
it. Fixed: `get_circuits()` now raises `HovalApiError` for any response
that isn't a plain list or a dict with a list `content` key, which the
coordinator's existing circuits-list error handling already surfaces
correctly (entities go `unavailable`, HA retries on its own schedule)
rather than silently proceeding with zero circuits.

### 9.2 HVC-005 — API session leaked on setup failure

Confirmed by reading `__init__.py`: `entry.runtime_data` (the only other
thing that references the constructed `HovalConnectApi`, via
`async_unload_entry`'s `api.aclose()` call) was assigned only after
`async_config_entry_first_refresh()` succeeded. Any exception before that
point — an auth failure, a timeout, a circuit-list error — meant the
`requests.Session` and its connection pool were never explicitly closed,
relying on Python's garbage collector at some non-deterministic later
time. Given Home Assistant retries a failed config entry setup
automatically, a persistently-failing entry (e.g. wrong credentials) could
accumulate several abandoned sessions before GC caught up. Fixed: the span
from `HovalConnectApi(...)` construction through `entry.runtime_data`
assignment is now wrapped in `try/except BaseException: await
api.aclose(); raise` — cleanup on any failure, ownership transfers to
`entry.runtime_data` (and its own `async_unload_entry` cleanup path) only
on success.

### 9.3 HVC-002 — post-write refresh race could drop a second write's confirmation

Confirmed by tracing `_async_update_data()` and `_fetch_all_data()`
directly: `do_full_refresh` was captured once, before the (potentially
slow) fetch started, and `_pending_full_refresh` was unconditionally reset
to `False` after that fetch completed — regardless of whether a second
write had set it back to `True` while the first fetch was still in
flight. If Home Assistant's coordinator debouncer schedules a follow-up
`_async_update_data()` call because of that second write's
`async_request_refresh()` (a reasonable assumption given
`Debouncer`'s trailing-edge semantics, though not independently verified
against a live HA instance for this specific interaction), that follow-up
call would see the flag already cleared and silently downgrade to a
health-check-only cycle — leaving the second write's actual effect
unconfirmed for up to `HEALTH_CHECK_INTERVAL`. A related, more directly
verified instance of the same root cause: `self._mode_override.clear()`
ran unconditionally at the end of every successful full fetch, which would
wipe the optimistic override for a *different* circuit if a write for that
circuit landed while the fetch was already in flight — since the fetch's
own data snapshot was taken before that write happened, clearing the
override left the entity showing genuinely stale (not just "not yet
confirmed") data until the next successful refresh.

Fixed by replacing the bare `_pending_full_refresh` bool with
`_pending_full_refresh_since`, a monotonic timestamp: `_async_update_data`
captures the timestamp it's serving before starting the fetch, and only
clears the field afterwards if nothing newer arrived during that window.
The mode-override clear is similarly scoped: `_fetch_all_data` now takes a
`fetch_started_at` parameter and only clears overrides timestamped before
it. Both mechanisms share the same underlying principle — never let a
fetch's completion erase state that was written *after* that fetch's
snapshot was taken, since the fetch could not possibly have reflected it.

Regression tests added specifically simulate the race (a second write's
timestamp survives an earlier write's refresh completing; an override for
a different circuit set mid-fetch survives; an override set before a
refresh started still correctly clears).

### 9.4 HVC-008 — cloud-problem sensor and optimistic state delayed after a write

Confirmed by reading `binary_sensor.py`: `HovalCloudApiProblem` computes
its state live from `last_successful_contact_at` on every property access,
but as a `CoordinatorEntity` it only actually re-renders (pushes new state
to Home Assistant's state machine, history, and any bound automations)
when the coordinator notifies its listeners — which otherwise only
happens after a full `_async_update_data()` cycle completes, not
immediately when a write updates that timestamp. The same gap applied to
every other entity's optimistic mode-override state. Fixed: both write
paths (`async_control_and_refresh`, `async_set_weather_impact`) now call
`self.async_update_listeners()` immediately after recording a successful
contact — a notification-only call that does not touch `self.data` or
trigger a new fetch.

### 9.5 HVC-003 — free telemetry from data already being fetched

The bulk of this finding (climate/fan/water-heater showing reduced
current-value detail) is the explicitly agreed design of this release, not
a defect — see §§1-2 above. Its specific fix suggestion was checked
against `docs/openapi-v3.json`'s `CircuitV3DTO` schema and confirmed:
`actualValue` (a number) and `temporaryChange` (null, or a
`TemporaryChangeV3DTO` object when a party/away override is active) are
both already part of the circuits-list response this release still
fetches for control purposes — mapping them into `HovalCircuitData` costs
zero additional API calls. Added `actual_value` and
`temporary_change_active` fields; climate/fan/water-heater now check them
before falling back to the (now-permanently-empty) `live_values` lookups,
which are left in place rather than removed for this release.

### 9.6 HVC-006 — missing `operationMode` reported as an active state

Confirmed in `climate.py`, `fan.py`, and `water_heater.py` by tracing the
actual boolean logic: `operationMode` is not a documented-required API
field, and in each file, `mode is None` fell through to whatever branch
handled "not standby" — `HVACMode.HEAT` in climate, fan `is_on = True`,
`_OP_HEAT_PUMP` in water_heater — reporting an active state with no actual
basis for it, rather than "unknown". Pre-existing in all three files since
before this release. Fixed: all three now return `None` (climate, water
heater) or use the pattern each already used for other unknown states,
consistent with what their respective HA entity base classes support.

### 9.7 HVC-007 — declared temperature bounds never enforced

Confirmed: `climate.py` and `water_heater.py` both declare
`_attr_min_temp`/`_attr_max_temp`, but neither's `async_set_temperature`
checked a requested value against them before sending it to the API.
Pre-existing since before this release. Fixed with a new shared
`clamp_temperature()` helper in `const.py` (rejecting non-finite input —
NaN, +-infinity — with `ValueError`, since clamping those is not
well-defined and almost always indicates a caller bug), used by both
entities' `async_set_temperature`. Clamped rather than rejected, matching
this codebase's existing convention for other out-of-range input (HV fan
speed, weather-impact values) rather than introducing a different failure
mode for temperature alone.

### 9.8 HVC-001 — caches/overrides keyed by circuit path alone

Confirmed by reading every relevant dict definition in `coordinator.py`:
`_mode_override`, `_program_cache`, `_settings_cache`, and
`_weather_impact_override` were all keyed by bare `circuit_path`, and
`async_set_weather_impact`'s "find this circuit" fallback scanned every
plant's circuits looking for a matching path rather than going straight to
the known plant. Pre-existing since v0.24.0 (confirmed by diffing against
that release). Zero practical impact for a single-plant account — the only
kind tested live — but nothing in the API guarantees an account stays
single-plant (e.g. a future plant split, such as AC being separated from
heating, was the scenario the user raised). Fixed: every one of those four
dicts is now keyed by `(plant_id, circuit_path)`; `set_mode_override()`,
`get_mode_override()`, and `get_weather_impact_override()` all take
`plant_id` as an explicit parameter; `async_control_and_refresh()`'s
parameters are now keyword-only after `coro` specifically so any call site
that wasn't updated for this change fails loudly (`TypeError`) rather than
silently misrouting a positional argument. `async_set_weather_impact`'s
circuit lookup now goes directly to `self.data.plants.get(plant_id)`
instead of scanning every plant.

A dedicated test class, `TestMultiPlantCircuitPathCollision`, constructs
two plants that both report a circuit at the same path and confirms mode
overrides, weather-impact overrides, and program/settings caches all stay
correctly isolated per plant.

### 9.9 / 9.10 — HVC-009, HVC-010 (minor cleanup)

`tools/mutation_check.py` had two mutations targeting the now-deleted
`sensor.py` (one retargeted to `number.py`, which still has the relevant
pattern; one removed outright, since its guarded behavior — `PERCENTAGE`
vs `UnitOfRatio.PERCENTAGE` — no longer exists anywhere in the codebase)
and a manifest-version mutation whose "find" string no longer matched the
real value. `strings.json` and `translations/en.json` both still had
`scan_interval` keys left over from before that option was removed
entirely; deleted.

## 10. Post-audit bug report — weather-impact override could mask a silently-failed write

Filed separately from the audit above, and verified live against a real
v0.24.1 deployment before being reported — this is empirical evidence of a
production incident, not a hypothetical: an automation's own idempotency
guard (`if states(entity) != target: number.set_value(...)`) read a masked
optimistic value, concluded no write was necessary, and skipped resending,
while the physical device remained on its previous configuration. The
mismatch was undetectable from Home Assistant's UI or logs at default log
level.

Traced precisely before fixing, since the report's own theory (a failed
write's error being swallowed) didn't fully match the code: `_send_value`
in `number.py` does correctly raise `HomeAssistantError` on an outright API
failure, and does not set an optimistic override in that case (the override
is only set after `async_set_weather_impact` returns successfully) — so a
genuine API error already correctly reverts the displayed value to the
last-polled data, without masking. The real gap was narrower and, in
production terms, worse: a write that returns success from the API's
perspective but is not actually applied by the device (accepted, then
silently overridden, ignored, or reverted) sets the optimistic override as
designed, and nothing ever reconciles that override against a genuine
fresh poll — `_weather_impact_override` is deliberately never cleared on a
successful poll (unlike `_mode_override`), only by its own 120-second TTL.

Worse still, once traced fully: `async_set_weather_impact` was also
optimistically pre-populating `_settings_cache` with its own just-written
guess immediately after every write. Since the scheduled post-write
verification refresh (`_do_refresh`, 2 seconds after the write) checks that
same cache's freshness to decide whether to actually re-fetch settings from
the API, this pre-population made that check see a "fresh" cache and skip
the real verification GET entirely — for up to `CIRCUIT_SETTINGS_CACHE_TTL`
(10 minutes), not just the override's 120 seconds. In effect, the
verification refresh that exists specifically to confirm a write never
actually verified anything; it just re-confirmed the write's own
assumption.

**Fix**: removed the optimistic `_settings_cache` write in
`async_set_weather_impact` entirely — the override alone already covers
the UI during the few-second gap before the real verification fetch lands,
so there was no need for the cache write to do that job too, and it was
actively harmful. Added genuine reconciliation: when `_fetch_all_data`
receives a real (not cached) settings response for a circuit that supports
weather impact, it now clears that circuit's optimistic override if the
override is older than when this fetch started — the same race-safe
timestamp guard built for HVC-002 in §9.3, so a write landing for the same
circuit while this exact fetch is in flight is correctly left alone rather
than wiped by a snapshot that couldn't have reflected it. This closes the
loop within the ~2-second post-write refresh window in the common case,
rather than the previous up-to-10-minutes (when the cache masked
verification) or up-to-2-minutes (the override's own TTL, the report's
stated worst case).

**Not fixed, and deliberately left as a documented limitation**: this is
still an optimistic-write architecture, not a verified one. If the
post-write refresh itself fails or times out (a real possibility — it's a
best-effort background task whose own failures are silently discarded, by
existing design), reconciliation is deferred to whenever the next full
refresh happens to run, which — outside of another write — could be a long
time under this release's architecture, since nothing else triggers a full
refresh on a schedule. Automations with a hard correctness requirement
should still follow the bug report's own stated workaround: send the
desired value unconditionally rather than gating on current entity state.

## 11. Second independent audit round

A follow-up independent ICS-style review (`Hoval_Connect_v1_0_0_ICS_Bugfix_Test_Report.md`)
was scoped deliberately narrowly — production/runtime code only, with
`tests/**` and prior `docs/audit-*.md` explicitly excluded as correctness
evidence — and found 9 further issues, HVC-ICS-001 through HVC-ICS-009.
All 9 were independently re-verified against the actual code (reading the
real control flow, tracing exact boolean/exception logic, and in one case
checking the actual Python `min`/`max` NaN semantics directly) before
acting on any of them; every one was confirmed real. 8 of 9 were fixed;
the 9th is a documented risk, not a confirmed bug, and was deliberately
left as documentation only.

### 11.1 HVC-ICS-001 — topology discovery could stall permanently

Confirmed by reading `_health_check()` and `_fetch_all_data()` together:
an offline plant at initial discovery gets stored with `circuits={}` and
`continue`s past circuit discovery entirely; `_health_check()` — the only
thing that runs on a schedule — always carries a plant's `circuits` dict
forward unchanged from the prior snapshot, and never calls
`get_circuits()` or fires `SIGNAL_NEW_CIRCUITS` (confirmed: that signal
has exactly one dispatch site in the whole file, inside
`_fetch_all_data()`). A brand-new plant appearing after startup hits the
identical dead end. The only recovery path was an unrelated write to some
other, already-known circuit — which might never happen, e.g. if this is
the only plant on the account and it has zero working entities yet.

Directly relevant to a scenario the user raised earlier in this project's
history as plausible (not certain): Hoval splitting an account into
multiple plants (e.g. separating AC from heating).

Fixed: `_health_check()` now compares each plant in the fresh
`get_plants()` response against the previous snapshot. If a plant has no
prior record at all, or was offline before and is online now, it treats
this as a topology change and immediately calls `_fetch_all_data()`
instead of returning its own (circuits-less) result — closing the gap
within the same 30-minute cycle it's first observable, at the cost of one
extra `get_plants()` call in that specific (rare) case. Going offline is
deliberately NOT treated as a topology change — there's nothing new to
discover in that direction, and the existing light check already handles
it correctly (`is_online` updated, circuits carried forward unchanged).

Four dedicated tests (`TestTopologyChangeDetection`) cover: offline→online
triggers a real fetch; a new plant appearing triggers one; going offline
does NOT trigger one; and the ordinary no-change case still stays a light
check (a regression guard against the fix over-triggering on every tick).

### 11.2 HVC-ICS-002 — fan turn-off race with a queued debounced write

Confirmed by reading `fan.py`: `async_set_percentage()` queues a 1.5s
debounced background task; the zero-percent path cancels it explicitly,
but `async_turn_off()` — a direct, separate entity action — did not.
Sequence: set 50%, then turn off within 1.5s; the turn-off's standby
command sends first, then the still-queued 50% write fires afterward and
turns the fan back on. `async_turn_on()` had the same gap in the other
direction (a stale queued percentage could override a fresh turn-on).
Fixed: both now call `_cancel_debounce()` (and clear `_pending_percentage`)
as their first action.

### 11.3 HVC-ICS-003 — water heater `high_demand` was not actually selectable

Confirmed by reading `water_heater.py`: `_attr_operation_list` advertised
`high_demand` as selectable, `current_operation` correctly reports it when
`circuit.temporary_change_active` is true, but `async_set_operation_mode`
routed both `heat_pump` and `high_demand` to the identical branch —
`reset_circuit()`, which resumes the normal schedule. Selecting the boost
mode did the opposite of what it implied. Not fixable by "implementing the
real action" in that method, because `async_set_operation_mode` only
receives a mode string, never a numeric value to boost to — the real boost
mechanism is `async_set_temperature` (a genuine `WaterHeaterEntityFeature.
TARGET_TEMPERATURE` call that already correctly sends a temporary-change
command with an actual value), which was already implemented correctly
and is what the existing `reset_ww_boost` action already assumes exists.
Fixed: removed `high_demand` from `_attr_operation_list` (current_operation
can still report it — that's a real, correctly-derived observable state,
just never a valid thing to *select*); `async_set_operation_mode` now
raises a clear error for anything outside `{heat_pump, off}`, pointing at
`async_set_temperature` as the actual way to start a boost.

### 11.4 HVC-ICS-004 — NaN silently became a valid clamp value

Confirmed mathematically: `min(WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MAX,
float("nan"))` returns `WEATHER_IMPACT_OUTSIDE_TEMPERATURE_MAX` in Python,
not an error or NaN — `clamp_hv_air_volume()`,
`clamp_weather_impact_outside_temperature()`, and
`clamp_weather_impact_solar_radiation()` all lacked the `isfinite()` guard
`clamp_temperature()` already had (added in the first audit round, §9.7 —
this gap was simply not applied consistently to the other three clamp
functions at the time). Fixed by applying the identical guard to all
three. This surfaced a second-order gap while fixing it: the new
`ValueError` from `clamp_hv_air_volume()` (in `fan.py`'s `_send_percentage`,
itself called from a fire-and-forget background task) and from the
weather-impact clamps (inside `resolve_weather_impact_update()`, called
from `async_set_weather_impact()`) were not caught anywhere, which would
have made them invisible unhandled exceptions in a background task —
exactly the "audit finding F5" pattern this codebase already fixes
elsewhere. Both call sites now catch `ValueError` and convert it to a
visible `HomeAssistantError`/`HovalApiError` at the appropriate layer.

### 11.5 HVC-ICS-005 — malformed nested objects could delete a whole circuit

Confirmed by reading both sites: `circuit.get("airQuality") or {}` and
`settings["weatherImpact"] or {}` both pass a truthy non-dict value
through unchanged (a string, list, or number is truthy, so `or {}` never
triggers), and the following `.get(...)` call on that non-dict value
raises `AttributeError`. Since `_fetch_circuit()` runs under
`asyncio.gather(..., return_exceptions=True)` in `_fetch_all_data()`, and
neither site has the same exception-isolation `try/except` the programs
block already has, that exception drops the ENTIRE circuit from
`plant_data.circuits` — not just the one optional feature that was
actually malformed. Fixed: both sites now use an explicit
`isinstance(x, dict)` check instead of `or {}`, so malformed optional
metadata degrades that one feature and leaves the rest of the circuit
(including its write capability) intact.

### 11.6 HVC-ICS-006 / HVC-ICS-007 — retry budget and auth-retry inconsistency

Confirmed by tracing `_request()`'s exact control flow: on a 401, the
method called `return await self._request(..., _retry=False)` —
recursing into a BRAND NEW invocation with its own fresh `for attempt in
range(_MAX_RETRIES)` loop, rather than continuing within the current one.
A 401 followed by one transient error (the audit's own example: 401, then
500) could therefore take 3 total HTTP attempts against a budget
`_MAX_RETRIES`'s own comment explicitly documents as 2 TOTAL, not 2
*additional* retries. Separately, `headers = await self._headers(plant_id)`
was called before the method's `try:` block even began — outside every
retry/backoff mechanism entirely — so a transient network problem while
acquiring or refreshing the ID token or plant-access-token (both real
network calls, via `_get_id_token()`/`_get_plant_access_token()`) failed
the whole request immediately on the very first hiccup, regardless of
`_MAX_RETRIES`. The two findings are opposite failure modes of the same
underlying design flaw: retry accounting split across a recursion
boundary and a loop-external step, rather than governed by one shared
mechanism.

Fixed together, since fixing one properly requires touching the same
code: removed the recursive self-call entirely. A 401 now does `continue`
within the same loop (guarded by a `token_refreshed` flag so at most one
401-triggered refresh is attempted, matching the old `_retry` flag's
intent), so one shared `attempt` counter governs both the 401 refresh and
any additional transient-error retries — capping total HTTP attempts at
`_MAX_RETRIES` no matter which kind of failure occurs first or in what
order. Header acquisition now has its own try/except inside the same
loop: a genuinely bad credential (`HovalAuthError`, from the IDP's own
400/401/403 response) still fails immediately, since retrying bad
credentials cannot help, but a transient connection problem acquiring a
token (`HovalApiError`, from `_get_id_token()`'s own network-exception
handling) is now retried with the same backoff as everything else.

Four new tests cover: the audit's exact 401-then-500 sequence stops at
`_MAX_RETRIES` total attempts; a transient error acquiring auth headers
is retried and can still succeed; a persistent one exhausts the budget
correctly; and a genuine bad-credentials response is never retried at all.

### 11.7 HVC-ICS-008 — "resume" could silently switch an active week2 schedule to week1

Confirmed: `api.reset_circuit()` defaults to `program="week1"`, and all
three "resume" callers — fan's "Resume time program" turn-on option,
climate's AUTO mode, water heater's `heat_pump` reset — called it without
ever passing the circuit's actual `active_program`. A circuit legitimately
running on week2 would have its schedule silently switched to week1 by an
action whose entire premise is "resume what's already running," not
"switch to a specific one." Fixed with a new pure helper,
`resolve_resume_program()`, returning `"week2"` only when the circuit's
last-known `active_program` is already `"week2"` and `"week1"` for
everything else (preserving the existing default for constant/ecoMode/
standby/unknown); wired into all three call sites.

### 11.8 Plant-name fallback inconsistency (found while verifying HVC-ICS-005)

`_fetch_all_data()` used `plant.get("description", plant_id)`, while
`_health_check()` used `raw.get("description") or (prior.name if ... else
plant_id)`. `dict.get(key, default)`'s default only applies when the key
is *absent* — an explicit `"description": null` response makes the first
form return `None`, violating `HovalPlantData`'s declared `name: str`,
while the second form's `or` correctly falls back for any falsy value.
Fixed by aligning `_fetch_all_data()` to the same `or`-based pattern.

### 11.9 HVC-ICS-009 — v3/v4 temporary-change API compatibility risk (not fixed)

Confirmed as described: the bundled `docs/openapi-v3.json` contains both
a `/v3/.../temporary-change` and a `/v4/.../temporary-change` endpoint for
the same operation, with materially different request DTOs (v3:
`{value, duration: fourHours|midnight}`; v4: `{type: endOfPhase|duration,
value, duration: number|null}`). The runtime hard-codes v3.

**Deliberately not changed.** There is no live evidence Hoval has actually
deprecated or is enforcing v4 for this endpoint — v3 has been confirmed
working in every live investigation this project has done, as recently as
the v0.24.0 API-blocking investigation earlier in this project's history.
Switching to v4 speculatively, without a live signal that v3 has actually
stopped working, would be exactly the kind of unvalidated assumption this
project has been burned by before (see docs/audit-v0.24.0.md's entire
investigation). This is recorded here as a known, real risk to watch for
— specifically, an HTTP 404/410 on the v3 temporary-change endpoint, or
Hoval documentation/changelog mentioning its removal — not as something to
act on preemptively.

## 12. Test coverage (updated)

Grew from 272 to 280 tests across this second audit round. Final: 280
tests pass, ruff clean (lint + format), 60.5% overall coverage
(`api.py` 86%, `coordinator.py` 88%).

## 13. Pre-deployment audit response, third round ("more" report)

A follow-up independent ICS-style review (`Hoval_Connect_v1_0_0_ICS_Bugfix_Test_Report_1_.md`)
found 12 further issues plus 2 documentation observations. All 14 were
independently re-verified against the actual code — including checking
`docs/openapi-v3.json`'s `required` list directly for finding #1, and
tracing exact control flow (not just reading docstrings) for the retry/auth
findings — before acting on any of them. All were confirmed real.

### 13.1 Finding #1 — `isSelectable` vs `selectable`

Confirmed directly against the schema: `CircuitV3DTO.required` includes
`isSelectable`; `selectable` is present as a property but not required.
The coordinator's circuit filter (`circuit.get("selectable", False)`)
would silently treat a response that omits the optional legacy field as
"not selectable", dropping an otherwise-valid HV/HK circuit with no error.
Fixed with a new `_is_circuit_selectable()` helper: prefers `isSelectable`
when present, falls back to `selectable` only when `isSelectable` is
absent. Both usage sites (the actual filter and a debug log-count
expression) updated.

### 13.2 Finding #2 — `get_plants()` failed open

Confirmed: identical anti-pattern to `get_circuits()`'s pre-fix behavior
(§9.1) — a dict response with no `content` key became `[]` via
`.get("content", [])`. Missed when fixing the sibling method because the
two methods were not reviewed together at the time. Now raises
`HovalApiError` for any response that isn't a plain list or a dict with a
list `content` key, at any page of pagination.

### 13.3 Finding #3 — anomalous empty response could wipe topology

Confirmed: both `_health_check()` and `_fetch_all_data()` rebuild their
plant dict from whatever `get_plants()` returns, unconditionally — a
single anomalous-but-well-formed empty response (a transient backend
hiccup returning `[]` instead of an error, for instance) would be treated
as authoritative evidence the account now has zero plants, silently
wiping every entity while the poll itself was recorded as a *successful*
contact. A fail-open-into-a-destructive-state problem, not a display
glitch. Fixed with `_guard_against_empty_plants()`, shared by both fetch
paths: an empty response is only trusted immediately when no plants were
previously known (a genuinely new account has nothing to lose); once
plants are known, `_EMPTY_PLANTS_CONFIRMATION_THRESHOLD` (2) consecutive
empty responses are required before accepting the wipe as genuine, so a
real "account now has zero plants" transition still eventually takes
effect rather than being permanently refused.

### 13.4 Finding #4 — stale settings cache in the weather-impact write path

Confirmed: the write-merge logic read `_settings_cache` with no
`time.time() - cached[1] > ttl` check at all, unlike the read/fetch path,
which does check it. **Correction to the audit's own analysis**: traced
through its stated failure scenario (another client changes a sibling
value externally; the stale cache reverts it on the next write) carefully,
and found that the audit's primary suggested fix — fall through to
`circuit.weather_impact_*` when the cache is stale — would **not** have
closed it. That field is populated from the exact same cache in the exact
same code path, in lockstep, so in the scenario described both sources
would be equally stale; falling through to one from the other provides no
new information. Implemented the audit's own secondary "for maximum
correctness" suggestion instead: when the cache is missing or stale, do a
genuine fresh `get_circuit_settings()` call — awaited, inside
`control_lock` — before merging, falling back to the last-known value only
if that fresh call itself fails.

### 13.5 Finding #5 — malformed cached weatherImpact crashes a write task

Confirmed: `cached[0].get("weatherImpact") or {}` in the write-merge path
is the identical `or {}` anti-pattern already fixed for the READ path in
the second audit round (HVC-ICS-005) — missed in this one additional
occurrence. A truthy non-dict value would raise `AttributeError` inside a
fire-and-forget background task with nothing to catch it. Fixed with a new
`_extract_weather_impact()` helper using an explicit `isinstance` check,
used by the new fresh-GET path from finding #4 above.

### 13.6 Finding #6 — plant-access-token 401 bypassed the retry path

Confirmed: `_get_plant_access_token()` raised `HovalAuthError` immediately
on any 401 from the plant-settings endpoint, even though the main request
path (`_request()`) treats the identical signal — a 401 — as "the bearer
token simply expired, refresh and retry", not a credentials problem. Given
the second audit round's fix (§11.6) specifically made `_request()`'s
header-acquisition step retry `HovalApiError` but NOT `HovalAuthError`,
this method raising the latter for what could just be a stale-token race
meant it could never benefit from that retry regardless. Fixed with a
self-contained, single-retry loop (not reusing `_request()`'s own loop, to
avoid a circular dependency: `_headers()` calls this method, which is
itself called from inside `_request()`'s loop) — a second consecutive 401
(even after a fresh ID token) is still treated as genuinely bad
credentials, matching the main path's own single-refresh semantics.

### 13.7 Finding #7 — untracked background refresh tasks

Confirmed: both `async_control_and_refresh()` and
`async_set_weather_impact()` scheduled their post-write refresh via a bare
`hass.async_create_task(...)` with the return value discarded — no
tracking structure existed anywhere. A write followed quickly by a
config-entry reload/unload could leave that task asleep through its
2-second settle delay past the point `async_unload_entry()` had already
closed the API session. Fixed with `_create_tracked_task()` (adds to a new
`self._background_tasks` set, removed automatically via a done-callback)
and `coordinator.async_shutdown()` (cancels any still-pending ones,
awaited to let their cancellation actually complete), called from
`async_unload_entry()` before `api.aclose()`.

### 13.8 Finding #8 — "resume" could act on a stale cached schedule

Confirmed as a real, if narrower, follow-on limitation of the previous
round's own fix (HVC-ICS-008, §11.7): `resolve_resume_program()` read
`circuit.active_program` from the coordinator's last-known snapshot, which
is only refreshed at startup or after a write — never on the routine
30-minute health check. A schedule changed externally (the Hoval app,
another client) since the last refresh could make "resume" reactivate the
wrong week with just as much false confidence as the original,
un-patched bug. Fixed properly this time: since this is a write-triggered
action, consistent with this integration's actual design principle ("no
data fetched on a schedule, but writes may fetch what they specifically
need"), it now does one fresh, targeted `get_circuits()` call immediately
before resolving, falling back to the cached value only if that fresh
fetch itself fails.

### 13.9 Finding #9 — diagnostics redaction gaps

Confirmed on both counts. `async_redact_data(asdict(coordinator.data),
REDACT_COORDINATOR)` redacts dictionary VALUES for matching KEY NAMES —
it was never going to touch `coordinator.data.plants`'s own keys (the
plant IDs themselves) or each plant's `circuits` dict keys (the circuit
paths themselves), since those are keys of the mapping, not values under a
field named e.g. "plant_id". Separately, `HovalCircuitData.path` — a real
field — was never redacted at all; the old `REDACT_COORDINATOR` set
included `"source_path"`, a name left over from the removed
`HovalEventData` (deleted in the original v1.0.0 telemetry removal), not
the actual `"path"` field this dataclass has used all along. Third,
`connection_health` — including `last_error.message`, a free-form string
that several `_LOGGER` call sites in this codebase format a circuit path
or plant ID directly into — was passed straight through with no redaction
call at all.

Fixed with a full rewrite of `diagnostics.py`: `_anonymise_coordinator_data()`
rebuilds the plants/circuits structure with indexed placeholder keys
("plant_1", "circuit_1", ...) instead of the real identifiers, in addition
to the existing field-level `async_redact_data` pass; `path` is redacted
explicitly; and a new `_redact_identifiers_in_text()` helper does a plain
substring replacement of this specific installation's own known plant IDs
and circuit paths inside `last_error.message` (necessary because
`async_redact_data` cannot help with substrings inside a larger string,
only whole dict values).

`tests/test_diagnostics.py` was rewritten from scratch. The previous
version only asserted that certain key names were present in the
redaction sets — never actually calling the diagnostics function and
inspecting real output — which is exactly how the underlying bug went
unnoticed: the sets themselves looked reasonable in isolation. The new
version drives `async_get_config_entry_diagnostics()` end-to-end with a
real (not mocked) `async_redact_data` implementation matching HA's actual
documented behavior.

**Incidental fix found while writing these tests**: the new test file's
`sys.modules.setdefault("voluptuous", ha_mock)` (copied from the file it
replaced) silently poisoned `voluptuous` for `config_flow.py`'s later
import in the same pytest session whenever `test_diagnostics.py` happened
to be collected first — `setdefault` only sets when the key is absent,
and nothing before it was guaranteed to have already imported the real
package. This produced a `StopIteration` failure in a completely
unrelated file (`test_ha_compat.py`) that looked, at first, like an
unrelated regression. Removed the unnecessary stub (`diagnostics.py`
never imports `voluptuous` at all) rather than working around the
symptom.

### 13.10 Finding #10 — duplicate program names ambiguous

Confirmed: if a user names both week1 and week2 identically in the Hoval
app, the select entity's option list contained a literal duplicate string,
and the reverse (display name → API key) lookup always resolved to
whichever key was checked first — selecting the second identical entry in
the dropdown silently activated the first one instead. Fixed by extracting
a new standalone pure function, `resolve_program_display_names()`, out of
the entity class specifically so this disambiguation algorithm is directly
unit-testable (`tests/test_select.py`, new file) without needing a full
entity/coordinator/config-entry stack. Any name colliding across more than
one key gets the API key appended (e.g. "Summer (week1)" / "Summer
(week2)"); a name that's already unique is returned unchanged.

### 13.11 Finding #11 — CI missing the `requests` dependency

Confirmed: `.github/workflows/lint.yml` installed `aiohttp` — a leftover
from before the v0.24.0 transport rewrite — instead of `requests`, which
the production code has actually required since that release. Practical
risk was lower than a strict reading suggests (`requests` is close to
universally pre-installed on GitHub's standard runner images), but this
was implicit/lucky rather than correct. Fixed to install `requests`
explicitly.

### 13.12 Finding #12 — obsolete example client

Confirmed, and worth having prioritized: `examples/hoval_client.py` sent
no custom `User-Agent` at all — running it unmodified today would
reproduce the exact blanket HTTP 403 documented in `docs/audit-v0.24.0.md`,
the single most effort-intensive investigation in this project's history.
It also implemented `get_live_values()`/`get_weather()`/`get_plant_events()`
(telemetry endpoints this integration deliberately stopped polling in the
original v1.0.0 redesign) and checked the legacy `selectable` field
instead of `isSelectable` (finding #1 above). Rewritten to demonstrate the
actual v1.0.0 control-only surface: auth with the correct `User-Agent`,
plant/circuit discovery using the guaranteed field, reading programs and
weather-impact settings, and the write operations.

### 13.13 Observation A — stale README sections

Confirmed: the troubleshooting section referenced "circuit-level sensors"
and a "weather, events" entity group, both removed entirely by the
original v1.0.0 telemetry removal; the known-limitations section listed a
"BL energy sensors" bullet describing sensors that no longer exist.
Corrected both sections to describe the actual current entity set and
added a limitations bullet stating plainly that telemetry sensors are
gone since v1.0.0, rather than leaving that only implicit elsewhere in the
document.

### 13.14 Observation B — stale translation strings

Confirmed: `strings.json` and `translations/en.json` both still had a
complete `entity.sensor` translation block for the deleted platform.
Removed from both files.

## 14. Polling interval reinstated as a configurable option

After the third audit round above, the user requested — independent of
any audit finding — that the health-check interval be made configurable
again, in the same place as the account credentials (the options flow).

This is **not** a reversal of the core v1.0.0 architecture decision
(§§1-3): circuit/program/settings data remains fetch-once-at-startup-and-
after-writes regardless of this setting, and no telemetry polling of any
kind returns. It is a narrower, differently-named, differently-scoped
setting (`CONF_HEALTH_CHECK_INTERVAL`, not the old, fully-removed
`CONF_SCAN_INTERVAL`) for the one thing that genuinely still runs on a
schedule: the minimal cloud-reachability check. Implemented by reinstating
the `_get_scan_interval()`-style pattern (now `_get_health_check_interval()`)
in `__init__.py`, reading from config-entry options with the same
string-coercion defensiveness the original had, and wiring the resulting
value into `coordinator.update_interval` right after construction.
Choices: 10/15/30/60/120 minutes, default unchanged at 30.

`CLOUD_API_PROBLEM_THRESHOLD` (2 hours) deliberately stays a fixed
constant, not derived from or scaled to this new option — a user choosing
a long health-check interval (e.g. 2 hours) gets correspondingly less
"several checks in a row" margin before that diagnostic trips, which is an
accepted, documented consequence of that choice rather than an
oversight, since scaling it automatically would make the diagnostic's
behavior implicit and surprising rather than a fixed, stated promise.

## 15. Test coverage (third round)

Grew from 280 to 341 tests across the third audit round and the
configurable-interval feature. 341 tests pass, ruff clean (lint +
format), 63.6% overall coverage (`api.py` 90%, `coordinator.py` 89%,
`diagnostics.py` 100%).

## 16. Pre-deployment audit response, fourth round ("ICS deep test" report)

A fourth independent review (`hoval_connect_v1_0_0_ics_deep_test_report_complete.md`),
explicitly the deepest yet — deliberately re-examining every async
boundary, state-machine transition, persistence boundary, and topology
transition — found 22 further issues. All 22 were independently
re-verified before acting on any of them, including two cases where
verification went beyond reading the code: checking the OpenAPI spec's
schema directly (confirming `week1OrWeek2Active` is a real field, for
§16.8 below) and writing a standalone async simulation to empirically test
a specific timing claim rather than judge it by inspection alone (§16.9).
**Two findings were rejected**, not accepted on faith. **Two more were
deliberately deferred by explicit agreement**, not fixed as part of this
round. The remaining 18 were fixed.

### 16.1 Two findings rejected

**"Number debounce cancellation can erase a newer pending value"**
(the report's HVC-010): the reported mechanism was traced in detail and
found not to hold. `_send_value()` clears `self._pending_value` at its
*start*, before any await — meaning by the time a second write (B) could
possibly be submitted, the first write's (A's) clearing has already
happened, and B's own assignment happens strictly after. A's later
completion or cancellation never touches `_pending_value` again (only its
`finally: self.async_write_ha_state()` runs, which just re-renders
whatever the CURRENT value is). Confirmed empirically with a standalone
asyncio simulation reproducing the exact reported sequence (debounce
expires, second value submitted, first task cancelled): the simulation's
final observed state at cancellation is the SECOND value, not erased.
This looks like a genuine mistrace in the audit (confusing "cleared at the
start of the coroutine" with "cleared in a finally block"), not a real
bug — the actual test transcript is preserved in this project's session
history rather than reproduced here.

**"Missing `isOnline` defaults to online"** (HVC-017): confirmed as
described (a missing optional field is treated as `is_online=True`), but
disagreed with the characterization of this as a bug. The alternative —
defaulting to offline when unknown — would be strictly worse: offline
plants are skipped from circuit discovery entirely
(`if not plant_data.is_online: continue`), so treating "unknown" as
"offline" would make a plant that's actually online but merely omitted
the field NEVER get its circuits discovered, which is a worse outcome
than the current behavior of trying and letting the real API calls (which
either succeed or fail on their own terms) reveal the truth. Left as-is
deliberately, not merely left unfixed.

### 16.2 Two findings deferred (by explicit agreement, not fixed this round)

**"Cancelling an async write does not cancel the underlying blocking HTTP
request"** (HVC-011): confirmed as structurally real and, unlike most
findings in this project's history, not a coding mistake at all — it's
inherent, standard `concurrent.futures`/`ThreadPoolExecutor` behavior.
Once a blocking `requests` call has actually started executing in an
executor thread, cancelling the asyncio task awaiting it does not stop
that thread; `concurrent.futures.Future.cancel()` only succeeds before
execution begins, and `Session.request()` for this integration's typical
payload sizes starts executing essentially immediately once submitted. So
"cancel the old write, send the new one" does not guarantee the old write
never reaches the device, and in principle a stale command could arrive
after a newer one. This is an architectural consequence of the v0.24.0
requests-in-executor transport decision (see docs/audit-v0.24.0.md),
not something fixable by a small patch — a real fix means a genuine
per-circuit write-serialization mechanism (a queue, or a lock plus
sequencing). Deferred as a separate, larger design conversation rather
than folded into this round's fixes.

**"`resolve_resume_program()` can switch from the wrong weekly schedule"**
(HVC-018): the report's own suggested improvement was checked against
`docs/openapi-v3.json` and confirmed real: `week1OrWeek2Active` is a
genuine boolean field on `CircuitV3DTO`, not a suggestion the auditor
invented. However, its exact semantics are not confirmed from the schema
alone — the name is genuinely ambiguous between "week1 is the active one
(vs week2)" and "some week program (either one) is currently active", and
guessing wrong would introduce a new, subtler version of the exact bug
this project has already fixed twice (HVC-ICS-008 in the second round,
finding #8 in the third). Consistent with this project's established
discipline about not shipping unvalidated assumptions about API field
semantics (the entire v0.24.0 investigation exists because of exactly
this kind of mistake), this was deferred pending live validation against
the real API rather than implemented speculatively.

### 16.3 HVC-001 — malformed circuit-list element could abort the whole refresh

Confirmed: `circuits_raw`'s outer shape (list, or dict with a list
"content" key) was validated by `get_circuits()` (fixed in an earlier
round), but nothing validated that each ELEMENT of that list is itself a
dict. `[{"type": "HK", ...}, null]` would raise `AttributeError` on
`circuit.get(...)` — before `_fetch_circuit()` ever runs, so outside the
per-circuit `asyncio.gather(..., return_exceptions=True)` isolation that's
supposed to protect against exactly this class of malformed input. One
bad element could abort the entire refresh (every circuit, every plant),
not just itself. Fixed by filtering to `isinstance(c, dict)` elements
before any further processing, with a warning logged for any discarded.

### 16.4 HVC-002 / HVC-022 — duplicate circuit paths / plant IDs

Confirmed: neither `plant_data.circuits[result.path] = result` nor
`plants[plant_id] = HovalPlantData(...)` checked for a pre-existing entry
before overwriting — a genuine upstream data error (never observed live)
producing two entries with the same identity would silently collapse into
one, with entity unique_ids (built from plant_id/path) potentially
colliding too. Fixed at both levels: first-seen wins, a warning is logged
so a real occurrence is never silent, and (for the plant level, in
`_fetch_all_data()`) the duplicate is skipped before any of its own
API calls are even made.

### 16.5 HVC-003 — session close ordering didn't cover fan/number debounce tasks

Confirmed, and this is the most significant finding of this round: it
exposes a real gap in the *previous* round's own fix (finding #7, §13.7).
That fix added task tracking and `async_shutdown()`, called before
`api.aclose()` — but only the coordinator's own post-write refresh tasks
were ever routed through it. `fan.py`'s and `number.py`'s debounced
control-write tasks were still created via a bare
`hass.async_create_task(...)`, tracked only by each entity's own
`async_will_remove_from_hass()` — which runs during platform *unload*,
which happens AFTER `async_unload_entry()` had already called
`async_shutdown()` and closed the session. A debounce task (1.5s window,
triggered on every slider interaction — far more frequent than the
coordinator's own 2-second post-write settle delay) still asleep at
reload time could wake up and try to use an already-closed
`requests.Session`.

Fixed by extending the existing mechanism rather than reordering the
unload sequence (a bigger, riskier change with unclear side effects on
other entities' own unload assumptions): `_create_tracked_task()` was
renamed to the public `create_tracked_task()` and now returns the created
task, so `fan.py`/`number.py` can route their debounce tasks through it
while still keeping their own local reference (needed for their existing
debounce-cancel-on-new-value logic). `async_shutdown()`'s existing
cancel-and-await loop now covers both kinds of task with no changes to
its own logic.

### 16.6 HVC-004 — HEAT and AUTO performed the identical operation

Confirmed: `_attr_hvac_modes` advertised `[HEAT, OFF, AUTO]` as three
distinct modes, but `async_set_hvac_mode`'s `elif hvac_mode in
(HVACMode.AUTO, HVACMode.HEAT):` routed both to the identical "resume the
week schedule" call — selecting HEAT produced no different outcome from
selecting AUTO, misleading given HA's climate contract implies each
advertised mode is a genuine, distinct target. Fixed by giving HEAT a
real, different target: `set_program(..., "constant")` — reusing an
already-proven mechanism (`select.py`'s "Constant" option already calls
this same thing) rather than a new, unvalidated API interaction. This
also makes the write side consistent with the already-correct read side:
`hvac_mode` already reported HEAT specifically for any circuit NOT on a
week/eco program (i.e., exactly the "constant"-like states) — selecting
HEAT now actually produces the state `hvac_mode` would report back as
HEAT, instead of silently resuming the schedule and often landing in AUTO.

### 16.7 HVC-005 — missing `cloud_api_problem` translation key

Confirmed: `HovalCloudApiProblem` (added in the original v1.0.0 draft) has
declared `_attr_translation_key = "cloud_api_problem"` since its
introduction, but neither `strings.json` nor `translations/en.json` was
ever updated with a matching entry — a genuine oversight that had
persisted through three subsequent audit rounds unnoticed until this one.
Added to both files.

### 16.8 HVC-006 / HVC-007 — persisted options trusted without re-validation

Both confirmed. `_get_health_check_interval()` coerced the persisted value
to `int` but never checked it against `HEALTH_CHECK_INTERVAL_OPTIONS` — a
`0`, negative, or absurdly large out-of-band value (never reachable
through the options form itself, which does validate, but reachable via
hand-edited storage or a future migration bug) would reach
`coordinator.update_interval` unchanged; an sufficiently large value could
even make `timedelta()` itself raise `OverflowError`, uncaught by the
original `except (TypeError, ValueError)`. Fixed with an explicit
membership check and an `OverflowError` catch (defense in depth, since the
membership check alone should already prevent ever reaching it).

`fan.py`'s `_turn_on_mode`/`_override_duration` properties read persisted
options directly with no validation against `VALID_TURN_ON_MODES`/
`VALID_OVERRIDE_DURATIONS` (both new). **A second, previously unnoticed
occurrence of the identical override-duration gap was found in
`climate.py`'s `async_set_temperature`** while fixing the first one —
exactly the "sibling method missed" pattern that has recurred several
times across this project's audit history. Fixed in both files.

### 16.9 HVC-008 — numeric API values not centrally normalized

Confirmed: `circuit.get("targetValue")`/`circuit.get("actualValue")` were
copied directly into `HovalCircuitData` with no validation. Verified
directly (not assumed) that Python's `json` module parses `NaN`/
`Infinity`/`-Infinity` from a response body by default — this is not a
hypothetical edge case requiring a contrived input. `fan.py`'s
`int(float(val))` raises `ValueError` for `float("nan")`, taking down that
entity's percentage property entirely. Fixed with a new
`_coerce_finite_number()` helper (rejects non-finite values and wrong
types, accepts numeric strings defensively) applied at the point both
fields are populated in the coordinator — climate.py/water_heater.py's
own direct reads of these fields needed no separate changes, since they
inherit the guarantee from the source.

### 16.10 HVC-009 — persisted timestamp can become timezone-naive

Confirmed: `datetime.fromisoformat(raw_contact)` returns a naive datetime
for a string with no UTC offset, and the later
`dt_util.utcnow() - last_successful_contact_at` in the cloud-problem
binary sensor raises `TypeError` when subtracting a naive datetime from an
aware one. Under normal operation this field is always written from
`dt_util.utcnow()` (aware), so this only matters for a corrupted or
hand-edited storage file — but if it happens, it would break that
diagnostic entity. Fixed: a naive parsed timestamp is now assumed UTC
(matching what this integration always writes) and normalized to
timezone-aware before being stored.

### 16.11 HVC-012 — corrupted persisted health data could still crash startup

Confirmed: the `error_counts` dict comprehension's `int(v)` had no
protection against a value that IS an `int`/`float` (passing the existing
`isinstance` check) but is non-finite — `int(float("nan"))` raises
`ValueError`, `int(float("1e400"))`-scale values can raise `OverflowError`
— neither caught anywhere, crashing the whole restore (and therefore
config-entry setup) on one corrupted entry. The EMA restoration had the
same class of gap: `ema > 0` is `True` for `+infinity`, letting it through
silently. Fixed by validating every value (finite, non-negative) before
conversion in all three restoration paths (the three main counters,
`error_counts`, and EMA), skipping bad entries individually rather than
aborting the whole restore.

### 16.12 HVC-013 — pagination exhaustion silently returned partial topology

Confirmed: reaching `_MAX_PLANT_PAGES` (50) logged a warning explicitly
calling the condition "almost certainly an upstream fault" — and then
returned the partial list collected so far as if it were a complete,
successful result anyway. Inconsistent with every other place in the same
method that already fails closed on a detected anomaly (the response-
shape validation added in an earlier round), which this one path had
simply been missed for. Fixed: raises `HovalApiError` instead. Practical
likelihood remains low (50 pages × 12 per page = 600 plants, or a genuine
backend pagination bug) — the fix is about consistency with this
project's own established principle, not a response to an observed
failure.

### 16.13 HVC-014 — plant-access-token lock serialized unrelated plants

Confirmed: `self._pat_lock` was a single `asyncio.Lock()` shared across
every plant on the account — a multi-plant account would serialize
token acquisition across entirely independent plants and endpoints,
needlessly extending startup time and compounding with the coordinator's
90-second overall timeout in a slow-network scenario. Fixed with one lock
per plant_id (`self._pat_locks: dict[str, asyncio.Lock]`), created lazily
via `setdefault()` — safe without additional locking because dict
check-and-set is atomic within asyncio's single-threaded model (no await
point between the check and the assignment).

### 16.14 HVC-015 — select `current_option` could violate its own `options` contract

Confirmed: `activeProgram` values the API permits (`manual`,
`externalConstant`) were never part of `API_PROGRAMS` (the selectable
list), but `current_option` returned them verbatim via a `.get(key,
key)`-style fallback when they occurred — violating `SelectEntity`'s own
contract that `current_option` should always be a member of `options`, or
`None`. Fixed: returns `None` for any `active_program` not present in the
resolved display-name mapping, the same choice already made for
`water_heater.py`'s `high_demand` (a real, observable state with no
"select this to activate it" action, not something to force into a
selectable list).

### 16.15 HVC-016 — non-string program names could crash the select entity

Confirmed: `w1.get("name")` was checked only for truthiness, not for
being a string — a malformed-but-truthy value (a list, a dict) would be
stored, and `resolve_program_display_names()`'s `counts[name] = ...`
requires `name` to be hashable, raising `TypeError` for either. This
crashes the WHOLE select entity's `options`/`current_option` computation,
not just the display of one malformed name. The existing exception
isolation in the coordinator's program-parsing block does not help here —
that block only protects the coordinator's own parsing; the crash would
happen later, in a different module (`select.py`), when an entity
property runs. Fixed: requires an actual, non-empty (after stripping
whitespace) string.

### 16.16 HVC-019 — weather-impact sibling values forwarded without validation

Confirmed: `resolve_weather_impact_update()` clamped the value the user
was actively changing, but forwarded the *other* (sibling) field's
current value — sourced from an optimistic override, the settings cache,
or last-parsed circuit data — completely as-is. A corrupted source could
put an out-of-range or non-finite value into the outgoing two-field PATCH
even though the field the user actually asked to change was itself valid.
Fixed with a new `_clamp_if_valid_number()` helper: re-validates the
sibling with the same clamp function used for a fresh value, degrading a
genuinely unusable value (non-finite, wrong type) to `None` rather than
propagating it or crashing the write outright.

### 16.17 / 16.18 HVC-020 / HVC-021 — no active entity removal, no live device-name sync

Both confirmed as described. Deliberately NOT fixed with new removal/sync
machinery — documented instead, per the audit's own explicitly offered
"or document" resolution path for both findings, and consistent with this
integration's pre-existing, deliberate "static hardware, no hot-swap"
design decision (recorded earlier in this project's history): circuits/
plants are discovered at startup and on a detected topology change or
write, not continuously reconciled against live renames or removals.
Recorded as accepted limitations in the README's Known Limitations, with
"reload the config entry" as the documented resynchronization boundary —
the same boundary already used for options changes.

## 17. Test coverage (fourth round)

Grew from 341 to 377 tests across the fourth audit round. 377 tests pass,
ruff clean (lint + format), 64.2% overall coverage (`api.py` 90%,
`coordinator.py` 89%, `diagnostics.py` 100%).

## 18. `sensor.py` partially reinstated (user request, not an audit finding)

After deploying v1.0.0 and living with it for the first time, the user
reported that removing `sensor.py` entirely (§§1-3) went further than
actually wanted: with zero sensor entities left, there was no at-a-glance
visibility from this integration at all, even for data it was already
retrieving for control purposes. Confirmed by inspecting a real deployed
instance's diagnostics export and log: the underlying architecture was
working exactly as designed (health check succeeding, circuits/programs/
settings correctly populated), but nothing surfaced any of it as a sensor.

This is scoped narrowly and deliberately, not a reversal of the "no
scheduled telemetry polling" decision documented in §§1-3, which remains
fully in effect:

**What was added.** Two categories, both using data the coordinator
already retrieves for other reasons — genuinely zero additional API calls:

- Per-circuit `HovalCircuitActualValue`/`HovalCircuitTargetValue`, reading
  `circuit.actual_value`/`circuit.target_value`. These fields have existed
  on `HovalCircuitData` since the third audit round's HVC-003 fix (mapping
  the circuits-list response's `actualValue`/`targetValue` fields, which
  this integration was already fetching for control purposes) — they were
  previously used only internally, as a fallback source for climate/fan/
  water-heater's own current-value display properties, never exposed as
  their own sensor entities. Scoped to HK/WW (temperature, °C) and HV
  (air-volume, %); BL is excluded — its values are consistently null/0.0
  in the confirmed real diagnostics export, not just in theory.
- Four plant-level `_HovalApiHealthSensor` subclasses
  (`HovalApiLastSuccess`, `HovalApiPollLatency`, `HovalApiFailureRate`,
  `HovalApiLastError`), reading directly from
  `coordinator.connection_health` — every value these expose was already
  computed for the diagnostics export and the `cloud_api_problem` binary
  sensor; nothing new is calculated either.

**What was deliberately left out**, and why re-adding it would be a much
bigger change than this one: `get_live_values()`, `get_weather()`,
`get_plant_events()`/`get_latest_event()` remain deleted from `api.py` —
restoring any of them means reintroducing scheduled polling of those
endpoints, which is the actual thing the original v1.0.0 redesign
removed. Bringing back a handful of already-fetched fields as sensors
costs nothing; bringing back live-values/weather/events would mean
undoing the core architectural decision, not extending it.

**A privacy-scoping decision worth recording explicitly**:
`HovalApiLastError` exposes only the error's *type* and *timestamp*, never
`last_error_msg`. Several `_LOGGER`/error-message call sites elsewhere in
this codebase format a circuit path or plant ID directly into their
message text (see finding #9 in the "more" report, §13.9, which is the
whole reason `diagnostics.py`'s export redacts this field). A live entity
attribute is a different exposure surface than a one-time diagnostics
export — visible in the Logbook and History, and to anyone with dashboard
access — so forwarding the raw, unredacted message there would quietly
undo that earlier redaction work. A test
(`test_last_error_sensor_does_not_expose_raw_message`) guards this
specifically by asserting `last_error_msg` never appears in the class's
own source.

**Test coverage**: new `tests/test_sensor.py`, 20 tests, constructing each
entity class directly (bypassing `async_setup_entry`, consistent with
this project's established entity-testing approach) and checking
`native_value`/`extra_state_attributes` against a lightweight fake
coordinator. `.available` is not exercised — it calls `super().available`,
which the shared test-harness stub (`StubCoordinatorEntity` in
`tests/ha_stubs.py`) does not implement, a pre-existing limitation shared
identically by every other entity-platform file in this project
(`climate.py`, `fan.py`, `number.py`, `select.py`, `water_heater.py`), not
something specific to the new file. `CIRCUIT_PLATFORMS` in
`tests/test_ha_compat.py` has `"sensor"` added back, since the new file
follows the same `AddConfigEntryEntitiesCallback`/dynamic-discovery
conventions as every other platform and passes that suite's existing
baseline checks unmodified.

Grew from 377 to 401 tests. Final: 401 tests pass, ruff clean (lint +
format), 64.7% overall coverage (`api.py` 90%, `coordinator.py` 89%,
`diagnostics.py` 100%, `sensor.py` 71% — notably higher than the other
entity-platform files, precisely because of the behavioral tests above).


