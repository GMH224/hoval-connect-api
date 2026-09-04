# Verification tooling

Two auditing tools used to produce `docs/audit-v2.2.0.md`. Neither runs in CI —
both are for release-time verification.

## `mutation_check.py`

Reverts one part of the HA 2026.8 migration at a time and asserts the test suite
turns red for each. A green suite only means something if it fails when the work
is undone.

```bash
python tools/mutation_check.py
```

Every mutation must be reported as `CAUGHT`. A `SURVIVED` result means a guard is
missing or, as happened once during development, that a test matches source text
loosely enough to pass against a definition when the call site is gone.

The tree is restored after each mutation, but the script edits files in place —
run it on a clean checkout.

## `check_ha_import_surface.py`

Resolves every `homeassistant.*` symbol the integration imports against real
Home Assistant source, catching anything removed or renamed upstream.

```bash
# Fetch the target HA version without installing it (it needs Python 3.14):
pip download "homeassistant==2026.9.0" --no-deps --only-binary=:all: \
    --python-version 3.14.2 --implementation cp --abi none --platform any -d /tmp/ha
mkdir -p /tmp/ha_src && unzip -q /tmp/ha/*.whl -d /tmp/ha_src

HA_SOURCE_ROOT=/tmp/ha_src python tools/check_ha_import_surface.py \
    custom_components/hoval_connect
```

Recent Home Assistant uses syntax (PEP 696 type-parameter defaults) that older
Python cannot parse, so the checker falls back to a lexical scan when `ast` fails.
That is sufficient for symbol-existence checks but does not validate signatures —
verify those by reading the relevant HA source directly, as was done for
`via_device_id` and `OptionsFlowWithReload`.
