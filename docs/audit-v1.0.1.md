# Hoval Connect Integration — v1.0.1 Audit Response

| | |
|---|---|
| **Subject** | `custom_components/hoval_connect` v1.0.0 → v1.0.1 |
| **Date** | 2026-09-11 |
| **Trigger** | Independent ICS-style audit (`Hoval_Connect_HACS___Independent_ICS_Quality_Audit.md`) run against the **deployed** v1.0.0 artifact, commissioned after the user's automation scenario raised concrete concurrency questions |
| **Scope** | Patch release. No architecture change, no new capability. Six confirmed defects fixed (ICS-001, -002, -004, -006, -007, -008); four deferred by explicit decision (ICS-003, -009, -010, -011, -012) |
| **Method** | Every finding re-verified against the actual code before acting. For each fix, the codebase was then **swept for the same shape** rather than patching only the reported line — a change in method adopted specifically because of §4 below |

## 1. The automation scenario that prompted this round

The user described a real automation writing three values in close
succession on the same circuit:

1. `number.*_weather_based_control_outside_temperature`
2. `number.*_weather_based_control_solar_radiation`
3. `select.*_program`

**That specific sequence is safe, and was already safe in v1.0.0.** The
audit's own §2 traces it and concludes the writes are "fundamentally
serialized". Verified independently: the two number entities hold
*separate* debounce tasks so they never cancel one another; `select.py`
does not debounce at all; and all three funnel through the coordinator's
single `control_lock`, which serialises the actual API calls.

The audit's executive summary nonetheless calls ICS-001 "the most
important finding for the requested automation scenario", which sits in
tension with its own §2. §2 is correct. ICS-001 is real, but it is **not**
triggered by this automation — it needs *the same entity* written twice
inside the 1.5 s debounce window.

This distinction mattered: it changed ICS-001 from "urgent, blocks the
user's automation" to "worth fixing because the mechanism is now
understood and the fix is contained".

## 2. ICS-001 — a cancelled write can still land, and land last

**Confirmed.** The mechanism, traced precisely:

`_cancel_debounce()` cancels the pending task. If that task is still in
its `asyncio.sleep(DEBOUNCE_SECONDS)`, cancellation is clean — nothing has
been sent. But if it has already reached `_send_value()`, the HTTP request
is running in Home Assistant's executor thread pool (see v0.24.0's
requests-in-executor transport). `concurrent.futures` cancellation only
succeeds *before* a worker picks the job up, so the request continues
regardless.

What cancellation *does* achieve is unwinding the coroutine — which
releases `control_lock`. The newer write then acquires the lock, runs, and
completes while the older request is still in flight. The older value can
land **after** the newer one and win.

**Fix.** Cancellation stays effective during the debounce sleep, where it
belongs and where nothing has been sent. Once a task has committed to its
API call it is left alone to finish; the newer write queues behind it on
`control_lock` and lands after it. **Ordering is delivered by the lock
rather than by a cancellation that cannot deliver it.**

Cost: in that narrow window both writes are sent rather than one. An extra
write with the correct final state is strictly better than one write with
the wrong one.

Implementation detail worth preserving: the committed task is tracked as a
**task reference**, not a boolean. A boolean would make a *sleeping* task
non-cancellable whenever any *other* task happened to be mid-send, which
would silently degrade debouncing under exactly the rapid-input conditions
it exists for. Applied identically in `number.py` and `fan.py`.

Not fixed, and unchanged from v1.0.0's `docs/audit-v1.0.0.md` §16.2: the
underlying inability to cancel an in-flight executor request. A complete
solution is per-circuit write serialisation (a queue or generation
counter), which remains a genuine design change rather than a patch.

## 3. ICS-002 — new circuits were seen and thrown away

**Confirmed, and self-inflicted.** `_refresh_circuit_values()` — added
only one release earlier — skipped unknown circuits with this comment:

> *"discovering a NEW circuit needs the full path … which is
> `_fetch_all_data()`'s job, triggered by the plant-level topology
> detection above."*

**That claim was false.** Plant-level detection fires only on a brand-new
plant or an offline→online transition. A circuit added to an
already-online plant matches neither. So the circuits response listed it
every single cycle and the code deliberately discarded it — no entity, no
warning — until a restart or an unrelated write happened to trigger a full
fetch.

It was also *worse than before that release*: previously the scheduled
check never fetched circuits at all, so "new circuits aren't discovered"
was an honest consequence of not looking. Afterwards it looked, saw, and
discarded — while a comment asserted otherwise. Same class of error as the
User-Agent comment in `docs/audit-v1.0.0.md` §19.2: **a comment claiming a
safety mechanism that does not exist is worse than no comment.**

**Fix.** An unknown circuit now sets `_pending_full_refresh_since`,
reusing the existing tested escalation path; the next cycle performs real
discovery. `_refresh_circuit_values()` keeps its narrow contract — it
refreshes values; `_fetch_all_data()` still owns discovery.

**A bug in the first version of this fix, caught by its own test.**
`plant_data.circuits` deliberately excludes unsupported types (SOL,
FRIWA …), so "absent from the snapshot" does **not** imply "new". Without
a supported-type filter, every unsupported circuit looked new on every
cycle and pinned `_pending_full_refresh_since` permanently on — silently
converting the lightweight health check into a perpetual full fetch, the
exact cost the v1.0.0 architecture exists to avoid.
`test_no_unknown_circuit_does_not_schedule_a_refresh` failed immediately
and is retained as the guard.

## 4. ICS-004, -006, -007 — the same mistake, three times

These are grouped because they are one pattern, not three coincidences.

- **ICS-004**: `_coerce_finite_number()` was added in v1.0.0's round 4 and
  applied to `targetValue`/`actualValue` — but not to `weatherImpact`,
  a few lines away in the same file. The write path clamped; the read path
  assigned raw API values straight into `NumberEntity` state, so `"abc"`,
  `NaN`, or an out-of-band `500` could become entity state in violation of
  the entity's own declared min/max.
- **ICS-006**: round 4 validated every *value* inside `error_counts`, but
  never that `error_counts` is a mapping. `.items()` on a persisted list or
  string raises `AttributeError` — and `restore_from_store()` sits
  *outside* the `try/except` added in that same round, which guards the
  **load**, not the **parse**. A corrupted diagnostics file could therefore
  block the entire integration from loading.
- **ICS-007**: `_get_health_check_interval()` in `__init__.py` was
  hardened; `config_flow.py`'s own independent `int()` on the same option
  was not. A corrupted value crashes the options dialog — leaving no UI
  route to fix the value that caused the crash.

In each case the earlier fix landed on the line it was pointed at and did
not sweep for the same shape elsewhere. This was already named as a known
weakness in this project's own v1.0.0 quality assessment; it recurring
three times in one audit is the clearest possible evidence for it.

**Method change adopted for this round:** every fix was preceded by an
explicit grep for the shape across the whole codebase, and what was swept
is recorded. Results:

- **ICS-004 sweep found six sites, not the four reported** — the
  optimistic-override fold-back path was also unvalidated. All six now
  route through two shared normalizers that reuse the *write* path's own
  clamp functions, so read and write agree on what "valid" means.
- **ICS-006 sweep** of the other three persisted reads in
  `restore_from_store()` confirmed them already safe (scalar checks, not
  container operations). The root cause was also fixed: the **parse** is
  now guarded in `__init__.py`, not just the load.
- **ICS-007 sweep found three unguarded reads, not one.** The other two
  (`turn_on_mode`, `override_duration`) fail *quietly* rather than loudly:
  a `default=` that is not one of the `vol.In()` keys renders the selector
  **empty** — precisely the v0.23.0 "Polling interval field renders empty"
  bug, recurring in two new places. All three now validate and fall back.

## 5. ICS-008 — a diagnostics failure could block cleanup

**Confirmed.** `async_save_health()` ran unguarded as the first step of
`async_unload_entry()`. An exception there (full disk, permissions,
serialisation bug) propagated out, skipping **both** remaining cleanup
steps — leaving background tasks uncancelled and the `requests.Session`
unclosed. Worse, because unload failed, Home Assistant would also refuse
to complete a *reload*, escalating a diagnostics-only problem into a stuck
integration.

**Fix.** The save is wrapped; failure logs a warning and continues.
Priority is explicit in the code: losing persisted diagnostics counters is
a non-event, leaking a connection pool or orphaning tasks is not. Ordering
(cancel tasks → close session → unload platforms) is unchanged and now
pinned by a test, since it encodes an earlier finding.

## 6. Deferred, by explicit decision

- **ICS-003** (offline plant leaves control entities available) —
  confirmed real, and a genuine v1.0.0 regression: in v0.24.x every poll
  was a full fetch, so an offline plant cleared `circuits` and entities
  correctly went unavailable. Deferred as a deliberate behaviour change
  that does not belong in a patch release.
- **ICS-009** (no optimistic program override), **ICS-010** (external
  Hoval-app change between GET and PATCH — inherent to a non-atomic
  two-field PATCH), **ICS-011** (token type validation), **ICS-012**
  (example-script defensiveness) — all confirmed as described, none
  affecting the user's automation, all deferred.

## 7. Verification

446 tests pass (up from 415), zero warnings under
`-W error::RuntimeWarning`, ruff clean. Coverage 65% overall;
`coordinator.py` 90%, `api.py` 90%, `config_flow.py` 46% (up from 27% —
the ICS-007 tests drive the real options-form path for the first time).

Every fix has dedicated regression tests. ICS-001's are source-contract
tests rather than behavioural: exercising the real race needs a live
executor and a controllable slow request, which this project's harness
does not provide. They pin the mechanism (marker set *after* the sleep,
cleared in a `finally`, cancellation refusing the committed task) in both
files.

**Unchanged from v1.0.0 and still true:** none of this has been validated
against the live API. 0.24.1 remains the supported rollback target.
