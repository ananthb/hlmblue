"""GATT over an ESPHome Bluetooth proxy.

A local adapter is not always good enough. The unit pushes its status as
41-byte notifications, and a BlueZ link that stays at the ATT MTU of 23 carries
only its 11-byte ``Diag:`` chatter: the link connects, the login is written, the
unit accepts both, and no status ever arrives. An ESPHome proxy negotiates a
large MTU (247 on this unit), so the same frames come through.

Home Assistant only routes a BLE connection through a proxy when the proxy's
scanner has seen the device, and its manager keeps remote scanners idle while a
local adapter is reporting advertisements for the same address. So the
integration talks to the proxy itself, over the API socket.

It opens its own connection rather than borrowing the one the ESPHome
integration holds. Holding the advertisement subscription above is what makes
the proxy scan, and it has to stay open for the life of the link; that
subscription is the one Home Assistant's own scanner wants, so it does not
belong on a connection Home Assistant owns. Keeping our own also means the
entry survives an ESPHome entry reload, and that a scanner Home Assistant has
paused (it keeps them idle while a local adapter sees the same address) is not
our problem. The host and Noise PSK come from the ESPHome config entry, so it
still costs the user nothing to set up.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import protocol as p

if TYPE_CHECKING:
    from aioesphomeapi import APIClient

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
CCCD_NOTIFY = b"\x01\x00"
CCCD_INDICATE = b"\x02\x00"

# Long enough for an A/C that is busy answering its own remote, short enough
# that a first refresh does not hang the config entry.
ADVERT_TIMEOUT = 20.0
CONNECT_TIMEOUT = 30.0


class ProxyUnavailable(Exception):
    """No proxy could reach the unit, or the link did not come up."""


@dataclass(frozen=True)
class Proxy:
    """An ESPHome device that can hold a BLE connection."""

    entry_id: str
    name: str
    host: str
    port: int
    noise_psk: str | None
    feature_flags: int


@dataclass(frozen=True)
class GattMap:
    """Handle numbers for the three characteristics we use, and their CCCDs."""

    write: int
    notify: int
    notify_cccd: int | None
    indicate: int | None
    indicate_cccd: int | None


def gatt_map(services) -> GattMap:
    """Pick the unit's characteristics out of a discovered GATT table.

    Raises ProxyUnavailable when the table does not have the pair the protocol
    needs, which is what a proxy that connected to something else would give.
    """
    handles: dict[str, int] = {}
    cccds: dict[str, int] = {}
    for service in services:
        for char in getattr(service, "characteristics", []):
            if char.uuid not in (p.CHAR_CMD, p.CHAR_NOTIFY, p.CHAR_INDICATE):
                continue
            handles[char.uuid] = char.handle
            for desc in getattr(char, "descriptors", []):
                if desc.uuid == CCCD_UUID:
                    cccds[char.uuid] = desc.handle

    if p.CHAR_CMD not in handles or p.CHAR_NOTIFY not in handles:
        raise ProxyUnavailable("the unit's GATT table did not appear over the proxy")
    return GattMap(
        write=handles[p.CHAR_CMD],
        notify=handles[p.CHAR_NOTIFY],
        notify_cccd=cccds.get(p.CHAR_NOTIFY),
        indicate=handles.get(p.CHAR_INDICATE),
        indicate_cccd=cccds.get(p.CHAR_INDICATE),
    )


def proxies(hass: HomeAssistant) -> list[Proxy]:
    """Every ESPHome device Home Assistant is connected to that proxies BLE."""
    try:
        from homeassistant.components.esphome.const import DOMAIN as ESPHOME_DOMAIN
    except ImportError:  # ESPHome integration not loaded at all
        return []

    found: list[Proxy] = []
    for entry in hass.config_entries.async_entries(ESPHOME_DOMAIN):
        data = getattr(entry, "runtime_data", None)
        info = getattr(data, "device_info", None)
        if info is None:
            continue
        # Only devices that actually advertised proxy support; there are other
        # ESPHome devices on this network that only report power.
        flags = getattr(info, "bluetooth_proxy_feature_flags", 0) or 0
        if not flags:
            continue
        host = entry.data.get("host")
        if not host:
            continue
        found.append(
            Proxy(
                entry_id=entry.entry_id,
                name=entry.title,
                host=host,
                port=int(entry.data.get("port") or 6053),
                noise_psk=entry.data.get("noise_psk") or None,
                feature_flags=flags,
            )
        )
    return found


class ProxySession:
    """Our own API connection to one ESPHome proxy, kept across reconnects.

    The proxy side of the link is cheap to hold open and expensive to rebuild,
    so the session outlives individual BLE links and only reconnects when the
    proxy itself drops us (a restart, usually).
    """

    def __init__(self, proxy: Proxy) -> None:
        self.proxy = proxy
        self._client: APIClient | None = None
        self._feature_flags = proxy.feature_flags
        self._lock = asyncio.Lock()
        self._waiters: dict[int, asyncio.Future] = {}
        self._unsub_adv = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def feature_flags(self) -> int:
        return self._feature_flags

    async def async_client(self) -> APIClient:
        """The connected client, opening a fresh connection if it went away."""
        async with self._lock:
            if self.is_connected:
                return self._client
            await self._async_drop()
            from aioesphomeapi import APIClient

            client = APIClient(
                self.proxy.host, self.proxy.port, noise_psk=self.proxy.noise_psk
            )
            try:
                await client.connect(login=True)
                info = await client.device_info()
            except Exception as err:  # noqa: BLE001 - any failure is "no proxy"
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001 - never came up
                    pass
                raise ProxyUnavailable(
                    f"could not reach {self.proxy.name} at {self.proxy.host}: {err}"
                ) from err
            self._client = client
            try:
                self._feature_flags = info.bluetooth_proxy_feature_flags_compat(
                    client.api_version
                )
            except Exception:  # noqa: BLE001 - keep the flags the entry had
                pass
            _LOGGER.debug(
                "connected to %s at %s (flags %s)",
                self.proxy.name,
                self.proxy.host,
                self._feature_flags,
            )
            return client

    def _on_advertisements(self, response) -> None:
        for advertisement in getattr(response, "advertisements", []):
            waiter = self._waiters.get(advertisement.address)
            if waiter is not None and not waiter.done():
                waiter.set_result(advertisement)

    async def async_wait_for_advertisement(self, wanted: int, timeout: float):
        """Wait for one address to turn up in this proxy's scan.

        Returns the advertisement, or ``None`` if it never showed up. The
        subscription is kept for the life of the session: the proxy has to be
        scanning both when it is asked to connect (it has nothing to find
        otherwise, and never answers the request) and while the link is up (a
        GATT discovery sent after the scan stopped never answers either).
        """
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[wanted] = waiter
        if self._unsub_adv is None:
            self._unsub_adv = self._client.subscribe_bluetooth_le_raw_advertisements(
                self._on_advertisements
            )
        try:
            return await asyncio.wait_for(waiter, timeout)
        except TimeoutError:
            return None
        finally:
            self._waiters.pop(wanted, None)

    async def async_close(self) -> None:
        async with self._lock:
            await self._async_drop()

    async def _async_drop(self) -> None:
        client, self._client = self._client, None
        unsub, self._unsub_adv = self._unsub_adv, None
        if unsub is not None:
            try:
                unsub()
            except Exception:  # noqa: BLE001 - the connection is going anyway
                pass
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as err:  # noqa: BLE001 - already gone
            _LOGGER.debug("closing %s: %s", self.proxy.name, err)


def address_to_int(address: str) -> int:
    return int(address.replace(":", "").replace("-", ""), 16)


class ProxyClient:
    """One open GATT link to the unit, through one ESPHome proxy."""

    def __init__(
        self,
        session: ProxySession,
        address: str,
        on_notify,
        on_disconnect,
    ) -> None:
        self._session = session
        self._proxy = session.proxy
        self._address = address.upper()
        self._on_notify = on_notify
        self._on_disconnect = on_disconnect
        self._cli: APIClient | None = None
        self._addr = 0
        self._gatt: GattMap | None = None
        self._connected = False
        self._unsub_connect = None
        self._unsub_notify: list = []

    @property
    def transport(self) -> str:
        return f"proxy:{self._proxy.name}"

    @property
    def is_connected(self) -> bool:
        return self._connected and self._gatt is not None and self._session.is_connected

    async def async_connect(self) -> None:
        """Find the unit on the proxy, connect, and subscribe to its status."""
        _LOGGER.debug("connect: opening session to %s", self._proxy.name)
        cli = self._cli = await self._session.async_client()
        _LOGGER.debug("connect: waiting for advertisement")
        advertisement = await self._session.async_wait_for_advertisement(
            address_to_int(self._address), ADVERT_TIMEOUT
        )
        _LOGGER.debug("connect: advertisement %s", advertisement is not None)
        if advertisement is None:
            raise ProxyUnavailable(
                f"{self._address} is not advertising to {self._proxy.name}"
            )
        self._addr = advertisement.address
        self._connected = False
        try:
            _LOGGER.debug("connect: bluetooth_device_connect start")
            self._unsub_connect = await cli.bluetooth_device_connect(
                self._addr,
                self._on_connection_state,
                timeout=CONNECT_TIMEOUT,
                feature_flags=self._session.feature_flags,
                # Ask the proxy to walk the GATT table rather than hand back a
                # cached one: we need the CCCD handles, which a cache without a
                # previous discovery would not have.
                has_cache=False,
                address_type=getattr(advertisement, "address_type", 0),
            )
            _LOGGER.debug("connect: bluetooth_device_connect done")
        except Exception as err:  # noqa: BLE001 - any failure is "no link"
            raise ProxyUnavailable(
                f"could not connect to {self._address} through {self._proxy.name}: {err}"
            ) from err

        try:
            _LOGGER.debug("connect: gatt services start")
            services = await cli.bluetooth_gatt_get_services(self._addr)
            _LOGGER.debug("connect: gatt services done")
            self._gatt = gatt_map(services.services)
        except ProxyUnavailable:
            await self.async_disconnect()
            raise
        except Exception as err:  # noqa: BLE001 - discovery failed
            await self.async_disconnect()
            raise ProxyUnavailable(
                f"could not read {self._address}'s GATT table through "
                f"{self._proxy.name}: {err}"
            ) from err

        _LOGGER.debug(
            "gatt over %s: write=%s notify=%s/%s indicate=%s/%s",
            self._proxy.name, self._gatt.write, self._gatt.notify,
            self._gatt.notify_cccd, self._gatt.indicate, self._gatt.indicate_cccd,
        )

        # The unit does not push until its CCCDs are written; ESPHome's
        # subscribe does that too, but it is what the vendor app does and the
        # unit is fussy about the order.
        for handle, value in (
            (self._gatt.notify_cccd, CCCD_NOTIFY),
            (self._gatt.indicate_cccd, CCCD_INDICATE),
        ):
            if handle is None:
                continue
            try:
                await cli.bluetooth_gatt_write_descriptor(
                    self._addr, handle, value, wait_for_response=True
                )
            except Exception as err:  # noqa: BLE001 - the notify sub retries it
                _LOGGER.debug("cccd %s on %s: %s", handle, self._proxy.name, err)

        try:
            unsub, _unsub_state = await cli.bluetooth_gatt_start_notify(
                self._addr, self._gatt.notify, self._on_gatt_notify
            )
            self._unsub_notify.append(unsub)
            if self._gatt.indicate is not None:
                try:
                    unsub, _unsub_state = await cli.bluetooth_gatt_start_notify(
                        self._addr, self._gatt.indicate, self._on_gatt_notify
                    )
                    self._unsub_notify.append(unsub)
                except Exception as err:  # noqa: BLE001 - indicate is optional
                    _LOGGER.debug("indicate on %s: %s", self._proxy.name, err)
        except Exception as err:  # noqa: BLE001 - a link without notify is useless
            await self.async_disconnect()
            raise ProxyUnavailable(
                f"could not subscribe to {self._address} through {self._proxy.name}: {err}"
            ) from err

        self._connected = True

    def _on_connection_state(self, connected: bool, mtu: int, error: int) -> None:
        self._connected = bool(connected)
        if connected:
            _LOGGER.debug("link up over %s, mtu %s", self._proxy.name, mtu)
            return
        _LOGGER.debug("link down over %s (error %s)", self._proxy.name, error)
        self._gatt = None
        self._on_disconnect()

    def _on_gatt_notify(self, _handle: int, data: bytearray) -> None:
        # The coordinator's handler is shared with the bleak path, which passes
        # the characteristic handle first; there is no handle to give here.
        self._on_notify(None, data)

    async def async_write(self, frame: bytes) -> None:
        if self._gatt is None or not self._connected or self._cli is None:
            raise ProxyUnavailable(f"no link to {self._address} over {self._proxy.name}")
        await self._cli.bluetooth_gatt_write(
            self._addr, self._gatt.write, frame, response=False
        )

    async def async_disconnect(self) -> None:
        for unsub in self._unsub_notify:
            try:
                await unsub()
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._unsub_notify.clear()
        if self._cli is not None:
            try:
                await self._cli.bluetooth_device_disconnect(self._addr)
            except Exception as err:  # noqa: BLE001 - already gone
                _LOGGER.debug("disconnect %s: %s", self._address, err)
        if self._unsub_connect is not None:
            try:
                self._unsub_connect()
            except Exception:  # noqa: BLE001
                pass
            self._unsub_connect = None
        self._connected = False
        self._gatt = None
        self._cli = None
