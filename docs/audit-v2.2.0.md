# Hoval Connect v2.2.0 — Home Assistant 2026.8 → 2026.12 Compatibility Audit

**Predecessor:** `hoval-connect-api-G_v0.21.1`
**Target:** Home Assistant Core **2026.8 – 2026.12**
**Audit date:** 2026-09-04
**Method:** verification against Home Assistant source, not release notes

---

## 1. Executive summary

v0.21.1 required three Home Assistant API migrations. All three are implemented
in v2.2.0, together with four smaller corrections found during verification.

The single most consequential finding is not any individual migration:

> **The three migrations collectively raise the minimum supported Home Assistant
> version from 2024.1 to 2026.8** — roughly two and a half years of releases.
> `via_device_id` does not exist before 2026.8, and passing it to an older
> device registry raises `TypeError`, so **no circuit entity is created at all**.

The predecessor review recommended all three changes without noting this. Shipping
them while `hacs.json` still declared `2024.1.0` would have offered the release to
users on 2026.7 and broken their installation. The floor is now declared in
`hacs.json` **and** enforced at runtime.

### Verification method

Home Assistant 2026.9.0 requires Python 3.14 and could not be executed in the
audit environment, so its wheel was downloaded from PyPI and read directly.
Version boundaries were established by downloading and diffing the
`device_registry`, `config_entries` and `const` modules across releases:

| Wheel inspected | Used to establish |
|---|---|
| 2026.9.0 | current `DeviceInfo`, `OptionsFlowManager`, deprecation registry |
| 2026.8.0 | `via_device_id` first appears |
| 2026.7.0 | `via_device_id` absent; `async_get_or_create` has no `**kwargs` |
| 2026.5.4, 2026.2.3 | `UnitOfRatio` absent |
| 2025.8.3 | `OptionsFlowWithReload` first appears |
| 2025.1.4 | neither present |

No claim in this document rests on recollection or on the predecessor review.

---

## 2. Where the predecessor review did not survive verification

The review was directionally correct on *what* to change. Three of its
supporting claims were wrong, and one of its recommendations would have caused
the exact failure it warned against.

### 2.1 `PERCENTAGE` is not deprecated

The review rated this **P1** and stated Home Assistant "explicitly identified
`PERCENTAGE` as a legacy constant."

Home Assistant's deprecation machinery says otherwise. `homeassistant/const.py`
contains exactly eight `_DEPRECATED_*` entries:

```
ATTR_VIA_DEVICE
CONCENTRATION_GRAMS_PER_CUBIC_METER
CONCENTRATION_MICROGRAMS_PER_CUBIC_FOOT
CONCENTRATION_MICROGRAMS_PER_CUBIC_METER
CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER
CONCENTRATION_PARTS_PER_BILLION
CONCENTRATION_PARTS_PER_CUBIC_METER
CONCENTRATION_PARTS_PER_MILLION
```

`PERCENTAGE` is not among them. It is a live constant:

```python
PERCENTAGE: Final = UnitOfRatio.PERCENTAGE.value
```

The `CONCENTRATION_PARTS_PER_*` constants *are* deprecated in favour of
`UnitOfRatio`, which is the likely origin of the generalisation.

**Disposition:** the migration was applied anyway — it is zero-risk and matches
the file's existing enum usage — but it is recorded as **style alignment, not a
compatibility fix**. Nothing would have broken had it been skipped.

### 2.2 The options-flow conflict is already an error, not a 2026.12 change

The review stated the listener + reload combination "becomes an error" from
2026.12. `OptionsFlowManager.async_finish_flow` in 2026.9 (present since 2025.8):

```python
if automatic_reload and entry.update_listeners:
    raise ValueError("Config entry update listeners should not be used with OptionsFlowWithReload")
```

**Consequence for implementation:** the two edits are atomic. The review lists
them as separate rows in its table, which invites a half-migration — switching
the base class while leaving the listener in place does not degrade gracefully,
it makes **every options save raise**.

### 2.3 The proposed `via_device_id` signature would orphan every device

The review proposed replacing:

```python
circuit_device_info(plant_id, circuit_data)
```

with:

```python
circuit_device_info(plant_device_id, circuit_data)
```

Circuit identifiers are built from `plant_id`:

```python
identifiers = {(DOMAIN, f"{plant_id}_{circuit_data.path}")}
```

Dropping `plant_id` rewrites every circuit identifier from `plant1_1/1` to
`dev3_1/1`. Home Assistant matches existing devices on identifiers, so each user
would receive a **second, parallel device tree** and lose entity history —
precisely the outcome the review's own §18 says to avoid.

**Disposition:** the implemented signature takes both values. `plant_id` remains
identity; `plant_device_id` is only the parent link. A dedicated regression test
(`test_circuit_identifier_still_derives_from_plant_id_not_device_id`) pins this,
and the mutation harness confirms it fails when the change is made.

### 2.4 The review's §8 dismissed a genuine 2026.12 break

§8 examined `async_update_reload_and_abort()` in the reauth flow and concluded
"there is no need to replace it," treating it as unrelated to the options flow.

`config_entries.py` (2026.9), inside `async_update_reload_and_abort`:

```python
if entry.update_listeners:
    report_usage(
        "has an update listener and should use it for scheduling a reload",
        core_behavior=ReportBehavior.LOG,
        breaks_in_ha_version="2026.12.0",
        integration_domain=self.handler,
    )
```

This is the integration's **only** exposure to a 2026.12-scheduled break, and it
is triggered by the update listener rather than by the reauth call itself.
Removing the listener (§3.1) resolves it; keeping `async_update_reload_and_abort`
is correct.

---

## 3. Migrations implemented

### 3.1 Options-flow lifecycle — P0

**Files:** `config_flow.py`, `__init__.py`

`HovalConnectOptionsFlow` now derives from `OptionsFlowWithReload`. The
config-entry update listener and `_async_options_updated()` are removed.

```
User saves options
      ↓
OptionsFlowWithReload.async_create_entry()
      ↓
HA compares options; reload scheduled only if they changed
      ↓
async_setup_entry()  →  _get_scan_interval()  →  coordinator.update_interval
```

Resolves both the live `ValueError` (§2.2) and the 2026.12 reauth break (§2.4).

**Behavioural change:** saving options now causes a brief reload rather than an
in-place timer adjustment. This is intentional and is the lifecycle HA prescribes;
it also removes the second, divergent configuration-update path.

### 3.2 Device parent relationship — P1

**Files:** `__init__.py`, `climate.py`, `fan.py`, `number.py`, `select.py`,
`sensor.py`, `water_heater.py`

`DeviceInfo` in HA 2026.9 no longer declares `via_device`; it declares
`via_device_id: str`. The keyword still functions through an explicit
`kwargs.pop()` in `async_get_or_create` (custom integrations get a `LOG`-level
report), with removal scheduled for **2027.8.0**.

A `HovalPlantDevices` resolver registers plant devices and caches their registry
IDs. Two properties matter:

1. **Registration precedes platform forwarding.** An unresolvable `via_device_id`
   raises `DeviceInfoError` and the entity is *dropped*; the old `via_device` only
   logged. Ordering is now load-bearing and is asserted by test.
2. **Plants are resolved on demand.** The platforms re-scan every plant on each
   `SIGNAL_NEW_CIRCUITS` dispatch, so a plant appearing after setup would
   otherwise have no registered parent.

**Identity is unchanged.** Plant identifiers remain `(DOMAIN, plant_id)`; circuit
identifiers remain `(DOMAIN, f"{plant_id}_{path}")`; all unique IDs are untouched.
Existing installations keep their devices, entities and history.

### 3.3 Percentage unit constant — cosmetic

**File:** `sensor.py` (9 descriptions)

`PERCENTAGE` → `UnitOfRatio.PERCENTAGE`. Both evaluate to `"%"`, and
`SensorDeviceClass.HUMIDITY` accepts either (`DEVICE_CLASS_UNITS` holds
`{UnitOfRatio.PERCENTAGE}`, and `StrEnum` compares equal to the plain string).
No unit change, so long-term statistics remain valid. See §2.1 for status.

---

## 4. Additional findings (not in the predecessor review)

| # | Finding | Action |
|---|---|---|
| A1 | Migration set raises the minimum HA version to 2026.8 | `hacs.json` floor + runtime guard |
| A2 | `async_update_reload_and_abort` + listener breaks in 2026.12 | resolved by §3.1 |
| A3 | Coordinator relied on the `current_entry` ContextVar | `config_entry` passed explicitly |
| A4 | `_enable_turn_on_off_backwards_compat` no longer exists in HA climate | removed |
| A5 | `AddEntitiesCallback` is not the config-entry type | → `AddConfigEntryEntitiesCallback` |
| A6 | Migrated files had 0% test coverage | 93 tests added |

### A1 — minimum version enforcement

HACS honours `hacs.json`, but a manual install bypasses it. Without a guard, a
user on 2026.7 sees `TypeError: async_get_or_create() got an unexpected keyword
argument 'via_device_id'` with nothing indicating the cause. `async_setup_entry`
now fails first, with:

> Hoval Connect requires Home Assistant 2026.8 or newer (running 2026.7). Upgrade
> Home Assistant, or install Hoval Connect v0.21.1, which supports older releases.

### A3 — coordinator config entry

For custom integrations HA reports this at `IGNORE`, so it produced no warning,
but the coordinator's `config_entry` attribute was populated from a ContextVar
that is only set during `async_setup_entry`. Passing it explicitly removes the
implicit dependency.

---

## 5. Deprecation sweep: everything scheduled to break by 2026.12

Rather than relying on a fixed checklist, the Home Assistant 2026.9 source was
scanned for every `breaks_in_ha_version` value up to and including 2026.12 in the
helpers, core, config-entry, loader and relevant platform modules, and each was
cross-checked against the integration.

| Deprecation | Breaks in | Present? |
|---|---|---|
| Update listener + `async_update_reload_and_abort` | 2026.12.0 | **Was — fixed (§3.1)** |
| Update listener + `OptionsFlowWithReload` | already `ValueError` | **Was — fixed (§3.1)** |
| `service.verify_domain_control(hass, …)` | 2026.10 | No |
| `service.async_extract_entity_ids(hass, …)` | 2026.10 | No |
| `service.async_extract_config_entry_ids(hass, …)` | 2026.10 | No |
| `service.async_extract_entities(hass, …)` | 2026.10 | No |
| `service.extract_entity_ids(hass, …)` | 2026.10 | No |
| `target.TargetSelection` deprecated class | 2026.12.0 | No |
| Non-string values passed to the device registry | 2026.12.0 | No |
| `DeviceEntry.suggested_area` property | 2026.9 | No |
| `suggested_area` in `async_update_device` | 2026.9.0 | No |
| `merge_connections` / `merge_identifiers` | 2027.x | No |
| `via_device` in `async_get_or_create` | 2027.8.0 | **Was — fixed (§3.2)** |
| `default_name` / `default_model` / `default_manufacturer` | 2027.9.0 | No |
| `created_at` / `modified_at` device-registry params | 2027.9.0 | No |
| `OptionsFlowWithConfigEntry` | phased out | No |
| Non-thread-safe operations (error for custom integrations) | — | No (fully async) |

**Result: after this release the integration has no known exposure to any Home
Assistant deprecation scheduled on or before 2026.12.** The next item to act on
is nothing until **2027.8**, and that surface is already migrated.

### Import-surface check

All **93** `homeassistant.*` symbols imported by the integration were resolved
against the real 2026.9.0 source. Nothing the integration imports has been
removed or renamed.

---

## 6. Testing

| Metric | v0.21.1 | v2.2.0 |
|---|---:|---:|
| Tests | 191 | **284** |
| Coverage | 44% | **61%** |
| Coverage of migrated files | **0%** | covered |
| Lint (ruff) | clean | clean |
| Mutations caught | — | **13 / 13** |

The migrated files — `config_flow.py`, all seven platforms, and the device-info
helpers — previously had **zero** test coverage. The 44% baseline came entirely
from `api.py` and `coordinator.py`.

### Test infrastructure

`tests/ha_stubs.py` provides realistic Home Assistant stand-ins so the entity
platforms and config flow are actually imported and exercised. MagicMock is
unusable here: subclassing a MagicMock turns the subclass into a mock, and two
MagicMock bases produce a metaclass conflict. The device-registry stub mirrors
HA 2026.9 behaviour — it rejects unknown keywords (including `via_device`) and
refuses an unregistered `via_device_id`.

### Coverage of the migration

- **Options flow:** base class, `automatic_reload`, listener absence, options
  persistence, interval re-read after reload, string coercion, garbage fallback.
- **Device identity:** plant and circuit identifiers, name/model/manufacturer,
  and an explicit regression guard against the §2.3 signature.
- **Device contract:** `via_device_id` present, `via_device` absent, and every
  emitted key validated against the real 2026.9 `DeviceInfo` key set.
- **Plant resolver:** registration, caching across repeated dispatches, plant
  independence, late-appearing plants, config-entry ID, end-to-end parent link,
  and rejection of an unregistered parent.
- **Platform wiring:** every circuit platform resolves and forwards the device
  ID; every circuit entity constructor accepts it; `binary_sensor` correctly
  left alone.
- **Units:** exactly 9 percentage descriptions, all `UnitOfRatio.PERCENTAGE`,
  string value unchanged.
- **Version floor:** accepted and rejected versions, error message content, call
  ordering (verified by AST, not substring), and agreement between the runtime
  constant and `hacs.json`.
- **Deprecation guards:** 14 banned symbols scanned across the component, with
  comments and docstrings stripped so documentation naming a removed API does not
  trip the guard.

### Mutation testing

A green suite is only meaningful if it turns red when the work is undone.
Thirteen reversions were applied one at a time; **all thirteen were caught**:

```
CAUGHT  revert via_device_id -> via_device
CAUGHT  revert OptionsFlowWithReload -> OptionsFlow
CAUGHT  reintroduce the config entry update listener
CAUGHT  break circuit identifier (use device id, as the review proposed)
CAUGHT  revert UnitOfRatio.PERCENTAGE -> PERCENTAGE
CAUGHT  lower the HACS minimum HA version below 2026.8
CAUGHT  register plant devices after forwarding platforms
CAUGHT  drop plant_device_id from a platform (sensor)
CAUGHT  revert manifest version to 0.21.1
CAUGHT  restore removed climate backwards-compat attribute
CAUGHT  lower the runtime HA version floor
CAUGHT  remove the runtime HA version guard entirely
CAUGHT  drop explicit config_entry from the coordinator
```

One mutation initially **survived**: removing the `_check_ha_version()` call
while leaving its definition in place. The test used a substring search that the
function definition itself satisfied. It was rewritten to inspect the AST of
`async_setup_entry`. This is recorded because it is the harness demonstrating
its own value.

---

## 7. Upgrade guidance

### Before upgrading

Confirm Home Assistant is **2026.8.0 or newer**. On anything older, remain on
v0.21.1 — HACS will not offer v2.2.0, and a manual install will refuse to load.

### Expected outcome

- Device tree unchanged; the plant/circuit hierarchy is preserved, now expressed
  through `via_device_id`.
- No new or duplicate devices.
- All entity IDs, unique IDs and units unchanged; statistics continue.
- No entity- or device-registry migration required.

### Post-upgrade verification

1. Plant device present, with circuits nested beneath it.
2. Device count unchanged from before the upgrade.
3. Percentage sensors still report `%`, with unbroken history.
4. Change an option and confirm the entry reloads and the new value applies.
5. With deprecation warnings enabled, confirm no `hoval_connect` warnings for:
   `via_device`, config-entry update listener, `OptionsFlow`,
   `DeviceEntry.config_entries`, `primary_config_entry`, `default_name`,
   `default_model`, `default_manufacturer`.

### Rollback

v2.2.0 makes no schema, identifier or storage changes, so downgrading to v0.21.1
restores prior behaviour. The device registry retains the `via_device_id` link,
which v0.21.1 re-establishes through its `via_device` tuple — the same parent
device either way.

---

## 8. Residual risk

| Risk | Severity | Notes |
|---|---|---|
| No execution against a live HA 2026.9 instance | Medium | HA 2026.9 needs Python 3.14, unavailable in the audit environment. Verification was static-plus-stub. Recommend one manual smoke test on a real instance before wide release. |
| Users below HA 2026.8 cannot take this release | Accepted | Deliberate, guarded twice, documented. v0.21.1 remains viable until 2027.8. |
| `via_device_id` semantics vs. child devices | Low | HA 2026.8 also introduced `parent_device_id` / `async_get_or_create_child` for sub-devices. Circuits are modelled as hub-attached devices, matching prior behaviour. Switching to child devices would be a behavioural change and is deliberately out of scope. |
| Platform entity logic still lightly covered | Low | Coverage rose from 0% to ~30–39% per platform, concentrated on the migration. Entity state logic remains largely untested — a candidate for a follow-up release. |

---

## 9. Verdict

All three migrations are complete, plus four corrections the original review did
not identify. The integration has **no known exposure to any Home Assistant
deprecation scheduled on or before 2026.12**, and the next relevant date is
2027.8, for which it is already migrated.

The migration's real risk was never the new APIs — it was silent identity drift
producing duplicate devices. That risk is closed by construction and pinned by
tests that are themselves verified to fail when the construction is undone.
