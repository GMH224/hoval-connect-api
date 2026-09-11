# Changelog

All notable changes to the `hoval_connect` integration are documented here.
This project follows a loose [Semantic Versioning](https://semver.org/) scheme
while pre-1.0 (minor = behavioural/feature change, patch = internal fix).

## [1.0.0] - 2026-09-10

**Pre-deployment note:** this entry was amended after an independent code
audit and a separately-reported production bug were reviewed and addressed
— all *before* this version was ever deployed, so the fixes below are
folded into this same v1.0.0 entry rather than a separate release. Full
details in `docs/audit-v1.0.0.md` §§ "Pre-deployment audit response" and
"Post-audit bug report".

**Architecture change, not a routine release: this integration no longer
polls telemetry on any schedule.** Full investigation and design rationale
in `docs/audit-v1.0.0.md`; summary below. Deliberately versioned 1.0.0
(a real jump, not 0.25.0) at the user's explicit request, so a previously
deployed v0.24.0 install is never silently overwritten in place — anyone
who wants to go back to the old always-polling behavior can simply not
upgrade, rather than needing to hunt down and reinstall a specific old
version.

### Why

The user runs a separate, CAN-bus-based HACS integration that already
covers all telemetry (temperatures, energy, live values) — a strict
superset of what this integration's cloud polling ever provided. The only
things they actually need this integration for are (a) the handful of
controls the CAN-bus integration can't reach — heating/hot-water program
selection, the weather-based-control Eco/Comfort sliders — and (b) knowing
if the cloud API itself has stopped working, so they can go investigate.
Continuously polling a dozen-plus telemetry endpoints for data going
unused, every 60 seconds to 10 minutes, forever, no longer served any
purpose and was pure standing cloud-API footprint.

### Changed — the core mechanism

- **No more recurring telemetry polling.** `get_live_values()`,
  `get_events()`, `get_latest_event()`, and `get_weather()` are removed
  from `api.py` entirely — not just unused, deleted, since nothing calls
  them anymore.
- **Circuit/program/settings data (the control surface) is now fetched
  once at startup, and again only after an actual write** — not on a
  recurring schedule. This still uses `get_circuits()`, `get_programs()`
  (for program display names), and `get_circuit_settings()` (for the
  weatherImpact Eco/Comfort sliders' current values) — all genuinely needed
  to render and use the controls — just no longer on a timer.
- **The only thing left on a recurring schedule is a new, minimal health
  check**, every `HEALTH_CHECK_INTERVAL` (30 minutes, fixed): one auth
  call, one `GET /api/my-plants`, nothing plant- or circuit-specific. Its
  only job is confirming the cloud API still responds at all.
- **New diagnostic: `binary_sensor.*_cloud_api_problem`** (device_class
  `problem`, diagnostic category). Tracks `last_successful_contact_at` —
  updated by *either* a successful health check *or* a successful write,
  whichever happens more recently — and turns on once more than
  `CLOUD_API_PROBLEM_THRESHOLD` (2 hours, fixed) has passed with no
  successful contact of either kind. Deliberately patient: a single missed
  health check does not trip it, several in a row do.
- **`plant.has_error` is now derived from circuits' own `hasError` flags**
  (already part of the circuits-list response, so this is free) instead of
  event-history telemetry. A narrower definition than before (any circuit
  reporting an active error, vs. any active blocking/locking/warning event
  in the plant's recent history), but needs no extra API call.

### Removed

- **`sensor.py` deleted entirely.** Every entity in it depended on
  telemetry this integration no longer polls, with no write capability of
  its own — there was nothing left for it to usefully show.
  **This is a breaking change**: existing `sensor.*` entities from this
  integration will stop updating on upgrade, and will eventually show as
  "not provided by the integration" in Settings → Devices & Services →
  Entities. Home Assistant does not remove orphaned entities
  automatically; deleting them from the registry (if desired) is a manual
  step. The user's own must-not-break automation entities were confirmed
  unaffected before this release shipped: `select.*_program` (both
  circuits), `number.*_weather_based_control_outside_temperature`,
  `number.*_weather_based_control_solar_radiation`,
  `binary_sensor.*_error`, and `water_heater.*` (including its
  `reset_ww_boost` action) were never `sensor.py` entities, and none of
  their identifying fields changed.
- **The polling-interval option is gone entirely** — `CONF_SCAN_INTERVAL`,
  `SCAN_INTERVAL_OPTIONS`, and `DEFAULT_SCAN_INTERVAL` are all removed from
  `const.py`, and the corresponding dropdown removed from the options
  flow. There is no longer a meaningful "poll rate" for a user to tune —
  see "Changed" above. This supersedes (makes moot, not wrong) the whole
  v0.19.0/v0.23.0 bug-fix history for that option.
- `climate.py` and `fan.py` are **unchanged** and remain fully functional
  for control — they simply show less live-value detail now (current
  temperature, HVAC action) since `HovalCircuitData.live_values` is never
  populated anymore. Every `.live_values.get(...)` call in those files
  degrades gracefully to `None` on its own; no code there needed to
  change.

### Internal

- `HovalCircuitHealth` (per-circuit reliability tracking, tied to the
  now-removed live-values polling), `HovalEventData`, `HovalWeatherData`,
  and the dead `resolve_fan_speed()` helper (defined but never actually
  called anywhere in the codebase) are all removed from `coordinator.py`.
- `HEALTH_STORAGE_VERSION` bumped 1 → 2 (the persisted health-tracking
  schema changed — added `last_successful_contact_at`, removed per-circuit
  data). **Found and fixed a real bug while writing this changelog entry**:
  Home Assistant's `Store` helper does not silently discard a
  version-mismatched file on its own — verified against its actual source,
  not assumed — so without a fix, this version bump would have made the
  whole integration fail to load on the very first run after either
  upgrading into this release or rolling back out of it. `__init__.py` now
  wraps the health-store load in a broad try/except that starts fresh on
  any load failure instead of blocking setup. See `docs/audit-v1.0.0.md`
  §8 for the rollback implication this still leaves (v0.24.0 itself has no
  equivalent fix).
- Test suite adjusted accordingly: 255 tests pass, ruff clean, 60% overall
  coverage. New coverage added specifically for the health-check dispatch
  (`TestHealthCheckDispatch` in `tests/test_coordinator_fetch.py`) — the
  core new mechanism this release exists for.

### Pre-deployment audit response

An independent code audit was run against this release before it was ever
deployed. Every finding was independently re-verified against the actual
code (not taken on faith) before being acted on; full detail, including
which findings were pre-existing vs. new to this release, in
`docs/audit-v1.0.0.md` § "Pre-deployment audit response". Fixed:

- **`get_circuits()` now fails closed on an unrecognised response shape**
  (a dict with no list `content` key, a string, null) instead of silently
  treating it as "zero circuits" — under this release's fetch-once-at-
  startup model, a single bad response during startup could otherwise
  leave every circuit entity missing until a restart.
- **The API session is now closed on any setup failure**, not just on a
  clean unload — previously, a failure between constructing
  `HovalConnectApi` and finishing first refresh left the session (and its
  connection pool) leaked, since `async_unload_entry()` never ran for an
  entry that never finished loading.
- **A post-write refresh race that could silently drop a second write's
  confirmation for up to `HEALTH_CHECK_INTERVAL`.** The pending-refresh
  flag is now a timestamp, not a bare bool, so a second write's request
  made while an earlier write's refresh is still in flight survives that
  earlier refresh completing. The optimistic mode-override clear at the
  end of a refresh is now scoped to only clear entries older than when
  that refresh started, so a write landing for a *different* circuit
  mid-fetch no longer gets its optimistic state wiped in favour of a stale
  snapshot.
- **The cloud-problem diagnostic (and every optimistic write) now updates
  immediately.** Both write paths call the coordinator's listener
  notification right after recording a successful contact, instead of
  waiting for the next full refresh to reach entities.
- **`actualValue` and `temporaryChange`** — already part of the
  circuits-list response this release still fetches, previously ignored —
  are now mapped into real fields and used by climate/fan/water-heater as
  a free, zero-extra-call source of some current-state display, restoring
  part of what removing live-values polling took away.
- **Missing/`None` `operationMode`** now correctly reports as unknown in
  climate, fan, and water-heater entities instead of defaulting to an
  active state with no actual basis for it.
- **Temperature bounds are now enforced** on `async_set_temperature` in
  both climate and water-heater entities (clamped, matching this
  codebase's existing convention for other out-of-range input, rather than
  rejected outright).
- **Every cache and optimistic override is now keyed by `(plant_id,
  circuit_path)`, not `circuit_path` alone.** Circuit paths are only
  guaranteed unique within a plant; a single-plant account can never hit
  this, but nothing guarantees that stays true (e.g. a future plant split
  such as AC being separated from heating). `async_control_and_refresh`'s
  parameters are now keyword-only specifically so a call site that wasn't
  updated for this fails loudly instead of silently misrouting.
- Two minor engineering-tool/cleanliness items: `tools/mutation_check.py`'s
  mutations that targeted the now-deleted `sensor.py` were retargeted or
  removed, and stale `scan_interval` translation strings were removed from
  `strings.json`/`translations/en.json`.

Test suite grew to 272 tests (from 255) covering these fixes specifically,
including a dedicated multi-plant collision test proving the cache-keying
fix actually works. Still ruff clean, 60% overall coverage.

### Post-audit bug report

A separately-filed bug report (verified live against a real v0.24.1
deployment, confirmed still present in this release before the fix below)
found that a weather-impact `number` write which returns success from the
API but is not actually applied by the device could be masked indefinitely
— far longer than the optimistic override's own 120-second TTL. The root
cause: `async_set_weather_impact` was optimistically pre-populating the
settings cache with its own just-written guess immediately after a write,
which made the scheduled post-write verification refresh see a "fresh"
cache and skip doing a real confirming API call entirely, for up to
`CIRCUIT_SETTINGS_CACHE_TTL` (10 minutes) — not just the override's 120s.
Fixed by removing that optimistic cache write (the override alone already
covers the UI during the few-second gap before real verification lands)
and by reconciling the override against genuine fresh poll data using the
same race-safe timestamp guard built for the mode-override fix above.

### Second independent audit round

A follow-up independent ICS-style review (scoped to production/runtime code
only, explicitly excluding tests and prior audit docs as evidence) found 9
further issues (HVC-ICS-001 through HVC-ICS-009). All 9 were independently
re-verified against the actual code before acting on any of them; full
detail in `docs/audit-v1.0.0.md` § "Second independent audit round". Fixed
8 of 9 (the 9th, a v3/v4 API compatibility risk, is a documented risk with
no live evidence of an actual problem, so left as documentation only rather
than an unvalidated speculative change):

- **Topology discovery could stall permanently.** A plant offline during
  initial setup, or a brand-new plant appearing later (e.g. Hoval splitting
  an account into multiple plants), would never get its circuits
  discovered — the health check never fires the "new circuits" signal, and
  nothing was watching for an offline→online transition or an
  unrecognised plant. `_health_check()` now detects either condition and
  upgrades itself to a real discovery fetch for that one cycle.
- **A fan turn-off could be silently undone.** `async_turn_off()` (and
  `async_turn_on()`) never cancelled a pending debounced speed write —
  setting a speed then turning off within the 1.5s debounce window could
  turn the fan back on right after the explicit off. Both now cancel any
  pending write first.
- **Water heater `high_demand` did the opposite of what it implied.**
  Selecting it ran the exact same code as `heat_pump` — resetting to the
  normal schedule, not starting a boost. Removed from the selectable
  operation list (the real way to start a boost, setting a target
  temperature, already worked correctly); selecting it now raises a clear
  error instead of silently doing nothing like what was asked.
- **NaN/infinity silently became a valid clamp value.** `clamp_hv_air_volume()`
  and both `clamp_weather_impact_*()` functions now reject non-finite input,
  matching the guard `clamp_temperature()` already had.
- **Malformed nested API objects could delete a whole circuit.** A truthy
  non-dict `airQuality` or `weatherImpact` value raised `AttributeError`
  deep inside per-circuit processing, which — since that runs under
  `asyncio.gather(..., return_exceptions=True)` — silently dropped the
  entire circuit rather than just that one optional feature. Both now use
  explicit `isinstance(x, dict)` checks.
- **A 401 could push total HTTP attempts past the documented retry
  budget**, and conversely **a transient error acquiring an auth token
  bypassed retries entirely.** The 401 handler used to recurse into a
  fresh `_request()` call with its own new retry budget (so a 401 followed
  by one transient error could take 3 total attempts against a documented
  budget of 2); separately, token acquisition ran outside this method's
  retry try/except altogether, so a network blip talking to the identity
  provider failed immediately regardless of `_MAX_RETRIES`. Both fixed
  together by removing the recursion — a 401 now `continue`s within the
  same loop, and header acquisition shares that same loop's retry/backoff.
- **"Resume the schedule" could silently switch an active week2 schedule
  to week1.** `reset_circuit()` defaults to `program="week1"`, and none of
  the three "resume" callers (fan/climate/water-heater) passed the
  circuit's actual active program. New `resolve_resume_program()` helper
  preserves week2 when that's what's actually running.
- A plant-name fallback inconsistency between `_fetch_all_data()` and
  `_health_check()` (`plant.get("description", plant_id)` doesn't apply
  the fallback for an explicit `null`, unlike the `or`-based version) was
  also fixed while verifying the malformed-object finding above.

Test suite grew to 280 tests (from 272). Still ruff clean, 60.5% overall
coverage.

### Third independent audit round ("more" report)

A follow-up review, again scoped to production/runtime code, found 12
further issues plus 2 documentation observations. All 14 were
independently re-verified against the actual code (including checking the
OpenAPI spec's `required` list directly for finding #1, and tracing exact
control flow for the retry/auth findings) before acting on any of them;
full detail in `docs/audit-v1.0.0.md` § "Pre-deployment audit response,
third round". All 12 code findings and both documentation observations
were fixed:

- **`isSelectable` vs `selectable`**: the v3 schema requires `isSelectable`
  but only optionally sends the legacy `selectable` field — a response
  omitting the optional field could silently drop an otherwise-selectable
  circuit. Now prefers the guaranteed field.
- **`get_plants()` failed open** on an unrecognised response shape — the
  same class of bug already fixed for `get_circuits()` in the previous
  round, just missed on this sibling method. Now raises consistently.
- **An anomalous-but-well-formed empty plant list could silently wipe
  every known plant.** Both `_health_check()` and `_fetch_all_data()` now
  require 2 consecutive empty responses (when plants were previously
  known) before accepting the wipe as genuine.
- **The weather-impact write path used a settings cache with no
  freshness check at all**, and could crash on a malformed cached value.
  Implemented the real fix (a fresh GET when the cache is stale or
  missing, not just falling through to equally-stale data), plus a proper
  `isinstance` guard on the cached value.
- **A 401 fetching the plant-access token was always treated as a hard
  auth failure**, unlike the main request path's identical signal (which
  correctly refreshes and retries). Now retries once before giving up,
  matching that same semantics.
- **Post-write refresh tasks were never tracked**, so one could wake up
  after a config-entry reload had already closed the API session. Now
  tracked and cancelled by a new `coordinator.async_shutdown()`, called
  before the session closes.
- **"Resume" could act on a stale cached schedule.** The previous round's
  fix read the coordinator's last-known snapshot, which is only refreshed
  at startup or after a write — `resolve_resume_program()` now does one
  fresh, targeted fetch immediately before resolving.
- **Diagnostics redaction had real gaps**: plant IDs and circuit paths used
  as *dictionary keys* were never touched by `async_redact_data` (it only
  redacts matching field names), and the entire `connection_health`
  section — including error messages that can embed a circuit path or
  plant ID — bypassed redaction completely. Diagnostics are now built
  explicitly with indexed placeholder keys and free-text identifier
  scrubbing; `tests/test_diagnostics.py` was rewritten from scratch with
  real behavioral tests instead of only checking redaction-set membership.
- **Two identically-named week1/week2 programs were ambiguous** in the
  program select entity — disambiguated automatically by appending the
  API key when a collision is detected.
- **CI installed `aiohttp` instead of `requests`** — a leftover from
  before the v0.24.0 transport rewrite.
- **The example client was badly stale**: no `User-Agent` (would
  reproduce the exact 403 this project spent significant effort
  diagnosing), and demonstrated telemetry endpoints this integration no
  longer uses. Rewritten to reflect the actual v1.0.0 control-only surface.
- **README's troubleshooting/limitations sections described entities that
  no longer exist** (circuit-level sensors, weather/events entities) —
  corrected, and stale translation strings for the deleted sensor platform
  were removed from `strings.json`/`translations/en.json`.

One correction made to the audit's own analysis rather than following its
suggestion as given: the settings-cache finding's own "simple" suggested
fix (fall through to `circuit.weather_impact_*` on a stale cache) was
traced through and found not to actually close the reported failure
scenario, since that field is populated from the same cache in lockstep —
implemented the audit's own noted "maximum correctness" alternative (a
fresh GET) instead.

Test suite grew to 341 tests (from 280). Still ruff clean, 63.6% overall
coverage.

### Polling interval reinstated as a configurable option

At the user's explicit request, after the above: `HEALTH_CHECK_INTERVAL`
is no longer a fixed constant. New `CONF_HEALTH_CHECK_INTERVAL` option,
alongside turn-on mode and override duration in the same options screen,
with choices of 10/15/30/60/120 minutes (default: 30, unchanged from
before). This reinstates a narrower, differently-scoped version of the
option removed at the start of v1.0.0 (see the very first entry below) —
it only affects the cadence of the one lightweight reachability check this
integration still runs on a schedule, never circuit/program/settings data,
which remains fetch-once-at-startup-and-after-writes regardless of this
setting. `CLOUD_API_PROBLEM_THRESHOLD` (2 hours) stays a fixed constant,
not scaled to this setting — choosing a long health-check interval means
that diagnostic gets less "several in a row" margin before tripping, an
accepted consequence of that choice rather than a bug.

### Fourth independent audit round ("ICS deep test" report)

A fourth review — the most thorough yet, deliberately re-examining every
async boundary, state-machine transition, persistence boundary, and
topology transition — found 22 further issues. All 22 were independently
re-verified against the actual code (including writing a standalone async
simulation to empirically test one timing claim rather than judge it by
inspection alone) before acting on any of them; full detail in
`docs/audit-v1.0.0.md` § "Pre-deployment audit response, fourth round".
**Two findings were rejected after verification**, not accepted on faith
— see that section for the reasoning. **Two findings (the report's own
highest-severity ones) were deliberately deferred**, per explicit
agreement: cancelling an in-flight write does not reliably stop the
underlying blocking HTTP call once it's started running in its executor
thread (an inherent property of Python's `ThreadPoolExecutor`, not a
coding mistake, and not fixable without a genuine per-circuit write-
serialization redesign), and a promising but unvalidated lead
(`week1OrWeek2Active`, a real OpenAPI field whose exact semantics aren't
confirmed) for improving resume-program correctness further. The
remaining 18 confirmed findings were fixed:

- **Malformed circuit-list elements** (`null`, strings, numbers mixed into
  an otherwise-valid list) no longer abort the entire refresh — filtered
  out with a warning before they can reach code that assumes every element
  is a dict.
- **Duplicate circuit paths and duplicate plant IDs** are now detected and
  logged (first-seen-wins) instead of one silently overwriting the other
  with no trace.
- **API session close ordering didn't cover fan/number debounce tasks** —
  a real gap in the previous round's own task-tracking fix, which only
  covered the coordinator's own post-write refresh tasks. The tracking
  mechanism (`create_tracked_task()`, now public) is extended to cover
  every entity's debounced control-write task too.
- **HEAT and AUTO climate modes performed the identical operation.** HEAT
  now genuinely activates "constant" mode (reusing the already-proven
  `set_program()` call, not a new unvalidated API interaction); AUTO keeps
  resuming the schedule. Read and write sides now agree on what HEAT means.
- **The `cloud_api_problem` binary sensor had no translation key** in
  either `strings.json` or `translations/en.json` — a genuine oversight
  from the very first v1.0.0 draft that had persisted through three
  subsequent audit rounds unnoticed.
- **Persisted options (health-check interval, fan turn-on mode, override
  duration) were trusted without re-validation** against the actual
  supported values — an out-of-band value could reach the API unchanged
  or, for the interval, make `timedelta()` itself raise `OverflowError`.
  Fixed across `__init__.py` and `fan.py`; **a second, previously
  unnoticed occurrence of the same override-duration gap was found in
  `climate.py`** while fixing the first one and fixed too.
- **`targetValue`/`actualValue` were copied from the API with no
  validation** — a malformed or non-finite (`NaN`/`Infinity` — Python's
  own `json` module parses these by default, not hypothetical) value
  could crash an entity property downstream. New `_coerce_finite_number()`
  helper applied at the source.
- **A timezone-naive persisted timestamp could crash the cloud-problem
  sensor** later (`dt_util.utcnow() - naive_datetime` raises `TypeError`).
  Naive timestamps are now assumed UTC instead.
- **Corrupted persisted health-counter data could crash startup entirely**
  — a single `NaN`/infinite/negative entry in `error_counts` or the EMA
  latency value raised inside an unguarded conversion. Every value is now
  validated (finite, non-negative) before use, with bad entries skipped
  rather than aborting the whole restore.
- **Pagination exhaustion silently returned a partial plant list** as if
  it were a complete, successful result — inconsistent with every other
  place in the same method that already fails closed on a detected
  anomaly. Now raises instead.
- **A single global lock serialized plant-access-token acquisition across
  every plant on the account**, even though different plants' tokens are
  entirely independent. Now one lock per plant, created on first use.
- **The program select entity could report a `current_option` outside its
  own `options` list** (`activeProgram` values like `manual` or
  `externalConstant`, which the API permits but the entity never offered
  as selectable) — a real violation of `SelectEntity`'s own contract. Now
  reports `None` for those, the same choice already made for
  water-heater's `high_demand`.
- **Non-string program names could crash the select entity's state
  computation entirely** (a malformed name that's merely truthy — a list
  or dict — is unhashable, crashing the disambiguation logic from the
  third audit round). Now requires an actual non-empty string.
- **Weather-impact sibling values weren't re-validated before being sent**
  — only the field the user was actually changing was clamped; the
  sibling (from cache/override/circuit data) was forwarded as-is even if
  its source had been corrupted. Now re-clamped, degrading a genuinely
  unusable value to `None` rather than propagating it.
- **No active entity/device-registry cleanup for removed circuits, and no
  live device-name sync on rename** — both documented as accepted,
  deliberate limitations (consistent with this integration's existing
  "static hardware, reboot/reload for topology changes" design) rather
  than new removal/sync machinery, per the audit's own "or document"
  option for both findings. See the README's Known Limitations.

Test suite grew to 377 tests (from 343). Still ruff clean, 64.2% overall
coverage.

### `sensor.py` partially reinstated

At the user's explicit request, after living with v1.0.0 for the first
time: removing `sensor.py` entirely turned out to go further than
actually wanted, leaving zero at-a-glance visibility from this integration
at all — even for data it was already fetching for control purposes. This
is a deliberately narrow revival, not a reversal of the "no scheduled
telemetry polling" architecture decision (§§1-3 above still apply in
full):

- **Per-circuit current-value sensors** — Actual value and Target value,
  for HK/WW (temperature) and HV (air-volume %) circuits. Both read
  `circuit.actual_value`/`circuit.target_value`, which are already part of
  the circuits-list response fetched for control purposes regardless (see
  the "more" audit report, finding HVC-003/finding-#3 lineage in
  `docs/audit-v1.0.0.md`) — zero additional API calls. BL circuits are
  excluded; their values are consistently null/meaningless in practice.
  Target value is diagnostic-category (it duplicates what the circuit's
  own climate/fan/water-heater entity already shows as its target).
- **API health diagnostic sensors** — last success (timestamp), poll
  latency (ms, with average/p95/EMA as attributes), failure rate (%,
  rolling 1-hour window), and last error type (with its timestamp as an
  attribute). All four read straight from the coordinator's already-
  computed `connection_health` — nothing new is fetched here either.
  Deliberately exposes only the error *type*, never the raw error
  message: some error messages elsewhere in this integration embed a
  circuit path or plant ID, and those are redacted in the diagnostics
  *export* but would not be if forwarded verbatim into a live entity
  attribute (visible in Logbook/History, not just a one-time export).

**Explicitly still out of scope**: live_values, weather forecasts, events,
and energy/hours counters. Restoring any of those would mean
reintroducing scheduled telemetry polling — the actual thing v1.0.0's
redesign was for. `Platform.SENSOR` is back in `PLATFORMS`; the five
entities the user depends on for automations are unaffected either way,
as they always have been across every change in this project's history.

Test suite grew to 401 tests (from 377), including a new
`tests/test_sensor.py` with genuine behavioral tests for every new sensor
class (unusually thorough for an entity-platform file in this project —
most others are limited to source-contract checks due to the shared test
harness not implementing `CoordinatorEntity.available`). Still ruff
clean, 64.7% overall coverage.

## [0.24.0] - 2026-09-10

Transport rewrite: replaces `aiohttp` with `requests` (run via
`hass.async_add_executor_job`, Home Assistant's sanctioned mechanism for
calling blocking code from an async integration) for every cloud-API call.
This is an unusual thing for a Home Assistant integration to do and is not a
style preference — it is the direct, empirically-forced conclusion of
re-investigating the v0.23.0 fix after it turned out not to work. Full
investigation in `docs/audit-v0.24.0.md`; summary below.

### Why this was necessary

v0.23.0 shipped a `User-Agent` fix for a blanket HTTP 403 on every endpoint,
without live confirmation that it worked. It didn't — the 403 persisted
after release. Re-diagnosing this properly required testing one variable at
a time directly against the live API (with the user's hands-on help running
a series of increasingly narrow scripts) and found **two independent causes
stacked on top of each other**:

1. **`aiohttp`'s TLS connection fingerprint is blocked outright**, regardless
   of any header content. Confirmed across four separate configurations, all
   against the real API, all HTTP 403: `aiohttp` default; `aiohttp` + a
   custom `User-Agent`; `aiohttp` + `Accept`/`Accept-Encoding`/`Connection`
   headers matching what `requests` sends by default; `aiohttp` with its TLS
   context rebuilt from `urllib3`'s own cipher list. That last one matches
   `requests`' cipher suite exactly and still failed — this is not fixable
   by header or cipher tuning from within `aiohttp`.
2. **`requests`' own default `User-Agent` string, `"python-requests/X.Y.Z"`,
   is separately blocked** — almost certainly a WAF signature rule against
   well-known scripting-tool identities. Confirmed by isolating this one
   variable on an otherwise byte-identical script: HTTP 403 with the default
   UA, HTTP 200 with a custom one, nothing else changed.

Two intermediate hypotheses were tested and ruled out along the way, in the
interest of an honest record: an account/IP-level anti-abuse block (ruled
out — the official app kept working throughout on the same account and
network, which a blanket account block could not explain) and a hidden
app-only credential absent from the public OAuth2 flow (ruled out — a
`requests`-based crawl with no special credential succeeded repeatedly,
including on the same day the 403s were otherwise constant).

### Changed

- **`api.py` no longer uses `aiohttp`.** Every network call now goes through
  a `requests.Session()`, created once and reused for the client's lifetime,
  with each individual call wrapped in `hass.async_add_executor_job()`. This
  keeps the integration non-blocking from Home Assistant's point of view
  even though the underlying HTTP client is synchronous. Every public
  method's signature and behavior is unchanged — retries, timeouts (now a
  `(connect, read)` tuple, `requests`' native equivalent of `aiohttp`'s split
  `ClientTimeout`), 401 token-refresh-and-retry, the distinct 403 log line
  added in v0.23.0, and all response-shape normalisation are all preserved
  exactly.
- **`HovalConnectApi.__init__` now takes `hass`, not an `aiohttp.ClientSession`.**
  `__init__.py` and `config_flow.py` updated accordingly. `HovalConnectApi`
  gained an `aclose()` method (closes the `requests.Session`'s connection
  pool); called on integration unload and after each config-flow validation
  attempt.
- **`USER_AGENT` (`const.py`) changed to the exact string empirically proven
  to work** — not the v0.23.0 string, which was never validated against a
  real 403 and turned out to be aimed at the wrong problem (`aiohttp`'s TLS
  fingerprint, not header content). See the comment on `USER_AGENT` for why
  this exact value must not be swapped for something "nicer" without
  re-validating against the live API first.
- **`manifest.json`**: `requests>=2.28.0` added as a declared dependency
  (previously an empty `requirements` list); version bumped to `0.24.0`.

### Testing

`tests/test_api.py` rewritten in full for the new transport — every mock now
targets `requests.Session` instead of simulating `aiohttp`'s async
context-manager response protocol. New tests specifically guard this
release: the transport really is `requests` (`isinstance(api._session,
requests.Session)`), `aiohttp` is not imported anywhere in the package,
`manifest.json` declares the dependency, `USER_AGENT` matches the validated
string exactly (a tripwire against an unvalidated "cleanup"), `aclose()`
actually closes the session, and concurrent `_request()` calls against one
shared session (matching the coordinator's fan-out-per-circuit pattern)
resolve to the correct results. 303 tests pass; ruff clean; 86% coverage on
`api.py`.



Cloud-API compatibility release, fixing a blanket HTTP 403 reported against
every endpoint, plus a config UI bug. Full investigation and verification
method in `docs/audit-v0.23.0.md`.

**Housekeeping note — version-number correction:** this repository's own
stated policy (top of this file) is a loose semver scheme that stays
pre-1.0. Every release through v0.21.1 (see below) followed that. The entry
immediately below this one is headed `[2.2.0]`, and the `manifest.json` this
release replaces said `"version": "2.22.0"` — both almost certainly a typo
for `0.22.0` (the leading `0.` dropped) rather than a deliberate jump past
1.0. An earlier pass at this release incorrectly treated `2.22.0` as
authoritative and bumped it to `2.23.0`, carrying the typo forward instead
of catching it. This entry corrects course: **0.23.0 continues from
0.21.1**, matching the project's actual policy and its own version history.
The `[2.2.0]` entry below is left as-is rather than retroactively renumbered
— it documents a real release, just under a mislabeled version string.

**Practical note for anyone updating a live installation:** if a Home
Assistant instance currently has `2.22.0` installed via HACS, `0.23.0` will
look like a *downgrade* to a naive string/semver comparison, and HACS may
not offer it as an update. A manual reinstall (or forcing the version) may
be needed on any instance that picked up the mislabeled `2.x` manifest.

### Fixed
- **Blanket HTTP 403 on every endpoint.** Root-caused via a one-off forensic
  crawl (`crawl.py` in the user's report, not shipped with the integration):
  a fresh password-grant login and every endpoint this integration calls
  succeeded, using a distinctive custom `User-Agent`. Nothing in `api.py` set
  a `User-Agent` at all — every request went out with Home Assistant's shared
  aiohttp session default. The API sits behind an Azure Application Gateway
  (visible in unrelated 502 error pages during discovery), a common place to
  enforce User-Agent allow/deny rules. `api.py` now sends an explicit,
  stable `USER_AGENT` (new constant in `const.py`) on every outbound request
  — the IDP token call, the plant-access-token fetch, and the shared
  `_headers()` builder used by all other endpoints. A distinct warning-level
  log line was also added for any future HTTP 403, so a recurrence is easy to
  spot and its response body is captured automatically instead of only
  reaching debug-level logging.
  **Not independently confirmed** against a captured 403 response
  body/headers from a live installation (none was available at diagnosis
  time) — see `docs/audit-v0.23.0.md` for what to capture if 403s recur.
- **Options flow — Polling interval field rendered empty.** `SCAN_INTERVAL_OPTIONS`
  (`const.py`) had no entry for 600 seconds ("10 minutes"), even though that is
  a documented, expected choice alongside "5 minutes". A config entry with a
  stored `scan_interval` of 600 had no matching key for the options dialog's
  dropdown to pre-select against, so — while every other field on the same
  form loaded its saved value correctly — the Polling interval field alone
  rendered blank. Added `600: "10 minutes"` to `SCAN_INTERVAL_OPTIONS`; both
  new entries and any config entry that already had `scan_interval: 600`
  stored now display and save correctly. No change to `config_flow.py`'s
  validation logic was needed (the `vol.Coerce(int)` fix from v0.19.0 already
  handles the save path correctly) — only the option set itself was missing a
  value.

### Changed (cloud API drift, not user-facing bugs on their own)
- **`get_circuit_settings` no longer returns `weatherImpact`.** The forensic
  crawl found the cloud now returns only `{"circuitName": ...}` for every
  circuit tested, where it previously also returned the `weatherImpact`
  sub-object. `coordinator.py` now checks for the *key's presence*
  (`"weatherImpact" in settings`) rather than treating any dict response as
  proof the feature is supported. Circuits where the cloud has dropped the
  key now correctly report their weather-impact number entities as
  unavailable instead of showing non-functional sliders with an unknown
  value. This is a graceful degradation, not a recovery of the feature — if
  Hoval relocated `weatherImpact` to a different endpoint, that endpoint has
  not yet been identified (the crawl's OpenAPI dump was captured but not
  fully diffed for a replacement path; see `docs/audit-v0.23.0.md` §
  "Known limitations").
- **`get_programs` is no longer called for BL (boiler) circuits.** The crawl
  confirmed the cloud returns HTTP 417 for this circuit type on every call,
  never 200 — it has no time-program of its own. New `SUPPORTS_PROGRAMS`
  constant (`const.py`) gates the coordinator's program fetch the same way
  `SUPPORTS_WEATHER_IMPACT` already gates the settings fetch. This was never
  a crash (the existing `asyncio.gather(..., return_exceptions=True)`
  isolation already absorbed the failure), only a wasted, guaranteed-to-fail
  round trip on every `PROGRAM_CACHE_TTL` refresh plus a debug-log line.

## [2.2.0] - 2026-09-04

Home Assistant forward-compatibility release, targeting HA **2026.8 → 2026.12**.
The full audit — including the claims that did *not* survive verification against
the Home Assistant source — is in `docs/audit-v2.2.0.md`.

No entity IDs, unique IDs, device identifiers, units or API semantics change.
Long-term statistics are preserved.

### ⚠️ Breaking — minimum Home Assistant version is now 2026.8.0

Previously 2024.1.0. `via_device_id`, used to link circuit devices to their
plant, was introduced in HA 2026.8; on 2026.7 and earlier the device-registry
call raises `TypeError` and **no circuit entity is created**. `hacs.json` now
declares the 2026.8.0 floor, and `async_setup_entry` re-checks it at runtime so
a manual install fails with an explanatory message rather than an opaque error.

**Users on HA older than 2026.8 should stay on v0.21.1**, which remains
functional until HA 2027.8.

### Changed
- **Options flow now reloads the config entry** (`config_flow.py`,
  `__init__.py`): `HovalConnectOptionsFlow` derives from `OptionsFlowWithReload`,
  and the config-entry update listener plus `_async_options_updated()` are gone.
  Home Assistant raises `ValueError` when an entry carries update listeners while
  such a flow saves options — this is already enforced, not a future change.
  Removing the listener also clears the separate 2026.12 deprecation on
  `async_update_reload_and_abort()` in the reauth flow. Saving options now costs
  a brief reload; the polling interval, turn-on mode and override duration are
  all re-read from `async_setup_entry()`.
- **Circuit devices link to their plant by device ID** (`__init__.py` and all six
  circuit platforms): `via_device=(DOMAIN, plant_id)` became
  `via_device_id=<plant DeviceEntry.id>`. `via_device` was removed from HA's
  `DeviceInfo` in 2026.9 and drops out of the device registry in 2027.8.
  A new `HovalPlantDevices` resolver registers plants and caches their device
  IDs, resolving on demand so a plant discovered after setup still gets a parent
  device — an unresolvable `via_device_id` raises `DeviceInfoError` and the
  entity is dropped, where the old `via_device` only logged.
  **Circuit identifiers are deliberately unchanged** (`<plant_id>_<path>`), so
  existing devices are matched rather than duplicated.
- **Percentage sensors use `UnitOfRatio.PERCENTAGE`** (`sensor.py`, 9
  descriptions). Style alignment with the file's existing `UnitOfTemperature` /
  `UnitOfEnergy` / `UnitOfTime` usage, **not** a deprecation fix: `PERCENTAGE` is
  still a supported HA constant, defined as `UnitOfRatio.PERCENTAGE.value`. The
  emitted unit is `%` either way.
- **Coordinator receives its config entry explicitly** (`coordinator.py`,
  `__init__.py`): `HovalDataCoordinator(hass, entry, api, health_store)` instead
  of relying on Home Assistant's `current_entry` ContextVar.
- **Entity platforms type their callback as `AddConfigEntryEntitiesCallback`**
  (all seven platforms), the correct type for config-entry platforms.

### Removed
- `_enable_turn_on_off_backwards_compat` (`climate.py`) — the attribute no longer
  exists in Home Assistant's climate platform and had no effect.

### Testing
- Suite grows from **191 to 284 tests**; coverage **44% → 61%**. The migrated
  files previously had **0%** coverage.
- New `tests/test_ha_compat.py` covers the options-flow lifecycle, the device
  parent link, device/entity identity stability, the plant-device resolver
  (caching, late plants, registration ordering), unit equivalence, the version
  floor, and static guards against reintroducing any API Home Assistant removes
  on or before 2026.12.
- `tests/ha_stubs.py` adds realistic Home Assistant stand-ins so the entity
  platforms and config flow are genuinely imported and exercised rather than
  grepped.
- Every guard is mutation-tested: 13 deliberate reversions of this migration were
  each confirmed to turn the suite red.



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
