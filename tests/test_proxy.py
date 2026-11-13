"""The proxy transport's GATT mapping is checked against the handle layout
docs/gatt.json records for this unit, so a wrong constant is caught offline.

proxy.py imports Home Assistant only for type hints, but it does import its
sibling protocol module relatively. Load both as submodules of a throwaway
package so that works without pulling in the integration's __init__, which
needs Home Assistant itself.
"""

import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "custom_components" / "hlmblue"
GATT = json.loads((ROOT / "docs" / "gatt.json").read_text())


def _load():
    package = types.ModuleType("hlmblue_under_test")
    package.__path__ = [str(MODULE_DIR)]
    sys.modules[package.__name__] = package
    loaded = {}
    for name in ("protocol", "proxy"):
        full = f"{package.__name__}.{name}"
        spec = importlib.util.spec_from_file_location(full, MODULE_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded["protocol"], loaded["proxy"]


p, proxy = _load()

CCCD = proxy.CCCD_UUID


def _uuid(short: str) -> str:
    """0xB003 -> 0000b003-0000-1000-8000-00805f9b34fb."""
    return f"{int(short, 16):08x}-0000-1000-8000-00805f9b34fb"


def _services():
    """The unit's table as docs/gatt.json lists it, in one service."""
    characteristics = []
    for entry in GATT:
        descriptors = [
            SimpleNamespace(uuid=_uuid(uuid), handle=handle)
            for uuid, handle in entry.get("descriptors", [])
        ]
        characteristics.append(
            SimpleNamespace(
                uuid=_uuid(entry["char"]),
                handle=entry["handle"],
                descriptors=descriptors,
            )
        )
    return [SimpleNamespace(characteristics=characteristics)]


def test_maps_the_unit_handles():
    mapped = proxy.gatt_map(_services())
    assert mapped.write == 17
    assert mapped.notify == 19
    assert mapped.notify_cccd == 20
    assert mapped.indicate == 22
    assert mapped.indicate_cccd == 23


def test_address_to_int_is_the_ble_address():
    assert proxy.address_to_int("E8:8F:8E:01:98:69") == 0xE88F8E019869


def test_missing_characteristics_are_reported():
    with pytest.raises(proxy.ProxyUnavailable):
        proxy.gatt_map([SimpleNamespace(characteristics=[])])


def test_indicate_is_optional():
    only = [
        characteristic
        for characteristic in _services()[0].characteristics
        if characteristic.uuid != p.CHAR_INDICATE
    ]
    mapped = proxy.gatt_map([SimpleNamespace(characteristics=only)])
    assert mapped.indicate is None
    assert mapped.indicate_cccd is None


class _FakeClient:
    """Enough of an APIClient to count advertisement subscriptions."""

    def __init__(self) -> None:
        self.subscriptions = 0

    def subscribe_bluetooth_le_raw_advertisements(self, _callback):
        self.subscriptions += 1
        return lambda: None

    @property
    def is_connected(self) -> bool:
        return True


def _session():
    instance = proxy.ProxySession(
        proxy.Proxy(
            entry_id="01M0ZDKN39QE9EZQMN1KGPW8D9",
            name="rack-exhaust-fan",
            host="192.168.31.173",
            port=6053,
            noise_psk=None,
            feature_flags=255,
        )
    )
    client = _FakeClient()
    instance._client = client
    return instance, client


def test_notify_reaches_the_coordinator_with_a_sender():
    """The notify callback is shared with the bleak path, which passes a sender.

    Calling it with the data alone raised TypeError inside aioesphomeapi for
    every status frame, so nothing was ever decoded even though the frames
    themselves were arriving.
    """
    seen = []
    instance, _ = _session()

    def record(*args):
        seen.append(args)

    client = proxy.ProxyClient(instance, "E8:8F:8E:01:98:69", record, lambda: None)
    client._on_gatt_notify(19, bytearray(b"\x01\x02"))
    assert seen == [(None, bytearray(b"\x01\x02"))]


def test_the_scan_outlives_the_advertisement_wait():
    """The proxy must still be scanning after the connect and the discovery.

    A connect that has the proxy do its own discovery has nothing to find if
    the scan has stopped, and never answers; the same goes for the GATT
    discovery that follows it. So the subscription is per session, not per
    wait, and waiting twice must not subscribe twice.
    """
    import asyncio

    instance, client = _session()

    async def wait_twice():
        first = await instance.async_wait_for_advertisement(0xE88F8E019869, 0.01)
        second = await instance.async_wait_for_advertisement(0xE88F8E019869, 0.01)
        return first, second

    assert asyncio.run(wait_twice()) == (None, None)
    assert client.subscriptions == 1


class _SilentGatt:
    """An APIClient whose discovery never answers, the way a wedged proxy acts.

    It accepts the API connection and keeps scanning, so it looks healthy; only
    the BLE side is stuck and answers neither the connect nor the discovery.
    """

    def __init__(self) -> None:
        self.disconnected = False

    async def bluetooth_device_connect(self, *_args, **_kwargs):
        return lambda: None

    async def bluetooth_gatt_get_services(self, _address):
        await asyncio.sleep(3600)

    async def bluetooth_device_disconnect(self, _address):
        self.disconnected = True


def test_a_silent_gatt_discovery_fails_instead_of_hanging(monkeypatch):
    """Discovery must give up, not wait for ever.

    The coordinator only schedules its next poll once the update finishes, so an
    answer that never comes stopped polling for good and left the entity
    unavailable until the entry was reloaded.
    """
    monkeypatch.setattr(proxy, "GATT_TIMEOUT", 0.05)
    instance, _ = _session()
    client = _SilentGatt()
    instance._client = client
    instance.async_client = AsyncMock(return_value=client)
    instance.async_wait_for_advertisement = AsyncMock(
        return_value=SimpleNamespace(address=0xE88F8E019869, address_type=0)
    )
    link = proxy.ProxyClient(
        instance, "E8:8F:8E:01:98:69", lambda *args: None, lambda: None
    )

    with pytest.raises(proxy.ProxyUnavailable):
        asyncio.run(link.async_connect())
    assert client.disconnected
