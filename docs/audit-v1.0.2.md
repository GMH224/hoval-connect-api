# Hoval Connect Integration — v1.0.2 Audit Response

| | |
|---|---|
| **Subject** | `custom_components/hoval_connect` v1.0.1 → v1.0.2 |
| **Date** | 2026-09-16 |
| **Trigger** | Independent ICS-style defect analysis (`hoval-connect-api-G_v1_0_1_ICS_defect_analysis.md`), commissioned as a full-codebase audit rather than in response to a specific incident. The auditor was given the code and test suite but **not** `docs/audit-v1.0.0.md`/`v1.0.1.md`, so several findings re-describe defects a prior round had partially, but not fully, addressed — see §2. |
| **Scope** | All 40 findings (8 Critical, 23 High, 8 Medium, 1 Low) verified against source line-by-line before any fix was written. 38 confirmed accurate as written; 2 corrected (below) before fixing. All 40 fixed in this release. |
| **Method** | Each finding was checked against the real code path it cited — not just its own excerpt — before being trusted. Two corrections came out of that verification, agreed with the user before proceeding. Every fix has a regression test; the full suite (497 tests, up from 446) passes under `-W error::RuntimeWarning`, and `ruff check`/`ruff format --check` are clean. |

## 1. Corrections made before fixing

### ICS-HIGH-013, downgraded to Medium and retitled

Filed as *"`get_plants()` pagination lacks per-page and total-item caps."*
False as written: `api.py` already had `_MAX_PLANT_PAGES = 50`, added in an
earlier round specifically to bound runaway pagination, and it already
failed closed (raised `HovalApiError`) rather than silently truncating.
The real, narrower gap — nothing checked that a *single page* didn't
contain far more items than the requested page size — is fixed here as
`_MAX_PLANTS_PER_PAGE`, alongside the pre-existing page-count cap.

### ICS-CRIT-007, wording corrected, severity unchanged

Filed as "the integration GETs current weatherImpact... builds a merged
object from stale data... PATCHes the stale object." Overstated: the code
already prioritizes a fresh GET immediately before merging, under the
control lock, specifically to close a staleness gap a prior round had
flagged. The real, still-open gap is narrower: no ETag/If-Match/version
check protects that fresh-GET-then-PATCH window from a **genuinely
concurrent external writer** (the Hoval phone app, another HA instance).
That gap is real and is **not fully fixable client-side** — closing it
completely needs the cloud API to support conditional writes, which
nothing observed so far indicates it does. Documented as an accepted
residual risk (§3, CRIT-007) rather than "fixed."

## 2. Interaction with prior rounds

Several findings looked, on first read, like they might already be fixed
because the code has inline comments referencing earlier audit rounds
under short IDs (`ICS-001` through `ICS-008`, `HVC-001` through `HVC-013`)
that are **unrelated in numbering** to this document's `ICS-CRIT-*`/
`ICS-HIGH-*` IDs. Two are worth calling out because the overlap was closer
than a numbering coincidence:

- **CRIT-001**: `fan.py`/`number.py` already had a fix (internally labeled
  `ICS-001`) protecting a *committed* control write from being cancelled
  by a *newer debounced write on the same entity*. That fix does nothing
  for `coordinator.async_shutdown()`, which still cancelled every tracked
  task — including committed ones — unconditionally. Confirmed still open,
  fixed here at the transport layer instead (§3).
- **CRIT-008**: this is the same defect the v1.0.1 changelog recorded as
  **ICS-003** and *deliberately deferred* — "confirmed real... held back
  as a behaviour change that does not belong in a patch release." It is
  fixed in this release.

## 3. Fixes, by finding

### Critical

- **CRIT-001 / CRIT-002 — cancellation vs. session lifetime.** Both were
  really one root cause: cancelling the coroutine *awaiting* an executor
  job never stopped the underlying thread, which kept running `requests`
  I/O against `self._session` regardless. `api.py` now wraps every
  blocking job in its own shielded `Task`
  (`HovalConnectApi._run_blocking()`); a caller's cancellation (e.g. from
  `async_shutdown()`) no longer prevents `self._inflight` from accurately
  reflecting real completion, and `aclose()` drains genuinely in-flight
  work (bounded by `_CLOSE_DRAIN_TIMEOUT = 35s`) before closing the
  session. Tests: `test_api_v1_0_2_fixes.py::TestCritOneAndTwoDrainOnClose`.
- **CRIT-003 — single global control lock.** Replaced with one
  `asyncio.Lock()` per `(plant_id, circuit_path)`
  (`HovalDataCoordinator._get_circuit_lock()`), created lazily and
  reconciled against live topology (see HIGH-011). Writes to different
  circuits no longer wait on each other; writes to the same circuit are
  still fully serialized.
- **CRIT-004 — unbounded concurrent circuit fetch.** Bounded by
  `asyncio.Semaphore(_MAX_CONCURRENT_CIRCUIT_FETCHES = 8)`, shared across
  one refresh cycle.
- **CRIT-005 / MED-003 — no positive write confirmation; failures
  invisible.** A new `_confirm_write()` compares the post-write refresh's
  real state against what was requested and, on mismatch, logs at WARNING
  and records `connection_health.unconfirmed_write_count`/
  `last_unconfirmed_write_at` (surfaced in diagnostics). Entity state stays
  optimistic by design (reverting would fight a user who's since changed
  it again) — this makes a persistent mismatch **visible**, which it
  previously was not. The background-refresh exception handler was
  bumped from DEBUG to WARNING for the same reason.
- **CRIT-006 — retries could duplicate a write.** `_SAFE_RETRY_METHODS =
  {"GET", "HEAD"}`: a timeout/connection-error/retryable-status on a
  POST/PATCH/DELETE now gets exactly one attempt. The 401-triggered
  token-refresh retry is unaffected (a 401 is a definite pre-write
  rejection, not an ambiguous outcome).
- **CRIT-007 — weather-impact read-modify-write race.** See §1. Mitigated
  as far as is possible client-side; the residual external-writer race is
  now stated explicitly in `async_set_weather_impact()`'s docstring as an
  accepted risk pending cloud-side conditional-write support.
- **CRIT-008 — offline plant, still-controllable entities.** `available`
  on climate/fan/number/water_heater/select now requires `plant.is_online`
  in addition to the circuit existing. This is the change v1.0.1 (as
  ICS-003) deliberately deferred as "a behaviour change" — accepted here
  as part of a full audit-remediation release rather than a narrow patch.

### High

- **HIGH-001 — stringly-typed booleans accepted as truthy.**
  `_coerce_bool()` (coordinator.py) requires a genuine `bool` for
  `isOnline`/`hasError`/`isSelectable`, falling back to a safe default
  otherwise instead of Python's `bool("false") == True` trap.
- **HIGH-002 / HIGH-003 — unvalidated identifiers, raw URL interpolation.**
  `_require_identifier()` and `_url()` (api.py) validate and
  percent-encode every plant/circuit-path segment; malformed circuit
  paths from the API are skipped with a warning rather than crashing the
  whole plant's refresh (a deliberate divergence from "fail the whole
  fetch" — one bad circuit should not take down every good one).
- **HIGH-004 — unbounded response body.** `_MAX_RESPONSE_BYTES` (8 MiB)
  checked via `Content-Length` and actual buffered size.
- **HIGH-005 — worst-case latency vs. the 90s coordinator timeout.**
  Addressed as a consequence of CRIT-006 (writes no longer retry, so
  worst-case latency per write dropped from ~57s to ~28s) — no separate
  change needed beyond documenting the arithmetic in `api.py`.
- **HIGH-006 — coroutine created before lock acquired.**
  `async_control_and_refresh()` now takes a zero-argument **factory**
  (called only once the per-circuit lock is held), not a pre-created
  coroutine. All 11 call sites across climate/fan/water_heater/select
  updated. As a side effect, this let HIGH-018 (below) be fixed at the
  same call sites for free.
- **HIGH-007 — circuits never removed from topology.**
  `_refresh_circuit_values()` now removes a circuit absent from
  `_STALE_CIRCUIT_CONFIRMATIONS = 2` consecutive responses (debounced, not
  on a single miss, to tolerate one transient truncated response).
- **HIGH-008 / HIGH-009 — duplicate-path race; collision-prone key.**
  Duplicate circuit paths are now deduplicated **before** any fetch is
  launched (not after `gather()`, which still let every duplicate race to
  write the same cache entries). The new-circuit-detection key is a
  `(plant_id, path)` tuple, not an underscore-joined string.
- **HIGH-010 / HIGH-011 — unbounded caches.** `api.py` gained
  `prune_plant_caches()`; `coordinator.py` reconciles
  `_program_cache`/`_settings_cache`/`_weather_impact_override`/
  `_mode_override`/`_circuit_locks` against live topology after every
  full refresh. A lock currently `.locked()` is never evicted.
- **HIGH-012 — partial plant list accepted as authoritative.**
  `_guard_against_empty_plants()` (renamed in spirit, not in name) now
  also distrusts a response reporting fewer than half the previously-known
  plant count, using the same debounced-confirmation mechanism as the
  original empty-response guard.
- **HIGH-013 — see §1.**
- **HIGH-014 — unconstrained program/duration values.** Validated against
  `_VALID_PROGRAMS`/`_VALID_DURATIONS_*` in `api.py` before being placed in
  a URL or request body.
- **HIGH-015 — JSON parsing on the event loop.** Moved into
  `_sync_request()` (already executor-thread work) via a precomputed
  `_precomputed_json`/`_precomputed_json_error` pair.
- **HIGH-016 — no jitter, `Retry-After` ignored.** `_retry_delay()` adds
  jitter and honors a sane `Retry-After` (capped, tolerant of garbage).
- **HIGH-017 — no circuit breaker.** A minimal `_CircuitBreaker` gates
  `_request()`; an open breaker fails fast without touching the network.
- **HIGH-018 — TOCTOU in resume-program resolution.** Fixed as a
  consequence of HIGH-006: the fresh read in `resolve_resume_program()` now
  happens inside the same factory the lock protects, in climate.py's
  `HVACMode.AUTO` branch, fan.py's `TURN_ON_RESUME` branch, and
  water_heater.py's heat-pump branch.
- **HIGH-019 — unsupported HVAC mode silently accepted.**
  `async_set_hvac_mode()`'s if/elif chain now has an explicit `else` that
  raises `HomeAssistantError`.
- **HIGH-020 — inconsistent schema enforcement.** `_coerce_optional_str()`
  applied to `operationMode`/`activeProgram` alongside HIGH-001's boolean
  coercion — the numeric weather fields were already normalized.
- **HIGH-021 — unshielded `aclose()` inside an expiring timeout scope.**
  Both `finally: await api.aclose()` blocks in `config_flow.py` now use
  `asyncio.shield()`.
- **HIGH-022 — release independent of CI.** `release.yml` rewritten:
  `create_release` now has `needs: [ruff, test, hacs, hassfest]`, all
  duplicated as jobs in the same workflow so a tag push cannot bypass them.
- **HIGH-023 — unpinned Actions.** All three workflow files now pin every
  action to a commit SHA (with the corresponding version in a comment).

### Medium

- **MED-001 — an older write could clobber a newer pending display
  value.** `_send_percentage()` only clears `_pending_percentage` if it
  still matches the value that call is sending.
- **MED-002 — no validation before display.** `async_set_percentage()`
  validates 0–100 integer before touching `_pending_percentage`/calling
  `async_write_ha_state()`.
- **MED-003 — see CRIT-005 above** (same fix).
- **MED-004 — device-registry cache never pruned.**
  `HovalPlantDevices.async_prune()` added, wired as a coordinator listener
  in `async_setup_entry()`.
- **MED-005 — unredacted error bodies in logs.** `redact_remote_error_body()`
  strips emails/JWTs/bearer-token-shaped substrings and truncates to 200
  chars (down from 500).
- **MED-006 — credentials as CLI arguments.** `examples/hoval_client.py`
  now reads `HOVAL_EMAIL`/`HOVAL_PASSWORD` from the environment or prompts
  interactively (`getpass` for the password).
- **MED-007 — no curl timeouts.** All three `curl` calls in
  `examples/get-live-values.sh` now pass `--connect-timeout 10 --max-time 30`.
- **MED-008 — mutation-check restore not exception-safe.**
  `tools/mutation_check.py`'s restore is now in a `finally`.

### Low

- **LOW-001 — bare `IndexError` on missing CLI argument.**
  `tools/check_ha_import_surface.py` now prints a usage message and exits
  with status 2.

## 4. Test coverage added

- `tests/test_api_v1_0_2_fixes.py` — 36 tests: CRIT-001/002 (shielded
  drain), CRIT-006 (no retry on writes), HIGH-002/003 (identifier
  validation/URL quoting), HIGH-004 (size bound), HIGH-013 (per-page cap),
  HIGH-014 (enum validation), HIGH-016 (jitter/Retry-After), HIGH-017
  (breaker), MED-005 (redaction).
- `tests/test_climate_v1_0_2_fixes.py` — 6 tests: CRIT-008
  (plant-online-gated availability), HIGH-019 (unsupported mode rejected).
- `tests/test_fan_v1_0_2_fixes.py` — 6 tests: MED-001 (pending-value
  clobber), MED-002 (percentage validation).
- `tests/test_ha_compat.py` — 3 new tests for
  `HovalPlantDevices.async_prune()` (MED-004).
- `tests/test_coordinator_fetch.py` — existing `_guard_against_empty_plants`
  suite extended in place to also cover the HIGH-012 partial-response case;
  `FakeApi` gained `prune_plant_caches()`.
- `tests/ha_stubs.py` — `StubCoordinatorEntity` gained an `available`
  property mirroring the real `CoordinatorEntity` base class (needed to
  test entity `available` overrides in isolation; previously untested
  entirely — no `test_climate.py`/`test_fan.py`/`test_number.py`/
  `test_water_heater.py` existed before this release).

497 tests total (up from 446), zero warnings under
`-W error::RuntimeWarning`, `ruff check` and `ruff format --check` clean.

## 5. Deferred / out of scope

None. All 40 findings (with the two corrections in §1) are fixed in this
release — the user's explicit direction was to fix everything the
verification pass confirmed, rather than triage by severity.

CRIT-007's residual external-write race (§1, §3) is not "deferred" in the
v1.0.1-changelog sense of a postponed decision; it is disclosed as a known
limit of what a client-only fix can achieve without cloud-side support for
conditional writes.
