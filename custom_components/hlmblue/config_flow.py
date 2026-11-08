"""Config flow for A/C Remote (BLE)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow

from .const import (
    CONF_ADDRESS,
    CONF_CONFIGURED,
    CONF_PIN,
    CONF_PROXY,
    CONF_TRANSPORT,
    DEFAULT_PIN,
    DOMAIN,
    TRANSPORT_AUTO,
    TRANSPORT_LOCAL,
    TRANSPORT_PROXY,
)
from .protocol import NAME_PREFIX
from .proxy import proxies


class AcConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for an A/C."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return AcOptionsFlow()

    def __init__(self) -> None:
        self._discovered: dict[str, str] = {}  # address -> name
        self._address: str | None = None
        self._name: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered over Bluetooth."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self._name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device.

        Leave the passkey blank and a random one is generated and set on the A/C.
        Fill it in only if the A/C already has a non-default passkey — that one is
        used as-is and left unchanged.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            pin = (user_input.get(CONF_PIN) or "").strip()
            if pin and not (len(pin) == 4 and pin.isdigit()):
                errors["base"] = "invalid_pin"
            else:
                data: dict[str, Any] = {CONF_ADDRESS: self._address}
                if pin:
                    # A known, already-set passkey: use it and don't overwrite it.
                    data[CONF_PIN] = pin
                    data[CONF_CONFIGURED] = True
                else:
                    # Blank: generate and set a random passkey on first setup.
                    data[CONF_PIN] = DEFAULT_PIN
                return self.async_create_entry(
                    title=self._name or self._address, data=data
                )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Optional(CONF_PIN, default=""): str}),
            errors=errors,
            description_placeholders={"name": self._name or self._address or ""},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick from discovered A/Cs (or none found)."""
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            self._name = self._discovered.get(self._address, self._address)
            await self.async_set_unique_id(self._address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_confirm()

        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current:
                continue
            if (info.name or "").startswith(NAME_PREFIX):
                self._discovered[info.address] = info.name or info.address

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered)}
            ),
        )


class AcOptionsFlow(OptionsFlow):
    """Where the link to the A/C comes from.

    A local adapter can connect and still deliver nothing: the unit's status
    frames are 41 bytes, and a BlueZ link stuck at the ATT MTU of 23 carries
    only its 11-byte Diag: chatter. An ESPHome proxy negotiates a large MTU, so
    "auto" prefers one and falls back to this host's adapter.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_TRANSPORT: user_input[CONF_TRANSPORT],
                    CONF_PROXY: user_input.get(CONF_PROXY) or "",
                }
            )

        # Keep a proxy the entry already names selectable even while it is
        # offline, so opening the form and saving it again cannot silently
        # switch the entry away from it.
        options = self.config_entry.options
        names = [proxy.name for proxy in proxies(self.hass)]
        saved = options.get(CONF_PROXY) or ""
        if saved and saved not in names:
            names.append(saved)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TRANSPORT,
                        default=options.get(CONF_TRANSPORT, TRANSPORT_AUTO),
                    ): vol.In(
                        {
                            TRANSPORT_AUTO: "Auto (a proxy if there is one)",
                            TRANSPORT_PROXY: "ESPHome proxy only",
                            TRANSPORT_LOCAL: "This host's adapter only",
                        }
                    ),
                    vol.Optional(CONF_PROXY, default=saved): vol.In(
                        [""] + names
                    ),
                }
            ),
            description_placeholders={
                "proxies": ", ".join(names) or "none",
            },
        )
