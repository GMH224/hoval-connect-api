"""Mutation testing for the v2.2.0 migration guards.

A green suite only means something if it turns red when the migration is undone.
Each mutation below reverts one part of the migration; the suite must fail for
every one of them, and the tree is restored afterwards.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BUILD = Path(__file__).resolve().parent.parent
PYTEST = sys.executable

# name -> (relative file, find, replace)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "revert via_device_id -> via_device",
        "custom_components/hoval_connect/__init__.py",
        "via_device_id=plant_device_id,",
        "via_device=(DOMAIN, plant_id),",
    ),
    (
        "revert OptionsFlowWithReload -> OptionsFlow",
        "custom_components/hoval_connect/config_flow.py",
        "class HovalConnectOptionsFlow(OptionsFlowWithReload):",
        "class HovalConnectOptionsFlow(OptionsFlow):",
    ),
    (
        "reintroduce the config entry update listener",
        "custom_components/hoval_connect/__init__.py",
        "    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)",
        "    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)\n"
        "    entry.async_on_unload(entry.add_update_listener(_x))",
    ),
    (
        "break circuit identifier (use device id, as the review proposed)",
        "custom_components/hoval_connect/__init__.py",
        'identifiers={(DOMAIN, f"{plant_id}_{circuit_data.path}")},',
        'identifiers={(DOMAIN, f"{plant_device_id}_{circuit_data.path}")},',
    ),
    (
        "revert UnitOfRatio.PERCENTAGE -> PERCENTAGE",
        "custom_components/hoval_connect/sensor.py",
        "native_unit_of_measurement=UnitOfRatio.PERCENTAGE,",
        "native_unit_of_measurement=PERCENTAGE,",
    ),
    (
        "lower the HACS minimum HA version below 2026.8",
        "hacs.json",
        '"homeassistant": "2026.8.0"',
        '"homeassistant": "2024.1.0"',
    ),
    (
        "register plant devices after forwarding platforms",
        "custom_components/hoval_connect/__init__.py",
        "    for plant_id, plant_data in coordinator.data.plants.items():\n"
        "        plant_devices.async_get_device_id(plant_id, plant_data)\n\n"
        "    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)",
        "    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)\n\n"
        "    for plant_id, plant_data in coordinator.data.plants.items():\n"
        "        plant_devices.async_get_device_id(plant_id, plant_data)",
    ),
    (
        "drop plant_device_id from a platform (sensor)",
        "custom_components/hoval_connect/sensor.py",
        "circuit_device_info(plant_id, plant_device_id, circuit_data)",
        "circuit_device_info(plant_id, plant_device_id if False else plant_id, circuit_data)",
    ),
    (
        "revert manifest version to 0.21.1",
        "custom_components/hoval_connect/manifest.json",
        '"version": "2.2.0"',
        '"version": "0.21.1"',
    ),
    (
        "restore removed climate backwards-compat attribute",
        "custom_components/hoval_connect/climate.py",
        "    _attr_hvac_modes = ",
        "    _enable_turn_on_off_backwards_compat = False\n    _attr_hvac_modes = ",
    ),
    (
        "lower the runtime HA version floor below via_device_id availability",
        "custom_components/hoval_connect/__init__.py",
        "MIN_HA_VERSION = (2026, 8)",
        "MIN_HA_VERSION = (2024, 1)",
    ),
    (
        "remove the runtime HA version guard entirely",
        "custom_components/hoval_connect/__init__.py",
        "    _check_ha_version()\n\n",
        "",
    ),
    (
        "drop explicit config_entry from the coordinator",
        "custom_components/hoval_connect/coordinator.py",
        "            config_entry=config_entry,\n",
        "",
    ),
]


def run_suite() -> bool:
    """Return True if the suite passes."""
    result = subprocess.run(
        [PYTEST, "-m", "pytest", "-q", "--no-header", "-x", "-p", "no:cacheprovider"],
        cwd=BUILD,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def main() -> int:
    backup = Path(tempfile.mkdtemp()) / "build"
    shutil.copytree(BUILD, backup)

    print("baseline: ", end="", flush=True)
    if not run_suite():
        print("FAIL — suite is red before mutation")
        return 1
    print("pass\n")

    survivors: list[str] = []
    for name, rel, find, replace in MUTATIONS:
        target = BUILD / rel
        original = target.read_text()
        if find not in original:
            print(f"  SKIP  {name} (anchor not found in {rel})")
            survivors.append(f"{name} [anchor missing]")
            continue
        target.write_text(original.replace(find, replace, 1))
        caught = not run_suite()
        target.write_text(original)
        print(f"  {'CAUGHT' if caught else 'SURVIVED'}  {name}")
        if not caught:
            survivors.append(name)

    shutil.rmtree(backup.parent, ignore_errors=True)

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) survived — tests are not sufficient:")
        for s in survivors:
            print("  - " + s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
