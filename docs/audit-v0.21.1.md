# Hoval Connect Integration — Code Audit Report (v0.21.1)

| | |
|---|---|
| **Subject** | `custom_components/hoval_connect` v0.21.0 → v0.21.1 |
| **Audit date** | 2026-07-20 |
| **Scope** | Full integration source (~4,300 LOC), test suite, CI, packaging, documentation |
| **Framing** | ICS-adjacent: the integration reads and commands residential heating, ventilation and hot-water equipment via the Hoval Connect cloud. Not safety-critical (all setpoints are clamped to the vendor app's own ranges and the physical controller enforces its own limits), but availability, fail-safe behaviour, and silent-degradation modes are treated as first-class audit concerns. |
| **Method** | Full manual source review of every module; empirical reproduction of every crash-path finding; execution of the complete test suite, linters, and coverage before and after remediation. |

## 1. Executive summary

The integration entered the audit in unusually good shape for a community HA
integration: bounded retries with backoff, split connect/read timeouts,
single-flight token refresh, TTL-bounded optimistic UI state, cache-tiered
polling against a rate-limited upstream, persisted health telemetry, and a
redacted diagnostics export. **No finding allows commanding an unsafe
physical state**, and no credential or token handling defect was found.

The material weaknesses were concentrated in one dimension: **ingest
hardening**. The code trusted nested response shapes from an API with a
documented history of unannounced changes (v1 endpoint removal 2026-04,
pagination enforcement 2026-05), and its failure modes there were either
*silent* (a circuit and all its entities disappearing with only a debug log —
F1) or *total* (the entire poll failing and every entity going unavailable —
F2). Both were reproduced empirically before remediation, both are fixed, and
both are now locked in by behavioural regression tests.

Remediated in this release: **F1, F2, F3, F4, F5, F9** plus the test-suite
overhaul (item 6). Deferred with rationale: **F6, F7, F8** (§6).

**Verification result after remediation: 191/191 tests pass (order-independent),
ruff lint and format clean, coverage 44 % overall / 85 % on the two
highest-risk modules (`coordinator.py`, `api.py`), gate raised to 40 %.**

## 2. Findings and dispositions

Severity scale: **High** = plausible unsafe command or credential exposure
(none found) · **Medium** = plausible loss of monitoring/control availability
or silent degradation · **Low** = correctness, operability, or
maintainability defect.

### F1 — Nested program-schema drift silently dropped circuits — **Medium — FIXED**

*Location:* `coordinator.py` `_resolve_active_program_value()` and its call
site in `_fetch_circuit`.

*Evidence (reproduced pre-fix):* a day configuration missing `"id"` →
`KeyError`; a `week1`/`week2` value that is a list → `AttributeError`; a phase
missing `"start"`/`"end"` → `KeyError`. Each escaped `_fetch_circuit`, whose
task is gathered with `return_exceptions=True`, so the **entire circuit —
including live values that had already been fetched successfully — was
discarded for the poll**, all its entities went unavailable, and the only
trace was a debug-level log line. This is the same failure class as the
documented 2026-05 BL-circuit incident; the fix at that time guarded only the
top level of the structure.

*Remediation:* the resolver is now fully defensive at every nesting level
(non-dict `dayPrograms`, non-list/`id`-less day configurations, non-dict week
entries, non-list `dayProgramIds`, non-dict phases, non-dict/non-numeric
`start`/`end` are skipped or degrade to `None` fields). A second isolation
barrier at the call site try/excepts the whole program-processing block and
logs at WARNING, so *no* residual parsing exception can drop a circuit.
Failure now degrades exactly three sensor values (week name, day name,
program air volume) for one cycle.

*Regression tests:* `TestResolveActiveProgramRobustness` (12 cases including
mixed valid/invalid siblings) and
`TestFetchAllDataDegradation::test_f1_malformed_programs_keep_circuit_and_live_values`
(end-to-end through the real fetch pipeline).

### F2 — Events path unhardened against response-shape drift; failure was total — **Medium — FIXED**

*Location:* `api.py` `get_events()`/`get_latest_event()`;
`coordinator.py` `_fetch_all_data` events/weather post-processing;
`_parse_event()`.

*Evidence (reproduced pre-fix):* `get_circuits`, `get_live_values` and
`get_plants` all normalise the May-2026 `{"content": [...]}` pagination
wrapper; the two event endpoints did not. A wrapped events response reached
`events_result[:10]` in the plant loop — which runs **outside** the
per-circuit exception isolation — and raised, converting one cosmetic
endpoint's shape change into a **whole-poll failure**: every entity of every
plant unavailable, classified `ERROR_TYPE_UNKNOWN`. `_parse_event()` also
raised on any non-dict payload.

*Remediation:* both event endpoints normalise the wrapper in the client
(list endpoint → flat list, latest-event → first content element), degrading
to `[]`/`{}` on unrecognised shapes; `_parse_event()` returns an empty (and
never problem-flagging) event for non-dict input; the coordinator's events
block gained isinstance guards, per-entry dict filtering, and a try/except
isolation barrier that falls back to the cached events at WARNING; the
weather block validates its first forecast element. **Contract: event and
weather data can never fail a poll.**

*Regression tests:* `TestEventEndpointNormalisation` (8 API-level cases),
`TestParseEventGuard`, and `TestFetchAllDataDegradation` (hostile wrapper
past the API layer, non-dict list entries, endpoint failure with cache
fallback, malformed weather).

### F3 — Unbounded pagination; config-flow validation had no outer timeout — **Low/Medium — FIXED**

*Location:* `api.py` `get_plants()`; `config_flow.py` user + reauth steps.

*Evidence:* the pagination loop ran for as long as the server reported
`"last": false` — a misbehaving or tampered-with upstream produces an
infinite loop with unbounded memory growth. The coordinator's 90 s outer
timeout contained this on the polling path, but the config flow called
`get_plants()` with **no outer bound at all** (per-request timeouts do not
bound the loop, and `ClientTimeout` deliberately sets no `total`, so a
byte-dripping server also extends a single request indefinitely).

*Remediation:* pagination hard-capped at `_MAX_PLANT_PAGES = 50` (600
plants) with a WARNING on truncation; both config-flow validation paths now
run inside `asyncio.timeout(30)`, mapped to the existing `cannot_connect`
error.

*Regression tests:* `TestGetPlantsPageCap` (endless server truncated at
exactly the cap; normal 2-page pagination unaffected).

### F4 — Comment/behaviour drift on optimistic weather-impact overrides — **Low — FIXED (documentation)**

*Location:* `coordinator.py` `_weather_impact_override` comment,
`get_weather_impact_override()` docstring, end-of-fetch clearing comment.

*Evidence:* comments claimed the weather-impact override is "cleared once a
successful poll confirms real data"; only `_mode_override` is poll-cleared —
the weather override has always been TTL-only (120 s).

*Disposition:* the **code** is correct and the comments were fixed, not the
reverse. Poll-clearing the weather override would be wrong: circuit settings
are cache-tiered (10 min TTL) and are *not* re-fetched on every poll, so
clearing would flicker the slider back to a pre-update value; it would also
introduce a race for writes landing mid-poll. The intentional TTL-only
semantics are now documented at all three sites, including an explicit
"do not fix this" note in `CLAUDE.md` for future maintainers.

### F5 — Debounced slider write failures were invisible — **Low/Medium — FIXED**

*Location:* `fan.py` / `number.py` `_debounced_set`.

*Evidence:* debounced writes execute inside `hass.async_create_task`; a
raised `HomeAssistantError` had no caller to reach — it landed in the event
loop's unhandled-task logging. On a control path, a failed actuation was
effectively silent: the slider showed a value the device never accepted
until the next poll snapped it back, with no operator-visible signal.

*Remediation:* `_debounced_set` now catches `HomeAssistantError`, logs at
**WARNING** with the circuit path and requested value, and rewrites entity
state so the reversion is immediate and visible. The direct service-call
paths (`turn_on`/`turn_off`/`set_temperature`/`select_option`) already
propagated errors to the UI and are unchanged.

*Test note:* not unit-testable in this HA-free suite (entities subclass HA
bases); covered by the source-level review and slated for the HA-harness
phase (§6). The behaviour change is deliberately minimal (one try/except per
file) to keep review confidence high.

### F9 — Coordinator reached into the health tracker's private state — **Low — FIXED**

*Location:* `coordinator.py` `_async_update_data` ↔ `HovalConnectionHealth`.

*Evidence:* the coordinator mutated `_poll_times`, `_latency_samples` and
called `_record_error()` directly, leaving the counter semantics scattered.

*Remediation:* new public recording API — `record_poll_attempt(ts)`,
`record_poll_success(ts, latency_ms)`, `record_error(ts, type, msg,
is_auth=)` (renamed from `_record_error`; identical semantics). The
coordinator now performs zero private-member access on the tracker. The
persisted storage schema is unchanged (no `HEALTH_STORAGE_VERSION` bump
needed).

*Regression tests:* `TestConnectionHealthPollRecording` (attempt/success,
attempt/error, streak reset) plus every `_async_update_data` test exercising
the API end-to-end.

## 3. Item 6 — Test-suite remediation

**Root-cause fix.** The suite could not test the coordinator's async core
because `conftest.py` stubbed `DataUpdateCoordinator` with a MagicMock — and
subclassing a MagicMock "class" silently turns the subclass itself into a
mock, shadowing every real method (verified empirically during the audit).
The rewritten `tests/conftest.py` installs minimal **real** stub classes for
`DataUpdateCoordinator`, `UpdateFailed`, `ConfigEntryAuthFailed`,
`HomeAssistantError`, the dispatcher functions, and `homeassistant.util.dt`,
with MagicMock retained only for machinery the tested paths never exercise.

**New coverage** (`tests/test_coordinator_fetch.py`, 28 behavioural tests
against a scripted fake API):

- `_fetch_all_data` happy path: circuit-type filtering (selectable HV,
  non-selectable BL allowed, unsupported SOL excluded, path-less circuit
  skipped), v1→v3 program mapping, live-value parsing, program-name
  extraction, event/weather parsing and problem flagging, cache reuse within
  TTL (programs/events/weather fetched once across two polls, live values
  twice), discovery-signal fired exactly once, mode-override clearing,
  offline-plant short-circuit with PAT invalidation.
- Degradation guarantees: the F1 and F2 acceptance tests, live-values
  failure recording per-circuit health, the live-values type guard
  (previously only grep-asserted), events cache fallback, circuit-list
  failure → `_CircuitListError`.
- `_async_update_data`: success health accounting + debounced save,
  auth error → `ConfigEntryAuthFailed` with `auth` classification,
  circuit-list vs generic API error classification, unknown-exception
  recording and re-raise, failure→success streak reset.
- HK weather-impact settings: fetched and parsed for HK only, never for HV,
  stale-cache fallback on settings-endpoint failure.

**Pseudo-test cleanup.** Four grep-the-source "tests" whose guarded
behaviour is now behaviourally tested were deleted
(`test_programs_guard_uses_isinstance_dict`,
`test_resolve_guard_uses_not_isinstance_dict`,
`test_lv_raw_type_guard_present`, `test_restore_from_store_uses_try_except`).
The remaining source-text checks (climate/sensor live-value field names)
were consolidated under `TestSourceContracts` with an honest docstring: they
exist only because the entity platforms import
`homeassistant.components.*`, which this HA-free suite cannot stub, and they
should be replaced when the HA test harness lands (§6).

**Latent order-dependency bug removed.** `tests/test_coordinator.py`
carried a legacy module-level shim that **hard-assigned**
`sys.modules["homeassistant.exceptions"] = MagicMock()` (not `setdefault`),
overwriting the conftest's real exception stubs. This made four
coordinator-core tests pass or fail depending on which test files were
selected — discovered during this audit when the full suite diverged from
per-file runs. The shim is deleted; order-independence was verified by
running the suite in default and reversed file order.

**CI defect fixed.** `.github/workflows/lint.yml` did not install
`voluptuous`, which `tests/test_api.py` imports at module level; the
dependency is now in the install step.

## 4. Verification evidence (post-remediation)

| Check | Result |
|---|---|
| `pytest tests/` (default order) | **191 passed** (was 141) |
| `pytest` (reversed file order) | **191 passed** — order-independent |
| `ruff check .` | clean |
| `ruff format --check .` | clean (20 files) |
| Coverage, total | **44 %** (was 31 %), gate raised 30 → **40** |
| Coverage, `coordinator.py` | **85 %** (was 48 %) |
| Coverage, `api.py` | **85 %** (was 83 %) |
| Coverage, `const.py` | 100 % |
| New tests added | 53 (28 coordinator-core, 12 resolver robustness, 8 event normalisation, 2 page cap, 3 health API); 4 pseudo-tests removed |

Remaining uncovered code is concentrated in the entity platforms and config
flow (0 %), which cannot be imported without a full HA test harness — see
Residual risks.

## 5. Security review summary (unchanged findings, for the record)

- **Credentials**: email/password stored in the HA config entry (standard HA
  practice; HA encrypts storage at its own layer where configured) and sent
  to the SAP IAS IdP via OAuth2 ROPC over TLS roughly every 25 minutes.
  Inherent to the reverse-engineered API — the vendor offers no device/PKCE
  flow. The hardcoded `CLIENT_ID` is the vendor app's public client id, not
  a secret. No tokens are persisted to disk; the health store contains
  counters only.
- **Diagnostics export**: `config_entry` and `coordinator_data` sections are
  redacted (credentials, tokens, plant IDs, names, source paths — asserted
  by `tests/test_diagnostics.py`). The `connection_health` section exposes
  circuit paths and raw error strings (no secrets) — see F8, deferred.
- **Reauth flow** pins the account: re-authentication with a different email
  is rejected (`wrong_account`), preventing silent entry rebinding.
- **Command path**: all outbound setpoints clamped to vendor-app ranges
  (HV 15–100 %, climate 5–30 °C, WW ≤ 65 °C, weather-impact 0–100 / −10–0);
  control actions serialised under `control_lock`; optimistic state
  TTL-bounded (120 s) so it cannot mask reality indefinitely; **no code path
  issues a write in response to a read failure**.

## 6. Residual risks and deferred findings

| ID | Item | Severity | Rationale for deferral |
|---|---|---|---|
| F6 | Climate `HEAT` mode maps to the same action as `AUTO` (`reset_circuit` → `week1`), so selecting HEAT immediately reports AUTO; `reset_circuit` also hard-defaults week2 households back to week1 on "resume". | Low (semantic/UX) | Behavioural change to a control surface; needs a live-plant decision (map HEAT→`constant` vs. drop HEAT) rather than an audit-driven guess. |
| F7 | ID-token TTL hardcoded at 25 min instead of honouring the IdP's `expires_in`; a 200 response with invalid JSON escapes retry classification as `ERROR_TYPE_UNKNOWN`; single global PAT lock serialises multi-plant token refresh. | Low | All failure modes are handled (401→refresh churn; unknown-bucket classification); pure robustness polish. |
| F8 | `connection_health` diagnostics section is exported unredacted (circuit paths as keys, raw exception text). No secrets involved. | Low (privacy consistency) | Requires a redaction pass that preserves the section's debugging value. |
| — | Entity platforms, config flow, `__init__.py` at 0 % coverage; F5 fix and `TestSourceContracts` items untestable in the HA-free suite. | Medium (test debt) | Requires adopting `pytest-homeassistant-custom-component`; a deliberate second phase, since it changes the CI dependency footprint. Recommended next step. |
| — | Cloud dependency: no local fallback exists (the API is cloud-only); a Hoval outage takes all entities unavailable. | Accepted | Inherent to the platform (acknowledged HA/product limitation). Mitigated by the connection-health sensors, which support alerting on degradation from within HA. |
| — | Upstream API may change shape again without notice. | Accepted / mitigated | The v0.21.1 posture is: every list endpoint normalises the known wrapper, every parser degrades instead of raising, circuit and poll isolation barriers are regression-tested, and truncation/degradation events log at WARNING so drift is *visible* instead of silent. |

## 7. Release integrity

- `manifest.json` version: **0.21.1**; `CHANGELOG.md` and `CLAUDE.md`
  updated; no changes to entity unique IDs, stored options, translation
  keys, services, or the health-storage schema — upgrade requires only an
  HA restart, no migration.
- CI: ruff (lint + format), pytest with coverage (gate 40 %), HACS and
  Hassfest validation workflows unchanged apart from the `voluptuous` fix.

*End of report.*
