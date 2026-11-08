"""BLE connection + status coordinator for the A/C.

The unit is request/response and gated behind a passkey: on connect we subscribe
to the notify stream, send the passkey login, then a status query; thereafter
status arrives as pushed notifications and we poll periodically as a keepalive.

The link can be held by this host's Bluetooth adapter or by an ESPHome
Bluetooth proxy. A proxy is preferred by default because the unit's status
frames are 41 bytes and a BlueZ link left at the ATT MTU of 23 drops every one
of them -- see proxy.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import protocol as p
from .const import (
    CONF_PIN,
    CONF_PROXY,
    CONF_TRANSPORT,
    DEFAULT_PIN,
    DOMAIN,
    POLL_INTERVAL,
    POWER_SETTLE,
    RUNNING_WATTS,
    STATUS_MTU,
    STATUS_TIMEOUT,
    TRANSPORT_AUTO,
    TRANSPORT_LOCAL,
    TRANSPORT_PROXY,
)
from .power import PowerTracker
from .proxy import ProxyClient, ProxySession, ProxyUnavailable, proxies

_LOGGER = logging.getLogger(__name__)


class AcCoordinator(DataUpdateCoordinator[dict]):
    """Owns one BLE connection to one A/C and its decoded status."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, address: str, pin: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {address}",
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self.entry = entry
        self.address = address
        self.pin = pin
        self._client: BleakClientWithServiceCache | None = None
        self._proxy: ProxyClient | None = None
        self._proxy_session: ProxySession | None = None
        self._lock = asyncio.Lock()
        self._raw: dict[int, int | None] = {}
        self._power = PowerTracker(RUNNING_WATTS, POWER_SETTLE)
        # When last we had reason to think the unit is talking to us: a decoded
        # status frame, or a power-on it accepted. Silent-when-off is normal, so
        # this only decides availability while the unit is on.
        self._alive_at: float | None = None
        self._local_mtu_warned = False
        self._closing = False

    # ---- connection ----

    def _chosen_proxy(self):
        """The proxy to use, or None to use this host's adapter."""
        if self.entry.options.get(CONF_TRANSPORT, TRANSPORT_AUTO) == TRANSPORT_LOCAL:
            return None
        available = proxies(self.hass)
        wanted = self.entry.options.get(CONF_PROXY) or ""
        for proxy in available:
            if not wanted or proxy.name == wanted:
                return proxy
        return None

    @property
    def _is_connected(self) -> bool:
        if self._proxy is not None:
            return self._proxy.is_connected
        return self._client is not None and self._client.is_connected

    async def _ensure_connected(self) -> None:
        async with self._lock:
            await self._async_leave_local_for_proxy()
            if self._proxy is not None:
                if self._proxy.is_connected:
                    return
                await self._async_drop_proxy()
            elif self._client is not None and self._client.is_connected:
                return

            proxy = self._chosen_proxy()
            if proxy is not None:
                await self._async_connect_proxy(proxy)
            elif self.entry.options.get(CONF_TRANSPORT) == TRANSPORT_PROXY:
                raise UpdateFailed("no ESPHome Bluetooth proxy is connected")
            else:
                await self._async_connect_local()

            # Unlock, then ask for a first status report.
            await self._write(p.frame_login(self.pin))
            await asyncio.sleep(1.0)
            await self._write(p.frame_status())

    async def _async_leave_local_for_proxy(self) -> None:
        """Drop this host's adapter once a proxy is available in auto mode.

        Startup order is not fixed, so the first refresh can arrive before the
        ESPHome entry is connected and this host's adapter is all there is. That
        link cannot carry the unit's status frames, and while it holds the unit
        the unit stops advertising, so the proxy could never see it either.
        Leaving as soon as a proxy appears breaks that deadlock.
        """
        if self._proxy is not None or self._client is None:
            return
        if self.entry.options.get(CONF_TRANSPORT, TRANSPORT_AUTO) != TRANSPORT_AUTO:
            return
        if self._chosen_proxy() is None:
            return
        _LOGGER.debug(
            "%s: a proxy is up, leaving this host's adapter", self.address
        )
        await self._async_drop_local()

    async def _async_connect_proxy(self, proxy) -> None:
        session = self._proxy_session
        if session is None or session.proxy.entry_id != proxy.entry_id:
            if session is not None:
                await session.async_close()
            session = self._proxy_session = ProxySession(proxy)
        client = ProxyClient(
            session, self.address, self._on_notify, self._on_disconnect
        )
        try:
            await client.async_connect()
        except ProxyUnavailable as err:
            raise UpdateFailed(str(err)) from err
        self._alive_at = None
        self._proxy = client
        _LOGGER.debug("%s is on %s", self.address, client.transport)

    async def _async_connect_local(self) -> None:
        device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            raise UpdateFailed(f"{self.address} not in range of any Bluetooth adapter/proxy")
        client = await establish_connection(
            BleakClientWithServiceCache, device, self.address, self._on_disconnect
        )
        await client.start_notify(p.CHAR_NOTIFY, self._on_notify)
        try:
            await client.start_notify(p.CHAR_INDICATE, self._on_notify)
        except Exception:  # noqa: BLE001 - indicate char is optional
            pass
        self._alive_at = None
        self._client = client
        self._check_local_mtu(client)

    def _check_local_mtu(self, client) -> None:
        """Warn once when this adapter cannot carry the unit's status frames.

        BlueZ leaves this link at the ATT MTU of 23: the connection succeeds,
        the login is accepted, and every 41-byte status frame is dropped, so the
        unit looks like it is off for ever. Nothing to retry -- the fix is to
        point the entry at an ESPHome Bluetooth proxy.
        """
        mtu = getattr(client, "mtu_size", None)
        if mtu is None or mtu >= STATUS_MTU or self._local_mtu_warned:
            return
        self._local_mtu_warned = True
        _LOGGER.warning(
            "%s connected over this host's adapter at ATT MTU %s, too small for "
            "its %s-byte status frames; the A/C cannot be read or controlled "
            "this way. Set the integration's transport to an ESPHome Bluetooth "
            "proxy (Settings -> Devices & services -> He A/C -> Configure).",
            self.address,
            mtu,
            STATUS_MTU - 3,
        )

    async def _async_drop_proxy(self) -> None:
        client, self._proxy = self._proxy, None
        if client is not None:
            await client.async_disconnect()

    async def _async_drop_local(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as err:  # noqa: BLE001 - already gone
                _LOGGER.debug("disconnect %s: %s", self.address, err)

    def _on_disconnect(self, client=None) -> None:
        """A link dropped; the next poll reconnects it.

        The proxy transport keeps its own state and calls this with no argument,
        so only touch the local client when that is the link that went away.
        """
        if client is not None and client is not self._client:
            return
        if client is not None:
            self._client = None
        elif self._proxy is None:
            self._client = None
        self._alive_at = None
        if not self._closing:
            self.async_set_updated_data(self._snapshot())

    async def _write(self, frame: bytes) -> None:
        try:
            if self._proxy is not None:
                await self._proxy.async_write(frame)
            elif self._client is not None:
                await self._client.write_gatt_char(p.CHAR_CMD, frame, response=False)
        except Exception:
            # A write that cannot go out means the link is finished, even if it
            # has not reported the drop yet. Let the next poll build a new one
            # rather than retrying into a dead handle for ever.
            if self._proxy is not None:
                await self._async_drop_proxy()
            else:
                await self._async_drop_local()
            raise

    def _on_notify(self, _sender, data: bytearray) -> None:
        if self._closing:
            return
        dps = p.decode_notify(bytes(data))
        if not dps:
            return
        self._alive_at = time.monotonic()
        # A torn frame whose checksum happens to land is still nonsense, so a
        # reading outside the unit's own range is dropped rather than stored,
        # leaving the last good one in place.
        self._raw.update({k: v for k, v in dps.items() if p.plausible(k, v)})
        self._power.report(self._raw.get(p.DP_POWER), self._raw.get(p.DP_POWER_W), time.monotonic())
        self.async_set_updated_data(self._snapshot())

    # ---- data ----

    @property
    def _local_mtu_ok(self) -> bool:
        mtu = getattr(self._client, "mtu_size", None)
        return mtu is None or mtu >= STATUS_MTU

    @property
    def _available(self) -> bool:
        """Whether the entity may be read and commanded.

        A live link is not enough. A BlueZ link that stays at the ATT MTU of 23
        carries only the unit's 11-byte Diag: chatter, drops every 41-byte
        status frame, and leaves the unit looking switched off for ever -- that
        was the original bug. An ESPHome proxy negotiates 247, so the MTU check
        only bites on this host's adapter.

        Silence is not a fault by itself: the unit stops pushing within a minute
        of switching off and does not answer a status request while off, so an
        idle unit and a stuck link look identical. Only a running unit that has
        gone quiet counts as broken, and it must stay available while off or the
        automations could never switch it back on.
        """
        if not self._is_connected:
            return False
        if self._proxy is None and not self._local_mtu_ok:
            return False
        if not self._power.is_on:
            return True
        return (
            self._alive_at is not None
            and time.monotonic() - self._alive_at < STATUS_TIMEOUT
        )

    def _snapshot(self) -> dict:
        r = self._raw
        watts = r.get(p.DP_POWER_W) or 0
        return {
            "available": self._available,
            "transport": self._proxy.transport if self._proxy is not None else (
                "local" if self._client is not None else None
            ),
            "power": self._power.is_on,
            "running": self._power.is_running,
            "temp": r.get(p.DP_TEMP),
            "mode": p.STATUS_MODE.get(r.get(p.DP_MODE)),
            "fan": p.FAN_REV.get(r.get(p.DP_FAN)),
            "swing": bool(r.get(p.DP_SWING_V)),
            "swing_h": bool(r.get(p.DP_SWING_H)),
            "room": r.get(p.DP_ROOM_TEMP),
            "watts": watts,
        }

    async def _async_update_data(self) -> dict:
        # Keepalive poll: reconnect if needed and re-request status.
        await self._ensure_connected()
        await self._write(p.frame_status())
        return self._snapshot()

    # ---- commands ----

    async def async_command(self, frame: bytes, *, power: bool | None = None) -> None:
        await self._ensure_connected()
        if power is not None:
            self._power.command(power, time.monotonic())
            if power:
                # The unit starts pushing within seconds of being switched on,
                # so start the "it is alive" clock now rather than going
                # unavailable for the gap between the command and its answer.
                self._alive_at = time.monotonic()
        await self._write(frame)
        await asyncio.sleep(0.4)
        await self._write(p.frame_status())
        self.async_set_updated_data(self._snapshot())

    async def async_set_passkey(self, new_pin: str) -> None:
        """Change the A/C's passkey (login with the current one, then send the new
        one — the vendor app's order-based flow) and remember it on the entry."""
        await self._ensure_connected()
        await self._write(p.frame_login(new_pin))
        await self._write(p.frame_status())
        self.pin = new_pin
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_PIN: new_pin}
        )

    async def async_reset_passkey(self) -> None:
        """Reset the A/C to the 0000 default."""
        await self.async_set_passkey(DEFAULT_PIN)

    async def async_shutdown(self) -> None:
        self._closing = True
        await super().async_shutdown()
        await self._async_drop_proxy()
        session, self._proxy_session = self._proxy_session, None
        if session is not None:
            await session.async_close()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
