"""Config flow for Hoval Connect integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)

from .api import HovalApiError, HovalAuthError, HovalConnectApi
from .const import (
    CONF_HEALTH_CHECK_INTERVAL,
    CONF_OVERRIDE_DURATION,
    CONF_TURN_ON_MODE,
    DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    DEFAULT_OVERRIDE_DURATION,
    DEFAULT_TURN_ON_MODE,
    DOMAIN,
    DURATION_FOUR_HOURS,
    DURATION_MIDNIGHT,
    HEALTH_CHECK_INTERVAL_OPTIONS,
    TURN_ON_RESUME,
    TURN_ON_WEEK1,
    TURN_ON_WEEK2,
    VALID_OVERRIDE_DURATIONS,
    VALID_TURN_ON_MODES,
)

_LOGGER = logging.getLogger(__name__)

# Outer bound on credential validation (audit finding F3, v0.21.1). The API
# client's per-request timeouts do not bound the get_plants() pagination loop
# as a whole, and unlike the coordinator (90 s asyncio.timeout) the config
# flow previously had no outer guard at all — a byte-dripping or
# endlessly-paginating server could hang the setup dialog indefinitely.
_VALIDATION_TIMEOUT_S = 30

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("email"): str,
        vol.Required("password"): str,
    }
)


class HovalConnectConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hoval Connect."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow handler."""
        return HovalConnectOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # v0.24.0: HovalConnectApi takes hass, not an aiohttp session —
            # see api.py's module docstring. api.aclose() releases this
            # short-lived validation client's connection pool once done;
            # the real long-lived client created in async_setup_entry is
            # closed separately, in async_unload_entry.
            api = HovalConnectApi(self.hass, user_input["email"], user_input["password"])

            try:
                async with asyncio.timeout(_VALIDATION_TIMEOUT_S):
                    await api.get_plants()
            except TimeoutError:
                _LOGGER.warning("Hoval validation timed out after %d s", _VALIDATION_TIMEOUT_S)
                errors["base"] = "cannot_connect"
            except HovalAuthError as err:
                _LOGGER.warning("Hoval auth failed: %s", err)
                errors["base"] = "invalid_auth"
            except HovalApiError as err:
                _LOGGER.error("Hoval API error during setup: %s", err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input["email"].lower())
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=user_input["email"],
                    data={
                        "email": user_input["email"],
                        "password": user_input["password"],
                    },
                )
            finally:
                # ICS-HIGH-021 (audit v1.0.1): shielded — see the identical
                # fix/rationale in async_step_reauth_confirm() below.
                await asyncio.shield(api.aclose())

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauth when tokens are rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            # Pin reauth to the original account: a reauth must not silently
            # rebind the entry to a different Hoval login.
            if user_input["email"].lower() != (reauth_entry.unique_id or "").lower():
                errors["base"] = "wrong_account"
            else:
                api = HovalConnectApi(self.hass, user_input["email"], user_input["password"])

                try:
                    async with asyncio.timeout(_VALIDATION_TIMEOUT_S):
                        await api.get_plants()
                except TimeoutError:
                    _LOGGER.warning(
                        "Hoval reauth validation timed out after %d s",
                        _VALIDATION_TIMEOUT_S,
                    )
                    errors["base"] = "cannot_connect"
                except HovalAuthError:
                    errors["base"] = "invalid_auth"
                except HovalApiError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data={
                            "email": user_input["email"],
                            "password": user_input["password"],
                        },
                    )
                finally:
                    # ICS-HIGH-021 (audit v1.0.1): this `finally` can run
                    # while still inside the `asyncio.timeout()` scope above
                    # (e.g. right after it expired) — an unshielded await
                    # here could be cancelled again by that same expired
                    # deadline before the session actually closes.
                    # `asyncio.shield()` lets aclose()'s own bounded
                    # drain-then-close (ICS-CRIT-002, api.py) run to
                    # completion regardless.
                    await asyncio.shield(api.aclose())

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class HovalConnectOptionsFlow(OptionsFlowWithReload):
    """Handle options for Hoval Connect.

    OptionsFlowWithReload reloads the config entry whenever saved options differ
    from the stored ones, so async_setup_entry re-reads the scan interval, turn-on
    mode and override duration. This replaces the former config-entry update
    listener, which HA now rejects outright alongside a reloading options flow.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options.

        v1.0.0 initially removed the polling-interval field entirely, since
        there was no longer a meaningful "poll rate" to tune for circuit/
        program/settings data (still true — see docs/audit-v1.0.0.md and
        CHANGELOG.md). Reinstated here, in this same v1.0.0 release before
        deployment, at the user's explicit request as CONF_HEALTH_CHECK_INTERVAL:
        a narrower, differently-scoped setting that only controls the cadence
        of the one lightweight reachability check this integration still runs
        on a schedule — see _get_health_check_interval()'s docstring in
        __init__.py. Any config entry with an old scan_interval value stored
        from a pre-1.0.0 install is simply ignored (not migrated or deleted),
        since nothing reads that option key anymore.
        """
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # ICS-007 (independent audit, 2026-09, v1.0.1 round): every value
        # read here is persisted config-entry data — i.e. untrusted input
        # from this code's point of view, reachable via hand-edited
        # storage, a migration bug, or a build that offered different
        # choices. The audit reported the unguarded int() below; a sweep
        # for the same shape found all THREE reads needed treatment, not
        # one.
        #
        # The int() case is the loud one: a non-numeric persisted value
        # raises ValueError inside async_step_init, which crashes the
        # options dialog and leaves the user no UI route to correct the
        # bad value that caused it — precisely the state where they most
        # need the form to open.
        #
        # The other two fail quietly instead, and that shape has bitten
        # this project before: a `default=` that is not one of the
        # vol.In() keys renders the selector EMPTY rather than erroring
        # (see the v0.23.0 "Polling interval field renders empty" fix in
        # CHANGELOG.md). Falling back to the documented default keeps the
        # form usable and self-correcting — saving it writes a valid value
        # back.
        current_duration = self.config_entry.options.get(
            CONF_OVERRIDE_DURATION, DEFAULT_OVERRIDE_DURATION
        )
        if current_duration not in VALID_OVERRIDE_DURATIONS:
            current_duration = DEFAULT_OVERRIDE_DURATION

        current_turn_on = self.config_entry.options.get(CONF_TURN_ON_MODE, DEFAULT_TURN_ON_MODE)
        if current_turn_on not in VALID_TURN_ON_MODES:
            current_turn_on = DEFAULT_TURN_ON_MODE

        try:
            current_interval = int(
                self.config_entry.options.get(
                    CONF_HEALTH_CHECK_INTERVAL, DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
                )
            )
        except (TypeError, ValueError):
            current_interval = DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
        if current_interval not in HEALTH_CHECK_INTERVAL_OPTIONS:
            current_interval = DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TURN_ON_MODE,
                        default=current_turn_on,
                    ): vol.In(
                        {
                            TURN_ON_RESUME: "Resume time program",
                            TURN_ON_WEEK1: "Activate week 1",
                            TURN_ON_WEEK2: "Activate week 2",
                        }
                    ),
                    vol.Required(
                        CONF_OVERRIDE_DURATION,
                        default=current_duration,
                    ): vol.In(
                        {
                            DURATION_FOUR_HOURS: "4 hours",
                            DURATION_MIDNIGHT: "Until midnight",
                        }
                    ),
                    vol.Required(
                        CONF_HEALTH_CHECK_INTERVAL,
                        default=current_interval,
                    ): vol.All(vol.Coerce(int), vol.In(HEALTH_CHECK_INTERVAL_OPTIONS)),
                }
            ),
        )
