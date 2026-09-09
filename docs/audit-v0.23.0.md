# Hoval Connect Integration — Cloud-API Compatibility & Bug-Fix Audit (v0.23.0)

| | |
|---|---|
| **Subject** | `custom_components/hoval_connect` v0.21.1 → v0.23.0 |
| **Audit date** | 2026-09-09 |
| **Trigger** | User report: integration failing with HTTP 403 on every request since a Hoval-side change, plus a config-UI report that the Polling interval field renders empty in the options dialog while every other field loads correctly |
| **Scope** | `api.py`, `const.py`, `coordinator.py`, `config_flow.py`; one forensic crawl report (`hoval_output.txt`, ~1,880 lines) produced by a one-off diagnostic script (`crawl.py`) run by the user against the live cloud API with real credentials |
| **Method** | Full read of the crawl report against the shipped integration source, line-by-line diff of every response shape against what the coordinator/API client expects, targeted source-code tracing of every code path touched by a proposed fix (not just the fix itself) to catch second-order regressions before they ship |

**Version-numbering note:** the manifest shipped immediately before this
audit read `"version": "2.22.0"`, and the prior `CHANGELOG.md` entry was
headed `[2.2.0]` — both almost certainly a typo for `0.22.0` (this
repository's own stated policy, top of `CHANGELOG.md`, is to stay pre-1.0).
This release is numbered `0.23.0`, continuing correctly from `0.21.1`, per
`CHANGELOG.md`'s `[0.23.0]` entry.

## 1. Executive summary

The reported 403 is **not** caused by a moved endpoint, a revoked OAuth
client, or a changed authentication flow — the crawl proves all three are
still exactly as the integration expects, using a fresh login against the
unmodified `CLIENT_ID`/`IDP_URL`/`BASE_URL` in `const.py`. Every endpoint the
integration calls returned its expected (or an additively-changed, harmless)
shape during the crawl.

The one variable that differed between "crawl: works" and "integration:
403" is the outbound `User-Agent`: the crawl script sent a distinctive
custom one; the integration's `api.py` never sets one at all and inherits
whatever Home Assistant's shared `aiohttp` session applies by default. The
API sits behind an Azure Application Gateway (visible in unrelated 502 error
pages the crawl received probing metadata endpoints), which is a common
place to enforce User-Agent allow/deny rules. **This is the leading
hypothesis, not a confirmed root cause** — see §4 for what would confirm or
rule it out, and what to do if 403s persist after this release.

Independently, the crawl surfaced two areas of real (but non-403-causing)
API drift, and the user separately reported a config-flow display bug. All
three are fixed in this release:

1. `GET .../circuits/{path}/settings` no longer returns `weatherImpact` for
   any circuit tested — the coordinator no longer claims the feature is
   supported when the key is simply absent.
2. `GET .../circuits/{path}/programs` returns HTTP 417 (never 200) for BL
   (boiler) circuits — the coordinator no longer calls it for that type.
3. The options-flow "Polling interval" dropdown had no entry for 600 seconds
   ("10 minutes"), so a stored value of 600 couldn't be matched against any
   option and the field rendered empty — even though every sibling field on
   the same form, whose stored values *did* have matching options, rendered
   correctly. Fixed by adding the missing entry.

**Verification result:** all pre-existing tests pass; new tests added for
every change below (§5). No entity IDs, unique IDs, device identifiers,
units, or stored-data schemas change. No breaking change.

## 2. What the crawl proves did *not* change

All of the following returned their expected status and an expected-or-
compatible shape, using a brand-new password-grant login performed at crawl
time (i.e. not relying on any cached/stale token from a prior session):

| Endpoint | Result |
|---|---|
| `POST {IDP_URL}` (password grant, current `CLIENT_ID`) | 200, valid `id_token`/`access_token`/`refresh_token` |
| IDP `.well-known/openid-configuration` | 200, `token_endpoint` matches `IDP_URL` exactly |
| `GET /api/my-plants` | 200, same shape (plain list), 2 new additive fields |
| `GET /v1/plants/{id}/settings` | 200, `token` present as expected, 3 new additive fields |
| `GET /v3/plants/{id}/circuits` | 200, same shape, many new additive fields, `path`/`type`/`selectable` unchanged |
| `GET /v1/plant-events/{id}` | 200, same shape, additive fields |
| `GET /v2/api/weather/forecast/{id}` | 200, unchanged shape |
| `GET /v3/api/statistics/live-values/{id}` | 200, unchanged shape |
| `GET /v3/plants/{id}/circuits/{c}/programs` (HK, WW circuits) | 200, `dayPrograms` still present, additive fields |

No 403 occurred anywhere in the crawl. This rules out the two most common
"Hoval changed their API" explanations (moved paths, rotated OAuth client)
as the cause of a *blanket* 403 — a path move would 404, and a revoked
client would fail the password grant itself, and the report shows neither.

## 3. Fixes in this release

### 3.1 `USER_AGENT` on every outbound request (`const.py`, `api.py`)

New constant `USER_AGENT` in `const.py`, applied to:
- the IDP password-grant `POST` in `_get_id_token()`,
- the plant-settings `GET` in `_get_plant_access_token()`,
- the shared `_headers()` builder used by every other call via `_request()`.

Chosen to be a fixed, descriptive string decoupled from the integration's
own release version (see the comment in `const.py`), so it never needs
bumping on every future release.

`_request()` also now logs HTTP 403 at `WARNING` with the response body
(truncated to 500 chars), rather than only surfacing it via the generic
`>=400` branch at `DEBUG` level. This does not change behaviour (still
raises `HovalApiError`, still not retried, since a 403 has not been observed
to be fixed by refreshing a token the way a 401 is) — it exists purely so
that if this diagnosis is wrong or incomplete, the *next* 403 is easy to
find and its body is captured automatically instead of requiring the user
to already have debug logging enabled.

### 3.2 `weatherImpact` key-presence check (`coordinator.py`)

Previously: `circuit_data.weather_impact_supported = True` was set whenever
`get_circuit_settings()` returned *any* dict, regardless of whether that
dict contained a `weatherImpact` key. The crawl shows the cloud now omits
the key entirely (returning only `{"circuitName": ...}`) for every circuit
tested. Left unfixed, this would have kept advertising the two "weather
based control" number entities as available with an unknown value, and any
attempt to drag their sliders would call `update_circuit_settings()`
(PATCH) against a field the cloud has apparently removed.

Fixed by checking `"weatherImpact" in settings` before setting
`weather_impact_supported = True`, in both the fresh-fetch branch and the
cached-fallback branch. A circuit type in `SUPPORTS_WEATHER_IMPACT` whose
settings response now lacks the key correctly reports its number entities
as unavailable (via `HovalWeatherImpactNumber.available`, unchanged) instead
of showing non-functional sliders.

This is a **graceful degradation, not a recovery**: if Hoval relocated the
feature to a different endpoint or a different field, that has not been
identified (see §4, Known limitations).

### 3.3 `SUPPORTS_PROGRAMS` gate (`const.py`, `coordinator.py`)

New constant, mirroring the existing `SUPPORTS_WEATHER_IMPACT` pattern:
`SUPPORTS_PROGRAMS = frozenset({CIRCUIT_TYPE_HV, CIRCUIT_TYPE_HK,
CIRCUIT_TYPE_WW})` — i.e. every currently-supported circuit type except BL
(boiler). The crawl showed `GET .../circuits/{path}/programs` returns HTTP
417 for BL on every attempt, including with alternate version prefixes
(`/v2`, `/v4`), never 200.

`need_programs` in `coordinator.py`'s `_fetch_circuit()` now additionally
requires `ctype in SUPPORTS_PROGRAMS`. This was **never a crash**: the
existing `asyncio.gather(..., return_exceptions=True)` isolation already
absorbed the 417 as an exception object, logged at `DEBUG` and otherwise
ignored. The fix only removes a guaranteed-to-fail round trip that repeated
on every `PROGRAM_CACHE_TTL` (5 min) refresh for every BL circuit.

**Regression caught during implementation, not shipped:** the existing
fallback line that reused a cached program value —
`results["programs"] = cached_prog[0]` — was unconditional on
`not need_programs`. Before this change that was always safe, because the
*only* way `need_programs` could be `False` was an unexpired cache
(`cached_prog` was therefore never `None` at that point). Gating
`need_programs` on circuit type as well means it can now be `False` with
`cached_prog is None` (a BL circuit, which never populates the cache at
all) — the old line would then evaluate `None[0]` and raise `TypeError` on
the very first poll of any BL circuit. Fixed by additionally requiring
`cached_prog is not None`, mirroring the pattern the `settings` cache
fallback two lines below it already used correctly. Covered by a new test
(§5) that fails against the naïve version of this fix.

### 3.4 Missing "10 minutes" polling-interval option (`const.py`)

`SCAN_INTERVAL_OPTIONS` offered `30, 60, 120, 300` seconds only. A config
entry with a stored `scan_interval` of `600` (10 minutes) — an option users
are told exists, and the natural doubling of the existing 5-minute choice —
had no matching key in the dict backing the options-flow dropdown
(`vol.In(SCAN_INTERVAL_OPTIONS)` in `config_flow.py`). Home Assistant's
frontend pre-selects a `select` control by matching the schema's `default=`
against the option list; when there is no match, the control renders with
no selection at all, i.e. empty — while the `turn_on_mode` and
`override_duration` fields on the same form, whose stored values *did* have
matching entries, rendered normally. This produced exactly the symptom
reported: "all other configuration items loaded, the polling interval is
empty."

Fixed by adding `600: "10 minutes"` to `SCAN_INTERVAL_OPTIONS`. No change
to `config_flow.py` was needed — the `vol.Coerce(int)` validator added in
v0.19.0 (for a different, already-fixed bug: the frontend submitting
dropdown values as strings) already handles `600`/`"600"` correctly once
it's a valid option. This also self-heals any existing config entry that
already had `600` stored: it will now both display and re-save correctly on
next options-flow open, with no migration step required.

## 4. Known limitations

- **The `USER_AGENT` fix is a strong hypothesis, not a confirmed root
  cause.** No response body or headers from an actual 403 returned to a
  live Home Assistant instance were available at diagnosis time — the
  evidence is entirely "a UA-less request via a different client succeeded
  everywhere the integration calls." If 403s persist after upgrading to
  0.23.0, enable debug logging for `custom_components.hoval_connect` and
  capture the new `WARNING`-level "API {method} {path} -> HTTP 403" log
  line added in §3.1, including its response body — that will confirm or
  rule this diagnosis out definitively.
- **No replacement for `weatherImpact` was identified.** The crawl captured
  `GET /v3/api-docs` (a live OpenAPI document, ~1.16 MB) but the saved
  report only includes a truncated first ~90 KB of it. A full diff of that
  document against `docs/openapi-v3.json` (bundled in this repo) against the
  live spec would be the next step to look for a relocated or renamed
  `weatherImpact` field before concluding the feature was removed outright.
- **Not executed against a live Hoval account.** As with prior audits in
  this repository, verification here is source-level plus the user-supplied
  crawl transcript, not a live end-to-end run of the patched integration.
  One manual smoke test against a real installation (in particular:
  confirming the 403 is actually gone, and that a BL-circuit poll cycle
  produces no new errors) is recommended before wide release.

## 5. Test coverage added

- `tests/test_api.py`: asserts `User-Agent: <USER_AGENT>` is present in the
  headers passed to `session.post()` (IDP call) and to `session.request()`
  (authenticated API calls), and that an HTTP 403 response raises
  `HovalApiError` without retrying.
- `tests/test_coordinator_fetch.py`: a BL circuit's `_fetch_circuit()` no
  longer calls `api.get_programs()` at all, and completes without raising
  even on the very first poll (i.e. with an empty program cache) — this is
  the regression described in §3.3 that a narrower fix would have shipped.
  A second case confirms an HK/WW circuit whose settings response omits
  `weatherImpact` entirely ends up with `weather_impact_supported = False`,
  while one whose response includes `weatherImpact` with null sub-fields
  still ends up `True`.
- `tests/test_ha_compat.py` (or equivalent config-flow test): the options
  flow's generated schema for `CONF_SCAN_INTERVAL` accepts and defaults to
  `600` when a config entry has that value stored, and `600` round-trips
  through the `vol.Coerce(int)` / `vol.In(...)` validator the same way `300`
  already does.

## 6. Rollback

This release makes no schema, identifier, or storage changes. Downgrading
to v0.21.1 restores prior behaviour, including the reported 403 and the
empty polling-interval field.
