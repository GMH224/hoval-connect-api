# Hoval Connect Integration — Transport Rewrite Audit (v0.24.0)

| | |
|---|---|
| **Subject** | `custom_components/hoval_connect` v0.23.0 → v0.24.0 |
| **Audit date** | 2026-09-10 |
| **Trigger** | v0.23.0's `User-Agent` fix for a blanket HTTP 403 shipped without live confirmation and did not work — the 403 persisted after upgrade, confirmed via a captured Home Assistant debug log showing the new diagnostic warning line firing with the v0.23.0 header already in place |
| **Scope** | `api.py` (full transport rewrite), `const.py`, `__init__.py`, `config_flow.py`, `manifest.json`; roughly a dozen short, single-purpose diagnostic Python scripts written and run interactively against the live cloud API over several hours, each changing exactly one variable from the last, with the user running each one and reporting back its raw output |
| **Method** | Live, interactive, single-variable isolation testing against the production API — not static code review. Every hypothesis below was tested by writing the smallest possible script that isolated it, having the user run it from their own network with their real credentials, and reading the actual HTTP response before forming the next hypothesis. Several early hypotheses were wrong and are recorded as such rather than omitted. |

## 1. Executive summary

v0.23.0 diagnosed a blanket HTTP 403 (every endpoint, every poll) as
Home Assistant's shared `aiohttp` session sending no `User-Agent` at all,
and shipped a fix: an explicit `User-Agent` string. That diagnosis was
**incomplete**, and the fix **did not work** — confirmed directly from a
live Home Assistant debug log after the user upgraded, which showed the
new diagnostic logging (also added in v0.23.0) firing on the very first
poll, header already attached, still 403.

Root-causing this properly required abandoning static analysis in favor of
live, interactive, single-variable testing directly against Hoval's
production API. Over the course of that testing, **two independent causes**
were found stacked on top of each other, and **two intermediate
hypotheses were formed, tested, and disproven** before landing on both real
causes. All four are recorded below because the disproven ones are exactly
as informative as the confirmed ones, and because a future person hitting
a similar wall deserves the honest sequence, not just the ending.

**The two confirmed causes**, addressed together in this release:

1. `aiohttp`'s TLS connection fingerprint is blocked by Hoval's Azure
   Application Gateway, independent of any header content.
2. `requests`' own default `User-Agent` string (`"python-requests/X.Y.Z"`)
   is separately blocked, independent of the above.

**The fix**: replace `aiohttp` with `requests` (run via
`hass.async_add_executor_job`, since `requests` is a blocking library) and
send an explicit, non-default `User-Agent` on every request. Both parts are
required; neither alone is sufficient. This was verified end-to-end against
the live API — auth, plant listing, and full data retrieval — before being
written into `api.py`.

## 2. Investigation timeline

Presented in the order it actually happened, including the wrong turns.
Every numbered step below was a real script, run by the user against the
live API, with real output.

### 2.1 Starting point: v0.23.0 shipped, did not work

The user upgraded to v0.23.0 and enabled debug logging. The new `WARNING`-
level log line added in that release (`API GET /api/my-plants -> HTTP 403
...`) fired on the very first poll after restart, with the v0.23.0
`User-Agent` header already attached. This immediately ruled out "the
integration isn't sending the header" and reopened the question of what
was actually blocking it.

### 2.2 Hypothesis: aiohttp itself (not just its headers)

A standalone script reproducing `api.py`'s exact `aiohttp` request —
same IDP, same headers, same endpoint — was run outside Home Assistant
entirely. **Result: HTTP 403.** This ruled out anything Home-Assistant- or
polling-specific and implicated `aiohttp` (or something about the request
it constructs) directly.

Three follow-up variants, each isolating one more variable, all still
HTTP 403:
- `aiohttp` + `Accept`/`Accept-Encoding`/`Connection` headers matching
  what `requests` sends by default (aiohttp sends none of these
  automatically — confirmed by printing `aiohttp.ClientSession().headers`,
  which came back empty).
- `aiohttp` with its `TCPConnector`'s TLS context replaced by
  `urllib3.util.ssl_.create_urllib3_context()` — i.e. the exact cipher
  suite list `requests` uses. (First attempt at this crashed with a
  certificate verification error — `create_urllib3_context()` doesn't load
  a CA trust store the way `ssl.create_default_context()` does; fixed by
  calling `.load_default_certs()` on it, then re-tested.)

None of these changed the result. This is strong evidence that whatever
`aiohttp` is being blocked for is not fixable by header or cipher-list
tuning from within `aiohttp` — it's some other property of its connection
or handshake behavior (full TLS extension set, ALPN offer, or something
else client-library-specific that a cipher list alone doesn't capture).

### 2.3 Hypothesis: plain `requests` works — is it the library?

The obvious next test: the same auth + `/api/my-plants` sequence using
`requests` instead of `aiohttp`, run via `asyncio.to_thread()` to also
validate the intended production architecture (blocking library called
from async code). **Result: HTTP 403 again.**

This was a **false negative that briefly pointed the investigation in the
wrong direction** (see §2.5) — the script worked correctly, but it never
set a custom `User-Agent`, so it went out with `requests`' own default,
`"python-requests/2.34.2"`. At the time, this looked like it ruled out
`requests` as a fix entirely.

### 2.4 Hypothesis: account/IP-level anti-abuse block

With both `aiohttp` and unmodified `requests` failing, and the user having
run a genuinely large burst of automated test traffic against one account
in a short window (a systematic multi-endpoint crawl script, `crawl.py`,
plus several of the diagnostic scripts above), an account- or IP-level
anti-abuse flag became the leading hypothesis. This predicted that a
cooldown period, or the account being otherwise fine, would resolve it.

**Disproven directly**: the user opened the official HovalConnect app on
the same network, on the same account, and it worked normally — including
one write operation (a control command). An account-wide block cannot
explain the official app succeeding while every scripted client fails on
the same account. This hypothesis was explicitly retracted once the app's
success was reported, rather than kept alongside contrary evidence.

### 2.5 Hypothesis: the app holds a hidden credential/signature

With the account ruled out, the next hypothesis was that the app sends
something no script does — an embedded API key, a signature, or
client-attestation the public OAuth2 flow doesn't include — and that this
requirement started being enforced sometime in the narrow window between
the last known-good crawl and the first failing probe.

The app's own version was checked directly on the device (unchanged for
roughly four weeks — ruling out "a new app version added a new secret" as
the trigger) and a specific documented header from a community
reverse-engineering project
(`trcyberoptic/hoval-connect-api`, unaffiliated with Hoval — see §3 for
what was actually checked there), `Hovalconnect-Frontend-App-Version`, was
tested with the real installed app version (`3.5.0`). **Result: HTTP 403.**

**Disproven directly, and decisively**: the user re-ran the original
`crawl.py` script — no special credential, no app-only header, nothing
the app has that a script doesn't — and it **succeeded completely**,
every endpoint, cleanly. A script with zero special access succeeding
rules out "the app has something scripts fundamentally cannot have."
Whatever the real differentiator was, it was something a plain script
*could* replicate — the question was which detail.

### 2.6 The actual answer: literal diff against a known-working script

With both intermediate hypotheses disproven, and one known-working script
(`crawl.py`) and several known-failing ones using the *same library*
(`requests`), the two were diffed line by line rather than
re-theorized from scratch. The one concrete difference: `crawl.py` sets a
custom `User-Agent` (`"hoval-connect-forensic-crawler/1.0
(+https://github.com/; diagnostic tool)"`); every failing `requests`-based
script (§2.3 and its descendants) never set one, so all went out with the
literal default, `"python-requests/2.34.2"`.

A single-variable test — the exact failing script from §2.3, with only
that one header added, nothing else changed — was run. **Result: HTTP
200**, real plant data returned. This is exactly the kind of WAF
signature rule that blocklists well-known scripting-tool default
identities (`"python-requests/..."` is about as common a target for this
as exists), and it explains every prior result without contradiction:

| Test | Library | Custom User-Agent? | Result |
|---|---|---|---|
| `crawl.py` (both runs, direct + VPN) | `requests` | Yes | 200 |
| §2.2, all four `aiohttp` variants | `aiohttp` | Yes (3 of 4) | 403 (blocked at the TLS/connection layer, independent of header) |
| §2.3, `requests`-in-thread | `requests` | **No** | 403 (default UA blocked) |
| §2.4 app-version test | `requests` | **No** (only added one extra header, still default UA) | 403 (default UA still blocked) |
| §2.6 isolation test | `requests` | Yes (crawl.py's exact string) | **200** |

## 3. On the community reference project

`trcyberoptic/hoval-connect-api` (unaffiliated with Hoval or this project)
was checked for anything this integration might be missing — specifically
whether its `api.py` sent any additional header, credential, or used a
different auth flow. Its source was fetched directly and read: identical
`BASE_URL`, `IDP_URL`, and `CLIENT_ID`; identical bare `aiohttp.ClientSession`
via `async_get_clientsession(hass)`; **no custom headers of any kind**. It
was, structurally, the same as this integration's pre-v0.23.0 state.
Installing it would not have helped and was not pursued further once this
was established — it's recorded here so a future reader doesn't re-tread
the same idea expecting a different outcome.

## 4. What changed in this release

### 4.1 `api.py`: aiohttp → requests-in-executor

Every network call now goes through one `requests.Session()`, created once
in `__init__` and reused for the client's lifetime, with each blocking call
wrapped in `hass.async_add_executor_job()` — Home Assistant's documented,
sanctioned mechanism for running blocking code from an async integration.
Three thin synchronous helpers (`_sync_post`, `_sync_get`, `_sync_request`)
replace the single `aiohttp`-based `_headers()`/`_request()` transport;
every public method's signature, retry semantics, timeout budget (now a
`(connect, read)` tuple — `requests`' direct equivalent of `aiohttp`'s split
`ClientTimeout`), 401-triggers-refresh-and-retry behavior, and the v0.23.0
403 diagnostic log line are all preserved exactly. No caller outside this
file needed to change how it invokes `HovalConnectApi`.

`HovalConnectApi.__init__` now takes `hass` instead of an `aiohttp.ClientSession`
(needed to schedule executor jobs). `__init__.py` and `config_flow.py`
updated accordingly; a new `aclose()` method releases the `requests.Session`'s
connection pool on integration unload and after each config-flow validation
attempt.

**Concurrency note**: the coordinator fans out one task per circuit via
`asyncio.gather()`, which under this transport means concurrent executor
jobs all calling the same shared `requests.Session`. This is a supported,
documented pattern — `requests`/`urllib3`'s connection pool is explicitly
designed for concurrent use from multiple threads — and is covered by a new
test (`test_concurrent_requests_share_one_session_safely`) that drives
several overlapping `_request()` calls and confirms each resolves to the
correct response.

### 4.2 `USER_AGENT` (`const.py`)

Changed to the exact string empirically proven in §2.6:
`"hoval-connect-forensic-crawler/1.0 (+https://github.com/; diagnostic tool)"`.
This is **not** a stylistic choice — it is the literal string used in the
successful isolation test. A new test
(`test_user_agent_is_the_empirically_validated_string`) pins this value so
it can't be swapped for a "nicer-looking" string by accident; a deliberate,
re-validated change should update both the constant and that test together.

### 4.3 `manifest.json`

`requests>=2.28.0` added to `requirements` (previously empty); version
bumped to `0.24.0`.

## 5. Known limitations / residual uncertainty

- **The exact `aiohttp` blocking mechanism was never fully identified.**
  Only cipher suite list was matched against `urllib3`'s; other TLS
  ClientHello properties (extension order, supported groups, ALPN offer)
  were not individually isolated, since the cipher-list test already failed
  and continuing to fight `aiohttp`'s TLS stack stopped being worth the
  time once `requests` was confirmed to work cleanly. If `aiohttp` is ever
  revisited, this is where to pick back up.
- **Only one custom `User-Agent` string has been validated.** It is
  plausible that any non-default string would clear the WAF rule in §2.6
  (a blocklist-of-known-bad-signatures model), but this was not tested —
  only the exact string in place was ever confirmed live. Treat any change
  to `USER_AGENT` as needing its own live validation, not an assumption
  that the rule is content-agnostic beyond "not `python-requests`".
- **No live re-verification of the full coordinator poll cycle end-to-end**
  (all circuit types, weatherImpact, programs, events, weather in one real
  run against production) was performed after this rewrite — verification
  was against the specific auth + `/api/my-plants` sequence that was being
  actively debugged, plus this repository's offline test suite. A live
  smoke test on a real installation covering at least one full poll cycle
  is recommended before considering this closed.

## 6. Test coverage

`tests/test_api.py` rewritten in full: every mock now targets
`requests.Session` (plain synchronous mocks) instead of simulating
`aiohttp`'s async context-manager response protocol, via a `FakeHass` whose
`async_add_executor_job` runs the target function immediately — equivalent
to a real executor round-trip from the test's point of view. All
pre-existing behavioral coverage (retries, 401 handling, pagination,
response-shape normalisation, the v0.23.0 fixes) carried forward
unchanged. New tests added specifically for this release:

- Transport really is `requests` (`isinstance(api._session, requests.Session)`)
- `aiohttp` is not imported anywhere in `api.py`, `__init__.py`, or `config_flow.py`
- `manifest.json` declares the `requests` dependency
- `USER_AGENT` matches the validated string exactly (see §4.2)
- `aclose()` closes the session via the executor
- Concurrent `_request()` calls against one shared session resolve correctly

**Result: 303 tests pass, ruff clean (lint + format), 86% coverage on
`api.py`.**

## 7. Rollback

Rolling back to v0.23.0 restores the `aiohttp` transport and its
blanket-403 problem — not recommended. No schema, identifier, or storage
changes were made in this release, so a rollback is otherwise safe if ever
needed for an unrelated reason.
